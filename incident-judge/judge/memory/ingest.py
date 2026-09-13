"""Ingest (SPEC §9.5). Runs when an incident resolves.

- write_raw: CODE turns the agent's durable record into an immutable, redacted timeline under raw/
  and appends a factual line to wiki/log.md (commit straight to main — facts, not opinions).
- propose:   the WRITER (LLM or heuristic) rewrites runbook prose; code assembles the page,
  keeps code-owned frontmatter, regenerates index.md, and opens a proposal for human review.
  A new page is only proposed on the 2nd occurrence of an incident class.
"""

from __future__ import annotations

import json
import re

import yaml

from judge.core.models import Incident, OutcomeResult, now
from judge.core.store import Store
from judge.memory.index import LOG_HEADER, render_index
from judge.memory.llm_protocols import RunbookWriter
from judge.memory.repo import MemoryRepo, Proposal
from judge.memory.schema import (
    REQUIRED_SECTIONS,
    Runbook,
    RunbookAction,
    RunbookFrontmatter,
    Signatures,
    split_frontmatter,
)
from judge.safety.redact import find_forbidden, redact
from judge.settings import Config

RAW_DIR = "raw/incidents"
MAX_RAW_CONTEXT = 5


def raw_path(incident: Incident) -> str:
    return f"{RAW_DIR}/{incident.created_at:%Y-%m-%d}-{incident.id}.md"


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


