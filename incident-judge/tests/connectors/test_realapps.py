"""`judge bootstrap` / `judge doctor` end to end against the sandbox (the real APIs share the same shapes)."""
from judge.realapps import bootstrap, doctor, update_env_file
from judge.settings import Config


async def test_bootstrap_discovers_every_id(settings):
    rep = await bootstrap(settings, Config(), channel_name="ij-bootstrap-test")
    assert rep.ok, [c for c in rep.checks if not c.ok]
    assert {"SENTRY_DSN_PROD", "SENTRY_DSN_STAGING", "LINEAR_TEAM_ID", "LINEAR_EVAL_LABEL_ID",
            "INSTATUS_COMPONENTS", "SLACK_ONCALL_CHANNEL"} <= set(rep.env_updates)


async def test_doctor_write_read_cleanup(settings, monkeypatch):
    settings.anthropic_api_key = ""  # no network to Anthropic in tests
    rep = await doctor(settings, Config())
    failed = [c for c in rep.checks if not c.ok and c.app != "claude"]
    assert not failed, failed
    steps = {(c.app, c.step) for c in rep.checks if c.ok}
    for app in ("sentry", "linear", "instatus", "slack"):
        assert (app, "cleanup") in steps


def test_update_env_file_preserves_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nANTHROPIC_API_KEY=keep\nLINEAR_TEAM_ID=old\n", encoding="utf-8")
    update_env_file({"LINEAR_TEAM_ID": "new", "SLACK_ONCALL_CHANNEL": "C1"}, env)
    text = env.read_text(encoding="utf-8")
    assert "# comment" in text and "ANTHROPIC_API_KEY=keep" in text
    assert "LINEAR_TEAM_ID=new" in text and "SLACK_ONCALL_CHANNEL=C1" in text and "old" not in text


async def test_optional_pagerduty_and_github_reported_when_unconfigured(settings):
    rep = await bootstrap(settings, Config(), channel_name="ij-bootstrap-test")
    by_app = {c.app: c for c in rep.checks}
    assert by_app["pagerduty"].ok and "optional" in by_app["pagerduty"].detail
    assert by_app["github"].ok and "optional" in by_app["github"].detail


async def test_pagerduty_and_github_checks_against_mocked_apis():
    import json

    import httpx

    from judge.connectors.transport import HttpClient
    from judge.realapps import Report, _bootstrap_pagerduty, _check_github, _doctor_pagerduty
    from judge.settings import Settings

    events = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "events.pagerduty.com":
            body = json.loads(req.content)
            events.append(body["event_action"])
            return httpx.Response(202, json={"status": "success", "dedup_key": body["dedup_key"]})
        if req.url.path == "/repos/o/r":
            return httpx.Response(200, json={"full_name": "o/r", "private": True, "default_branch": "main",
                                             "permissions": {"pull": True, "push": True}})
        if req.url.path == "/repos/o/r/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "abc12345deadbeef"}})
        return httpx.Response(404, json={})

    s = Settings.from_env(backend="real", trial_id=None).model_copy(update={
        "pagerduty_routing_key": "c" * 32, "github_token": "t", "github_memory_repo": "o/r"})
    http = HttpClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rep = Report()
    _bootstrap_pagerduty(s, rep)
    await _doctor_pagerduty(s, http, rep, "t1")
    assert await _check_github(s, http, rep)
    assert rep.ok, [c for c in rep.checks if not c.ok]
    assert events == ["trigger", "resolve"]
    assert {("github", "push + pull requests"), ("pagerduty", "cleanup")} <= {(c.app, c.step) for c in rep.checks}

    bad = Report()
    _bootstrap_pagerduty(s.model_copy(update={"pagerduty_routing_key": "short"}), bad)
    assert not bad.ok
