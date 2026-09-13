"""TrialSnapshot: everything a grader may look at, captured AFTER the agent is stopped.

Sources (all read back, never taken from the agent's own claims):
- sandbox /__admin/state        -> the apps' final state (Linear, Instatus, Slack, Sentry)
- agent SQLite (read-only)      -> audit log: decisions, steps, executions, approvals, outcomes
- memory repo (git)             -> wiki files on main + proposal branches
- supervisor /config/*          -> ShopLab runtime config at the end
- runner timeline               -> fault windows, human actions, restarts
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DB_TABLES = ["incidents", "signals", "steps", "decisions", "external_writes", "plans", "executions",
             "verifications", "approvals", "outcomes", "kv"]


@dataclass
class TrialSnapshot:
    trial_id: str
    scenario_id: str
    started_at: datetime
    ended_at: datetime
    sandbox: dict[str, list[dict]] = field(default_factory=dict)  # normalized, see normalize_sandbox
    db: dict[str, list[dict]] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)  # {"main": {path: text}, "proposals": [...]}
    memory_seed: dict[str, Any] = field(default_factory=dict)  # {"outcome_ids": [...], "runbooks": {id: fm dict}}
    config_end: dict[str, dict] = field(default_factory=dict)
    fault_windows: list[dict] = field(default_factory=list)  # {"fault","service","start","end"}
    human_actions: list[dict] = field(default_factory=list)
    agent_restarts: int = 0
    agent_exit_codes: list[int | None] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # harness errors (not agent failures)

    def to_json(self) -> dict:
        def conv(o):
            if isinstance(o, datetime):
                return o.isoformat()
            raise TypeError(type(o))
        return json.loads(json.dumps(self.__dict__, default=conv))


# ---------------------------------------------------------------- time helpers


def parse_ts(v: Any) -> datetime | None:
    """ISO strings, Slack-style epoch strings ('1726000000.000100'), epoch numbers."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), UTC)
    s = str(v)
    try:
        return datetime.fromtimestamp(float(s), UTC)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


# ---------------------------------------------------------------- sandbox


def _pick(d: dict, *paths: str) -> list:
    for p in paths:
        cur: Any = d
        for part in p.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if isinstance(cur, list):
            return cur
        if isinstance(cur, dict) and cur and all(isinstance(v, dict) for v in cur.values()):
            return list(cur.values())
    return []


def normalize_sandbox(raw: dict) -> dict[str, list[dict]]:
    """Tolerant of nesting: {"linear": {"issues": [...]}} or {"linear_issues": [...]}."""
    raw = raw or {}
    channels = _pick(raw, "slack.channels", "slack_channels")
    messages = _pick(raw, "slack.messages", "slack_messages")
    if not messages:
        for ch in channels:
            for m in ch.get("messages", []) or []:
                messages.append({"channel": ch.get("id"), **m})
    for m in messages:  # flatten nested thread replies if present
        for r in m.get("replies", []) or []:
            if isinstance(r, dict):
                messages.append({"channel": m.get("channel"), "thread_ts": m.get("ts"), **r})
    for m in messages:  # dedupe markers travel as Slack metadata; graders treat them as part of the message
        ref = ((m.get("metadata") or {}).get("event_payload") or {}).get("ref")
        if ref and ref not in str(m.get("text", "")):
            m["text"] = f"{m.get('text', '')}\n{ref}"
    issues = _pick(raw, "linear.issues", "linear_issues")
    comments = _pick(raw, "linear.comments", "linear_comments")
    for c in comments:
        for i in issues:
            if i.get("id") == (c.get("issueId") or c.get("issue_id")):
                i.setdefault("comments", [])
                if c not in i["comments"]:
                    i["comments"].append(c)
    for i in issues:
        cm = i.get("comments")
        if isinstance(cm, dict):
            i["comments"] = cm.get("nodes", [])
    return {
        "sentry_issues": _pick(raw, "sentry.issues", "sentry_issues"),
        "linear_issues": issues,
        "instatus_incidents": _pick(raw, "instatus.incidents", "instatus_incidents"),
        "slack_channels": channels,
        "slack_messages": messages,
    }


# ---------------------------------------------------------------- agent db


def load_db(path: Path, attempts: int = 8) -> dict[str, list[dict]]:
    """Retry: right after the agent process is hard-killed on Windows its -shm mapping can still be held,
    and SQLite reports a transient 'disk I/O error'."""
    import time

    for i in range(attempts):
        try:
            return _load_db_once(path)
        except sqlite3.OperationalError as e:
            if "disk I/O" not in str(e) and "locked" not in str(e) or i == attempts - 1:
                raise
            time.sleep(0.5 * (i + 1))
    return _load_db_once(path)


def _load_db_once(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {t: [] for t in DB_TABLES}
    if not Path(path).exists():
        return out
    # Not mode=ro: a read-only connection cannot recover a WAL left behind by a killed agent (SQLITE_IOERR).
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA query_only=1")
    conn.row_factory = sqlite3.Row
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in DB_TABLES:
            if t not in existing:
                continue
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
            for r in rows:
                if isinstance(r.get("data"), str):
                    try:
                        r["data"] = json.loads(r["data"])
                    except json.JSONDecodeError:
                        pass
            out[t] = rows
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------- memory repo


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def _tree(repo: Path, ref: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in _git(repo, "ls-tree", "-r", "--name-only", ref).splitlines():
        if path.endswith((".md", ".yaml", ".yml", ".json")):
            try:
                files[path] = _git(repo, "show", f"{ref}:{path}")
            except RuntimeError:
                pass
    return files


def load_memory(repo: Path, proposals_json: Path | None = None) -> dict[str, Any]:
    """main tree + every non-main branch; proposal records if the memory lane persists them."""
    repo = Path(repo)
    out: dict[str, Any] = {"main": {}, "branches": {}, "proposals": [], "commits_main": []}
    if not (repo / ".git").exists():
        return out
    try:
        out["main"] = _tree(repo, "main")
        out["commits_main"] = _git(repo, "log", "main", "--format=%H|%an|%s").splitlines()
        for b in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
            if b != "main":
                out["branches"][b] = _tree(repo, b)
    except RuntimeError as e:
        out["error"] = str(e)
    records: list[dict] = []
    candidates = [proposals_json] if proposals_json else []
    candidates += [repo / ".git" / "ij" / "proposals.json"]
    candidates += list(repo.parent.glob(f"{repo.name}-proposals*.json")) + list((repo / ".ij").glob("proposals*.json"))
    for c in candidates:
        if c and Path(c).exists():
            try:
                data = json.loads(Path(c).read_text(encoding="utf-8"))
                records += data if isinstance(data, list) else list(data.values())
            except (json.JSONDecodeError, OSError):
                pass
    out["proposals"] = records
    return out
