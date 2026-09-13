from __future__ import annotations

import pytest

from judge.core.models import Decision, DecisionResult, OutcomeResult, VerifyResult
from judge.core.outbox import PolicyDenied
from judge.remediation.runner import RemediationRunner
from remediation_fakes import FakeControl, FakeMetrics, SimulatedCrash, Sleeps

HEALTHY = {"error_rate": 0.0, "pool_wait_p95": 0.01, "latency_p95": 0.1, "rps": 30.0}
BROKEN = {"error_rate": 0.4, "pool_wait_p95": 0.9, "latency_p95": 2.0, "rps": 30.0}


def runner(store, config, settings, control, metrics, sleeps=None):
    return RemediationRunner(store, config, settings, control, metrics, sleep=sleeps or Sleeps())


async def test_success_persists_prev_state_before_apply(store, config, settings, make_plan, allow):
    control = FakeControl()
    plan = make_plan()
    seen = {}

    def on_apply():
        saved, status = store.get_plan(plan.plan_id)
        seen["status"], seen["prev"] = status, saved.prev_state

    control.on_apply = on_apply
    res = await runner(store, config, settings, control, FakeMetrics(HEALTHY)).execute(plan, allow, "apr_1")

    assert seen == {"status": "applying", "prev": {"pool_size": 2}}
    assert res.applied and res.verify == VerifyResult.pass_ and res.outcome == OutcomeResult.success
    assert res.status == "done" and store.get_plan(plan.plan_id)[1] == "done"
    assert control.configs["checkout"]["pool_size"] == 20
    execs = store.executions(service="checkout")
    assert [e["kind"] for e in execs] == ["apply"]
    assert execs[0]["decision_id"] == allow.decision_id and execs[0]["approval_id"] == "apr_1"
    [out] = store.outcomes("db-pool-starved")
    assert out.result == OutcomeResult.success and out.trial_id == "t1"
    assert store.lock_holder("service:checkout") is None


async def test_failure_rolls_back_and_records_failure(store, config, settings, make_plan, allow):
    control = FakeControl()
    plan = make_plan()
    res = await runner(store, config, settings, control, FakeMetrics(BROKEN)).execute(plan, allow, None)

    assert res.verify == VerifyResult.fail and res.rolled_back and res.outcome == OutcomeResult.failure
    assert control.configs["checkout"]["pool_size"] == 2  # restored
    assert [e["kind"] for e in store.executions(service="checkout")] == ["apply", "rollback"]
    assert store.get_plan(plan.plan_id)[1] == "rolled_back"
    rb = [d for d in store.decisions() if d.intent == "remediation.rollback"]
    assert rb and rb[0].result == DecisionResult.ALLOW
    assert store.executions(service="checkout")[1]["decision_id"] == rb[0].decision_id
    assert store.lock_holder("service:checkout") is None


async def test_inconclusive_rolls_back_and_records_inconclusive(store, config, settings, make_plan, allow):
    control = FakeControl()
    res = await runner(store, config, settings, control, FakeMetrics({**HEALTHY, "rps": 0.0})).execute(
        make_plan(), allow, None)
    assert res.verify == VerifyResult.inconclusive and res.rolled_back
    assert res.outcome == OutcomeResult.inconclusive
    assert control.configs["checkout"]["pool_size"] == 2


async def test_rollback_allowed_under_kill_switch(store, config, settings, make_plan, allow):
    store.put_kv("kill_switch", True)
    control = FakeControl()
    res = await runner(store, config, settings, control, FakeMetrics(BROKEN)).execute(make_plan(), allow, None)
    assert res.rolled_back and control.configs["checkout"]["pool_size"] == 2


async def test_irreversible_action_fails_without_rollback(store, config, settings, make_plan, allow):
    control = FakeControl()
    plan = make_plan(action="restart_service", params={})
    res = await runner(store, config, settings, control, FakeMetrics(BROKEN)).execute(plan, allow, None)
    assert res.verify == VerifyResult.fail and not res.rolled_back and res.status == "failed"
    assert res.outcome == OutcomeResult.failure
    assert [c[0] for c in control.calls] == ["restart"]


async def test_resume_after_crash_between_apply_and_verify_does_not_reapply(store, config, settings, make_plan,
                                                                             allow):
    control = FakeControl()
    plan = make_plan()
    with pytest.raises(SimulatedCrash):
        await runner(store, config, settings, control, FakeMetrics(HEALTHY), Sleeps(crash_at=1)).execute(
            plan, allow, "apr_1")
    saved, status = store.get_plan(plan.plan_id)
    assert status == "verifying" and saved.prev_state == {"pool_size": 2}
    assert store.outcomes() == []

    # a fresh process resumes from the store
    res = await runner(store, config, settings, control, FakeMetrics(HEALTHY)).resume(saved, status)
    assert res.status == "done" and res.outcome == OutcomeResult.success
    assert [c for c in control.calls if c[0] == "set_pool_size"] == [("set_pool_size", "checkout", 20)]
    assert len([e for e in store.executions() if e["kind"] == "apply"]) == 1


