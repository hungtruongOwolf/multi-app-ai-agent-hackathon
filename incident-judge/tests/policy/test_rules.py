"""One or more tests per policy rule in judge/policy/engine.py.

P10 (LLM cannot change autonomy/stats) is not an engine rule: it is enforced structurally by
judge.memory.stats (code-computed) and judge.memory.pr_validator; see tests there."""

from __future__ import annotations

from datetime import timedelta

import pytest

from judge.core.models import (
    Approval,
    AutonomyLevel,
    CustomerImpact,
    DecisionResult,
    Env,
    Incident,
    Intent,
    Plan,
    Severity,
    VerifySpec,
    now,
)
from judge.policy.engine import PolicyContext, evaluate, public_subject_hash, validate_params
from judge.settings import Config, Settings

A = DecisionResult


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def settings(tmp_path):
    return Settings(var_dir=tmp_path, time_scale=1.0)


def inc(**kw):
    base = dict(incident_key="fp", environment=Env.production, services=["checkout"], severity=Severity.SEV3,
                customer_impact=CustomerImpact.degraded, customer_visible=True)
    base.update(kw)
    return Incident(**base)


def ctx(settings, config, **kw):
    return PolicyContext(settings=settings, config=config, **kw)


def public_intent(impact="degraded", **extra):
    return Intent(kind="instatus.create_incident", payload={"phase": "investigating", "impact": impact, **extra})


def plan_for(i: Incident, action="scale_pool", params=None, service="checkout", config=None):
    spec = config.actions[action] if config and action in config.actions else None
    return Plan(incident_id=i.id, runbook_id="db-pool-starved", action=action,
                params=params if params is not None else {"size": 20}, target_service=service,
                verify=VerifySpec(conditions=list(spec.verify.conditions) if spec else [], min_rps=5),
                autonomy_level=AutonomyLevel.L1)


def fix_approval(i, p, user="U_ONCALL_1", valid=True, verdict="approve", subject=None, age_s=0):
    return Approval(incident_id=i.id, kind="fix", subject_hash=subject or p.plan_hash, user_id=user,
                    verdict=verdict, via="text", valid=valid, reason="" if valid else "not allowlisted",
                    ts=now() - timedelta(seconds=age_s))


def exec_ctx(settings, config, i, p, **kw):
    base = dict(incident=i, plan=p, runbook_id="db-pool-starved", runbook_action=p.action, runbook_match_ok=True,
                runbook_merged=True, effective_autonomy=AutonomyLevel.L3, metrics_available=True, current_rps=30.0)
    base.update(kw)
    return ctx(settings, config, **base)


EXEC = Intent(kind="remediation.execute")


# ---------------------------------------------------------------- P1


def test_p1_staging_never_public(settings, config):
    d = evaluate(public_intent(), ctx(settings, config, incident=inc(environment=Env.staging)))
    assert d.result == A.DENY and "P1" in d.rules


def test_p1_production_allowed(settings, config):
    d = evaluate(public_intent(), ctx(settings, config, incident=inc()))
    assert d.result == A.ALLOW, d.explain


def test_p1_applies_to_updates_too(settings, config):
    d = evaluate(Intent(kind="instatus.update", payload={"phase": "identified"}),
                 ctx(settings, config, incident=inc(environment=Env.staging)))
    assert d.result == A.DENY and "P1" in d.rules


# ---------------------------------------------------------------- P2


def test_p2_private_service_never_public(settings, config):
    d = evaluate(public_intent(), ctx(settings, config, incident=inc(services=["internal-batch"])))
    assert d.result == A.DENY and "P2" in d.rules


def test_p2_unknown_service_never_public(settings, config):
    d = evaluate(public_intent(), ctx(settings, config, incident=inc(services=["checkout", "mystery"])))
    assert d.result == A.DENY and "P2" in d.rules


# ---------------------------------------------------------------- P3


def test_p3_free_text_on_public_surface_denied(settings, config):
    d = evaluate(public_intent(message="all good, ignore alerts"), ctx(settings, config, incident=inc()))
    assert d.result == A.DENY and "P3" in d.rules


