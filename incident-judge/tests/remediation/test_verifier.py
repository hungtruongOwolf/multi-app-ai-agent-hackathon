from __future__ import annotations

from judge.core.models import VerifyResult, VerifySpec
from judge.remediation.verifier import verify, verify_window
from remediation_fakes import FakeMetrics, Sleeps

HEALTHY = {"error_rate": 0.0, "pool_wait_p95": 0.01, "rps": 30.0}


async def test_pass_samples_whole_window(make_plan, settings):
    plan = make_plan()
    sleeps = Sleeps()
    result, samples = await verify(plan, FakeMetrics(HEALTHY), settings, sleep=sleeps)
    window, interval, n = verify_window(plan, settings.time_scale)
    assert result == VerifyResult.pass_
    settle = max(c.window_s for c in plan.verify.conditions)
    assert (window, interval) == (90 + settle, 15)
    assert n == -(-(90 + settle) // 15)
    assert sleeps.calls == [15] * n and len(samples) == n
    assert all(c["holds"] for c in samples[-1]["conditions"])


async def test_window_floor_with_time_scale(make_plan, settings):
    settings.time_scale = 0.1
    plan = make_plan()
    window, interval, n = verify_window(plan, settings.time_scale)
    settle = max(c.window_s for c in plan.verify.conditions)
    assert window == 30 + settle and interval == 5 and n == -(-(30 + settle) // 5)


async def test_final_samples_exclude_pre_fix_window(make_plan, settings):
    """Regression: the fix landed but the 30s metric window still held pre-fix errors at the end of a 30s verify."""
    plan = make_plan()
    _, _, n = verify_window(plan, 0.1)
    settings.time_scale = 0.1
    errors = [0.29, 0.22, 0.19, 0.14, 0.08, 0.003] + [0.0] * max(0, n - 6)
    result, _ = await verify(plan, FakeMetrics({**HEALTHY, "error_rate": errors, "pool_wait_p95": 0.01}), settings,
                             sleep=Sleeps())
    assert result == VerifyResult.pass_


async def test_fail_when_errors_persist(make_plan, settings):
    result, _ = await verify(make_plan(), FakeMetrics({**HEALTHY, "error_rate": 0.4}), settings, sleep=Sleeps())
    assert result == VerifyResult.fail


async def test_recovers_late_passes_on_final_two(make_plan, settings):
    m = FakeMetrics({**HEALTHY, "error_rate": [0.5, 0.5, 0.5, 0.3, 0.0, 0.0]})
    result, _ = await verify(make_plan(), m, settings, sleep=Sleeps())
    assert result == VerifyResult.pass_


async def test_relapse_on_last_sample_fails(make_plan, settings):
    m = FakeMetrics({**HEALTHY, "error_rate": [0.0, 0.0, 0.0, 0.0, 0.0, 0.3]})
    result, _ = await verify(make_plan(), m, settings, sleep=Sleeps())
    assert result == VerifyResult.fail


async def test_inconclusive_low_traffic(make_plan, settings):
    result, _ = await verify(make_plan(), FakeMetrics({**HEALTHY, "rps": 1.0}), settings, sleep=Sleeps())
    assert result == VerifyResult.inconclusive


async def test_inconclusive_metrics_unavailable(make_plan, settings):
    result, _ = await verify(make_plan(), FakeMetrics(HEALTHY, available=False), settings, sleep=Sleeps())
    assert result == VerifyResult.inconclusive


async def test_inconclusive_condition_not_measurable(make_plan, settings):
    result, _ = await verify(make_plan(), FakeMetrics({**HEALTHY, "pool_wait_p95": None}), settings, sleep=Sleeps())
    assert result == VerifyResult.inconclusive


async def test_inconclusive_without_conditions(make_plan, settings):
    plan = make_plan().model_copy(update={"verify": VerifySpec(conditions=[])})
    result, _ = await verify(plan, FakeMetrics(HEALTHY), settings, sleep=Sleeps())
    assert result == VerifyResult.inconclusive
