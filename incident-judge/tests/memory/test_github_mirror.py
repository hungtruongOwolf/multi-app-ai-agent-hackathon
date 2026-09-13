"""GitHub mirror of the LLM Wiki: main sync, proposal PRs, merge/close detection (fake GitHub API)."""

from __future__ import annotations

from judge.memory.github_mirror import GitHubMirror


class FakeGitHub:
    def __init__(self):
        self.branches: dict[str, dict[str, str]] = {"main": {}}
        self.commits: list[tuple[str, dict, str]] = []
        self.prs: dict[int, dict] = {}
        self.merge_ok = True

    async def read_file(self, path, ref="main"):
        return self.branches.get(ref, {}).get(path)

    async def commit_files(self, branch, files, message, create_from=None):
        tree = self.branches.setdefault(branch, dict(self.branches[create_from or "main"]))
        for k, v in files.items():
            if v is None:
                tree.pop(k, None)
            else:
                tree[k] = v
        self.commits.append((branch, dict(files), message))
        return f"sha{len(self.commits):037d}"

    async def open_pr(self, branch, title, body, labels=None, base="main"):
        n = len(self.prs) + 1
        self.prs[n] = {"number": n, "url": f"https://github.com/o/r/pull/{n}", "branch": branch,
                       "state": "open", "merged": False, "merged_by": None, "labels": labels}
        return {"number": n, "url": self.prs[n]["url"]}

    async def get_pr(self, number):
        return self.prs[number]

    async def merge_pr(self, number, title):
        if not self.merge_ok:
            return False
        pr = self.prs[number]
        self.branches["main"].update(self.branches[pr["branch"]])
        pr.update(state="closed", merged=True, merged_by="agent")
        return True

    async def close_pr(self, number, comment=None):
        self.prs[number].update(state="closed", comment=comment)


async def test_sync_main_baselines_then_pushes_only_changes(repo):
    gh = FakeGitHub()
    m = GitHubMirror(repo, gh)
    assert await m.sync_main("first") is None and gh.commits == []  # baseline: never overwrite knowledge/
    assert await m.sync_main("again") is None
    prop = repo.create_proposal({"wiki/log.md": (repo.read("wiki/log.md") or "") + "\n- learned\n"}, "t", "b")
    repo.merge_proposal(prop.id)
    assert await m.sync_main("merged") is not None
    files = gh.commits[-1][1]
    assert "knowledge/wiki/log.md" in files and all(k.startswith("knowledge/") for k in files)
    assert await m.sync_main("noop") is None


async def test_proposal_pr_merge_and_reject(repo):
    gh = FakeGitHub()
    m = GitHubMirror(repo, gh)
    await m.sync_main()
    prop = repo.create_proposal({"wiki/raw-note.md": "hello\n"}, "Add note", "because")
    pr = await m.open_pr(prop, "SEV2 checkout incident", "Slack: thread")
    assert pr["number"] == 1 and gh.prs[1]["labels"] == ["incident-judge", "knowledge"]
    assert gh.branches[f"incident-judge/{prop.id}"]["knowledge/wiki/raw-note.md"] == "hello\n"
    assert await m.open_pr(prop, "x", "y") == pr  # idempotent
    assert await m.merged_on_github(prop.id) is None and not await m.closed_on_github(prop.id)

    gh.prs[1].update(state="closed", merged=True, merged_by="octocat")
    assert (await m.merged_on_github(prop.id))["merged_by"] == "octocat"

    prop2 = repo.create_proposal({"wiki/other.md": "x\n"}, "Other", "b")
    await m.open_pr(prop2, "i", "s")
    await m.reject(prop2.id, "rejected in Slack")
    assert await m.closed_on_github(prop2.id)


async def test_merge_falls_back_to_close_and_apply(repo):
    gh = FakeGitHub()
    m = GitHubMirror(repo, gh)
    await m.sync_main()
    prop = repo.create_proposal({"wiki/note.md": "n\n"}, "Note", "b")
    await m.open_pr(prop, "i", "s")
    gh.merge_ok = False
    repo.merge_proposal(prop.id)
    await m.merge(prop.id, "Note")
    assert gh.prs[1]["state"] == "closed" and not gh.prs[1]["merged"]
    assert gh.branches["main"]["knowledge/wiki/note.md"] == "n\n"
