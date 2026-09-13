from __future__ import annotations

import hashlib


def fingerprint(error_type: str, culprit: str, environment: str, service: str) -> str:
    """Stable identity of an incident class. Never uses the raw message (volatile, untrusted)."""
    basis = "|".join([error_type or "", culprit or "", environment or "", service or ""])
    return hashlib.sha256(basis.encode()).hexdigest()[:12]


def slo_fingerprint(slo_name: str, environment: str, service: str) -> str:
    return fingerprint("SLOBurn", slo_name, environment, service)
