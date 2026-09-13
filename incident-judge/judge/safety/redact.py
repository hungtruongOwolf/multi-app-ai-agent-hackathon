"""Denylist redactor for INTERNAL surfaces (Linear, Slack, wiki, raw timelines).

Public surfaces never receive free text at all — see safety.templates (allowlist).
The eval grader does NOT import this module; it checks seeded canaries instead."""

from __future__ import annotations

import re

FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("py_traceback", re.compile(r'File "[^"]+", line \d+')),
    ("stack_frame", re.compile(r"\bat [^\s(]+ ?\([^)]*:\d+(?::\d+)?\)")),
    ("unix_path", re.compile(r"/(?:home|Users|var|opt|srv|app|tmp|etc|root)/[\w./-]+")),
    ("windows_path", re.compile(r"\b[A-Za-z]:\\[\w\\. -]+")),
    ("internal_host", re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.(?:internal|local|svc|cluster\.local|corp)\b")),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9_-]{12,}")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}")),
]

REDACTED = "[redacted]"


class UnsafeWrite(Exception):
    pass


def redact(text: str | None, max_len: int | None = None) -> str:
    if not text:
        return ""
    out = text
    for _, pattern in FORBIDDEN:
        out = pattern.sub(REDACTED, out)
    if max_len is not None and len(out) > max_len:
        out = out[: max_len - 1] + "…"
    return out


def find_forbidden(text: str) -> list[str]:
    return [name for name, pattern in FORBIDDEN if pattern.search(text or "")]


def assert_internal_safe(text: str) -> None:
    hits = find_forbidden(text)
    if hits:
        raise UnsafeWrite(f"forbidden content in outbound text: {hits}")
