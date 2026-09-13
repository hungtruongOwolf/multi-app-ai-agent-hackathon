"""Shared data contracts. Every module speaks these types; change with care."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def canonical_hash(obj: Any, length: int = 16) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:length]


# ---------------------------------------------------------------- enums


class Env(StrEnum):
    production = "production"
    staging = "staging"


class Severity(StrEnum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"

    @property
    def rank(self) -> int:  # lower = more severe
        return int(self.value[-1])


class CustomerImpact(StrEnum):
    none = "none"
    degraded = "degraded"
    partial_outage = "partial_outage"
    major_outage = "major_outage"


class IncidentState(StrEnum):
    DETECTED = "DETECTED"
    TRIAGING = "TRIAGING"
    OPEN = "OPEN"
    AWAITING_PUBLIC_APPROVAL = "AWAITING_PUBLIC_APPROVAL"
    REMEDIATION_PROPOSED = "REMEDIATION_PROPOSED"
    AWAITING_FIX_APPROVAL = "AWAITING_FIX_APPROVAL"
    VETO_WINDOW = "VETO_WINDOW"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    ROLLING_BACK = "ROLLING_BACK"
    MONITORING = "MONITORING"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


TERMINAL_STATES = {IncidentState.CLOSED}


class DecisionResult(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DOWNGRADE = "DOWNGRADE"


class AutonomyLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"

    @property
    def rank(self) -> int:
        return int(self.value[-1])


class VerifyResult(StrEnum):
    pass_ = "pass"
    fail = "fail"
    inconclusive = "inconclusive"


class OutcomeResult(StrEnum):
    success = "success"
    failure = "failure"
    inconclusive = "inconclusive"


# ---------------------------------------------------------------- catalog / config


class ServiceEntry(BaseModel):
    name: str
    public: bool
    tier: int
    capability: str
    instatus_component_id: str | None = None
    depends_on: list[str] = []
    owners: list[str] = []
    routes: list[str] = []
    critical_routes: list[str] = []  # business-critical user journeys (e.g. paying); others are secondary


MetricName = Literal[
    "error_rate",      # 5xx / total, 0..1
    "rps",             # requests per second
    "latency_p95",     # seconds
    "pool_utilization",  # in_use / size, 0..1
    "pool_wait_p95",   # seconds
    "db_query_p95",    # seconds
    "memory_mb",
]


class MetricCondition(BaseModel):
    """Machine-checkable condition over a named metric. Used by runbook match_conditions and verify specs."""

    metric: MetricName
    service: str = "{service}"  # "{service}" is substituted with the incident's target service
    route: str | None = None
    op: Literal["<", "<=", ">", ">="]
    value: float
    window_s: int = 60

    def bind(self, service: str) -> "MetricCondition":
        return self.model_copy(update={"service": self.service.replace("{service}", service)})

    def holds(self, observed: float) -> bool:
        return {
            "<": observed < self.value,
            "<=": observed <= self.value,
            ">": observed > self.value,
            ">=": observed >= self.value,
        }[self.op]


class VerifySpec(BaseModel):
    conditions: list[MetricCondition]
    window_s: int = 90
    min_rps: float = 5.0


class ActionSpec(BaseModel):
    name: str
    params_schema: dict[str, str] = {}
    blast_radius: Literal["pod", "service", "global"]
    reversible: bool
    safe_to_repeat: bool = False
    preconditions: list[str] = []
    verify: VerifySpec
    max_per_hour: int = 2
    autonomy_cap: AutonomyLevel = AutonomyLevel.L1


# ---------------------------------------------------------------- signals / incidents


class Signal(BaseModel):
    signal_id: str = Field(default_factory=lambda: new_id("sig"))
    source: Literal["sentry", "slo"]
    fingerprint: str
    service: str
    environment: Env
    error_type: str = ""
    culprit: str = ""
    message_redacted: str = ""  # untrusted, redacted, truncated
    count: int = 0
    user_count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    external_id: str | None = None  # sentry issue id / slo name
    slo_name: str | None = None
    burn_rate: float | None = None
    trial_id: str | None = None


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: new_id("inc"))
    incident_key: str
    trial_id: str | None = None
    state: IncidentState = IncidentState.DETECTED
    environment: Env
    services: list[str]
    severity: Severity | None = None
    customer_impact: CustomerImpact | None = None
    customer_visible: bool = False
    runbook_id: str | None = None
    related_incident_id: str | None = None
    linear_issue_id: str | None = None
    instatus_incident_id: str | None = None
    slack_channel_id: str | None = None
    slack_thread_ts: str | None = None
    public_posted: bool = False
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    state_entered_at: datetime = Field(default_factory=now)
    resolved_at: datetime | None = None

    @property
    def primary_service(self) -> str:
        return self.services[0]


# ---------------------------------------------------------------- LLM proposal


class ActionProposal(BaseModel):
    name: str
    params: dict[str, Any] = {}
    target_service: str


class TriageProposal(BaseModel):
    severity: Severity
    customer_impact: CustomerImpact
    customer_visible: bool
    confidence: float = Field(ge=0, le=1)
    related_incident_id: str | None = None
    runbook_id: str | None = None
    runbook_match_evidence: list[str] = []
    proposed_action: ActionProposal | None = None
    needs_human: bool = False
    rationale_internal: str = ""


# ---------------------------------------------------------------- policy


class Intent(BaseModel):
    """A write the agent wants to perform. kind examples:
    linear.create_issue, linear.comment, linear.close,
    instatus.create_incident, instatus.update, instatus.resolve,
    slack.create_channel, slack.post,
    remediation.execute, remediation.rollback,
    incident.resolve, incident.merge,
    memory.propose, memory.merge
    """

    kind: str
    incident_id: str | None = None
    payload: dict[str, Any] = {}


class Decision(BaseModel):
    decision_id: str = Field(default_factory=lambda: new_id("dec"))
    incident_id: str | None = None
    trial_id: str | None = None
    intent: str
    result: DecisionResult
    rules: list[str] = []
    explain: str = ""
    downgrade_to: str | None = None
    approval_kind: str | None = None
    approval_id: str | None = None
    inputs_digest: str = ""
    ts: datetime = Field(default_factory=now)

    @property
    def allowed(self) -> bool:
        return self.result == DecisionResult.ALLOW


# ---------------------------------------------------------------- remediation


class Plan(BaseModel):
    plan_id: str = Field(default_factory=lambda: new_id("plan"))
    incident_id: str
    runbook_id: str | None
    action: str
    params: dict[str, Any]
    target_service: str
    prev_state: dict[str, Any] = {}
    verify: VerifySpec
    autonomy_level: AutonomyLevel
    created_at: datetime = Field(default_factory=now)
    # runbook = a known fix with earned autonomy; diagnosis = inferred from docs/changes/metrics for a new failure;
    # human = proposed by an engineer in the incident thread. Only runbook plans may ever run without a click.
    source: Literal["runbook", "diagnosis", "human"] = "runbook"
    proposed_by: str | None = None
    rationale: str = ""

    @property
    def plan_hash(self) -> str:
        return canonical_hash(
            {
                "incident": self.incident_id,
                "source": self.source,
                "runbook": self.runbook_id,
                "action": self.action,
                "params": self.params,
                "target": self.target_service,
                "verify": self.verify.model_dump(),
            }
        )


class Approval(BaseModel):
    approval_id: str = Field(default_factory=lambda: new_id("apr"))
    incident_id: str
    kind: Literal["public_post", "fix", "memory_merge"]
    subject_hash: str  # plan_hash for fixes, proposal hash for public posts
    user_id: str
    verdict: Literal["approve", "reject"]
    via: Literal["button", "text", "cli"]
    valid: bool
    reason: str = ""
    ts: datetime = Field(default_factory=now)


class Outcome(BaseModel):
    outcome_id: str = Field(default_factory=lambda: new_id("out"))
    runbook_id: str | None
    incident_id: str
    plan_id: str
    action: str
    result: OutcomeResult
    trial_id: str | None = None
    ts: datetime = Field(default_factory=now)


class RunbookStats(BaseModel):
    success: int = 0
    failure: int = 0
    inconclusive: int = 0
    failure_since_review: int = 0
    last_verified: datetime | None = None
    recent: list[OutcomeResult] = []  # newest last, up to 10
