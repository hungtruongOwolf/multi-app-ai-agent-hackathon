"""The on-call agent: a durable per-incident state machine.

tick():
  1. poll signals (Sentry + SLO) -> attach to incidents or open new ones
  2. advance every non-terminal incident one step
  3. process pending memory proposals

Every external write: Intent -> policy.evaluate -> Decision (stored) -> Outbox (idempotent, marker-reconciled).
The LLM only produces a TriageProposal; it never holds credentials."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from judge.core.models import (
    AutonomyLevel,
    CustomerImpact,
    Decision,
    DecisionResult,
    Env,
    Incident,
    IncidentState,
    Intent,
    Severity,
    Signal,
    TriageProposal,
    VerifyResult,
    now,
)
from judge.core.outbox import Outbox, parse_marker, slack_thread_link
from judge.core.store import Store
from judge.policy.engine import PolicyContext, evaluate, public_subject_hash
from judge.reasoning.context import TriageContext
from judge.reasoning.judge import JudgeError
from judge.safety import templates
from judge.safety.redact import redact
from judge.settings import Config, Settings

STATUS_FLOW = ("Flow: triage → your approval (if needed) → fix → verify on SLOs → monitoring → resolved. "
               "Anything unapproved or unverifiable stops safely and waits for a human.")

log = logging.getLogger("judge.agent")

LINEAR_PRIORITY = {Severity.SEV1: 1, Severity.SEV2: 2, Severity.SEV3: 3, Severity.SEV4: 4}
ACTIVE = {IncidentState.OPEN, IncidentState.ESCALATED, IncidentState.MONITORING,
          IncidentState.AWAITING_PUBLIC_APPROVAL, IncidentState.AWAITING_FIX_APPROVAL, IncidentState.VETO_WINDOW}
RETRYABLE_DENY = {"P9", "P15"}
METRIC_NAMES = ["error_rate", "rps", "latency_p95", "pool_utilization", "pool_wait_p95", "db_query_p95", "memory_mb"]
RESOLVE_REQUEST = re.compile(r"\b(resolve|close it|looks fine|all good|it's fixed)\b", re.I)


@dataclass
class Deps:
    settings: Settings
    config: Config
    store: Store
    sentry: Any
    linear: Any
    instatus: Any
    slack: Any
    control: Any
    metrics: Any
    repo: Any
    query: Any
    ingestor: Any
    runner: Any
    approval_poller: Any
    judge: Any
    sentry_poller: Any
    slo_poller: Any
    baseline: str | None = None
    extras: dict = field(default_factory=dict)


class Agent:
    def __init__(self, deps: Deps):
        self.d = deps
        self.s = deps.settings
        self.c = deps.config
        self.store = deps.store
        self.outbox = Outbox(deps.store, deps.settings.trial_id)
        self.tasks: dict[str, asyncio.Task] = {}
        self.baseline = deps.baseline
        self.socket = None  # Slack Socket Mode card buttons (real mode, optional)

    # ================================================================ loop

    async def _mirror_main(self, message: str) -> None:
        mirror = self.d.extras.get("github")
        if mirror is None:
            return
        try:
            await mirror.sync_main(message)
        except Exception as e:
            log.warning("GitHub knowledge sync failed: %r", e)

    async def page(self, inc: Incident, reason: str, key: str) -> None:
        """Wake a human through PagerDuty when the agent should not (or cannot) decide. One PD incident per
        Incident Judge incident (dedup_key); resolved automatically when the incident resolves."""
        pd = self.d.extras.get("pagerduty")
        if pd is None or not pd.enabled or self.store.get_kv(f"{inc.id}:paged:{key}"):
            return
        info = self.store.get_kv(f"{inc.id}:linear") or {}
        links = [{"href": "http://127.0.0.1:8700/incidents/" + inc.id, "text": "Incident Judge console"}]
        if info.get("url"):
            links.append({"href": info["url"], "text": f"Linear {info.get('identifier')}"})
        dec = self.decide(Intent(kind="pagerduty.trigger", incident_id=inc.id, payload={"scope": key}), self.pctx(inc))
        if not dec.allowed:
            return
        try:
            await pd.trigger(f"ij-{inc.id}", f"[{inc.severity or 'SEV?'}] {', '.join(inc.services)}: {reason}",
                             (inc.severity.value if inc.severity else "SEV2"), ", ".join(inc.services),
                             {"incident": inc.id, "state": inc.state.value, "reason": reason}, links)
        except Exception as e:
            log.warning("PagerDuty trigger failed: %r", e)
            return
        self.store.put_kv(f"{inc.id}:paged:{key}", now().isoformat())
        self.store.put_kv(f"{inc.id}:paged", True)
        self.store.put_kv(f"{inc.id}:pages", (self.store.get_kv(f"{inc.id}:pages") or []) +
                          [{"at": now().isoformat(), "reason": reason, "key": key}])
        await self.note(inc, f":pager: *Paged on-call via PagerDuty* — {reason}", key=f"paged:{key}")
        await self.linear_comment(inc, f"**Paged on-call via PagerDuty:** {reason}", scope=f"paged:{key}")

    async def start(self) -> None:
        await self._mirror_main("knowledge: sync on agent start")
        if getattr(self.d, "runner", None) is not None:
            self.d.runner.on_sample = self._narrate_sample
            self.d.runner.on_applied = self._narrate_applied
        if self.socket is not None:
            try:
                await self.socket.start()
            except Exception as e:  # buttons are a convenience; typed commands keep working
                log.error("Slack Socket Mode failed to start (%r); typed approvals still work", e)
        self._sync_stats()
        await self._resume_plans()
        if os.environ.get("IJ_TEST_LINT_ON_START") == "1":
            await self.memory_lint()

    async def memory_lint(self) -> None:
        """Run wiki lint; apply code-owned demotions; file findings to Linear (label memory-lint)."""
        from judge.memory import lint as lint_mod

        findings = lint_mod.apply_lint(self.d.repo, self.c, self.store, now())
        self._sync_stats()
        if not findings:
            return
        body = "\n".join(f"- {redact(str(f), 300)}" for f in findings)
        dec = self.decide(Intent(kind="linear.create_issue", incident_id=None, payload={"scope": "memory-lint"}),
                          PolicyContext(settings=self.s, config=self.c, kill_switch=self.store.kill_switch()))
        if dec.allowed:
            day = now().strftime("%Y%m%d")
            await self.outbox.run(
                app="linear", op="create_issue", incident_id=None, scope=f"memory-lint:{day}", decision=dec,
                reconcile=lambda mk: self.d.linear.find_issue_by_marker(mk),
                execute=lambda mk: self.d.linear.create_issue(
                    f"[memory-lint] {len(findings)} wiki findings",
                    f"memory-lint findings:\n{body}\n\n---\n{slack_thread_link(mk, self.s.slack_oncall_channel, 'On-call channel')}",
                    4, self._labels()),
            )

    async def run(self, max_seconds: float | None = None, once: bool = False, interval_s: float = 2.0) -> None:
        await self.start()
        started = now()
        while True:
            try:
                await asyncio.wait_for(self.tick(), timeout=max(60.0, 30 * interval_s))
            except TimeoutError:
                log.error("tick watchdog fired; task stacks follow")
                for task in asyncio.all_tasks():
                    for frame in task.get_stack(limit=12):
                        log.error("  %s %s:%s %s", task.get_name(), frame.f_code.co_filename, frame.f_lineno,
                                  frame.f_code.co_name)
            except Exception:
                log.exception("tick failed")
            if once:
                break
            if max_seconds and (now() - started).total_seconds() > max_seconds:
                break
            await asyncio.sleep(interval_s)
        for t in self.tasks.values():
            if not t.done():
                await t

    async def tick(self) -> None:
        signals = []
        signals += await self.d.sentry_poller.poll()
        signals += await self.d.slo_poller.poll()
        for sig in signals:
            self.on_signal(sig)
        for inc in self.store.incidents():
            if inc.state in (IncidentState.CLOSED,):
                continue
            try:
                await self.advance(inc)
            except Exception:
                log.exception("advance failed for %s", inc.id)
        await self.memory_flow()
        self.store.put_kv("agent_heartbeat", now().isoformat())

    # ================================================================ signals -> incidents

    def on_signal(self, sig: Signal) -> Incident | None:
        inc = self.store.open_incident_by_key(sig.fingerprint)
        if inc is None:
            # An error that stopped before an earlier incident resolved is that incident's evidence, not a new
            # symptom; attaching it to a newer incident on the same service would match the old runbook.
            if self._already_resolved(sig):
                return None
            inc = self._open_for_service(sig)
        if inc is None:
            inc = Incident(incident_key=sig.fingerprint, trial_id=self.s.trial_id, environment=sig.environment,
                           services=[sig.service])
            self.store.save_incident(inc)
            log.info("opened %s for %s/%s (%s)", inc.id, sig.service, sig.environment, sig.fingerprint)
        self.store.add_signal(sig, inc.id)
        return inc

    def _open_for_service(self, sig: Signal) -> Incident | None:
        for inc in self.store.incidents():
            if inc.state in (IncidentState.RESOLVED, IncidentState.CLOSED):
                continue
            if inc.environment == sig.environment and sig.service in inc.services:
                return inc
        return None

    def _already_resolved(self, sig: Signal) -> bool:
        for inc in reversed(self.store.incidents([IncidentState.RESOLVED, IncidentState.CLOSED])):
            if inc.environment != sig.environment or (sig.service not in inc.services
                                                      and inc.incident_key != sig.fingerprint):
                continue
            if inc.resolved_at and sig.last_seen and sig.last_seen <= inc.resolved_at:
                return True
            if self.store.get_kv(f"{inc.id}:merged_into") and sig.fingerprint == inc.incident_key:
                return True
        return False

    # ================================================================ state machine

    async def advance(self, inc: Incident) -> None:
        task = self.tasks.get(inc.id)
        if task is not None:
            if not task.done():
                await self.chat_commands(inc)  # engineers can still ask questions while a fix is verifying
                return
            self.tasks.pop(inc.id)
            if task.exception():
                log.error("remediation task for %s crashed: %r", inc.id, task.exception())
            inc = self.store.get_incident(inc.id)

        st = inc.state
        if st in (IncidentState.DETECTED, IncidentState.TRIAGING):
            settle = max(10.0, 60 * self.s.time_scale)
            if (now() - inc.created_at).total_seconds() < settle:
                return  # let metrics windows reflect the incident before judging it
            await self.triage(inc)
            return
        if st == IncidentState.RESOLVED:
            await self.close(inc)
            return
        if st in (IncidentState.EXECUTING, IncidentState.VERIFYING, IncidentState.ROLLING_BACK):
            await self._resume_incident_plan(inc)
            return
        if st in ACTIVE:
            if st in (IncidentState.OPEN, IncidentState.ESCALATED):
                await self.retriage(inc)
                inc = self.store.get_incident(inc.id)
            await self.public_flow(inc)
            inc = self.store.get_incident(inc.id)
            if inc.state == IncidentState.OPEN:
                await self.remediation_flow(inc)
                inc = self.store.get_incident(inc.id)
                if inc.state == IncidentState.OPEN:
                    await self.diagnosis_flow(inc)
            elif inc.state == IncidentState.AWAITING_FIX_APPROVAL:
                await self.awaiting_fix(inc)
            elif inc.state == IncidentState.VETO_WINDOW:
                await self.veto_window(inc)
            inc = self.store.get_incident(inc.id)
            await self.flush_cards(inc)
            if inc.id in self.tasks:
                return
            await self.chat_commands(inc)
            await self.linear_progress(inc)
            inc = self.store.get_incident(inc.id)
            if inc.state in ACTIVE:
                await self.resolution_check(inc)

    # ---------------------------------------------------------------- triage

    async def triage(self, inc: Incident) -> None:
        self.store.transition(inc, IncidentState.TRIAGING)
        ctx = await self.gather(inc)
        judge_failed = False
        try:
            proposal = await self.d.judge.triage(ctx)
        except JudgeError as e:
            log.error("judge failed for %s: %s", inc.id, e)
            judge_failed = True
            proposal = self._fallback_proposal(inc, ctx)
        proposal = self.clamp(inc, proposal)
        self.store.put_kv(f"{inc.id}:proposal", proposal.model_dump(mode="json"))
        self.store.put_kv(f"{inc.id}:judge", self.d.judge.name)

        inc.severity = proposal.severity
        inc.customer_impact = proposal.customer_impact
        inc.customer_visible = proposal.customer_visible
        inc.runbook_id = proposal.runbook_id or ctx.runbook_id
        self.store.save_incident(inc)

        if proposal.related_incident_id and await self.try_merge(inc, proposal.related_incident_id):
            return

        await self.announce(inc, proposal, ctx)
        self.store.transition(inc, IncidentState.ESCALATED if judge_failed else IncidentState.OPEN)

    async def retriage(self, inc: Incident) -> None:
        """Re-judge periodically while the incident is open. Severity/impact only ever move UP
        automatically (humans downgrade). A runbook that did not match earlier may match now."""
        interval = max(15.0, 120 * self.s.time_scale)
        last = self.store.get_kv(f"{inc.id}:retriage_at") or inc.state_entered_at.isoformat()
        if (now() - datetime.fromisoformat(last)).total_seconds() < interval:
            return
        self.store.put_kv(f"{inc.id}:retriage_at", now().isoformat())
        if self.store.active_plan(inc.id):
            return
        ctx = await self.gather(inc)
        try:
            new = self.clamp(inc, await self.d.judge.triage(ctx))
        except JudgeError:
            return
        old = TriageProposal.model_validate(self.store.get_kv(f"{inc.id}:proposal"))
        impact_order = [CustomerImpact.none, CustomerImpact.degraded, CustomerImpact.partial_outage,
                        CustomerImpact.major_outage]
        upd: dict[str, Any] = {}
        if new.severity.rank < old.severity.rank:
            upd["severity"] = new.severity
        if impact_order.index(new.customer_impact) > impact_order.index(old.customer_impact):
            upd["customer_impact"] = new.customer_impact
            upd["customer_visible"] = new.customer_visible or old.customer_visible
        if new.proposed_action and not old.proposed_action:
            upd.update(proposed_action=new.proposed_action, runbook_id=new.runbook_id,
                       runbook_match_evidence=new.runbook_match_evidence)
            if self.store.get_kv(f"{inc.id}:fix_closed") == "mismatch":
                self.store.put_kv(f"{inc.id}:fix_closed", None)
        if not upd:
            return
        merged = old.model_copy(update={**upd, "rationale_internal": new.rationale_internal})
        self.store.put_kv(f"{inc.id}:proposal", merged.model_dump(mode="json"))
        changes = []
        if "severity" in upd:
            changes.append(f"severity {old.severity.value}→{merged.severity.value}")
            inc.severity = merged.severity
        if "customer_impact" in upd:
            changes.append(f"impact {old.customer_impact.value}→{merged.customer_impact.value}")
            inc.customer_impact, inc.customer_visible = merged.customer_impact, merged.customer_visible
            if not inc.public_posted:  # new impact = new approval subject
                self.store.put_kv(f"{inc.id}:public_closed", None)
                self.store.put_kv(f"{inc.id}:public_pending", None)
        if "proposed_action" in upd:
            changes.append(f"runbook `{merged.runbook_id}` now matches")
        self.store.save_incident(inc)
        if changes:
            await self.note(inc, ":arrow_up: Re-triage: " + ", ".join(changes), key=f"retriage:{now().timestamp():.0f}")
            if "severity" in upd and inc.linear_issue_id:
                await self.linear_comment(inc, "Re-triage: " + ", ".join(changes), scope=f"retriage:{merged.severity.value}")

    def clamp(self, inc: Incident, p: TriageProposal) -> TriageProposal:
        """Code clamps LLM output toward safety only. Baseline B0 skips this."""
        if self.baseline == "B0":
            return p
        upd: dict[str, Any] = {}
        svc = self.c.service(inc.primary_service)
        if inc.environment != Env.production:
            upd["customer_visible"] = False
            if p.severity.rank < 3:
                upd["severity"] = Severity.SEV3
        if not svc or not svc.public:
            upd["customer_visible"] = False
        if p.customer_impact == CustomerImpact.none:
            upd["customer_visible"] = False
        if p.proposed_action and p.proposed_action.target_service not in inc.services:
            upd["proposed_action"] = None
        upd["rationale_internal"] = redact(p.rationale_internal, 1500)
        return p.model_copy(update=upd)

    def _fallback_proposal(self, inc: Incident, ctx: TriageContext) -> TriageProposal:
        svc = self.c.service(inc.primary_service)
        sev = Severity.SEV2 if svc and svc.tier == 1 and inc.environment == Env.production else Severity.SEV3
        return TriageProposal(severity=sev, customer_impact=CustomerImpact.none, customer_visible=False,
                              confidence=0.0, needs_human=True,
                              rationale_internal="judge unavailable: safe fallback (no public post, no fix)")

    async def gather(self, inc: Incident) -> TriageContext:
        signals = self.store.signals_for(inc.id)
        metrics = {svc: self._metrics_for(svc, inc.environment) for svc in inc.services}
        others = [o for o in self.store.incidents() if o.state not in (IncidentState.RESOLVED, IncidentState.CLOSED)]
        other_services = {s for o in others if o.id != inc.id for s in o.services} - set(inc.services)
        ctx = TriageContext(incident=inc, signals=signals, services=[self.c.service(s) for s in inc.services],
                            metrics=metrics, open_incidents=others, time_scale=self.s.time_scale,
                            catalog=self.c.catalog,
                            other_metrics={s: self._metrics_for(s, inc.environment) for s in other_services})
        if self.baseline != "B1" and inc.environment == Env.production:
            try:
                mr = await self.d.query.find(inc, signals)
            except Exception:
                log.exception("runbook query failed; continuing without memory")
                mr = None
            if mr and mr.runbook_id:
                if self.baseline == "B3":
                    mr.match_ok = True
                ctx.runbook_id, ctx.runbook_via, ctx.runbook_match_ok = mr.runbook_id, mr.via, mr.match_ok
                rb = self.d.repo.get_runbook(mr.runbook_id)
                if rb is not None:
                    ctx.runbook_markdown = rb.to_markdown()
                    fm = rb.frontmatter
                    ctx.runbook_action = fm.action.model_dump() if fm.action else None
                self.store.put_kv(f"{inc.id}:match", mr.model_dump(mode="json"))
        return ctx

    def _metric_key(self, service: str, env: Env) -> str:
        return service if env == Env.production else f"{service}@{env.value}"

    def _window(self, base: int = 60) -> int:
        return int(max(20, base * max(self.s.time_scale, 0.3)))

    def _metrics_for(self, service: str, env: Env) -> dict[str, float | None]:
        key = self._metric_key(service, env)
        out = {}
        for m in METRIC_NAMES:
            try:
                out[m] = self.d.metrics.value(m, key, self._window())
            except Exception:
                out[m] = None
        entry = self.c.service(service)
        for route in (entry.critical_routes if entry else []):
            for m in ("error_rate", "latency_p95"):
                try:
                    out[f"{m}:{route}"] = self.d.metrics.value(m, key, self._window(), route=route)
                except Exception:
                    out[f"{m}:{route}"] = None
        return out

    # ---------------------------------------------------------------- merge (P13)

    async def try_merge(self, inc: Incident, target_id: str) -> bool:
        target = self.store.get_incident(target_id)
        if target is None or target.state in (IncidentState.RESOLVED, IncidentState.CLOSED):
            return False
        dec = self.decide(Intent(kind="incident.merge", incident_id=inc.id, payload={"into": target_id}),
                          self.pctx(inc, merge_target=target))
        if not dec.allowed:
            return False
        for s in inc.services:
            if s not in target.services:
                target.services.append(s)
        self.store.save_incident(target)
        for sig in self.store.signals_for(inc.id):
            self.store.add_signal(sig, target.id)
        self.store.put_kv(f"{inc.id}:merged_into", target.id)
        inc.related_incident_id = target.id
        self.store.transition(inc, IncidentState.CLOSED)
        await self.note(target, f"Merged incident {inc.id} ({', '.join(inc.services)}) into this one: shared dependency "
                                f"in the catalog. Decision {dec.decision_id}.", key=f"merge:{inc.id}")
        if target.public_posted:
            await self.public_update(target, "identified", scope=f"merge:{inc.id}")
        return True

    # ---------------------------------------------------------------- announce (internal writes)

    async def announce(self, inc: Incident, p: TriageProposal, ctx: TriageContext) -> None:
        from judge import narration

        meta = self._sentry_meta(ctx.signals)
        sentry_sig = next((s for s in reversed(ctx.signals) if s.source == "sentry"), None)
        if sentry_sig is not None:
            what = (meta.get(sentry_sig.external_id or "", {}).get("title")
                    or f"{sentry_sig.error_type} at {sentry_sig.culprit}")
        else:
            slo = next((s for s in ctx.signals if s.source == "slo"), None)
            what = f"SLO {slo.slo_name} burning" if slo else "anomaly"
        title = redact(f"[{p.severity.value}] {inc.primary_service} — {what} ({inc.environment.value})", 200)
        match = self.store.get_kv(f"{inc.id}:match") or {}
        evidence = narration.evidence_lines(ctx.signals, ctx.metrics, meta)
        proposal_line = None
        if p.proposed_action:
            pa = p.proposed_action
            proposal_line = (f"Known fix proposed: `{pa.name}` "
                             f"({', '.join(f'{k}={v}' for k, v in pa.params.items())}) on `{pa.target_service}`. "
                             "It runs only after approval (or the runbook's earned autonomy), is verified on live SLOs "
                             "and rolled back automatically if they don't recover.")
        summary = (f"{inc.primary_service} in {inc.environment.value}: {what}. "
                   f"Rated {p.severity.value} ({p.customer_impact.value.replace('_', ' ')}"
                   f"{', customer-visible' if p.customer_visible else ''}).")
        description = narration.linear_description(
            summary=summary, severity=p.severity.value, impact=p.customer_impact.value, visible=p.customer_visible,
            services=inc.services, env=inc.environment.value, evidence=evidence, metrics=ctx.metrics,
            rationale=redact(p.rationale_internal, 1500), match=match, proposal_line=proposal_line,
            slack_hint="Live discussion and approvals: the incident thread in the on-call Slack channel.",
            judge=self.d.judge.name)
        if self.baseline == "B0":
            description = f"{summary}\n\n{p.rationale_internal}"

        # Linear issue
        dec = self.decide(Intent(kind="linear.create_issue", incident_id=inc.id), self.pctx(inc))
        if dec.allowed:
            res = await self.outbox.run(
                app="linear", op="create_issue", incident_id=inc.id, scope="", decision=dec,
                reconcile=lambda mk: self.d.linear.find_issue_by_marker(mk),
                execute=lambda mk: self.d.linear.create_issue(
                    title, f"{description}\n\n---\n{slack_thread_link(mk, self.s.slack_oncall_channel, 'Discussion & approvals in Slack')}",
                    LINEAR_PRIORITY[p.severity], self._labels()),
            )
            inc.linear_issue_id = res.ref
            self.store.save_incident(inc)
            try:
                issue = await self.d.linear.get_issue(res.ref)
                self.store.put_kv(f"{inc.id}:linear", {"identifier": issue.get("identifier"), "url": issue.get("url")})
            except Exception:
                pass

        # Slack triage thread in #oncall
        visible = ("customers can see it" if p.customer_visible else "not visible to customers")
        text = (f":rotating_light: *{title}*\n"
                f"*Status:* triaged — {p.severity.value}, {p.customer_impact.value.replace('_', ' ')}, {visible} "
                f"(judge: {self.d.judge.name})\n"
                f"_Everything about this incident happens in this thread. {STATUS_FLOW}_\n"
                + self._linear_link(inc))
        seen = evidence
        if seen:
            text += "*What I see*\n" + "\n".join(f"• {line}" for line in seen) + "\n"
        if match.get("runbook_id"):
            text += "*Known fix?*\n" + "\n".join(narration.match_lines(match)) + "\n"
        text += f"*Why this severity (judge)*: {redact(p.rationale_internal, 400) or '-'}\n"
        dec = self.decide(Intent(kind="slack.post", incident_id=inc.id, payload={"scope": "triage"}), self.pctx(inc))
        if dec.allowed:
            res = await self.outbox.run(
                app="slack", op="post", incident_id=inc.id, scope="triage", decision=dec,
                reconcile=lambda mk: self.d.slack.find_message_by_marker(self.s.slack_oncall_channel, mk),
                execute=lambda mk: self.d.slack.post(self.s.slack_oncall_channel, text, ref=mk),
            )
            inc.slack_thread_ts = res.ref
            inc.slack_channel_id = inc.slack_channel_id
            self.store.save_incident(inc)

        # Optional dedicated war room for SEV1/SEV2 in production (off by default: the thread is the war room)
        if self.c.policy.war_room_channel and inc.environment == Env.production and p.severity.rank <= 2:
            name = f"inc-{inc.created_at:%Y%m%d}-{inc.id.split('_')[-1][:6]}"
            if self.s.trial_id:
                name += f"-{re.sub(r'[^a-z0-9]', '', self.s.trial_id.lower())[:10]}"
            dec = self.decide(Intent(kind="slack.create_channel", incident_id=inc.id, payload={"name": name}),
                              self.pctx(inc))
            if dec.allowed:
                res = await self.outbox.run(
                    app="slack", op="create_channel", incident_id=inc.id, scope=name, decision=dec,
                    reconcile=lambda mk: self.d.slack.find_channel(name),
                    execute=lambda mk: self.d.slack.create_channel(name),
                )
                inc.slack_channel_id = res.ref
                self.store.save_incident(inc)
                await self._invite_oncall(res.ref)
                await self.note(inc, f"War room for {title}. Main thread is in #oncall.", key="warroom-hello",
                                channel=res.ref, thread=False)

    async def _person(self, user_id: str) -> str:
        """Display name for a Slack user id (cached); falls back to the id."""
        cached = self.store.get_kv(f"slack_user:{user_id}")
        if cached:
            return cached
        name = user_id
        try:
            body = await self.d.slack._call("user_info", "users.info", user=user_id)
            u = body.get("user") or {}
            name = (u.get("profile") or {}).get("real_name") or u.get("real_name") or u.get("name") or user_id
        except Exception:
            pass
        self.store.put_kv(f"slack_user:{user_id}", name)
        return name

    def _linear_link(self, inc: Incident) -> str:
        info = self.store.get_kv(f"{inc.id}:linear") or {}
        if info.get("url"):
            return f"Ticket: <{info['url']}|Linear {info.get('identifier')}>\n"
        return f"Ticket: Linear `{inc.linear_issue_id}`\n" if inc.linear_issue_id else ""

    async def _already_in_effect(self, plan: Plan) -> str | None:
        try:
            cfg = await self.d.control.get_config(plan.target_service)
        except Exception:
            return None
        p = plan.params
        if plan.action == "toggle_flag" and "flag" in p and (cfg.get("flags") or {}).get(p["flag"]) == p.get("value"):
            return f"`{plan.target_service}` flag `{p['flag']}` is already `{str(p.get('value')).lower()}`"
        if plan.action == "scale_pool" and p.get("size") is not None and cfg.get("pool_size") == p["size"]:
            return f"`{plan.target_service}` pool size is already {p['size']}"
        if plan.action == "rollback_deploy" and p.get("to_version") and cfg.get("app_version") == p["to_version"]:
            return f"`{plan.target_service}` already runs {p['to_version']}"
        return None

    def _sentry_meta(self, signals: list[Signal]) -> dict[str, dict]:
        return {s.external_id: self.store.get_kv(f"sentry_issue_meta:{s.external_id}") or {}
                for s in signals if s.source == "sentry" and s.external_id}

    async def _narrate_applied(self, plan, prev_state: dict, result: dict) -> None:
        from judge import narration

        inc = self.store.get_incident(plan.incident_id)
        before = {"flags": {prev_state.get("flag"): prev_state.get("value")}} if "flag" in prev_state else \
            {"pool_size": prev_state.get("pool_size", prev_state.get("prev")),
             "app_version": prev_state.get("app_version", prev_state.get("prev"))}
        change, rollback = narration.change_preview(plan, before)
        self.store.put_kv(f"{inc.id}:changes", (self.store.get_kv(f"{inc.id}:changes") or []) +
                          [{"at": now().isoformat(), "change": change, "rollback": rollback}])
        before_values = {}
        for cond in plan.verify.conditions:
            try:
                before_values[cond.metric] = self.d.metrics.value(cond.metric, plan.target_service, cond.window_s)
            except Exception:
                before_values[cond.metric] = None
        self.store.put_kv(f"{plan.plan_id}:before", {"at": now().isoformat(), "values": before_values})
        await self.linear_comment(inc, f"**Change applied** at {narration.hhmmss(now())} UTC: {change.replace('*', '**')}\n\n"
                                       f"System response: `{redact(str(result), 200)}`\n\nRollback if needed: {rollback}. "
                                       f"Verification against live SLOs started.", scope=f"applied:{plan.plan_id}")
        await self.note(inc, f":white_check_mark: *Change applied* at {narration.hhmmss(now())} UTC: {change}\n"
                             f"System answered: `{redact(str(result), 200)}`\n"
                             f"*Rollback if needed:* {rollback}. *Next:* verifying on live metrics.",
                        key=f"applied:{plan.plan_id}")

    async def _narrate_sample(self, plan, samples: list[dict], n: int, interval: float) -> None:
        from judge import narration

        inc = self.store.get_incident(plan.incident_id)
        before = self.store.get_kv(f"{plan.plan_id}:before") or {}
        text = narration.progress_text(plan, samples, n, interval, before=before.get("values"),
                                       applied_at=before.get("at"))
        key = f"{inc.id}:progress:{plan.plan_id}"
        ts = self.store.get_kv(key)
        if ts is None:
            ts = await self.note(inc, text, key=f"progress:{plan.plan_id}")
            self.store.put_kv(key, ts)
        else:
            try:
                await self.d.slack.update_message(self.s.slack_oncall_channel, ts, text)
            except Exception as e:
                log.debug("progress update failed: %r", e)

    async def _finish_progress(self, plan, result) -> None:
        from judge import narration

        inc = self.store.get_incident(plan.incident_id)
        ts = self.store.get_kv(f"{inc.id}:progress:{plan.plan_id}")
        if not ts or not result.samples:
            return
        _, interval, n = __import__("judge.remediation.verifier", fromlist=["verify_window"]).verify_window(
            plan, self.s.time_scale)
        done = (":white_check_mark: *Verification PASSED*" if result.verify == VerifyResult.pass_ else
                f":x: *Verification {str(result.verify.value).upper()}* — rolled back" if result.rolled_back else
                f":x: *Verification {str(result.verify.value).upper()}*")
        before = self.store.get_kv(f"{plan.plan_id}:before") or {}
        final = narration.progress_text(plan, result.samples, n, interval, done=done, before=before.get("values"),
                                        applied_at=before.get("at"))
        try:
            await self.d.slack.update_message(self.s.slack_oncall_channel, ts, final)
        except Exception as e:
            log.debug("final progress update failed: %r", e)
        await self.linear_comment(inc, final.replace(":white_check_mark:", "✅").replace(":x:", "❌")
                                  .replace("*", "**"), scope=f"verified:{plan.plan_id}")

    async def post_report(self, inc: Incident) -> None:
        """Timeline + what changed + links, in the thread and on the Linear issue."""
        from judge import narration

        tl: list[tuple[datetime, str]] = []
        sigs = self.store.signals_for(inc.id)
        firsts = [s.first_seen for s in sigs if s.first_seen]
        if firsts:
            tl.append((min(min(firsts), inc.created_at), "First error / SLO breach detected"))
        tl.append((inc.created_at, "Incident opened"))
        for (k, v) in self.store._exec("SELECT k, v FROM kv WHERE k LIKE ?", (f"transition:{inc.id}:%",)):
            ts = datetime.fromtimestamp(float(k.rsplit(":", 1)[-1]), tz=inc.created_at.tzinfo)
            to = str(v).strip('"').split("->")[-1]
            names = {"OPEN": f"Triaged: {inc.severity}, {inc.customer_impact}", "AWAITING_FIX_APPROVAL":
                     "Waiting for approval", "EXECUTING": "Fix started", "MONITORING": "Verification passed",
                     "ESCALATED": "Escalated to a human", "RESOLVED": "Resolved"}
            if to in names:
                tl.append((ts, names[to]))
        what = {"public_post": "Status page post", "fix": "Fix", "memory_merge": "Runbook update"}
        for a in self.store.approvals(inc.id):
            if a.valid:
                tl.append((a.ts, f"{what.get(a.kind, a.kind)} {a.verdict}d by {await self._person(a.user_id)}"))
        for d in self.store.decisions(inc.id):
            if d.intent == "instatus.create_incident" and d.allowed:
                tl.append((d.ts, "Status page incident posted"))
        for page in self.store.get_kv(f"{inc.id}:pages") or []:
            tl.append((datetime.fromisoformat(page["at"]), f"Paged on-call via PagerDuty: {page['reason']}"))
        if self.store.get_kv(f"{inc.id}:paged_resolved"):
            tl.append((datetime.fromisoformat(self.store.get_kv(f"{inc.id}:paged_resolved")), "PagerDuty incident resolved"))
        for e in self.store.executions():
            if e["incident_id"] == inc.id:
                verb = {"apply": "Change applied", "rollback": "Change rolled back"}.get(e["kind"], e["kind"])
                tl.append((datetime.fromisoformat(e["ts"]), f"{verb}: `{e['action']}` on {e['service']}"))
        changes = [f"{c['change']} (rollback: {c['rollback']})" for c in (self.store.get_kv(f"{inc.id}:changes") or [])]
        links = []
        for meta in self._sentry_meta(sigs).values():
            if meta.get("permalink"):
                links.append(f"<{meta['permalink']}|Sentry {meta.get('shortId') or ''}>")
        if inc.linear_issue_id:
            try:
                issue = await self.d.linear.get_issue(inc.linear_issue_id)
                links.append(f"<{issue.get('url')}|Linear {issue.get('identifier')}>")
            except Exception:
                pass
        if inc.instatus_incident_id:
            page_url = os.environ.get("INSTATUS_PAGE_URL") if self.s.backend == "real" else None
            links.append(f"<{page_url}|Status page>" if page_url else f"Status page incident `{inc.instatus_incident_id}`")
        if self.store.get_kv(f"{inc.id}:sentry_resolved"):
            links.append(f"{len(self.store.get_kv(f'{inc.id}:sentry_resolved'))} Sentry issue(s) marked resolved")
        if self.store.get_kv(f"{inc.id}:pages"):
            links.append("PagerDuty: paged, " + ("resolved" if self.store.get_kv(f"{inc.id}:paged_resolved") else "still open"))
        memory_line = None
        if inc.runbook_id:
            rb = self.d.repo.get_runbook(inc.runbook_id)
            if rb is not None:
                st = rb.frontmatter.stats
                memory_line = (f"runbook `{rb.frontmatter.id}` now has {st.success} verified successes and "
                               f"{st.failure} failures (autonomy {rb.frontmatter.autonomy.level.value})")
        report = narration.incident_report(inc, tl, changes, links, memory_line)
        await self.note(inc, report, key="report")
        await self.linear_comment(inc, report.replace("*", "**"), scope="report")

    async def _invite_oncall(self, channel: str) -> None:
        if self.s.backend != "real":
            return
        try:
            await self.d.slack.invite(channel, self.c.policy.oncall_allowlist)
        except Exception as e:  # inviting is a convenience; never block the incident on it
            log.warning("could not invite on-call to %s: %r", channel, e)

    def _labels(self) -> list[str]:
        return [self.s.linear_eval_label_id] if self.s.trial_id and self.s.linear_eval_label_id else []

    # ---------------------------------------------------------------- public status page

    async def public_flow(self, inc: Incident) -> None:
        if inc.public_posted or self.store.get_kv(f"{inc.id}:public_closed"):
            return
        if not inc.customer_impact:
            return
        impact = inc.customer_impact.value
        subject = public_subject_hash(inc, impact)
        pending = self.store.get_kv(f"{inc.id}:public_pending")
        approval = None
        if pending and pending.get("ts"):
            await self.d.approval_poller.poll(inc, "public_post", subject, pending["ts"], channel=self.s.slack_oncall_channel)
            approval = self._latest_approval(inc, "public_post", subject)

        intent = Intent(kind="instatus.create_incident", incident_id=inc.id,
                        payload={"phase": "investigating", "impact": impact})
        dec = self.decide(intent, self.pctx(inc, approval=approval))

        if dec.result == DecisionResult.ALLOW:
            await self._create_public(inc, dec)
        elif dec.result == DecisionResult.REQUIRE_APPROVAL:
            if not pending or pending.get("subject") != subject:
                # the card itself is posted by flush_cards(), bundled with any fix request from the same tick
                self.store.put_kv(f"{inc.id}:public_pending", {"subject": subject, "ts": None,
                                                               "at": now().isoformat()})
            else:
                ttl = self.s.human_window(self.c.policy.public_approval_ttl_s)
                if (now() - datetime.fromisoformat(pending["at"])).total_seconds() > max(ttl, 20):
                    timeout = Decision(incident_id=inc.id, trial_id=self.s.trial_id, intent=intent.kind,
                                       result=DecisionResult.DENY, rules=["P6"],
                                       explain="approval timed out; safe default is not posting publicly")
                    self.store.add_decision(timeout)
                    self.store.put_kv(f"{inc.id}:public_closed", True)
                    await self.page(inc, "nobody approved the public status page post in time", "public-timeout")
                    await self.note(inc, "Approval timed out: NOT posting to the status page (safe default).",
                                    key="public-timeout")
        else:
            if not (set(dec.rules) & RETRYABLE_DENY):
                self.store.put_kv(f"{inc.id}:public_closed", True)
            if "P6" in dec.rules and "rejected" in dec.explain:
                await self.note(inc, f"Public post rejected. ({dec.decision_id})", key="public-rejected")
            elif dec.rules and dec.rules != ["P6"]:
                await self.note(inc, f":no_entry: Public status page post blocked — {', '.join(dec.rules)}: "
                                     f"{dec.explain}\nDecision {dec.decision_id}.", key=f"public-deny:{dec.rules}")

    async def _create_public(self, inc: Incident, dec: Decision) -> None:
        services = [self.c.service(s) for s in inc.services]
        impact = inc.customer_impact
        raw = self._raw_message(inc) if self.baseline == "B0" else None

        async def reconcile(mk: str) -> str | None:
            key, _, trial = parse_marker(mk)
            return await self.d.instatus.find_incident_by_ref(templates.public_ref(key, trial))

        async def execute(mk: str) -> str:
            key, _, trial = parse_marker(mk)
            ref = templates.public_ref(key, trial)
            if raw is not None:  # B0 naive baseline: free text straight from the alert
                name, message = f"Incident: {raw[:80]}", f"{raw} Ref {ref}"
            else:
                name = templates.public_title(impact, services)
                message = templates.public_message("investigating", services, ref)
            return await self.d.instatus.create_incident(
                name, message, [s.instatus_component_id for s in services],
                templates.INSTATUS_COMPONENT_STATUS[impact], status="INVESTIGATING")

        res = await self.outbox.run(app="instatus", op="create_incident", incident_id=inc.id, scope="", decision=dec,
                                    reconcile=reconcile, execute=execute)
        inc.instatus_incident_id = res.ref
        inc.public_posted = True
        self.store.save_incident(inc)
        self.store.put_kv(f"{inc.id}:public_pending", None)
        await self.note(inc, f":mega: *Status page updated:* posted “{templates.public_title(impact, [self.c.service(s) for s in inc.services])}” "
                             f"({impact.value.replace('_', ' ')}).", key="public-posted")
        await self.linear_comment(inc, f"**Public status page incident posted** ({impact.value.replace('_', ' ')}) "
                                       f"after on-call approval.", scope="public-posted")

    async def public_update(self, inc: Incident, phase: str, scope: str = "", ctx: PolicyContext | None = None) -> None:
        if not inc.public_posted or not inc.instatus_incident_id:
            return
        kind = "instatus.resolve" if phase == "resolved" else "instatus.update"
        dec = self.decide(Intent(kind=kind, incident_id=inc.id, payload={"phase": phase}), ctx or self.pctx(inc))
        if not dec.allowed:
            return
        services = [self.c.service(s) for s in inc.services]
        impact = CustomerImpact.none if phase == "resolved" else inc.customer_impact

        async def reconcile(mk: str) -> str | None:
            key, _, trial = parse_marker(mk)
            return await self.d.instatus.find_incident_by_ref(templates.public_ref(key, trial))

        async def execute(mk: str) -> str:
            key, _, trial = parse_marker(mk)
            msg = templates.public_message(phase, services, templates.public_ref(key, trial))
            if phase != "resolved":  # e.g. a merge added a service: the incident's component list must grow too
                await self.d.instatus.set_components(inc.instatus_incident_id,
                                                     [s.instatus_component_id for s in services],
                                                     templates.INSTATUS_COMPONENT_STATUS[impact])
            await self.d.instatus.add_update(inc.instatus_incident_id, msg, templates.INSTATUS_INCIDENT_STATUS[phase],
                                             [s.instatus_component_id for s in services],
                                             templates.INSTATUS_COMPONENT_STATUS[impact])
            return inc.instatus_incident_id

        try:
            await self.outbox.run(app="instatus", op=f"update_{phase}", incident_id=inc.id, scope=scope, decision=dec,
                                  reconcile=reconcile, execute=execute)
        except Exception as e:  # the step stays in_flight in the outbox; report instead of derailing the incident
            log.warning("status page update (%s) failed for %s: %r", phase, inc.id, e)
            await self.note(inc, f":warning: Status page update `{phase}` failed ({type(e).__name__}); "
                                 f"will not block the incident. On-call may update it manually.",
                            key=f"public-update-failed:{phase}:{scope}")

    def _raw_message(self, inc: Incident) -> str:
        for sig in reversed(self.store.signals_for(inc.id)):
            ev = self.store.get_kv(f"sentry_event:{(sig.signal_id or '').removeprefix('sentry:')}")
            if ev:
                return str(ev.get("message") or ev.get("title") or sig.error_type)
        return "service incident"

    # ---------------------------------------------------------------- remediation

    async def remediation_flow(self, inc: Incident) -> None:
        if self.store.get_kv(f"{inc.id}:fix_closed") or self.store.active_plan(inc.id):
            return
        pdata = self.store.get_kv(f"{inc.id}:proposal")
        if not pdata:
            return
        proposal = TriageProposal.model_validate(pdata)
        match = self.store.get_kv(f"{inc.id}:match") or {}
        if not proposal.proposed_action and match.get("runbook_id") and match.get("match_ok") is True:
            # The runbook matched on live metrics but the judge proposed no action: code proposes the runbook's own
            # catalog action. The policy engine checks it exactly as if the LLM had proposed it.
            rb = self.d.repo.get_runbook(match["runbook_id"])
            fm = rb.frontmatter if rb is not None else None
            if fm and fm.action and fm.action.name in self.c.actions:
                from judge.core.models import ActionProposal

                proposal = proposal.model_copy(update={
                    "runbook_id": rb.id,
                    "proposed_action": ActionProposal(name=fm.action.name, params=dict(fm.action.params),
                                                      target_service=inc.primary_service)})
                self.store.put_kv(f"{inc.id}:proposal", proposal.model_dump(mode="json"))
        if not proposal.proposed_action:
            if match.get("runbook_id") and match.get("match_ok") is not True:
                rb = self.d.repo.get_runbook(match["runbook_id"])
                fm = rb.frontmatter if rb is not None else None
                if fm and fm.action and fm.action.name in self.c.actions:
                    from judge.core.models import ActionProposal
                    from judge.remediation.planner import build_plan

                    ghost = build_plan(inc, ActionProposal(name=fm.action.name, params=fm.action.params,
                                                           target_service=inc.primary_service), rb, self.c,
                                       AutonomyLevel.L0)
                    self.decide(Intent(kind="remediation.execute", incident_id=inc.id,
                                       payload={"plan": ghost.plan_hash, "scope": "lookalike"}),
                                self.pctx_plan(inc, ghost, rb, match))
                await self.note(inc, f"Runbook `{match['runbook_id']}` looks similar but its machine-checked conditions do NOT hold "
                                     f"({self._fmt_conditions(match)}). Not reusing the old fix; needs a human to investigate.",
                                key=f"runbook-mismatch:{match.get('runbook_id')}")
                self.store.put_kv(f"{inc.id}:fix_closed", "mismatch")
            return

        from judge.remediation import autonomy as autonomy_mod
        from judge.remediation.planner import build_plan

        # The machine match is authoritative for WHICH runbook applies; the LLM may only propose that runbook's action
        # (P7 still checks name + params). LLMs sometimes propose the right action but omit runbook_id.
        runbook_id = proposal.runbook_id
        if match.get("runbook_id") and match.get("match_ok") is True and runbook_id in (None, match["runbook_id"]):
            runbook_id = match["runbook_id"]
        if runbook_id != proposal.runbook_id:
            proposal = proposal.model_copy(update={"runbook_id": runbook_id})
            self.store.put_kv(f"{inc.id}:proposal", proposal.model_dump(mode="json"))
        runbook = self.d.repo.get_runbook(runbook_id) if runbook_id else None
        spec = self.c.actions.get(proposal.proposed_action.name)
        svc = proposal.proposed_action.target_service
        breaker = autonomy_mod.consecutive_failures(self.store, svc) >= self.c.policy.circuit_breaker_failures
        level = autonomy_mod.effective_level(runbook, spec, self.store.kill_switch(), breaker) \
            if runbook is not None and spec is not None else AutonomyLevel.L0
        if runbook is None or spec is None:
            # Let policy explain the denial (P7) with a minimal plan-less context.
            dec = self.decide(Intent(kind="remediation.execute", incident_id=inc.id,
                                     payload={"action": proposal.proposed_action.model_dump()}), self.pctx(inc))
            self.store.put_kv(f"{inc.id}:fix_closed", True)
            await self.note(inc, f"Not executing the proposed fix — {dec.rules}: {dec.explain}", key="fix-deny-noplan")
            return

        plan = build_plan(inc, proposal.proposed_action, runbook, self.c, level)
        noop = await self._already_in_effect(plan)
        if noop:
            self.store.put_kv(f"{inc.id}:match", {**match, "match_ok": "already_in_effect"})
            self.store.put_kv(f"{inc.id}:fix_closed", "noop")
            await self.note(inc, f"Runbook `{runbook.id}` matched, but its fix is already in effect ({noop}), so it cannot be "
                                 "what fixes this incident. Diagnosing from the service docs, change log and metrics instead.",
                            key=f"fix-noop:{plan.plan_hash[:8]}")
            return
        dec = self.decide(Intent(kind="remediation.execute", incident_id=inc.id, payload={"plan": plan.plan_hash}),
                          self.pctx_plan(inc, plan, runbook, match))
        if dec.result == DecisionResult.DENY:
            if not (set(dec.rules) & RETRYABLE_DENY):
                self.store.put_kv(f"{inc.id}:fix_closed", True)
            await self.note(inc, f":no_entry: Not auto-fixing `{plan.action}` — {', '.join(dec.rules)}: {dec.explain}\n"
                                 f"Decision {dec.decision_id}.", key=f"fix-deny:{dec.rules}")
            return
        if dec.result == DecisionResult.REQUIRE_APPROVAL:
            veto = dec.approval_kind == "veto"
            self.d.store.save_plan(plan, "veto_window" if veto else "awaiting_approval")
            self.store.put_kv(f"{inc.id}:fix_pending", {"plan_id": plan.plan_id, "ts": None, "at": now().isoformat(),
                                                        "kind": "veto" if veto else "fix", "level": level.value})
            self.store.transition(inc, IncidentState.VETO_WINDOW if veto else IncidentState.AWAITING_FIX_APPROVAL)
            if not veto:
                await self.flush_cards(inc)
                if os.environ.get("IJ_TEST_MUTATE_PLAN_AFTER_REQUEST") == "1":
                    await self._test_mutate_plan(inc, plan, runbook, level)
            return
        self.d.store.save_plan(plan, "approved")
        await self.start_execution(inc, plan, dec, None)

    def _fmt_conditions(self, match: dict) -> str:
        parts = []
        for c in match.get("condition_results", [])[:4]:
            if c.get("check") != "metric":
                continue
            obs = c.get("observed")
            obs = f"{obs:.3f}" if isinstance(obs, (int, float)) else obs
            parts.append(f"{c.get('metric')} {c.get('op')} {c.get('value')} → observed {obs} "
                         f"({'ok' if c.get('ok') else 'no'})")
        return "; ".join(parts) or "no data"

    async def _test_mutate_plan(self, inc: Incident, plan, runbook, level) -> None:
        """Eval hook (R4): the plan changes after the human saw the first card."""

        await asyncio.sleep(max(3.0, 20 * self.s.time_scale))
        numeric = {k: v for k, v in plan.params.items() if isinstance(v, int) and not isinstance(v, bool)}
        if not numeric:
            return
        k = next(iter(numeric))
        plan.params = {**plan.params, k: plan.params[k] + 5}
        self.store.save_plan(plan, "awaiting_approval")
        pending = self.store.get_kv(f"{inc.id}:fix_pending") or {}
        self.store.put_kv(f"{inc.id}:fix_pending", {**pending, "ts": None, "updated": True})
        await self.flush_cards(inc)

    async def awaiting_fix(self, inc: Incident) -> None:
        pending = self.store.get_kv(f"{inc.id}:fix_pending")
        active = self.store.active_plan(inc.id)
        if not pending or not active:
            self.store.transition(inc, IncidentState.OPEN)
            return
        plan, _ = active
        if pending.get("ts"):
            await self.d.approval_poller.poll(inc, "fix", plan.plan_hash, pending["ts"], channel=self.s.slack_oncall_channel)
        runbook = self.d.repo.get_runbook(plan.runbook_id)
        match = self.store.get_kv(f"{inc.id}:match") or {}
        seen = set(self.store.get_kv(f"{inc.id}:approvals_seen", []))
        other_cards = self._other_card_hashes(inc, exclude=plan.plan_hash)
        for a in self.store.approvals(inc.id, "fix"):
            if a.approval_id in seen:
                continue
            seen.add(a.approval_id)
            self.store.put_kv(f"{inc.id}:approvals_seen", sorted(seen))
            if not a.valid and a.subject_hash[:8] in other_cards:
                continue  # a reply to a different card in the same thread (e.g. the public-post approval)
            dec = self.decide(Intent(kind="remediation.execute", incident_id=inc.id, payload={"plan": plan.plan_hash}),
                              self.pctx_plan(inc, plan, runbook, match, approval=a))
            if dec.allowed:
                self.store.put_kv(f"{inc.id}:fix_pending", None)
                await self.start_execution(inc, plan, dec, a.approval_id)
                return
            if dec.result == DecisionResult.DENY:
                if a.valid and a.verdict == "reject":
                    self.store.save_plan(plan, "cancelled")
                    self.store.put_kv(f"{inc.id}:fix_pending", None)
                    if plan.source == "human" and self.store.get_kv(f"{inc.id}:superseded_plan"):
                        self.store.put_kv(f"{inc.id}:superseded_plan", None)
                        self.store.put_kv(f"{inc.id}:fix_closed", None)
                        await self.note(inc, f"{await self._person(a.user_id)} didn't confirm the alternative plan. "
                                             "Offering the agent's original plan again.", key=f"alt-rejected:{a.approval_id}")
                    else:
                        self.store.put_kv(f"{inc.id}:fix_closed", True)
                        await self.note(inc, f"Fix rejected by {await self._person(a.user_id)}. Not executing — the "
                                             "incident stays open. Reply here if you want me to try something else.",
                                        key=f"fix-rejected:{a.approval_id}")
                    self.store.transition(inc, IncidentState.OPEN)
                    return
                await self.note(inc, f":warning: Ignoring approval from <@{a.user_id}> — {', '.join(dec.rules)}: "
                                     f"{dec.explain}", key=f"fix-invalid:{a.approval_id}")
        ttl = self.s.human_window(self.c.policy.fix_approval_ttl_s)
        if (now() - datetime.fromisoformat(pending["at"])).total_seconds() > max(ttl, 30):
            self.store.save_plan(plan, "cancelled")
            self.store.put_kv(f"{inc.id}:fix_closed", True)
            self.store.put_kv(f"{inc.id}:fix_pending", None)
            self.store.add_decision(Decision(incident_id=inc.id, trial_id=self.s.trial_id, intent="remediation.execute",
                                             result=DecisionResult.DENY, rules=["P8"],
                                             explain="approval timed out; safe default is not executing"))
            await self.note(inc, "Fix approval timed out: NOT executing.", key="fix-timeout")
            await self.page(inc, "nobody approved the proposed fix in time; the incident is still open", "fix-timeout")
            self.store.transition(inc, IncidentState.ESCALATED)

    async def veto_window(self, inc: Incident) -> None:
        pending = self.store.get_kv(f"{inc.id}:fix_pending")
        active = self.store.active_plan(inc.id)
        if not pending or not active:
            self.store.transition(inc, IncidentState.OPEN)
            return
        plan, _ = active
        if pending.get("ts"):
            await self.d.approval_poller.poll(inc, "fix", plan.plan_hash, pending["ts"], channel=self.s.slack_oncall_channel)
        approvals = [a for a in self.store.approvals(inc.id, "fix") if a.valid and a.subject_hash == plan.plan_hash]
        vetoed = any(a.verdict == "reject" for a in approvals)
        early = next((a for a in approvals if a.verdict == "approve"), None)
        elapsed = (now() - inc.state_entered_at).total_seconds() >= self.s.human_window(self.c.policy.veto_window_s)
        runbook = self.d.repo.get_runbook(plan.runbook_id)
        match = self.store.get_kv(f"{inc.id}:match") or {}
        if not (vetoed or early or elapsed):
            return
        dec = self.decide(Intent(kind="remediation.execute", incident_id=inc.id, payload={"plan": plan.plan_hash}),
                          self.pctx_plan(inc, plan, runbook, match, approval=early, vetoed=vetoed,
                                         veto_window_elapsed=elapsed))
        if dec.allowed:
            self.store.put_kv(f"{inc.id}:fix_pending", None)
            await self.start_execution(inc, plan, dec, early.approval_id if early else None)
        elif dec.result == DecisionResult.DENY:
            self.store.save_plan(plan, "cancelled")
            self.store.put_kv(f"{inc.id}:fix_closed", True)
            self.store.put_kv(f"{inc.id}:fix_pending", None)
            await self.note(inc, f"Not executing (L2) — {', '.join(dec.rules)}: {dec.explain}", key="veto-deny")
            self.store.transition(inc, IncidentState.OPEN)

    def _other_card_hashes(self, inc: Incident, exclude: str) -> set[str]:
        hashes = set()
        pending = self.store.get_kv(f"{inc.id}:public_pending")
        if pending and pending.get("subject"):
            hashes.add(pending["subject"][:8])
        if inc.customer_impact:
            hashes.add(public_subject_hash(inc, inc.customer_impact.value)[:8])
        for row in self.store._exec("SELECT plan_hash FROM plans WHERE incident_id=?", (inc.id,)):
            hashes.add(row[0][:8])
        hashes.discard(exclude[:8])
        return hashes

    async def start_execution(self, inc: Incident, plan, dec: Decision, approval_id: str | None) -> None:
        self.store.transition(inc, IncidentState.EXECUTING)
        who = next((a for a in self.store.approvals(inc.id) if a.approval_id == approval_id), None)
        name = await self._person(who.user_id) if who else None
        how = {"button": "clicked Approve in Slack", "text": "replied approve in Slack"}.get(who.via, who.via) if who else ""
        await self.linear_comment(
            inc, (f"**Fix approved** by {name} ({how}) at {who.ts:%H:%M:%S} UTC — "
                  if who else f"**Fix started** automatically (runbook autonomy {plan.autonomy_level.value}) — ")
            + f"running `{plan.action}` on `{plan.target_service}`.", scope=f"approved:{plan.plan_id}")
        await self.note(inc, f":wrench: *Status: fixing.* Running `{plan.action}` on {plan.target_service}.\n"
                             f"*Next:* verifying against SLOs; if they don't recover it is rolled back automatically.",
                        key=f"exec:{plan.plan_id}")
        # Start the fix first: a status-page hiccup must never block remediation.
        self.tasks[inc.id] = asyncio.create_task(self._run_plan(inc.id, plan, dec, approval_id))
        await self.public_update(inc, "identified", scope=plan.plan_id)

    async def _run_plan(self, incident_id: str, plan, dec: Decision, approval_id: str | None, resume_status=None):
        if self.baseline == "B2":
            self.d.runner.skip_verify = True
        if resume_status:
            result = await self.d.runner.resume(plan, resume_status)
        else:
            result = await self.d.runner.execute(plan, dec, approval_id)
        await self._after_run(incident_id, plan, result)

    async def _after_run(self, incident_id: str, plan, result) -> None:
        inc = self.store.get_incident(incident_id)
        self._sync_stats()
        await self._finish_progress(plan, result)
        if result.verify == VerifyResult.pass_:
            self.store.transition(inc, IncidentState.MONITORING)
            await self.note(inc, f":white_check_mark: *Status: monitoring.* Verification PASSED for `{plan.action}` — SLOs recovered.\n"
                                 f"*Next:* resolves automatically once no errors arrive for the quiet window.",
                            key=f"verify-pass:{plan.plan_id}")
            pass  # the verification table was already added to the ticket by _finish_progress
            await self.public_update(inc, "monitoring", scope=plan.plan_id)
            if self.baseline == "B2":
                await self.resolve(inc, forced=True)
        elif not result.applied:
            self.store.put_kv(f"{inc.id}:fix_closed", True)
            self.store.transition(inc, IncidentState.ESCALATED)
            await self.note(inc, f":warning: `{plan.action}` was not applied: {result.error or 'plan status ' + str(result.status)}. "
                                 f"On-call needs to take over.",
                            key=f"not-applied:{plan.plan_id}")
        else:
            self.store.put_kv(f"{inc.id}:fix_closed", True)
            self.store.transition(inc, IncidentState.ESCALATED)
            rb ="rolled back" if result.rolled_back else "could not roll back (action is not reversible)"
            await self.note(inc, f":rotating_light: Verification {result.verify} for `{plan.action}` — {rb}. Runbook autonomy "
                                 f"demoted. On-call needs to take over.", key=f"verify-fail:{plan.plan_id}")
            await self.page(inc, f"the fix `{plan.action}` did not recover the SLOs ({rb}); a human needs to take over",
                            f"verify-fail:{plan.plan_id}")
            waiting = self.store.get_kv(f"{inc.id}:alt_after_failure")
            if waiting:
                from judge.reasoning.discuss import AlternativeFix

                self.store.put_kv(f"{inc.id}:alt_after_failure", None)
                who = waiting.pop("proposed_by", "an engineer")
                await self.offer_alternative(self.store.get_incident(inc.id), AlternativeFix(**waiting), who, "")
            await self.linear_comment(inc, f"Remediation `{plan.action}` verify={result.verify}; rolled_back="
                                           f"{result.rolled_back}. Escalated.", scope=f"fail:{plan.plan_id}")

    async def _resume_plans(self) -> None:
        for inc in self.store.incidents([IncidentState.EXECUTING, IncidentState.VERIFYING, IncidentState.ROLLING_BACK]):
            await self._resume_incident_plan(inc)

    async def _resume_incident_plan(self, inc: Incident) -> None:
        if inc.id in self.tasks:
            return
        active = self.store.active_plan(inc.id)
        if not active:
            self.store.transition(inc, IncidentState.ESCALATED)
            return
        plan, status = active
        dec = next((d for d in reversed(self.store.decisions(inc.id))
                    if d.intent == "remediation.execute" and d.allowed), None)
        if dec is None:
            self.store.save_plan(plan, "cancelled")
            self.store.transition(inc, IncidentState.ESCALATED)
            return
        log.info("resuming plan %s for %s from status %s", plan.plan_id, inc.id, status)
        if status in ("approved", "awaiting_approval", "veto_window"):
            self.tasks[inc.id] = asyncio.create_task(self._run_plan(inc.id, plan, dec, dec.approval_id))
        else:
            self.tasks[inc.id] = asyncio.create_task(self._run_plan(inc.id, plan, dec, dec.approval_id,
                                                                    resume_status=status))

    # ---------------------------------------------------------------- chat, progress, resolution

    async def chat_commands(self, inc: Incident) -> None:
        if not inc.slack_thread_ts:
            return
        cursor = self.store.get_kv(f"{inc.id}:chat_cursor", inc.slack_thread_ts)
        try:
            msgs = await self.d.slack.replies(self.s.slack_oncall_channel, inc.slack_thread_ts, oldest=cursor)
        except Exception:
            return
        for m in msgs:
            ts = m.get("ts")
            if not ts or ts <= cursor:
                continue
            cursor = max(cursor, ts)
            text = m.get("text") or ""
            if m.get("bot_id") or text.lower().startswith(("approve", "reject")):
                continue
            if RESOLVE_REQUEST.search(text):
                dec = self.decide(Intent(kind="incident.resolve", incident_id=inc.id, payload={"requested_by": m.get("user")}),
                                  await self.pctx_resolution(inc))
                if dec.allowed:
                    await self.resolve(inc)
                    return
                await self.note(inc, f"<@{m.get('user')}> asked to resolve, but NOT yet — {', '.join(dec.rules)}: "
                                     f"{dec.explain}", key=f"resolve-deny:{ts}")
                continue
            if m.get("user") and m.get("subtype") is None:
                self.store.put_kv(f"{inc.id}:chat_cursor", cursor)
                await self.discuss(inc, m)
        self.store.put_kv(f"{inc.id}:chat_cursor", cursor)

    # ================================================================ discussion & diagnosis

    async def _discuss_context(self, inc: Incident):
        from judge import narration
        from judge.reasoning.discuss import DiscussContext

        sigs = self.store.signals_for(inc.id)
        metrics = {svc: self._metrics_for(svc, inc.environment) for svc in inc.services}
        active = self.store.active_plan(inc.id)
        plan = active[0] if active else None
        return DiscussContext(
            incident_summary=f"{inc.severity} {', '.join(inc.services)} ({inc.environment.value}), state "
                             f"{inc.state.value}, impact {inc.customer_impact}",
            services=inc.services,
            evidence=narration.evidence_lines(sigs, metrics, self._sentry_meta(sigs)),
            diagnosis=self.store.get_kv(f"{inc.id}:diagnosis"),
            runbook=self.store.get_kv(f"{inc.id}:match"),
            current_plan=({"action": plan.action, "params": plan.params, "target": plan.target_service,
                           "source": plan.source, "status": active[1]} if plan else None),
            changes=await self._recent_changes(inc),
            docs=self._docs(inc),
            history=self.store.get_kv(f"{inc.id}:discussion") or [],
            catalog={name: {"params_schema": a.params_schema, "reversible": a.reversible,
                            "blast_radius": a.blast_radius} for name, a in self.c.actions.items()},
        )

    async def _recent_changes(self, inc: Incident) -> list[dict]:
        try:
            since = (inc.created_at - timedelta(minutes=30)).isoformat()
            return [c for c in await self.d.control.changes(since=since)
                    if c.get("service") in inc.services or c.get("service", "").split("@")[0] in inc.services]
        except Exception:
            return []

    def _docs(self, inc: Incident) -> list[tuple[str, str]]:
        try:
            from judge.memory.docs import docs_for

            return docs_for(self.d.repo, inc.services, self.c)
        except Exception:
            return []

    async def discuss(self, inc: Incident, m: dict) -> None:
        from judge.reasoning.discuss import make_discussant

        author = await self._person(m.get("user", ""))
        text = m.get("text") or ""
        ctx = await self._discuss_context(inc)
        discussant = self.d.extras.get("discussant") or make_discussant(self.s)
        self.d.extras["discussant"] = discussant
        reply = await discussant.reply(ctx, text, author)
        history = (self.store.get_kv(f"{inc.id}:discussion") or []) + [
            {"ts": m.get("ts"), "author": author, "role": "human", "text": redact(text, 1500)},
            {"ts": now().isoformat(), "author": "Incident Judge", "role": "agent", "text": reply.answer}]
        self.store.put_kv(f"{inc.id}:discussion", history[-40:])
        await self.note(inc, f":speech_balloon: {reply.answer}", key=f"discuss:{m.get('ts')}")
        if reply.alternative_fix is not None:
            await self.offer_alternative(inc, reply.alternative_fix, author, m.get("user", ""))

    async def offer_alternative(self, inc: Incident, alt, author: str, user_id: str) -> None:
        from judge.core.models import ActionProposal
        from judge.remediation.planner import build_plan

        active = self.store.active_plan(inc.id)
        if active and active[1] not in ("awaiting_approval", "veto_window", "proposed"):
            await self.note(inc, f"A fix (`{active[0].action}`) is already running, so I won't start another one now. "
                                 "If its verification fails I'll bring your plan back up.", key=f"alt-busy:{alt.action}")
            self.store.put_kv(f"{inc.id}:alt_after_failure", {**alt.model_dump(), "proposed_by": author})
            return
        plan = build_plan(inc, ActionProposal(name=alt.action, params=alt.params, target_service=alt.target_service),
                          None, self.c, AutonomyLevel.L1)
        plan = plan.model_copy(update={"source": "human", "proposed_by": author, "rationale": alt.why})
        dec = self.decide(Intent(kind="remediation.execute", incident_id=inc.id, payload={"plan": plan.plan_hash,
                                                                                         "scope": "human"}),
                          self.pctx_plan(inc, plan, None, {}))
        if dec.result == DecisionResult.DENY:
            await self.note(inc, f":no_entry: I can't offer that plan — {', '.join(dec.rules)}: {dec.explain}",
                            key=f"alt-deny:{plan.plan_hash}")
            return
        if active:
            self.store.save_plan(active[0], "cancelled")
            self.store.put_kv(f"{inc.id}:superseded_plan", active[0].plan_id)
        self.store.save_plan(plan, "awaiting_approval")
        self.store.put_kv(f"{inc.id}:alt_meta:{plan.plan_id}", {"why": alt.why, "risk": alt.risk,
                                                                "replaces": (f"`{active[0].action}` {active[0].params}"
                                                                             if active else None)})
        self.store.put_kv(f"{inc.id}:fix_closed", None)
        self.store.put_kv(f"{inc.id}:fix_pending", {"plan_id": plan.plan_id, "ts": None, "at": now().isoformat(),
                                                    "kind": "fix", "level": "L1"})
        self.store.transition(inc, IncidentState.AWAITING_FIX_APPROVAL)
        await self.flush_cards(inc)

    async def diagnosis_flow(self, inc: Incident) -> None:
        """New or look-alike failures: read the docs, the change log and the metrics, explain the likely cause and
        propose a catalog fix with WHY. Never automatic: a diagnosis plan always needs an explicit click."""
        if self.store.get_kv(f"{inc.id}:diagnosis_state") or inc.environment != Env.production:
            return
        match = self.store.get_kv(f"{inc.id}:match") or {}
        if match.get("runbook_id") and match.get("match_ok") is True:
            return
        if self.store.active_plan(inc.id) or self.store.get_kv(f"{inc.id}:fix_closed") is True:
            return
        self.store.put_kv(f"{inc.id}:diagnosis_state", "running")
        try:
            from judge.reasoning.diagnose import ClaudeDiagnoser, HeuristicDiagnoser
        except Exception:
            return
        diagnoser = (ClaudeDiagnoser(self.s.anthropic_api_key, self.s.memory_model)
                     if self.s.judge_impl == "claude" and self.s.anthropic_api_key else HeuristicDiagnoser())
        sigs = self.store.signals_for(inc.id)
        metrics = {svc: self._metrics_for(svc, inc.environment) for svc in inc.services}
        try:
            dg = await diagnoser.diagnose(
                incident=inc, signals=sigs, metrics=metrics, changes=await self._recent_changes(inc),
                docs=self._docs(inc), runbook_match=match,
                catalog_actions=self.c.actions)
        except Exception:
            log.exception("diagnosis failed for %s", inc.id)
            self.store.put_kv(f"{inc.id}:diagnosis_state", "failed")
            return
        data = dg.model_dump(mode="json")
        self.store.put_kv(f"{inc.id}:diagnosis", data)
        self.store.put_kv(f"{inc.id}:diagnosis_state", "done")
        from judge import narration

        text = narration.diagnosis_text(data, had_runbook=bool(match.get("runbook_id")))
        await self.note(inc, text, key="diagnosis")
        await self.linear_comment(inc, text.replace("*", "**"), scope="diagnosis")
        fix = data.get("recommended_fix")
        if not fix and inc.severity and inc.severity.rank <= 2:
            await self.page(inc, f"no safe automatic fix for this failure — {data.get('summary', '')[:200]}",
                            "no-safe-fix")
        if not fix or self.store.active_plan(inc.id):
            return
        from judge.core.models import ActionProposal
        from judge.remediation.planner import build_plan

        plan = build_plan(inc, ActionProposal(name=fix["action"], params=fix.get("params") or {},
                                              target_service=fix["target_service"]), None, self.c, AutonomyLevel.L1)
        plan = plan.model_copy(update={"source": "diagnosis", "rationale": fix.get("why_this_fixes_it", "")})
        dec = self.decide(Intent(kind="remediation.execute", incident_id=inc.id,
                                 payload={"plan": plan.plan_hash, "scope": "diagnosis"}),
                          self.pctx_plan(inc, plan, None, {}))
        if dec.result == DecisionResult.DENY:
            await self.note(inc, f"I won't offer the diagnosed fix — {', '.join(dec.rules)}: {dec.explain}",
                            key="diagnosis-deny")
            return
        self.store.save_plan(plan, "awaiting_approval")
        self.store.put_kv(f"{inc.id}:fix_pending", {"plan_id": plan.plan_id, "ts": None, "at": now().isoformat(),
                                                    "kind": "fix", "level": "L1"})
        self.store.transition(inc, IncidentState.AWAITING_FIX_APPROVAL)
        await self.flush_cards(inc)

    async def linear_progress(self, inc: Incident) -> None:
        if not inc.linear_issue_id:
            return
        bucket_s = max(30, 300 * self.s.time_scale)
        bucket = int(now().timestamp() // bucket_s)
        last = self.store.get_kv(f"{inc.id}:progress_bucket")
        if last is None:
            self.store.put_kv(f"{inc.id}:progress_bucket", bucket)
            return
        if bucket == last:
            return
        self.store.put_kv(f"{inc.id}:progress_bucket", bucket)
        if inc.state not in (IncidentState.OPEN, IncidentState.ESCALATED, IncidentState.AWAITING_FIX_APPROVAL):
            return  # once a fix is running/verified, the verification comment tells the story
        sigs = self.store.signals_for(inc.id)
        total = sum(s.count for s in sigs if s.source == "sentry")
        prev = self.store.get_kv(f"{inc.id}:progress_count", 0)
        if total > 0 and (prev == 0 or total >= prev * 1.5):
            self.store.put_kv(f"{inc.id}:progress_count", total)
            users = max((s.user_count for s in sigs if s.source == "sentry"), default=0)
            waiting = " Waiting for an on-call decision in Slack." if inc.state == IncidentState.AWAITING_FIX_APPROVAL \
                else ""
            await self.linear_comment(inc, f"**Still happening:** {total} errors so far, {users} users affected.{waiting}",
                                      scope=f"progress:{bucket}")

    async def resolution_check(self, inc: Incident) -> None:
        ctx = await self.pctx_resolution(inc)
        dec = evaluate(Intent(kind="incident.resolve", incident_id=inc.id), ctx)
        if dec.allowed:
            self.store.add_decision(dec)
            await self.resolve(inc)

    async def resolve(self, inc: Incident, forced: bool = False) -> None:
        active = self.store.active_plan(inc.id)
        if active and active[1] in ("awaiting_approval", "veto_window"):
            self.store.save_plan(active[0], "cancelled")
        rctx = await self.pctx_resolution(inc) if not forced else self._forced_ctx(inc)
        await self.public_update(inc, "resolved", ctx=rctx)
        dec = self.decide(Intent(kind="linear.close", incident_id=inc.id), rctx)
        if dec.allowed and inc.linear_issue_id:
            await self.outbox.run(app="linear", op="close_issue", incident_id=inc.id, scope="", decision=dec,
                                  reconcile=lambda mk: self._linear_closed(inc.linear_issue_id),
                                  execute=lambda mk: self._linear_close(inc.linear_issue_id, mk))
        await self.note(inc, ":large_green_circle: *Status: resolved.* No errors for the quiet window and SLOs are healthy. "
                             "Status page and Linear were updated. *Next:* the agent writes what it learned to the runbook wiki.",
                        key="resolved")
        if self.s.backend == "real":
            for issue_id in sorted({s.external_id for s in self.store.signals_for(inc.id)
                                    if s.source == "sentry" and s.external_id}):
                if self.decide(Intent(kind="sentry.resolve_issue", incident_id=inc.id,
                                      payload={"issue": issue_id}), rctx).allowed:
                    try:
                        await self.d.sentry.resolve_issue(issue_id)
                        self.store.put_kv(f"{inc.id}:sentry_resolved", sorted(
                            {*(self.store.get_kv(f"{inc.id}:sentry_resolved") or []), issue_id}))
                    except Exception as e:
                        log.warning("Sentry resolve %s failed: %r", issue_id, e)
        pd = self.d.extras.get("pagerduty")
        if pd is not None and pd.enabled and self.store.get_kv(f"{inc.id}:paged"):
            try:
                await pd.resolve(f"ij-{inc.id}")
                self.store.put_kv(f"{inc.id}:paged_resolved", now().isoformat())
                await self.note(inc, ":pager: PagerDuty incident resolved.", key="paged-resolved")
            except Exception as e:
                log.warning("PagerDuty resolve failed: %r", e)
        inc = self.store.get_incident(inc.id)
        inc.resolved_at = now()
        self.store.transition(inc, IncidentState.RESOLVED)
        try:
            self._sync_stats()
            await self.post_report(inc)
        except Exception:
            log.exception("incident report failed")

    def _forced_ctx(self, inc: Incident) -> PolicyContext:
        ctx = self.pctx(inc)
        ctx.seconds_since_last_event, ctx.metrics_available, ctx.slo_healthy = 1e9, True, True  # B2 only
        return ctx

    async def _linear_closed(self, issue_id: str) -> str | None:
        try:
            issue = await self.d.linear.get_issue(issue_id)
        except Exception:
            return None
        state = (issue.get("state") or {}).get("type")
        return issue_id if state in ("completed", "canceled") else None

    async def _linear_close(self, issue_id: str, mk: str) -> str:
        await self.d.linear.comment(issue_id, f"Resolved by Incident Judge. {slack_thread_link(mk, self.s.slack_oncall_channel)}")
        await self.d.linear.close_issue(issue_id)
        return issue_id

    async def close(self, inc: Incident) -> None:
        """RESOLVED -> CLOSED: write raw timeline, recompute stats, propose wiki update (PR)."""
        if self.baseline != "B1" and inc.environment == Env.production or self.store.get_kv(f"{inc.id}:force_ingest"):
            try:
                self.d.ingestor.write_raw(inc)
                self._sync_stats()
                proposal = await self.d.ingestor.propose(inc)
                if proposal is not None:
                    await self._handle_proposal(inc, proposal)
            except Exception:
                log.exception("memory ingest failed for %s", inc.id)
        await self._mirror_main("knowledge: incident timeline")
        self.store.transition(inc, IncidentState.CLOSED)

    async def _handle_proposal(self, inc: Incident, proposal) -> None:
        from judge.memory.pr_validator import validate_proposal

        if os.environ.get("IJ_TEST_WRITER") == "tamper_stats":
            from judge.testhooks import tamper_proposal

            proposal = tamper_proposal(self.d.repo, proposal)
        errors = validate_proposal(self.d.repo, proposal, self.c)
        ctx = self.pctx(inc)
        ctx.pr_validation_errors = errors
        dec = self.decide(Intent(kind="memory.propose", incident_id=inc.id,
                                 payload={"proposal_id": proposal.id, "proposal_hash": proposal.hash}), ctx)
        if not dec.allowed:
            self.d.repo.reject_proposal(proposal.id, "; ".join(errors) or dec.explain)
            await self.note(inc, f"Runbook update proposal rejected automatically — {dec.explain}", key=f"mem-reject:{proposal.id}")
            return
        from judge.approvals.response_card import CardItem, build_card

        pr_line = []
        mirror = self.d.extras.get("github")
        if mirror is not None:
            try:
                await self._mirror_main("knowledge: incident timeline and stats")
                pr = await mirror.open_pr(proposal, f"{inc.severity} {', '.join(inc.services)} incident",
                                          "Slack: the incident thread in the on-call channel.")
                pr_line = [f"Review on GitHub: <{pr['url']}|pull request #{pr['number']}> (merge there or here)"]
                self.store.put_kv(f"{inc.id}:runbook_pr", {"number": pr["number"], "url": pr["url"],
                                                           "proposal_id": proposal.id, "title": proposal.title,
                                                           "at": now().isoformat()})
                await self.linear_comment(inc, f"**Runbook update proposed:** [GitHub PR #{pr['number']}]({pr['url']})",
                                          scope=f"pr:{proposal.id}")
            except Exception as e:
                log.warning("GitHub PR for proposal failed: %r", e)
        text, blocks = build_card(inc, [CardItem(
            "memory_merge", proposal.hash, f"Update the runbook wiki: `{proposal.title}`",
            [*pr_line, f"Files: {', '.join(proposal.files)}",
             "Validated automatically: schema, required sections, no secrets, stats/autonomy untouched by the LLM",
             "Merging changes what the agent knows next time; it does not change any system now"])],
            oncall=self.c.policy.oncall_allowlist, timeout_min=60 * 24,
            title=":books: Review a runbook update learned from this incident")
        text = text.replace("nothing is posted or changed (safe default)", "the proposal stays unmerged")
        ts = await self.note(inc, text, key=f"mem-card:{proposal.id}", blocks=blocks)
        pending = self.store.get_kv("memory_pending", [])
        pending.append({"proposal_id": proposal.id, "incident_id": inc.id, "ts": ts or "0"})
        self.store.put_kv("memory_pending", pending)

    async def memory_flow(self) -> None:
        pending = self.store.get_kv("memory_pending", [])
        if not pending:
            return
        from judge.memory.pr_validator import validate_proposal

        keep = []
        for item in pending:
            proposal = self.d.repo.proposal(item["proposal_id"])
            inc = self.store.get_incident(item["incident_id"])
            if proposal is None or proposal.status != "open" or inc is None:
                continue
            await self.d.approval_poller.poll(inc, "memory_merge", proposal.hash, item["ts"], channel=self.s.slack_oncall_channel)
            approvals = [a for a in self.store.approvals(inc.id, "memory_merge") if a.subject_hash == proposal.hash]
            valid = next((a for a in reversed(approvals) if a.valid), None)
            mirror = self.d.extras.get("github")
            if valid is None and mirror is not None:
                try:
                    merged = await mirror.merged_on_github(proposal.id)
                    if merged:
                        errors = validate_proposal(self.d.repo, proposal, self.c)
                        if errors:
                            await self.note(inc, f"The PR was merged on GitHub but failed validation locally ({errors[0]}); "
                                                 "the agent keeps its previous knowledge.", key=f"gh-invalid:{proposal.id}")
                        else:
                            self.d.repo.merge_proposal(proposal.id)
                            self._sync_stats()
                            await self._mirror_main(f"knowledge: {proposal.title}")
                            await self.note(inc, f":books: Runbook update merged on GitHub by {merged.get('merged_by')}. "
                                                 "The agent will use it from the next incident.",
                                            key=f"mem-merged:{proposal.id}")
                        continue
                    if await mirror.closed_on_github(proposal.id):
                        self.d.repo.reject_proposal(proposal.id, "closed on GitHub")
                        continue
                except Exception as e:
                    log.warning("GitHub PR status check failed: %r", e)
            if valid is None:
                keep.append(item)
                continue
            ctx = self.pctx(inc, approval=valid)
            ctx.pr_validation_errors = validate_proposal(self.d.repo, proposal, self.c)
            dec = self.decide(Intent(kind="memory.merge", incident_id=inc.id,
                                     payload={"proposal_id": proposal.id, "proposal_hash": proposal.hash}), ctx)
            if dec.allowed:
                self.d.repo.merge_proposal(proposal.id)
                self._sync_stats()
                pr = mirror.pr_for(proposal.id) if mirror is not None else None
                if mirror is not None:
                    try:
                        await mirror.merge(proposal.id, proposal.title)
                    except Exception as e:
                        log.warning("GitHub merge failed: %r", e)
                where = f" and merged <{pr['url']}|PR #{pr['number']}> on GitHub" if pr else ""
                await self.note(inc, f":books: Runbook update merged (approved by {await self._person(valid.user_id)}){where}.",
                                key=f"mem-merged:{proposal.id}")
            elif valid.verdict == "reject":
                self.d.repo.reject_proposal(proposal.id, f"rejected by {valid.user_id}")
                if mirror is not None:
                    try:
                        await mirror.reject(proposal.id, f"rejected in Slack by {await self._person(valid.user_id)}")
                    except Exception:
                        pass
            else:
                keep.append(item)
        self.store.put_kv("memory_pending", keep)

    # ================================================================ policy plumbing

    def decide(self, intent: Intent, ctx: PolicyContext) -> Decision:
        if self.baseline == "B0":
            dec = Decision(incident_id=intent.incident_id, trial_id=self.s.trial_id, intent=intent.kind,
                           result=DecisionResult.ALLOW, rules=["B0-bypass"], explain="baseline B0: no policy engine")
        else:
            dec = evaluate(intent, ctx)
        sig = [dec.result.value, list(dec.rules), dec.approval_id, dec.explain]
        key = f"lastdec:{intent.incident_id}:{intent.kind}:{intent.payload.get('scope', '')}"
        if dec.allowed or self.store.get_kv(key) != sig:
            self.store.add_decision(dec)
            self.store.put_kv(key, sig)
        return dec

    def pctx(self, inc: Incident, **kw) -> PolicyContext:
        ctx = PolicyContext(settings=self.s, config=self.c, incident=inc, kill_switch=self.store.kill_switch(),
                            metrics_available=bool(self.d.metrics.available()))
        pdata = self.store.get_kv(f"{inc.id}:proposal")
        if pdata:
            ctx.proposal = TriageProposal.model_validate(pdata)
        for k, v in kw.items():
            setattr(ctx, k, v)
        return ctx

    def pctx_plan(self, inc: Incident, plan, runbook, match: dict, **kw) -> PolicyContext:
        from judge.remediation import autonomy as autonomy_mod

        svc = plan.target_service
        ctx = self.pctx(inc, **kw)
        ctx.plan = plan
        ctx.runbook_id = plan.runbook_id
        fm = runbook.frontmatter if runbook is not None else None
        ctx.runbook_action = fm.action.name if fm and fm.action else None
        ctx.runbook_match_ok = match.get("match_ok") if match.get("runbook_id") == plan.runbook_id else None
        ctx.runbook_merged = bool(match.get("merged", True))
        ctx.effective_autonomy = plan.autonomy_level
        ctx.service_lock_holder = self.store.lock_holder(f"service:{svc}")
        ctx.executions_last_hour = autonomy_mod.executions_last_hour(self.store, svc, now())
        ctx.consecutive_failures = autonomy_mod.consecutive_failures(self.store, svc)
        ctx.current_rps = self.d.metrics.value("rps", svc, self._window(30))
        return ctx

    async def pctx_resolution(self, inc: Incident) -> PolicyContext:
        ctx = self.pctx(inc)
        last: datetime | None = None
        for sig in self.store.signals_for(inc.id):
            if sig.source == "sentry" and sig.external_id:
                ls = await self.d.sentry_poller.last_seen(sig.external_id)
                ls = ls or sig.last_seen
            else:
                ls = self.d.slo_poller.last_firing(sig.service) if sig.source == "slo" else sig.last_seen
            if ls and (last is None or ls > last):
                last = ls
        for svc in inc.services:
            lf = self.d.slo_poller.last_firing(svc)
            if inc.environment == Env.production and lf and (last is None or lf > last):
                last = lf
        ctx.seconds_since_last_event = (now() - last).total_seconds() if last else None
        healthy = True
        for svc in inc.services:
            key = self._metric_key(svc, inc.environment)
            entry = self.c.service(svc)
            if entry and not entry.routes:
                continue  # no request traffic to measure (batch); rely on signal quiet window
            er = self.d.metrics.value("error_rate", key, self._window(30))
            p95 = self.d.metrics.value("latency_p95", key, self._window(30))
            if er is None or er >= 0.02 or (p95 is not None and p95 >= 0.8):
                healthy = False
        ctx.slo_healthy = healthy
        return ctx

    def _latest_approval(self, inc: Incident, kind: str, subject: str):
        items = [a for a in self.store.approvals(inc.id, kind) if a.subject_hash == subject and a.valid]
        return items[-1] if items else None

    # ================================================================ comms helpers

    async def note(self, inc: Incident, text: str, key: str, channel: str | None = None, thread: bool = True,
                   blocks: list | None = None) -> str | None:
        channel = channel or self.s.slack_oncall_channel
        thread_ts = inc.slack_thread_ts if thread and channel == self.s.slack_oncall_channel else None
        dec = self.decide(Intent(kind="slack.post", incident_id=inc.id, payload={"scope": key}), self.pctx(inc))
        if not dec.allowed:
            return None
        body = redact(text, 3500) if self.baseline != "B0" else text
        try:
            res = await self.outbox.run(
                app="slack", op="post", incident_id=inc.id, scope=f"{channel}:{key}", decision=dec,
                reconcile=lambda mk: self.d.slack.find_message_by_marker(channel, mk, thread_ts),
                execute=lambda mk: self.d.slack.post(channel, body, thread_ts=thread_ts, blocks=blocks, ref=mk),
            )
            return res.ref
        except Exception as e:
            log.warning("slack note failed (%s): %r", key, e)
            return None

    @staticmethod
    def _with_marker(blocks: list | None, mk: str) -> list | None:
        if not blocks:
            return None
        return blocks

    async def flush_cards(self, inc: Incident) -> None:
        """Post ONE card for everything that newly needs a human on this incident (bundled public post + fix)."""
        from judge.approvals.response_card import CardItem, build_card

        pub = self.store.get_kv(f"{inc.id}:public_pending")
        fix = self.store.get_kv(f"{inc.id}:fix_pending")
        items: list[tuple[str, CardItem]] = []
        if pub and pub.get("ts") is None and not inc.public_posted and inc.customer_impact \
                and inc.customer_impact != CustomerImpact.none:
            services = [self.c.service(s) for s in inc.services]
            title = templates.public_title(inc.customer_impact, services)
            status = templates.INSTATUS_COMPONENT_STATUS[inc.customer_impact]
            items.append(("public", CardItem(
                "public_post", pub["subject"], f"Post to the public status page: \u201c{title}\u201d",
                [f"Customers will see it: {', '.join(s.capability for s in services)} \u2192 {status}",
                 "Text comes from a fixed template; no internal details are published",
                 "Later updates (monitoring, resolved) follow automatically"])))
        if fix and fix.get("ts") is None:
            got = self.store.get_plan(fix["plan_id"])
            if got:
                plan = got[0]
                rb = self.d.repo.get_runbook(plan.runbook_id) if plan.runbook_id else None
                stats = rb.frontmatter.stats if rb is not None else None
                params = ", ".join(f"{k}={v}" for k, v in plan.params.items()) or "no parameters"
                conds = ", ".join(f"{c.metric} {c.op} {c.value}" for c in plan.verify.conditions)
                kind = fix.get("kind", "fix")
                from judge import narration

                try:
                    current = await self.d.control.get_config(plan.target_service)
                except Exception:
                    current = None
                change, rollback = narration.change_preview(plan, current)
                match = self.store.get_kv(f"{inc.id}:match") or {}
                details = [
                    f"Exactly what changes: {change}",
                    (f"Why: runbook `{rb.frontmatter.id}` — {stats.success} verified successes, {stats.failure} "
                     f"failures, autonomy {fix.get('level', 'L1')}")
                    if rb is not None and stats is not None else "Why: proposed by the judge",
                    *[f"   {line.strip()}" for line in narration.match_lines(match)[1:]],
                    f"How I'll know it worked: {narration.verify_targets(plan)} on live metrics",
                    f"If it doesn't work: automatic rollback — {rollback}",
                ]
                summary = f"Fix: `{plan.action}` ({params}) on {plan.target_service}"
                if plan.source == "human":
                    meta = self.store.get_kv(f"{inc.id}:alt_meta:{plan.plan_id}") or {}
                    summary = f"Fix proposed by {plan.proposed_by}: `{plan.action}` ({params}) on {plan.target_service}"
                    details = [f"Exactly what changes: {change}",
                               f"Why (as understood from the thread): {meta.get('why') or plan.rationale or '-'}",
                               *([f"Risk: {meta['risk']}"] if meta.get("risk") else []),
                               *([f"Replaces the agent's plan: {meta['replaces']}"] if meta.get("replaces") else []),
                               f"How I'll know it worked: {narration.verify_targets(plan)} on live metrics",
                               f"If it doesn't work: automatic rollback — {rollback}"]
                elif plan.source == "diagnosis":
                    dg = self.store.get_kv(f"{inc.id}:diagnosis") or {}
                    rf = dg.get("recommended_fix") or {}
                    summary = f"Fix from diagnosis (no runbook yet): `{plan.action}` ({params}) on {plan.target_service}"
                    details = [f"Exactly what changes: {change}",
                               f"Why this should fix it: {rf.get('why_this_fixes_it') or plan.rationale or '-'}",
                               *([f"Risk: {rf['risk']}"] if rf.get("risk") else []),
                               f"How I'll know it worked: {narration.verify_targets(plan)} on live metrics",
                               f"If it doesn't work: automatic rollback — {rollback}",
                               "This is a first-time fix: it never runs without your click, and if it works it can "
                               "become a runbook"]
                if fix.get("updated"):
                    details.insert(0, "The plan CHANGED since the previous card; earlier approvals no longer apply")
                items.append(("fix", CardItem(kind, plan.plan_hash, summary, details)))
        if not items:
            return
        where = f"{inc.severity or ''} {', '.join(inc.services)} ({inc.environment.value})".strip()
        kinds = {i.kind for _, i in items}
        if kinds in ({"public_post", "fix"}, {"public_post"}, {"fix"}, {"veto"}, {"memory_merge"}):
            groups = [items]
        else:
            groups = [[it] for it in items]
        for group in groups:
            gkinds = {i.kind for _, i in group}
            if gkinds == {"veto"}:
                title = f":hourglass_flowing_sand: Automatic fix scheduled \u2014 {where}"
                minutes = self.s.human_window(self.c.policy.veto_window_s) / 60
            else:
                title = f":rotating_light: Action needed \u2014 {where}"
                ttls = [self.c.policy.public_approval_ttl_s if i.kind == "public_post" else
                        self.c.policy.fix_approval_ttl_s for _, i in group]
                minutes = self.s.human_window(min(ttls)) / 60
            text, blocks = build_card(inc, [i for _, i in group], oncall=self.c.policy.oncall_allowlist,
                                      timeout_min=minutes, title=title)
            key = "card:" + ":".join(i.subject_hash[:8] for _, i in group)
            ts = await self.note(inc, text, key=key, blocks=blocks)
            for which, _ in group:
                k = f"{inc.id}:public_pending" if which == "public" else f"{inc.id}:fix_pending"
                cur = self.store.get_kv(k) or {}
                self.store.put_kv(k, {**cur, "ts": ts or "0"})

    def current_subject(self, inc: Incident, kind: str, clicked: str) -> str | None:
        """What a card button may approve right now (used by the Socket Mode handler)."""
        inc = self.store.get_incident(inc.id) or inc
        if kind == "public_post":
            if inc.public_posted or not inc.customer_impact:
                return None
            return public_subject_hash(inc, inc.customer_impact.value)
        if kind in ("fix", "veto"):
            active = self.store.active_plan(inc.id)
            return active[0].plan_hash if active else None
        if kind == "memory_merge":
            for p in self.d.repo.proposals():
                if p.hash == clicked and p.status == "open":
                    return p.hash
        return None

    async def linear_comment(self, inc: Incident, text: str, scope: str) -> None:
        if not inc.linear_issue_id:
            return
        dec = self.decide(Intent(kind="linear.comment", incident_id=inc.id, payload={"scope": scope}), self.pctx(inc))
        if not dec.allowed:
            return
        issue_id = inc.linear_issue_id
        try:
            await self.outbox.run(
                app="linear", op="comment", incident_id=inc.id, scope=scope, decision=dec,
                reconcile=lambda mk: self.d.linear.find_comment_by_marker(issue_id, mk),
                execute=lambda mk: self.d.linear.comment(
                    issue_id, f"{redact(text, 3500)}\n\n{slack_thread_link(mk, self.s.slack_oncall_channel)}"),
            )
        except Exception as e:
            log.warning("linear comment failed: %r", e)

    def _sync_stats(self) -> None:
        try:
            from judge.memory.stats import sync_code_owned

            sync_code_owned(self.d.repo, self.store, self.c, now())
        except Exception:
            log.exception("stats sync failed")
