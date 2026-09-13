"""Raw-shape checks of the sandbox endpoints that emulate discovery APIs (auth quirks included)."""

import httpx


def test_sentry_discovery_shapes_and_auth(sandbox_url):
    h = {"Authorization": "Bearer t"}
    assert httpx.get(f"{sandbox_url}/api/0/organizations/shoplab/projects/").status_code == 401
    assert httpx.get(f"{sandbox_url}/api/0/organizations/nope/projects/", headers=h).status_code == 404
    org = httpx.get(f"{sandbox_url}/api/0/organizations/shoplab/", headers=h).json()
    assert org["slug"] == "shoplab"
    keys = httpx.get(f"{sandbox_url}/api/0/projects/shoplab/shoplab-staging/keys/", headers=h).json()
    assert keys[0]["isActive"] and keys[0]["dsn"]["public"].endswith("/2")
    r = httpx.post(f"{sandbox_url}/api/0/teams/shoplab/no-team/projects/", headers=h, json={"name": "x"})
    assert r.status_code == 404


def test_instatus_pages_require_bearer(sandbox_url):
    assert httpx.get(f"{sandbox_url}/instatus/v2/pages").status_code == 401
    pages = httpx.get(f"{sandbox_url}/instatus/v1/pages", headers={"Authorization": "Bearer k"}).json()
    assert pages == [{"id": "page_shoplab", "subdomain": "shoplab", "name": "ShopLab"}]
    r = httpx.post(f"{sandbox_url}/instatus/v1/page_shoplab/components", headers={"Authorization": "Bearer k"},
                   json={"name": "X", "status": "BROKEN"})
    assert r.status_code == 400


def test_linear_teams_and_labels_ops(sandbox_url):
    def gql(op, variables=None):
        return httpx.post(f"{sandbox_url}/linear/graphql", headers={"Authorization": "lin_api_key"},
                          json={"operationName": op, "query": "…", "variables": variables or {}}).json()

    assert gql("Teams")["data"]["teams"]["nodes"][0]["key"] == "SHO"
    names = {l["name"] for l in gql("IssueLabels")["data"]["issueLabels"]["nodes"]}
    assert "ij-eval" in names
    assert gql("IssueLabelCreate", {"input": {"name": "x", "teamId": "wrong"}})["errors"]
