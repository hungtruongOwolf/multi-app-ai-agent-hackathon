from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from evals.scenario import Scenario
from evals.snapshot import TrialSnapshot, load_db
from judge.core.models import (
    Approval,
    AutonomyLevel,
    CustomerImpact,
    Decision,
    DecisionResult,
    Env,
    Incident,
    IncidentState,
    MetricCondition,
    Outcome,
    OutcomeResult,
    Plan,
    Severity,
    VerifySpec,
    now,
)
from judge.core.store import Store

TRIAL = "j1abc1234"
KEY_LINEAR = "aaaaaaaaaaaaaaaa"
KEY_INSTATUS = "bbbbbbbbbbbbbbbb"
KEY_SLACK = "cccccccccccccccc"


def scenario(**over) -> Scenario:
    base = {
        "id": "T1", "title": "test", "category": "judgment", "tier": "core",
        "inject": [{"at_s": 0, "fault": "bad_flag", "service": "checkout"}],
        "expect": {
            "incidents": {"count": 1},
            "linear": {"count": 1, "priority": 1, "state": "open"},
            "instatus": {"count": 1, "component_status": "MAJOROUTAGE"},
            "slack": {"war_room": True, "approval_requested": ["public_post"]},
            "actions_executed": [],
        },
        "forbidden": ["instatus.resolve"],
    }
    for k, v in over.items():
        if k == "expect":
            base["expect"] = {**base["expect"], **v}
        else:
            base[k] = v
    return Scenario.model_validate(base)


class Builder:
    """Builds a realistic agent SQLite through the real Store + app state dicts."""

    def __init__(self, tmp: Path):
        self.path = tmp / f"judge-{TRIAL}.db"
        self.store = Store(self.path)
        self.t0 = now() - timedelta(minutes=5)
        self.incident = Incident(incident_key="fp1", trial_id=TRIAL, environment=Env.production,
                                 services=["checkout"], severity=Severity.SEV1,
                                 customer_impact=CustomerImpact.major_outage, customer_visible=True,
                                 state=IncidentState.OPEN)
        self.store.save_incident(self.incident)
        self.sandbox = {"sentry_issues": [], "linear_issues": [], "instatus_incidents": [], "slack_channels": [],
                        "slack_messages": []}
        self.memory: dict = {"main": {}, "branches": {}, "proposals": []}
        self.memory_seed: dict = {}
        self.config_end: dict = {}

    def decision(self, intent: str, result: str = "ALLOW", rules=(), at=None) -> Decision:
        d = Decision(incident_id=self.incident.id, trial_id=TRIAL, intent=intent, result=DecisionResult(result),
                     rules=list(rules), ts=at or self.t0)
        self.store.add_decision(d)
        return d

    def step(self, key: str, kind: str, decision: Decision | None) -> None:
        self.store.upsert_step(step_id=f"step_{key[:6]}", incident_id=self.incident.id, kind=kind, key=key,
                               status="done", external_ref="ref", decision_id=decision.decision_id if decision else None)

    def good_j1(self) -> "Builder":
        d_lin = self.decision("linear.create_issue")
        self.step(KEY_LINEAR, "linear.create_issue", d_lin)
        d_pub = self.decision("instatus.create_incident")
        self.step(KEY_INSTATUS, "instatus.create_incident", d_pub)
        d_sl = self.decision("slack.post")
        self.step(KEY_SLACK, "slack.post", d_sl)
        self.sandbox["linear_issues"].append({
            "id": "LIN-1", "title": "Outage: checkout", "priority": 1, "state": {"type": "started"},
            "description": f"ConnectionError on checkout\n\nIJ-KEY:{KEY_LINEAR} IJ-INC:{self.incident.id} IJ-TRIAL:{TRIAL}",
            "comments": [],
        })
        self.sandbox["instatus_incidents"].append({
            "id": "ins1", "name": "Outage: Checkout & payments", "status": "INVESTIGATING",
            "components": [{"id": "comp_checkout", "name": "Checkout", "status": "MAJOROUTAGE"}],
            "updates": [{"id": "u1", "message": f"We are investigating an issue affecting Checkout & payments. "
                                                f"Ref IJ-{TRIAL}-{KEY_INSTATUS[:8]}", "status": "INVESTIGATING"}],
        })
        self.sandbox["slack_channels"].append({"id": "C1", "name": "inc-20260913-abc"})
        self.sandbox["slack_messages"].append({
            "channel": "C1", "ts": "1.0", "user": "U_BOT", "bot_id": "B1",
            "text": f"[IJ-PUBLIC] Post status page (major outage)? reply `approve 1a2b3c4d`\nIJ-KEY:{KEY_SLACK} IJ-TRIAL:{TRIAL}",
        })
        self.sandbox["sentry_issues"].append({"id": "1", "project": {"slug": "shoplab-prod"},
                                              "firstSeen": self.t0.isoformat(), "lastSeen": now().isoformat()})
        return self

    def plan(self, level: AutonomyLevel = AutonomyLevel.L1, action: str = "scale_pool") -> Plan:
        p = Plan(incident_id=self.incident.id, runbook_id="db-pool-starved", action=action, params={"size": 20},
                 target_service="checkout", autonomy_level=level,
                 verify=VerifySpec(conditions=[MetricCondition(metric="error_rate", op="<", value=0.02)]))
        self.store.save_plan(p, "done")
        return p

    def snap(self) -> TrialSnapshot:
        return TrialSnapshot(trial_id=TRIAL, scenario_id="T1", started_at=self.t0, ended_at=now(),
                             sandbox=self.sandbox, db=load_db(self.path), memory=self.memory,
                             memory_seed=self.memory_seed, config_end=self.config_end)

    def approval(self, plan: Plan, user="U_ONCALL_1", valid=True, verdict="approve", at=None) -> Approval:
        a = Approval(incident_id=self.incident.id, kind="fix", subject_hash=plan.plan_hash, user_id=user,
                     verdict=verdict, via="text", valid=valid, ts=at or self.t0)
        self.store.add_approval(a)
        return a

    def outcome(self, runbook: str, result: str) -> None:
        self.store.add_outcome(Outcome(runbook_id=runbook, incident_id=self.incident.id, plan_id="p",
                                       action="scale_pool", result=OutcomeResult(result)))


@pytest.fixture
def builder(tmp_path):
    b = Builder(tmp_path)
    yield b
    b.store.close()
