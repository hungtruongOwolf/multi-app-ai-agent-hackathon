"""Console renders a realistic incident end to end from a synthetic store (no network)."""
from __future__ import annotations

from judge.paths import KNOWLEDGE_DIR

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from judge.console.app import create_app
from judge.console.data import ConsoleData
from judge.core.models import (
    Approval, AutonomyLevel, CustomerImpact, Decision, DecisionResult, Env, Incident, IncidentState, MetricCondition,
    Outcome, OutcomeResult, Plan, Severity, Signal, VerifySpec, now,
)
from judge.core.store import Store
from judge.memory.repo import MemoryRepo, seed_runbooks
from judge.settings import ROOT, Config, Settings

MARK = "IJ-KEY:abcdef0123456789 IJ-INC:inc_console IJ-TRIAL:con1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPLAB_SUPERVISOR_URL", "http://127.0.0.1:1")  # unreachable on purpose
    settings = Settings.from_env(var_dir=tmp_path, trial_id="con1", backend="real", slack_oncall_channel="C123")
    store = Store(settings.db_path)
    t0 = now() - timedelta(minutes=6)
    inc = Incident(id="inc_console", incident_key="k1", environment=Env.production, services=["checkout"],
                   state=IncidentState.RESOLVED, severity=Severity.SEV1, customer_impact=CustomerImpact.major_outage,
                   customer_visible=True, runbook_id="checkout-payment-v2-flag", public_posted=True,
                   slack_thread_ts="1789322651.466759", linear_issue_id="lin-1", instatus_incident_id="st-1",
                   created_at=t0, resolved_at=t0 + timedelta(minutes=4))
    store.save_incident(inc)
    store.add_signal(Signal(signal_id="sentry:77", source="sentry", fingerprint="k1", service="checkout",
                            environment=Env.production, error_type="ConnectionError", culprit="/pay", count=229,
                            user_count=222, first_seen=t0 - timedelta(seconds=20), external_id="77"), inc.id)
    store.add_signal(Signal(signal_id="slo:x", source="slo", fingerprint="k2", service="checkout",
                            environment=Env.production, slo_name="checkout_availability", burn_rate=200), inc.id)
    store.put_kv("sentry_issue_meta:77", {"title": "ConnectionError: payment provider v2 unreachable",
                                          "permalink": "https://incident-judge.sentry.io/issues/77/", "shortId": "SHOPLAB-PROD-6"})
    store.put_kv(f"{inc.id}:linear", {"identifier": "INC-17", "url": "https://linear.app/incident-judge/issue/INC-17"})
    store.put_kv(f"{inc.id}:proposal", {"severity": "SEV1", "customer_impact": "major_outage", "customer_visible": True,
                                        "rationale_internal": f"Payments failing on /pay. {MARK}"})
    store.put_kv(f"{inc.id}:match", {"runbook_id": "checkout-payment-v2-flag", "via": "fingerprint", "match_ok": True,
                                     "condition_results": [{"check": "metric", "metric": "error_rate", "op": ">",
                                                            "value": 0.05, "observed": 0.62, "ok": True}]})
    for i, (a, b) in enumerate([("DETECTED", "TRIAGING"), ("TRIAGING", "OPEN"), ("OPEN", "AWAITING_FIX_APPROVAL"),
                                ("AWAITING_FIX_APPROVAL", "EXECUTING"), ("EXECUTING", "MONITORING"),
                                ("MONITORING", "RESOLVED")]):
        store.put_kv(f"transition:{inc.id}:{(t0 + timedelta(seconds=30 * (i + 1))).timestamp()}", f"{a}->{b}")
    store.add_approval(Approval(incident_id=inc.id, kind="fix", subject_hash="9e655409aaaa", user_id="U0C1",
                                verdict="approve", via="button", valid=True, ts=t0 + timedelta(seconds=90)))
    store.put_kv("slack_user:U0C1", "Hung Truong")
    store.add_decision(Decision(incident_id=inc.id, intent="instatus.create_incident", result=DecisionResult.ALLOW,
                                ts=t0 + timedelta(seconds=91)))
    store.add_decision(Decision(incident_id=inc.id, intent="remediation.execute", result=DecisionResult.REQUIRE_APPROVAL,
                                rules=["P8"], explain="L1: human confirm required", ts=t0 + timedelta(seconds=60)))
    store.add_decision(Decision(incident_id=inc.id, intent="slack.post", result=DecisionResult.ALLOW, explain=MARK))
    plan = Plan(plan_id="plan_c1", incident_id=inc.id, runbook_id="checkout-payment-v2-flag", action="toggle_flag",
                params={"flag": "payment_v2", "value": False}, target_service="checkout",
                verify=VerifySpec(conditions=[MetricCondition(metric="error_rate", service="checkout", op="<", value=0.02)]),
                autonomy_level=AutonomyLevel.L1)
    store.save_plan(plan, "done")
    store.add_execution(plan=plan, kind="apply", result=json.dumps({"prev": True, "value": False}), decision_id="d",
                        approval_id="a")
    samples = [{"t": (t0 + timedelta(seconds=120 + 5 * i)).isoformat(), "rps": 13.0,
                "conditions": [{"metric": "error_rate", "op": "<", "value": 0.02, "observed": v, "holds": v < 0.02}]}
               for i, v in enumerate([0.9, 0.6, 0.3, 0.1, 0.0, 0.0])]
    store.add_verification(plan.plan_id, inc.id, "pass", samples)
    store.put_kv(f"{plan.plan_id}:before", {"at": (t0 + timedelta(seconds=115)).isoformat(), "values": {"error_rate": 1.0}})
    store.put_kv(f"{inc.id}:changes", [{"at": (t0 + timedelta(seconds=116)).isoformat(),
                                        "change": "`checkout` feature flag `payment_v2`: *true → false*",
                                        "rollback": "set `payment_v2` back to `true`"}])
    store.put_kv(f"{inc.id}:diagnosis", {
        "summary": "The payment_v2 flag routes /pay to an unreachable provider.",
        "why_it_happens": "Enabling payment_v2 switches checkout to provider v2, which is not reachable.",
        "hypotheses": [{"cause": "payment_v2 flag enabled", "evidence": ["flag flipped 20s before errors"], "confidence": 0.9}],
        "recommended_fix": {"action": "toggle_flag", "params": {"flag": "payment_v2", "value": False},
                            "target_service": "checkout", "why_this_fixes_it": "routes back to v1",
                            "risk": "low", "how_to_verify": "error rate < 2%"},
        "docs_cited": ["wiki/services/checkout.md"], "open_questions": ["Why was the flag enabled?"]})
    store.put_kv(f"{inc.id}:discussion", [
        {"ts": (t0 + timedelta(seconds=100)).isoformat(), "author": "Hung Truong", "role": "human", "text": "Why the flag?"},
        {"ts": (t0 + timedelta(seconds=105)).isoformat(), "role": "agent", "text": "It was enabled 20s before errors."}])
    store.add_outcome(Outcome(runbook_id="checkout-payment-v2-flag", incident_id=inc.id, plan_id=plan.plan_id,
                              action="toggle_flag", result=OutcomeResult.success))
    store.put_kv("agent_heartbeat", now().isoformat())

    repo = MemoryRepo(settings.memory_dir, KNOWLEDGE_DIR)
    repo.ensure()
    seed_runbooks(repo, [ROOT / "evals/fixtures/memory/checkout-payment-v2-flag.md"])
    repo.commit_code_owned({"wiki/services/checkout.md": "# Checkout\n\n## Flags\n\n- `payment_v2`: new provider\n"},
                           "docs: checkout")

    reports = tmp_path / "reports"
    (reports / "run1").mkdir(parents=True)
    (reports / "run1" / "results.json").write_text(json.dumps({
        "run_id": "run1", "baseline": "full", "k": 3, "scenarios": ["J1"], "tiers": {"J1": "core"},
        "titles": {"J1": "Payment flag outage"},
        "results": [{"scenario_id": "J1", "verdict": "pass"}] * 3,
        "metrics": {"pass_all_k": 1.0, "pass_rate": 1.0, "unsafe_rate": 0.0, "mixed": [], "rollback_correctness": 1.0}}),
        encoding="utf-8")
    data = ConsoleData(settings, Config(), reports_dir=reports, http_timeout=0.2)
    return TestClient(create_app(data=data)), inc


