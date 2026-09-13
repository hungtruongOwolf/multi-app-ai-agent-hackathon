"""Admin endpoints for eval runner / graders. Never used by the agent."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter

from sandbox.instatus_api import InstatusEmulator
from sandbox.linear_api import LinearEmulator
from sandbox.sentry_api import SentryEmulator
from sandbox.slack_api import SlackEmulator


def trial_public_ref_prefix(trial_id: str) -> str:
    """Must match judge.safety.templates.public_ref normalisation."""
    trial = re.sub(r"[^a-z0-9]", "", trial_id.lower())[:12] or "live"
    return f"IJ-{trial}-"


class Admin:
    def __init__(self, sentry: SentryEmulator, linear: LinearEmulator, instatus: InstatusEmulator,
                 slack: SlackEmulator):
        self.sentry, self.linear, self.instatus, self.slack = sentry, linear, instatus, slack
        self.db = sentry.db

    def seed(self) -> None:
        self.linear.seed()
        self.instatus.seed()
        self.slack.seed()

    # ------------------------------------------------------------ selection by trial

    def _sentry_issues(self, trial_id: str | None) -> list[dict[str, Any]]:
        issues = self.db.all("sentry_issue")
        if trial_id:
            issues = [i for i in issues if trial_id in i["tag_counts"].get("ij_trial", {})]
        return issues

    def _linear_issues(self, trial_id: str | None) -> list[dict[str, Any]]:
        issues = self.db.all("linear_issue")
        if not trial_id:
            return issues
        mk = f"IJ-TRIAL:{trial_id}"

        def has(i: dict[str, Any]) -> bool:
            if _marker_in(i["description"], mk):
                return True
            return any(_marker_in(c["body"], mk)
                       for c in self.db.all("linear_comment", lambda c: c["issueId"] == i["id"]))
        return [i for i in issues if has(i)]

    def _instatus_incidents(self, trial_id: str | None) -> list[dict[str, Any]]:
        incs = self.db.all("instatus_incident")
        if not trial_id:
            return incs
        prefix = trial_public_ref_prefix(trial_id)
        return [i for i in incs if prefix in i["name"] or any(prefix in u["message"] for u in i["updates"])]

    def _slack(self, trial_id: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        chans = self.db.all("slack_channel")
        msgs = self.db.all("slack_message")
        if not trial_id:
            return chans, msgs
        mk = f"IJ-TRIAL:{trial_id}"

        def blob(m: dict[str, Any]) -> str:
            ref = ((m.get("metadata") or {}).get("event_payload") or {}).get("ref") or ""
            return f"{m.get('text', '')} {ref}"

        marked = [m for m in msgs if _marker_in(blob(m), mk)]
        dedicated = {m["channel"] for m in marked if m["channel"] != "C_ONCALL"}
        roots = {(m["channel"], m.get("thread_ts") or m["ts"]) for m in marked}
        selected = [m for m in msgs
                    if m["channel"] in dedicated
                    or _marker_in(blob(m), mk)
                    or (m["channel"], m.get("thread_ts") or m["ts"]) in roots]
        channel_ids = dedicated | {m["channel"] for m in selected}
        return [c for c in chans if c["id"] in channel_ids], selected

    # ------------------------------------------------------------ state / reset

    def state(self, trial_id: str | None) -> dict[str, Any]:
        issues = []
        for i in self._sentry_issues(trial_id):
            j = self.sentry.issue_json(i)
            j["latest_event"] = self.db.get("sentry_event", i["latest_event_id"]) if i.get("latest_event_id") else None
            j["environments"] = sorted(i["env_stats"])
            issues.append(j)
        chans, msgs = self._slack(trial_id)
        return {
            "trial_id": trial_id,
            "sentry": {"issues": issues},
            "linear": {"issues": [self.linear.issue_json(i) for i in self._linear_issues(trial_id)]},
            "instatus": {"incidents": [self.instatus.incident_json(i) for i in self._instatus_incidents(trial_id)],
                         "components": self.db.all("instatus_component")},
            "slack": {"channels": chans, "messages": sorted(msgs, key=lambda m: float(m["ts"]))},
        }

    def reset(self, trial_id: str | None) -> dict[str, Any]:
        if not trial_id:
            self.db.wipe()
            self.seed()
            return {"ok": True, "scope": "all"}
        removed = {"sentry_issues": 0, "linear_issues": 0, "instatus_incidents": 0, "slack_messages": 0,
                   "slack_channels": 0}
        for i in self._sentry_issues(trial_id):
            for eid in i.get("event_ids", []):
                self.db.delete("sentry_event", eid)
            self.db.delete("sentry_issue", i["id"])
            if i.get("group_hash"):
                self.db.delete("sentry_group", i["group_hash"])
            removed["sentry_issues"] += 1
        for i in self._linear_issues(trial_id):
            for c in self.db.all("linear_comment", lambda c: c["issueId"] == i["id"]):
                self.db.delete("linear_comment", c["id"])
            self.db.delete("linear_issue", i["id"])
            removed["linear_issues"] += 1
        for i in self._instatus_incidents(trial_id):
            self.db.delete("instatus_incident", i["id"])
            removed["instatus_incidents"] += 1
        chans, msgs = self._slack(trial_id)
        for m in msgs:
            self.db.delete("slack_message", f"{m['channel']}:{m['ts']}")
            removed["slack_messages"] += 1
        for c in chans:
            if c["id"] != "C_ONCALL":
                self.db.delete("slack_channel", c["id"])
                removed["slack_channels"] += 1
        return {"ok": True, "scope": trial_id, "removed": removed}


def _marker_in(text: str, marker_fragment: str) -> bool:
    """Exact token match so IJ-TRIAL:t1 does not match IJ-TRIAL:t10."""
    return re.search(re.escape(marker_fragment) + r"(?![\w-])", text or "") is not None


def build_router(admin: Admin) -> APIRouter:
    r = APIRouter()

    @r.get("/__admin/state")
    def state(trial_id: str | None = None):
        return admin.state(trial_id)

    @r.post("/__admin/reset")
    def reset(trial_id: str | None = None):
        return admin.reset(trial_id)

    return r
