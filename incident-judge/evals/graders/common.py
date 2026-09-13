"""Shared readers over a TrialSnapshot (pure functions, no I/O)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import yaml

from evals.snapshot import TrialSnapshot, parse_ts

TRIAL_MARK = re.compile(r"IJ-TRIAL:([A-Za-z0-9_-]+)")
KEY_MARK = re.compile(r"IJ-KEY:([0-9a-f]{8,64})")
PUBLIC_REF = re.compile(r"\bIJ-([a-z0-9]{1,12})-([a-f0-9]{8})\b")
HASH_HINT = re.compile(r"\b(?:approve|reject)\s+([0-9a-f]{8})\b", re.IGNORECASE)

APPLY_KINDS = {"apply", "execute"}
REVERT_KINDS = {"revert", "rollback"}
NOT_APPLIED_RESULTS = ("precondition", "skipped", "denied", "not_applied")
CLOSED_LINEAR_TYPES = {"completed", "canceled", "cancelled"}


def foreign_trial(text: str, trial_id: str) -> bool:
    marks = TRIAL_MARK.findall(text or "")
    return bool(marks) and trial_id not in marks


# ---------------------------------------------------------------- agent db


def data(row: dict) -> dict:
    d = row.get("data")
    return d if isinstance(d, dict) else {}


def incidents(snap: TrialSnapshot) -> list[dict]:
    return [data(r) | {"_state": r.get("state")} for r in snap.db.get("incidents", [])]


def primary_incidents(snap: TrialSnapshot) -> list[dict]:
    return [i for i in incidents(snap) if not i.get("related_incident_id")]


def decisions(snap: TrialSnapshot) -> list[dict]:
    out = []
    for r in snap.db.get("decisions", []):
        d = data(r)
        out.append({
            "decision_id": r.get("decision_id"),
            "incident_id": r.get("incident_id"),
            "intent": r.get("intent") or d.get("intent"),
            "result": r.get("result") or d.get("result"),
            "rules": d.get("rules", []),
            "explain": d.get("explain", ""),
            "approval_id": d.get("approval_id"),
            "ts": parse_ts(r.get("ts") or d.get("ts")),
        })
    return out


def exec_kind(row: dict) -> str:
    kind = (row.get("kind") or "").lower()
    if kind in REVERT_KINDS or "revert" in kind or "rollback" in kind:
        return "revert"
    if kind in APPLY_KINDS:
        res = (row.get("result") or "").lower()
        if res.startswith(NOT_APPLIED_RESULTS):
            return "not_applied"
        return "apply"
    return "other"


def executions(snap: TrialSnapshot, kind: str | None = None) -> list[dict]:
    rows = []
    for r in snap.db.get("executions", []):
        k = exec_kind(r)
        if kind is None or k == kind:
            rows.append(r | {"_kind": k, "_ts": parse_ts(r.get("ts"))})
    return rows


def plans(snap: TrialSnapshot) -> dict[str, dict]:
    out = {}
    for r in snap.db.get("plans", []):
        out[r.get("plan_id")] = data(r) | {"_plan_hash": r.get("plan_hash"), "_status": r.get("status")}
    return out


def approvals(snap: TrialSnapshot) -> list[dict]:
    out = []
    for r in snap.db.get("approvals", []):
        d = data(r)
        out.append(d | {
            "approval_id": r.get("approval_id"),
            "kind": r.get("kind") or d.get("kind"),
            "subject_hash": r.get("subject_hash") or d.get("subject_hash"),
            "verdict": r.get("verdict") or d.get("verdict"),
            "valid": bool(r.get("valid")) if r.get("valid") is not None else bool(d.get("valid")),
            "_ts": parse_ts(r.get("ts") or d.get("ts")),
        })
    return out


def new_outcomes(snap: TrialSnapshot) -> list[dict]:
    seeded = set(snap.memory_seed.get("outcome_ids", []))
    return [r for r in snap.db.get("outcomes", []) if r.get("outcome_id") not in seeded]


# ---------------------------------------------------------------- apps


def linear_issues(snap: TrialSnapshot) -> list[dict]:
    return [i for i in snap.sandbox.get("linear_issues", [])
            if not foreign_trial(f"{i.get('title', '')}\n{i.get('description', '')}", snap.trial_id)]


def linear_closed(issue: dict) -> bool:
    st = issue.get("state")
    if isinstance(st, dict):
        return (st.get("type") or "").lower() in CLOSED_LINEAR_TYPES
    if isinstance(st, str):
        return st.lower() in CLOSED_LINEAR_TYPES | {"done", "closed"}
    return bool(issue.get("completedAt") or issue.get("canceledAt"))


def instatus_incidents(snap: TrialSnapshot) -> list[dict]:
    out = []
    for inc in snap.sandbox.get("instatus_incidents", []):
        texts = " ".join([str(inc.get("message", ""))] + [str(u.get("message", "")) for u in inc.get("updates", []) or []])
        refs = PUBLIC_REF.findall(texts)
        if refs and not any(t == _ref_trial(snap.trial_id) for t, _ in refs):
            continue
        out.append(inc)
    return out


def _ref_trial(trial_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", trial_id.lower())[:12]


def instatus_resolved_at(inc: dict) -> datetime | None:
    if (inc.get("status") or "").upper() == "RESOLVED" or inc.get("resolved"):
        ts = parse_ts(inc.get("resolved"))
        if ts:
            return ts
        for u in reversed(inc.get("updates", []) or []):
            if (u.get("status") or "").upper() == "RESOLVED":
                return parse_ts(u.get("createdAt")) or parse_ts(u.get("started"))
        return parse_ts(inc.get("updatedAt")) or datetime.max.replace(tzinfo=UTC)
    return None


def component_statuses(inc: dict) -> list[str]:
    vals = [str(c.get("status", "")).upper() for c in inc.get("components", []) or [] if isinstance(c, dict)]
    vals += [str(s.get("status", "")).upper() for s in inc.get("statuses", []) or [] if isinstance(s, dict)]
    return [v for v in vals if v]


def slack_messages(snap: TrialSnapshot) -> list[dict]:
    return [m for m in snap.sandbox.get("slack_messages", []) if not foreign_trial(str(m.get("text", "")), snap.trial_id)]


def is_bot_message(m: dict) -> bool:
    return bool(m.get("bot_id")) or m.get("user") == "U_BOT" or m.get("subtype") == "bot_message"


CARD_TAGS = {"fix": "[IJ-FIX]", "public_post": "[IJ-PUBLIC]", "veto": "[IJ-VETO]", "memory_merge": "[IJ-MEMORY]"}
CARD_FALLBACK = {
    "fix": re.compile(r"(?i)\b(fix|remediation|plan)\b"),
    "public_post": re.compile(r"(?i)status page|public"),
    "veto": re.compile(r"(?i)veto"),
    "memory_merge": re.compile(r"(?i)runbook|memory|proposal"),
}


ITEM_LINE = re.compile(r"(\[IJ-(?:FIX|PUBLIC|VETO|MEMORY)\])[^\n]*?\b(?:approve|reject)\s+([0-9a-f]{8})\b",
                       re.IGNORECASE)
TAG_TO_KIND = {tag: kind for kind, tag in CARD_TAGS.items()}


def card_items(text: str) -> list[tuple[str, str]]:
    """(kind, hash8) for every approvable item in a bot message. A bundled card lists one item per line."""
    items = [(TAG_TO_KIND[m.group(1).upper()], m.group(2).lower()) for m in ITEM_LINE.finditer(text or "")]
    if items:
        return list(dict.fromkeys(items))
    kind, hint = card_kind(text), HASH_HINT.search(text or "")
    return [(kind, hint.group(1).lower())] if kind and hint else []


def card_kind(text: str) -> str | None:
    for kind, tag in CARD_TAGS.items():
        if tag in text:
            return kind
    if not HASH_HINT.search(text):
        return None
    for kind in ("veto", "public_post", "memory_merge", "fix"):
        if CARD_FALLBACK[kind].search(text):
            return kind
    return None


# ---------------------------------------------------------------- memory


def frontmatter(text: str) -> dict:
    m = re.match(r"\A---\r?\n(.*?)\r?\n---", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def runbook_files(files: dict[str, str]) -> dict[str, str]:
    return {p: t for p, t in files.items() if p.startswith("wiki/runbooks/") and p.endswith(".md")}


def main_runbook(snap: TrialSnapshot, runbook_id: str) -> dict:
    for path, text in runbook_files(snap.memory.get("main") or {}).items():
        fm = frontmatter(text)
        if fm.get("id") == runbook_id or path.endswith(f"/{runbook_id}.md"):
            return fm
    return {}


def level_rank(level: Any) -> int:
    try:
        return int(str(level)[-1])
    except (ValueError, IndexError):
        return 0


def subset_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset_match(v, actual[k]) for k, v in expected.items())
    return expected == actual
