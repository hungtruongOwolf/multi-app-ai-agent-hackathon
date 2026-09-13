from __future__ import annotations

import json
from datetime import timedelta

import pytest

from judge.approvals.cards import fix_card, public_post_card
from judge.approvals.slack_commands import ApprovalPoller, parse_command
from judge.approvals.response_card import CardItem, build_card, verdicts_for
from judge.approvals.slack_socket import handle_card_action
from judge.approvals.verifier import ApprovalVerifier
from judge.core.models import (
    AutonomyLevel,
    Env,
    Incident,
    Plan,
    RunbookStats,
    Severity,
    VerifySpec,
    now,
)
from judge.core.store import Store
from judge.safety.redact import find_forbidden
from judge.settings import Config, Settings

SUBJECT = "1a2b3c4d5e6f7a8b"


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def verifier(config, tmp_path):
    return ApprovalVerifier(config, Settings(var_dir=tmp_path))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "judge.db")
    yield s
    s.close()


@pytest.fixture
def incident():
    return Incident(incident_key="fp", environment=Env.production, services=["checkout"], severity=Severity.SEV1,
                    slack_channel_id="C_INC", slack_thread_ts="1000.000100")


# ---------------------------------------------------------------- parse_command


@pytest.mark.parametrize("text,expected", [
    ("approve 1a2b3c4d", ("approve", "1a2b3c4d")),
    ("  Reject 1A2B3C4D please", ("reject", "1a2b3c4d")),
    ("<@U_BOT> approve 1a2b3c4d", ("approve", "1a2b3c4d")),
    ("approve 1a2b3c4d5e6f", None),   # word boundary: 8 hex exactly followed by more hex is not a match
    ("approve", None),
    ("approve xyz12345", None),
    ("please approve 1a2b3c4d", None),  # must start with the command
    ("looks fine, resolve it", None),
    ("", None),
])
def test_parse_command(text, expected):
    assert parse_command(text) == expected


# ---------------------------------------------------------------- verifier (P14)


def _verify(verifier, incident, **kw):
    args = dict(incident=incident, kind="fix", subject_hash=SUBJECT, user_id="U_ONCALL_1", is_bot=False,
                verdict="approve", via="text", provided_hash=SUBJECT[:8])
    args.update(kw)
    return verifier.verify(**args)


def test_allowlisted_user_valid(verifier, incident):
    a = _verify(verifier, incident)
    assert a.valid and a.subject_hash == SUBJECT and a.verdict == "approve" and a.reason == ""


def test_intruder_invalid(verifier, incident):
    a = _verify(verifier, incident, user_id="U_INTRUDER")
    assert not a.valid and "not in on-call allowlist" in a.reason


def test_bot_invalid_even_if_allowlisted(verifier, incident):
    a = _verify(verifier, incident, is_bot=True)
    assert not a.valid and "bot" in a.reason


def test_hash_mismatch_invalid_and_not_bound_to_subject(verifier, incident):
    a = _verify(verifier, incident, provided_hash="deadbeef")
    assert not a.valid and a.subject_hash == "deadbeef"


def test_short_hash_prefix_rejected(verifier, incident):
    a = _verify(verifier, incident, provided_hash=SUBJECT[:4])
    assert not a.valid


def test_reject_verdict_is_valid_record(verifier, incident):
    a = _verify(verifier, incident, verdict="reject")
    assert a.valid and a.verdict == "reject"


def test_approval_before_request_invalid(verifier, incident):
    t = now()
    a = _verify(verifier, incident, requested_at=t, ts=t - timedelta(seconds=5))
    assert not a.valid and "predates" in a.reason


def test_service_owner_is_authorized(config, tmp_path, incident):
    config.catalog["checkout"].owners.append("U_OWNER")
    v = ApprovalVerifier(config, Settings(var_dir=tmp_path))
    assert _verify(v, incident, user_id="U_OWNER").valid


# ---------------------------------------------------------------- poller


class FakeSlack:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def replies(self, channel, thread_ts, oldest=None):
        self.calls.append((channel, thread_ts, oldest))
        return self.messages

    async def auth_user(self):
        return "U_BOT"


async def test_poller_records_every_attempt_once(store, verifier, incident):
    msgs = [
        {"ts": "1000.000100", "user": "U_BOT", "bot_id": "B1", "text": f"approve {SUBJECT[:8]} (card)"},  # parent
        {"ts": "1000.000050", "user": "U_ONCALL_1", "text": f"approve {SUBJECT[:8]}"},  # before request
        {"ts": "1001.000000", "user": "U_INTRUDER", "text": f"approve {SUBJECT[:8]}"},
        {"ts": "1002.000000", "user": "U_ONCALL_1", "text": "looks fine, resolve it"},
        {"ts": "1003.000000", "user": "U_BOT", "text": f"approve {SUBJECT[:8]}"},
        {"ts": "1004.000000", "user": "U_ONCALL_1", "text": f"approve {SUBJECT[:8]}"},
    ]
    poller = ApprovalPoller(store, FakeSlack(msgs), verifier)
    got = await poller.poll(incident, "fix", SUBJECT, requested_at_ts="1000.000100")
    assert [(a.user_id, a.valid) for a in got] == [("U_INTRUDER", False), ("U_BOT", False), ("U_ONCALL_1", True)]
    assert all(a.via == "text" for a in got)
    assert len(store.approvals(incident.id, "fix")) == 3

    again = await poller.poll(incident, "fix", SUBJECT, requested_at_ts="1000.000100")
    assert again == [] and len(store.approvals(incident.id, "fix")) == 3


