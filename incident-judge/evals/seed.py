"""Per-trial seeding: fresh memory repo (from fixtures) + seeded outcomes in the agent's SQLite.

Autonomy levels in eval come from seeded outcomes (honest weakness, SPEC §22): the code-owned
stats/autonomy are recomputed by the memory lane's own `sync_code_owned`, never hand-written."""

from __future__ import annotations

import shutil
import stat
from datetime import timedelta
from pathlib import Path

from evals.graders.common import frontmatter
from evals.scenario import FIXTURES_DIR, Scenario, load_outcomes_fixture
from judge.core.models import Outcome, OutcomeResult, now
from judge.core.store import Store
from judge.settings import Config, Settings


def _rm(path: Path) -> None:
    def onerror(func, p, _exc):
        Path(p).chmod(stat.S_IWRITE)
        func(p)

    if path.is_dir():
        shutil.rmtree(path, onerror=onerror)
    elif path.exists():
        path.unlink()


def clean_trial_files(settings: Settings) -> None:
    _rm(settings.memory_dir)
    for suffix in ("", "-wal", "-shm"):
        _rm(Path(str(settings.db_path) + suffix))


def seed_trial(s: Scenario, settings: Settings, config: Config | None = None) -> dict:
    from judge.memory.repo import MemoryRepo, seed_runbooks
    from judge.memory.stats import sync_code_owned

    config = config or Config()
    clean_trial_files(settings)
    settings.var_dir.mkdir(parents=True, exist_ok=True)

    store = Store(settings.db_path)
    outcome_ids: list[str] = []
    t = now()
    try:
        for name in s.memory_fixture.outcomes:
            fx = load_outcomes_fixture(name)
            for n, o in enumerate(fx.outcomes):
                out = Outcome(runbook_id=fx.runbook_id, incident_id=f"seed_inc_{name}_{n}",
                              plan_id=f"seed_plan_{name}_{n}", action=fx.action, result=OutcomeResult(o.result),
                              trial_id=None, ts=t - timedelta(days=o.days_ago))
                store.add_outcome(out)
                outcome_ids.append(out.outcome_id)

        repo = MemoryRepo(settings.memory_dir)
        repo.ensure()
        paths = [FIXTURES_DIR / "memory" / f"{rb}.md" for rb in s.memory_fixture.runbooks]
        if paths:
            seed_runbooks(repo, paths)
        if s.memory_fixture.raw:
            raw_files = {f"raw/incidents/{name}.md": (FIXTURES_DIR / "raw" / f"{name}.md").read_text(encoding="utf-8")
                         for name in s.memory_fixture.raw}
            repo.commit_code_owned(raw_files, "seed: raw incidents")
        sync_code_owned(repo, store, config, t)

        proposals, records = [], []
        for p in s.memory_fixture.proposals:
            files = {path: (FIXTURES_DIR / src).read_text(encoding="utf-8") for path, src in p.files.items()}
            prop = repo.create_proposal(files, p.title, p.body)
            proposals.append({"title": p.title, "files": files, "id": prop.id, "branch": prop.branch})
            records.append(prop.model_dump(mode="json"))

        runbooks = {}
        runbook_paths = []
        for rb in repo.runbooks():
            runbooks[rb.id] = frontmatter(rb.to_markdown())
            runbook_paths.append(rb.path)
        return {
            "outcome_ids": outcome_ids,
            "runbooks": runbooks,
            "runbook_paths": runbook_paths,
            "branches": [p["branch"] for p in proposals],
            "proposals": proposals,
            "proposal_records": records,
            "log_len": len(repo.read("wiki/log.md") or ""),
        }
    finally:
        store.close()
