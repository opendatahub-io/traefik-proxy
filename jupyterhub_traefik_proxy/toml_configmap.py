"""Traefik implementation

Custom proxy implementations can subclass :class:`Proxy`
and register in JupyterHub config:

.. sourcecode:: python

    from mymodule import MyProxy
    c.JupyterHub.proxy_class = MyProxy

Route Specification:

- A routespec is a URL prefix ([host]/path/), e.g.
  'host.tld/path/' for host-based routing or '/path/' for default routing.
- Route paths should be normalized to always start and end with '/'
"""

# Copyright (c) Jupyter Development Team. (Modifications made by Red Hat)
# Distributed under the terms of the Modified BSD License.

import json
import os
import asyncio
import string
import escapism
import kubernetes
from kubernetes.client.exceptions import ApiException

from traitlets import Any, default, Unicode

from . import traefik_utils
from jupyterhub.proxy import Proxy
from jupyterhub_traefik_proxy import TraefikProxy

from kubernetes import client, config

class TraefikTomlConfigmapProxy(TraefikProxy):
    """JupyterHub Proxy implementation using traefik and toml config file stored in a configmap"""

    # disable proxy start/stop handling
    should_start = False

    mutex = Any()

    @default("mutex")
    def _default_mutex(self):
        return asyncio.Lock()

    # TODO: remove
    toml_dynamic_config_file = Unicode(
        "rules.toml", config=True, help="""traefik's dynamic configuration file"""
    )
    
    v1 = None

    cm_name = "traefik-rules"
    cm_namespace = "default"


    def _ensure_configmap(self):
        exists = False
        try:
            self.v1.read_namespaced_config_map(
                namespace=self.cm_namespace,
                name=self.cm_name,
            )
            exists = True
        except ApiException as err:
            if err.status != "404":
                raise err
        
        if not exists:
            self.v1.create_namespaced_config_map(
                namespace=self.cm_namespace,
                body=client.V1ConfigMap(
                    api_version="v1",
                    kind="ConfigMap",
                    metadata=client.V1ObjectMeta(
                        name=self.cm_name,
                        namespace=self.cm_namespace,
                    ),
                    data={
                        "rules.toml": ""
                    },
                ),
            )

    def __init__(self, **kwargs):
        config.load_kube_config()
        # config.load_incluster_config()
        self.v1 = client.CoreV1Api()

        self._ensure_configmap()
        # cm = self.v1.read_namespaced_config_map(
        #     name="traefik-rules",
        #     namespace="default",
        # )
        # self.v1.replace_namespaced_config_map(
        #     name="traefik-rules",
        #     namespace="default",
        #     body={},
        # )

        super().__init__(**kwargs)
        try:
            # Load initial routing table from disk
            # TODO: rewrite to read from configmap (cache?)
            self.routes_cache = traefik_utils.load_routes(self.toml_dynamic_config_file)
        except FileNotFoundError:
            self.routes_cache = {}

        if not self.routes_cache:
            self.routes_cache = {"backends": {}, "frontends": {}}

    # TODO: rewrite to read from configmap (cache?)
    def _get_route_unsafe(self, traefik_routespec):
        backend_alias = traefik_utils.generate_alias(traefik_routespec, "backend")
        frontend_alias = traefik_utils.generate_alias(traefik_routespec, "frontend")
        routespec = self._routespec_from_traefik_path(traefik_routespec)
        result = {"data": "", "target": "", "routespec": routespec}

        def get_target_data(d, to_find):
            if to_find == "url":
                key = "target"
            else:
                key = to_find
            if result[key]:
                return
            for k, v in d.items():
                if k == to_find:
                    result[key] = v
                if isinstance(v, dict):
                    get_target_data(v, to_find)

        if backend_alias in self.routes_cache["backends"]:
            get_target_data(self.routes_cache["backends"][backend_alias], "url")

        if frontend_alias in self.routes_cache["frontends"]:
            get_target_data(self.routes_cache["frontends"][frontend_alias], "data")

        if not result["data"] and not result["target"]:
            self.log.info("No route for {} found!".format(routespec))
            result = None
        else:
            result["data"] = json.loads(result["data"])
        return result

    # TODO: rewrite to write to configmap (cache?)
    async def add_route(self, routespec, target, data):
        """Add a route to the proxy.

        **Subclasses must define this method**

        Args:
            routespec (str): A URL prefix ([host]/path/) for which this route will be matched,
                e.g. host.name/path/
            target (str): A full URL that will be the target of this route.
            data (dict): A JSONable dict that will be associated with this route, and will
                be returned when retrieving information about this route.

        Will raise an appropriate Exception (FIXME: find what?) if the route could
        not be added.

        The proxy implementation should also have a way to associate the fact that a
        route came from JupyterHub.
        """
        routespec = self._routespec_to_traefik_path(routespec)
        backend_alias = traefik_utils.generate_alias(routespec, "backend")
        frontend_alias = traefik_utils.generate_alias(routespec, "frontend")
        data = json.dumps(data)
        rule = traefik_utils.generate_rule(routespec)

        async with self.mutex:
            self.routes_cache["frontends"][frontend_alias] = {
                "backend": backend_alias,
                "passHostHeader": True,
                "routes": {"test": {"rule": rule, "data": data}},
            }

            self.routes_cache["backends"][backend_alias] = {
                "servers": {"server1": {"url": target, "weight": 1}}
            }
            traefik_utils.persist_routes(
                self.toml_dynamic_config_file, self.routes_cache
            )

        if self.should_start:
            try:
                # Check if traefik was launched
                pid = self.traefik_process.pid
            except AttributeError:
                self.log.error(
                    "You cannot add routes if the proxy isn't running! Please start the proxy: proxy.start()"
                )
                raise
        try:
            await self._wait_for_route(routespec, provider="file")
        except TimeoutError:
            self.log.error(
                f"Is Traefik configured to watch {self.toml_dynamic_config_file}?"
            )
            raise

    # TODO: rewrite to write to configmap (cache?)
    async def delete_route(self, routespec):
        """Delete a route with a given routespec if it exists.

        **Subclasses must define this method**
        """
        routespec = self._routespec_to_traefik_path(routespec)
        backend_alias = traefik_utils.generate_alias(routespec, "backend")
        frontend_alias = traefik_utils.generate_alias(routespec, "frontend")

        async with self.mutex:
            self.routes_cache["frontends"].pop(frontend_alias, None)
            self.routes_cache["backends"].pop(backend_alias, None)

        traefik_utils.persist_routes(self.toml_dynamic_config_file, self.routes_cache)

    # TODO: rewrite to read from configmap (cache?)
    async def get_all_routes(self):
        """Fetch and return all the routes associated by JupyterHub from the
        proxy.

        **Subclasses must define this method**

        Should return a dictionary of routes, where the keys are
        routespecs and each value is a dict of the form::

          {
            'routespec': the route specification ([host]/path/)
            'target': the target host URL (proto://host) for this route
            'data': the attached data dict for this route (as specified in add_route)
          }
        """
        all_routes = {}

        async with self.mutex:
            for key, value in self.routes_cache["frontends"].items():
                escaped_routespec = "".join(key.split("_", 1)[1:])
                traefik_routespec = escapism.unescape(escaped_routespec)
                routespec = self._routespec_from_traefik_path(traefik_routespec)
                all_routes[routespec] = self._get_route_unsafe(traefik_routespec)

        return all_routes

    # TODO: rewrite to read from configmap (cache?)
    async def get_route(self, routespec):
        """Return the route info for a given routespec.

        Args:
            routespec (str):
                A URI that was used to add this route,
                e.g. `host.tld/path/`

        Returns:
            result (dict):
                dict with the following keys::

                'routespec': The normalized route specification passed in to add_route
                    ([host]/path/)
                'target': The target host for this route (proto://host)
                'data': The arbitrary data dict that was passed in by JupyterHub when adding this
                        route.

            None: if there are no routes matching the given routespec
        """
        routespec = self._routespec_to_traefik_path(routespec)
        async with self.mutex:
            return self._get_route_unsafe(routespec)
