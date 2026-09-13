"""Local git memory repository: the copy the agent reads. With GitHub configured it is mirrored to the monorepo's
`knowledge/` by `judge.memory.github_mirror` (proposals become pull requests); this module never pushes itself.

All writes use git plumbing (hash-object / temporary index / commit-tree / update-ref with CAS),
so branches can be written without touching the working tree. `main` is the reviewed truth.

- commit_code_owned: code-generated facts (raw timelines, log lines, stats/autonomy) straight to main
- create_proposal:   LLM-authored prose on a `proposal/<id>` branch; main is untouched
- merge_proposal:    overlay proposal files onto CURRENT main, re-applying main's code-owned
                     frontmatter, union-merging log.md and regenerating index.md
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from judge.core.models import RunbookStats, canonical_hash, new_id, now
from judge.memory.index import merge_log, render_index
from judge.memory.schema import (
    CODE_OWNED_FIELDS,
    Runbook,
    RunbookAutonomy,
    RunbookParseError,
)
from judge.settings import ROOT

CODE_BOT = ("ij-code-bot", "code-bot@incident-judge.local")
LLM_AUTHOR = ("ij-llm", "llm@incident-judge.local")
from judge.paths import KNOWLEDGE_DIR

DEFAULT_TEMPLATE = KNOWLEDGE_DIR
RUNBOOK_DIR = "wiki/runbooks"


class GitError(RuntimeError):
    pass


class Proposal(BaseModel):
    id: str
    branch: str
    title: str
    body: str = ""
    author: str = LLM_AUTHOR[0]
    files: list[str]
    base_sha: str
    head_sha: str
    status: str = "open"  # open | merged | rejected
    hash: str
    created_at: datetime
    merged_sha: str | None = None
    reason: str | None = None


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class MemoryRepo:
    def __init__(self, path: Path | str, template_dir: Path | str = DEFAULT_TEMPLATE):
        self.path = Path(path).resolve()
        self.template_dir = Path(template_dir)
        self._lock = threading.RLock()
        self._guard_location()

    # ------------------------------------------------------------ plumbing

    def _guard_location(self) -> None:
        root = ROOT.resolve()
        if self.path == root or self.path in root.parents:
            raise GitError(f"refusing to use {self.path} as memory repo (project root or its ancestor)")
        if root in self.path.parents and (root / "var") not in [self.path, *self.path.parents]:
            raise GitError(f"memory repo inside the project must live under var/: {self.path}")

    def _git(self, *args: str, input: bytes | None = None, env: dict | None = None, check: bool = True) -> str:
        full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
        if env:
            full_env.update(env)
        # safe.directory: drives like exFAT don't record ownership; scoped to this command, never global config
        cmd = ["git", "-c", f"safe.directory={self.path.as_posix()}", "-c", "core.autocrlf=false",
               "-c", "core.hooksPath=", "-c", "commit.gpgsign=false",
               "-C", str(self.path), *args]
        proc = subprocess.run(cmd, input=input, capture_output=True, env=full_env)
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc.stdout.decode("utf-8", "replace")

    def _rev(self, ref: str) -> str | None:
        out = self._git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).strip()
        return out or None

    def _ij_dir(self) -> Path:
        d = self.path / ".git" / "ij"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _commit_files(self, *, parent: str, files: dict[str, str | None], message: str,
                      author: tuple[str, str], extra_parents: list[str] | None = None) -> str:
        """Build a commit = parent tree + files (None deletes). Returns commit sha (ref not moved)."""
        index_file = self._ij_dir() / f"index-{uuid.uuid4().hex}"
        env = {"GIT_INDEX_FILE": str(index_file)}
        try:
            self._git("read-tree", parent, env=env)
            lines = []
            for rel, content in files.items():
                rel = rel.replace("\\", "/")
                if content is None:
                    self._git("update-index", "--force-remove", "--", rel, env=env)
                    continue
                data = content.replace("\r\n", "\n").encode("utf-8")
                blob = self._git("hash-object", "-w", "--stdin", input=data).strip()
                lines.append(f"100644 {blob}\t{rel}")
            if lines:
                self._git("update-index", "--index-info", input=("\n".join(lines) + "\n").encode("utf-8"), env=env)
            tree = self._git("write-tree", env=env).strip()
        finally:
            index_file.unlink(missing_ok=True)
        parents = ["-p", parent] + [x for p in (extra_parents or []) for x in ("-p", p)]
        name, email = author
        cenv = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
        return self._git("commit-tree", tree, *parents, input=message.encode("utf-8"), env=cenv).strip()

    def _advance_main(self, new: str, old: str) -> None:
        self._git("update-ref", "refs/heads/main", new, old)  # CAS: fails if main moved
        self._git("reset", "--hard", "-q")  # keep the human-browsable working tree in sync

    # ------------------------------------------------------------ lifecycle

    def ensure(self) -> None:
        with self._lock:
            if (self.path / ".git").exists():
                self._assert_no_remote()
                return
            self.path.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q", "-b", "main")
            self._git("config", "user.name", CODE_BOT[0])
            self._git("config", "user.email", CODE_BOT[1])
            self._git("config", "core.autocrlf", "false")
            if self.template_dir.exists():
                # The knowledge base in the monorepo includes reviewed runbooks. Unless asked to start from them
                # (IJ_KNOWLEDGE_RUNBOOKS=1), a fresh memory starts with docs only and learns runbooks itself
                # (evals seed exactly the runbooks each scenario needs).
                with_runbooks = os.environ.get("IJ_KNOWLEDGE_RUNBOOKS") == "1"
                shutil.copytree(self.template_dir, self.path, dirs_exist_ok=True)
                if not with_runbooks:
                    for page in (self.path / RUNBOOK_DIR).glob("*.md"):
                        page.unlink()
                    from judge.memory.index import render_index

                    (self.path / "wiki" / "index.md").write_text(render_index([]), encoding="utf-8")
            self._git("add", "-A")
            name, email = CODE_BOT
            self._git("-c", f"user.name={name}", "-c", f"user.email={email}",
                      "commit", "-q", "--allow-empty", "-m", "init memory from template")
            self._assert_no_remote()

    def _assert_no_remote(self) -> None:
        if self._git("remote").strip():
            raise GitError("memory repo must not have a remote")

    def head(self, ref: str = "main") -> str:
        sha = self._rev(ref)
        if not sha:
            raise GitError(f"unknown ref {ref}")
        return sha

    # ------------------------------------------------------------ reads

    def read(self, rel: str, ref: str = "main") -> str | None:
        rel = rel.replace("\\", "/")
        proc = subprocess.run(
            ["git", "-c", f"safe.directory={self.path.as_posix()}", "-c", "core.autocrlf=false", "-C", str(self.path),
             "show", f"{ref}:{rel}"],
            capture_output=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8")

    def list_files(self, prefix: str, ref: str = "main") -> list[str]:
        out = self._git("ls-tree", "-r", "--name-only", ref, "--", prefix, check=False)
        return [l for l in out.splitlines() if l.strip()]

    def runbooks(self, ref: str = "main") -> list[Runbook]:
        out = []
        for rel in self.list_files(RUNBOOK_DIR, ref):
            if not rel.endswith(".md"):
                continue
            text = self.read(rel, ref)
            try:
                out.append(Runbook.parse(text or ""))
            except RunbookParseError:
                continue  # lint reports unparseable pages
        return out

    def get_runbook(self, runbook_id: str, ref: str = "main") -> Runbook | None:
        text = self.read(f"{RUNBOOK_DIR}/{runbook_id}.md", ref)
        if text is None:
            return None
        try:
            return Runbook.parse(text)
        except RunbookParseError:
            return None

    # ------------------------------------------------------------ code-owned writes

    def commit_code_owned(self, files: dict[str, str], message: str) -> str:
        """Direct commit to main by code. The ONLY path that may write stats/autonomy."""
        with self._lock:
            old = self.head()
            if all(self.read(rel) == content.replace("\r\n", "\n") for rel, content in files.items()):
                return old
            new = self._commit_files(parent=old, files=dict(files), message=message, author=CODE_BOT)
            self._advance_main(new, old)
            return new

    # ------------------------------------------------------------ proposals (LLM, reviewed)

    def _proposals_path(self) -> Path:
        return self._ij_dir() / "proposals.json"

    def _load_proposals(self) -> dict[str, Proposal]:
        p = self._proposals_path()
        if not p.exists():
            return {}
        return {k: Proposal.model_validate(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}

    def _save_proposals(self, props: dict[str, Proposal]) -> None:
        p = self._proposals_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in props.items()}, indent=2,
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def create_proposal(self, files: dict[str, str], title: str, body: str, author: str = "ij-llm") -> Proposal:
        with self._lock:
            base = self.head()
            pid = new_id("prop")
            branch = f"proposal/{pid}"
            head = self._commit_files(parent=base, files=dict(files), message=f"{title}\n\n{body}",
                                      author=(author, LLM_AUTHOR[1]))
            self._git("update-ref", f"refs/heads/{branch}", head)
            files_norm = sorted(f.replace("\\", "/") for f in files)
            prop = Proposal(
                id=pid, branch=branch, title=title, body=body, author=author, files=files_norm, base_sha=base,
                head_sha=head, created_at=now(),
                hash=canonical_hash({"base": base, "title": title,
                                     "files": {f: _content_hash(files[f]) for f in files}}),
            )
            props = self._load_proposals()
            props[pid] = prop
            self._save_proposals(props)
            return prop

    def proposals(self, status: str | None = None) -> list[Proposal]:
        props = sorted(self._load_proposals().values(), key=lambda p: p.created_at)
        return [p for p in props if status is None or p.status == status]

    def proposal(self, proposal_id: str) -> Proposal | None:
        return self._load_proposals().get(proposal_id)

    def merge_proposal(self, proposal_id: str) -> str:
        with self._lock:
            props = self._load_proposals()
            prop = props.get(proposal_id)
            if not prop:
                raise GitError(f"unknown proposal {proposal_id}")
            if prop.status != "open":
                raise GitError(f"proposal {proposal_id} is {prop.status}")
            if self._rev(prop.branch) != prop.head_sha:
                raise GitError("proposal branch moved since creation; re-review required")
            old = self.head()
            merged: dict[str, str | None] = {}
            for rel in prop.files:
                content = self.read(rel, prop.head_sha)
                if content is None:
                    continue
                if rel.startswith(RUNBOOK_DIR + "/") and rel.endswith(".md"):
                    content = self._reapply_code_owned(rel, content)
                elif rel == "wiki/log.md":
                    content = merge_log(self.read(rel) or "", content)
                elif rel == "wiki/index.md":
                    continue  # regenerated below from the merged runbooks
                merged[rel] = content
            # regenerate index from the tree as it will be after merge
            staged = {rb.path: rb for rb in self.runbooks()}
            for rel, content in merged.items():
                if rel.startswith(RUNBOOK_DIR + "/") and content:
                    try:
                        rb = Runbook.parse(content)
                        staged[rel] = rb
                    except RunbookParseError:
                        pass
            merged["wiki/index.md"] = render_index(list(staged.values()))
            new = self._commit_files(parent=old, files=merged, extra_parents=[prop.head_sha],
                                     message=f"Merge {prop.id}: {prop.title}", author=CODE_BOT)
            self._advance_main(new, old)
            prop.status, prop.merged_sha = "merged", new
            props[prop.id] = prop
            self._save_proposals(props)
            return new

    def _reapply_code_owned(self, rel: str, content: str) -> str:
        rb = Runbook.parse(content)  # validator must have passed; parse errors propagate
        current = self.read(rel)
        if current is not None:
            main_rb = Runbook.parse(current)
            for field in CODE_OWNED_FIELDS:
                setattr(rb.frontmatter, field, getattr(main_rb.frontmatter, field).model_copy(deep=True))
        else:  # new page: code-owned zone starts from defaults, whatever the branch says
            rb.frontmatter.stats = RunbookStats()
            rb.frontmatter.autonomy = RunbookAutonomy()
        return rb.to_markdown()

    def reject_proposal(self, proposal_id: str, reason: str) -> None:
        with self._lock:
            props = self._load_proposals()
            prop = props.get(proposal_id)
            if not prop:
                raise GitError(f"unknown proposal {proposal_id}")
            if prop.status != "open":
                raise GitError(f"proposal {proposal_id} is {prop.status}")
            prop.status, prop.reason = "rejected", reason
            props[prop.id] = prop
            self._save_proposals(props)


def seed_runbooks(repo: MemoryRepo, paths: list[Path | str]) -> str:
    """Eval/demo fixture seeding: commit runbook pages to main and regenerate index.md.
    Code-owned values in fixtures are placeholders; run stats.sync_code_owned afterwards."""
    repo.ensure()
    files: dict[str, str] = {}
    pages = {rb.id: rb for rb in repo.runbooks()}
    for p in paths:
        rb = Runbook.parse(Path(p).read_text(encoding="utf-8"))
        files[rb.path] = rb.to_markdown()
        pages[rb.id] = rb
    files["wiki/index.md"] = render_index(list(pages.values()))
    return repo.commit_code_owned(files, f"seed: runbooks {sorted(pages)}")
