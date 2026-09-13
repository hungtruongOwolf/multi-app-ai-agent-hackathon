"""Deterministic policy engine. Pure: evaluate(intent, ctx) -> Decision. No network, no LLM.

Every external write and every remediation goes through here first (invariant I2).
Combination: any DENY -> DENY; else any REQUIRE_APPROVAL -> REQUIRE_APPROVAL; else any
DOWNGRADE -> DOWNGRADE; else ALLOW."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from judge.core.models import (
    Approval,
    AutonomyLevel,
    Decision,
    DecisionResult,
    Incident,
    Intent,
    Plan,
    Signal,
    TriageProposal,
    canonical_hash,
    now,
)
from judge.settings import Config, Settings


@dataclass
class PolicyContext:
    settings: Settings
    config: Config
    incident: Incident | None = None
    proposal: TriageProposal | None = None
    signals: list[Signal] = field(default_factory=list)
    at: datetime = field(default_factory=now)
    kill_switch: bool = False

    # measured facts (filled by the caller from Sentry / metrics; never from the LLM)
    metrics_available: bool = False
    seconds_since_last_event: float | None = None
    slo_healthy: bool | None = None
    current_rps: float | None = None

    # remediation facts
    plan: Plan | None = None
    runbook_id: str | None = None
    runbook_action: str | None = None
    runbook_match_ok: bool | None = None
    runbook_merged: bool = True
    effective_autonomy: AutonomyLevel = AutonomyLevel.L0
    approval: Approval | None = None
    veto_window_elapsed: bool = False
    vetoed: bool = False
    service_lock_holder: str | None = None
    executions_last_hour: int = 0
    consecutive_failures: int = 0

    # memory facts
    pr_validation_errors: list[str] = field(default_factory=list)

    # merge facts
    merge_target: Incident | None = None


@dataclass
class RuleHit:
    rule: str
    result: DecisionResult
    explain: str
    approval_kind: str | None = None
    downgrade_to: str | None = None


PUBLIC_WRITE = {"instatus.create_incident", "instatus.update"}
RESOLVE = {"instatus.resolve", "incident.resolve", "linear.close"}
CREATE_ONCE = {"linear.create_issue": "linear_issue_id", "instatus.create_incident": "instatus_incident_id",
               "slack.create_channel": "slack_channel_id"}
IMPACT_ORDER = ["none", "degraded", "partial_outage", "major_outage"]


def deny(rule: str, explain: str) -> RuleHit:
    return RuleHit(rule, DecisionResult.DENY, explain)


# ---------------------------------------------------------------- rules


def p01_env(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind in PUBLIC_WRITE and c.incident and c.incident.environment.value != "production":
        return deny("P1", f"environment={c.incident.environment.value} (source: signal project/tag, not LLM)")
    return None


def p02_public_service(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind in PUBLIC_WRITE and c.incident:
        private = [s for s in c.incident.services if not (c.config.service(s) and c.config.service(s).public)]
        if private:
            return deny("P2", f"service(s) not public in catalog: {private}")
    return None


def p03_template_only(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind in PUBLIC_WRITE | {"instatus.resolve"}:
        allowed = {"phase", "impact", "components"}
        extra = set(intent.payload) - allowed
        if extra:
            return deny("P3", f"public write carries non-template fields: {sorted(extra)}")
    return None


def p04_no_premature_resolve(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind not in RESOLVE:
        return None
    quiet = c.config.policy.quiet_window_s * c.settings.time_scale
    if c.seconds_since_last_event is None or c.seconds_since_last_event < quiet:
        return deny("P4", f"signal still firing: last event {c.seconds_since_last_event}s ago < quiet window {quiet:.0f}s")
    if not c.metrics_available or c.slo_healthy is not True:
        return deny("P4", f"SLO not verified healthy (metrics_available={c.metrics_available}, healthy={c.slo_healthy})")
    return None


def p05_no_duplicates(intent: Intent, c: PolicyContext) -> RuleHit | None:
    attr = CREATE_ONCE.get(intent.kind)
    if attr and c.incident and getattr(c.incident, attr):
        return deny("P5", f"incident already has {attr}={getattr(c.incident, attr)}")
    return None


def p06_public_threshold_and_approval(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind != "instatus.create_incident" or not c.incident:
        return None
    impact = intent.payload.get("impact") or (c.incident.customer_impact and c.incident.customer_impact.value) or "none"
    pol = c.config.policy
    if IMPACT_ORDER.index(impact) < IMPACT_ORDER.index(pol.public_min_impact):
        return deny("P6", f"impact={impact} below public threshold {pol.public_min_impact}")
    if not c.incident.customer_visible:
        return deny("P6", "incident not customer-visible")
    sev = c.incident.severity.value if c.incident.severity else "SEV4"
    if impact in pol.public_requires_approval_impacts or sev in pol.public_requires_approval_severities:
        subject = public_subject_hash(c.incident, impact)
        a = c.approval
        if a and a.kind == "public_post" and a.valid and a.subject_hash == subject:
            if a.verdict == "reject":
                return deny("P6", f"public post rejected by {a.user_id}")
            ttl = c.settings.human_window(pol.public_approval_ttl_s)
            if (c.at - a.ts).total_seconds() > max(ttl, 1):
                return RuleHit("P6", DecisionResult.REQUIRE_APPROVAL, "approval expired", approval_kind="public_post")
            return None
        return RuleHit("P6", DecisionResult.REQUIRE_APPROVAL, f"impact={impact} severity={sev} needs human approval",
                       approval_kind="public_post")
    return None


def p07_action_catalog(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind != "remediation.execute":
        return None
    p = c.plan
    if p is None:
        return deny("P7", "no plan")
    if p.action not in c.config.actions:
        return deny("P7", f"action {p.action!r} not in catalog")
    if c.config.service(p.target_service) is None:
        return deny("P7", f"unknown target service {p.target_service!r}")
    if c.incident and p.target_service not in c.incident.services:
        return deny("P7", f"target {p.target_service} not part of incident services {c.incident.services}")
    if p.source in ("diagnosis", "human"):
        err = validate_params(c.config.actions[p.action].params_schema, p.params)
        return deny("P7", err) if err else None  # no runbook needed; P8 forces an explicit human approval
    if not c.runbook_id or c.runbook_action != p.action:
        return deny("P7", f"action {p.action} does not match a runbook action (runbook={c.runbook_id}, "
                          f"runbook_action={c.runbook_action})")
    if not c.runbook_merged:
        return deny("P7", "runbook content is not merged (unreviewed proposal)")
    if c.runbook_match_ok is not True:
        return deny("P7", f"runbook match_conditions not satisfied (result={c.runbook_match_ok})")
    err = validate_params(c.config.actions[p.action].params_schema, p.params)
    if err:
        return deny("P7", err)
    return None


def p08_autonomy(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind != "remediation.execute" or c.plan is None:
        return None
    if c.plan.source in ("diagnosis", "human"):
        return _check_fix_approval(c)  # never automatic, whatever the autonomy ladder says
    lvl = c.effective_autonomy
    if lvl == AutonomyLevel.L0:
        return deny("P8", "autonomy L0: suggest only")
    if lvl == AutonomyLevel.L1:
        return _check_fix_approval(c)
    if lvl == AutonomyLevel.L2:
        if c.vetoed or (c.approval and c.approval.valid and c.approval.verdict == "reject"
                        and c.approval.subject_hash == c.plan.plan_hash):
            return deny("P8", "vetoed during veto window")
        if c.approval and c.approval.valid and c.approval.verdict == "approve" \
                and c.approval.subject_hash == c.plan.plan_hash:
            return None
        if not c.veto_window_elapsed:
            return RuleHit("P8", DecisionResult.REQUIRE_APPROVAL, "L2: waiting for veto window", approval_kind="veto")
        return None
    return None  # L3


def _check_fix_approval(c: PolicyContext) -> RuleHit | None:
    a = c.approval
    if not a or a.kind != "fix":
        return RuleHit("P8", DecisionResult.REQUIRE_APPROVAL, "L1: human confirm required", approval_kind="fix")
    if not a.valid:
        return deny("P14", f"approval invalid: {a.reason}")
    if a.subject_hash != c.plan.plan_hash:
        return deny("P14", "approval bound to a different plan_hash")
    if a.verdict == "reject":
        return deny("P8", f"fix rejected by {a.user_id}")
    ttl = c.settings.human_window(c.config.policy.fix_approval_ttl_s)
    if (c.at - a.ts).total_seconds() > max(ttl, 1):
        return deny("P14", "approval expired")
    return None


def p09_concurrency(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind != "remediation.execute" or c.plan is None:
        return None
    spec = c.config.actions.get(c.plan.action)
    if c.service_lock_holder and c.service_lock_holder != c.plan.plan_id:
        return deny("P9", f"service {c.plan.target_service} locked by {c.service_lock_holder}")
    if spec and c.executions_last_hour >= spec.max_per_hour:
        return deny("P9", f"rate limit: {c.executions_last_hour} executions in last hour >= {spec.max_per_hour}")
    if c.consecutive_failures >= c.config.policy.circuit_breaker_failures:
        return deny("P9", f"circuit breaker open: {c.consecutive_failures} consecutive failures")
    return None


def p11_kill_switch(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if c.kill_switch and intent.kind in {"remediation.execute"} | PUBLIC_WRITE:
        return deny("P11", "kill switch engaged")
    return None


def p12_memory(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind not in {"memory.propose", "memory.merge"}:
        return None
    if c.pr_validation_errors:
        return deny("P12", "; ".join(c.pr_validation_errors))
    if intent.kind == "memory.merge":
        a = c.approval
        if not a or a.kind != "memory_merge" or not a.valid or a.verdict != "approve":
            return RuleHit("P12", DecisionResult.REQUIRE_APPROVAL, "memory merge requires human review",
                           approval_kind="memory_merge")
        if a.subject_hash != intent.payload.get("proposal_hash"):
            return deny("P14", "memory approval bound to a different proposal")
    return None


def p13_merge(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind != "incident.merge":
        return None
    a, b = c.incident, c.merge_target
    if not a or not b:
        return deny("P13", "merge target missing")
    if a.environment != b.environment:
        return deny("P13", "different environments")
    if not any(c.config.shares_dependency(x, y) for x in a.services for y in b.services):
        return deny("P13", f"no shared dependency between {a.services} and {b.services}")
    window = c.config.policy.correlation_window_s * max(c.settings.time_scale, 0.01)
    if abs((a.created_at - b.created_at).total_seconds()) > max(window, 60):
        return deny("P13", "outside correlation window")
    return None


def p15_measurable(intent: Intent, c: PolicyContext) -> RuleHit | None:
    if intent.kind != "remediation.execute" or c.plan is None:
        return None
    if not c.metrics_available:
        return deny("P15", "metrics backend unavailable: cannot verify, will not act")
    if c.current_rps is None or c.current_rps < c.plan.verify.min_rps:
        return deny("P15", f"traffic too low to verify (rps={c.current_rps}, min={c.plan.verify.min_rps})")
    return None


RULES = [p11_kill_switch, p01_env, p02_public_service, p03_template_only, p04_no_premature_resolve,
         p05_no_duplicates, p06_public_threshold_and_approval, p07_action_catalog, p15_measurable,
         p09_concurrency, p08_autonomy, p12_memory, p13_merge]


# ---------------------------------------------------------------- helpers


def public_subject_hash(incident: Incident, impact: str) -> str:
    return canonical_hash({"incident": incident.id, "impact": impact, "services": sorted(incident.services)})


def validate_params(schema: dict[str, str], params: dict) -> str | None:
    if set(params) != set(schema):
        return f"params {sorted(params)} do not match schema {sorted(schema)}"
    for name, typ in schema.items():
        v = params[name]
        if typ == "str" and not isinstance(v, str):
            return f"param {name} must be str"
        if typ == "bool" and not isinstance(v, bool):
            return f"param {name} must be bool"
        if typ.startswith("int"):
            if not isinstance(v, int) or isinstance(v, bool):
                return f"param {name} must be int"
            if "[" in typ:
                lo, hi = typ[typ.index("[") + 1: typ.index("]")].split("..")
                if not int(lo) <= v <= int(hi):
                    return f"param {name}={v} out of bounds [{lo}..{hi}]"
    return None


def evaluate(intent: Intent, ctx: PolicyContext) -> Decision:
    hits = [h for rule in RULES if (h := rule(intent, ctx))]
    by = {r: [h for h in hits if h.result == r] for r in DecisionResult}
    if by[DecisionResult.DENY]:
        chosen, result = by[DecisionResult.DENY], DecisionResult.DENY
    elif by[DecisionResult.REQUIRE_APPROVAL]:
        chosen, result = by[DecisionResult.REQUIRE_APPROVAL], DecisionResult.REQUIRE_APPROVAL
    elif by[DecisionResult.DOWNGRADE]:
        chosen, result = by[DecisionResult.DOWNGRADE], DecisionResult.DOWNGRADE
    else:
        chosen, result = [], DecisionResult.ALLOW
    inc = ctx.incident
    return Decision(
        incident_id=intent.incident_id or (inc.id if inc else None),
        trial_id=ctx.settings.trial_id,
        intent=intent.kind,
        result=result,
        rules=[h.rule for h in chosen],
        explain=" | ".join(h.explain for h in chosen),
        approval_kind=next((h.approval_kind for h in chosen if h.approval_kind), None),
        downgrade_to=next((h.downgrade_to for h in chosen if h.downgrade_to), None),
        approval_id=ctx.approval.approval_id if ctx.approval else None,
        inputs_digest=canonical_hash({
            "intent": intent.model_dump(),
            "incident": inc.model_dump(mode="json") if inc else None,
            "plan": ctx.plan.plan_hash if ctx.plan else None,
            "facts": [ctx.metrics_available, ctx.seconds_since_last_event, ctx.slo_healthy, ctx.current_rps,
                      ctx.runbook_match_ok, ctx.effective_autonomy.value],
        }),
    )
