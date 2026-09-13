import pytest

from judge.connectors.linear import LinearClient
from judge.connectors.transport import ConnectorError


async def test_create_find_comment_close_delete(settings, http, uid):
    client = LinearClient(settings, http)
    marker = f"IJ-KEY:{uid} IJ-INC:inc_{uid} IJ-TRIAL:t{uid}"
    assert await client.find_issue_by_marker(marker) is None

    issue_id = await client.create_issue("Checkout outage", f"Summary\n\n{marker}", 1, ["label_ij_eval"])
    assert await client.find_issue_by_marker(marker) == issue_id
    assert [i["id"] for i in await client.issues_by_marker(f"IJ-TRIAL:t{uid}")] == [issue_id]
    assert issue_id in [i["id"] for i in await client.issues_by_label("label_ij_eval")]

    cmarker = f"IJ-KEY:c{uid}"
    assert await client.find_comment_by_marker(issue_id, cmarker) is None
    comment_id = await client.comment(issue_id, f"still firing\n{cmarker}")
    assert await client.find_comment_by_marker(issue_id, cmarker) == comment_id

    issue = await client.get_issue(issue_id)
    assert issue["priority"] == 1 and issue["state"]["type"] == "started"  # incidents are created In Progress
    await client.close_issue(issue_id)
    assert (await client.get_issue(issue_id))["state"]["type"] == "completed"

    await client.delete_issue(issue_id)
    assert await client.find_issue_by_marker(marker) is None
    with pytest.raises(ConnectorError):
        await client.get_issue(issue_id)


async def test_bearer_prefix_is_rejected(settings, http):
    bad = settings.model_copy(update={"linear_api_key": "Bearer sandbox"})
    with pytest.raises(ConnectorError) as e:
        await LinearClient(bad, http).create_issue("x", "y", 3, [])
    assert e.value.status == 401


async def test_documents_carry_operation_name_and_real_header(settings, uid):
    import httpx

    seen = {}

    async def handler(request: httpx.Request):
        import json
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"issueCreate": {"success": True, "issue": {"id": "i1"}}}})

    from judge.connectors.transport import HttpClient

    async with HttpClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as http:
        real = settings.model_copy(update={"backend": "real", "linear_api_key": "lin_api_xyz"})
        assert await LinearClient(real, http).create_issue("t", "d", 2, []) == "i1"
    assert seen["auth"] == "lin_api_xyz"
    assert seen["body"]["operationName"] == "IssueCreate"
    assert seen["body"]["query"].startswith("mutation IssueCreate($input: IssueCreateInput!)")
    assert http.calls[0][3] == "https://api.linear.app/graphql"
