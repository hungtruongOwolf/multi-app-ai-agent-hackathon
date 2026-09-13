"""Real-app onboarding: `judge bootstrap` discovers/creates the IDs the agent needs and writes them to .env;
`judge doctor` proves each integration end to end (auth → one write → read it back → clean up).

Both work against the sandbox too (IJ_BACKEND=sandbox), which is how they are tested."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from judge.core.outbox import slack_thread_link
from judge.settings import ROOT, Config, Settings

EVAL_LABEL = "ij-eval"
DEFAULT_CHANNEL = "incident-judge-oncall"


@dataclass
class Check:
    app: str
    step: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    env_updates: dict[str, str] = field(default_factory=dict)

    def add(self, app: str, step: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(app, step, ok, detail))
        return ok

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


# ---------------------------------------------------------------- .env editing


def update_env_file(updates: dict[str, str], path: Path = ROOT / ".env") -> None:
    """Set keys in .env in place, preserving comments/other keys. Never prints secret values."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            seen.add(m.group(1))
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _missing(settings: Settings, names: dict[str, str]) -> list[str]:
    return [env for attr, env in names.items() if not getattr(settings, attr) or getattr(settings, attr) == "sandbox"]


def _clients(settings: Settings):
    from judge.connectors.instatus import InstatusClient
    from judge.connectors.linear import LinearClient
    from judge.connectors.sentry import SentryClient
    from judge.connectors.slack import SlackClient
    from judge.connectors.transport import HttpClient

    http = HttpClient()
    return http, SentryClient(settings, http), LinearClient(settings, http), InstatusClient(settings, http), \
        SlackClient(settings, http)


# ---------------------------------------------------------------- bootstrap


async def bootstrap(settings: Settings, config: Config, channel_name: str = DEFAULT_CHANNEL,
                    team_key: str | None = None) -> Report:
    rep = Report()
    http, sentry, linear, instatus, slack = _clients(settings)
    try:
        await _bootstrap_sentry(settings, sentry, rep)
        await _bootstrap_linear(settings, linear, rep, team_key)
        await _bootstrap_instatus(settings, config, instatus, rep)
        await _bootstrap_slack(settings, slack, rep, channel_name)
        _bootstrap_pagerduty(settings, rep)
        await _bootstrap_github(settings, http, rep)
    finally:
        await http.aclose()
    return rep


def _bootstrap_pagerduty(s: Settings, rep: Report) -> None:
    if not s.pagerduty_routing_key:
        rep.add("pagerduty", "routing key", True, "not configured (optional): set PAGERDUTY_ROUTING_KEY to page on-call")
        return
    ok = bool(re.fullmatch(r"[0-9a-zA-Z]{32}", s.pagerduty_routing_key))
    rep.add("pagerduty", "routing key", ok, "Events API v2 integration key" if ok
            else "expected a 32-character Events API v2 integration key")


async def _bootstrap_github(s: Settings, http, rep: Report) -> None:
    if not (s.github_token and s.github_memory_repo):
        rep.add("github", "token + repo", True, "not configured (optional): set GITHUB_TOKEN and GITHUB_REPO=owner/name "
                                                 "to open runbook updates as pull requests")
        return
    await _check_github(s, http, rep)


async def _check_github(s: Settings, http, rep: Report) -> bool:
    from judge.connectors.github import GitHubClient

    gh = GitHubClient(s.github_token, s.github_memory_repo, http)
    info = await _guard(rep, "github", "read repository", gh.repo_info())
    if info is None:
        return False
    perms = info.get("permissions") or {}
    rep.add("github", "read repository", True, f"{info.get('full_name')} ({'private' if info.get('private') else 'public'})")
    ok = rep.add("github", "push + pull requests", bool(perms.get("push")),
                 "token can push branches and open/merge pull requests" if perms.get("push")
                 else "token lacks push permission (needs Contents and Pull requests: read and write)")
    head = await _guard(rep, "github", "default branch", gh.head_sha(info.get("default_branch") or "main"))
    if head:
        rep.add("github", "default branch", True, f"{info.get('default_branch') or 'main'} @ {head[:8]}")
    return ok and head is not None


