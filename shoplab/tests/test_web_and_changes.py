"""Change log, storefront and control-room routes (no service processes: lifespan is not started)."""

import json

import httpx
from fastapi.testclient import TestClient

from judge.connectors.shoplab import ShopLabControl
from judge.connectors.transport import HttpClient
from shoplab.catalog_data import PRODUCTS, seed_rows
from shoplab.changes import ChangeLog
from shoplab.supervisor import Supervisor, create_app

TOKEN = {"X-Control-Token": "t0k"}


def make_client(tmp_path):
    sup = Supervisor(port_base=9950, control_token="t0k", data=tmp_path, traffic_enabled=False)
    return sup, TestClient(create_app(sup))  # no `with`: lifespan (process spawning) does not run


def test_changelog_persists_filters_and_diffs(tmp_path):
    log = ChangeLog(tmp_path / "changes.jsonl")
    log.record(service="checkout", kind="flag", actor="release-bot", summary="payment_v2: false → true")
    log.record(service="search", kind="restart", actor="incident-judge", summary="process restarted")
    reloaded = ChangeLog(tmp_path / "changes.jsonl")
    assert [c["service"] for c in reloaded.list()] == ["checkout", "search"]
    assert [c["kind"] for c in reloaded.list(service="search")] == ["restart"]
    assert reloaded.list(since="2999-01-01T00:00:00Z") == []
    diff = log.record_config_diff("checkout", {"flags": {"payment_v2": True}, "pool_size": 2, "app_version": "1.4.2"},
                                  {"flags": {"payment_v2": False}, "pool_size": 10, "app_version": "1.4.1"}, "operator")
    assert [d["summary"] for d in diff] == ["payment_v2: true → false", "DB pool size: 2 → 10",
                                            "deployed 1.4.1 (was 1.4.2)"]


def test_control_changes_record_actor_and_release_bot_faults(tmp_path):
    sup, c = make_client(tmp_path)
    assert c.post("/faults", json={"fault": "bad_flag"}, headers=TOKEN).status_code == 200
    assert c.post("/faults", json={"fault": "slow_query"}, headers=TOKEN).status_code == 200  # infra: not a change
    r = c.put("/config/checkout/flags/payment_v2", json={"value": False}, headers={**TOKEN, "X-Actor": "incident-judge"})
    assert r.json() == {"prev": True, "value": False}
    c.put("/config/checkout/pool_size", json={"size": 20}, headers=TOKEN)
    c.put("/config/checkout/pool_size", json={"size": 20}, headers=TOKEN)  # no-op: not recorded
    changes = c.get("/changes").json()
    assert [(x["actor"], x["kind"], x["summary"]) for x in changes] == [
        ("release-bot", "flag", "payment_v2: false → true"),
        ("incident-judge", "flag", "payment_v2: true → false"),
        ("operator", "pool_size", "DB pool size: 10 → 20"),
    ]
    assert c.get("/changes", params={"service": "search"}).json() == []
    # clearing faults restores defaults and records what was reverted
    c.delete("/faults", headers=TOKEN)
    assert c.get("/changes").json()[-1]["summary"] == "DB pool size: 20 → 10"
    assert (tmp_path / "changes.jsonl").exists()


def test_deploy_and_pool_faults_are_changes(tmp_path):
    sup, c = make_client(tmp_path)
    c.post("/faults", json={"fault": "bad_deploy"}, headers=TOKEN)
    c.post("/faults", json={"fault": "pool_starved"}, headers=TOKEN)
    summaries = [x["summary"] for x in c.get("/changes").json()]
    assert summaries == ["deployed 1.4.2 (was 1.4.1)", "DB pool size: 10 → 2"]


def test_pages_served_without_exposing_token(tmp_path):
    sup, c = make_client(tmp_path)
    shop = c.get("/")
    assert shop.status_code == 200 and "ShopLab" in shop.text and "shop.js" in shop.text
    ops = c.get("/ops")
    assert ops.status_code == 200 and "Control Room" in ops.text
    for asset in ("shop.css", "shop.js", "ops.css", "ops.js", "favicon.svg"):
        body = c.get(f"/static/{asset}")
        assert body.status_code == 200, asset
        assert "t0k" not in body.text and "dev-control-token" not in body.text


def test_ops_inject_and_clear_use_server_side_token(tmp_path):
    sup, c = make_client(tmp_path)
    r = c.post("/ops/faults", json={"fault": "bad_flag"})  # no token from the browser
    assert r.status_code == 200 and r.json()["services"] == ["checkout"]
    state = c.get("/ops/api/state").json()
    assert [a["fault"] for a in state["active"]] == ["bad_flag"]
    assert state["active"][0]["info"]["title"]
    assert {f["id"] for f in state["faults"]} >= {"bad_flag", "slow_query", "pool_starved"}
    assert c.post("/ops/faults", json={"fault": "nope"}).status_code == 400
    assert c.post("/ops/faults/clear").status_code == 200
    assert c.get("/ops/api/state").json()["active"] == []
    assert sup.configs["checkout"]["flags"]["payment_v2"] is False


def test_storefront_failures_are_customer_facing(tmp_path):
    sup, c = make_client(tmp_path)  # services are not running: every upstream call fails
    catalog = c.get("/shop/api/catalog").json()
    assert len(catalog["products"]) == len(PRODUCTS) and "Audio" in catalog["categories"]
    r = c.post("/shop/api/pay", json={"items": [{"id": 1, "qty": 2}]})
    body = r.json()
    assert r.status_code == 502 and body["ok"] is False
    assert body["message"].startswith(("Payment couldn't be processed", "Checkout is taking longer"))
    assert body["reference"].startswith("SL-")
    assert "Error" not in body["message"] and "Traceback" not in json.dumps(body)
    assert c.post("/shop/api/pay", json={"items": []}).status_code == 400
    assert c.get("/shop/api/search", params={"q": "tent"}).json()["ok"] is False
    errors = c.get("/ops/api/state").json()["customer_errors"]
    assert errors[0]["journey"] == "search" and errors[1]["journey"] == "checkout"


def test_seed_rows_match_storefront_catalog():
    rows = seed_rows(500)
    assert len(rows) == 500 and rows[0] == (1, PRODUCTS[0]["name"], PRODUCTS[0]["price"])


async def test_connector_changes_and_actor_header(tmp_path):
    from judge.settings import Settings

    settings = Settings.from_env(backend="sandbox", trial_id=None, var_dir=tmp_path)
    seen = []

    async def handler(request: httpx.Request):
        seen.append((request.method, request.url.path, request.headers.get("x-actor"), dict(request.url.params)))
        if request.url.path == "/changes":
            return httpx.Response(200, json=[{"ts": "2026-09-13T10:00:00+00:00", "service": "checkout"}])
        return httpx.Response(200, json={"prev": 1, "value": 2})

    async with HttpClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as http:
        ctl = ShopLabControl(settings, http, base_url="http://sup")
        await ctl.set_flag("checkout", "payment_v2", False)
        await ctl.get_config("checkout")
        items = await ctl.changes(since="2026-09-13T09:00:00Z", service="checkout")
    assert items[0]["service"] == "checkout"
    assert seen[0][2] == "incident-judge" and seen[1][2] is None
    assert seen[2][:2] == ("GET", "/changes") and seen[2][3] == {"since": "2026-09-13T09:00:00Z", "service": "checkout"}