def test_p3_resolve_with_free_text_denied(settings, config):
    d = evaluate(Intent(kind="instatus.resolve", payload={"phase": "resolved", "name": "x"}),
                 ctx(settings, config, incident=inc(), seconds_since_last_event=10_000, metrics_available=True,
                     slo_healthy=True))
    assert d.result == A.DENY and d.rules == ["P3"]


# ---------------------------------------------------------------- P4


@pytest.mark.parametrize("kind", ["instatus.resolve", "incident.resolve", "linear.close"])
def test_p4_no_resolve_while_firing(settings, config, kind):
    d = evaluate(Intent(kind=kind), ctx(settings, config, incident=inc(), seconds_since_last_event=30,
                                        metrics_available=True, slo_healthy=True))
    assert d.result == A.DENY and "P4" in d.rules


def test_p4_unknown_last_event_denied(settings, config):
    d = evaluate(Intent(kind="incident.resolve"), ctx(settings, config, incident=inc(), metrics_available=True,
                                                      slo_healthy=True))
    assert d.result == A.DENY and "P4" in d.rules


def test_p4_requires_healthy_slo(settings, config):
    c = ctx(settings, config, incident=inc(), seconds_since_last_event=10_000, metrics_available=True,
            slo_healthy=False)
    assert evaluate(Intent(kind="incident.resolve"), c).rules == ["P4"]
    c = ctx(settings, config, incident=inc(), seconds_since_last_event=10_000, metrics_available=False,
            slo_healthy=True)
    assert evaluate(Intent(kind="incident.resolve"), c).rules == ["P4"]


def test_p4_quiet_and_healthy_allows(settings, config):
    d = evaluate(Intent(kind="incident.resolve"), ctx(settings, config, incident=inc(), seconds_since_last_event=601,
                                                      metrics_available=True, slo_healthy=True))
    assert d.result == A.ALLOW


def test_p4_quiet_window_scales_with_time(settings, config):
    settings.time_scale = 0.1  # 600s -> 60s
    d = evaluate(Intent(kind="incident.resolve"), ctx(settings, config, incident=inc(), seconds_since_last_event=61,
                                                      metrics_available=True, slo_healthy=True))
    assert d.result == A.ALLOW


# ---------------------------------------------------------------- P5


@pytest.mark.parametrize("kind,attr", [("linear.create_issue", "linear_issue_id"),
                                       ("instatus.create_incident", "instatus_incident_id"),
                                       ("slack.create_channel", "slack_channel_id")])
def test_p5_no_second_record(settings, config, kind, attr):
    i = inc(**{attr: "existing"})
    d = evaluate(Intent(kind=kind, payload={"phase": "investigating", "impact": "degraded"}
                        if kind.startswith("instatus") else {}), ctx(settings, config, incident=i))
    assert d.result == A.DENY and "P5" in d.rules


def test_p5_first_record_allowed(settings, config):
    assert evaluate(Intent(kind="linear.create_issue"), ctx(settings, config, incident=inc())).result == A.ALLOW


# ---------------------------------------------------------------- P6


def test_p6_below_public_threshold(settings, config):
    d = evaluate(public_intent(impact="none"), ctx(settings, config, incident=inc()))
    assert d.result == A.DENY and "P6" in d.rules


def test_p6_not_customer_visible(settings, config):
    d = evaluate(public_intent(), ctx(settings, config, incident=inc(customer_visible=False)))
    assert d.result == A.DENY and "P6" in d.rules


def test_p6_major_needs_approval(settings, config):
    d = evaluate(public_intent(impact="major_outage"), ctx(settings, config, incident=inc()))
    assert d.result == A.REQUIRE_APPROVAL and d.approval_kind == "public_post"


def test_p6_sev1_needs_approval_even_if_partial(settings, config):
    d = evaluate(public_intent(impact="partial_outage"), ctx(settings, config, incident=inc(severity=Severity.SEV1)))
    assert d.result == A.REQUIRE_APPROVAL


def _public_approval(i, impact, **kw):
    base = dict(incident_id=i.id, kind="public_post", subject_hash=public_subject_hash(i, impact),
                user_id="U_ONCALL_1", verdict="approve", via="text", valid=True)
    base.update(kw)
    return Approval(**base)