async def _guard(rep: Report, app: str, step: str, coro):
    try:
        return await coro
    except Exception as e:  # report and continue with the next app
        rep.add(app, step, False, _short(e))
        return None


def _short(e: Exception) -> str:
    body = getattr(e, "body", None)
    return f"{type(e).__name__}: {str(body or e)[:300]}"


async def _bootstrap_sentry(s: Settings, sentry, rep: Report) -> None:
    if _missing(s, {"sentry_token": "SENTRY_TOKEN"}) and s.backend == "real":
        rep.add("sentry", "credentials", False, "set SENTRY_TOKEN (Personal Token) and SENTRY_ORG in .env")
        return
    org = await _guard(rep, "sentry", "organization", sentry.organization())
    if org is None:
        return
    rep.add("sentry", "organization", True, org.get("slug", s.sentry_org))
    projects = await _guard(rep, "sentry", "projects", sentry.projects())
    if projects is None:
        return
    slugs = {p["slug"] for p in projects}
    teams = None
    for env_name, slug in (("SENTRY_DSN_PROD", s.sentry_projects["production"]),
                           ("SENTRY_DSN_STAGING", s.sentry_projects["staging"])):
        if slug not in slugs:
            teams = teams if teams is not None else (await _guard(rep, "sentry", "teams", sentry.teams()) or [])
            if not teams:
                rep.add("sentry", f"project {slug}", False, "missing and no team to create it in")
                continue
            created = await _guard(rep, "sentry", f"create project {slug}",
                                   sentry.create_project(teams[0]["slug"], slug, slug))
            if created is None:
                continue
            rep.add("sentry", f"project {slug}", True, f"created in team {teams[0]['slug']}")
        dsn = await _guard(rep, "sentry", f"dsn {slug}", sentry.project_dsn(slug))
        if dsn:
            rep.env_updates[env_name] = dsn
            rep.add("sentry", f"dsn {slug}", True, "found")


async def _bootstrap_linear(s: Settings, linear, rep: Report, team_key: str | None) -> None:
    if _missing(s, {"linear_api_key": "LINEAR_API_KEY"}) and s.backend == "real":
        rep.add("linear", "credentials", False, "set LINEAR_API_KEY in .env")
        return
    me = await _guard(rep, "linear", "viewer", linear.viewer())
    if me is None:
        return
    rep.add("linear", "viewer", True, me.get("name") or me.get("id", ""))
    if me.get("id"):
        rep.env_updates["LINEAR_ASSIGNEE_ID"] = me["id"]  # incidents are assigned so they appear in "My issues"
    teams = await _guard(rep, "linear", "teams", linear.teams()) or []
    team = next((t for t in teams if team_key and t["key"] == team_key), None) or (teams[0] if teams else None)
    if team is None:
        rep.add("linear", "team", False, "no team visible to this API key")
        return
    rep.env_updates["LINEAR_TEAM_ID"] = team["id"]
    rep.add("linear", "team", True, f"{team['key']} ({team['name']})")
    labels = await _guard(rep, "linear", "labels", linear.labels()) or []
    label = next((l for l in labels if l["name"] == EVAL_LABEL
                  and (l.get("team") or {}).get("id") in (team["id"], None)), None)
    label_id = label["id"] if label else await _guard(rep, "linear", "create label",
                                                      linear.create_label(EVAL_LABEL, team["id"]))
    if label_id:
        rep.env_updates["LINEAR_EVAL_LABEL_ID"] = label_id
        rep.add("linear", f"label {EVAL_LABEL}", True, "exists" if label else "created")


