"""Sentry emulation: envelope/store ingest (what sentry-sdk 2.x sends) + org issues API subset."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
from datetime import timedelta
from typing import Any

from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from sandbox.db import DocStore, iso, parse_iso, utcnow

try:  # sentry-sdk uses brotli when the module is installed
    import brotli  # type: ignore
except ImportError:  # pragma: no cover
    brotli = None

PROJECTS: dict[str, dict[str, str]] = {
    "1": {"id": "1", "slug": "shoplab-prod", "name": "shoplab-prod", "environment": "production"},
    "2": {"id": "2", "slug": "shoplab-staging", "name": "shoplab-staging", "environment": "staging"},
}
MAX_EVENTS_PER_ISSUE = 50
ORG = {"id": "1", "slug": "shoplab", "name": "ShopLab"}
TEAMS = [{"id": "1", "slug": "shoplab", "name": "ShopLab"}]


def _decode_body(raw: bytes, encoding: str | None) -> bytes:
    enc = (encoding or "").lower().strip()
    if enc == "gzip":
        return gzip.decompress(raw)
    if enc == "deflate":
        return zlib.decompress(raw)
    if enc == "br":
        if brotli is None:
            raise HTTPException(415, "brotli not available")
        return brotli.decompress(raw)
    return raw


def parse_envelope(body: bytes) -> tuple[dict[str, Any], list[tuple[dict[str, Any], bytes]]]:
    """Envelope format: header JSON line, then (item header JSON line, payload) pairs.
    Payload length comes from the item header `length` if present, else runs until newline."""
    pos = 0

    def read_line() -> bytes:
        nonlocal pos
        end = body.find(b"\n", pos)
        if end == -1:
            line, pos = body[pos:], len(body)
        else:
            line, pos = body[pos:end], end + 1
        return line

    header_line = read_line()
    header = json.loads(header_line) if header_line.strip() else {}
    items: list[tuple[dict[str, Any], bytes]] = []
    while pos < len(body):
        line = read_line()
        if not line.strip():
            continue
        item_header = json.loads(line)
        length = item_header.get("length")
        if length is not None:
            payload = body[pos: pos + int(length)]
            pos += int(length)
            if pos < len(body) and body[pos: pos + 1] == b"\n":
                pos += 1
        else:
            payload = read_line()
        items.append((item_header, payload))
    return header, items


def _exception(event: dict[str, Any]) -> dict[str, Any] | None:
    exc = event.get("exception")
    values = exc.get("values") if isinstance(exc, dict) else exc if isinstance(exc, list) else None
    return values[-1] if values else None


def _top_frame_culprit(exc: dict[str, Any] | None) -> str:
    if not exc:
        return ""
    frames = (exc.get("stacktrace") or {}).get("frames") or []
    for frame in reversed(frames):
        if frame.get("in_app", True):
            mod = frame.get("module") or frame.get("filename") or ""
            fn = frame.get("function") or ""
            return f"{mod} in {fn}".strip()
    return ""


def _tags_dict(event: dict[str, Any]) -> dict[str, str]:
    tags = event.get("tags") or {}
    if isinstance(tags, list):
        tags = {t[0]: t[1] for t in tags if isinstance(t, (list, tuple)) and len(t) == 2}
    return {str(k): str(v) for k, v in tags.items()}


class SentryEmulator:
    def __init__(self, db: DocStore, base_url: str = "http://127.0.0.1:8900"):
        self.db = db
        self.base_url = base_url

    # ---------------------------------------------------------- projects

    def projects(self) -> dict[str, dict[str, str]]:
        """Seeded projects plus any created through the API (persisted in the doc store)."""
        out = dict(PROJECTS)
        for proj in self.db.all("sentry_project"):
            out[proj["id"]] = proj
        return out

    def create_project(self, team_slug: str, body: dict[str, Any]) -> dict[str, str]:
        if not any(t["slug"] == team_slug for t in TEAMS):
            raise HTTPException(404, {"detail": "The requested resource does not exist"})
        name = str(body.get("name") or "").strip()
        slug = str(body.get("slug") or name).strip().lower()
        if not name or not re.fullmatch(r"[a-z0-9_-]{1,50}", slug):
            raise HTTPException(400, {"detail": "Invalid project name or slug"})
        with self.db.lock:
            if any(p["slug"] == slug for p in self.projects().values()):
                raise HTTPException(409, {"detail": "A project with this slug already exists."})
            pid = str(max(int(k) for k in self.projects()) + 1)
            env = "staging" if "staging" in slug else "production"
            proj = {"id": pid, "slug": slug, "name": name, "environment": env,
                    "platform": str(body.get("platform") or "python")}
            self.db.put("sentry_project", pid, proj)
        return proj

    def project_json(self, proj: dict[str, str]) -> dict[str, Any]:
        return {"id": proj["id"], "slug": proj["slug"], "name": proj["name"],
                "platform": proj.get("platform", "python"), "organization": ORG}

    def dsn(self, proj: dict[str, str]) -> str:
        from urllib.parse import urlparse

        u = urlparse(self.base_url)
        return f"{u.scheme}://sandboxkey@{u.hostname}:{u.port or 80}/{proj['id']}"

    # ---------------------------------------------------------- ingest

    def ingest_event(self, project_id: str, event: dict[str, Any]) -> str:
        with self.db.lock:  # runs in a threadpool; group lookup + create must be atomic
            return self._ingest_event(project_id, event)

    def _ingest_event(self, project_id: str, event: dict[str, Any]) -> str:
        project = self.projects().get(str(project_id))
        if project is None:
            raise HTTPException(404, "project not found")
        exc = _exception(event)
        err_type = (exc or {}).get("type") or ""
        err_value = (exc or {}).get("value") or ""
        message = event.get("message")
        if isinstance(message, dict):
            message = message.get("formatted") or message.get("message")
        if not err_type and not message and event.get("logentry"):
            message = (event["logentry"] or {}).get("formatted") or (event["logentry"] or {}).get("message")
        transaction = event.get("transaction") or ""
        culprit = transaction or _top_frame_culprit(exc) or event.get("culprit") or ""
        environment = event.get("environment") or project["environment"]
        tags = _tags_dict(event)
        tags.setdefault("environment", environment)
        tags.setdefault("level", event.get("level") or "error")
        if event.get("release"):
            tags.setdefault("release", str(event["release"]))
        if transaction:
            tags.setdefault("transaction", transaction)
        user = event.get("user") or {}
        user_key = str(user.get("id") or user.get("email") or user.get("ip_address") or user.get("username") or "")
        if user_key:
            tags.setdefault("user", f"id:{user_key}")

        fingerprint = event.get("fingerprint")
        if fingerprint:
            group_basis = ["fp", *[str(x) for x in fingerprint]]
        else:
            group_basis = ["default", err_type or str(message or ""), culprit]
        group_hash = hashlib.sha256(json.dumps([project_id, group_basis]).encode()).hexdigest()[:32]

        ts = parse_iso(event.get("timestamp")) or utcnow()
        received = utcnow()
        title = f"{err_type}: {err_value}" if err_type else str(message or "<unlabeled event>")
        title = title[:200]

        # O(1) group lookup: scanning every issue per event collapses under eval load and makes the SDK drop events
        group_ref = self.db.get("sentry_group", group_hash)
        issue = self.db.get("sentry_issue", group_ref["issue_id"]) if group_ref else None
        if issue is None:
            n = self.db.next(f"sentry_issue_short_{project_id}")
            issue_id = str(self.db.next("sentry_issue_id") + 1000)
            issue = {
                "id": issue_id,
                "group_hash": group_hash,
                "shortId": f"{project['slug'].upper()}-{n}",
                "project_id": str(project_id),
                "title": title,
                "culprit": culprit,
                "level": event.get("level") or "error",
                "status": "unresolved",
                "metadata": {"type": err_type, "value": err_value or str(message or "")},
                "firstSeen": iso(ts),
                "lastSeen": iso(ts),
                "count": 0,
                "users": [],
                "env_stats": {},
                "tag_counts": {},
                "platform": event.get("platform") or "python",
                "latest_event_id": None,
            }
            self.db.put("sentry_group", group_hash, {"issue_id": issue_id})
        issue["count"] += 1
        if user_key and user_key not in issue["users"]:
            issue["users"].append(user_key)
        if iso(ts) < issue["firstSeen"]:
            issue["firstSeen"] = iso(ts)
        if iso(ts) > issue["lastSeen"]:
            issue["lastSeen"] = iso(ts)
        es = issue["env_stats"].setdefault(environment, {"count": 0, "users": [], "firstSeen": iso(ts),
                                                          "lastSeen": iso(ts)})
        es["count"] += 1
        if user_key and user_key not in es["users"]:
            es["users"].append(user_key)
        es["firstSeen"] = min(es["firstSeen"], iso(ts))
        es["lastSeen"] = max(es["lastSeen"], iso(ts))
        for k, v in tags.items():
            issue["tag_counts"].setdefault(k, {})
            issue["tag_counts"][k][v] = issue["tag_counts"][k].get(v, 0) + 1
        if issue["status"] == "resolved":  # regression
            issue["status"] = "unresolved"

        event_id = (event.get("event_id") or hashlib.md5(json.dumps(event, default=str).encode()).hexdigest())
        event_id = str(event_id).replace("-", "")
        stored = {
            "eventID": event_id,
            "id": event_id,
            "groupID": issue["id"],
            "projectID": str(project_id),
            "dateCreated": iso(ts),
            "dateReceived": iso(received),
            "message": str(message or err_value or ""),
            "title": title,
            "culprit": culprit,
            "environment": environment,
            "platform": event.get("platform") or "python",
            "tags": [{"key": k, "value": v} for k, v in sorted(tags.items())],
            "user": user or None,
            "contexts": event.get("contexts") or {},
            "extra": event.get("extra") or {},
            "breadcrumbs": event.get("breadcrumbs") or {},
            "entries": ([{"type": "exception", "data": {"values": (event.get("exception") or {}).get("values", [])}}]
                        if exc else [{"type": "message", "data": {"formatted": str(message or "")}}]),
            "fingerprints": [str(x) for x in (fingerprint or [])],
        }
        self.db.put("sentry_event", event_id, stored)
        issue["latest_event_id"] = event_id
        issue.setdefault("event_ids", []).append(event_id)
        for old in issue["event_ids"][:-MAX_EVENTS_PER_ISSUE]:
            self.db.delete("sentry_event", old)
        issue["event_ids"] = issue["event_ids"][-MAX_EVENTS_PER_ISSUE:]
        self.db.put("sentry_issue", issue["id"], issue)
        return event_id

    # ---------------------------------------------------------- read API

    def issue_json(self, issue: dict[str, Any], environments: list[str] | None = None) -> dict[str, Any]:
        project = self.projects()[issue["project_id"]]
        count, users = issue["count"], len(issue["users"])
        first, last = issue["firstSeen"], issue["lastSeen"]
        if environments:
            stats = [issue["env_stats"][e] for e in environments if e in issue["env_stats"]]
            if stats:
                count = sum(s["count"] for s in stats)
                users = len({u for s in stats for u in s["users"]})
                first = min(s["firstSeen"] for s in stats)
                last = max(s["lastSeen"] for s in stats)
        return {
            "id": issue["id"],
            "shortId": issue["shortId"],
            "title": issue["title"],
            "culprit": issue["culprit"],
            "level": issue["level"],
            "status": issue["status"],
            "substatus": "ongoing",
            "type": "error",
            "platform": issue["platform"],
            "isUnhandled": True,
            "count": str(count),
            "userCount": users,
            "firstSeen": first,
            "lastSeen": last,
            "metadata": issue["metadata"],
            "project": {"id": project["id"], "slug": project["slug"], "name": project["name"]},
            "permalink": f"{self.base_url}/organizations/shoplab/issues/{issue['id']}/",
            "tags": [{"key": k, "name": k.replace("_", " ").title(), "totalValues": sum(v.values())}
                     for k, v in sorted(issue["tag_counts"].items())],
        }

    def search(self, *, query: str, environments: list[str], projects: list[str], limit: int) -> list[dict[str, Any]]:
        status_filter: str | None = None
        last_seen_after = None
        tag_filters: list[tuple[str, str]] = []
        text_terms: list[str] = []
        for token in query.split():
            if ":" in token:
                key, value = token.split(":", 1)
                if key == "is":
                    status_filter = value
                elif key == "lastSeen":
                    m = re.fullmatch(r"-(\d+)([smhdw])", value)
                    if not m:
                        raise HTTPException(400, f"invalid lastSeen {value}")
                    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
                    last_seen_after = utcnow() - timedelta(**{unit: int(m.group(1))})
                else:
                    tag_filters.append((key, value.strip('"')))
            else:
                text_terms.append(token.lower())

        wanted_projects = set()
        for p in projects:
            for pid, proj in self.projects().items():
                if p in (pid, proj["slug"]) or p == "-1":
                    wanted_projects.add(pid)

        out = []
        for issue in reversed(self.db.all("sentry_issue")):
            if wanted_projects and issue["project_id"] not in wanted_projects:
                continue
            if environments and not any(e in issue["env_stats"] for e in environments):
                continue
            if status_filter and issue["status"] != status_filter:
                continue
            j = self.issue_json(issue, environments or None)
            if last_seen_after and (parse_iso(j["lastSeen"]) or utcnow()) < last_seen_after:
                continue
            if any(value not in issue["tag_counts"].get(key, {}) for key, value in tag_filters):
                continue
            if any(t not in issue["title"].lower() for t in text_terms):
                continue
            out.append(j)
        out.sort(key=lambda j: j["lastSeen"], reverse=True)
        return out[:limit]


def build_router(emu: SentryEmulator) -> APIRouter:
    r = APIRouter()

    def require_auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            raise HTTPException(401, {"detail": "Authentication credentials were not provided."})

    async def _ingest(project_id: str, request: Request, envelope: bool) -> JSONResponse:
        raw = _decode_body(await request.body(), request.headers.get("content-encoding"))
        last_id = None
        if envelope:
            header, items = parse_envelope(raw)
            for item_header, payload in items:
                if item_header.get("type") == "event":
                    last_id = await run_in_threadpool(emu.ingest_event, project_id, json.loads(payload))
            return JSONResponse({"id": last_id or header.get("event_id")})
        event = json.loads(raw)
        return JSONResponse({"id": await run_in_threadpool(emu.ingest_event, project_id, event)})

    @r.post("/api/{project_id}/envelope/")
    async def envelope(project_id: str, request: Request):
        return await _ingest(project_id, request, envelope=True)

    @r.post("/api/{project_id}/store/")
    async def store(project_id: str, request: Request):
        return await _ingest(project_id, request, envelope=False)

    def require_org(org: str) -> None:
        if org != ORG["slug"]:
            raise HTTPException(404, {"detail": "The requested resource does not exist"})

    @r.get("/api/0/organizations/{org}/")
    def organization(org: str, request: Request):
        require_auth(request)
        require_org(org)
        return ORG

    @r.get("/api/0/organizations/{org}/projects/")
    def list_projects(org: str, request: Request):
        require_auth(request)
        require_org(org)
        return [emu.project_json(p) for p in emu.projects().values()]

    @r.get("/api/0/organizations/{org}/teams/")
    def list_teams(org: str, request: Request):
        require_auth(request)
        require_org(org)
        return TEAMS

    @r.post("/api/0/teams/{org}/{team_slug}/projects/", status_code=201)
    async def create_project(org: str, team_slug: str, request: Request):
        require_auth(request)
        require_org(org)
        return emu.project_json(emu.create_project(team_slug, await request.json()))

    @r.get("/api/0/projects/{org}/{project_slug}/keys/")
    def project_keys(org: str, project_slug: str, request: Request):
        require_auth(request)
        require_org(org)
        proj = next((p for p in emu.projects().values() if p["slug"] == project_slug), None)
        if proj is None:
            raise HTTPException(404, {"detail": "The requested resource does not exist"})
        dsn = emu.dsn(proj)
        return [{"id": f"key{proj['id']}", "name": "Default", "isActive": True,
                 "dsn": {"public": dsn, "secret": dsn.replace("sandboxkey@", "sandboxkey:secret@")}}]

    @r.get("/api/0/organizations/{org}/issues/")
    def list_issues(org: str, request: Request):
        require_auth(request)
        qp = request.query_params
        query = qp.get("query", "is:unresolved")
        return emu.search(query=query, environments=qp.getlist("environment"), projects=qp.getlist("project"),
                          limit=int(qp.get("limit", "100")))

    @r.get("/api/0/organizations/{org}/issues/{issue_id}/")
    def get_issue(org: str, issue_id: str, request: Request):
        require_auth(request)
        issue = emu.db.get("sentry_issue", issue_id)
        if issue is None:
            raise HTTPException(404, {"detail": "The requested resource does not exist"})
        return emu.issue_json(issue, request.query_params.getlist("environment") or None)

    @r.put("/api/0/organizations/{org}/issues/{issue_id}/")
    async def update_issue(org: str, issue_id: str, request: Request):
        require_auth(request)
        issue = emu.db.get("sentry_issue", issue_id)
        if issue is None:
            raise HTTPException(404, {"detail": "The requested resource does not exist"})
        body = await request.json()
        if body.get("status") in ("resolved", "unresolved", "ignored"):
            issue["status"] = body["status"]
            emu.db.put("sentry_issue", issue_id, issue)
        return emu.issue_json(issue)

    @r.get("/api/0/organizations/{org}/issues/{issue_id}/events/latest/")
    def latest_event(org: str, issue_id: str, request: Request):
        require_auth(request)
        issue = emu.db.get("sentry_issue", issue_id)
        if issue is None or not issue.get("latest_event_id"):
            raise HTTPException(404, {"detail": "The requested resource does not exist"})
        return emu.db.get("sentry_event", issue["latest_event_id"])

    return r
