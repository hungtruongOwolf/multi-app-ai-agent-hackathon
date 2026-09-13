"""PagerDuty Events API v2 (https://events.pagerduty.com/v2/enqueue) — page a human when the agent should not decide.

Only a service integration key (routing key) is needed; no REST token. `dedup_key` makes every trigger for the same
incident collapse into one PagerDuty incident while it is open, and `resolve` closes it.
Trap (from the vendor docs): incidents are only created when the service's escalation policy has an on-call user."""

from __future__ import annotations

from judge.connectors.transport import ConnectorError, HttpClient, check
from judge.settings import Settings

APP = "pagerduty"
SEVERITY = {"SEV1": "critical", "SEV2": "error", "SEV3": "warning", "SEV4": "info"}


class PagerDutyClient:
    def __init__(self, settings: Settings, http: HttpClient):
        self.s = settings
        self.http = http

    @property
    def enabled(self) -> bool:
        return bool(self.s.pagerduty_routing_key)

    @property
    def _url(self) -> str:
        return (f"{self.s.sandbox_url}/pagerduty/v2/enqueue" if self.s.backend == "sandbox"
                else "https://events.pagerduty.com/v2/enqueue")

    async def _enqueue(self, op: str, body: dict) -> str:
        resp = await self.http.request(APP, op, "POST", self._url, json=body)
        data = check(APP, op, resp)
        if not isinstance(data, dict) or data.get("status") != "success":
            raise ConnectorError(APP, op, resp.status_code, data)
        return data.get("dedup_key") or body.get("dedup_key", "")

    async def trigger(self, dedup_key: str, summary: str, severity: str, source: str,
                      details: dict | None = None, links: list[dict] | None = None) -> str:
        return await self._enqueue("trigger", {
            "routing_key": self.s.pagerduty_routing_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {"summary": summary[:1024], "severity": SEVERITY.get(severity, severity),
                        "source": source, "component": source, "group": "shoplab",
                        "class": "incident-judge", "custom_details": details or {}},
            "links": links or [],
        })

    async def resolve(self, dedup_key: str) -> str:
        return await self._enqueue("resolve", {"routing_key": self.s.pagerduty_routing_key,
                                               "event_action": "resolve", "dedup_key": dedup_key})
