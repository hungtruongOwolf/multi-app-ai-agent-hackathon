"""Instatus REST (real: https://api.instatus.com/v1/:page_id/..., auth: Bearer <key>).

Public surface: messages must come from judge.safety.templates. We default to notify=false and
shouldPublish from settings (false in dev) — Instatus publishes immediately otherwise."""

from __future__ import annotations

from datetime import UTC, datetime

from judge.connectors.transport import ConnectorError, HttpClient, check
from judge.core.outbox import parse_marker
from judge.safety.templates import public_ref
from judge.settings import Settings

APP = "instatus"


def ref_from_marker(marker: str) -> str:
    """Outbox markers are internal; the public surface only ever sees the opaque ref derived from them."""
    key, _incident, trial = parse_marker(marker)
    if not key:
        raise ValueError(f"marker without IJ-KEY: {marker!r}")
    return public_ref(key, trial)


class InstatusClient:
    def __init__(self, settings: Settings, http: HttpClient):
        self.s = settings
        self.http = http

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.s.instatus_api_key}", "Content-Type": "application/json"}

    def _url(self, path: str = "") -> str:
        return f"{self.s.instatus_base}/v1/{self.s.instatus_page_id}/incidents{path}"

    async def create_incident(self, name: str, message: str, component_ids: list[str], component_status: str,
                              status: str = "INVESTIGATING") -> str:
        body = {
            "name": name,
            "message": message,
            "components": component_ids,
            "started": datetime.now(UTC).isoformat(),
            "status": status,
            "notify": False,
            "statuses": [{"id": cid, "status": component_status} for cid in component_ids],
            "shouldPublish": self.s.instatus_should_publish,
        }
        resp = await self.http.request(APP, "create_incident", "POST", self._url(), json=body,
                                       headers=self._headers)
        data = check(APP, "create_incident", resp)
        if not isinstance(data, dict) or not data.get("id"):
            raise ConnectorError(APP, "create_incident", resp.status_code, data)
        return data["id"]

    async def set_components(self, incident_id: str, component_ids: list[str], component_status: str) -> None:
        """Affected components are part of the incident itself; changing them needs PUT (updates don't add them)."""
        current = await self.get_incident(incident_id)
        # The real API validates the whole incident on PUT (e.g. `started` must be a date), so send it back complete.
        body = {
            "name": current.get("name"),
            "status": current.get("status") or "INVESTIGATING",
            "started": current.get("started") or datetime.now(UTC).isoformat(),
            "notify": False,
            "components": component_ids,
            "statuses": [{"id": cid, "status": component_status} for cid in component_ids],
        }
        resp = await self.http.request(APP, "set_components", "PUT", self._url(f"/{incident_id}"), json=body,
                                       headers=self._headers)
        check(APP, "set_components", resp)

    async def add_update(self, incident_id: str, message: str, status: str, component_ids: list[str],
                         component_status: str) -> str:
        body = {
            "message": message,
            "status": status,
            "notify": False,
            "started": datetime.now(UTC).isoformat(),
            # the real API treats a missing list as "remove all components" and rejects the update
            "components": component_ids,
            "statuses": [{"id": cid, "status": component_status} for cid in component_ids],
        }
        resp = await self.http.request(APP, "add_update", "POST", self._url(f"/{incident_id}/incident-updates"),
                                       json=body, headers=self._headers)
        data = check(APP, "add_update", resp)
        updates = (data or {}).get("updates") or []
        return updates[-1]["id"] if updates else str((data or {}).get("id", incident_id))

    async def get_incident(self, incident_id: str) -> dict:
        resp = await self.http.request(APP, "get_incident", "GET", self._url(f"/{incident_id}"),
                                       headers=self._headers)
        return check(APP, "get_incident", resp)

    async def list_incidents(self) -> list[dict]:
        resp = await self.http.request(APP, "list_incidents", "GET", self._url(), headers=self._headers)
        data = check(APP, "list_incidents", resp)
        if isinstance(data, dict):  # tolerate paginated/wrapped shapes
            data = data.get("incidents") or data.get("data") or []
        return data or []

    async def find_incident_by_ref(self, ref: str) -> str | None:
        for inc in await self.list_incidents():
            texts = [inc.get("name") or ""] + [u.get("message") or "" for u in inc.get("updates") or []]
            if any(ref in t for t in texts):
                return inc["id"]
        return None

    async def delete_incident(self, incident_id: str) -> None:
        resp = await self.http.request(APP, "delete_incident", "DELETE", self._url(f"/{incident_id}"),
                                       headers=self._headers)
        check(APP, "delete_incident", resp)

    # ------------------------------------------------------------ discovery (bootstrap / doctor)

    async def pages(self) -> list[dict]:
        resp = await self.http.request(APP, "pages", "GET", f"{self.s.instatus_base}/v2/pages", headers=self._headers)
        if resp.status_code == 404:
            resp = await self.http.request(APP, "pages", "GET", f"{self.s.instatus_base}/v1/pages",
                                           headers=self._headers)
        return check(APP, "pages", resp) or []

    async def components(self) -> list[dict]:
        resp = await self.http.request(APP, "components", "GET",
                                       f"{self.s.instatus_base}/v1/{self.s.instatus_page_id}/components",
                                       headers=self._headers)
        return check(APP, "components", resp) or []

    async def create_component(self, name: str, description: str = "") -> str:
        resp = await self.http.request(APP, "create_component", "POST",
                                       f"{self.s.instatus_base}/v1/{self.s.instatus_page_id}/components",
                                       json={"name": name, "description": description, "status": "OPERATIONAL",
                                             "showUptime": True},
                                       headers=self._headers)
        data = check(APP, "create_component", resp)
        if not isinstance(data, dict) or not data.get("id"):
            raise ConnectorError(APP, "create_component", resp.status_code, data)
        return data["id"]
