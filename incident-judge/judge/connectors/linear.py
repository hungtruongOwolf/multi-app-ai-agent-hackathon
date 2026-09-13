"""Linear GraphQL (real: https://api.linear.app/graphql, auth header `Authorization: <API_KEY>` — no Bearer).

Documents below are valid against the real schema and always carry operationName (the sandbox dispatches on it).
Linear has no idempotency keys: callers reconcile via markers in description/comment bodies."""

from __future__ import annotations

from typing import Any

from judge.connectors.transport import ConnectorError, HttpClient, check
from judge.settings import Settings

APP = "linear"

ISSUE_FIELDS = """
  id identifier title description priority url createdAt updatedAt completedAt
  state { id name type }
  labels { nodes { id name } }
"""

Q_ISSUE_CREATE = f"""mutation IssueCreate($input: IssueCreateInput!) {{
  issueCreate(input: $input) {{ success issue {{ {ISSUE_FIELDS} }} }}
}}"""

Q_ISSUE_UPDATE = f"""mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {{
  issueUpdate(id: $id, input: $input) {{ success issue {{ {ISSUE_FIELDS} }} }}
}}"""

Q_ISSUE_DELETE = """mutation IssueDelete($id: String!) {
  issueDelete(id: $id) { success }
}"""

Q_COMMENT_CREATE = """mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id body } }
}"""

Q_ISSUE_GET = f"""query IssueGet($id: String!) {{
  issue(id: $id) {{ {ISSUE_FIELDS} comments(first: 100) {{ nodes {{ id body createdAt }} }} }}
}}"""

Q_ISSUES_BY_DESCRIPTION = f"""query IssuesByDescription($contains: String!, $first: Int) {{
  issues(filter: {{ description: {{ contains: $contains }} }}, first: $first) {{
    nodes {{ {ISSUE_FIELDS} }}
    pageInfo {{ hasNextPage }}
  }}
}}"""

Q_ISSUES_BY_LABEL = f"""query IssuesByLabel($labelId: ID!, $first: Int) {{
  issues(filter: {{ labels: {{ id: {{ eq: $labelId }} }} }}, first: $first) {{
    nodes {{ {ISSUE_FIELDS} }}
    pageInfo {{ hasNextPage }}
  }}
}}"""

Q_VIEWER = """query Viewer {
  viewer { id name email }
}"""

Q_TEAMS = """query Teams {
  teams(first: 50) { nodes { id key name } }
}"""

Q_ISSUE_LABELS = """query IssueLabels {
  issueLabels(first: 250) { nodes { id name team { id } } }
}"""

Q_ISSUE_LABEL_CREATE = """mutation IssueLabelCreate($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) { success issueLabel { id name } }
}"""

Q_WORKFLOW_STATES = """query WorkflowStates($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) { nodes { id name type } }
}"""


