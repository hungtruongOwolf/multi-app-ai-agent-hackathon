"""Read-only data access for the console. Never writes to the agent store, the wiki or any app."""

from __future__ import annotations

from judge.paths import KNOWLEDGE_DIR

import difflib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from judge.core.models import Incident, IncidentState, Plan
from judge.core.store import Store
from judge.settings import ROOT, Config, Settings

OPEN_STATES = {s for s in IncidentState} - {IncidentState.RESOLVED, IncidentState.CLOSED}
NOISY_INTENTS = {"slack.post", "linear.comment"}


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class TimelineEvent:
    at: datetime
    kind: str  # alert | triage | approval | public | change | verify | resolve | discussion | memory | escalate
    title: str
    detail: str = ""
    tone: str = "gray"


@dataclass
class PlanView:
    plan: Plan
    status: str
    executions: list[dict] = field(default_factory=list)
    verifications: list[dict] = field(default_factory=list)
    before: dict = field(default_factory=dict)


class ConsoleData:
    def __init__(self, settings: Settings, config: Config | None = None,
                 reports_dir: Path | None = None, http_timeout: float = 1.5):
        self.s = settings
        self.c = config or Config()
        self.reports_dir = reports_dir or (ROOT / "var" / "reports")
        self.http_timeout = http_timeout
        self._store: Store | None = None

    # ------------------------------------------------------------ sources

    @property
    def store(self) -> Store | None:
        if self._store is None and Path(self.s.db_path).exists():
            self._store = Store(self.s.db_path)
        return self._store

    def repo(self):
        from judge.memory.repo import MemoryRepo

        path = Path(self.s.memory_dir)
        if not (path / ".git").exists():
            return None
        return MemoryRepo(path, KNOWLEDGE_DIR)

    def kv(self, key: str, default: Any = None) -> Any:
        return self.store.get_kv(key, default) if self.store else default

    # ------------------------------------------------------------ links

    def slack_thread_url(self, inc: Incident) -> str | None:
        if not inc.slack_thread_ts or not self.s.slack_oncall_channel or self.s.backend != "real":
            return None
        return f"https://slack.com/archives/{self.s.slack_oncall_channel}/p{inc.slack_thread_ts.replace('.', '')}"

    def linear_url(self, inc: Incident) -> tuple[str | None, str | None]:
        info = self.kv(f"{inc.id}:linear") or {}
        return info.get("url"), info.get("identifier")

    def status_page_url(self) -> str | None:
        return os.environ.get("INSTATUS_PAGE_URL") or (
            f"{self.s.sandbox_url}/status/{self.s.instatus_page_id}" if self.s.backend == "sandbox" else None)

    def sentry_links(self, inc: Incident) -> list[dict]:
        out = []
        for sig in self.store.signals_for(inc.id) if self.store else []:
            if sig.source == "sentry" and sig.external_id:
                meta = self.kv(f"sentry_issue_meta:{sig.external_id}") or {}
                out.append({"title": meta.get("title") or f"{sig.error_type} at {sig.culprit}",
                            "url": meta.get("permalink"), "short_id": meta.get("shortId"),
                            "count": sig.count, "users": sig.user_count})
        return out

    def escalation(self, inc: Incident) -> dict:
        """PagerDuty pages for this incident (reason, when) and whether the PagerDuty incident was resolved."""
        pages = [{**p, "at": _dt(p.get("at"))} for p in (self.kv(f"{inc.id}:pages") or [])]
        if not pages and self.kv(f"{inc.id}:paged"):
            pages = [{"at": None, "reason": "on-call paged", "key": ""}]
        resolved = _dt(self.kv(f"{inc.id}:paged_resolved"))
        if pages and resolved is None and inc.resolved_at:
            resolved = _dt(inc.resolved_at)  # the agent resolves the PagerDuty incident when the incident resolves
        return {"pages": pages, "resolved_at": resolved}

    def github_prs(self) -> dict[str, dict]:
        """proposal id -> pull request, from the GitHub mirror state kept in the memory repo."""
        path = Path(self.s.memory_dir) / ".git" / "ij" / "github.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("prs", {})
        except (OSError, ValueError):
            return {}

    def runbook_pr(self, inc: Incident) -> dict | None:
        pr = self.kv(f"{inc.id}:runbook_pr")
        repo = self.repo()
        if not pr and repo is not None:
            prs = self.github_prs()
            for p in repo.proposals():
                if f"incident: {inc.id}\n" in (p.body + "\n") and p.id in prs:
                    pr = {**prs[p.id], "proposal_id": p.id, "title": p.title, "at": p.created_at.isoformat()}
                    break
        if not pr:
            return None
        prop = repo.proposal(pr.get("proposal_id")) if repo is not None and pr.get("proposal_id") else None
        return {**pr, "status": prop.status if prop is not None else "open"}

    def sentry_resolved(self, inc: Incident) -> list[str]:
        ids = list(self.kv(f"{inc.id}:sentry_resolved") or [])
        if not ids and self.store:
            ids = [d.decision_id for d in self.store.decisions(inc.id) if d.intent == "sentry.resolve_issue" and d.allowed]
        return ids

    @staticmethod
    def shoplab_url(path: str = "/") -> str:
        base = os.environ.get("SHOPLAB_SUPERVISOR_URL", "http://127.0.0.1:8800").rstrip("/")
        return f"{base}{path}"

    # ------------------------------------------------------------ incidents

    def incidents(self) -> list[Incident]:
        if not self.store:
            return []
        return sorted(self.store.incidents(), key=lambda i: i.created_at, reverse=True)

    def incident(self, incident_id: str) -> Incident | None:
        return self.store.get_incident(incident_id) if self.store else None

    def plans(self, inc: Incident) -> list[PlanView]:
        if not self.store:
            return []
        rows = self.store._exec("SELECT plan_id FROM plans WHERE incident_id=? ORDER BY ts", (inc.id,)).fetchall()
        views = []
        execs = [x for x in self.store.executions() if x["incident_id"] == inc.id]
        verifs = self.store.verifications(inc.id)
        for (plan_id,) in rows:
            got = self.store.get_plan(plan_id)
            if not got:
                continue
            plan, status = got
            ver = []
            for v in verifs:
                if v["plan_id"] != plan_id:
                    continue
                samples = v.get("samples")
                try:
                    samples = json.loads(samples) if isinstance(samples, str) else (samples or [])
                except ValueError:
                    samples = []
                ver.append({**v, "samples": samples})
            views.append(PlanView(plan, status, [x for x in execs if x["plan_id"] == plan_id], ver,
                                  self.kv(f"{plan_id}:before") or {}))
        return views

    def transitions(self, inc: Incident) -> list[tuple[datetime, str, str]]:
        if not self.store:
            return []
        out = []
        for k, v in self.store._exec("SELECT k, v FROM kv WHERE k LIKE ?", (f"transition:{inc.id}:%",)):
            try:
                ts = datetime.fromtimestamp(float(k.rsplit(":", 1)[-1]), tz=UTC)
            except ValueError:
                continue
            a, _, b = str(json.loads(v) if v.startswith('"') else v).partition("->")
            out.append((ts, a.split(".")[-1], b.split(".")[-1]))
        return sorted(out)

    def timeline(self, inc: Incident) -> list[TimelineEvent]:
        if not self.store:
            return []
        ev: list[TimelineEvent] = []
        signals = self.store.signals_for(inc.id)
        firsts = [s.first_seen for s in signals if s.first_seen]
        start = min([*firsts, inc.created_at]) if firsts else inc.created_at
        detected = ", ".join(sorted({(f"Sentry: {s.error_type}" if s.source == "sentry" else f"SLO {s.slo_name}")
                                     for s in signals})) or inc.incident_key
        ev.append(TimelineEvent(start, "alert", "Problem detected", detected, "red"))
        proposal = self.kv(f"{inc.id}:proposal") or {}
        names = {
            "OPEN": ("triage", "Triaged", "blue"),
            "AWAITING_FIX_APPROVAL": ("approval", "Waiting for an on-call decision", "purple"),
            "VETO_WINDOW": ("approval", "Automatic fix scheduled (veto window)", "purple"),
            "EXECUTING": ("change", "Fix started", "blue"),
            "MONITORING": ("verify", "Verification passed — monitoring", "green"),
            "ESCALATED": ("escalate", "Escalated to a human", "orange"),
            "RESOLVED": ("resolve", "Resolved", "green"),
        }
        for ts, _, to in self.transitions(inc):
            if to in names:
                kind, title, tone = names[to]
                detail = ""
                if to == "OPEN" and proposal:
                    detail = (f"{proposal.get('severity')} · {str(proposal.get('customer_impact', '')).replace('_', ' ')}"
                              f"{' · customer-visible' if proposal.get('customer_visible') else ''}")
                ev.append(TimelineEvent(ts, kind, title, detail, tone))
        verb = {"public_post": "Status page post", "fix": "Fix", "memory_merge": "Runbook update"}
        for a in self.store.approvals(inc.id):
            if a.valid:
                ev.append(TimelineEvent(a.ts, "approval", f"{verb.get(a.kind, a.kind)} {a.verdict}d",
                                        f"by {self.person(a.user_id)} ({'button' if a.via == 'button' else a.via})",
                                        "green" if a.verdict == "approve" else "orange"))
        for d in self.store.decisions(inc.id):
            if d.intent == "instatus.create_incident" and d.allowed:
                ev.append(TimelineEvent(d.ts, "public", "Status page incident posted", "", "purple"))
            elif d.result.value == "DENY" and d.intent not in NOISY_INTENTS and d.rules:
                ev.append(TimelineEvent(d.ts, "escalate", f"Blocked by policy {', '.join(d.rules)}", d.explain, "orange"))
        for change in self.kv(f"{inc.id}:changes") or []:
            at = _dt(change.get("at"))
            if at:
                ev.append(TimelineEvent(at, "change", "Change applied", change.get("change", ""), "blue"))
        for pv in self.plans(inc):
            for x in pv.executions:
                if x["kind"] in ("rollback", "revert"):
                    ev.append(TimelineEvent(_dt(x["ts"]), "change", "Change rolled back", pv.plan.action, "orange"))
            for v in pv.verifications:
                tone = "green" if v["result"] == "pass" else "red"
                ev.append(TimelineEvent(_dt(v["ts"]), "verify", f"Verification {v['result']}", pv.plan.action, tone))
        for msg in self.kv(f"{inc.id}:discussion") or []:
            at = _dt(msg.get("ts"))
            if at:
                who = msg.get("author") or ("Incident Judge" if msg.get("role") == "agent" else "on-call")
                ev.append(TimelineEvent(at, "discussion", f"{who}", msg.get("text", ""), "gray"))
        esc = self.escalation(inc)
        for page in esc["pages"]:
            if page["at"]:
                ev.append(TimelineEvent(page["at"], "escalate", "Paged on-call via PagerDuty", page.get("reason", ""), "red"))
        if esc["resolved_at"]:
            ev.append(TimelineEvent(esc["resolved_at"], "resolve", "PagerDuty incident resolved", "", "green"))
        if self.sentry_resolved(inc):
            for d in self.store.decisions(inc.id):
                if d.intent == "sentry.resolve_issue" and d.allowed:
                    ev.append(TimelineEvent(d.ts, "resolve", "Sentry issues marked resolved",
                                            "a recurrence will show up as a regression", "green"))
                    break
        pr = self.runbook_pr(inc)
        if pr and _dt(pr.get("at")):
            ev.append(TimelineEvent(_dt(pr["at"]), "memory", f"Runbook update proposed as GitHub PR #{pr['number']}",
                                    pr.get("title", ""), "purple"))
        if self.kv(f"{inc.id}:diagnosis"):
            ev.append(TimelineEvent(inc.state_entered_at if inc.state == IncidentState.OPEN else inc.created_at,
                                    "triage", "Diagnosis written", "See the diagnosis panel", "blue"))
        seen = set()
        uniq = []
        for item in sorted((x for x in ev if x.at), key=lambda x: x.at):
            key = (item.at.replace(microsecond=0), item.title)
            if key not in seen:
                seen.add(key)
                uniq.append(item)
        return uniq

    def person(self, user_id: str) -> str:
        return self.kv(f"slack_user:{user_id}") or user_id

    def audit(self, inc: Incident, include_noise: bool = False) -> list[dict]:
        if not self.store:
            return []
        return [{"ts": d.ts, "intent": d.intent, "result": d.result.value, "rules": d.rules, "explain": d.explain}
                for d in self.store.decisions(inc.id) if include_noise or d.intent not in NOISY_INTENTS]

    # ------------------------------------------------------------ overview

    def overview(self) -> dict:
        incs = self.incidents()
        open_ = [i for i in incs if i.state in OPEN_STATES]
        resolved = [i for i in incs if i.resolved_at]
        mttr = None
        if resolved:
            mttr = sum((i.resolved_at - i.created_at).total_seconds() for i in resolved) / len(resolved)
        heartbeat = _dt(self.kv("agent_heartbeat"))
        return {
            "open": open_, "recent_resolved": resolved[:6], "total": len(incs), "mttr_s": mttr,
            "heartbeat": heartbeat,
            "agent_live": bool(heartbeat and (datetime.now(UTC) - heartbeat).total_seconds() < 30),
            "runbooks": self.runbooks(), "integrations": self.integrations(),
            "waiting": [i for i in open_ if i.state in (IncidentState.AWAITING_FIX_APPROVAL, IncidentState.VETO_WINDOW,
                                                         IncidentState.AWAITING_PUBLIC_APPROVAL)],
        }

    def integrations(self) -> list[dict]:
        s = self.s
        real = s.backend == "real"

        def configured(value: str) -> bool:
            return bool(value) and value not in ("sandbox", "xoxb-sandbox")

        last: dict[str, datetime] = {}
        prefixes = {"sentry.": "Sentry", "linear.": "Linear", "instatus.": "Instatus", "slack.": "Slack",
                    "pagerduty.": "PagerDuty", "memory.": "GitHub"}
        for d in (self.store.decisions() if self.store else []):
            if not d.allowed:
                continue
            for prefix, app in prefixes.items():
                ts = _dt(d.ts)
                if d.intent.startswith(prefix) and (app not in last or ts > last[app]):
                    last[app] = ts
        if self.store:
            opened = [_dt(i.created_at) for i in self.store.incidents()]
            if opened:
                last["Sentry"] = max(opened + ([last["Sentry"]] if "Sentry" in last else []))
        if not (real and s.github_token and s.github_memory_repo):
            last.pop("GitHub", None)
        rows = [
            ("Sentry", "error signals; issues resolved with the incident", configured(s.sentry_token) if real else True),
            ("Linear", "one ticket per incident", configured(s.linear_api_key) if real else True),
            ("Instatus", "public status page, templates only", configured(s.instatus_api_key) if real else True),
            ("Slack", "incident thread and approval cards", configured(s.slack_bot_token) if real else True),
            ("Slack buttons", "Socket Mode, no public URL", bool(s.slack_app_token) if real else False),
            ("PagerDuty", "pages on-call when a human must take over", configured(s.pagerduty_routing_key)),
            ("GitHub", "runbook updates as pull requests", real and bool(s.github_token and s.github_memory_repo)),
            ("Claude", "judge, diagnosis, discussion, wiki writer", bool(s.anthropic_api_key) and s.judge_impl == "claude"),
        ]
        mode = "real account" if real else "local sandbox"
        detail = {"GitHub": s.github_memory_repo if real else ""}
        return [{"name": n, "role": r, "ok": ok, "mode": mode, "last": last.get(n), "detail": detail.get(n, "")}
                for n, r, ok in rows]

    # ------------------------------------------------------------ wiki

    def runbooks(self) -> list[dict]:
        repo = self.repo()
        if repo is None:
            return []
        out = []
        for rb in repo.runbooks():
            fm = rb.frontmatter
            out.append({"id": fm.id, "title": fm.title, "level": fm.autonomy.level.value, "cap": fm.autonomy.cap.value,
                        "review": fm.autonomy.review_required, "success": fm.stats.success,
                        "failure": fm.stats.failure, "last_verified": fm.stats.last_verified,
                        "action": fm.action.name if fm.action else None,
                        "services": fm.signatures.services})
        return sorted(out, key=lambda r: r["id"])

    def runbook(self, runbook_id: str):
        repo = self.repo()
        return repo.get_runbook(runbook_id) if repo else None

    def docs(self) -> list[str]:
        repo = self.repo()
        if repo is None:
            return []
        files = [f for f in repo.list_files("wiki") if f.endswith(".md") and not f.startswith("wiki/runbooks/")]
        return sorted(files, key=lambda f: (f.count("/"), f))

    def doc(self, path: str) -> str | None:
        repo = self.repo()
        if repo is None or not path.startswith("wiki/") or ".." in path:
            return None
        return repo.read(path)

    def proposals(self) -> list[dict]:
        repo = self.repo()
        if repo is None:
            return []
        out = []
        prs = self.github_prs()
        for p in sorted(repo.proposals(), key=lambda p: p.created_at, reverse=True):
            diffs = []
            for f in p.files:
                old = repo.read(f) or ""
                new = repo.read(f, ref=p.branch) or ""
                diffs.append({"file": f, "lines": list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                                                            "current", "proposed", lineterm="", n=2))})
            out.append({"proposal": p, "diffs": diffs, "pr": prs.get(p.id)})
        return out

    # ------------------------------------------------------------ shoplab

    def shoplab(self) -> dict:
        base = self.shoplab_url("")
        out: dict = {"reachable": False, "services": [], "changes": [], "storefront": self.shoplab_url("/"),
                     "ops": self.shoplab_url("/ops")}
        try:
            with httpx.Client(timeout=self.http_timeout) as client:
                services = client.get(f"{base}/services").json()
                out["reachable"] = True
                for svc in services:
                    health = {"name": svc.get("name"), "environment": svc.get("environment"),
                              "alive": svc.get("alive"), "url": svc.get("url")}
                    try:
                        cfg = client.get(f"{base}/config/{svc.get('name')}").json()
                        health.update(flags=cfg.get("flags"), pool_size=cfg.get("pool_size"),
                                      version=cfg.get("app_version"), faults=list((cfg.get("faults") or {}).keys()))
                    except Exception:
                        pass
                    out["services"].append(health)
                try:
                    r = client.get(f"{base}/changes")
                    if r.status_code == 200:
                        out["changes"] = list(reversed(r.json()))[:30]
                except Exception:
                    pass
        except Exception:
            pass
        return out

    # ------------------------------------------------------------ evals

    def reports(self) -> list[dict]:
        out = []
        if not self.reports_dir.exists():
            return out
        for d in self.reports_dir.iterdir():
            f = d / "results.json"
            if f.is_file():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                out.append({"dir": d.name, "mtime": f.stat().st_mtime, **data})
        return sorted(out, key=lambda r: r["mtime"], reverse=True)
