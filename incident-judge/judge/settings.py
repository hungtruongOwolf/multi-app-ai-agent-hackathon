"""Runtime settings (.env) and declarative config (config/*.yaml)."""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from judge.paths import runtime_dir
from judge.core.models import ActionSpec, AutonomyLevel, MetricCondition, ServiceEntry, VerifySpec

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


SANDBOX_IDENTITY = dict(
    sentry_org="shoplab", sentry_token="sandbox",
    sentry_projects={"production": "shoplab-prod", "staging": "shoplab-staging"},
    linear_api_key="sandbox", linear_team_id="team_shoplab", linear_eval_label_id="label_ij_eval", linear_assignee_id="", pagerduty_routing_key="", pagerduty_api_token="", pagerduty_subdomain="",
    instatus_api_key="sandbox", instatus_page_id="page_shoplab",
    slack_bot_token="xoxb-sandbox", slack_app_token="", slack_oncall_channel="C_ONCALL",
    github_token="", github_memory_repo="",
)


def real_backend() -> bool:
    return os.environ.get("IJ_BACKEND", "sandbox") == "real"


class PolicyConfig(BaseModel):
    oncall_allowlist: list[str]
    # A per-incident channel only helps when many people swarm; the incident thread in the on-call channel is the
    # default war room (one place to read, approve and audit).
    war_room_channel: bool = False
    public_approval_ttl_s: int = 600
    fix_approval_ttl_s: int = 900
    veto_window_s: int = 300
    quiet_window_s: int = 600
    correlation_window_s: int = 300
    circuit_breaker_failures: int = 2
    lock_lease_s: int = 60
    public_requires_approval_impacts: list[str] = ["major_outage"]
    public_requires_approval_severities: list[str] = ["SEV1", "SEV2"]
    public_min_impact: str = "degraded"


class AutonomyThresholds(BaseModel):
    L1_min_success: int = 2
    L2_min_success: int = 5
    L2_recent_window: int = 10
    L3_min_success: int = 10
    stale_after_days: int = 30


