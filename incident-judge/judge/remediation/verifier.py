"""Post-remediation verification against SLO metrics (not "Sentry went quiet").

window   = max(30, verify.window_s * time_scale) + max(condition window_s)   (settle: windows look backwards)
interval = max(2, base / 6)
pass         : every condition holds on the final 2 consecutive samples, rps >= min_rps, metrics available
inconclusive : metrics unavailable / traffic too low / a condition not measurable / no conditions
fail         : otherwise
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Awaitable, Callable

from judge.core.models import Plan, VerifyResult
from judge.signals.metrics import MetricsBackend, evaluate

RPS_WINDOW_S = 30
FINAL_SAMPLES = 2


def verify_window(plan: Plan, time_scale: float) -> tuple[float, float, int]:
    base = max(30.0, plan.verify.window_s * time_scale)
    interval = max(2.0, base / 6)
    # Metric windows are real seconds and look backwards: until one full condition window has elapsed after the
    # change, samples still contain pre-fix data. Extend the window so the final samples only see post-fix data.
    settle = max((c.window_s for c in plan.verify.conditions), default=0)
    window = base + settle
    n = max(FINAL_SAMPLES, math.ceil(window / interval))
    return window, interval, n


def take_sample(plan: Plan, metrics: MetricsBackend) -> dict:
    available = bool(metrics.available())
    rps = metrics.value("rps", plan.target_service, RPS_WINDOW_S) if available else None
    conds = []
    for cond in plan.verify.conditions:
        bound = cond.bind(plan.target_service)
        holds, observed = evaluate(metrics, bound) if available else (None, None)
        conds.append({"metric": bound.metric, "service": bound.service, "route": bound.route, "op": bound.op,
                      "value": bound.value, "observed": observed, "holds": holds})
    return {"t": datetime.now(UTC).isoformat(), "available": available, "rps": rps, "conditions": conds}


def classify(plan: Plan, samples: list[dict]) -> VerifyResult:
    final = samples[-FINAL_SAMPLES:]
    if len(final) < FINAL_SAMPLES or not plan.verify.conditions:
        return VerifyResult.inconclusive
    for s in final:
        if not s["available"] or s["rps"] is None or s["rps"] < plan.verify.min_rps:
            return VerifyResult.inconclusive
        if any(c["holds"] is None for c in s["conditions"]):
            return VerifyResult.inconclusive
    if all(c["holds"] for s in final for c in s["conditions"]):
        return VerifyResult.pass_
    return VerifyResult.fail


async def verify(plan: Plan, metrics: MetricsBackend, settings,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 on_sample: Callable[[Plan, list[dict], int, float], Awaitable[None]] | None = None,
                 ) -> tuple[VerifyResult, list[dict]]:
    _, interval, n = verify_window(plan, settings.time_scale)
    samples: list[dict] = []
    for _ in range(n):
        await sleep(interval)
        samples.append(take_sample(plan, metrics))
        if on_sample is not None:
            try:
                await on_sample(plan, samples, n, interval)
            except Exception:  # narration must never affect the verdict
                pass
    return classify(plan, samples), samples
