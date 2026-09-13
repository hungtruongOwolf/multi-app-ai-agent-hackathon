"""PagerDuty Events API v2 client (mocked transport)."""

from __future__ import annotations

import json

import httpx
import pytest

from judge.connectors.pagerduty import PagerDutyClient
from judge.connectors.transport import ConnectorError, HttpClient
from judge.settings import Settings


def _client(handler, key="rk"):
    s = Settings.from_env(backend="real", trial_id=None).model_copy(update={"pagerduty_routing_key": key})
    return PagerDutyClient(s, HttpClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))


async def test_trigger_and_resolve_share_dedup_key():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(202, json={"status": "success", "dedup_key": body["dedup_key"]})

    pd = _client(handler)
    assert pd.enabled
    assert await pd.trigger("ij-inc1", "checkout down", "SEV1", "checkout", {"a": 1}) == "ij-inc1"
    assert await pd.resolve("ij-inc1") == "ij-inc1"
    assert seen[0]["payload"]["severity"] == "critical" and seen[0]["event_action"] == "trigger"
    assert seen[1] == {"routing_key": "rk", "event_action": "resolve", "dedup_key": "ij-inc1"}


async def test_disabled_without_key_and_errors_raise():
    assert not _client(lambda r: None, key="").enabled
    pd = _client(lambda r: httpx.Response(400, json={"status": "invalid event", "errors": ["bad"]}))
    with pytest.raises(ConnectorError):
        await pd.trigger("k", "s", "SEV2", "x")
