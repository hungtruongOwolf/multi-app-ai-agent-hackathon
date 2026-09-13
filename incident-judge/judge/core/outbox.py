"""Exactly-once-effect external writes: idempotency key + marker reconcile before create.

Flow for every external write:
  1. mark step in_flight (committed)
  2. reconcile: search the external system for our marker; if found -> done (catches ghost writes)
  3. otherwise perform the write -> done
On restart, every in_flight step goes back through step 2. Never create blindly."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from judge.core.models import Decision, canonical_hash, new_id
from judge.core.store import Store

log = logging.getLogger("judge.outbox")


def idempotency_key(incident_id: str | None, kind: str, scope: str = "") -> str:
    return canonical_hash({"i": incident_id, "k": kind, "s": scope})


def marker(key: str, incident_id: str | None, trial_id: str | None) -> str:
    parts = [f"IJ-KEY:{key}"]
    if incident_id:
        parts.append(f"IJ-INC:{incident_id}")
    if trial_id:
        parts.append(f"IJ-TRIAL:{trial_id}")
    return " ".join(parts)


def compact_marker(mk: str) -> str:
    """URL-safe form of a marker ('+' instead of spaces) for embedding in links."""
    return mk.replace(" ", "+")


def key_token(mk: str) -> str:
    return next((p for p in mk.split() if p.startswith("IJ-KEY:")), mk)


def slack_thread_link(mk: str, channel: str, label: str = "Slack thread") -> str:
    """Markdown link to the incident discussion that also carries the (hidden) dedupe marker in its URL.
    Humans see a useful link; the agent can still find what it already wrote after a crash or lost response."""
    return f"[{label}](https://slack.com/app_redirect?channel={channel}&ij={compact_marker(mk)})"


def marker_in(text: str, mk: str) -> bool:
    return bool(text) and (mk in text or compact_marker(mk) in text)


def parse_marker(mk: str) -> tuple[str | None, str | None, str | None]:
    """-> (key, incident_id, trial_id)"""
    found: dict[str, str] = {}
    for part in mk.replace("+", " ").split():
        if ":" in part and part.startswith("IJ-"):
            name, value = part.split(":", 1)
            found[name] = value
    return found.get("IJ-KEY"), found.get("IJ-INC"), found.get("IJ-TRIAL")


class PolicyDenied(Exception):
    def __init__(self, decision: Decision):
        super().__init__(f"{decision.intent} denied by {decision.rules}: {decision.explain}")
        self.decision = decision


@dataclass
class StepResult:
    ref: str | None
    reconciled: bool
    skipped: bool = False


class Outbox:
    def __init__(self, store: Store, trial_id: str | None):
        self.store = store
        self.trial_id = trial_id

    async def run(
        self,
        *,
        app: str,
        op: str,
        incident_id: str | None,
        scope: str,
        decision: Decision,
        reconcile: Callable[[str], Awaitable[str | None]],
        execute: Callable[[str], Awaitable[str]],
    ) -> StepResult:
        """reconcile(marker) -> external ref or None; execute(marker) -> external ref."""
        if not decision.allowed:
            raise PolicyDenied(decision)
        kind = f"{app}.{op}"
        key = idempotency_key(incident_id, kind, scope)
        mk = marker(key, incident_id, self.trial_id)
        existing = self.store.get_step(key)
        if existing and existing["status"] == "done":
            return StepResult(ref=existing["external_ref"], reconciled=True, skipped=True)

        step_id = existing["step_id"] if existing else new_id("step")
        self.store.upsert_step(step_id=step_id, incident_id=incident_id, kind=kind, key=key, status="in_flight",
                               decision_id=decision.decision_id, bump_attempt=True)

        ref = await reconcile(mk)
        if ref:
            log.info("reconciled %s via marker -> %s", kind, ref)
            self.store.upsert_step(step_id=step_id, incident_id=incident_id, kind=kind, key=key, status="done",
                                   external_ref=ref)
            return StepResult(ref=ref, reconciled=True)

        try:
            ref = await execute(mk)
        except Exception as e:  # leave in_flight: next attempt reconciles first
            self.store.upsert_step(step_id=step_id, incident_id=incident_id, kind=kind, key=key,
                                   status="in_flight", error=repr(e)[:500])
            raise
        self.store.add_external_write(incident_id=incident_id, decision_id=decision.decision_id, app=app, op=op,
                                      ref=ref, idempotency_key=key)
        self.store.upsert_step(step_id=step_id, incident_id=incident_id, kind=kind, key=key, status="done",
                               external_ref=ref)
        return StepResult(ref=ref, reconciled=False)
