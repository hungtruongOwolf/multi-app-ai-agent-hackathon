"""Starts a real ShopLab (supervisor + 5 service processes), injects a real fault, fixes it,
restarts a service. ~30s."""

import asyncio
import subprocess
import sys
import time

import httpx
import pytest

from judge.signals.scrape_backend import DirectScrapeBackend
from shoplab.common import ROOT

BASE = 9800
SUP = f"http://127.0.0.1:{BASE}"
H = {"X-Control-Token": "test-token"}


async def wait_until(pred, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await pred()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


@pytest.fixture
def supervisor(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "shoplab.supervisor", "--port-base", str(BASE), "--trial-id", "t_it",
         "--rps", "40", "--staging-rps", "0", "--control-token", "test-token", "--data-dir", str(tmp_path)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    yield proc
    try:
        httpx.post(f"{SUP}/shutdown", headers=H, timeout=5)
    except httpx.HTTPError:
        pass
    try:
        proc.wait(20)
    except subprocess.TimeoutExpired:
        proc.kill()


async def all_healthy(client: httpx.AsyncClient):
    try:
        services = (await client.get(f"{SUP}/services")).json()
    except httpx.HTTPError:
        return False
    for s in services:
        try:
            if (await client.get(f"{s['url']}/health")).status_code != 200:
                return False
        except httpx.HTTPError:
            return False
    return services


async def _health(client: httpx.AsyncClient, pred):
    data = (await client.get(f"{SUP}/ops/api/health")).json()
    return next((s for s in data["services"] if pred(s)), None)


async def test_fault_fix_restart_end_to_end(supervisor):
    async with httpx.AsyncClient(timeout=10) as c:
        services = await wait_until(lambda: all_healthy(c), timeout=40)
        names = {s["name"] for s in services}
        assert names == {"checkout", "search", "catalog", "internal-batch", "checkout@staging"}
        assert all(s["alive"] for s in services)

        # auth on mutations
        assert (await c.post(f"{SUP}/faults", json={"fault": "bad_flag"})).status_code == 401
        assert (await c.post(f"{SUP}/faults", json={"fault": "nope"}, headers=H)).status_code == 400

        be = await DirectScrapeBackend.from_supervisor(SUP, interval_s=0.5, stale_after_s=5)
        await be.start()
        try:
            await wait_until(lambda: _v(be.value("rps", "checkout", 3), lambda v: v > 5), timeout=15)
            assert be.available()
            assert (be.value("error_rate", "checkout", 3) or 0) < 0.05

            # storefront: a real purchase goes through the proxy to the real checkout service
            shop = await c.post(f"{SUP}/shop/api/pay", json={"items": [{"id": 1, "qty": 1}]})
            assert shop.status_code == 200 and shop.json()["ok"] is True, shop.text
            assert (await c.get(f"{SUP}/shop/api/products/3")).json()["product"]["name"] == "Studio Vinyl Turntable"

            # real fault: payment_v2 flag on -> /pay raises ConnectionError -> 5xx
            r = await c.post(f"{SUP}/faults", json={"fault": "bad_flag"}, headers=H)
            assert r.status_code == 200, r.text
            cfg = (await c.get(f"{SUP}/config/checkout")).json()
            assert cfg["flags"]["payment_v2"] is True
            await wait_until(lambda: _v(be.value("error_rate", "checkout", 2), lambda v: v > 0.5), timeout=15)

            # shoppers get a customer-facing failure, the control room sees it, the change log explains it
            async def shop_fails():
                resp = await c.post(f"{SUP}/shop/api/pay", json={"items": [{"id": 1, "qty": 1}]})
                return resp.status_code == 502 and resp.json()
            failed = await wait_until(shop_fails, timeout=10)
            assert failed["message"].startswith("Payment couldn't be processed") and failed["reference"].startswith("SL-")
            changes = (await c.get(f"{SUP}/changes", params={"service": "checkout"})).json()
            assert changes[-1]["actor"] == "release-bot" and changes[-1]["summary"] == "payment_v2: false → true"
            health = await wait_until(
                lambda: _health(c, lambda s: s["service"] == "checkout" and s["status"] in ("outage", "degraded")),
                timeout=15)
            assert health["error_rate"] > 0.1

            # fix through the control plane the agent will use
            r = await c.put(f"{SUP}/config/checkout/flags/payment_v2", json={"value": False},
                            headers={**H, "X-Actor": "incident-judge"})
            assert r.json() == {"prev": True, "value": False}
            last = (await c.get(f"{SUP}/changes")).json()[-1]
            assert (last["actor"], last["summary"]) == ("incident-judge", "payment_v2: true → false")
            assert (await c.put(f"{SUP}/config/checkout/flags/nope", json={"value": False},
                                headers=H)).status_code == 404
            await wait_until(lambda: _v(be.value("error_rate", "checkout", 2), lambda v: v < 0.05), timeout=15)

            # restart: new process, healthy, counters reset handled
            before = (await c.get(f"http://127.0.0.1:{BASE + 1}/health")).json()
            r = await c.post(f"{SUP}/services/checkout/restart", headers=H, timeout=30)
            assert r.status_code == 200, r.text
            after = (await c.get(f"http://127.0.0.1:{BASE + 1}/health")).json()
            assert after["pid"] == r.json()["pid"] != before["pid"]
            assert after["started_at"] > before["started_at"]
            await asyncio.sleep(3)
            rps = be.value("rps", "checkout", 4)
            err = be.value("error_rate", "checkout", 4)
            assert rps is not None and rps > 0
            assert err is not None and 0 <= err < 0.2

            # other control endpoints
            r = await c.put(f"{SUP}/config/checkout/pool_size", json={"size": 20}, headers=H)
            assert r.json() == {"prev": 10, "value": 20}
            r = await c.post(f"{SUP}/deploy/checkout", json={"version": "9.9.9"}, headers=H)
            assert r.status_code == 400
            assert (await c.delete(f"{SUP}/faults", headers=H)).status_code == 200
            assert (await c.get(f"{SUP}/config/checkout")).json()["pool_size"] == 10
        finally:
            await be.stop()


async def _v(value, pred):
    return value is not None and pred(value)