class Ingestor:
    def __init__(self, repo: MemoryRepo, store: Store, config: Config, writer: RunbookWriter):
        self.repo = repo
        self.store = store
        self.config = config
        self.writer = writer

    # ------------------------------------------------------------ facts

    def _incident_facts(self, incident: Incident) -> dict:
        signals = self.store.signals_for(incident.id)
        outcomes = [o for o in self.store.outcomes() if o.incident_id == incident.id]
        by_plan = {o.plan_id: o for o in outcomes}
        executions = [e for e in self.store.executions() if e["incident_id"] == incident.id]
        actions, seen = [], set()
        for e in executions:  # one entry per plan; the outcome carries the verified result
            if e["plan_id"] in seen:
                continue
            seen.add(e["plan_id"])
            o = by_plan.get(e["plan_id"])
            actions.append({"plan_id": e["plan_id"], "action": e["action"], "params": json.loads(e["params"] or "{}"),
                            "service": e["service"], "result": o.result.value if o else None})
        slo_fps = {s.fingerprint for s in signals if s.source == "slo"}
        fingerprints = sorted(({s.fingerprint for s in signals} | {incident.incident_key}) - slo_fps)
        return {
            "incident_id": incident.id,
            "incident_key": incident.incident_key,
            "fingerprints": fingerprints,
            "environment": incident.environment.value,
            "services": list(incident.services),
            "error_types": sorted({s.error_type for s in signals if s.error_type and s.source != "slo"}),
            "severity": incident.severity.value if incident.severity else None,
            "customer_impact": incident.customer_impact.value if incident.customer_impact else None,
            "runbook_id": incident.runbook_id,
            "created_at": incident.created_at.isoformat(),
            "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
            "actions": actions,
            "trial_id": incident.trial_id,
        }, signals

    def _timeline(self, incident: Incident, facts: dict, signals) -> str:
        body = [f"# Incident {incident.id}", "", "## Signals"]
        for s in signals:
            body.append(f"- {s.last_seen or ''} {s.source} {s.service}/{s.environment.value} `{s.error_type}` "
                        f"culprit=`{s.culprit}` count={s.count} users={s.user_count} slo={s.slo_name or '-'}")
            if s.message_redacted:
                body.append(f"  - message (untrusted): {redact(s.message_redacted, 300)}")
        body += ["", "## Decisions"]
        for d in self.store.decisions(incident.id):
            body.append(f"- {d.ts.isoformat()} `{d.intent}` → **{d.result.value}** {d.rules} {redact(d.explain, 300)}")
        body += ["", "## Approvals"]
        for a in self.store.approvals(incident.id):
            body.append(f"- {a.ts.isoformat()} {a.kind} by {a.user_id} via {a.via}: {a.verdict} "
                        f"(valid={a.valid}{', ' + redact(a.reason, 120) if a.reason else ''})")
        body += ["", "## Remediation"]
        for e in (e for e in self.store.executions() if e["incident_id"] == incident.id):
            body.append(f"- {e['ts']} {e['kind']} `{e['action']}` {e['params']} on {e['service']} → {e['result']}")
        rows = self.store._exec("SELECT plan_id, result, ts FROM verifications WHERE incident_id=? ORDER BY id",
                                (incident.id,)).fetchall()
        for r in rows:
            body.append(f"- {r['ts']} verify {r['plan_id']} → {r['result']}")
        header = yaml.safe_dump(facts, sort_keys=False, allow_unicode=True, width=120).rstrip()
        text = "---\n" + header + "\n---\n\n" + "\n".join(body) + "\n"
        text = redact(text)
        leftover = find_forbidden(text)
        if leftover:  # redaction must be idempotent and complete; never commit otherwise
            raise ValueError(f"raw timeline still contains forbidden content: {leftover}")
        return text

    def _raw_for_class(self, fingerprints: set[str]) -> list[tuple[str, dict, str]]:
        out = []
        for rel in self.repo.list_files(RAW_DIR):
            if not rel.endswith(".md"):
                continue
            text = self.repo.read(rel) or ""
            try:
                fm, _ = split_frontmatter(text)
            except ValueError:
                continue
            if fingerprints & set(fm.get("fingerprints", [])):
                out.append((rel, fm, text))
        out.sort(key=lambda t: t[1].get("created_at", ""))
        return out

    def _runbook_for(self, incident: Incident, fingerprints: set[str]) -> Runbook | None:
        if incident.runbook_id:
            rb = self.repo.get_runbook(incident.runbook_id)
            if rb:
                return rb
        for rb in self.repo.runbooks():
            if fingerprints & set(rb.frontmatter.signatures.fingerprints):
                return rb
        return None

    # ------------------------------------------------------------ public API

    def write_raw(self, incident: Incident) -> str:
        facts, signals = self._incident_facts(incident)
        rel = raw_path(incident)
        fps = set(facts["fingerprints"])
        files = {rel: self._timeline(incident, facts, signals)}

        existing_raw = self.repo.read(rel)
        if existing_raw is None:
            occurrences = len(self._raw_for_class(fps)) + 1
            runbook = self._runbook_for(incident, fps)
            results = ",".join(a["result"] or "none" for a in facts["actions"]) or "no-action"
            label = " | candidate" if runbook is None and occurrences == 1 else ""
            line = (f"- {now():%Y-%m-%dT%H:%MZ} | {incident.id} | fp:{incident.incident_key} | "
                    f"{','.join(incident.services)} | {facts['severity'] or '-'} | {results} | "
                    f"runbook:{runbook.id if runbook else '-'} | occurrence:{occurrences}{label}")
            log = self.repo.read("wiki/log.md") or LOG_HEADER
            files["wiki/log.md"] = log.rstrip("\n") + "\n" + redact(line) + "\n"
        self.repo.commit_code_owned(files, f"raw: timeline for {incident.id}")
        return rel

    async def propose(self, incident: Incident) -> Proposal | None:
        facts, signals = self._incident_facts(incident)
        fps = set(facts["fingerprints"])
        raws = self._raw_for_class(fps)
        if not any(rel == raw_path(incident) for rel, _, _ in raws):
            self.write_raw(incident)
            raws = self._raw_for_class(fps)
        raw_texts = [t for _, _, t in raws[-MAX_RAW_CONTEXT:]]
        agents_md = self.repo.read("AGENTS.md") or ""
        existing = self._runbook_for(incident, fps)

        if existing is None and len(raws) < 2:
            return None  # first occurrence: raw + candidate log line only

        sections = await self.writer.write(existing, raw_texts, agents_md)
        clean = {name: redact(sections.get(name, "")).strip() for name in REQUIRED_SECTIONS}

        if existing is not None:
            rb = existing.copy()
            for name in REQUIRED_SECTIONS:
                if clean[name]:
                    rb.sections[name] = clean[name]
            # Signatures only grow from incidents this runbook's fix verifiably resolved. A look-alike that was found
            # by fingerprint but rejected by match_conditions must not teach the runbook to match it next time.
            fixed_by_it = incident.runbook_id == rb.id and any(a.get("result") == "success" for a in facts["actions"])
            if fixed_by_it:
                sig = rb.frontmatter.signatures
                sig.fingerprints = sorted(set(sig.fingerprints) | fps)
                sig.error_types = sorted(set(sig.error_types) | set(facts["error_types"]))
                sig.services = sorted(set(sig.services) | set(facts["services"]))
            if rb.to_markdown() == existing.to_markdown():
                return None
            title = f"Update runbook {rb.id} from {incident.id}"
        else:
            error_type = (facts["error_types"] or ["incident"])[0]
            base_id = _slug(f"{incident.primary_service}-{error_type}")[:60] or "incident"
            rid, n = base_id, 2
            while self.repo.get_runbook(rid) is not None:
                rid, n = f"{base_id}-{n}", n + 1
            action = self._verified_action(raws)
            rb = Runbook(
                RunbookFrontmatter(
                    id=rid,
                    title=f"{error_type} on {incident.primary_service}",
                    signatures=Signatures(fingerprints=sorted(fps), error_types=sorted(set(facts["error_types"])),
                                          services=sorted(set(facts["services"]))),
                    action=action,
                ),
                clean,
            )
            title = f"New runbook {rid} (occurrence {len(raws)} of {incident.incident_key})"

        others = [r for r in self.repo.runbooks() if r.id != rb.id]
        files = {rb.path: rb.to_markdown(), "wiki/index.md": render_index([*others, rb])}
        body = (f"incident: {incident.id}\nincident_key: {incident.incident_key}\n"
                f"raw: {', '.join(rel for rel, _, _ in raws[-MAX_RAW_CONTEXT:])}\n"
                "Code-owned fields (stats, autonomy) are ignored on merge.")
        return self.repo.create_proposal(files, title, body)

    @staticmethod
    def _verified_action(raws: list[tuple[str, dict, str]]) -> RunbookAction | None:
        """A new page's action comes only from a verified success in the raw record (code fact)."""
        for _, fm, _ in reversed(raws):
            for a in reversed(fm.get("actions", [])):
                if a.get("result") == OutcomeResult.success.value:
                    return RunbookAction(name=a["action"], params=a.get("params") or {})
        return None
