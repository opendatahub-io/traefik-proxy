from jupyterhub_traefik_proxy import TraefikTomlConfigmapProxy

c.JupyterHub.proxy_class = TraefikTomlConfigmapProxy
# mark the proxy as externally managed

c.TraefikTomlConfigmapProxy.traefik_api_url = "http://localhost:30099"
# traefik api endpoint login password
c.TraefikTomlConfigmapProxy.traefik_api_password = "123"
# traefik api endpoint login username
c.TraefikTomlConfigmapProxy.traefik_api_username = "abc"
# c.JupyterHub.services[0]['url'] = 'http://jupyterhub:8181'

c.TraefikTomlConfigmapProxy.cm_namespace = "redhat-ods-applications"
c.TraefikTomlConfigmapProxy.cm_name = "traefik-rules"
c.TraefikTomlConfigmapProxy.in_cluster = True