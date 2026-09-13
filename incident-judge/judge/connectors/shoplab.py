"""ShopLab supervisor control plane client (the only thing remediation actions may touch)."""

from __future__ import annotations

from typing import Any

from judge.connectors.transport import HttpClient, check
from judge.settings import Settings

APP = "shoplab"


class ShopLabControl:
    def __init__(self, settings: Settings, http: HttpClient, base_url: str | None = None):
        self.s = settings
        self.http = http
        self.base = (base_url or settings.shoplab_supervisor_url).rstrip("/")

    ACTOR = "incident-judge"

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Control-Token": self.s.shoplab_control_token}

    async def _req(self, op: str, method: str, path: str, **kw: Any) -> Any:
        headers = dict(self._headers)
        if method != "GET":  # every change the agent makes is attributed in ShopLab's change log
            headers["X-Actor"] = self.ACTOR
        resp = await self.http.request(APP, op, method, f"{self.base}{path}", headers=headers, **kw)
        return check(APP, op, resp)

    async def changes(self, since: str | None = None, service: str | None = None) -> list[dict]:
        """Recent config changes (flags, pool size, deploys, restarts) with actor and summary, newest last."""
        params = {k: v for k, v in {"since": since, "service": service}.items() if v}
        return await self._req("changes", "GET", "/changes", params=params)

    async def services(self) -> list[dict]:
        return await self._req("services", "GET", "/services")

    async def get_config(self, service: str) -> dict:
        return await self._req("get_config", "GET", f"/config/{service}")

    async def set_flag(self, service: str, flag: str, value: bool) -> dict:
        return await self._req("set_flag", "PUT", f"/config/{service}/flags/{flag}", json={"value": value})

    async def set_pool_size(self, service: str, size: int) -> dict:
        return await self._req("set_pool_size", "PUT", f"/config/{service}/pool_size", json={"size": size})

    async def deploy(self, service: str, version: str) -> dict:
        return await self._req("deploy", "POST", f"/deploy/{service}", json={"version": version})

    async def restart(self, service: str) -> dict:
        return await self._req("restart", "POST", f"/services/{service}/restart")