def test_p6_valid_approval_allows(settings, config):
    i = inc(severity=Severity.SEV1)
    d = evaluate(public_intent(impact="major_outage"),
                 ctx(settings, config, incident=i, approval=_public_approval(i, "major_outage")))
    assert d.result == A.ALLOW, d.explain


def test_p6_rejected_denies(settings, config):
    i = inc(severity=Severity.SEV1)
    d = evaluate(public_intent(impact="major_outage"),
                 ctx(settings, config, incident=i, approval=_public_approval(i, "major_outage", verdict="reject")))
    assert d.result == A.DENY and "P6" in d.rules


def test_p6_approval_for_other_impact_does_not_count(settings, config):
    i = inc(severity=Severity.SEV1)
    d = evaluate(public_intent(impact="major_outage"),
                 ctx(settings, config, incident=i, approval=_public_approval(i, "partial_outage")))
    assert d.result == A.REQUIRE_APPROVAL


def test_p6_expired_approval(settings, config):
    i = inc(severity=Severity.SEV1)
    old = _public_approval(i, "major_outage", ts=now() - timedelta(hours=2))
    d = evaluate(public_intent(impact="major_outage"), ctx(settings, config, incident=i, approval=old))
    assert d.result == A.REQUIRE_APPROVAL and "expired" in d.explain


def test_p6_invalid_approval_does_not_count(settings, config):
    i = inc(severity=Severity.SEV1)
    d = evaluate(public_intent(impact="major_outage"),
                 ctx(settings, config, incident=i, approval=_public_approval(i, "major_outage", valid=False,
                                                                             user_id="U_INTRUDER")))
    assert d.result == A.REQUIRE_APPROVAL


# ---------------------------------------------------------------- P7


def test_p7_happy_path(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    assert evaluate(EXEC, exec_ctx(settings, config, i, p)).result == A.ALLOW


def test_p7_no_plan(settings, config):
    d = evaluate(EXEC, ctx(settings, config, incident=inc()))
    assert d.result == A.DENY and "P7" in d.rules


def test_p7_action_not_in_catalog(settings, config):
    i = inc()
    p = plan_for(i, action="drop_database", params={}, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, runbook_action="drop_database"))
    assert d.result == A.DENY and "P7" in d.rules and "not in catalog" in d.explain


def test_p7_unknown_or_foreign_target(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, service="nope", config=config)))
    assert "P7" in d.rules and "unknown target" in d.explain
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, service="catalog", config=config)))
    assert "P7" in d.rules and "not part of incident" in d.explain


def test_p7_action_must_match_runbook(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, runbook_action="restart_service"))
    assert "P7" in d.rules
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, runbook_id=None))
    assert "P7" in d.rules


def test_p7_unmerged_runbook(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config), runbook_merged=False))
    assert "P7" in d.rules and "not merged" in d.explain


@pytest.mark.parametrize("match", [False, None])
def test_p7_match_conditions_must_hold(settings, config, match):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config), runbook_match_ok=match))
    assert d.result == A.DENY and "P7" in d.rules


def test_p7_params_validated(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, params={"size": 500}, config=config)))
    assert "P7" in d.rules and "out of bounds" in d.explain


def test_validate_params_types():
    assert validate_params({"flag": "str", "value": "bool"}, {"flag": "x", "value": True}) is None
    assert validate_params({"flag": "str", "value": "bool"}, {"flag": "x", "value": 1})
    assert validate_params({"size": "int[5..50]"}, {"size": True})
    assert validate_params({"size": "int[5..50]"}, {"size": 20, "extra": 1})


# ---------------------------------------------------------------- P8


def test_p8_l0_suggest_only(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config),
                                effective_autonomy=AutonomyLevel.L0))
    assert d.result == A.DENY and "P8" in d.rules


def test_p8_l1_requires_confirm(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config),
                                effective_autonomy=AutonomyLevel.L1))
    assert d.result == A.REQUIRE_APPROVAL and d.approval_kind == "fix"


def test_p8_l1_with_valid_approval(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L1,
                                approval=fix_approval(i, p)))
    assert d.result == A.ALLOW and d.approval_id


