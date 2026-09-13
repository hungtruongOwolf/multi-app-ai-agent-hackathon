import json

import httpx

from judge.connectors.shoplab import ShopLabControl
from judge.connectors.transport import HttpClient


async def test_control_client_paths_and_token(settings):
    seen = []

    async def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, request.headers.get("x-control-token"),
                     json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"prev": 1, "value": 2})

    async with HttpClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as http:
        c = ShopLabControl(settings, http, base_url="http://sup")
        await c.set_flag("checkout", "payment_v2", False)
        await c.set_pool_size("checkout", 20)
        await c.deploy("checkout", "1.4.1")
        await c.restart("search")
        await c.get_config("checkout")
    assert seen == [
        ("PUT", "/config/checkout/flags/payment_v2", "dev-control-token", {"value": False}),
        ("PUT", "/config/checkout/pool_size", "dev-control-token", {"size": 20}),
        ("POST", "/deploy/checkout", "dev-control-token", {"version": "1.4.1"}),
        ("POST", "/services/search/restart", "dev-control-token", None),
        ("GET", "/config/checkout", "dev-control-token", None),
    ]
