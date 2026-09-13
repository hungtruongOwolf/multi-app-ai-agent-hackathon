"""Sentry org-level issues API (real: https://sentry.io, auth: Bearer <Personal Token>)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from judge.connectors.transport import ConnectorError, HttpClient, check
from judge.core.models import Env, Signal
from judge.safety.redact import redact
from judge.settings import Settings
from judge.signals.fingerprint import fingerprint

APP = "sentry"


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def event_tags(event: dict[str, Any]) -> dict[str, str]:
    tags = event.get("tags") or []
    if isinstance(tags, dict):
        return {str(k): str(v) for k, v in tags.items()}
    return {str(t.get("key")): str(t.get("value")) for t in tags if isinstance(t, dict)}


class SentryClient:
    def __init__(self, settings: Settings, http: HttpClient):
        self.s = settings
        self.http = http

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.s.sentry_token}"}

    def _url(self, path: str) -> str:
        return f"{self.s.sentry_base}/api/0/organizations/{self.s.sentry_org}/{path}"

    async def list_issues(self, environment: str, query: str = "is:unresolved lastSeen:-10m",
                          trial_id: str | None = None, limit: int = 100) -> list[dict]:
        """Issues seen in `environment`. When a trial id is active (arg or settings) the query is scoped
        to the `ij_trial` tag so parallel eval trials never see each other's issues."""
        trial = trial_id if trial_id is not None else self.s.trial_id
        if trial and "ij_trial:" not in query:
            query = f"{query} ij_trial:{trial}"
        params = {"query": query, "environment": environment, "limit": str(limit)}
        resp = await self.http.request(APP, "list_issues", "GET", self._url("issues/"), params=params,
                                       headers=self._headers)
        if resp.status_code == 404 and "ij_trial:" in query:
            # Real Sentry answers 404 for a tag key it has not indexed yet (first events of a trial): nothing yet.
            return []
        body = check(APP, "list_issues", resp)
        if not isinstance(body, list):
            raise ConnectorError(APP, "list_issues", resp.status_code, body)
        return body

    # ------------------------------------------------------------ discovery (bootstrap / doctor)

    def _api(self, path: str) -> str:
        return f"{self.s.sentry_base}/api/0/{path}"

    async def organization(self) -> dict:
        resp = await self.http.request(APP, "organization", "GET", self._url(""), headers=self._headers)
        return check(APP, "organization", resp)

    async def projects(self) -> list[dict]:
        resp = await self.http.request(APP, "projects", "GET", self._url("projects/"), headers=self._headers)
        return check(APP, "projects", resp) or []

    async def teams(self) -> list[dict]:
        resp = await self.http.request(APP, "teams", "GET", self._url("teams/"), headers=self._headers)
        return check(APP, "teams", resp) or []

    async def create_project(self, team_slug: str, slug: str, name: str, platform: str = "python") -> dict:
        resp = await self.http.request(APP, "create_project", "POST",
                                       self._api(f"teams/{self.s.sentry_org}/{team_slug}/projects/"),
                                       json={"name": name, "slug": slug, "platform": platform},
                                       headers=self._headers)
        return check(APP, "create_project", resp)

    async def project_dsn(self, project_slug: str) -> str:
        resp = await self.http.request(APP, "project_dsn", "GET",
                                       self._api(f"projects/{self.s.sentry_org}/{project_slug}/keys/"),
                                       headers=self._headers)
        keys = check(APP, "project_dsn", resp) or []
        for key in keys:
            if key.get("isActive", True) and (key.get("dsn") or {}).get("public"):
                return key["dsn"]["public"]
        raise ConnectorError(APP, "project_dsn", resp.status_code,
                             {"error": "no active client key", "project": project_slug})

    async def resolve_issue(self, issue_id: str) -> None:
        resp = await self.http.request(APP, "resolve_issue", "PUT", self._url(f"issues/{issue_id}/"),
                                       json={"status": "resolved"}, headers=self._headers)
        check(APP, "resolve_issue", resp)

    async def get_issue(self, issue_id: str) -> dict:
        resp = await self.http.request(APP, "get_issue", "GET", self._url(f"issues/{issue_id}/"),
                                       headers=self._headers)
        return check(APP, "get_issue", resp)

    async def latest_event(self, issue_id: str) -> dict:
        resp = await self.http.request(APP, "latest_event", "GET", self._url(f"issues/{issue_id}/events/latest/"),
                                       headers=self._headers)
        return check(APP, "latest_event", resp)

    def to_signal(self, issue: dict, event: dict, trial_id: str | None) -> Signal:
        tags = event_tags(event)
        env_raw = tags.get("environment") or event.get("environment") or ""
        # Anything that is not explicitly production is treated as non-production (safe side for P1).
        environment = Env.production if env_raw == "production" else Env.staging
        metadata = issue.get("metadata") or {}
        error_type = metadata.get("type") or ""
        culprit = issue.get("culprit") or ""
        service = tags.get("service") or ""
        raw_message = metadata.get("value") or event.get("message") or issue.get("title") or ""
        return Signal(
            source="sentry",
            fingerprint=fingerprint(error_type, culprit, environment.value, service),
            service=service,
            environment=environment,
            error_type=error_type,
            culprit=culprit,
            message_redacted=redact(str(raw_message), max_len=300),
            count=int(issue.get("count") or 0),
            user_count=int(issue.get("userCount") or 0),
            first_seen=_dt(issue.get("firstSeen")),
            last_seen=_dt(issue.get("lastSeen")),
            external_id=str(issue.get("id")),
            trial_id=trial_id or tags.get("ij_trial") or None,
        )
