"""Instatus REST emulation (v1 incidents + incident-updates) and a public status page."""

from __future__ import annotations

import html
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from sandbox.db import DocStore, iso, utcnow

PAGE_ID = "page_shoplab"
INCIDENT_STATUSES = {"INVESTIGATING", "IDENTIFIED", "MONITORING", "RESOLVED"}
COMPONENT_STATUSES = {"OPERATIONAL", "UNDERMAINTENANCE", "DEGRADEDPERFORMANCE", "PARTIALOUTAGE", "MAJOROUTAGE"}


def default_components() -> list[dict[str, str]]:
    try:
        from judge.settings import Config

        comps = [{"id": s.instatus_component_id, "name": s.capability}
                 for s in Config().catalog.values() if s.instatus_component_id]
        if comps:
            return comps
    except Exception:  # pragma: no cover - sandbox must still boot without judge config
        pass
    return [{"id": "comp_checkout", "name": "Checkout & payments"}, {"id": "comp_search", "name": "Search"},
            {"id": "comp_catalog", "name": "Product pages"}]


class InstatusEmulator:
    def __init__(self, db: DocStore):
        self.db = db

    def seed(self) -> None:
        for c in default_components():
            if self.db.get("instatus_component", c["id"]) is None:
                self.db.put("instatus_component", c["id"], {**c, "status": "OPERATIONAL", "pageId": PAGE_ID})

    def _component(self, cid: str) -> dict[str, Any]:
        comp = self.db.get("instatus_component", cid)
        if comp is None:
            raise HTTPException(400, {"message": f"Unknown component {cid}"})
        return comp

    def _apply_statuses(self, statuses: list[dict[str, Any]] | None) -> None:
        for s in statuses or []:
            if s.get("status") not in COMPONENT_STATUSES:
                raise HTTPException(400, {"message": f"Invalid component status {s.get('status')}"})
            comp = self._component(s.get("id", ""))
            comp["status"] = s["status"]
            self.db.put("instatus_component", comp["id"], comp)

    def incident_json(self, inc: dict[str, Any]) -> dict[str, Any]:
        comps = [self.db.get("instatus_component", cid) or {"id": cid} for cid in inc["componentIds"]]
        return {
            "id": inc["id"],
            "name": inc["name"],
            "status": inc["status"],
            "started": inc["started"],
            "resolved": inc.get("resolved"),
            "published": inc["published"],
            "notify": inc["notify"],
            "components": [{"id": c["id"], "name": c.get("name"), "status": c.get("status")} for c in comps],
            "updates": inc["updates"],
            "createdAt": inc["createdAt"],
        }

    def _update(self, message: str, status: str, notify: bool) -> dict[str, Any]:
        return {"id": str(uuid.uuid4()), "message": message, "messageHtml": f"<p>{html.escape(message)}</p>",
                "status": status, "notify": notify, "started": iso(utcnow()), "ended": None, "duration": None,
                "createdAt": iso(utcnow())}

    def create(self, page_id: str, body: dict[str, Any]) -> dict[str, Any]:
        status = body.get("status") or "INVESTIGATING"
        if status not in INCIDENT_STATUSES:
            raise HTTPException(400, {"message": f"Invalid status {status}"})
        if not body.get("name"):
            raise HTTPException(400, {"message": "name is required"})
        component_ids = list(body.get("components") or [])
        for cid in component_ids:
            self._component(cid)
        self._apply_statuses(body.get("statuses"))
        now = iso(utcnow())
        inc = {
            "id": "inc" + uuid.uuid4().hex[:16],
            "pageId": page_id,
            "name": body["name"],
            "status": status,
            "started": body.get("started") or now,
            "resolved": now if status == "RESOLVED" else None,
            "published": body.get("shouldPublish", True) is not False,
            "notify": bool(body.get("notify", False)),
            "componentIds": component_ids,
            "updates": [self._update(body.get("message") or "", status, bool(body.get("notify", False)))],
            "createdAt": now,
        }
        self.db.put("instatus_incident", inc["id"], inc)
        return self.incident_json(inc)

    def get(self, page_id: str, incident_id: str) -> dict[str, Any]:
        inc = self.db.get("instatus_incident", incident_id)
        if inc is None or inc["pageId"] != page_id:
            raise HTTPException(404, {"message": "Incident not found"})
        return inc

    def add_update(self, page_id: str, incident_id: str, body: dict[str, Any]) -> dict[str, Any]:
        inc = self.get(page_id, incident_id)
        status = body.get("status") or inc["status"]
        if status not in INCIDENT_STATUSES:
            raise HTTPException(400, {"message": f"Invalid status {status}"})
        self._apply_statuses(body.get("statuses"))
        inc["updates"].append(self._update(body.get("message") or "", status, bool(body.get("notify", False))))
        inc["status"] = status
        if status == "RESOLVED":
            inc["resolved"] = iso(utcnow())
        self.db.put("instatus_incident", inc["id"], inc)
        return self.incident_json(inc)

    def put(self, page_id: str, incident_id: str, body: dict[str, Any]) -> dict[str, Any]:
        inc = self.get(page_id, incident_id)
        if "name" in body:
            inc["name"] = body["name"]
        if "components" in body:
            for cid in body["components"]:
                self._component(cid)
            inc["componentIds"] = list(body["components"])
        self._apply_statuses(body.get("statuses"))
        if "status" in body:
            if body["status"] not in INCIDENT_STATUSES:
                raise HTTPException(400, {"message": f"Invalid status {body['status']}"})
            inc["status"] = body["status"]
            if body["status"] == "RESOLVED":
                inc["resolved"] = iso(utcnow())
        if body.get("message"):
            inc["updates"].append(self._update(body["message"], inc["status"], bool(body.get("notify", False))))
        if "shouldPublish" in body:
            inc["published"] = body["shouldPublish"] is not False
        self.db.put("instatus_incident", inc["id"], inc)
        return self.incident_json(inc)


