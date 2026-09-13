"""Real sentry-sdk 2.x -> sandbox envelope ingest -> org issues API shapes and query filters."""

import gzip
import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import sentry_sdk

from judge.connectors.sentry import SentryClient
from judge.connectors.transport import HttpClient

AUTH = {"Authorization": "Bearer sandbox"}


def _dsn(sandbox_url: str, project: int) -> str:
    return sandbox_url.replace("http://", "http://sandboxkey@") + f"/{project}"


def _send_with_sdk(sandbox_url, project, environment, trial, users, service="checkout", exc_type=ConnectionError,
                   message="payment provider v2 unreachable"):
    def before_send(event, hint):
        exc = event["exception"]["values"][-1]["type"]
        event["fingerprint"] = [exc, event.get("transaction", ""), environment, service, trial]
        return event

    sentry_sdk.init(dsn=_dsn(sandbox_url, project), environment=environment, release="1.4.1",
                    default_integrations=False, traces_sample_rate=0, before_send=before_send)
    try:
        for user in users:
            with sentry_sdk.isolation_scope() as scope:
                scope.set_tag("service", service)
                scope.set_tag("ij_trial", trial)
                scope.set_user({"id": user})
                scope.set_transaction_name("/pay")
                try:
                    raise exc_type(message)
                except Exception as e:  # noqa: BLE001
                    sentry_sdk.capture_exception(e)
        sentry_sdk.flush(timeout=10)
    finally:
        sentry_sdk.get_client().close(timeout=2)


def _wait_issues(url, params, expect, timeout=10.0):
    deadline = time.time() + timeout
    issues = []
    while time.time() < deadline:
        issues = httpx.get(url, params=params, headers=AUTH).json()
        if expect(issues):
            return issues
        time.sleep(0.2)
    return issues


def test_sdk_events_grouped_by_fingerprint_with_real_shapes(sandbox_url, uid):
    trial = f"t{uid}"
    _send_with_sdk(sandbox_url, 1, "production", trial, ["u1", "u2", "u1"])
    url = f"{sandbox_url}/api/0/organizations/shoplab/issues/"
    issues = _wait_issues(url, {"query": f"is:unresolved lastSeen:-10m ij_trial:{trial}", "environment": "production"},
                          lambda xs: xs and xs[0]["count"] == "3")
    assert len(issues) == 1
    issue = issues[0]
    assert issue["count"] == "3" and isinstance(issue["count"], str)
    assert issue["userCount"] == 2 and isinstance(issue["userCount"], int)
    assert issue["metadata"]["type"] == "ConnectionError"
    assert issue["culprit"] == "/pay"
    assert issue["project"]["slug"] == "shoplab-prod"
    assert isinstance(issue["tags"], list) and {"key", "name", "totalValues"} <= set(issue["tags"][0])

    detail = httpx.get(f"{url}{issue['id']}/", headers=AUTH).json()
    assert detail["id"] == issue["id"]
    event = httpx.get(f"{url}{issue['id']}/events/latest/", headers=AUTH).json()
    tags = {t["key"]: t["value"] for t in event["tags"]}
    assert tags["service"] == "checkout" and tags["environment"] == "production" and tags["ij_trial"] == trial
    assert isinstance(event["tags"], list)


def test_trials_and_environments_do_not_share_issues(sandbox_url, uid):
    t1, t2 = f"a{uid}", f"b{uid}"
    _send_with_sdk(sandbox_url, 1, "production", t1, ["x"])
    _send_with_sdk(sandbox_url, 1, "production", t2, ["y"])
    _send_with_sdk(sandbox_url, 2, "staging", t1, ["z"],
                   message="CRITICAL: database connection pool exhausted", exc_type=TimeoutError)
    url = f"{sandbox_url}/api/0/organizations/shoplab/issues/"
    prod_t1 = _wait_issues(url, {"query": f"ij_trial:{t1}", "environment": "production"}, lambda xs: len(xs) >= 1)
    assert len(prod_t1) == 1 and prod_t1[0]["metadata"]["type"] == "ConnectionError"
    staging_t1 = _wait_issues(url, {"query": f"ij_trial:{t1}", "environment": "staging"}, lambda xs: len(xs) >= 1)
    assert len(staging_t1) == 1 and staging_t1[0]["project"]["slug"] == "shoplab-staging"
    assert "CRITICAL" in staging_t1[0]["title"]
    both = httpx.get(url, params={"query": f"ij_trial:{t2}"}, headers=AUTH).json()
    assert len(both) == 1


