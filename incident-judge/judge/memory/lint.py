"""Lint the wiki (SPEC §9.6). lint() only reports; apply_lint() commits code-owned consequences
(stale or orphaned runbooks lose autonomy)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from judge.core.models import AutonomyLevel
from judge.core.store import Store
from judge.memory.index import parse_index
from judge.memory.repo import RUNBOOK_DIR, MemoryRepo
from judge.memory.schema import DISTINGUISH_SECTION, Runbook, RunbookParseError, code_owned_dump
from judge.memory.stats import is_stale
from judge.settings import Config


class LintFinding(BaseModel):
    kind: Literal["stale", "orphan_action", "duplicate_signature", "index_missing", "index_dangling",
                  "missing_section", "missing_distinguish", "no_signatures", "unparseable", "no_match_conditions"]
    runbook_id: str | None = None
    detail: str
    severity: Literal["info", "warn", "error"] = "warn"


def lint(repo: MemoryRepo, config: Config, store: Store | None, now: datetime) -> list[LintFinding]:
    findings: list[LintFinding] = []
    runbooks: list[Runbook] = []
    for rel in repo.list_files(RUNBOOK_DIR):
        if not rel.endswith(".md"):
            continue
        try:
            runbooks.append(Runbook.parse(repo.read(rel) or ""))
        except RunbookParseError as e:
            findings.append(LintFinding(kind="unparseable", detail=f"{rel}: {e}", severity="error"))

    by_fp: dict[str, list[str]] = defaultdict(list)
    for rb in runbooks:
        fm = rb.frontmatter
        if is_stale(fm.stats, config.autonomy, now):
            findings.append(LintFinding(kind="stale", runbook_id=fm.id,
                                        detail=f"last_verified {fm.stats.last_verified} older than "
                                               f"{config.autonomy.stale_after_days} days"))
        if fm.action and fm.action.name not in config.actions:
            findings.append(LintFinding(kind="orphan_action", runbook_id=fm.id, severity="error",
                                        detail=f"action {fm.action.name!r} no longer in actions.yaml"))
        if fm.action and not fm.match_conditions:
            findings.append(LintFinding(kind="no_match_conditions", runbook_id=fm.id,
                                        detail="has an action but no match_conditions; cannot be used to auto-fix"))
        missing = rb.missing_sections()
        if DISTINGUISH_SECTION in missing:
            findings.append(LintFinding(kind="missing_distinguish", runbook_id=fm.id,
                                        detail=f"section '{DISTINGUISH_SECTION}' is missing or empty"))
        other = [s for s in missing if s != DISTINGUISH_SECTION]
        if other:
            findings.append(LintFinding(kind="missing_section", runbook_id=fm.id, detail=f"missing {other}"))
        if not (fm.signatures.fingerprints or fm.signatures.error_types):
            findings.append(LintFinding(kind="no_signatures", runbook_id=fm.id, detail="no signatures"))
        for fp in fm.signatures.fingerprints:
            by_fp[fp].append(fm.id)

    for fp, ids in sorted(by_fp.items()):
        if len(ids) > 1:
            for rid in ids:
                findings.append(LintFinding(kind="duplicate_signature", runbook_id=rid, severity="error",
                                            detail=f"fingerprint {fp} also claimed by {[i for i in ids if i != rid]}"))

    index_ids = {e["id"] for e in parse_index(repo.read("wiki/index.md") or "")}
    page_ids = {rb.id for rb in runbooks}
    for rid in sorted(page_ids - index_ids):
        findings.append(LintFinding(kind="index_missing", runbook_id=rid, detail="runbook not listed in index.md"))
    for rid in sorted(index_ids - page_ids):
        findings.append(LintFinding(kind="index_dangling", runbook_id=rid, detail="index.md points to missing page"))
    return findings


def apply_lint(repo: MemoryRepo, config: Config, store: Store | None, now: datetime) -> list[LintFinding]:
    """Stale -> autonomy capped at L1. Orphan action -> L0 + review_required. Committed as code-owned."""
    findings = lint(repo, config, store, now)
    stale = {f.runbook_id for f in findings if f.kind == "stale"}
    orphan = {f.runbook_id for f in findings if f.kind == "orphan_action"}
    changed: dict[str, str] = {}
    for rb in repo.runbooks():
        fm = rb.frontmatter
        before = code_owned_dump(fm)
        if fm.id in stale and fm.autonomy.level.rank > AutonomyLevel.L1.rank:
            fm.autonomy.level = AutonomyLevel.L1
        if fm.id in orphan:
            fm.autonomy.level = AutonomyLevel.L0
            fm.autonomy.review_required = True
        if code_owned_dump(fm) != before:
            changed[rb.path] = rb.to_markdown()
    if changed:
        repo.commit_code_owned(changed, f"lint: demote {sorted(r.split('/')[-1][:-3] for r in changed)}")
    return findings
