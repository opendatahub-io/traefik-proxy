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
import toml
import time

from traitlets import Any, default, Unicode, Bool

from . import traefik_utils
from jupyterhub.proxy import Proxy
from jupyterhub_traefik_proxy import TraefikProxy

from kubernetes import client, config
from kubernetes.client.rest import ApiException

class TraefikTomlConfigmapProxy(TraefikProxy):
    """JupyterHub Proxy implementation using traefik and toml config file stored in a configmap"""

    # disable proxy start/stop handling
    should_start = False

    mutex = Any()

    @default("mutex")
    def _default_mutex(self):
        return asyncio.Lock()

    v1 = None
    in_cluster = Bool(
        True, config=True, help="""Should proxy use the in-cluster kubernetes config?"""
    )

    cm_name = Unicode(
        "traefik-rules", config=True, help="""Name of configmap in which traefik will read the rules.toml file"""
    )

    cm_namespace = Unicode(
        "default", config=True, help="""Namespace in which the configmap for traefik-rules will be created and updated"""
    )

    def _get_route_unsafe(self, traefik_routespec):
        backend_alias = traefik_utils.generate_alias(
            traefik_routespec, "backend")
        frontend_alias = traefik_utils.generate_alias(
            traefik_routespec, "frontend")
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
            get_target_data(
                self.routes_cache["backends"][backend_alias], "url")

        if frontend_alias in self.routes_cache["frontends"]:
            get_target_data(
                self.routes_cache["frontends"][frontend_alias], "data")

        if not result["data"] and not result["target"]:
            self.log.info("No route for {} found!".format(routespec))
            result = None
        else:
            result["data"] = json.loads(result["data"])
        return result

    def _ensure_configmap(self):
        try:
            self.v1.read_namespaced_config_map(
                namespace=self.cm_namespace,
                name=self.cm_name,
            )

        except client.rest.ApiException as apiEx:
            if apiEx.reason == 'Not Found':
                print("Configmap not found, generating one")
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
                            "rules.toml": toml.dumps({"backends": {}, "frontends": {}})
                        },
                    ),
                )

            else:
                raise apiEx

    def _persist_routes_cache(self):
        '''
        This method persists the routes_cache dict to a configmap as a formatted TOML string.
        WARN: Only call this function while self.mutex is locked.
        '''
        self.v1.patch_namespaced_config_map(
            name=self.cm_name,
            namespace=self.cm_namespace,
            body=client.V1ConfigMap(
                api_version="v1",
                kind="ConfigMap",
                metadata=client.V1ObjectMeta(
                    name=self.cm_name,
                    namespace=self.cm_namespace,
                ),
                data={
                    "rules.toml": toml.dumps(self.routes_cache)
                },
            ),
        )

    async def _wait_for_route_in_traefik_pods(self, routespec):
        self.log.info("Waiting for %s to register with all traefik pods", routespec)

        # - resolve traefik svc/eps to pods
        # - hope that traefik svc endpoints didn't race in a new pod
        # - for each pod: run previous procedure

        endpoints = await self.v1.read_namespaced_endpoints(
            name=self.traefik_svc_name,
            namespace=self.traefik_svc_namespace,
            async_req=True
        )

        pod_ips = []
        for subset in endpoints.subsets:
            for address in subset.adresses:
                if address.targetRef['kind'] != "Pod":
                    continue
                pod_ips.append(address.ip)
        
        print("pod_ips", pod_ips)
        return

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.v1 = client.CoreV1Api()

        self._ensure_configmap()
        self.routes_cache = toml.loads(self.v1.read_namespaced_config_map(name=self.cm_name, namespace=self.cm_namespace).data['rules.toml'])

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
            self._persist_routes_cache()

        # dirty hack time!
        # ideally this is replaced by an implementation
        # that is aware of all traefik pods
        # and checks all of them for the route
        # racyness: after this function completes, jupyterhub assumes
        # that the route is avaiable when, in fact, it is maybe only
        # available in one of many traefik pods.
        # let's see if racyness can be "reasonably" avoided by adding
        # a delay and then checking for the routes multiple times

        print("check time")
        await self._wait_for_route_in_traefik_pods(routespec)

        time.sleep(15)
        try:
            for _ in range(10): await self._wait_for_route(routespec, provider="file")

        except TimeoutError:
            self.log.error(
                f"Is Traefik configured to watch the configmap {self.cm_name}?"
            )
            raise

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
            self._persist_routes_cache()

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
