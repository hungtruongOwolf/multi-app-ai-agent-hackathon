import json

import httpx
import pytest

from judge.connectors.linear import LinearClient
from judge.connectors.transport import ConnectorError, HttpClient, ToolFaultPlan
from judge.core.models import Decision, DecisionResult
from judge.core.outbox import Outbox
from judge.core.store import Store


async def test_error_fault_is_not_sent(settings, uid):
    plan = ToolFaultPlan.from_list([{"app": "linear", "op": "create_issue", "mode": "error", "count": 1,
                                     "status": 503}])
    marker = f"IJ-KEY:err{uid}"
    async with HttpClient(fault_plan=plan) as http:
        client = LinearClient(settings, http)
        with pytest.raises(ConnectorError) as e:
            await client.create_issue("t", marker, 2, [])
        assert e.value.status == 503 and e.value.retryable
        assert await client.find_issue_by_marker(marker) is None  # nothing landed
        issue_id = await client.create_issue("t", marker, 2, [])  # count exhausted -> real call
        assert await client.find_issue_by_marker(marker) == issue_id
        await client.delete_issue(issue_id)


async def test_ghost_write_lands_but_client_times_out(settings, uid):
    plan = ToolFaultPlan.from_list([{"app": "linear", "op": "create_issue", "mode": "ghost_write"}])
    marker = f"IJ-KEY:ghost{uid}"
    async with HttpClient(fault_plan=plan) as http:
        client = LinearClient(settings, http)
        with pytest.raises(httpx.ReadTimeout):
            await client.create_issue("ghost", marker, 1, [])
        landed = await client.issues_by_marker(marker)
        assert len(landed) == 1  # the write really happened
        await client.delete_issue(landed[0]["id"])


async def test_outbox_reconciles_ghost_write_without_duplicate(settings, tmp_path, uid):
    plan = ToolFaultPlan.from_list([{"app": "linear", "op": "create_issue", "mode": "ghost_write"}])
    store = Store(tmp_path / "judge.db")
    outbox = Outbox(store, trial_id=f"t{uid}")
    decision = Decision(intent="linear.create_issue", result=DecisionResult.ALLOW, incident_id=f"inc_{uid}")
    async with HttpClient(fault_plan=plan) as http:
        client = LinearClient(settings, http)

        async def reconcile(mk):
            return await client.find_issue_by_marker(mk)

        async def execute(mk):
            return await client.create_issue("Checkout outage", f"desc\n{mk}", 1, [])

        kw = dict(app="linear", op="create_issue", incident_id=f"inc_{uid}", scope="", decision=decision,
                  reconcile=reconcile, execute=execute)
        with pytest.raises(httpx.ReadTimeout):
            await outbox.run(**kw)
        result = await outbox.run(**kw)  # retry: reconcile finds the ghost write
        assert result.reconciled and result.ref
        assert len(await client.issues_by_marker(f"IJ-TRIAL:t{uid}")) == 1
        again = await outbox.run(**kw)
        assert again.skipped and again.ref == result.ref
        await client.delete_issue(result.ref)
    store.close()


async def test_latency_and_plan_file_and_after(tmp_path, monkeypatch):
    path = tmp_path / "faults.json"
    path.write_text(json.dumps([{"app": "slack", "op": "*", "mode": "error", "status": 500, "after": 1,
                                 "count": -1}]))
    monkeypatch.setenv("IJ_TOOL_FAULTS", str(path))
    plan = ToolFaultPlan.from_env()
    assert plan.take("slack", "post") is None  # first match skipped by `after`
    assert plan.take("slack", "post").status == 500
    assert plan.take("slack", "replies") is not None  # count -1: always
    assert plan.take("linear", "post") is None
    with pytest.raises(ValueError):
        ToolFaultPlan.from_list([{"app": "x", "op": "y", "mode": "explode"}])

    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    lat = ToolFaultPlan.from_list([{"app": "a", "op": "b", "mode": "latency", "ms": 50}])
    async with HttpClient(fault_plan=lat, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as http:
        import time
        t = time.perf_counter()
        r = await http.request("a", "b", "GET", "http://x/")
        assert r.status_code == 200 and time.perf_counter() - t >= 0.045 and len(calls) == 1