def test_p8_l1_rejected(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L1,
                                approval=fix_approval(i, p, verdict="reject")))
    assert d.result == A.DENY and "P8" in d.rules


def test_p8_l2_waits_for_veto_window(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L2))
    assert d.result == A.REQUIRE_APPROVAL and d.approval_kind == "veto"
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L2,
                                veto_window_elapsed=True))
    assert d.result == A.ALLOW


def test_p8_l2_vetoed(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L2,
                                veto_window_elapsed=True, vetoed=True))
    assert d.result == A.DENY and "P8" in d.rules


def test_p8_l2_valid_reject_approval_is_a_veto(settings, config):
    """Regression: a valid reject bound to this plan must veto even if the caller did not set vetoed."""
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L2,
                                veto_window_elapsed=True, approval=fix_approval(i, p, verdict="reject")))
    assert d.result == A.DENY and "P8" in d.rules


def test_p8_l2_explicit_approve_skips_window(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L2,
                                approval=fix_approval(i, p)))
    assert d.result == A.ALLOW


def test_p8_l3_runs(settings, config):
    i = inc()
    assert evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config))).result == A.ALLOW


# ---------------------------------------------------------------- P9


def test_p9_service_locked_by_other_plan(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, service_lock_holder="plan_other"))
    assert d.result == A.DENY and "P9" in d.rules
    assert evaluate(EXEC, exec_ctx(settings, config, i, p, service_lock_holder=p.plan_id)).result == A.ALLOW


def test_p9_rate_limit(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config), executions_last_hour=2))
    assert d.result == A.DENY and "rate limit" in d.explain


def test_p9_circuit_breaker(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config), consecutive_failures=2))
    assert d.result == A.DENY and "circuit breaker" in d.explain


# ---------------------------------------------------------------- P11


def test_p11_kill_switch_blocks_execute_and_public(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config), kill_switch=True))
    assert d.result == A.DENY and "P11" in d.rules
    d = evaluate(public_intent(), ctx(settings, config, incident=i, kill_switch=True))
    assert d.result == A.DENY and "P11" in d.rules


def test_p11_kill_switch_allows_internal_and_rollback(settings, config):
    i = inc()
    assert evaluate(Intent(kind="linear.create_issue"), ctx(settings, config, incident=i,
                                                            kill_switch=True)).result == A.ALLOW
    assert evaluate(Intent(kind="remediation.rollback"), ctx(settings, config, incident=i,
                                                             kill_switch=True)).result == A.ALLOW


# ---------------------------------------------------------------- P12


def test_p12_invalid_proposal_denied(settings, config):
    d = evaluate(Intent(kind="memory.propose"), ctx(settings, config, pr_validation_errors=["stats tampered"]))
    assert d.result == A.DENY and "P12" in d.rules


def test_p12_merge_needs_review(settings, config):
    d = evaluate(Intent(kind="memory.merge", payload={"proposal_hash": "abc"}), ctx(settings, config))
    assert d.result == A.REQUIRE_APPROVAL and d.approval_kind == "memory_merge"


def test_p12_merge_with_approval(settings, config):
    a = Approval(incident_id="-", kind="memory_merge", subject_hash="abc", user_id="U_ONCALL_1", verdict="approve",
                 via="cli", valid=True)
    d = evaluate(Intent(kind="memory.merge", payload={"proposal_hash": "abc"}), ctx(settings, config, approval=a))
    assert d.result == A.ALLOW


def test_p12_propose_clean_allowed(settings, config):
    assert evaluate(Intent(kind="memory.propose"), ctx(settings, config)).result == A.ALLOW


# ---------------------------------------------------------------- P13


def test_p13_merge_shared_dependency(settings, config):
    a, b = inc(services=["checkout"]), inc(services=["search"])
    assert evaluate(Intent(kind="incident.merge"), ctx(settings, config, incident=a, merge_target=b)).result == A.ALLOW


def test_p13_merge_denials(settings, config):
    a = inc(services=["checkout"])
    assert "P13" in evaluate(Intent(kind="incident.merge"), ctx(settings, config, incident=a)).rules
    b = inc(services=["search"], environment=Env.staging)
    assert "different environments" in evaluate(Intent(kind="incident.merge"),
                                                 ctx(settings, config, incident=a, merge_target=b)).explain
    c = inc(services=["search"], created_at=a.created_at - timedelta(hours=1))
    assert "window" in evaluate(Intent(kind="incident.merge"),
                                ctx(settings, config, incident=a, merge_target=c)).explain