async def _bootstrap_instatus(s: Settings, config: Config, instatus, rep: Report) -> None:
    if _missing(s, {"instatus_api_key": "INSTATUS_API_KEY"}) and s.backend == "real":
        rep.add("instatus", "credentials", False, "set INSTATUS_API_KEY in .env")
        return
    if not s.instatus_page_id or (s.backend == "real" and s.instatus_page_id == "page_shoplab"):
        pages = await _guard(rep, "instatus", "pages", instatus.pages()) or []
        if not pages:
            rep.add("instatus", "page", False, "no status page found; create one in the Instatus dashboard")
            return
        s.instatus_page_id = pages[0]["id"]
        rep.env_updates["INSTATUS_PAGE_ID"] = pages[0]["id"]
        rep.add("instatus", "page", True, pages[0].get("subdomain") or pages[0]["id"])
    if s.backend == "real":
        pages = await _guard(rep, "instatus", "page url", instatus.pages()) or []
        page = next((pg for pg in pages if pg.get("id") == s.instatus_page_id), pages[0] if pages else None)
        if page and page.get("subdomain"):
            rep.env_updates["INSTATUS_PAGE_URL"] = f"https://{page['subdomain']}.instatus.com"
    comps = await _guard(rep, "instatus", "components", instatus.components())
    if comps is None:
        return
    by_name = {c.get("name"): c.get("id") for c in comps}
    mapping = []
    for svc in config.catalog.values():
        if not svc.public:
            continue
        cid = by_name.get(svc.capability)
        created = False
        if not cid:
            cid = await _guard(rep, "instatus", f"create component {svc.capability}",
                               instatus.create_component(svc.capability, f"ShopLab {svc.name}"))
            created = bool(cid)
        if cid:
            mapping.append(f"{svc.name}={cid}")
            rep.add("instatus", f"component {svc.capability}", True, "created" if created else "exists")
    if mapping:
        rep.env_updates["INSTATUS_COMPONENTS"] = ",".join(mapping)


async def _bootstrap_slack(s: Settings, slack, rep: Report, channel_name: str) -> None:
    if _missing(s, {"slack_bot_token": "SLACK_BOT_TOKEN"}) and s.backend == "real":
        rep.add("slack", "credentials", False, "set SLACK_BOT_TOKEN (xoxb-…) in .env")
        return
    info = await _guard(rep, "slack", "auth.test", slack.auth_info())
    if info is None:
        return
    rep.add("slack", "auth.test", True, f"bot {info.get('user')} in {info.get('team')}")
    cid = await _guard(rep, "slack", "find channel", slack.find_channel(channel_name))
    created = False
    if not cid:
        cid = await _guard(rep, "slack", "create channel", slack.create_channel(channel_name))
        created = bool(cid)
    if not cid:
        return
    await _guard(rep, "slack", "join channel", slack.join(cid))
    oncall = Config().policy.oncall_allowlist
    if s.backend == "real" and oncall and oncall != ["U_ONCALL_1"]:
        if await _guard(rep, "slack", "invite on-call", slack.invite(cid, oncall)) is None and not rep.checks[-1].ok:
            pass
        else:
            rep.add("slack", "invite on-call", True, ",".join(oncall))
    rep.env_updates["SLACK_ONCALL_CHANNEL"] = cid
    rep.add("slack", f"#{channel_name}", True, "created" if created else "exists")


# ---------------------------------------------------------------- doctor


async def doctor(settings: Settings, config: Config, skip_sentry_ingest: bool = False) -> Report:
    from judge.core.outbox import marker

    rep = Report()
    http, sentry, linear, instatus, slack = _clients(settings)
    tag = f"doctor{int(time.time())}"
    mk = marker(f"{int(time.time()):x}doctor", None, tag)
    try:
        await _doctor_anthropic(settings, rep)
        await _doctor_sentry(settings, sentry, rep, tag, skip_sentry_ingest)
        await _doctor_linear(settings, linear, rep, mk)
        await _doctor_instatus(settings, config, instatus, rep, tag)
        await _doctor_slack(settings, slack, rep, mk)
        await _doctor_pagerduty(settings, http, rep, tag)
        if settings.backend == "real" and settings.github_token and settings.github_memory_repo:
            await _check_github(settings, http, rep)
        else:
            rep.add("github", "token + repo", True, "not configured (optional; real apps only)")
    finally:
        await http.aclose()
    return rep


