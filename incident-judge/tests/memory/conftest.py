from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from judge.core.models import (
    CustomerImpact,
    Env,
    Incident,
    IncidentState,
    Outcome,
    OutcomeResult,
    Plan,
    Severity,
    Signal,
    VerifySpec,
    now,
)
from judge.core.store import Store
from judge.memory.repo import MemoryRepo, seed_runbooks
from judge.settings import ROOT, Config

FIXTURES = ROOT / "evals" / "fixtures" / "memory"


class FakeMetrics:
    def __init__(self, values: dict[tuple[str, str], float] | None = None, up: bool = True):
        self.values = values or {}
        self.up = up

    def available(self) -> bool:
        return self.up

    def value(self, metric, service, window_s, route=None):
        return self.values.get((metric, service))


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "judge.db")
    yield s
    s.close()


@pytest.fixture
def repo(tmp_path: Path) -> MemoryRepo:
    r = MemoryRepo(tmp_path / "memory")
    r.ensure()
    return r


@pytest.fixture
def seeded(repo: MemoryRepo) -> MemoryRepo:
    seed_runbooks(repo, [FIXTURES / "checkout-payment-v2-flag.md", FIXTURES / "db-pool-starved.md"])
    return repo


def make_incident(key="1664c5cb4162", service="checkout", error_type="PoolTimeout", **kw) -> tuple[Incident, Signal]:
    inc = Incident(incident_key=key, environment=Env.production, services=[service], severity=Severity.SEV1,
                   customer_impact=CustomerImpact.major_outage, customer_visible=True,
                   state=IncidentState.RESOLVED, **kw)
    sig = Signal(source="sentry", fingerprint=key, service=service, environment=Env.production,
                 error_type=error_type, culprit="/pay", count=120, user_count=80, last_seen=now())
    return inc, sig


def record_outcomes(store: Store, runbook_id: str, results: list[OutcomeResult], start=None, step_min=10):
    t0 = start or (now() - timedelta(minutes=step_min * (len(results) + 1)))
    for i, r in enumerate(results):
        store.add_outcome(Outcome(runbook_id=runbook_id, incident_id=f"inc_seed{i}", plan_id=f"plan_seed{i}",
                                  action="scale_pool", result=r, ts=t0 + timedelta(minutes=step_min * i)))


def remediated_incident(store: Store, inc: Incident, sig: Signal, action="scale_pool", params=None,
                        result=OutcomeResult.success, runbook_id=None):
    store.save_incident(inc)
    store.add_signal(sig, inc.id)
    plan = Plan(incident_id=inc.id, runbook_id=runbook_id, action=action, params={"size": 20} if params is None else params,
                target_service=inc.primary_service, verify=VerifySpec(conditions=[]), autonomy_level="L1")
    store.save_plan(plan, "done")
    store.add_execution(plan=plan, kind="apply", result="applied", decision_id="dec_x", approval_id="apr_x")
    store.add_verification(plan.plan_id, inc.id, result.value if result != OutcomeResult.success else "pass", [])
    store.add_outcome(Outcome(runbook_id=runbook_id, incident_id=inc.id, plan_id=plan.plan_id, action=action,
                              result=result))
    return plan
