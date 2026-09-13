"""Raw-HTTP checks of sandbox API traps and admin trial filtering."""

import httpx

LINEAR_CREATE = {"operationName": "IssueCreate", "query": "mutation IssueCreate($input: IssueCreateInput!) { x }"}


def _linear(url, variables, op="IssueCreate", auth="sandbox"):
    return httpx.post(f"{url}/linear/graphql", headers={"Authorization": auth},
                      json={"operationName": op, "query": "…", "variables": variables})


def test_linear_rejects_bearer_prefix(sandbox_url):
    r = _linear(sandbox_url, {"input": {"title": "x", "teamId": "team_shoplab"}}, auth="Bearer sandbox")
    assert r.status_code == 401
    assert r.json()["errors"][0]["extensions"]["type"] == "authentication error"


def test_linear_unknown_operation(sandbox_url):
    r = _linear(sandbox_url, {}, op="Nope")
    assert r.status_code == 400


def test_slack_errors_are_http_200_ok_false(sandbox_url):
    r = httpx.post(f"{sandbox_url}/slack/api/chat.postMessage", data={"channel": "C_ONCALL", "text": "hi"},
                   headers={"Authorization": "Bearer xoxb-wrong"})
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "invalid_auth"}


def test_status_page_renders_published_only(sandbox_url, uid):
    h = {"Authorization": "Bearer k"}
    base = f"{sandbox_url}/instatus/v1/page_shoplab/incidents"
    pub = httpx.post(base, headers=h, json={"name": f"Pub {uid}", "message": "m", "components": ["comp_search"],
                                            "status": "INVESTIGATING", "shouldPublish": True}).json()
    hid = httpx.post(base, headers=h, json={"name": f"Hidden {uid}", "message": "m", "components": [],
                                            "status": "INVESTIGATING", "shouldPublish": False}).json()
    page = httpx.get(f"{sandbox_url}/status/page_shoplab").text
    assert f"Pub {uid}" in page and f"Hidden {uid}" not in page
    for i in (pub, hid):
        httpx.delete(f"{base}/{i['id']}", headers=h)


def test_admin_state_and_reset_filter_by_trial(sandbox_url, uid):
    t1, t10 = f"t{uid}", f"t{uid}0"  # t1 is a prefix of t10: must not match
    for trial in (t1, t10):
        assert _linear(sandbox_url, {"input": {"title": trial, "teamId": "team_shoplab", "priority": 1,
                                               "description": f"body\nIJ-KEY:abc IJ-TRIAL:{trial}"}}).status_code == 200
        httpx.post(f"{sandbox_url}/instatus/v1/page_shoplab/incidents", headers={"Authorization": "Bearer k"},
                   json={"name": "Outage", "message": f"We are investigating. Ref IJ-{trial}-deadbeef",
                         "components": ["comp_checkout"], "status": "INVESTIGATING"})
        bot = {"Authorization": "Bearer xoxb-sandbox"}
        root = httpx.post(f"{sandbox_url}/slack/api/chat.postMessage", headers=bot,
                          data={"channel": "C_ONCALL", "text": f"card IJ-TRIAL:{trial}"}).json()
        httpx.post(f"{sandbox_url}/slack/api/chat.postMessage", headers={"Authorization": "Bearer xoxp-oncall1"},
                   data={"channel": "C_ONCALL", "text": "approve deadbeef", "thread_ts": root["ts"]})

    state = httpx.get(f"{sandbox_url}/__admin/state", params={"trial_id": t1}).json()
    assert [i["title"] for i in state["linear"]["issues"]] == [t1]
    assert len(state["instatus"]["incidents"]) == 1
    texts = [m["text"] for m in state["slack"]["messages"]]
    assert f"card IJ-TRIAL:{t1}" in texts and "approve deadbeef" in texts and len(texts) == 2

    removed = httpx.post(f"{sandbox_url}/__admin/reset", params={"trial_id": t1}).json()["removed"]
    assert removed["linear_issues"] == 1 and removed["instatus_incidents"] == 1 and removed["slack_messages"] == 2
    after = httpx.get(f"{sandbox_url}/__admin/state", params={"trial_id": t1}).json()
    assert after["linear"]["issues"] == [] and after["instatus"]["incidents"] == []
    still = httpx.get(f"{sandbox_url}/__admin/state", params={"trial_id": t10}).json()
    assert len(still["linear"]["issues"]) == 1
    httpx.post(f"{sandbox_url}/__admin/reset", params={"trial_id": t10})


def test_slack_ui_act_posts_command_as_user(sandbox_url, uid):
    bot = {"Authorization": "Bearer xoxb-sandbox"}
    root = httpx.post(f"{sandbox_url}/slack/api/chat.postMessage", headers=bot,
                      data={"channel": "C_ONCALL", "text": f"Fix plan. Reply `approve 1a2b3c4d` {uid}"}).json()
    page = httpx.get(f"{sandbox_url}/slack/ui/channel/C_ONCALL").text
    assert "Approve as oncall-1 (1a2b3c4d)" in page
    r = httpx.post(f"{sandbox_url}/slack/ui/act", data={"channel": "C_ONCALL", "thread_ts": root["ts"],
                                                        "hash": "1a2b3c4d", "action": "approve",
                                                        "as_user": "U_INTRUDER"})
    assert r.status_code == 303
    replies = httpx.post(f"{sandbox_url}/slack/api/conversations.replies", headers=bot,
                         data={"channel": "C_ONCALL", "ts": root["ts"]}).json()["messages"]
    assert replies[-1]["user"] == "U_INTRUDER" and replies[-1]["text"] == "approve 1a2b3c4d"
