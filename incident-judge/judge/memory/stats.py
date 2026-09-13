"""Code-owned runbook numbers. Derived only from the agent's `outcomes` table (verified results)
and human review marks. The LLM can never write these (invariant I4, rule P10)."""

from __future__ import annotations

from datetime import datetime, timedelta

from judge.core.models import ActionSpec, AutonomyLevel, Outcome, OutcomeResult, RunbookStats
from judge.core.store import Store
from judge.memory.repo import MemoryRepo
from judge.memory.schema import RunbookAutonomy, code_owned_dump
from judge.settings import AutonomyThresholds, Config

RECENT_MAX = 10
_LEVELS = [AutonomyLevel.L0, AutonomyLevel.L1, AutonomyLevel.L2, AutonomyLevel.L3]


def _min_level(*levels: AutonomyLevel) -> AutonomyLevel:
    return min(levels, key=lambda l: l.rank)


def compute_stats(outcomes: list[Outcome], review_marks: list[datetime] | None = None) -> RunbookStats:
    ordered = sorted(outcomes, key=lambda o: o.ts)
    last_review = max(review_marks) if review_marks else None
    successes = [o for o in ordered if o.result == OutcomeResult.success]
    return RunbookStats(
        success=len(successes),
        failure=sum(o.result == OutcomeResult.failure for o in ordered),
        inconclusive=sum(o.result == OutcomeResult.inconclusive for o in ordered),
        failure_since_review=sum(
            o.result == OutcomeResult.failure and (last_review is None or o.ts > last_review) for o in ordered
        ),
        last_verified=successes[-1].ts if successes else None,
        recent=[o.result for o in ordered[-RECENT_MAX:]],
    )


def is_stale(stats: RunbookStats, thresholds: AutonomyThresholds, now: datetime) -> bool:
    if stats.last_verified is None:
        return False  # never verified -> ladder already keeps it at L0
    return now - stats.last_verified > timedelta(days=thresholds.stale_after_days)


def compute_autonomy(
    stats: RunbookStats,
    action_spec: ActionSpec | None,
    runbook_cap: AutonomyLevel,
    thresholds: AutonomyThresholds,
    review_required: bool,
    now: datetime,
) -> RunbookAutonomy:
    """review_required: external override (e.g. lint found the action missing). A failure since the
    last human review always forces review_required; only a new review mark clears it."""
    needs_review = review_required or stats.failure_since_review > 0
    if action_spec is None:
        return RunbookAutonomy(level=AutonomyLevel.L0, cap=runbook_cap, review_required=needs_review)

    level = AutonomyLevel.L0
    if stats.success >= thresholds.L1_min_success:
        level = AutonomyLevel.L1
    if not needs_review:
        recent = stats.recent[-thresholds.L2_recent_window:]
        clean_recent = OutcomeResult.failure not in recent
        repeatable = action_spec.reversible or action_spec.safe_to_repeat
        if stats.success >= thresholds.L2_min_success and clean_recent and repeatable:
            level = AutonomyLevel.L2
            if (stats.success >= thresholds.L3_min_success and action_spec.reversible
                    and action_spec.blast_radius in ("pod", "service")):
                level = AutonomyLevel.L3
    if is_stale(stats, thresholds, now):
        level = _min_level(level, AutonomyLevel.L1)
    level = _min_level(level, action_spec.autonomy_cap, runbook_cap)
    return RunbookAutonomy(level=level, cap=runbook_cap, review_required=needs_review)


# ---------------------------------------------------------------- review marks (human)


def review_marks(store: Store, runbook_id: str) -> list[datetime]:
    return [datetime.fromisoformat(m["ts"]) for m in store.get_kv(f"runbook_review:{runbook_id}", [])]


def mark_reviewed(store: Store, runbook_id: str, user_id: str, at: datetime) -> None:
    marks = store.get_kv(f"runbook_review:{runbook_id}", [])
    marks.append({"user": user_id, "ts": at.isoformat()})
    store.put_kv(f"runbook_review:{runbook_id}", marks)


# ---------------------------------------------------------------- sync to repo


def sync_code_owned(repo: MemoryRepo, store: Store, config: Config, now: datetime) -> list[str]:
    """Recompute stats + autonomy for every runbook on main; commit the ones that changed."""
    changed: dict[str, str] = {}
    ids: list[str] = []
    for rb in repo.runbooks():
        fm = rb.frontmatter
        stats = compute_stats(store.outcomes(fm.id), review_marks(store, fm.id))
        spec = config.actions.get(fm.action.name) if fm.action else None
        orphan = fm.action is not None and spec is None
        autonomy = compute_autonomy(stats, spec, fm.autonomy.cap, config.autonomy, orphan, now)
        before = code_owned_dump(fm)
        fm.stats, fm.autonomy = stats, autonomy
        if code_owned_dump(fm) != before:
            changed[rb.path] = rb.to_markdown()
            ids.append(fm.id)
    if changed:
        repo.commit_code_owned(changed, f"stats: recompute code-owned fields for {', '.join(ids)}")
    return ids