class Settings(BaseModel):
    backend: Literal["sandbox", "real"] = "sandbox"
    trial_id: str | None = None
    time_scale: float = 1.0
    var_dir: Path = Field(default_factory=runtime_dir)

    # sandbox
    sandbox_url: str = "http://127.0.0.1:8900"

    # sentry
    sentry_org: str = "shoplab"
    sentry_token: str = ""
    sentry_projects: dict[str, str] = {"production": "shoplab-prod", "staging": "shoplab-staging"}
    # linear
    linear_api_key: str = ""
    linear_team_id: str = ""
    linear_eval_label_id: str = ""
    linear_assignee_id: str = ""
    pagerduty_routing_key: str = ""
    pagerduty_api_token: str = ""   # optional, read-only REST key: links and live status of the PagerDuty incident
    pagerduty_subdomain: str = ""   # e.g. "acme" for https://acme.pagerduty.com
    # instatus
    instatus_api_key: str = ""
    instatus_page_id: str = ""
    instatus_should_publish: bool = False
    # slack
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_oncall_channel: str = ""
    # memory
    memory_backend: Literal["local_git", "github"] = "local_git"
    github_token: str = ""
    github_memory_repo: str = ""
    # shoplab
    shoplab_supervisor_url: str = "http://127.0.0.1:8800"
    shoplab_control_token: str = "dev-control-token"
    # llm
    anthropic_api_key: str = ""
    judge_model: str = "claude-sonnet-5"
    memory_model: str = "claude-opus-5"
    judge_impl: Literal["claude", "heuristic"] = "claude"

    @property
    def db_path(self) -> Path:
        name = f"judge-{self.trial_id}.db" if self.trial_id else "judge.db"
        return self.var_dir / name

    @property
    def memory_dir(self) -> Path:
        return self.var_dir / ("memory" if not self.trial_id else f"memory-{self.trial_id}")

    def scaled(self, seconds: float) -> float:
        return max(1.0, seconds * self.time_scale)

    def human_window(self, seconds: float) -> float:
        """Windows a human must act in (approvals, veto). TIME_SCALE compresses machine time for evals, but with
        real people on real apps a 2-minute approval window just times out, so real mode keeps at least 10 min."""
        scaled = seconds * self.time_scale
        return max(scaled, 600.0) if self.backend == "real" else scaled

    # base urls
    @property
    def sentry_base(self) -> str:
        return self.sandbox_url if self.backend == "sandbox" else "https://sentry.io"

    @property
    def linear_url(self) -> str:
        return f"{self.sandbox_url}/linear/graphql" if self.backend == "sandbox" else "https://api.linear.app/graphql"

    @property
    def instatus_base(self) -> str:
        return f"{self.sandbox_url}/instatus" if self.backend == "sandbox" else "https://api.instatus.com"

    @property
    def slack_base(self) -> str:
        return f"{self.sandbox_url}/slack/api" if self.backend == "sandbox" else "https://slack.com/api"

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        load_dotenv(ROOT / ".env", override=False)
        data = dict(
            backend=_env("IJ_BACKEND", "sandbox"),
            trial_id=_env("IJ_TRIAL_ID") or None,
            time_scale=float(_env("TIME_SCALE", "1.0")),
            sandbox_url=_env("IJ_SANDBOX_URL", "http://127.0.0.1:8900"),
            sentry_org=_env("SENTRY_ORG", "shoplab"),
            sentry_token=_env("SENTRY_TOKEN", "sandbox"),
            sentry_projects={"production": _env("SENTRY_PROJECT_PROD", "shoplab-prod"),
                             "staging": _env("SENTRY_PROJECT_STAGING", "shoplab-staging")},
            linear_api_key=_env("LINEAR_API_KEY", "sandbox"),
            linear_team_id=_env("LINEAR_TEAM_ID", "team_shoplab"),
            linear_eval_label_id=_env("LINEAR_EVAL_LABEL_ID", "label_ij_eval"),
            linear_assignee_id=_env("LINEAR_ASSIGNEE_ID", ""),
            pagerduty_routing_key=_env("PAGERDUTY_ROUTING_KEY", ""),
            pagerduty_api_token=_env("PAGERDUTY_API_TOKEN", ""),
            pagerduty_subdomain=_env("PAGERDUTY_SUBDOMAIN", ""),
            instatus_api_key=_env("INSTATUS_API_KEY", "sandbox"),
            instatus_page_id=_env("INSTATUS_PAGE_ID", "page_shoplab"),
            instatus_should_publish=_env("INSTATUS_SHOULD_PUBLISH", "false").lower() == "true",
            slack_bot_token=_env("SLACK_BOT_TOKEN", "xoxb-sandbox"),
            slack_app_token=_env("SLACK_APP_TOKEN", ""),
            slack_oncall_channel=_env("SLACK_ONCALL_CHANNEL", "C_ONCALL"),
            memory_backend=_env("IJ_MEMORY_BACKEND", "local_git"),
            github_token=_env("GITHUB_TOKEN", ""),
            github_memory_repo=_env("GITHUB_REPO", _env("GITHUB_MEMORY_REPO", "")),
            shoplab_supervisor_url=_env("SHOPLAB_SUPERVISOR_URL", "http://127.0.0.1:8800"),
            shoplab_control_token=_env("SHOPLAB_CONTROL_TOKEN", "dev-control-token"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY", ""),
            judge_model=_env("IJ_JUDGE_MODEL", "claude-sonnet-5"),
            memory_model=_env("IJ_MEMORY_MODEL", "claude-opus-5"),
            judge_impl=_env("IJ_JUDGE_IMPL", "claude" if _env("ANTHROPIC_API_KEY") else "heuristic"),
        )
        if _env("IJ_VAR_DIR"):
            data["var_dir"] = Path(_env("IJ_VAR_DIR"))
        data.update(overrides)
        if data["backend"] == "sandbox":
            # .env may hold REAL account ids/credentials; the sandbox only knows its own seeded ones.
            data.update(SANDBOX_IDENTITY)
        return cls(**data)


class Config:
    """Declarative config loaded from yaml. Immutable during a run."""

    def __init__(self, config_dir: Path = CONFIG_DIR):
        self.dir = config_dir
        load_dotenv(ROOT / ".env", override=False)

    def _load(self, name: str) -> dict:
        return yaml.safe_load((self.dir / name).read_text(encoding="utf-8"))

    @cached_property
    def catalog(self) -> dict[str, ServiceEntry]:
        raw = self._load("catalog.yaml")["services"]
        catalog = {name: ServiceEntry(name=name, **spec) for name, spec in raw.items()}
        # Real accounts: component ids and on-call users differ from the sandbox defaults.
        #   INSTATUS_COMPONENTS=checkout=<id>,search=<id>,catalog=<id>
        #   ONCALL_SLACK_USER_IDS=U123,U456
        real = real_backend()
        for pair in filter(None, (p.strip() for p in (_env("INSTATUS_COMPONENTS") if real else "").split(","))):
            name, _, cid = pair.partition("=")
            if name in catalog and cid:
                catalog[name].instatus_component_id = cid
        oncall = [u.strip() for u in _env("ONCALL_SLACK_USER_IDS").split(",") if u.strip()] if real else []
        if oncall:
            for entry in catalog.values():
                entry.owners = oncall
        return catalog

    @cached_property
    def actions(self) -> dict[str, ActionSpec]:
        raw = self._load("actions.yaml")["actions"]
        out = {}
        for name, spec in raw.items():
            spec = dict(spec)
            v = spec.pop("verify")
            spec["verify"] = VerifySpec(
                conditions=[MetricCondition(**c) for c in v["conditions"]],
                window_s=v.get("window_s", 90),
                min_rps=v.get("min_rps", 5.0),
            )
            spec["autonomy_cap"] = AutonomyLevel(spec.get("autonomy_cap", "L1"))
            out[name] = ActionSpec(name=name, **spec)
        return out

    @cached_property
    def policy(self) -> PolicyConfig:
        data = self._load("policy.yaml")["policy"]
        oncall = [u.strip() for u in _env("ONCALL_SLACK_USER_IDS").split(",") if u.strip()] if real_backend() else []
        if oncall:
            data["oncall_allowlist"] = oncall
        return PolicyConfig(**data)

    @cached_property
    def autonomy(self) -> AutonomyThresholds:
        return AutonomyThresholds(**self._load("policy.yaml").get("autonomy", {}))

    @cached_property
    def slos(self) -> dict[str, dict]:
        return self._load("slos.yaml")["slos"]

    def service(self, name: str) -> ServiceEntry | None:
        return self.catalog.get(name)

    def shares_dependency(self, a: str, b: str) -> bool:
        sa, sb = self.service(a), self.service(b)
        if not sa or not sb:
            return False
        return bool(set(sa.depends_on) & set(sb.depends_on))
