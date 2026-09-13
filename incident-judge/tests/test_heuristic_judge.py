from judge.paths import KNOWLEDGE_DIR
"""Business-impact rules of the offline judge (regressions from eval J2/J6)."""
import asyncio

from judge.core.models import Env, Incident, Signal
from judge.reasoning.context import TriageContext
from judge.reasoning.judge import HeuristicJudge
from judge.settings import Config


def _ctx(service, metrics, signals):
    cfg = Config()
    inc = Incident(incident_key="k", environment=Env.production, services=[service])
    return TriageContext(incident=inc, signals=signals, services=[cfg.service(service)], metrics={service: metrics},
                         catalog=cfg.catalog)


def _sev(ctx):
    return asyncio.run(HeuristicJudge().triage(ctx)).severity.value


def test_secondary_journey_failure_capped_at_sev3():
    m = {"error_rate": 0.25, "latency_p95": 0.1, "error_rate:/products/{id}": 0.0, "latency_p95:/products/{id}": 0.1}
    sig = [Signal(source="sentry", fingerprint="f", service="catalog", environment=Env.production,
                  error_type="OSError", culprit="/profile/avatar", count=500, user_count=400),
           Signal(source="slo", fingerprint="s", service="catalog", environment=Env.production)]
    assert _sev(_ctx("catalog", m, sig)) == "SEV3"


def test_slow_critical_journey_not_capped():
    m = {"error_rate": 0.0, "latency_p95": 0.95, "error_rate:/search": 0.0, "latency_p95:/search": 0.95}
    sig = [Signal(source="slo", fingerprint="s", service="search", environment=Env.production)]
    assert _sev(_ctx("search", m, sig)) == "SEV2"


def test_few_users_unable_to_pay_is_at_least_sev2():
    m = {"error_rate": 0.004, "latency_p95": 0.1, "error_rate:/pay": 0.004, "latency_p95:/pay": 0.1}
    sig = [Signal(source="sentry", fingerprint="f", service="checkout", environment=Env.production,
                  error_type="PaymentLedgerError", culprit="/pay", count=12, user_count=8)]
    assert _sev(_ctx("checkout", m, sig)) in ("SEV1", "SEV2")


def test_llm_omitting_runbook_id_uses_machine_match(tmp_path):
    """Regression (live run): Claude proposed the runbook's exact action but left runbook_id null -> P7 'no plan'."""
    import asyncio
    from types import SimpleNamespace

    from judge.agent import Agent, Deps
    from judge.core.models import ActionProposal, AutonomyLevel, CustomerImpact, Env, Incident, IncidentState, \
        Severity, TriageProposal
    from judge.core.store import Store
    from judge.memory.repo import MemoryRepo, seed_runbooks
    from judge.settings import ROOT, Config, Settings

    settings = Settings.from_env(var_dir=tmp_path, trial_id="t1")
    store = Store(settings.db_path)
    repo = MemoryRepo(tmp_path / "memory", KNOWLEDGE_DIR)
    seed_runbooks(repo, [ROOT / "evals/fixtures/memory/checkout-payment-v2-flag.md"])
    inc = Incident(incident_key="k", environment=Env.production, services=["checkout"], state=IncidentState.OPEN,
                   severity=Severity.SEV1, customer_impact=CustomerImpact.major_outage, customer_visible=True)
    store.save_incident(inc)
    store.put_kv(f"{inc.id}:proposal", TriageProposal(
        severity=Severity.SEV1, customer_impact=CustomerImpact.major_outage, customer_visible=True, confidence=0.9,
        runbook_id=None, proposed_action=ActionProposal(name="toggle_flag", params={"flag": "payment_v2", "value": False},
                                                        target_service="checkout")).model_dump(mode="json"))
    store.put_kv(f"{inc.id}:match", {"runbook_id": "checkout-payment-v2-flag", "match_ok": True, "merged": True})
    decisions = []
    agent = Agent(Deps(settings=settings, config=Config(), store=store, sentry=None, linear=None, instatus=None,
                       slack=None, control=None, metrics=SimpleNamespace(available=lambda: True, value=lambda *a, **k: 30.0),
                       repo=repo, query=None, ingestor=None, runner=None, approval_poller=None,
                       judge=SimpleNamespace(name="test"), sentry_poller=None, slo_poller=None))

    async def fake_note(*a, **k):
        return "1.0"
    agent.note = fake_note
    real_decide = agent.decide
    agent.decide = lambda intent, ctx: decisions.append(real_decide(intent, ctx)) or decisions[-1]
    asyncio.run(agent.remediation_flow(inc))
    fix = [d for d in decisions if d.intent == "remediation.execute"]
    assert fix and "P7" not in fix[0].rules, fix
