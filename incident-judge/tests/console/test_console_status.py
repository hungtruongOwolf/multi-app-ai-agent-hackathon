"""'Where things stand' banner and the live 'Across the apps' panel (no network)."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from judge.console.app import create_app
from judge.console.data import ConsoleData
from judge.console.live import LiveApps
from judge.core.models import (
    CustomerImpact, Decision, DecisionResult, Env, Incident, IncidentState, MetricCondition, Plan, Severity, Signal,
    VerifySpec, now,
)
from judge.core.store import Store
from judge.settings import Config, Settings


def _setup(tmp_path, monkeypatch, backend="sandbox"):
    monkeypatch.setenv("SHOPLAB_SUPERVISOR_URL", "http://127.0.0.1:1")
    s = Settings.from_env(var_dir=tmp_path, trial_id="st1", backend=backend, slack_oncall_channel="C1")
    store = Store(s.db_path)
    return s, store


def _incident(store, state, **kw):
    inc = Incident(id=f"inc_{state.value.lower()}", incident_key="k", environment=Env.production, services=["checkout"],
                   state=state, severity=Severity.SEV1, customer_impact=CustomerImpact.major_outage,
                   customer_visible=True, created_at=now() - timedelta(minutes=5), **kw)
    store.save_incident(inc)
    return inc


def test_needs_human_when_diagnosed_fix_is_blocked(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    inc = _incident(store, IncidentState.OPEN)
    store.put_kv(f"{inc.id}:diagnosis_state", "done")
    store.put_kv(f"{inc.id}:diagnosis", {"summary": "bad deploy", "recommended_fix": {
        "action": "rollback_deploy", "params": {"to_version": "1.4.1"}, "target_service": "checkout"}})
    store.add_decision(Decision(incident_id=inc.id, intent="remediation.execute", result=DecisionResult.DENY,
                                rules=["P9"], explain="rate limit: 1 executions in last hour >= 1"))
    store.put_kv(f"{inc.id}:pages", [{"at": now().isoformat(), "reason": "fix blocked", "key": "diagnosis-deny"}])
    st = ConsoleData(s, Config()).status(inc)
    assert st["label"] == "Needs human" and st["waiting_on"] == "on-call"
    assert "rollback_deploy(to_version=1.4.1)" in st["detail"] and "P9" in st["detail"] and "paged" in st["detail"]


def test_awaiting_approval_lists_items_and_expiry(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    inc = _incident(store, IncidentState.AWAITING_FIX_APPROVAL)
    plan = Plan(plan_id="plan_a", incident_id=inc.id, runbook_id=None, action="scale_pool", params={"size": 20}, target_service="checkout",
                verify=VerifySpec(conditions=[MetricCondition(metric="error_rate", service="checkout", op="<", value=0.02)]),
                autonomy_level="L1")
    store.save_plan(plan, "awaiting_approval")
    store.put_kv(f"{inc.id}:fix_pending", {"plan_id": "plan_a", "at": now().isoformat(), "kind": "fix"})
    store.put_kv(f"{inc.id}:public_pending", {"subject": "abc", "at": now().isoformat(), "ts": "1.2"})
    st = ConsoleData(s, Config()).status(inc)
    assert st["label"] == "Awaiting approval"
    assert "fix `scale_pool`" in st["detail"] and "status page post" in st["detail"] and "Expires in" in st["detail"]


def test_monitoring_and_closed_labels_in_list(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    mon = _incident(store, IncidentState.MONITORING)
    store.add_signal(Signal(signal_id="sentry:1", source="sentry", fingerprint="k", service="checkout",
                            environment=Env.production, error_type="E", last_seen=now()), mon.id)
    closed = _incident(store, IncidentState.CLOSED, resolved_at=now())
    data = ConsoleData(s, Config())
    assert data.status(mon)["label"] == "Monitoring" and "quiet window" in data.status(mon)["detail"]
    page = TestClient(create_app(data=data)).get("/incidents").text
    assert "Monitoring" in page and "Closed" in page and ">MONITORING<" not in page
    assert data.status(closed)["tone"] == "green"


class FakeLive(LiveApps):
    def enabled(self) -> bool:
        return True

    def fetch(self, inc, sentry_ids, pr_number):
        return {"linear": {"ok": True, "identifier": "INC-9", "state": "Done", "state_type": "completed",
                           "assignee": "On Call", "url": "https://linear.app/x/INC-9"},
                "instatus": {"ok": True, "status": "RESOLVED", "components": [{"name": "Checkout", "status": "OPERATIONAL"}]},
                "sentry": {"ok": True, "issues": [{"id": "77", "status": "resolved", "count": "12", "short_id": "PROD-1",
                                                   "url": "https://sentry.io/issues/77/"}]}}


def test_apps_panel_shows_live_state(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch, backend="real")
    inc = _incident(store, IncidentState.RESOLVED, resolved_at=now(), linear_issue_id="lin-9", instatus_incident_id="st-9",
                    public_posted=True)
    store.add_signal(Signal(signal_id="sentry:77", source="sentry", fingerprint="k", service="checkout",
                            environment=Env.production, error_type="E", external_id="77"), inc.id)
    data = ConsoleData(s, Config())
    page = TestClient(create_app(data=data, live=FakeLive(s))).get(f"/incidents/{inc.id}").text
    assert "Across the apps" in page and "Where things stand" in page
    assert "INC-9" in page and "Done" in page and "On Call" in page
    assert "resolved" in page and "Checkout: operational" in page and "PROD-1" in page
    assert "not paged" in page and "read live" in page


def test_live_apps_off_in_sandbox_and_without_credentials(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    inc = _incident(store, IncidentState.OPEN, linear_issue_id="lin-1")
    assert LiveApps(s).fetch(inc, ["1"], 3) == {}
    real = Settings.from_env(var_dir=tmp_path, trial_id="st1", backend="real")
    assert LiveApps(real).fetch(inc, [], None) == {}  # conftest blanks credentials: nothing to call
