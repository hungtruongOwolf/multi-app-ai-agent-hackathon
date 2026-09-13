"""Runbook page format for the LLM Wiki.

A runbook is markdown with YAML frontmatter. Frontmatter is split into two zones:
- LLM-maintained (via reviewed proposals): id, title, signatures, match_conditions, action
- CODE-OWNED (only `MemoryRepo.commit_code_owned` writes them): stats, autonomy
"""

from __future__ import annotations

import re

import yaml
from pydantic import BaseModel, ValidationError

from judge.core.models import AutonomyLevel, MetricCondition, RunbookStats


class RunbookParseError(ValueError):
    pass


class Signatures(BaseModel):
    fingerprints: list[str] = []
    error_types: list[str] = []
    services: list[str] = []


class RunbookAction(BaseModel):
    name: str
    params: dict = {}


class RunbookAutonomy(BaseModel):
    level: AutonomyLevel = AutonomyLevel.L0
    cap: AutonomyLevel = AutonomyLevel.L2
    review_required: bool = False


class RunbookFrontmatter(BaseModel):
    id: str
    title: str
    signatures: Signatures = Signatures()
    match_conditions: list[MetricCondition] = []
    action: RunbookAction | None = None
    stats: RunbookStats = RunbookStats()  # CODE-OWNED
    autonomy: RunbookAutonomy = RunbookAutonomy()  # CODE-OWNED


CODE_OWNED_FIELDS = ("stats", "autonomy")
REQUIRED_SECTIONS = [
    "Summary",
    "Symptoms",
    "Known root causes",
    "Remediation",
    "Tried and did not work",
    "How to tell apart",
    "Notes",
]
DISTINGUISH_SECTION = "How to tell apart"
CODE_ZONE_MARKER = "# ---- code-owned zone — an LLM proposal that touches this is rejected ----"
RUNBOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        raise RunbookParseError("missing YAML frontmatter")
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise RunbookParseError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise RunbookParseError("frontmatter must be a mapping")
    return data, m.group(2)


def parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _dump(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None, width=120)


class Runbook:
    def __init__(self, frontmatter: RunbookFrontmatter, sections: dict[str, str] | None = None):
        self.frontmatter = frontmatter
        self.sections = dict(sections or {})

    @property
    def id(self) -> str:
        return self.frontmatter.id

    @property
    def path(self) -> str:
        return f"wiki/runbooks/{self.id}.md"

    @classmethod
    def parse(cls, text: str) -> "Runbook":
        data, body = split_frontmatter(text)
        try:
            fm = RunbookFrontmatter.model_validate(data)
        except ValidationError as e:
            raise RunbookParseError(f"invalid frontmatter: {e}") from e
        return cls(fm, parse_sections(body))

    def missing_sections(self) -> list[str]:
        return [s for s in REQUIRED_SECTIONS if not self.sections.get(s, "").strip()]

    def summary(self) -> str:
        text = self.sections.get("Summary", "").strip()
        first = text.split("\n", 1)[0].strip()
        return first[:160]

    def to_markdown(self) -> str:
        fm = self.frontmatter.model_dump(mode="json", exclude_none=True)
        owned = {k: fm.pop(k) for k in CODE_OWNED_FIELDS}
        if fm.get("match_conditions"):
            fm["match_conditions"] = [
                {k: v for k, v in c.items() if v is not None} for c in fm["match_conditions"]
            ]
        out = ["---", _dump(fm).rstrip(), CODE_ZONE_MARKER, _dump(owned).rstrip(), "---", "",
               f"# {self.frontmatter.title}", ""]
        ordered = REQUIRED_SECTIONS + [s for s in self.sections if s not in REQUIRED_SECTIONS]
        for name in ordered:
            out += [f"## {name}", "", self.sections.get(name, "").strip(), ""]
        return "\n".join(out).rstrip() + "\n"

    def copy(self) -> "Runbook":
        return Runbook(self.frontmatter.model_copy(deep=True), dict(self.sections))


def code_owned_dump(fm: RunbookFrontmatter) -> dict:
    return {k: getattr(fm, k).model_dump(mode="json") for k in CODE_OWNED_FIELDS}
