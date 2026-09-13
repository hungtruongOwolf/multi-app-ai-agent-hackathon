"""Runtime view of the autonomy ladder.

The runbook's stored level is computed by code from outcomes (judge.memory.stats). Here we apply
runtime and structural caps defensively: kill switch, circuit breaker, review_required, catalog cap,
runbook cap, reversibility and blast radius. Caps only ever lower the level."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from judge.core.models import ActionSpec, AutonomyLevel, OutcomeResult
from judge.core.store import Store

if TYPE_CHECKING:
    from judge.memory.schema import Runbook


def _min(*levels: AutonomyLevel) -> AutonomyLevel:
    return min(levels, key=lambda lv: lv.rank)


def effective_level(runbook: "Runbook | None", action_spec: ActionSpec | None, kill_switch: bool,
                    breaker_open: bool) -> AutonomyLevel:
    if kill_switch or breaker_open or runbook is None or action_spec is None:
        return AutonomyLevel.L0
    fm = runbook.frontmatter
    if fm.action is None or fm.action.name != action_spec.name:
        return AutonomyLevel.L0
    auto = fm.autonomy
    level = _min(AutonomyLevel(auto.level), AutonomyLevel(auto.cap), action_spec.autonomy_cap)
    if auto.review_required:
        level = _min(level, AutonomyLevel.L1)
    if not action_spec.reversible and not action_spec.safe_to_repeat:
        level = _min(level, AutonomyLevel.L1)  # no inverse -> never beyond one-click
    if level == AutonomyLevel.L3 and (not action_spec.reversible or action_spec.blast_radius == "global"):
        level = AutonomyLevel.L2
    return level


def _service_plan_ids(store: Store, service: str, since: datetime | None = None) -> list[str]:
    seen: list[str] = []
    for row in store.executions(service=service, since=since):
        if row["kind"] == "apply" and row["plan_id"] not in seen:
            seen.append(row["plan_id"])
    return seen


def consecutive_failures(store: Store, service: str) -> int:
    """Trailing failures for remediations on this service (newest first). Inconclusive neither counts nor resets."""
    plan_ids = set(_service_plan_ids(store, service))
    outcomes = [o for o in store.outcomes() if o.plan_id in plan_ids]
    count = 0
    for o in sorted(outcomes, key=lambda o: o.ts, reverse=True):
        if o.result == OutcomeResult.failure:
            count += 1
        elif o.result == OutcomeResult.success:
            break
    return count


def executions_last_hour(store: Store, service: str, now: datetime) -> int:
    return len(_service_plan_ids(store, service, since=now - timedelta(hours=1)))
