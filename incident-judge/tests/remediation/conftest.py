from __future__ import annotations

import pytest

from judge.core.models import AutonomyLevel, Decision, DecisionResult, Env, Incident, Plan, Severity, VerifySpec
from judge.core.store import Store
from judge.settings import Config, Settings


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def settings(tmp_path):
    return Settings(var_dir=tmp_path, time_scale=1.0, trial_id="t1")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "judge.db")
    yield s
    s.close()


@pytest.fixture
def incident():
    return Incident(incident_key="fp1", environment=Env.production, services=["checkout"], severity=Severity.SEV1)


@pytest.fixture
def make_plan(config, incident):
    def _make(action="scale_pool", params=None, service="checkout"):
        spec = config.actions[action]
        return Plan(incident_id=incident.id, runbook_id="db-pool-starved", action=action,
                    params=params if params is not None else {"size": 20}, target_service=service,
                    verify=VerifySpec(conditions=[c.bind(service) for c in spec.verify.conditions],
                                      window_s=spec.verify.window_s, min_rps=spec.verify.min_rps),
                    autonomy_level=AutonomyLevel.L1)
    return _make


@pytest.fixture
def allow():
    return Decision(intent="remediation.execute", result=DecisionResult.ALLOW)
