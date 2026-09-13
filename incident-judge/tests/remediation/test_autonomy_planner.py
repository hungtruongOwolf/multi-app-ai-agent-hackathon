from __future__ import annotations

from datetime import timedelta

from judge.core.models import ActionProposal, AutonomyLevel, Outcome, OutcomeResult, now
from judge.remediation.actions.base import REGISTRY
from judge.remediation.autonomy import consecutive_failures, effective_level, executions_last_hour
from judge.remediation.planner import build_plan
from remediation_fakes import make_runbook


def test_registry_has_all_catalog_actions(config):
    assert set(REGISTRY) == set(config.actions)


def test_effective_level_caps(config):
    scale = config.actions["scale_pool"]        # cap L2, reversible
    restart = config.actions["restart_service"]  # cap L2, safe_to_repeat
    rollback = config.actions["rollback_deploy"]  # cap L1, global
    toggle = config.actions["toggle_flag"]       # cap L3, reversible

    assert effective_level(make_runbook(level="L3", cap="L3"), scale, False, False) == AutonomyLevel.L2
    assert effective_level(make_runbook(action="toggle_flag", level="L3", cap="L3"), toggle, False, False) \
        == AutonomyLevel.L3
    assert effective_level(make_runbook(action="toggle_flag", level="L3", cap="L1"), toggle, False, False) \
        == AutonomyLevel.L1
    assert effective_level(make_runbook(action="restart_service", level="L3", cap="L3"), restart, False, False) \
        == AutonomyLevel.L2
    assert effective_level(make_runbook(action="rollback_deploy", level="L3", cap="L3"), rollback, False, False) \
        == AutonomyLevel.L1
    assert effective_level(make_runbook(level="L2", review_required=True), scale, False, False) == AutonomyLevel.L1


def test_effective_level_zero_conditions(config):
    scale = config.actions["scale_pool"]
    rb = make_runbook(level="L2")
    assert effective_level(rb, scale, True, False) == AutonomyLevel.L0
    assert effective_level(rb, scale, False, True) == AutonomyLevel.L0
    assert effective_level(None, scale, False, False) == AutonomyLevel.L0
    assert effective_level(rb, None, False, False) == AutonomyLevel.L0
    assert effective_level(make_runbook(action="toggle_flag"), scale, False, False) == AutonomyLevel.L0


def _executed(store, plan, result, ts):
    store.add_execution(plan=plan, kind="apply", result="{}", decision_id=None, approval_id=None)
    store.add_outcome(Outcome(runbook_id=plan.runbook_id, incident_id=plan.incident_id, plan_id=plan.plan_id,
                              action=plan.action, result=result, ts=ts))


def test_consecutive_failures_and_rate(store, make_plan):
    t = now()
    _executed(store, make_plan(), OutcomeResult.failure, t - timedelta(minutes=50))
    _executed(store, make_plan(), OutcomeResult.success, t - timedelta(minutes=40))
    _executed(store, make_plan(), OutcomeResult.failure, t - timedelta(minutes=30))
    _executed(store, make_plan(), OutcomeResult.inconclusive, t - timedelta(minutes=20))
    _executed(store, make_plan(), OutcomeResult.failure, t - timedelta(minutes=10))
    assert consecutive_failures(store, "checkout") == 2
    assert consecutive_failures(store, "search") == 0
    assert executions_last_hour(store, "checkout", t) == 5
    assert executions_last_hour(store, "checkout", t + timedelta(hours=2)) == 0


def test_planner_runbook_params_win_and_verify_bound(config, incident):
    proposal = ActionProposal(name="scale_pool", params={"size": 50}, target_service="checkout")
    plan = build_plan(incident, proposal, make_runbook(params={"size": 20}), config, AutonomyLevel.L1)
    assert plan.params == {"size": 20} and plan.runbook_id == "db-pool-starved"
    assert plan.verify.conditions and all(c.service == "checkout" for c in plan.verify.conditions)
    assert plan.verify.window_s == 90


def test_planner_hash_changes_with_params(config, incident):
    rb = make_runbook(params={})
    a = build_plan(incident, ActionProposal(name="scale_pool", params={"size": 20}, target_service="checkout"),
                   rb, config, AutonomyLevel.L1)
    b = build_plan(incident, ActionProposal(name="scale_pool", params={"size": 30}, target_service="checkout"),
                   rb, config, AutonomyLevel.L1)
    assert a.plan_hash != b.plan_hash


def test_planner_unknown_action_still_produces_plan(config, incident):
    proposal = ActionProposal(name="rm_rf", params={}, target_service="checkout")
    plan = build_plan(incident, proposal, make_runbook(), config, AutonomyLevel.L1)
    assert plan.action == "rm_rf" and plan.verify.conditions == []
