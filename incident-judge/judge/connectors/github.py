"""GitHub REST (git data + pulls) for the monorepo: wiki proposals as real pull requests, code-owned wiki commits,
and config-as-code commits for ShopLab changes. Auth: `Authorization: Bearer <token>`.

Multi-file commits use the git data API (tree → commit → ref) so a proposal is exactly one commit on its branch."""

from __future__ import annotations

from typing import Any

from judge.connectors.transport import ConnectorError, HttpClient, check

APP = "github"


class GitHubClient:
    def __init__(self, token: str, repo: str, http: HttpClient, base_url: str = "https://api.github.com"):
        self.repo = repo  # "owner/name"
        self.http = http
        self.base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"}

    def _url(self, path: str) -> str:
        return f"{self.base}/repos/{self.repo}/{path.lstrip('/')}"

    async def _req(self, op: str, method: str, path: str, **kw: Any) -> Any:
        resp = await self.http.request(APP, op, method, self._url(path), headers=self._headers, **kw)
        return check(APP, op, resp)

    async def repo_info(self) -> dict:
        """Repository metadata, including the token's `permissions` (pull, push, admin)."""
        resp = await self.http.request(APP, "get_repo", "GET", f"{self.base}/repos/{self.repo}", headers=self._headers)
        return check(APP, "get_repo", resp)

    # ------------------------------------------------------------ refs / commits

    async def head_sha(self, branch: str = "main") -> str:
        return (await self._req("get_ref", "GET", f"git/ref/heads/{branch}"))["object"]["sha"]

    async def commit_files(self, branch: str, files: dict[str, str | None], message: str,
                           create_from: str | None = None) -> str:
        """One commit with all `files` (None deletes) on `branch`; creates the branch from `create_from` if given."""
        if create_from is not None:
            base_sha = await self.head_sha(create_from)
            await self._req("create_ref", "POST", "git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        else:
            base_sha = await self.head_sha(branch)
        base_tree = (await self._req("get_commit", "GET", f"git/commits/{base_sha}"))["tree"]["sha"]
        tree = [({"path": p, "mode": "100644", "type": "blob", "content": c} if c is not None
                 else {"path": p, "mode": "100644", "type": "blob", "sha": None}) for p, c in files.items()]
        new_tree = (await self._req("create_tree", "POST", "git/trees",
                                    json={"base_tree": base_tree, "tree": tree}))["sha"]
        commit = await self._req("create_commit", "POST", "git/commits",
                                 json={"message": message, "tree": new_tree, "parents": [base_sha]})
        await self._req("update_ref", "PATCH", f"git/refs/heads/{branch}", json={"sha": commit["sha"], "force": False})
        return commit["sha"]

    async def read_file(self, path: str, ref: str = "main") -> str | None:
        import base64

        try:
            data = await self._req("get_contents", "GET", f"contents/{path}", params={"ref": ref})
        except ConnectorError as e:
            if e.status == 404:
                return None
            raise
        return base64.b64decode(data["content"]).decode("utf-8")

    def commit_url(self, sha: str) -> str:
        return f"https://github.com/{self.repo}/commit/{sha}"

    # ------------------------------------------------------------ pull requests

    async def open_pr(self, branch: str, title: str, body: str, labels: list[str] | None = None,
                      base: str = "main") -> dict:
        pr = await self._req("create_pr", "POST", "pulls", json={"title": title, "head": branch, "base": base,
                                                                 "body": body, "maintainer_can_modify": True})
        if labels:
            try:
                await self._req("add_labels", "POST", f"issues/{pr['number']}/labels", json={"labels": labels})
            except ConnectorError:
                pass  # labels are a convenience
        return {"number": pr["number"], "url": pr["html_url"], "branch": branch}

    async def get_pr(self, number: int) -> dict:
        pr = await self._req("get_pr", "GET", f"pulls/{number}")
        return {"number": number, "url": pr["html_url"], "state": pr["state"], "merged": bool(pr.get("merged")),
                "merged_by": (pr.get("merged_by") or {}).get("login")}

    async def merge_pr(self, number: int, title: str) -> bool:
        try:
            await self._req("merge_pr", "PUT", f"pulls/{number}/merge",
                            json={"merge_method": "squash", "commit_title": title})
            return True
        except ConnectorError as e:
            if e.status in (405, 409):  # not mergeable (conflict) — caller applies content and closes the PR
                return False
            raise

    async def close_pr(self, number: int, comment: str | None = None) -> None:
        if comment:
            await self._req("comment_pr", "POST", f"issues/{number}/comments", json={"body": comment})
        await self._req("close_pr", "PATCH", f"pulls/{number}", json={"state": "closed"})

    async def comment(self, number: int, body: str) -> None:
        await self._req("comment_pr", "POST", f"issues/{number}/comments", json={"body": body})
