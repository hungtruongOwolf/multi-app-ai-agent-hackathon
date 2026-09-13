"""What the memory layer needs from an LLM. Implemented by judge.reasoning (Claude) and
judge.memory.heuristic (deterministic, offline). Both only return data; neither writes anything."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from judge.memory.schema import Runbook


class RunbookChooser(Protocol):
    async def choose(self, index_md: str, incident_summary: str) -> tuple[str | None, list[str]]:
        """Pick a runbook id from index.md for this incident, or None. Returns (id, evidence)."""
        ...


class RunbookWriter(Protocol):
    async def write(self, existing: "Runbook | None", raw_timelines: list[str], agents_md: str) -> dict[str, str]:
        """Return prose per section name (see schema.REQUIRED_SECTIONS). Never frontmatter."""
        ...
