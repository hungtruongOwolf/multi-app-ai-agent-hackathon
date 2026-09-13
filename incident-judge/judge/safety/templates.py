"""Allowlist rendering for PUBLIC surfaces (status page). Rule P3.

The LLM never writes public text. The only variables are values from the service catalog
and enums; any other placeholder is a programming error."""

from __future__ import annotations

import re
import string

from judge.core.models import CustomerImpact, ServiceEntry
from judge.safety.redact import UnsafeWrite, find_forbidden

PUBLIC_TEMPLATES: dict[str, str] = {
    "investigating": "We are investigating an issue affecting {capability}. Ref {ref}",
    "identified": "We have identified the cause of the issue affecting {capability} and are applying a fix. Ref {ref}",
    "monitoring": "A fix has been applied for {capability}. We are monitoring the results. Ref {ref}",
    "resolved": "The issue affecting {capability} has been resolved. Ref {ref}",
}

# Opaque public reference derived from the idempotency key (and trial). Lets us reconcile
# status-page writes without putting internal ids or free text on the public surface.
REF_PATTERN = re.compile(r"^IJ-[a-z0-9]{1,12}-[a-f0-9]{8}$")

PUBLIC_TITLES: dict[CustomerImpact, str] = {
    CustomerImpact.degraded: "Degraded performance: {capability}",
    CustomerImpact.partial_outage: "Partial outage: {capability}",
    CustomerImpact.major_outage: "Outage: {capability}",
}

INSTATUS_INCIDENT_STATUS = {
    "investigating": "INVESTIGATING",
    "identified": "IDENTIFIED",
    "monitoring": "MONITORING",
    "resolved": "RESOLVED",
}

INSTATUS_COMPONENT_STATUS = {
    CustomerImpact.degraded: "DEGRADEDPERFORMANCE",
    CustomerImpact.partial_outage: "PARTIALOUTAGE",
    CustomerImpact.major_outage: "MAJOROUTAGE",
    CustomerImpact.none: "OPERATIONAL",
}

ALLOWED_FIELDS = {"capability", "ref"}


def _fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def public_ref(key: str, trial_id: str | None) -> str:
    trial = re.sub(r"[^a-z0-9]", "", (trial_id or "live").lower())[:12] or "live"
    return f"IJ-{trial}-{key[:8]}"


def _render(template: str, services: list[ServiceEntry], ref: str = "") -> str:
    extra = _fields(template) - ALLOWED_FIELDS
    if extra:
        raise UnsafeWrite(f"template uses non-allowlisted fields: {extra}")
    if "{ref}" in template and not REF_PATTERN.match(ref):
        raise UnsafeWrite(f"invalid public ref {ref!r}")
    capability = " and ".join(s.capability for s in services)
    text = template.format(capability=capability, ref=ref)
    hits = find_forbidden(text)
    if hits:  # catalog itself should never contain these; defense in depth
        raise UnsafeWrite(f"public text failed post-check: {hits}")
    return text


def public_message(phase: str, services: list[ServiceEntry], ref: str) -> str:
    if any(not s.public for s in services):
        raise UnsafeWrite("public message requested for non-public service")
    return _render(PUBLIC_TEMPLATES[phase], services, ref)


def public_title(impact: CustomerImpact, services: list[ServiceEntry]) -> str:
    if impact == CustomerImpact.none:
        raise UnsafeWrite("no public title for impact=none")
    return _render(PUBLIC_TITLES[impact], services)
