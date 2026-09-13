"""Discussion: engineers can ask questions and propose their own fix; proposals become catalog plans or are refused."""
import asyncio

from judge.reasoning.discuss import AlternativeFix, DiscussContext, DiscussReply, HeuristicDiscussant, validate_alternative
from judge.settings import Config


def _ctx():
    cfg = Config()
    return DiscussContext(
        incident_summary="SEV1 checkout", services=["checkout"], evidence=["error rate 68% on /pay"],
        diagnosis=None, runbook=None, current_plan={"action": "toggle_flag", "params": {"flag": "payment_v2", "value": False}},
        catalog={n: {"params_schema": a.params_schema, "reversible": a.reversible, "blast_radius": a.blast_radius}
                 for n, a in cfg.actions.items()})


def test_engineer_proposal_becomes_catalog_plan():
    r = asyncio.run(HeuristicDiscussant().reply(_ctx(), "I'd rather scale the pool to 30", "Hung"))
    assert r.alternative_fix and r.alternative_fix.action == "scale_pool" and r.alternative_fix.params == {"size": 30}


def test_out_of_bounds_or_foreign_service_is_refused_with_explanation():
    ctx = _ctx()
    too_big = validate_alternative(DiscussReply(answer="ok", alternative_fix=AlternativeFix(
        action="scale_pool", params={"size": 500}, target_service="checkout")), ctx)
    assert too_big.alternative_fix is None and "couldn't turn that into a runnable plan" in too_big.answer
    other = validate_alternative(DiscussReply(answer="ok", alternative_fix=AlternativeFix(
        action="restart_service", params={}, target_service="catalog")), ctx)
    assert other.alternative_fix is None and "not part of this incident" in other.answer
    unknown = validate_alternative(DiscussReply(answer="ok", alternative_fix=AlternativeFix(
        action="drop_database", params={}, target_service="checkout")), ctx)
    assert unknown.alternative_fix is None


def test_question_gets_grounded_answer_without_plan():
    r = asyncio.run(HeuristicDiscussant().reply(_ctx(), "why do you think it's the flag?", "Hung"))
    assert r.alternative_fix is None and "error rate 68%" in r.answer