async def test_poller_stale_hash_after_plan_change(store, verifier, incident):
    msgs = [{"ts": "1001.000000", "user": "U_ONCALL_1", "text": "approve 1a2b3c4d"}]
    got = await ApprovalPoller(store, FakeSlack(msgs), verifier).poll(
        incident, "fix", "ffffeeee00001111", requested_at_ts="1000.000100")
    assert len(got) == 1 and not got[0].valid and "does not match" in got[0].reason


async def test_poller_without_channel_returns_nothing(store, verifier):
    inc = Incident(incident_key="k", environment=Env.production, services=["checkout"])
    assert await ApprovalPoller(store, FakeSlack([]), verifier).poll(inc, "fix", SUBJECT, "1.0") == []


# ---------------------------------------------------------------- buttons (socket mode path)


def _click(user, items, choice, incident_id, is_bot=False):
    return {"user": {"id": user, "is_bot": is_bot},
            "actions": [{"action_id": f"ij_card:{choice}",
                         "value": json.dumps({"incident_id": incident_id, "items": items, "choice": choice})}]}


PUBLIC = "9f8e7d6c5b4a3928"


def test_card_buttons_use_same_verifier_and_reject_stale_cards(store, verifier, incident):
    lookup = {incident.id: incident}.get
    live = lambda inc, kind, clicked: SUBJECT if kind == "fix" else PUBLIC  # noqa: E731
    items = [{"kind": "public_post", "hash": PUBLIC}, {"kind": "fix", "hash": SUBJECT}]

    approvals, outcome = handle_card_action(store, verifier, lookup, live, _click("U_ONCALL_1", items, "all", incident.id))
    assert [(a.kind, a.verdict, a.valid, a.via) for a in approvals] == [
        ("public_post", "approve", True, "button"), ("fix", "approve", True, "button")]
    assert "decided by <@U_ONCALL_1>" in outcome

    approvals, _ = handle_card_action(store, verifier, lookup, live, _click("U_ONCALL_1", items, "fix_only", incident.id))
    assert {(a.kind, a.verdict) for a in approvals} == {("public_post", "reject"), ("fix", "approve")}

    stale = [{"kind": "fix", "hash": "0000111122223333"}]
    approvals, outcome = handle_card_action(store, verifier, lookup, live, _click("U_ONCALL_1", stale, "approve", incident.id))
    assert not approvals[0].valid and "not accepted" in outcome

    approvals, outcome = handle_card_action(store, verifier, lookup, live, _click("U_INTRUDER", items, "all", incident.id))
    assert not any(a.valid for a in approvals) and "not accepted" in outcome

    bad = {"user": {"id": "U_ONCALL_1"}, "actions": [{"value": "nope"}]}
    assert handle_card_action(store, verifier, lookup, live, bad)[0] == []


def test_bundled_card_explains_every_choice_and_keeps_typed_fallback(incident):
    items = [CardItem("public_post", PUBLIC, "Post to the public status page"),
             CardItem("fix", SUBJECT, "Fix: toggle_flag", ["Why: runbook matched"])]
    text, blocks = build_card(incident, items, oncall=["U_ONCALL_1"], timeout_min=10, title="Action needed")
    assert f"[IJ-PUBLIC] Post to the public status page — `approve {PUBLIC[:8]}`" in text
    assert f"[IJ-FIX] Fix: toggle_flag — `approve {SUBJECT[:8]}`" in text
    for label in ("Approve all", "Fix only", "Post only", "Reject"):
        assert label in text
    assert "No answer within 10 min" in text and "<@U_ONCALL_1>" in text
    buttons = blocks[-1]["elements"]
    assert [json.loads(b["value"])["choice"] for b in buttons] == ["all", "fix_only", "post_only", "reject"]
    assert verdicts_for([{"kind": "public_post", "hash": PUBLIC}, {"kind": "fix", "hash": SUBJECT}], "post_only") == \
        {"public_post": "approve", "fix": "reject"}


def test_eval_parser_reads_every_item_of_a_bundled_card(incident):
    from evals.graders.common import card_items

    items = [CardItem("public_post", PUBLIC, "Post"), CardItem("fix", SUBJECT, "Fix")]
    text, _ = build_card(incident, items, oncall=[], timeout_min=10, title="t")
    assert card_items(text) == [("public_post", PUBLIC[:8]), ("fix", SUBJECT[:8])]


# ---------------------------------------------------------------- cards


def test_fix_card_contents(incident, config):
    spec = config.actions["scale_pool"]
    plan = Plan(incident_id=incident.id, runbook_id="db-pool-starved", action="scale_pool", params={"size": 20},
                target_service="checkout", prev_state={"pool_size": 2},
                verify=VerifySpec(conditions=[c.bind("checkout") for c in spec.verify.conditions]),
                autonomy_level=AutonomyLevel.L1)

    class RB:
        class frontmatter:
            id = "db-pool-starved"
            title = "DB pool starved at /srv/app/db.py"  # forbidden content must be redacted
            stats = RunbookStats(success=3)

    text, blocks = fix_card(incident, plan, RB, AutonomyLevel.L1)
    assert f"approve {plan.plan_hash[:8]}" in text and "scale_pool" in text and "pool_size=2" in text
    assert find_forbidden(text) == []
    assert blocks[-1]["type"] == "actions"
    assert json.loads(blocks[-1]["elements"][0]["value"])["subject_hash"] == plan.plan_hash

    text3, blocks3 = fix_card(incident, plan, RB, AutonomyLevel.L3)
    assert all(b["type"] != "actions" for b in blocks3)


def test_public_post_card(incident):
    text, blocks = public_post_card(incident, "major_outage", SUBJECT)
    assert f"approve {SUBJECT[:8]}" in text and "No answer = not published" in text
    assert blocks[-1]["type"] == "actions"
