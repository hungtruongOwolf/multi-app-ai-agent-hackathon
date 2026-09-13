import asyncio

import pytest

from judge.core.models import (
    CustomerImpact,
    Decision,
    DecisionResult,
    Env,
    Incident,
    IncidentState,
)
from judge.core.outbox import Outbox, PolicyDenied, marker, parse_marker
from judge.core.store import Store
from judge.safety.redact import find_forbidden, redact
from judge.safety.templates import public_message, public_ref, public_title
from judge.safety.redact import UnsafeWrite
from judge.settings import Config


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.mark.parametrize("text,kind", [
    ("user ij-canary-t1@example.com failed", "email"),
    ('File "/srv/app/billing.py", line 220, in pay', "py_traceback"),
    ("at Object.<anonymous> (/app/x.js:10:15)", "stack_frame"),
    ("connect db-1.prod.svc.cluster.local:5432", "internal_host"),
    ("token xoxb-ijcanaryt10000 leaked", "slack_token"),
    ("key sk-proj-abcdefghijklmnop", "api_key"),
    ("host 10.0.3.4", "ipv4"),
])
def test_redactor_catches(text, kind):
    assert kind in find_forbidden(text)
    assert find_forbidden(redact(text)) == []


def test_internal_host_fully_redacted():
    out = redact("db-1.prod.svc.cluster.local")
    assert "db-1" not in out


def test_public_templates_allowlist():
    cfg = Config()
    ref = public_ref("0123456789abcdef", "trial-XY_1")
    msg = public_message("investigating", [cfg.catalog["checkout"]], ref)
    assert "Checkout & payments" in msg and ref in msg
    assert public_title(CustomerImpact.major_outage, [cfg.catalog["checkout"]]).startswith("Outage")
    with pytest.raises(UnsafeWrite):
        public_message("investigating", [cfg.catalog["internal-batch"]], ref)
    with pytest.raises(UnsafeWrite):
        public_message("investigating", [cfg.catalog["checkout"]], "not a ref")


def test_marker_roundtrip():
    mk = marker("abcd", "inc_1", "t9")
    assert parse_marker(mk) == ("abcd", "inc_1", "t9")


def test_store_incident_lifecycle(store):
    inc = Incident(incident_key="fp1", environment=Env.production, services=["checkout"])
    store.save_incident(inc)
    assert store.open_incident_by_key("fp1").id == inc.id
    store.transition(inc, IncidentState.RESOLVED)
    assert store.open_incident_by_key("fp1") is None


def test_locks(store):
    assert store.acquire_lock("service:checkout", "a", 30)
    assert not store.acquire_lock("service:checkout", "b", 30)
    store.release_lock("service:checkout", "a")
    assert store.acquire_lock("service:checkout", "b", 30)


def _allow():
    return Decision(intent="linear.create_issue", result=DecisionResult.ALLOW)


def test_outbox_ghost_write_reconciles(store):
    external: dict[str, str] = {}
    calls = {"n": 0}

    async def reconcile(mk):
        return external.get(mk)

    async def execute_ghost(mk):
        calls["n"] += 1
        external[mk] = "ISSUE-1"  # the write landed...
        raise TimeoutError("...but the response was lost")

    async def execute_ok(mk):
        calls["n"] += 1
        external[mk] = f"ISSUE-{calls['n']}"
        return external[mk]

    ob = Outbox(store, "t1")

    async def scenario():
        with pytest.raises(TimeoutError):
            await ob.run(app="linear", op="create_issue", incident_id="inc_1", scope="", decision=_allow(),
                         reconcile=reconcile, execute=execute_ghost)
        res = await ob.run(app="linear", op="create_issue", incident_id="inc_1", scope="", decision=_allow(),
                           reconcile=reconcile, execute=execute_ok)
        assert res.reconciled and res.ref == "ISSUE-1"
        again = await ob.run(app="linear", op="create_issue", incident_id="inc_1", scope="", decision=_allow(),
                             reconcile=reconcile, execute=execute_ok)
        assert again.skipped and again.ref == "ISSUE-1"

    asyncio.run(scenario())
    assert calls["n"] == 1  # never created twice


def test_outbox_refuses_denied(store):
    ob = Outbox(store, None)

    async def go():
        await ob.run(app="x", op="y", incident_id=None, scope="", reconcile=None, execute=None,
                     decision=Decision(intent="x.y", result=DecisionResult.DENY, rules=["P1"]))

    with pytest.raises(PolicyDenied):
        asyncio.run(go())
