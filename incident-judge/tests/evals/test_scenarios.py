from __future__ import annotations

import ast
from pathlib import Path

import pytest

from evals.scenario import FIXTURES_DIR, SCENARIOS_DIR, all_scenarios, load_outcomes_fixture, select

SPEC_IDS = ([f"J{i}" for i in range(1, 7)] + [f"D{i}" for i in range(1, 4)] + [f"M{i}" for i in range(1, 6)]
            + [f"R{i}" for i in range(1, 9)] + [f"A{i}" for i in range(1, 4)] + [f"X{i}" for i in range(1, 6)])
CORE = {"J1", "J2", "J3", "J4", "D1", "D2", "D3", "M1", "M2", "M3", "R1", "R2", "R3", "R8", "A1", "A2", "X1", "X2"}


def test_all_scenarios_parse_and_cover_spec():
    scenarios = all_scenarios()
    ids = [s.id for s in scenarios]
    assert len(ids) == len(set(ids))
    assert sorted(ids) == sorted(SPEC_IDS)
    assert {s.id for s in scenarios if s.tier == "core"} == CORE
    assert len(select("core")) == len(CORE)


@pytest.mark.parametrize("s", all_scenarios(), ids=lambda s: s.id)
def test_scenario_is_meaningful(s):
    e = s.expect
    has_expectation = any([e.incidents, e.linear, e.instatus, e.slack, e.actions_executed is not None,
                           e.decisions_contains, e.memory, e.config_end, e.terminal_states, e.severity,
                           e.severity_order])
    assert has_expectation or s.forbidden
    assert s.timeline() or s.memory_fixture.runbooks, "scenario must do something"
    assert s.timeout_s > s.wait.min_runtime_s
    for rb in s.memory_fixture.runbooks:
        assert (FIXTURES_DIR / "memory" / f"{rb}.md").exists()
    for name in s.memory_fixture.outcomes:
        fx = load_outcomes_fixture(name)
        assert fx.outcomes


def test_scenario_yaml_has_no_boolean_on_key():
    for p in SCENARIOS_DIR.glob("*.yaml"):
        assert "\n  - on:" not in p.read_text(encoding="utf-8") and "{on:" not in p.read_text(encoding="utf-8"), p


def test_graders_never_import_redactor():
    for p in (Path(__file__).resolve().parents[2] / "evals" / "graders").glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("judge.safety"), p
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("judge.safety") for a in node.names), p


def test_seed_trial_builds_memory_and_autonomy(tmp_path):
    from evals.scenario import select
    from evals.seed import seed_trial
    from judge.settings import Settings

    s = select("R2")[0]
    settings = Settings.from_env(trial_id="r2test123", var_dir=tmp_path)
    seed = seed_trial(s, settings)
    assert len(seed["outcome_ids"]) == 3
    fm = seed["runbooks"]["db-pool-starved"]
    assert fm["autonomy"]["level"] == "L1"
    assert fm["stats"]["success"] == 3
    assert len(fm["match_conditions"]) == 1  # weak variant

    a3 = select("A3")[0]
    settings = Settings.from_env(trial_id="a3test123", var_dir=tmp_path)
    seed = seed_trial(a3, settings)
    assert seed["branches"] and seed["proposals"][0]["files"]
    from evals.snapshot import load_memory

    mem = load_memory(settings.memory_dir)
    assert "wiki/runbooks/checkout-payment-v2-flag.md" in mem["main"]
    assert any(p["status"] == "open" for p in mem["proposals"])
    assert "SYSTEM NOTE" not in mem["main"]["wiki/runbooks/checkout-payment-v2-flag.md"]
    assert any("SYSTEM NOTE" in f.get("wiki/runbooks/checkout-payment-v2-flag.md", "") for f in mem["branches"].values())