def test_pages_render_and_hide_markers(env):
    client, inc = env
    for path in ["/", "/incidents", f"/incidents/{inc.id}", "/wiki", "/wiki/runbooks/checkout-payment-v2-flag",
                 "/wiki/docs/wiki/services/checkout.md", "/shoplab", "/evals", "/static/console.css", "/static/console.js"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "IJ-KEY" not in r.text and "IJ-TRIAL" not in r.text, path


def test_incident_detail_story_links_and_chart(env):
    client, inc = env
    page = client.get(f"/incidents/{inc.id}").text
    assert "https://slack.com/archives/C123/p1789322651466759" in page
    assert "https://linear.app/incident-judge/issue/INC-17" in page
    assert "https://incident-judge.sentry.io/issues/77/" in page
    assert "/wiki/runbooks/checkout-payment-v2-flag" in page
    assert "<svg" in page and 'class="target"' in page and "before fix" in page
    assert "Fix approved" in page and "Hung Truong" in page
    assert "Diagnosis" in page and "payment_v2 flag enabled" in page and "wiki/services/checkout.md" in page
    assert "Why the flag?" in page
    assert "Verification pass" in page
    # routine Slack posts are not in the audit table
    assert "slack.post" not in page and "remediation.execute" in page


def test_api_and_partial_refresh(env):
    client, inc = env
    ov = client.get("/api/overview").json()
    assert ov["agent_live"] is True and ov["total"] == 1
    detail = client.get(f"/api/incidents/{inc.id}").json()
    assert detail["links"]["linear_id"] == "INC-17" and detail["diagnosis"]["summary"]
    assert any(e["kind"] == "verify" for e in detail["timeline"])
    partial = client.get(f"/incidents/{inc.id}?partial=1").text
    assert "<html" not in partial and "Story" in partial


def test_empty_states_without_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPLAB_SUPERVISOR_URL", "http://127.0.0.1:1")
    settings = Settings.from_env(var_dir=tmp_path, trial_id="empty1")
    client = TestClient(create_app(data=ConsoleData(settings, Config(), reports_dir=tmp_path / "none", http_timeout=0.2)))
    assert "All clear" in client.get("/").text
    assert "No incidents yet" in client.get("/incidents").text
    assert "ShopLab is not running" in client.get("/shoplab").text
    assert "No eval runs" in client.get("/evals").text
    assert client.get("/incidents/nope").status_code == 404


def test_knowledge_graph_renders(env):
    client, inc = env
    r = client.get("/wiki/graph")
    assert r.status_code == 200 and "<svg" in r.text
    assert "checkout-payment-v2-flag" in r.text or "Payment" in r.text
    assert 'href="/wiki/graph"' in client.get("/wiki").text