async def _doctor_pagerduty(s: Settings, http, rep: Report, tag: str) -> None:
    from judge.connectors.pagerduty import PagerDutyClient

    pd = PagerDutyClient(s, http)
    if not pd.enabled or s.backend != "real":
        rep.add("pagerduty", "routing key", True, "not configured (optional; real apps only)")
        return
    key = f"ij-doctor-{tag}"
    sent = await _guard(rep, "pagerduty", "trigger event", pd.trigger(
        key, "Incident Judge doctor: connectivity check (auto-resolved)", "SEV4", "judge-doctor"))
    if sent is None:
        return
    rep.add("pagerduty", "trigger event", True, f"dedup_key {key}")
    if await _guard(rep, "pagerduty", "cleanup", pd.resolve(key)) is not None:
        rep.add("pagerduty", "cleanup", True, "test event resolved")


async def _doctor_anthropic(s: Settings, rep: Report) -> None:
    if not s.anthropic_api_key:
        rep.add("claude", "api key", False, "ANTHROPIC_API_KEY not set (heuristic judge will be used)")
        return
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=s.anthropic_api_key)
    for model in dict.fromkeys([s.judge_model, s.memory_model]):
        try:
            t = time.time()
            await client.messages.create(model=model, max_tokens=16, messages=[{"role": "user", "content": "ping"}])
            rep.add("claude", model, True, f"{time.time() - t:.1f}s")
        except Exception as e:
            rep.add("claude", model, False, _short(e))


async def _doctor_sentry(s: Settings, sentry, rep: Report, tag: str, skip_ingest: bool) -> None:
    if await _guard(rep, "sentry", "auth + organization", sentry.organization()) is None:
        return
    rep.add("sentry", "auth + organization", True, s.sentry_org)
    dsn = await _guard(rep, "sentry", "project dsn", sentry.project_dsn(s.sentry_projects["production"]))
    if not dsn or skip_ingest:
        return
    import sentry_sdk

    # A detached Scope has no client in sdk 2.x (capture returns None); bind the SDK for this CLI process instead.
    sentry_sdk.init(dsn=dsn, environment="production", default_integrations=False)
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("ij_trial", tag)
        scope.set_tag("service", "doctor")
        scope.fingerprint = ["ij-doctor", tag]
        event_id = sentry_sdk.capture_message(f"Incident Judge doctor check {tag}", level="error")
    sentry_sdk.flush(timeout=10)
    if not event_id:
        rep.add("sentry", "send event", False, "SDK did not accept the event")
        return
    rep.add("sentry", "send event", True, "sent via SDK")
    deadline = time.time() + (90 if s.backend == "real" else 20)
    issue = None
    while time.time() < deadline and issue is None:  # ingest has no SLA: poll, never sleep-and-assert
        issues = await _guard(rep, "sentry", "list issues", sentry.list_issues("production",
                                                                             f"is:unresolved ij_trial:{tag}",
                                                                             trial_id=tag)) or []
        issue = issues[0] if issues else None
        if issue is None:
            await asyncio.sleep(3)
    if not rep.add("sentry", "read back event", issue is not None,
                   f"issue {issue['id']}" if issue else "not visible within timeout"):
        return
    await _guard(rep, "sentry", "resolve test issue", sentry.resolve_issue(issue["id"]))
    rep.add("sentry", "cleanup", True, "test issue resolved")


