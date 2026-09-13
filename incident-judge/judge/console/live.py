"""Live state of every external record an incident created, read back from the apps themselves.

The store says what the agent *did*; this says what each app *shows now* (someone may have closed the Linear ticket
or resolved the Sentry issue by hand). Reads only, concurrently, with a short overall timeout and a small in-process
cache so the console's 3-second auto-refresh never hammers the APIs. Anything that can't be read degrades to
"unknown" next to the value the agent stored."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from judge.core.models import Incident
from judge.settings import Settings

TTL_S = 15.0
TIMEOUT_S = 3.0

Q_LIVE_ISSUE = """query IssueLive($id: String!) {
  issue(id: $id) { identifier url state { name type } assignee { name } }
}"""


def _configured(value: str | None) -> bool:
    return bool(value) and value not in ("sandbox", "xoxb-sandbox")


class LiveApps:
    def __init__(self, settings: Settings, ttl_s: float = TTL_S, timeout_s: float = TIMEOUT_S):
        self.s = settings
        self.ttl_s = ttl_s
        self.timeout_s = timeout_s
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        return self.s.backend == "real"

    def fetch(self, inc: Incident, sentry_ids: list[str], pr_number: int | None) -> dict[str, dict]:
        """{app: {"ok": bool, ...fields}}; apps that are not configured or can't be read are absent or ok=False."""
        if not self.enabled():
            return {}
        key = f"{inc.id}:{inc.linear_issue_id}:{inc.instatus_incident_id}:{','.join(sentry_ids)}:{pr_number}"
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl_s:
                return hit[1]
        try:
            result = asyncio.run(self._fetch_all(inc, sentry_ids, pr_number))
        except Exception:  # never break the page because an app is slow or down
            result = {}
        with self._lock:
            self._cache[key] = (time.monotonic(), result)
        return result

    async def _fetch_all(self, inc: Incident, sentry_ids: list[str], pr_number: int | None) -> dict[str, dict]:
        from judge.connectors.transport import HttpClient

        s = self.s
        async with HttpClient(timeout=self.timeout_s) as http:
            jobs: dict[str, Any] = {}
            if inc.linear_issue_id and _configured(s.linear_api_key):
                jobs["linear"] = self._linear(http, inc.linear_issue_id)
            if inc.instatus_incident_id and _configured(s.instatus_api_key):
                jobs["instatus"] = self._instatus(http, inc.instatus_incident_id)
            if sentry_ids and _configured(s.sentry_token):
                jobs["sentry"] = self._sentry(http, sentry_ids)
            if pr_number and s.github_token and s.github_memory_repo:
                jobs["github"] = self._github(http, pr_number)
            if not jobs:
                return {}
            names = list(jobs)
            done = await asyncio.wait_for(asyncio.gather(*jobs.values(), return_exceptions=True), self.timeout_s)
        return {n: (r if isinstance(r, dict) else {"ok": False, "error": type(r).__name__}) for n, r in zip(names, done)}

    async def _linear(self, http, issue_id: str) -> dict:
        from judge.connectors.linear import LinearClient

        data = await LinearClient(self.s, http)._gql("live_issue", "IssueLive", Q_LIVE_ISSUE, {"id": issue_id})
        issue = data.get("issue") or {}
        state = issue.get("state") or {}
        return {"ok": bool(issue), "state": state.get("name"), "state_type": state.get("type"),
                "assignee": (issue.get("assignee") or {}).get("name"), "identifier": issue.get("identifier"),
                "url": issue.get("url")}

    async def _instatus(self, http, incident_id: str) -> dict:
        from judge.connectors.instatus import InstatusClient

        data = await InstatusClient(self.s, http).get_incident(incident_id) or {}
        comps = [{"name": c.get("name"), "status": c.get("status")} for c in (data.get("components") or [])]
        return {"ok": bool(data), "status": data.get("status"), "components": comps, "name": data.get("name")}

    async def _sentry(self, http, issue_ids: list[str]) -> dict:
        from judge.connectors.sentry import SentryClient

        client = SentryClient(self.s, http)
        got = await asyncio.gather(*(client.get_issue(i) for i in issue_ids), return_exceptions=True)
        issues = [{"id": i, "status": g.get("status"), "count": g.get("count"), "short_id": g.get("shortId"),
                   "url": g.get("permalink")} if isinstance(g, dict) else {"id": i, "status": None}
                  for i, g in zip(issue_ids, got)]
        return {"ok": any(x["status"] for x in issues), "issues": issues}

    async def _github(self, http, number: int) -> dict:
        from judge.connectors.github import GitHubClient

        pr = await GitHubClient(self.s.github_token, self.s.github_memory_repo, http).get_pr(number)
        state = "merged" if pr.get("merged") else pr.get("state")
        return {"ok": True, "state": state, "url": pr.get("url"), "merged_by": pr.get("merged_by")}
