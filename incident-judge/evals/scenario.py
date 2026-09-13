"""Scenario schema (SPEC §14.2, CONTRACTS §7).

A scenario is a real fault on ShopLab + a scripted environment (humans, tool faults, crashes)
+ expectations over the FINAL state of the apps and the agent's audit log."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

EVALS_DIR = Path(__file__).resolve().parent
SCENARIOS_DIR = EVALS_DIR / "scenarios"
FIXTURES_DIR = EVALS_DIR / "fixtures"

FAULTS = {
    "bad_flag", "pool_starved", "slow_query", "worker_hang", "bad_deploy", "staging_fire", "batch_fail",
    "pii_leak", "injection", "db_slow_shared", "avatar_errors", "pay_few_users",
}
SERVICES = {"checkout", "search", "catalog", "internal-batch", "checkout@staging"}
TERMINAL_CHOICES = {
    "DETECTED", "TRIAGING", "OPEN", "AWAITING_PUBLIC_APPROVAL", "REMEDIATION_PROPOSED", "AWAITING_FIX_APPROVAL",
    "VETO_WINDOW", "EXECUTING", "VERIFYING", "ROLLING_BACK", "MONITORING", "ESCALATED", "RESOLVED", "CLOSED",
}
# Audit events the runner can wait for (crash plans, timeline triggers):
#   execution:<kind>                 row in executions with kind (e.g. execution:apply)
#   plan_status:<status>             any plan with status
#   decision:<intent>[:<RESULT>]     decision row
#   step:<app.op>[:<status>]         outbox step row
#   state:<IncidentState>            any incident in state
EVENT_PREFIXES = ("execution:", "plan_status:", "decision:", "step:", "state:")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolFault(_Strict):
    app: str
    op: str
    mode: Literal["error", "ghost_write", "latency"]
    count: int = 1
    status: int = 500
    ms: int = 0


class TimelineStep(_Strict):
    """Exactly one trigger (at_s | on) and one action."""

    at_s: float | None = None
    on: str | None = None  # audit event, see EVENT_PREFIXES
    action: Literal["inject", "clear_faults", "traffic", "tool_faults_add", "tool_faults_clear"] = "inject"
    fault: str | None = None
    service: str | None = None
    params: dict[str, Any] = {}
    rps: float | None = None
    enabled: bool | None = None
    tool_faults: list[ToolFault] = []

    @model_validator(mode="after")
    def _check(self) -> "TimelineStep":
        if (self.at_s is None) == (self.on is None):
            raise ValueError("timeline step needs exactly one of at_s / on")
        if self.on and not self.on.startswith(EVENT_PREFIXES):
            raise ValueError(f"unknown event {self.on!r}")
        if self.action == "inject":
            if self.fault not in FAULTS:
                raise ValueError(f"unknown fault {self.fault!r}")
            if self.service not in SERVICES:
                raise ValueError(f"unknown service {self.service!r}")
        if self.action == "traffic" and self.rps is None and self.enabled is None:
            raise ValueError("traffic step needs rps or enabled")
        if self.action == "tool_faults_add" and not self.tool_faults:
            raise ValueError("tool_faults_add needs tool_faults")
        return self


class Flap(_Strict):
    fault: str
    service: str
    on_s: float
    off_s: float
    cycles: int
    start_s: float = 0

    def expand(self) -> list[TimelineStep]:
        steps, t = [], self.start_s
        for _ in range(self.cycles):
            steps.append(TimelineStep(at_s=t, action="inject", fault=self.fault, service=self.service))
            steps.append(TimelineStep(at_s=t + self.on_s, action="clear_faults"))
            t += self.on_s + self.off_s
        return steps


class HumanRule(_Strict):
    """Simulated human acting through the Slack API (sandbox user tokens).

    when:
      fix_approval_requested   bot card tagged [IJ-FIX]      -> reply approve/reject <hash8>
      public_post_requested    bot card tagged [IJ-PUBLIC]
      veto_window              bot card tagged [IJ-VETO]
      memory_merge_requested   bot card tagged [IJ-MEMORY]
      at_time                  at at_s, post `text` in the trial's first bot thread (or #oncall)
    """

    when: Literal["fix_approval_requested", "public_post_requested", "veto_window", "memory_merge_requested",
                  "at_time"]
    actor: Literal["oncall_1", "intruder"] = "oncall_1"
    reply: Literal["approve", "reject", "text"] = "approve"
    text: str | None = None
    delay_s: float = 2.0
    at_s: float | None = None
    times: int = 1
    hash_mode: Literal["current", "first_seen", "wrong"] = "current"

    @model_validator(mode="after")
    def _check(self) -> "HumanRule":
        if self.when == "at_time" and (self.at_s is None or not self.text):
            raise ValueError("at_time rule needs at_s and text")
        if self.reply == "text" and not self.text:
            raise ValueError("reply=text needs text")
        return self


class CrashPlan(_Strict):
    after: str
    restart_after_s: float = 2.0
    times: int = 1

    @model_validator(mode="after")
    def _check(self) -> "CrashPlan":
        if not self.after.startswith(EVENT_PREFIXES):
            raise ValueError(f"unknown crash event {self.after!r}")
        return self


class ProposalFixture(_Strict):
    title: str
    body: str = ""
    files: dict[str, str]  # repo path -> fixture path relative to evals/fixtures


class MemoryFixture(_Strict):
    runbooks: list[str] = []  # evals/fixtures/memory/<name>.md
    raw: list[str] = []  # evals/fixtures/raw/<name>.md -> raw/incidents/<name>.md
    outcomes: list[str] = []  # evals/fixtures/outcomes/<name>.yaml
    proposals: list[ProposalFixture] = []


class WaitSpec(_Strict):
    until_states: list[str] = []  # every primary incident in one of these
    min_runtime_s: float = 30
    settle_s: float = 25  # no new audit rows for this long => quiescent
    require_incident: bool = True  # quiescence only counts once an incident exists

    @model_validator(mode="after")
    def _check(self) -> "WaitSpec":
        bad = set(self.until_states) - TERMINAL_CHOICES
        if bad:
            raise ValueError(f"unknown states {bad}")
        return self


# ---------------------------------------------------------------- expectations


class DecisionExpect(_Strict):
    intent: str
    result: Literal["ALLOW", "DENY", "REQUIRE_APPROVAL", "DOWNGRADE"] | None = None
    rules: list[str] = []


class LinearExpect(_Strict):
    count: int | None = None
    priority: int | None = None
    priority_at_least: int | None = None  # numeric >= (i.e. no more urgent than)
    priority_at_most: int | None = None  # numeric <= (at least this urgent)
    state: Literal["open", "closed"] | None = None
    min_comments: int | None = None


class InstatusExpect(_Strict):
    count: int | None = None
    component_status: str | None = None
    component_status_in: list[str] = []
    component_status_not: list[str] = []
    status: str | None = None
    status_not: list[str] = []
    components_count: int | None = None


class SlackExpect(_Strict):
    war_room: bool | None = None
    war_room_count: int | None = None  # more inc-* channels than this => duplicate (unsafe)
    approval_requested: list[Literal["fix", "public_post", "veto", "memory_merge"]] = []
    deny_explained: list[str] = []  # rule ids that must appear in an internal Slack message
    messages_match: list[str] = []  # regexes over trial messages


class SeverityExpect(_Strict):
    service: str
    at_most_rank: int | None = None  # SEV rank <= (at least this severe)
    at_least_rank: int | None = None  # SEV rank >= (no more severe than)
    customer_visible: bool | None = None


class MemoryExpect(_Strict):
    runbook: str | None = None
    stats_delta: dict[Literal["success", "failure", "inconclusive"], int] = {}
    proposal_created: bool | None = None
    new_page: bool | None = None
    proposal_status_not: list[str] = []  # e.g. [merged] for poisoned/tampered proposals
    validator_rejected: bool | None = None
    autonomy_level: str | None = None
    review_required: bool | None = None
    index_contains: list[str] = []
    log_appended: bool | None = None
    lint_linear_issue: bool | None = None
    linked_in_linear: bool | None = None


class ApprovalsExpect(_Strict):
    invalid_min: int | None = None
    valid_min: int | None = None


class Expect(_Strict):
    incidents: dict[Literal["count"], int] = {}
    terminal_states: list[str] = []
    severity: list[SeverityExpect] = []
    severity_order: list[str] = []  # services, most severe first
    linear: LinearExpect | None = None
    instatus: InstatusExpect | None = None
    slack: SlackExpect | None = None
    actions_executed: list[str] | None = None  # exact multiset of applied actions (None = don't care)
    rollbacks: list[str] | None = None
    decisions_contains: list[DecisionExpect] = []
    approvals: ApprovalsExpect | None = None
    memory: MemoryExpect | None = None
    config_end: dict[str, dict[str, Any]] = {}
    alert_firing_at_end: bool = False
    lookalike: bool = False  # counts toward runbook-abstention metric

    @model_validator(mode="after")
    def _check(self) -> "Expect":
        bad = set(self.terminal_states) - TERMINAL_CHOICES
        if bad:
            raise ValueError(f"unknown terminal states {bad}")
        return self


FORBIDDEN_PREFIXES = (
    "instatus.any", "instatus.resolve", "incident.resolve", "linear.close", "action.", "memory.merge",
    "public_text:", "autonomy.increase:",
)


class Scenario(_Strict):
    id: str
    title: str
    category: Literal["judgment", "dedupe", "memory", "remediation", "attack", "robustness"]
    tier: Literal["core", "extended"]
    env: Literal["production", "staging"] = "production"
    time_scale: float = 0.1
    timeout_s: float = 240
    traffic: dict[str, Any] = Field(default_factory=lambda: {"rps": 30, "enabled": True})
    memory_fixture: MemoryFixture = MemoryFixture()
    inject: list[TimelineStep] = []
    flap: Flap | None = None
    humans: list[HumanRule] = []
    tool_faults: list[ToolFault] = []
    crash_plan: CrashPlan | None = None
    agent_env: dict[str, str] = {}  # test hooks the agent honors (IJ_TEST_*)
    wait: WaitSpec = WaitSpec()
    expect: Expect = Expect()
    forbidden: list[str] = []
    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> "Scenario":
        for f in self.forbidden:
            if not f.startswith(FORBIDDEN_PREFIXES):
                raise ValueError(f"unknown forbidden entry {f!r}")
        for name in self.memory_fixture.outcomes:
            if not (FIXTURES_DIR / "outcomes" / f"{name}.yaml").exists():
                raise ValueError(f"missing outcomes fixture {name}")
        for name in self.memory_fixture.raw:
            if not (FIXTURES_DIR / "raw" / f"{name}.md").exists():
                raise ValueError(f"missing raw fixture {name}")
        for p in self.memory_fixture.proposals:
            for src in p.files.values():
                if not (FIXTURES_DIR / src).exists():
                    raise ValueError(f"missing proposal fixture {src}")
        return self

    def timeline(self) -> list[TimelineStep]:
        steps = list(self.inject)
        if self.flap:
            steps += self.flap.expand()
        return steps

    @property
    def grace_s(self) -> float:
        return max(30.0, 60.0 * self.time_scale)


class OutcomeSeed(_Strict):
    result: Literal["success", "failure", "inconclusive"]
    days_ago: float = 1


class OutcomesFixture(_Strict):
    runbook_id: str
    action: str
    outcomes: list[OutcomeSeed]


def load_scenario(path: Path) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_outcomes_fixture(name: str) -> OutcomesFixture:
    path = FIXTURES_DIR / "outcomes" / f"{name}.yaml"
    return OutcomesFixture.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def all_scenarios() -> list[Scenario]:
    return [load_scenario(p) for p in sorted(SCENARIOS_DIR.glob("*.yaml"))]


def select(spec: str) -> list[Scenario]:
    """spec: core | extended | all | comma-separated ids."""
    scenarios = all_scenarios()
    if spec == "all":
        return scenarios
    if spec in ("core", "extended"):
        return [s for s in scenarios if s.tier == spec]
    wanted = [x.strip() for x in spec.split(",") if x.strip()]
    by_id = {s.id: s for s in scenarios}
    missing = [w for w in wanted if w not in by_id]
    if missing:
        raise KeyError(f"unknown scenarios: {missing}")
    return [by_id[w] for w in wanted]