async def test_resume_from_applying_with_recorded_apply_skips_apply(store, config, settings, make_plan, allow):
    control = FakeControl()
    plan = make_plan()
    plan.prev_state = {"pool_size": 2}
    store.save_plan(plan, "applying")
    store.add_execution(plan=plan, kind="apply", result="{}", decision_id=allow.decision_id, approval_id=None)
    res = await runner(store, config, settings, control, FakeMetrics(HEALTHY)).resume(plan, "applying")
    assert res.status == "done" and control.calls == []


async def test_resume_rollback_after_crash_during_revert(store, config, settings, make_plan, allow):
    control = FakeControl()
    plan = make_plan()
    plan.prev_state = {"pool_size": 2}
    control.configs["checkout"]["pool_size"] = 20
    store.save_plan(plan, "reverting")
    store.add_execution(plan=plan, kind="apply", result="{}", decision_id=allow.decision_id, approval_id=None)
    store.put_kv(f"plan_verify:{plan.plan_id}", "fail")
    res = await runner(store, config, settings, control, FakeMetrics(BROKEN)).resume(plan, "reverting")
    assert res.status == "rolled_back" and res.outcome == OutcomeResult.failure
    assert control.configs["checkout"]["pool_size"] == 2


async def test_execute_twice_is_idempotent(store, config, settings, make_plan, allow):
    control = FakeControl()
    plan = make_plan()
    r = runner(store, config, settings, control, FakeMetrics(HEALTHY))
    await r.execute(plan, allow, None)
    again = await r.execute(plan, allow, None)
    assert again.status == "done" and again.outcome == OutcomeResult.success
    assert len(control.calls) == 1


async def test_lock_contention_does_nothing(store, config, settings, make_plan, allow):
    control = FakeControl()
    assert store.acquire_lock("service:checkout", "plan_other", 60)
    res = await runner(store, config, settings, control, FakeMetrics(HEALTHY)).execute(make_plan(), allow, None)
    assert not res.applied and "locked by plan_other" in res.error
    assert control.calls == [] and store.outcomes() == []
    assert store.lock_holder("service:checkout") == "plan_other"


async def test_denied_decision_raises(store, config, settings, make_plan):
    denied = Decision(intent="remediation.execute", result=DecisionResult.DENY, rules=["P7"])
    with pytest.raises(PolicyDenied):
        await runner(store, config, settings, FakeControl(), FakeMetrics(HEALTHY)).execute(make_plan(), denied, None)


async def test_decision_for_other_intent_raises(store, config, settings, make_plan):
    wrong = Decision(intent="linear.create_issue", result=DecisionResult.ALLOW)
    with pytest.raises(PolicyDenied):
        await runner(store, config, settings, FakeControl(), FakeMetrics(HEALTHY)).execute(make_plan(), wrong, None)


async def test_precondition_failure_does_not_apply(store, config, settings, make_plan, allow):
    control = FakeControl()
    plan = make_plan(action="rollback_deploy", params={"to_version": "9.9.9"})
    res = await runner(store, config, settings, control, FakeMetrics(HEALTHY)).execute(plan, allow, None)
    assert not res.applied and res.status == "failed" and "version_known" in res.error
    assert control.calls == [] and store.outcomes() == []
    assert store.lock_holder("service:checkout") is None


async def test_unknown_service_precondition(store, config, settings, make_plan, allow):
    plan = make_plan(action="toggle_flag", params={"flag": "payment_v2", "value": False}, service="search")
    res = await runner(store, config, settings, FakeControl(), FakeMetrics(HEALTHY)).execute(plan, allow, None)
    assert res.status == "failed" and "service_known" in res.error


async def test_toggle_flag_success_and_revert_values(store, config, settings, make_plan, allow):
    control = FakeControl()
    plan = make_plan(action="toggle_flag", params={"flag": "payment_v2", "value": False})
    res = await runner(store, config, settings, control, FakeMetrics(BROKEN)).execute(plan, allow, None)
    assert res.rolled_back and control.configs["checkout"]["flags"]["payment_v2"] is True
    assert control.calls == [("set_flag", "checkout", "payment_v2", False), ("set_flag", "checkout", "payment_v2", True)]


async def test_execute_from_veto_window_status_applies(store, config, settings, make_plan, allow):
    """Regression (R5): a plan saved as 'veto_window' by the agent must execute, not be treated as terminal."""
    from judge.remediation.runner import RemediationRunner
    from remediation_fakes import FakeControl, FakeMetrics, Sleeps

    plan = make_plan()
    store.save_plan(plan, "veto_window")
    runner = RemediationRunner(store, config, settings, FakeControl(),
                               FakeMetrics({"error_rate": 0.0, "rps": 30.0, "pool_wait_p95": 0.01}), sleep=Sleeps())
    res = await runner.execute(plan, allow, None)
    assert res.applied