def build_router(emu: InstatusEmulator) -> APIRouter:
    r = APIRouter()

    def auth(request: Request, page_id: str) -> None:
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer ") or not h[7:].strip():
            raise HTTPException(401, {"message": "Unauthorized"})
        if page_id != PAGE_ID:
            raise HTTPException(404, {"message": "Page not found"})

    @r.post("/instatus/v1/{page_id}/incidents")
    async def create(page_id: str, request: Request):
        auth(request, page_id)
        return emu.create(page_id, await request.json())

    @r.get("/instatus/v1/{page_id}/incidents")
    def list_(page_id: str, request: Request):
        auth(request, page_id)
        incs = emu.db.all("instatus_incident", lambda i: i["pageId"] == page_id)
        return [emu.incident_json(i) for i in reversed(incs)]

    @r.get("/instatus/v1/{page_id}/incidents/{incident_id}")
    def get(page_id: str, incident_id: str, request: Request):
        auth(request, page_id)
        return emu.incident_json(emu.get(page_id, incident_id))

    @r.put("/instatus/v1/{page_id}/incidents/{incident_id}")
    async def put(page_id: str, incident_id: str, request: Request):
        auth(request, page_id)
        return emu.put(page_id, incident_id, await request.json())

    @r.delete("/instatus/v1/{page_id}/incidents/{incident_id}")
    def delete(page_id: str, incident_id: str, request: Request):
        auth(request, page_id)
        emu.get(page_id, incident_id)
        emu.db.delete("instatus_incident", incident_id)
        return {"id": incident_id, "deleted": True}

    @r.post("/instatus/v1/{page_id}/incidents/{incident_id}/incident-updates")
    async def add_update(page_id: str, incident_id: str, request: Request):
        auth(request, page_id)
        return emu.add_update(page_id, incident_id, await request.json())

    @r.get("/instatus/v1/{page_id}/components")
    def components(page_id: str, request: Request):
        auth(request, page_id)
        return emu.db.all("instatus_component", lambda c: c["pageId"] == page_id)

    @r.post("/instatus/v1/{page_id}/components")
    async def create_component(page_id: str, request: Request):
        auth(request, page_id)
        body = await request.json()
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, {"message": "name is required"})
        status = body.get("status") or "OPERATIONAL"
        if status not in COMPONENT_STATUSES:
            raise HTTPException(400, {"message": f"Invalid component status {status}"})
        comp = {"id": f"comp_{uuid.uuid4().hex[:10]}", "name": name, "description": str(body.get("description") or ""),
                "status": status, "showUptime": bool(body.get("showUptime", True)), "pageId": page_id}
        emu.db.put("instatus_component", comp["id"], comp)
        return comp

    def _pages(request: Request) -> list[dict[str, str]]:
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer ") or not h[7:].strip():
            raise HTTPException(401, {"message": "Unauthorized"})
        return [{"id": PAGE_ID, "subdomain": "shoplab", "name": "ShopLab"}]

    @r.get("/instatus/v2/pages")
    def pages_v2(request: Request):
        return _pages(request)

    @r.get("/instatus/v1/pages")
    def pages_v1(request: Request):
        return _pages(request)

    @r.get("/status/{page_id}", response_class=HTMLResponse)
    def status_page(page_id: str):
        if page_id != PAGE_ID:
            raise HTTPException(404, "Page not found")
        comps = emu.db.all("instatus_component")
        incs = [i for i in reversed(emu.db.all("instatus_incident")) if i["published"]]
        rows = "".join(f"<li><b>{html.escape(c['name'])}</b> — {c['status']}</li>" for c in comps)
        items = "".join(
            f"<article><h3>{html.escape(i['name'])} <small>[{i['status']}]</small></h3>"
            + "".join(f"<p><small>{u['createdAt']} {u['status']}</small><br>{html.escape(u['message'])}</p>"
                      for u in reversed(i["updates"]))
            + "</article>"
            for i in incs
        )
        return (f"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5><title>ShopLab status</title>"
                f"<body style='font-family:sans-serif;max-width:720px;margin:auto;padding:16px'>"
                f"<h1>ShopLab status</h1><ul>{rows}</ul><h2>Incidents</h2>{items or '<p>No incidents.</p>'}</body>")

    return r
