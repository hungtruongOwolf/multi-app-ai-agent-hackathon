"""Tests never touch real accounts: force the sandbox backend and placeholder credentials before any
module reads .env (load_dotenv never overrides variables that are already set)."""
import os

for key, value in {
    "IJ_BACKEND": "sandbox",
    "IJ_JUDGE_IMPL": "heuristic",
    "ANTHROPIC_API_KEY": "",
    "SENTRY_ORG": "shoplab",
    "SENTRY_TOKEN": "sandbox",
    "LINEAR_API_KEY": "sandbox",
    "LINEAR_TEAM_ID": "team_shoplab",
    "LINEAR_EVAL_LABEL_ID": "label_ij_eval",
    "INSTATUS_API_KEY": "sandbox",
    "INSTATUS_PAGE_ID": "page_shoplab",
    "INSTATUS_COMPONENTS": "",
    "SLACK_BOT_TOKEN": "xoxb-sandbox",
    "SLACK_ONCALL_CHANNEL": "C_ONCALL",
    "ONCALL_SLACK_USER_IDS": "",
    "SENTRY_DSN_PROD": "",
    "SENTRY_DSN_STAGING": "",
    "SLACK_APP_TOKEN": "",
    "INSTATUS_SHOULD_PUBLISH": "false",
    "PAGERDUTY_ROUTING_KEY": "",
    "PAGERDUTY_API_TOKEN": "",
    "GITHUB_TOKEN": "",
    "INSTATUS_COMPONENTS": "",
}.items():
    os.environ[key] = value