def test_last_seen_filter_and_store_endpoint(sandbox_url, uid):
    old = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    event = {"event_id": uid * 4, "timestamp": old, "level": "error", "environment": "production",
             "transaction": "/search", "tags": {"service": "search", "ij_trial": f"old{uid}"},
             "fingerprint": ["TimeoutError", "/search", "production", "search", f"old{uid}"],
             "exception": {"values": [{"type": "TimeoutError", "value": "slow"}]}}
    r = httpx.post(f"{sandbox_url}/api/1/store/", content=json.dumps(event))
    assert r.status_code == 200
    url = f"{sandbox_url}/api/0/organizations/shoplab/issues/"
    recent = httpx.get(url, params={"query": f"lastSeen:-10m ij_trial:old{uid}"}, headers=AUTH).json()
    assert recent == []
    hour = httpx.get(url, params={"query": f"lastSeen:-1h ij_trial:old{uid}"}, headers=AUTH).json()
    assert len(hour) == 1


def test_raw_envelope_with_length_headers_and_gzip(sandbox_url, uid):
    payload = json.dumps({"event_id": uid * 4, "environment": "production", "transaction": "/products/{id}",
                          "tags": {"service": "catalog", "ij_trial": f"env{uid}"},
                          "exception": {"values": [{"type": "KeyError", "value": "'currency'"}]}}).encode()
    body = (json.dumps({"event_id": uid * 4}).encode() + b"\n"
            + json.dumps({"type": "client_report"}).encode() + b"\n" + b"{}" + b"\n"
            + json.dumps({"type": "event", "length": len(payload)}).encode() + b"\n" + payload + b"\n")
    r = httpx.post(f"{sandbox_url}/api/1/envelope/", content=gzip.compress(body),
                   headers={"Content-Encoding": "gzip", "Content-Type": "application/x-sentry-envelope"})
    assert r.status_code == 200
    issues = httpx.get(f"{sandbox_url}/api/0/organizations/shoplab/issues/",
                       params={"query": f"ij_trial:env{uid}"}, headers=AUTH).json()
    assert len(issues) == 1 and issues[0]["culprit"] == "/products/{id}"


def test_org_api_requires_bearer(sandbox_url):
    r = httpx.get(f"{sandbox_url}/api/0/organizations/shoplab/issues/")
    assert r.status_code == 401


async def test_sentry_connector_to_signal(sandbox_url, settings, uid):
    trial = f"sig{uid}"
    _send_with_sdk(sandbox_url, 1, "production", trial, ["u1", "u2"])
    s = settings.model_copy(update={"trial_id": trial})
    async with HttpClient() as http:
        client = SentryClient(s, http)
        deadline = time.time() + 10
        issues = []
        while time.time() < deadline and not issues:
            issues = await client.list_issues("production")
            time.sleep(0.2)
        assert len(issues) == 1  # scoped to the trial automatically
        issue = await client.get_issue(issues[0]["id"])
        event = await client.latest_event(issue["id"])
        sig = client.to_signal(issue, event, trial)
    assert sig.service == "checkout" and sig.environment.value == "production"
    assert sig.error_type == "ConnectionError" and sig.culprit == "/pay"
    assert sig.count == 2 and sig.user_count == 2 and len(sig.fingerprint) == 12
    assert sig.trial_id == trial
