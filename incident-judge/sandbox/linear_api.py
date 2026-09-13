"""Linear GraphQL emulation. No GraphQL parsing: dispatch on operationName + variables.
The connector sends documents that are valid against the real Linear API; this returns full objects."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sandbox.db import DocStore, iso, utcnow

TEAM_ID = "team_shoplab"
TEAM_KEY = "SHO"
STATES = [
    {"id": "state_todo", "name": "Todo", "type": "unstarted"},
    {"id": "state_in_progress", "name": "In Progress", "type": "started"},
    {"id": "state_done", "name": "Done", "type": "completed"},
    {"id": "state_canceled", "name": "Canceled", "type": "canceled"},
]
LABELS = [{"id": "label_ij_eval", "name": "ij-eval"}, {"id": "label_incident", "name": "incident"}]


def _err(message: str, type_: str = "invalid input", status: int = 200) -> JSONResponse:
    return JSONResponse({"errors": [{"message": message, "extensions": {"type": type_}}], "data": None},
                        status_code=status)


class LinearEmulator:
    def __init__(self, db: DocStore):
        self.db = db

    def seed(self) -> None:
        pass  # states and seeded labels are static

    def labels(self) -> list[dict[str, Any]]:
        return [*LABELS, *self.db.all("linear_label")]

    def issue_json(self, issue: dict[str, Any]) -> dict[str, Any]:
        state = next(s for s in STATES if s["id"] == issue["stateId"])
        comments = self.db.all("linear_comment", lambda c: c["issueId"] == issue["id"])
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "description": issue["description"],
            "priority": issue["priority"],
            "url": f"https://linear.app/shoplab/issue/{issue['identifier']}",
            "createdAt": issue["createdAt"],
            "updatedAt": issue["updatedAt"],
            "completedAt": issue.get("completedAt"),
            "state": state,
            "team": {"id": TEAM_ID, "key": TEAM_KEY},
            "labels": {"nodes": [{"id": l["id"], "name": l["name"]} for l in self.labels() if l["id"] in issue["labelIds"]]},
            "comments": {"nodes": [{"id": c["id"], "body": c["body"], "createdAt": c["createdAt"]} for c in comments]},
        }

    def handle(self, op: str, v: dict[str, Any]) -> dict[str, Any] | JSONResponse:
        if op == "IssueCreate":
            inp = v.get("input") or {}
            if not inp.get("title") or not inp.get("teamId"):
                return _err("title and teamId are required")
            if inp["teamId"] != TEAM_ID:
                return _err("Entity not found: Team", "invalid input")
            priority = int(inp.get("priority", 0))
            if priority not in range(5):
                return _err("priority must be 0..4")
            n = self.db.next("linear_issue_number")
            now = iso(utcnow())
            issue = {
                "id": str(uuid.uuid4()),
                "identifier": f"{TEAM_KEY}-{n}",
                "title": inp["title"],
                "description": inp.get("description") or "",
                "priority": priority,
                "stateId": inp.get("stateId") or "state_todo",
                "labelIds": list(inp.get("labelIds") or []),
                "createdAt": now,
                "updatedAt": now,
            }
            self.db.put("linear_issue", issue["id"], issue)
            return {"issueCreate": {"success": True, "issue": self.issue_json(issue)}}

        if op == "IssueUpdate":
            issue = self.db.get("linear_issue", v.get("id", ""))
            if issue is None:
                return _err("Entity not found: Issue")
            inp = v.get("input") or {}
            if "stateId" in inp:
                if not any(s["id"] == inp["stateId"] for s in STATES):
                    return _err("Entity not found: WorkflowState")
                issue["stateId"] = inp["stateId"]
                state = next(s for s in STATES if s["id"] == inp["stateId"])
                issue["completedAt"] = iso(utcnow()) if state["type"] == "completed" else None
            for field in ("priority", "description", "title"):
                if field in inp:
                    issue[field] = inp[field]
            if "labelIds" in inp:
                issue["labelIds"] = list(inp["labelIds"])
            issue["updatedAt"] = iso(utcnow())
            self.db.put("linear_issue", issue["id"], issue)
            return {"issueUpdate": {"success": True, "issue": self.issue_json(issue)}}

        if op == "IssueDelete":
            ok = self.db.delete("linear_issue", v.get("id", ""))
            if not ok:
                return _err("Entity not found: Issue")
            for c in self.db.all("linear_comment", lambda c: c["issueId"] == v["id"]):
                self.db.delete("linear_comment", c["id"])
            return {"issueDelete": {"success": True}}

        if op == "CommentCreate":
            inp = v.get("input") or {}
            if self.db.get("linear_issue", inp.get("issueId", "")) is None:
                return _err("Entity not found: Issue")
            comment = {"id": str(uuid.uuid4()), "issueId": inp["issueId"], "body": inp.get("body") or "",
                       "createdAt": iso(utcnow())}
            self.db.put("linear_comment", comment["id"], comment)
            return {"commentCreate": {"success": True, "comment": {"id": comment["id"], "body": comment["body"]}}}

        if op == "IssueGet":
            issue = self.db.get("linear_issue", v.get("id", ""))
            if issue is None:
                return _err("Entity not found: Issue")
            return {"issue": self.issue_json(issue)}

        if op == "IssuesByDescription":
            needle = v.get("contains") or ""
            nodes = [self.issue_json(i) for i in self.db.all("linear_issue", lambda i: needle in i["description"])]
            return {"issues": {"nodes": nodes[: int(v.get("first", 50))], "pageInfo": {"hasNextPage": False}}}

        if op == "IssuesByLabel":
            label = v.get("labelId") or ""
            nodes = [self.issue_json(i) for i in self.db.all("linear_issue", lambda i: label in i["labelIds"])]
            return {"issues": {"nodes": nodes[: int(v.get("first", 50))], "pageInfo": {"hasNextPage": False}}}

        if op == "WorkflowStates":
            if v.get("teamId") and v["teamId"] != TEAM_ID:
                return {"workflowStates": {"nodes": []}}
            return {"workflowStates": {"nodes": STATES}}

        if op == "Viewer":
            return {"viewer": {"id": "user_sandbox", "name": "Sandbox", "email": "sandbox@shoplab.test"}}

        if op == "Teams":
            return {"teams": {"nodes": [{"id": TEAM_ID, "key": TEAM_KEY, "name": "ShopLab"}]}}

        if op == "IssueLabels":
            nodes = [{"id": l["id"], "name": l["name"], "team": {"id": l.get("teamId", TEAM_ID)}} for l in self.labels()]
            return {"issueLabels": {"nodes": nodes[: int(v.get("first", 250))]}}

        if op == "IssueLabelCreate":
            inp = v.get("input") or {}
            name = str(inp.get("name") or "").strip()
            if not name:
                return _err("name is required")
            if inp.get("teamId") and inp["teamId"] != TEAM_ID:
                return _err("Entity not found: Team")
            if any(l["name"].lower() == name.lower() for l in self.labels()):
                return _err("duplicate label name")
            label = {"id": f"label_{uuid.uuid4().hex[:10]}", "name": name, "teamId": inp.get("teamId") or TEAM_ID,
                     "color": inp.get("color") or "#6B7280"}
            self.db.put("linear_label", label["id"], label)
            return {"issueLabelCreate": {"success": True, "issueLabel": {"id": label["id"], "name": name}}}

        return _err(f"Unknown operation {op!r} (sandbox dispatches on operationName)", "graphql error", 400)


def build_router(emu: LinearEmulator) -> APIRouter:
    r = APIRouter()

    @r.post("/linear/graphql")
    async def graphql(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth:
            return _err("Authentication required, not authenticated", "authentication error", 401)
        if auth.lower().startswith("bearer "):
            # Real Linear: personal API keys are sent WITHOUT the Bearer prefix (Bearer is for OAuth tokens).
            return _err("Authentication required, not authenticated (API keys must not use 'Bearer')",
                        "authentication error", 401)
        body = await request.json()
        op = body.get("operationName")
        if not op:
            return _err("operationName is required by the sandbox", "graphql error", 400)
        result = emu.handle(op, body.get("variables") or {})
        if isinstance(result, JSONResponse):
            return result
        return {"data": result}

    return r