class LinearClient:
    def __init__(self, settings: Settings, http: HttpClient):
        self.s = settings
        self.http = http
        self._states: list[dict[str, Any]] | None = None

    async def _gql(self, op: str, operation_name: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = await self.http.request(
            APP, op, "POST", self.s.linear_url,
            json={"operationName": operation_name, "query": query, "variables": variables},
            headers={"Authorization": self.s.linear_api_key, "Content-Type": "application/json"},
        )
        body = check(APP, op, resp)
        if not isinstance(body, dict) or body.get("errors"):
            raise ConnectorError(APP, op, resp.status_code, body)
        return body["data"]

    async def create_issue(self, title: str, description: str, priority: int, label_ids: list[str],
                           state_type: str | None = "started", assignee_id: str | None = None) -> str:
        """Incidents are active work: create them In Progress (not Backlog, which default Linear views hide) and
        assigned, so they show up in "My issues" and the team's active view."""
        payload: dict = {"title": title, "description": description, "teamId": self.s.linear_team_id,
                         "priority": priority, "labelIds": label_ids}
        if state_type:
            state = await self.state_of_type(state_type)
            if state:
                payload["stateId"] = state["id"]
        assignee = assignee_id or self.s.linear_assignee_id
        if assignee:
            payload["assigneeId"] = assignee
        data = await self._gql("create_issue", "IssueCreate", Q_ISSUE_CREATE, {"input": payload})
        result = data["issueCreate"]
        if not result.get("success"):
            raise ConnectorError(APP, "create_issue", None, result)
        return result["issue"]["id"]

    async def issues_by_marker(self, marker_fragment: str) -> list[dict]:
        data = await self._gql("issues_by_marker", "IssuesByDescription", Q_ISSUES_BY_DESCRIPTION,
                               {"contains": marker_fragment, "first": 100})
        return data["issues"]["nodes"]

    async def find_issue_by_marker(self, marker: str) -> str | None:
        from judge.core.outbox import key_token, marker_in

        for issue in await self.issues_by_marker(key_token(marker)):
            if marker_in(issue.get("description") or "", marker):
                return issue["id"]
        return None

    async def issues_by_label(self, label_id: str) -> list[dict]:
        data = await self._gql("issues_by_label", "IssuesByLabel", Q_ISSUES_BY_LABEL,
                               {"labelId": label_id, "first": 250})
        return data["issues"]["nodes"]

    async def comment(self, issue_id: str, body: str) -> str:
        data = await self._gql("comment", "CommentCreate", Q_COMMENT_CREATE,
                               {"input": {"issueId": issue_id, "body": body}})
        result = data["commentCreate"]
        if not result.get("success"):
            raise ConnectorError(APP, "comment", None, result)
        return result["comment"]["id"]

    async def get_issue(self, issue_id: str) -> dict:
        data = await self._gql("get_issue", "IssueGet", Q_ISSUE_GET, {"id": issue_id})
        if not data.get("issue"):
            raise ConnectorError(APP, "get_issue", 404, data)
        return data["issue"]

    async def find_comment_by_marker(self, issue_id: str, marker: str) -> str | None:
        issue = await self.get_issue(issue_id)
        from judge.core.outbox import marker_in

        for c in (issue.get("comments") or {}).get("nodes", []):
            if marker_in(c.get("body") or "", marker):
                return c["id"]
        return None

    async def workflow_states(self) -> list[dict[str, Any]]:
        if self._states is None:
            data = await self._gql("workflow_states", "WorkflowStates", Q_WORKFLOW_STATES,
                                   {"teamId": self.s.linear_team_id})
            self._states = data["workflowStates"]["nodes"]
        return self._states

    async def state_of_type(self, state_type: str) -> dict | None:
        try:
            states = await self.workflow_states()
        except Exception:
            return None
        preferred = {"started": ("In Progress",), "completed": ("Done",), "canceled": ("Canceled", "Cancelled")}
        typed = [s for s in states if s.get("type") == state_type]
        return next((s for s in typed if s.get("name") in preferred.get(state_type, ())), typed[0] if typed else None)

    async def cancel_issue(self, issue_id: str) -> None:
        state = await self.state_of_type("canceled")
        if state is None:
            raise ConnectorError(APP, "cancel_issue", None, "no canceled workflow state for team")
        await self._gql("cancel_issue", "IssueUpdate", Q_ISSUE_UPDATE, {"id": issue_id, "input": {"stateId": state["id"]}})

    async def close_issue(self, issue_id: str) -> None:
        done = next((s for s in await self.workflow_states() if s["type"] == "completed"), None)
        if done is None:
            raise ConnectorError(APP, "close_issue", None, "no completed workflow state for team")
        data = await self._gql("close_issue", "IssueUpdate", Q_ISSUE_UPDATE,
                               {"id": issue_id, "input": {"stateId": done["id"]}})
        if not data["issueUpdate"].get("success"):
            raise ConnectorError(APP, "close_issue", None, data)

    async def update_issue(self, issue_id: str, **fields: Any) -> dict:
        data = await self._gql("update_issue", "IssueUpdate", Q_ISSUE_UPDATE, {"id": issue_id, "input": fields})
        return data["issueUpdate"]["issue"]

    async def delete_issue(self, issue_id: str) -> None:
        data = await self._gql("delete_issue", "IssueDelete", Q_ISSUE_DELETE, {"id": issue_id})
        if not data["issueDelete"].get("success"):
            raise ConnectorError(APP, "delete_issue", None, data)

    # ------------------------------------------------------------ discovery (bootstrap / doctor)

    async def viewer(self) -> dict:
        data = await self._gql("viewer", "Viewer", Q_VIEWER, {})
        return data["viewer"]

    async def teams(self) -> list[dict]:
        data = await self._gql("teams", "Teams", Q_TEAMS, {})
        return data["teams"]["nodes"]

    async def labels(self) -> list[dict]:
        data = await self._gql("labels", "IssueLabels", Q_ISSUE_LABELS, {})
        return data["issueLabels"]["nodes"]

    async def create_label(self, name: str, team_id: str, color: str = "#6B7280") -> str:
        data = await self._gql("create_label", "IssueLabelCreate", Q_ISSUE_LABEL_CREATE,
                               {"input": {"name": name, "teamId": team_id, "color": color}})
        result = data["issueLabelCreate"]
        if not result.get("success"):
            raise ConnectorError(APP, "create_label", None, result)
        return result["issueLabel"]["id"]
