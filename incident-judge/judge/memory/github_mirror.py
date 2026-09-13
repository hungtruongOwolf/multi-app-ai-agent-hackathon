"""Mirror the local LLM Wiki to the GitHub monorepo (`knowledge/`), so humans review what the agent learned where
they already review code.

- Code-owned commits on the local `main` (raw timelines, log, recomputed stats/autonomy) are pushed to GitHub `main`.
- Every wiki proposal becomes a real pull request (label `incident-judge`, `knowledge`).
- A proposal is merged either by clicking Merge in Slack (the agent merges the PR) or by merging the PR on GitHub
  (the agent notices and merges locally). Validation (P12) always runs before either path.

The local git repo stays the source the agent reads from (fast, offline-safe, already validated); GitHub is the
human-facing copy. State (proposal → PR, last mirrored commit) lives in the local repo's .git/ij/github.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from judge.connectors.github import GitHubClient
from judge.memory.repo import MemoryRepo

log = logging.getLogger("judge.memory.github")


class GitHubMirror:
    def __init__(self, repo: MemoryRepo, gh: GitHubClient, prefix: str = "knowledge/"):
        self.repo = repo
        self.gh = gh
        self.prefix = prefix.rstrip("/") + "/"

    # ------------------------------------------------------------ state
    @property
    def _state_path(self) -> Path:
        return self.repo.path / ".git" / "ij" / "github.json"

    def _state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"prs": {}, "mirrored_sha": None}

    def _save(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def pr_for(self, proposal_id: str) -> dict | None:
        return self._state()["prs"].get(proposal_id)

    # ------------------------------------------------------------ main
    async def sync_main(self, message: str = "knowledge: sync from Incident Judge") -> str | None:
        """Push files changed on local main since the last mirror (skipping ones already identical on GitHub)."""
        state = self._state()
        head = self.repo.head()
        base = state.get("mirrored_sha")
        if base == head:
            return None
        rng = f"{base}..{head}" if base else head
        names = self.repo._git("diff", "--name-only", rng) if base else \
            self.repo._git("ls-tree", "-r", "--name-only", head)
        files: dict[str, str | None] = {}
        for rel in (n.strip() for n in names.splitlines() if n.strip()):
            if rel.startswith((".", "AGENTS.md")) and rel != "AGENTS.md":
                continue
            local = self.repo.read(rel)
            remote = await self.gh.read_file(self.prefix + rel)
            if local != remote:
                files[self.prefix + rel] = local
        sha = None
        if files:
            sha = await self.gh.commit_files("main", files, message)
            log.info("mirrored %d knowledge files to GitHub (%s)", len(files), sha[:8])
        state["mirrored_sha"] = head
        self._save(state)
        return sha

    # ------------------------------------------------------------ proposals
    async def open_pr(self, proposal, incident_title: str, slack_hint: str) -> dict:
        existing = self.pr_for(proposal.id)
        if existing:
            return existing
        files = {self.prefix + rel: self.repo.read(rel, ref=proposal.branch) for rel in proposal.files}
        branch = f"incident-judge/{proposal.id}"
        await self.gh.commit_files(branch, files, proposal.title, create_from="main")
        body = (f"**What the agent learned from:** {incident_title}\n\n{proposal.body}\n\n"
                "---\n"
                "* Written by Incident Judge from the incident's raw timeline. Stats and autonomy levels are "
                "code-owned and cannot be changed by this PR (checked before merge).\n"
                f"* Merge here, or click **Merge** on the card in Slack. {slack_hint}")
        pr = await self.gh.open_pr(branch, proposal.title, body, labels=["incident-judge", "knowledge"])
        state = self._state()
        state["prs"][proposal.id] = pr
        self._save(state)
        return pr

    async def merged_on_github(self, proposal_id: str) -> dict | None:
        pr = self.pr_for(proposal_id)
        if not pr:
            return None
        info = await self.gh.get_pr(pr["number"])
        return info if info["merged"] else None

    async def closed_on_github(self, proposal_id: str) -> bool:
        pr = self.pr_for(proposal_id)
        if not pr:
            return False
        info = await self.gh.get_pr(pr["number"])
        return info["state"] == "closed" and not info["merged"]

    async def merge(self, proposal_id: str, title: str) -> None:
        """Called after the local merge succeeded (content validated, code-owned fields re-applied)."""
        pr = self.pr_for(proposal_id)
        if pr and not await self.gh.merge_pr(pr["number"], title):
            await self.gh.close_pr(pr["number"], "Approved in Slack. The branch conflicted with newer code-owned "
                                                 "updates, so the validated content was applied to `main` directly.")
        await self.sync_main(f"knowledge: {title}")

    async def reject(self, proposal_id: str, reason: str) -> None:
        pr = self.pr_for(proposal_id)
        if pr:
            await self.gh.close_pr(pr["number"], f"Not merged: {reason}")