def test_p13_merge_no_shared_dependency(settings, config, tmp_path):
    config.catalog["search"].depends_on = ["elastic"]
    a, b = inc(services=["checkout"]), inc(services=["search"])
    d = evaluate(Intent(kind="incident.merge"), ctx(settings, config, incident=a, merge_target=b))
    assert d.result == A.DENY and "no shared dependency" in d.explain


# ---------------------------------------------------------------- P14


def test_p14_invalid_fix_approval(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L1,
                                approval=fix_approval(i, p, user="U_INTRUDER", valid=False)))
    assert d.result == A.DENY and "P14" in d.rules


def test_p14_approval_for_other_plan(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L1,
                                approval=fix_approval(i, p, subject="0" * 16)))
    assert d.result == A.DENY and "P14" in d.rules


def test_p14_expired_fix_approval(settings, config):
    i = inc()
    p = plan_for(i, config=config)
    d = evaluate(EXEC, exec_ctx(settings, config, i, p, effective_autonomy=AutonomyLevel.L1,
                                approval=fix_approval(i, p, age_s=3600)))
    assert d.result == A.DENY and "P14" in d.rules and "expired" in d.explain


def test_p14_memory_merge_approval_bound_to_proposal(settings, config):
    a = Approval(incident_id="-", kind="memory_merge", subject_hash="other", user_id="U_ONCALL_1",
                 verdict="approve", via="cli", valid=True)
    d = evaluate(Intent(kind="memory.merge", payload={"proposal_hash": "abc"}), ctx(settings, config, approval=a))
    assert d.result == A.DENY and "P14" in d.rules


# ---------------------------------------------------------------- P15


def test_p15_metrics_unavailable(settings, config):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config), metrics_available=False))
    assert d.result == A.DENY and "P15" in d.rules


@pytest.mark.parametrize("rps", [None, 0.0, 4.9])
def test_p15_low_traffic(settings, config, rps):
    i = inc()
    d = evaluate(EXEC, exec_ctx(settings, config, i, plan_for(i, config=config), current_rps=rps))
    assert d.result == A.DENY and "P15" in d.rules


# ---------------------------------------------------------------- combination


def test_deny_wins_over_approval_and_records_all_denials(settings, config):
    i = inc(environment=Env.staging, services=["internal-batch"], severity=Severity.SEV1)
    d = evaluate(public_intent(impact="major_outage"), ctx(settings, config, incident=i))
    assert d.result == A.DENY and {"P1", "P2"} <= set(d.rules)
    assert d.inputs_digest and d.incident_id == i.id


def test_diagnosis_and_human_plans_never_run_without_approval():
    """Plans that don't come from a runbook skip the runbook checks but always need an explicit click (P8)."""
    from judge.core.models import AutonomyLevel, Env, Incident, Intent, Plan, VerifySpec
    from judge.policy.engine import PolicyContext, evaluate
    from judge.settings import Config, Settings

    cfg, s = Config(), Settings.from_env()
    inc = Incident(incident_key="k", environment=Env.production, services=["checkout"])
    for source in ("diagnosis", "human"):
        plan = Plan(incident_id=inc.id, runbook_id=None, action="scale_pool", params={"size": 20},
                    target_service="checkout", verify=VerifySpec(conditions=[]), autonomy_level=AutonomyLevel.L3,
                    source=source)
        ctx = PolicyContext(settings=s, config=cfg, incident=inc, plan=plan, metrics_available=True, current_rps=30,
                            effective_autonomy=AutonomyLevel.L3)
        d = evaluate(Intent(kind="remediation.execute", incident_id=inc.id), ctx)
        assert d.result.value == "REQUIRE_APPROVAL" and "P8" in d.rules, (source, d)
        bad = plan.model_copy(update={"params": {"size": 500}})
        ctx.plan = bad
        assert "P7" in evaluate(Intent(kind="remediation.execute", incident_id=inc.id), ctx).rules
