"""Crash-safe remediation execution: lock -> preconditions -> snapshot (persisted) -> apply ->
verify -> (revert) -> outcome -> unlock.

Plan status is persisted at every boundary so a restarted agent can resume() without re-applying:

  approved -> applying -> applied -> verifying -> done
                                              +-> reverting -> rolled_back   (reversible)
                                              +-> failed                     (not reversible / preconditions)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from pydantic import BaseModel

from judge.core.models import (
    Decision,
    Intent,
    Outcome,
    OutcomeResult,
    Plan,
    VerifyResult,
)
from judge.core.outbox import PolicyDenied
from judge.core.store import Store
from judge.policy.engine import PolicyContext, evaluate
from judge.remediation.actions.base import REGISTRY
from judge.remediation.verifier import verify, verify_window
from judge.signals.metrics import MetricsBackend

if TYPE_CHECKING:
    from judge.connectors.shoplab import ShopLabControl
    from judge.settings import Config, Settings

log = logging.getLogger("judge.remediation")

TERMINAL = {"done", "rolled_back", "failed", "cancelled"}


class RunResult(BaseModel):
    plan_id: str
    applied: bool
    verify: VerifyResult | None = None
    rolled_back: bool = False
    outcome: OutcomeResult | None = None
    samples: list[dict] = []
    status: str = ""
    error: str | None = None


class RemediationRunner:
    def __init__(self, store: Store, config: "Config", settings: "Settings", control: "ShopLabControl",
                 metrics: MetricsBackend, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 holder_prefix: str = "runner"):
        self.store = store
        self.config = config
        self.settings = settings
        self.control = control
        self.metrics = metrics
        self.sleep = sleep
        self.holder_prefix = holder_prefix
        self.skip_verify = False
        # optional narration hooks (the agent posts them to the incident thread); never affect outcomes
        self.on_sample = None   # async (plan, samples, n, interval)
        self.on_applied = None  # async (plan, prev_state, result)

    # ------------------------------------------------------------ public

    async def execute(self, plan: Plan, decision: Decision, approval_id: str | None) -> RunResult:
        if not decision.allowed or decision.intent != "remediation.execute":
            raise PolicyDenied(decision)
        existing = self.store.get_plan(plan.plan_id)
        if existing and existing[1] not in ("approved", "proposed", "awaiting_approval", "veto_window"):
            return await self.resume(existing[0], existing[1])
        self.store.put_kv(self._meta_key(plan), {"decision_id": decision.decision_id, "approval_id": approval_id})
        self.store.save_plan(plan, "approved")
        return await self._run(plan, "approved")

    async def resume(self, plan: Plan, status: str) -> RunResult:
        if status in TERMINAL:
            return self._terminal_result(plan, status)
        return await self._run(plan, status)

    # ------------------------------------------------------------ internals

    def _meta_key(self, plan: Plan) -> str:
        return f"plan_meta:{plan.plan_id}"

    def _meta(self, plan: Plan) -> dict:
        return self.store.get_kv(self._meta_key(plan), {}) or {}

    def _lock_name(self, plan: Plan) -> str:
        return f"service:{plan.target_service}"

    def _lease(self, plan: Plan) -> float:
        return float(self.config.policy.lock_lease_s)

    async def _run(self, plan: Plan, status: str) -> RunResult:
        impl = REGISTRY.get(plan.action)
        if impl is None:
            self.store.save_plan(plan, "failed")
            return RunResult(plan_id=plan.plan_id, applied=False, status="failed",
                             error=f"no implementation for action {plan.action!r}")

        lock, holder = self._lock_name(plan), plan.plan_id
        if not self.store.acquire_lock(lock, holder, self._lease(plan)):
            return RunResult(plan_id=plan.plan_id, applied=False, status=status,
                             error=f"locked by {self.store.lock_holder(lock)}")
        meta = self._meta(plan)
        try:
            if status == "approved":
                failures = await impl.preconditions(plan, self.control)
                if failures:
                    self.store.save_plan(plan, "failed")
                    self.store.add_execution(plan=plan, kind="precondition_failed", result=json.dumps(failures),
                                             decision_id=meta.get("decision_id"), approval_id=meta.get("approval_id"))
                    return RunResult(plan_id=plan.plan_id, applied=False, status="failed",
                                     error="; ".join(failures))
                plan.prev_state = await impl.snapshot(plan, self.control)
                self.store.save_plan(plan, "applying")  # prev_state persisted BEFORE apply
                status = "applying"

            if status == "applying":
                if not self._has_execution(plan, "apply"):
                    result = await impl.apply(plan, self.control)
                    self.store.add_execution(plan=plan, kind="apply", result=json.dumps(result, default=str),
                                             decision_id=meta.get("decision_id"),
                                             approval_id=meta.get("approval_id"))
                    if self.on_applied is not None:
                        try:
                            await self.on_applied(plan, plan.prev_state, result)
                        except Exception:
                            log.warning("on_applied narration failed", exc_info=True)
                self.store.save_plan(plan, "applied")
                status = "applied"

            if status in ("applied", "verifying"):
                self.store.save_plan(plan, "verifying")
                if self.skip_verify:  # baseline B2 only: trust the fix without measuring
                    result, samples = VerifyResult.pass_, [{"baseline": "B2", "skipped": True}]
                else:
                    result, samples = await verify(plan, self.metrics, self.settings,
                                                   sleep=self._renewing_sleep(plan), on_sample=self.on_sample)
                self.store.add_verification(plan.plan_id, plan.incident_id, result.value, samples)
                if result == VerifyResult.pass_:
                    self.store.save_plan(plan, "done")
                    outcome = self._record_outcome(plan, OutcomeResult.success)
                    return RunResult(plan_id=plan.plan_id, applied=True, verify=result, outcome=outcome,
                                     samples=samples, status="done")
                self.store.put_kv(f"plan_verify:{plan.plan_id}", result.value)
                status = "reverting"
                self.store.save_plan(plan, "reverting")
            else:
                samples, result = [], None

            if status == "reverting":
                if result is None:
                    result = VerifyResult(self.store.get_kv(f"plan_verify:{plan.plan_id}", "fail"))
                rolled_back = await self._revert(plan, impl, result)
                final = "rolled_back" if rolled_back else "failed"
                self.store.save_plan(plan, final)
                outcome = self._record_outcome(
                    plan, OutcomeResult.failure if result == VerifyResult.fail else OutcomeResult.inconclusive)
                return RunResult(plan_id=plan.plan_id, applied=True, verify=result, rolled_back=rolled_back,
                                 outcome=outcome, samples=samples, status=final)

            return self._terminal_result(plan, status)
        finally:
            self.store.release_lock(lock, holder)

    def _renewing_sleep(self, plan: Plan) -> Callable[[float], Awaitable[None]]:
        lock, holder = self._lock_name(plan), plan.plan_id
        _, interval, _ = verify_window(plan, self.settings.time_scale)
        lease = max(self._lease(plan), interval * 3)

        async def _sleep(seconds: float) -> None:
            self.store.acquire_lock(lock, holder, lease)
            await self.sleep(seconds)
            self.store.acquire_lock(lock, holder, lease)

        return _sleep

    async def _revert(self, plan: Plan, impl, result: VerifyResult) -> bool:
        spec = self.config.actions.get(plan.action)
        if spec is None or not spec.reversible:
            return False
        decision = evaluate(
            Intent(kind="remediation.rollback", incident_id=plan.incident_id,
                   payload={"plan_id": plan.plan_id, "verify": result.value}),
            PolicyContext(settings=self.settings, config=self.config, plan=plan,
                          kill_switch=self.store.kill_switch()),
        )
        decision.explain = decision.explain or f"automatic rollback after verify={result.value}"
        self.store.add_decision(decision)
        if not decision.allowed:
            log.error("rollback of %s denied: %s", plan.plan_id, decision.explain)
            return False
        if self._has_execution(plan, "rollback"):
            return True
        reverted = await impl.revert(plan, self.control)
        if reverted is None:
            return False
        self.store.add_execution(plan=plan, kind="rollback", result=json.dumps(reverted, default=str),
                                 decision_id=decision.decision_id, approval_id=None)
        return True

    def _record_outcome(self, plan: Plan, result: OutcomeResult) -> OutcomeResult:
        self.store.add_outcome(Outcome(outcome_id=f"out_{plan.plan_id}", runbook_id=plan.runbook_id,
                                       incident_id=plan.incident_id, plan_id=plan.plan_id, action=plan.action,
                                       result=result, trial_id=self.settings.trial_id))
        return result

    def _has_execution(self, plan: Plan, kind: str) -> bool:
        return any(r["plan_id"] == plan.plan_id and r["kind"] == kind
                   for r in self.store.executions(service=plan.target_service))

    def _terminal_result(self, plan: Plan, status: str) -> RunResult:
        applied = self._has_execution(plan, "apply")
        outcome = next((o.result for o in self.store.outcomes() if o.plan_id == plan.plan_id), None)
        v = self.store.get_kv(f"plan_verify:{plan.plan_id}")
        verify_result = VerifyResult.pass_ if status == "done" else (VerifyResult(v) if v else None)
        return RunResult(plan_id=plan.plan_id, applied=applied, verify=verify_result,
                         rolled_back=self._has_execution(plan, "rollback"), outcome=outcome, status=status)
