"""Discovery / cleanup calls used by `judge bootstrap` and `judge doctor`, exercised against the sandbox."""

import httpx
import pytest

from judge.connectors.instatus import InstatusClient
from judge.connectors.linear import Q_ISSUE_LABEL_CREATE, Q_ISSUE_LABELS, Q_TEAMS, Q_VIEWER, LinearClient
from judge.connectors.sentry import SentryClient
from judge.connectors.slack import SlackClient
from judge.connectors.transport import ConnectorError, HttpClient


async def test_linear_viewer_teams_labels(settings, http, uid):
    client = LinearClient(settings, http)
    viewer = await client.viewer()
    assert viewer["id"] and "email" in viewer
    teams = await client.teams()
    team = next(t for t in teams if t["id"] == settings.linear_team_id)
    assert team["key"]

    name = f"ij-test-{uid}"
    label_id = await client.create_label(name, team["id"])
    labels = await client.labels()
    assert any(l["id"] == label_id and l["name"] == name and l["team"]["id"] == team["id"] for l in labels)

    issue_id = await client.create_issue(f"label test {uid}", f"IJ-KEY:{uid}lbl", 4, [label_id])
    assert [i["id"] for i in await client.issues_by_label(label_id)] == [issue_id]
    await client.delete_issue(issue_id)

    with pytest.raises(ConnectorError):
        await client.create_label(name, team["id"])  # duplicate name


async def test_linear_documents_match_real_schema():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        captured.append({"auth": request.headers.get("authorization"), **body})
        op = body["operationName"]
        data = {
            "Viewer": {"viewer": {"id": "u1", "name": "N", "email": "e@x.io"}},
            "Teams": {"teams": {"nodes": [{"id": "t1", "key": "ENG", "name": "Eng"}]}},
            "IssueLabels": {"issueLabels": {"nodes": []}},
            "IssueLabelCreate": {"issueLabelCreate": {"success": True, "issueLabel": {"id": "l1", "name": "ij-eval"}}},
        }[op]
        return httpx.Response(200, json={"data": data})

    from judge.settings import Settings

    s = Settings.from_env(backend="real", linear_api_key="lin_api_x", linear_team_id="t1")
    http = HttpClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client = LinearClient(s, http)
    await client.viewer()
    await client.teams()
    await client.labels()
    assert await client.create_label("ij-eval", "t1") == "l1"
    assert all(c["auth"] == "lin_api_x" for c in captured)  # no Bearer
    assert [c["query"] for c in captured] == [Q_VIEWER, Q_TEAMS, Q_ISSUE_LABELS, Q_ISSUE_LABEL_CREATE]
    assert captured[-1]["variables"] == {"input": {"name": "ij-eval", "teamId": "t1", "color": "#6B7280"}}
    await http.aclose()


async def test_sentry_org_projects_create_dsn_ingest_resolve(settings, http, uid, sandbox_url):
    client = SentryClient(settings, http)
    org = await client.organization()
    assert org["slug"] == settings.sentry_org
    slugs = {p["slug"] for p in await client.projects()}
    assert {"shoplab-prod", "shoplab-staging"} <= slugs
    team = (await client.teams())[0]["slug"]

    dsn_prod = await client.project_dsn("shoplab-prod")
    assert dsn_prod.endswith("/1") and "@" in dsn_prod

    slug = f"ij-{uid}"
    proj = await client.create_project(team, slug, slug)
    assert proj["slug"] == slug
    dsn = await client.project_dsn(slug)
    assert dsn.endswith(f"/{proj['id']}")
    with pytest.raises(ConnectorError):
        await client.create_project(team, slug, slug)  # duplicate slug

    # the new project's DSN accepts events and they become searchable issues
    project_id = dsn.rsplit("/", 1)[-1]
    r = await http.request("sentry", "store", "POST", f"{sandbox_url}/api/{project_id}/store/", json={
        "event_id": uid * 4, "level": "error", "environment": "production", "tags": {"ij_trial": f"d{uid}"},
        "exception": {"values": [{"type": "DoctorProbe", "value": "connectivity check"}]},
        "transaction": "/doctor", "fingerprint": ["doctor", uid],
    })
    assert r.status_code == 200
    issues = await client.list_issues("production", query=f"is:unresolved ij_trial:d{uid}", trial_id="")
    assert len(issues) == 1 and issues[0]["project"]["slug"] == slug
    await client.resolve_issue(issues[0]["id"])
    assert (await client.get_issue(issues[0]["id"]))["status"] == "resolved"
    assert await client.list_issues("production", query=f"is:unresolved ij_trial:d{uid}", trial_id="") == []

    with pytest.raises(ConnectorError):
        await client.project_dsn("does-not-exist")


async def test_instatus_pages_components(settings, http, uid):
    client = InstatusClient(settings, http)
    pages = await client.pages()
    assert any(p["id"] == settings.instatus_page_id for p in pages)
    before = {c["id"] for c in await client.components()}
    assert "comp_checkout" in before
    cid = await client.create_component(f"Doctor {uid}", "connectivity check")
    comps = {c["id"]: c for c in await client.components()}
    assert cid not in before and comps[cid]["name"] == f"Doctor {uid}" and comps[cid]["status"] == "OPERATIONAL"


async def test_instatus_pages_falls_back_to_v1():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v2/pages":
            return httpx.Response(404, json={"message": "Not found"})
        return httpx.Response(200, json=[{"id": "p1", "subdomain": "x"}])

    from judge.settings import Settings

    s = Settings.from_env(backend="real", instatus_api_key="k", instatus_page_id="p1")
    http = HttpClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await InstatusClient(s, http).pages() == [{"id": "p1", "subdomain": "x"}]
    assert seen == ["/v2/pages", "/v1/pages"]
    await http.aclose()


async def test_slack_auth_info_and_delete_message(settings, http, uid):
    bot = SlackClient(settings, http)
    oncall = SlackClient(settings, http, token="xoxp-oncall1")
    info = await bot.auth_info()
    assert info["user_id"] == "U_BOT" and info.get("bot_id") and info["team"]
    assert "bot_id" not in await oncall.auth_info()

    channel = settings.slack_oncall_channel
    ts = await bot.post(channel, f"doctor IJ-KEY:{uid}")
    await oncall.join(channel)
    with pytest.raises(ConnectorError) as e:  # only the author may delete
        await oncall.delete_message(channel, ts)
    assert e.value.body["error"] == "cant_delete_message"
    await bot.delete_message(channel, ts)
    assert await bot.find_message_by_marker(channel, f"IJ-KEY:{uid}") is None
    with pytest.raises(ConnectorError) as e:
        await bot.delete_message(channel, ts)
    assert e.value.body["error"] == "message_not_found"
