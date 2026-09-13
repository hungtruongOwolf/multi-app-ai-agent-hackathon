from __future__ import annotations

from datetime import timedelta

from judge.core.models import AutonomyLevel, OutcomeResult, now
from judge.memory.lint import apply_lint, lint
from judge.memory.stats import compute_autonomy, compute_stats, mark_reviewed, review_marks, sync_code_owned
from tests.memory.conftest import record_outcomes

S, F = OutcomeResult.success, OutcomeResult.failure


def _auto(config, store, results, action="scale_pool", cap=AutonomyLevel.L3, runbook="rb"):
    record_outcomes(store, runbook, results)
    stats = compute_stats(store.outcomes(runbook), review_marks(store, runbook))
    return stats, compute_autonomy(stats, config.actions.get(action), cap, config.autonomy, False, now())


def test_ladder_levels(config, store):
    assert _auto(config, store, [S], runbook="a")[1].level == AutonomyLevel.L0
    assert _auto(config, store, [S, S], runbook="b")[1].level == AutonomyLevel.L1
    # scale_pool action cap is L2 even with many successes
    assert _auto(config, store, [S] * 12, runbook="c")[1].level == AutonomyLevel.L2
    # toggle_flag (reversible, service, cap L3) reaches L3
    assert _auto(config, store, [S] * 10, action="toggle_flag", runbook="d")[1].level == AutonomyLevel.L3
    # rollback_deploy is capped at L1
    assert _auto(config, store, [S] * 10, action="rollback_deploy", runbook="e")[1].level == AutonomyLevel.L1
    # runbook cap applies
    assert _auto(config, store, [S] * 10, action="toggle_flag", cap=AutonomyLevel.L1,
                 runbook="f")[1].level == AutonomyLevel.L1
    # no action -> L0
    assert _auto(config, store, [S] * 10, action="nope", runbook="g")[1].level == AutonomyLevel.L0


def test_one_failure_demotes_to_l1_until_reviewed(config, store):
    stats, auto = _auto(config, store, [S] * 6 + [F], action="toggle_flag", runbook="h")
    assert stats.failure_since_review == 1
    assert auto.level == AutonomyLevel.L1 and auto.review_required
    mark_reviewed(store, "h", "U_ONCALL_1", now())
    record_outcomes(store, "h", [S], start=now() + timedelta(seconds=1))
    stats = compute_stats(store.outcomes("h"), review_marks(store, "h"))
    auto = compute_autonomy(stats, config.actions["toggle_flag"], AutonomyLevel.L3, config.autonomy, False, now())
    assert not auto.review_required
    # failure still in the recent window keeps it below L2
    assert auto.level == AutonomyLevel.L1


def test_stale_caps_at_l1(config, store):
    old = now() - timedelta(days=60)
    record_outcomes(store, "s", [S] * 6, start=old, step_min=1)
    stats = compute_stats(store.outcomes("s"))
    auto = compute_autonomy(stats, config.actions["toggle_flag"], AutonomyLevel.L3, config.autonomy, False, now())
    assert auto.level == AutonomyLevel.L1


def test_sync_code_owned_writes_numbers_from_outcomes(config, store, seeded):
    record_outcomes(store, "db-pool-starved", [S, S, S])
    changed = sync_code_owned(seeded, store, config, now())
    assert changed == ["db-pool-starved"]
    rb = seeded.get_runbook("db-pool-starved")
    assert rb.frontmatter.stats.success == 3 and rb.frontmatter.autonomy.level == AutonomyLevel.L1
    assert sync_code_owned(seeded, store, config, now()) == []


def test_lint_findings_and_demotion(config, store, seeded):
    rb = seeded.get_runbook("checkout-payment-v2-flag")
    rb.frontmatter.stats.success = 12
    rb.frontmatter.stats.last_verified = now() - timedelta(days=45)
    rb.frontmatter.autonomy.level = AutonomyLevel.L3
    rb.sections["How to tell apart"] = ""
    rb.frontmatter.signatures.fingerprints.append("1664c5cb4162")  # duplicate with db-pool-starved
    orphan = seeded.get_runbook("db-pool-starved")
    orphan.frontmatter.action.name = "drop_database"
    seeded.commit_code_owned({rb.path: rb.to_markdown(), orphan.path: orphan.to_markdown(),
                              "wiki/index.md": "# Runbook index\n- [x](runbooks/ghost.md) — gone · services: - · errors: -\n"},
                             "break things")
    kinds = {(f.kind, f.runbook_id) for f in lint(seeded, config, store, now())}
    assert ("stale", "checkout-payment-v2-flag") in kinds
    assert ("orphan_action", "db-pool-starved") in kinds
    assert ("missing_distinguish", "checkout-payment-v2-flag") in kinds
    assert ("duplicate_signature", "db-pool-starved") in kinds
    assert ("index_missing", "db-pool-starved") in kinds
    assert ("index_dangling", "ghost") in kinds

    apply_lint(seeded, config, store, now())
    assert seeded.get_runbook("checkout-payment-v2-flag").frontmatter.autonomy.level == AutonomyLevel.L1
    o = seeded.get_runbook("db-pool-starved").frontmatter.autonomy
    assert o.level == AutonomyLevel.L0 and o.review_required