async def _doctor_linear(s: Settings, linear, rep: Report, mk: str) -> None:
    if await _guard(rep, "linear", "auth (viewer)", linear.viewer()) is None:
        return
    rep.add("linear", "auth (viewer)", True, "")
    if not s.linear_team_id or s.linear_team_id == "team_shoplab" and s.backend == "real":
        rep.add("linear", "team id", False, "LINEAR_TEAM_ID missing — run `judge bootstrap`")
        return
    issue_id = await _guard(rep, "linear", "create issue",
                            linear.create_issue("[ij-doctor] connectivity check",
                                                f"Safe to delete. {slack_thread_link(mk, s.slack_oncall_channel)}", 4, []))
    if not issue_id:
        return
    rep.add("linear", "create issue", True, issue_id)
    found = await _guard(rep, "linear", "find by marker", linear.find_issue_by_marker(mk))
    rep.add("linear", "find by marker", found == issue_id, "reconcile works" if found == issue_id else str(found))
    cid = await _guard(rep, "linear", "comment",
                       linear.comment(issue_id, f"doctor comment {slack_thread_link(mk, s.slack_oncall_channel)}"))
    if cid:
        rep.add("linear", "comment", True, cid)
    states = await _guard(rep, "linear", "workflow states", linear.workflow_states())
    if states is not None:
        rep.add("linear", "workflow states", any(x["type"] == "completed" for x in states),
                "has a completed state" if any(x["type"] == "completed" for x in states) else "no completed state")
    if await _guard(rep, "linear", "delete issue", linear.delete_issue(issue_id)) is None and \
            rep.checks[-1].step == "delete issue" and not rep.checks[-1].ok:
        return
    rep.add("linear", "cleanup", True, "test issue deleted")


async def _doctor_instatus(s: Settings, config: Config, instatus, rep: Report, tag: str) -> None:
    from judge.core.models import CustomerImpact
    from judge.safety.templates import INSTATUS_COMPONENT_STATUS, public_message, public_ref

    comps = await _guard(rep, "instatus", "auth + components", instatus.components())
    if comps is None:
        return
    rep.add("instatus", "auth + components", True, f"{len(comps)} components")
    ids = {c.get("id") for c in comps}
    public = [svc for svc in config.catalog.values() if svc.public]
    unmapped = [svc.name for svc in public if svc.instatus_component_id not in ids]
    if not rep.add("instatus", "catalog component ids", not unmapped,
                   "all mapped" if not unmapped else f"not on page: {unmapped} — run `judge bootstrap`"):
        return
    svc = public[0]
    ref = public_ref(f"{int(time.time()):08x}", tag)
    if not s.instatus_should_publish:
        rep.add("instatus", "unpublished mode", True, "INSTATUS_SHOULD_PUBLISH=false: test incident is not public")
    iid = await _guard(rep, "instatus", "create incident", instatus.create_incident(
        "Maintenance check", public_message("investigating", [svc], ref), [svc.instatus_component_id],
        INSTATUS_COMPONENT_STATUS[CustomerImpact.degraded]))
    if not iid:
        return
    rep.add("instatus", "create incident", True, iid)
    found = await _guard(rep, "instatus", "find by ref", instatus.find_incident_by_ref(ref))
    rep.add("instatus", "find by ref", found == iid, "reconcile works" if found == iid else str(found))
    await _guard(rep, "instatus", "delete incident", instatus.delete_incident(iid))
    rep.add("instatus", "cleanup", True, "test incident deleted")


async def _doctor_slack(s: Settings, slack, rep: Report, mk: str) -> None:
    info = await _guard(rep, "slack", "auth.test", slack.auth_info())
    if info is None:
        return
    rep.add("slack", "auth.test", True, f"bot {info.get('user')}")
    ch = s.slack_oncall_channel
    if not ch or (s.backend == "real" and ch == "C_ONCALL"):
        rep.add("slack", "on-call channel", False, "SLACK_ONCALL_CHANNEL missing — run `judge bootstrap`")
        return
    ts = await _guard(rep, "slack", "post", slack.post(ch, "[ij-doctor] connectivity check (safe to ignore)", ref=mk))
    if not ts:
        return
    rep.add("slack", "post", True, ts)
    found = await _guard(rep, "slack", "read back (history)", slack.find_message_by_marker(ch, mk))
    rep.add("slack", "read back (history)", found == ts, "reconcile works" if found == ts else str(found))
    await _guard(rep, "slack", "delete test message", slack.delete_message(ch, ts))
    rep.add("slack", "cleanup", True, "test message deleted")
    cfg_users = Config().policy.oncall_allowlist
    rep.add("slack", "on-call allowlist", s.backend != "real" or cfg_users != ["U_ONCALL_1"],
            f"{cfg_users}" if cfg_users != ["U_ONCALL_1"] else "set ONCALL_SLACK_USER_IDS to real member IDs")
