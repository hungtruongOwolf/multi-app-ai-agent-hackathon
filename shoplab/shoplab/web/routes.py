"""Web surfaces served by the supervisor:

  GET  /                 ShopLab storefront (real shopping flows hitting the real services)
  GET  /ops              internal control room: faults, live service health, change log
  /shop/api/...          storefront backend: proxies to catalog / search / checkout (no CORS, no service ports exposed)
  /ops/api/..., /ops/faults...  control-room backend; the control token stays server-side

Bound to 127.0.0.1 like the rest of ShopLab; /ops is a local demo console, not an internet-facing admin panel."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from shoplab.catalog_data import BY_ID, CATEGORIES, PRODUCTS
from shoplab.web.faults_info import BY_ID as FAULT_BY_ID
from shoplab.web.faults_info import FAULTS

if TYPE_CHECKING:
    from shoplab.supervisor import Supervisor

log = logging.getLogger("shoplab.web")
STATIC = Path(__file__).resolve().parent / "static"
WEB_USER = "web-shopper"


class CartItem(BaseModel):
    id: int
    qty: int = 1


class PayBody(BaseModel):
    items: list[CartItem] = []
    email: str | None = None


class OpsFault(BaseModel):
    fault: str
    service: str | None = None


def _ref() -> str:
    return f"SL-{secrets.token_hex(3).upper()}"


class CustomerErrors:
    """Last customer-facing failures, so the control room shows what shoppers just experienced."""

    def __init__(self, size: int = 40):
        self.items: deque[dict] = deque(maxlen=size)

    def add(self, *, journey: str, status: int, ref: str, message: str, upstream: str) -> None:
        self.items.appendleft({"ts": datetime.now(UTC).isoformat(), "journey": journey, "status": status,
                               "ref": ref, "message": message, "upstream": upstream})


def attach_web(app: FastAPI, sup: "Supervisor") -> None:
    app.mount("/static", StaticFiles(directory=STATIC), name="shoplab-static")
    app.state.customer_errors = errors = CustomerErrors()
    app.state.health_backend = None
    client = httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.0))

    @app.get("/", include_in_schema=False)
    async def storefront():
        return FileResponse(STATIC / "shop.html")

    @app.get("/ops", include_in_schema=False)
    async def ops_page():
        return FileResponse(STATIC / "ops.html")

    shop = APIRouter(prefix="/shop/api")

    async def call(service: str, method: str, path: str, **kw) -> tuple[int, dict | None, str]:
        base = sup.service_url(service)
        try:
            r = await client.request(method, f"{base}{path}", headers={"x-user-id": WEB_USER}, **kw)
            try:
                body = r.json()
            except ValueError:
                body = None
            return r.status_code, body, (body or {}).get("error", "") if isinstance(body, dict) else ""
        except httpx.TimeoutException:
            return 504, None, "timeout"
        except httpx.HTTPError as e:
            return 503, None, type(e).__name__

    def failure(journey: str, status: int, upstream: str, message: str) -> JSONResponse:
        ref = _ref()
        errors.add(journey=journey, status=status, ref=ref, message=message, upstream=upstream)
        return JSONResponse({"ok": False, "message": message, "reference": ref}, status_code=502)

    @shop.get("/catalog")
    async def catalog():
        return {"categories": CATEGORIES, "products": PRODUCTS}

    @shop.get("/products/{pid}")
    async def product(pid: int):
        if pid not in BY_ID:
            raise HTTPException(404, "not found")
        status, body, upstream = await call("catalog", "GET", f"/products/{pid}")
        if status != 200:
            return failure("product page", status, upstream,
                           "We couldn't load this product right now. Please try again in a moment.")
        return {"ok": True, "product": {**BY_ID[pid], "price": (body or {}).get("price", BY_ID[pid]["price"])}}

    @shop.get("/search")
    async def search(q: str = ""):
        status, body, upstream = await call("search", "GET", "/search", params={"q": q})
        if status != 200:
            return failure("search", status, upstream, "Search is having trouble. Browse the catalog while we fix it.")
        ids = {r["id"] for r in (body or {}).get("results", [])}
        needle = q.strip().lower()
        results = [p for p in PRODUCTS if p["id"] in ids or (needle and needle in p["name"].lower())]
        return {"ok": True, "results": results}

    @shop.post("/pay")
    async def pay(body: PayBody):
        items = [(BY_ID[i.id], max(1, i.qty)) for i in body.items if i.id in BY_ID]
        if not items:
            raise HTTPException(400, "cart is empty")
        amount = round(sum(p["price"] * q for p, q in items), 2)
        status, resp, upstream = await call("checkout", "POST", "/pay",
                                            json={"amount": amount, "currency": "USD",
                                                  "items": [{"id": p["id"], "qty": q} for p, q in items]})
        if status == 200:
            return {"ok": True, "order_id": f"SL-{secrets.randbelow(900000) + 100000}", "amount": amount}
        if status in (503, 504):
            msg = "Checkout is taking longer than usual. Your card was not charged — please try again."
        else:
            msg = "Payment couldn't be processed — please try again. Your card was not charged."
        return failure("checkout", status, upstream, msg)

    @shop.post("/avatar")
    async def avatar():
        status, _, upstream = await call("catalog", "POST", "/profile/avatar")
        if status != 200:
            return failure("profile photo", status, upstream, "We couldn't update your photo. Please try again later.")
        return {"ok": True}

    app.include_router(shop)

    ops = APIRouter(prefix="/ops")

    async def backend():
        if app.state.health_backend is None:
            from judge.signals.scrape_backend import DirectScrapeBackend

            be = DirectScrapeBackend({sid: sup.service_url(sid) for sid in sup.services}, interval_s=2.0,
                                     stale_after_s=10.0)
            await be.start()
            app.state.health_backend = be
            await asyncio.sleep(0)  # first scrape happens in the background
        return app.state.health_backend

    @ops.get("/api/state")
    async def ops_state():
        active = [{**f, "info": FAULT_BY_ID.get(f.get("fault"), {})} for f in sup.faults]
        return {"faults": FAULTS, "active": active, "traffic": sup.traffic.state() if sup.traffic else {},
                "trial_id": sup.trial_id, "customer_errors": list(errors.items)}

    @ops.get("/api/health")
    async def ops_health():
        be = await backend()
        out = []
        for sid, m in sup.services.items():
            window = 20
            er = be.value("error_rate", sid, window)
            p95 = be.value("latency_p95", sid, window)
            rps = be.value("rps", sid, window)
            cfg = sup.configs.get(sid, {})
            if not sup.alive(sid):
                status = "down"
            elif er is not None and er >= 0.25:
                status = "outage"
            elif (er is not None and er >= 0.02) or (p95 is not None and p95 >= 0.8):
                status = "degraded"
            elif rps is None and sid != "internal-batch":
                status = "warming"
            else:
                status = "healthy"
            out.append({"service": sid, "status": status, "alive": sup.alive(sid), "error_rate": er,
                        "latency_p95": p95, "rps": rps, "flags": cfg.get("flags", {}),
                        "pool_size": cfg.get("pool_size"), "version": cfg.get("app_version")})
        return {"available": be.available(), "services": out}

    @ops.post("/faults")
    async def ops_inject(body: OpsFault, request: Request):
        return sup.add_fault(body.fault, body.service, {}, actor=request.headers.get("x-actor") or "ops-console")

    @ops.post("/faults/clear")
    async def ops_clear():
        return sup.clear_faults(actor="ops-console")

    app.include_router(ops)

    async def close() -> None:  # called from the supervisor lifespan on shutdown
        await client.aclose()
        if app.state.health_backend is not None:
            await app.state.health_backend.stop()

    app.state.web_close = close
