"""Validate an LLM proposal before it can be merged (rule P12). [] = ok.

Checks every file on the proposal branch against the proposal's base commit:
path allowlist, forbidden content, runbook schema, required sections, known action + valid params,
parseable match_conditions, known services, and code-owned fields untouched."""

from __future__ import annotations

import re

from pydantic import ValidationError

from judge.core.models import MetricCondition
from judge.memory.repo import RUNBOOK_DIR, MemoryRepo, Proposal
from judge.memory.schema import (
    CODE_OWNED_FIELDS,
    RUNBOOK_ID_RE,
    Runbook,
    RunbookFrontmatter,
    RunbookParseError,
    code_owned_dump,
    split_frontmatter,
)
from judge.policy.engine import validate_params
from judge.safety.redact import find_forbidden
from judge.settings import Config

ALLOWED_PATHS = [
    re.compile(rf"^{RUNBOOK_DIR}/[a-z0-9][a-z0-9-]*\.md$"),
    re.compile(r"^wiki/index\.md$"),
    re.compile(r"^wiki/log\.md$"),
]


def validate_proposal(repo: MemoryRepo, proposal: Proposal, config: Config) -> list[str]:
    errors: list[str] = []
    if proposal.status != "open":
        errors.append(f"proposal is {proposal.status}")
    branch_head = repo._rev(proposal.branch)
    if branch_head != proposal.head_sha:
        errors.append("proposal branch moved since creation (content no longer matches reviewed hash)")

    changed = set(repo._git("diff", "--name-only", proposal.base_sha, proposal.head_sha).split())
    undeclared = changed - set(proposal.files)
    if undeclared:
        errors.append(f"branch changes undeclared files: {sorted(undeclared)}")

    for rel in sorted(set(proposal.files) | changed):
        if not any(p.match(rel) for p in ALLOWED_PATHS):
            errors.append(f"{rel}: path not allowed for LLM proposals")
            continue
        content = repo.read(rel, proposal.head_sha)
        if content is None:
            errors.append(f"{rel}: deletion not allowed")
            continue
        hits = find_forbidden(content)
        if hits:
            errors.append(f"{rel}: forbidden content {hits}")
        if rel.startswith(RUNBOOK_DIR + "/"):
            errors += [f"{rel}: {e}" for e in _validate_runbook(repo, proposal, rel, content, config)]
    return errors


def _validate_runbook(repo: MemoryRepo, proposal: Proposal, rel: str, content: str, config: Config) -> list[str]:
    errs: list[str] = []
    try:
        data, _ = split_frontmatter(content)
    except RunbookParseError as e:
        return [str(e)]

    for i, raw in enumerate(data.get("match_conditions") or []):
        try:
            MetricCondition.model_validate(raw)
        except ValidationError as e:
            errs.append(f"match_conditions[{i}] unparseable: {e.errors()[0].get('msg')}")
    try:
        rb = Runbook.parse(content)
    except RunbookParseError as e:
        return errs + [f"schema: {e}"]

    fm = rb.frontmatter
    expected_id = rel.rsplit("/", 1)[1][:-3]
    if fm.id != expected_id:
        errs.append(f"frontmatter id {fm.id!r} does not match filename {expected_id!r}")
    if not RUNBOOK_ID_RE.match(fm.id):
        errs.append(f"invalid runbook id {fm.id!r}")
    missing = rb.missing_sections()
    if missing:
        errs.append(f"missing required sections: {missing}")
    if fm.action:
        spec = config.actions.get(fm.action.name)
        if spec is None:
            errs.append(f"unknown action {fm.action.name!r}")
        else:
            perr = validate_params(spec.params_schema, fm.action.params)
            if perr:
                errs.append(f"action params: {perr}")
    unknown_services = [s for s in fm.signatures.services if config.service(s) is None]
    if unknown_services:
        errs.append(f"unknown services in signatures: {unknown_services}")
    if not (fm.signatures.fingerprints or fm.signatures.error_types):
        errs.append("signatures must include fingerprints or error_types")

    base_text = repo.read(rel, proposal.base_sha)
    if base_text is not None:
        try:
            base_owned = code_owned_dump(Runbook.parse(base_text).frontmatter)
        except RunbookParseError:
            base_owned = None
    else:
        base_owned = code_owned_dump(RunbookFrontmatter(id=fm.id, title=fm.title))
    if base_owned is not None:
        now_owned = code_owned_dump(fm)
        for field in CODE_OWNED_FIELDS:
            if now_owned[field] != base_owned[field]:
                errs.append(f"code-owned field '{field}' modified (only code may write it)")
    return errs
