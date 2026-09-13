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
