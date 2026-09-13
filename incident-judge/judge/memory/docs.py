"""Service documentation pages in the wiki (human-maintained engineering docs).

The diagnosis step reads these for incidents no runbook covers: architecture first, then the affected services,
then services they share a dependency with (a shared-dependency failure shows up on several services at once)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from judge.memory.repo import MemoryRepo
    from judge.settings import Config

ARCHITECTURE = "wiki/architecture.md"
SERVICES_DIR = "wiki/services"


def service_doc_path(service: str) -> str:
    return f"{SERVICES_DIR}/{service.split('@')[0]}.md"


def docs_for(repo: "MemoryRepo", services: list[str], config: "Config | None" = None,
             include_related: bool = True, ref: str = "main") -> list[tuple[str, str]]:
    """(path, markdown) for the architecture page, the incident's services and — optionally — services that share
    a dependency with them. Missing pages are skipped. Only merged content (`main`) is read."""
    wanted: list[str] = [ARCHITECTURE]
    base = [s.split("@")[0] for s in services]
    wanted += [service_doc_path(s) for s in base]
    if include_related and config is not None:
        deps = {d for s in base if config.service(s) for d in config.service(s).depends_on}
        for name, entry in sorted(config.catalog.items()):
            if name not in base and deps & set(entry.depends_on):
                wanted.append(service_doc_path(name))
    out: list[tuple[str, str]] = []
    for path in dict.fromkeys(wanted):
        text = repo.read(path, ref)
        if text:
            out.append((path, text))
    return out


def section(markdown: str, heading: str) -> str:
    """Body of a `## heading` section (empty if absent)."""
    lines, grab, out = markdown.splitlines(), False, []
    for line in lines:
        if line.startswith("## "):
            if grab:
                break
            grab = line[3:].strip().lower() == heading.lower()
            continue
        if grab:
            out.append(line)
    return "\n".join(out).strip()
