"""Robust extraction of forced tool-call arguments.

Models occasionally echo the call envelope ({"name": "submit_triage", "input": {...}}) instead of the bare
arguments. Unwrapping it here keeps one bad formatting habit from costing a retry (or a failed triage)."""

from __future__ import annotations

from typing import Any

ENVELOPE_KEYS = ("input", "parameters", "arguments")


def tool_args(block: Any) -> dict:
    data = getattr(block, "input", None) or {}
    if not isinstance(data, dict):
        return {}
    for _ in range(2):  # at most two nested envelopes
        if "name" in data and len(data) <= 3:
            inner = next((data[k] for k in ENVELOPE_KEYS if isinstance(data.get(k), dict)), None)
            if inner is not None:
                data = inner
                continue
        break
    return data
