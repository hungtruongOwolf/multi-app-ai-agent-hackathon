"""Incident Judge console (read-only). `uv run judge console` → http://127.0.0.1:8700"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from judge.console import html as h
from judge.console.data import ConsoleData
from judge.core.models import Incident
from judge.narration import fmt_value, label
from judge.settings import Config, Settings

STATIC = Path(__file__).resolve().parent / "static"
KIND_ICON = {"alert": "alert", "triage": "brain", "approval": "hand", "public": "megaphone", "change": "wrench",
             "verify": "check", "resolve": "check", "discussion": "chat", "memory": "book", "escalate": "x"}


def create_app(settings: Settings | None = None, config: Config | None = None,
               data: ConsoleData | None = None) -> FastAPI:
    data = data or ConsoleData(settings or Settings.from_env(), config)
    app = FastAPI(title="Incident Judge console", docs_url=None, redoc_url=None)

    def page(request: Request, title: str, active: str, body: str, **kw) -> HTMLResponse:
        if request.query_params.get("partial") == "1":
            return HTMLResponse(body)
        return HTMLResponse(h.layout(title, active, body, context=_context(data), **kw))

    @app.get("/static/{name}")
    def static(name: str) -> Response:
        path = STATIC / name
        if not path.is_file() or path.parent != STATIC:
            raise HTTPException(404)
        media = {"css": "text/css", "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], "application/javascript")
        return Response(path.read_text(encoding="utf-8"), media_type=media, headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    # ------------------------------------------------------------ overview
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        ov = data.overview()
        return page(request, "Overview", "overview", render_overview(data, ov),
                    subtitle="What is broken right now, what needs you, and what the agent has learned.",
                    refresh="/?partial=1")

    @app.get("/api/overview")
    def api_overview() -> JSONResponse:
        ov = data.overview()
        return JSONResponse({
            "open": [_inc_json(data, i) for i in ov["open"]], "waiting": [i.id for i in ov["waiting"]],
            "total": ov["total"], "mttr_s": ov["mttr_s"], "agent_live": ov["agent_live"],
            "runbooks": ov["runbooks"], "integrations": [{**i, "last": h.when(i["last"])} for i in ov["integrations"]],
        }, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ incidents
    @app.get("/incidents", response_class=HTMLResponse)
    def incidents(request: Request) -> HTMLResponse:
        return page(request, "Incidents", "incidents", render_incident_table(data, data.incidents()),
                    subtitle="Every incident the agent opened, newest first.", refresh="/incidents?partial=1")

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident(request: Request, incident_id: str) -> HTMLResponse:
        inc = data.incident(incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        return page(request, _incident_title(data, inc), "incidents", render_incident(data, inc),
                    subtitle=f"{h.severity_badge(inc.severity.value if inc.severity else None)} "
                             f"{h.state_badge(inc.state.value)} <span class='muted'>opened {h.rel(inc.created_at)}</span>",
                    refresh=f"/incidents/{inc.id}?partial=1")

    @app.get("/api/incidents")
    def api_incidents() -> JSONResponse:
        return JSONResponse([_inc_json(data, i) for i in data.incidents()], headers={"Cache-Control": "no-store"})

    @app.get("/api/incidents/{incident_id}")
    def api_incident(incident_id: str) -> JSONResponse:
        inc = data.incident(incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        return JSONResponse({
            **_inc_json(data, inc),
            "timeline": [{"at": h.when(t.at), "kind": t.kind, "title": h.clean(t.title), "detail": h.clean(t.detail)}
                         for t in data.timeline(inc)],
            "plans": [{"action": p.plan.action, "params": p.plan.params, "status": p.status,
                       "verifications": [{"result": v["result"], "samples": len(v["samples"])} for v in p.verifications]}
                      for p in data.plans(inc)],
            "diagnosis": data.kv(f"{inc.id}:diagnosis"),
        }, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ wiki
    @app.get("/wiki", response_class=HTMLResponse)
    def wiki(request: Request) -> HTMLResponse:
        return page(request, "Runbooks & docs", "wiki", render_wiki(data),
                    subtitle="The agent's memory: runbooks it earned, service docs it reads, and updates waiting for review.")

    @app.get("/wiki/runbooks/{runbook_id}", response_class=HTMLResponse)
    def runbook(request: Request, runbook_id: str) -> HTMLResponse:
        rb = data.runbook(runbook_id)
        if rb is None:
            raise HTTPException(404, "runbook not found")
        return page(request, rb.frontmatter.title, "wiki", render_runbook(data, rb), subtitle=f"<code>{h.e(rb.path)}</code>")

    @app.get("/wiki/docs/{path:path}", response_class=HTMLResponse)
    def doc(request: Request, path: str) -> HTMLResponse:
        text = data.doc(path)
        if text is None:
            raise HTTPException(404, "document not found")
        return page(request, path.rsplit("/", 1)[-1].removesuffix(".md").replace("-", " ").title(), "wiki",
                    h.card("", f'<article class="prose">{h.markdown(text)}</article>'), subtitle=f"<code>{h.e(path)}</code>")

    # ------------------------------------------------------------ shoplab & evals
    @app.get("/shoplab", response_class=HTMLResponse)
    def shoplab(request: Request) -> HTMLResponse:
        return page(request, "ShopLab", "shoplab", render_shoplab(data.shoplab()),
                    subtitle="The system under watch: live service state and every change made to it.",
                    refresh="/shoplab?partial=1")

    @app.get("/evals", response_class=HTMLResponse)
    def evals(request: Request) -> HTMLResponse:
        return page(request, "Evaluation", "evals", render_evals(data.reports()),
                    subtitle="Graded on the final state of the apps, audit-log invariants and seeded canaries.")

    return app


# ============================================================ helpers


def _context(data: ConsoleData) -> str:
    live = data.kv("agent_heartbeat")
    mode = "Real apps" if data.s.backend == "real" else "Local sandbox"
    judge = "Claude" if data.s.judge_impl == "claude" and data.s.anthropic_api_key else "Heuristic judge"
    return (f'<div class="ctx"><span class="pulse {"on" if live else ""}"></span>'
            f'<div><strong>{h.e(mode)}</strong><small>{h.e(judge)}'
            f'{" · run " + h.e(data.s.trial_id) if data.s.trial_id else ""}</small></div></div>')


def _incident_title(data: ConsoleData, inc: Incident) -> str:
    sentry = data.sentry_links(inc)
    if sentry:
        return f"{inc.primary_service} — {h.clean(sentry[0]['title'])}"
    proposal = data.kv(f"{inc.id}:proposal") or {}
    impact = str(proposal.get("customer_impact", "")).replace("_", " ")
    return f"{inc.primary_service} — {impact or 'incident'} ({inc.environment.value})"


def _inc_json(data: ConsoleData, inc: Incident) -> dict:
    url, ident = data.linear_url(inc)
    return {"id": inc.id, "title": _incident_title(data, inc), "state": inc.state.value,
            "severity": inc.severity.value if inc.severity else None,
            "impact": inc.customer_impact.value if inc.customer_impact else None,
            "services": inc.services, "environment": inc.environment.value,
            "created_at": h.when(inc.created_at), "resolved_at": h.when(inc.resolved_at),
            "links": {"slack": data.slack_thread_url(inc), "linear": url, "linear_id": ident,
                      "sentry": [s["url"] for s in data.sentry_links(inc) if s["url"]],
                      "status_page": data.status_page_url() if inc.public_posted else None,
                      "github_pr": (data.runbook_pr(inc) or {}).get("url")},
            "pagerduty": {"paged": bool(data.escalation(inc)["pages"]),
                          "resolved": bool(data.escalation(inc)["resolved_at"])}}


def links_bar(data: ConsoleData, inc: Incident) -> str:
    url, ident = data.linear_url(inc)
    buttons = [h.button(data.slack_thread_url(inc), "Open in Slack", h.icon("slack"), primary=True),
               h.button(url, f"Linear {ident}" if ident else "Linear", h.icon("link"))]
    for s in data.sentry_links(inc):
        buttons.append(h.button(s["url"], f"Sentry {s['short_id'] or ''}".strip(), h.icon("alert")))
    if inc.public_posted:
        buttons.append(h.button(data.status_page_url(), "Status page", h.icon("megaphone")))
    if inc.runbook_id:
        buttons.append(h.button(f"/wiki/runbooks/{inc.runbook_id}", "Runbook", h.icon("book")))
    pr = data.runbook_pr(inc)
    if pr:
        buttons.append(h.button(pr["url"], f"GitHub PR #{pr['number']}", h.icon("book")))
    buttons.append(h.button(data.shoplab_url("/ops"), "ShopLab ops", h.icon("wrench")))
    return f'<div class="btn-row">{"".join(b for b in buttons if b)}</div>'


def render_overview(data: ConsoleData, ov: dict) -> str:
    stats = (f'<div class="stats">'
             + h.stat("Open incidents", str(len(ov["open"])), "not resolved yet")
             + h.stat("Waiting for you", str(len(ov["waiting"])), "approvals pending in Slack")
             + h.stat("Mean time to resolve", h.duration(ov["mttr_s"]), f"{len(ov['recent_resolved'])} resolved")
             + h.stat("Agent", '<span class="ok">live</span>' if ov["agent_live"] else '<span class="muted">idle</span>',
                      f"heartbeat {'' if not ov['heartbeat'] else ''}")
             + "</div>")
    open_body = render_incident_table(data, ov["open"], compact=True) if ov["open"] else h.empty(
        "All clear", "No open incidents. When ShopLab breaks, the incident appears here within seconds.")
    resolved_body = render_incident_table(data, ov["recent_resolved"], compact=True) if ov["recent_resolved"] else h.empty(
        "Nothing resolved yet", "Resolved incidents show how long they took and what fixed them.")
    rb_rows = "".join(
        f'<tr><td><a href="/wiki/runbooks/{h.e(r["id"])}">{h.e(r["title"])}</a></td>'
        f'<td>{h.badge(r["level"], _level_tone(r["level"]))}{" " + h.badge("needs review", "orange") if r["review"] else ""}</td>'
        f'<td class="num">{r["success"]} <span class="muted">/ {r["failure"]}</span></td></tr>' for r in ov["runbooks"])
    runbooks = (f'<table class="table"><thead><tr><th>Runbook</th><th>Autonomy</th><th class="num">Verified ✓ / ✗</th></tr>'
                f'</thead><tbody>{rb_rows}</tbody></table>') if rb_rows else h.empty(
        "No runbooks yet", "Runbooks appear after an incident class is seen twice and a human merges the proposal.")
    def _integ_note(i: dict) -> str:
        parts = [h.e(i["role"])]
        if i.get("detail"):
            parts.append(h.e(i["detail"]))
        parts.append(f"last used {h.rel(i['last'])}" if i.get("last") else ("" if i["ok"] else "not configured"))
        return " · ".join(x for x in parts if x)

    integ = "".join(
        f'<li class="integ"><span class="dot-{("ok" if i["ok"] else "off")}"></span><strong>{h.e(i["name"])}</strong>'
        f'<span class="muted">{_integ_note(i)}</span></li>' for i in ov["integrations"])
    mode = ov["integrations"][0]["mode"] if ov["integrations"] else ""
    return (stats
            + '<div class="grid-2">'
            + h.card("Open incidents", open_body, extra=h.button("/incidents", "All incidents"))
            + h.card("Recently resolved", resolved_body)
            + "</div><div class='grid-2'>"
            + h.card("Runbooks the agent can use", runbooks, extra=h.button("/wiki", "Wiki"))
            + h.card("Integrations", f'<ul class="integ-list">{integ}</ul><p class="muted small">Mode: {h.e(mode)}. '
                                    f'Run <code>judge doctor</code> to prove each one end to end.</p>')
            + "</div>")


def _level_tone(level: str) -> str:
    return {"L0": "gray", "L1": "blue", "L2": "purple", "L3": "green"}.get(level, "gray")


def render_incident_table(data: ConsoleData, incs: list[Incident], compact: bool = False) -> str:
    if not incs:
        return h.empty("No incidents yet", "Inject a fault from ShopLab ops; the agent opens an incident here within seconds.")
    rows = []
    for inc in incs:
        url, ident = data.linear_url(inc)
        age = h.rel(inc.created_at)
        took = h.duration((inc.resolved_at - inc.created_at).total_seconds()) if inc.resolved_at else ""
        rows.append(
            f'<tr class="rowlink" data-href="/incidents/{h.e(inc.id)}">'
            f'<td>{h.severity_badge(inc.severity.value if inc.severity else None)}</td>'
            f'<td><a href="/incidents/{h.e(inc.id)}" class="strong">{h.e(_incident_title(data, inc))}</a>'
            f'<div class="muted small">{h.e(", ".join(inc.services))} · {h.e(inc.environment.value)}</div></td>'
            f'<td>{h.state_badge(inc.state.value)}</td>'
            + ("" if compact else f'<td>{h.e(ident) if ident else "<span class=muted>—</span>"}</td>')
            + f'<td class="num">{age}{"<div class=muted small>took " + took + "</div>" if took else ""}</td></tr>')
    head = ("<th>Severity</th><th>Incident</th><th>Status</th>" + ("" if compact else "<th>Ticket</th>")
            + "<th class='num'>Opened</th>")
    return f'<div class="table-wrap"><table class="table"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def render_incident(data: ConsoleData, inc: Incident) -> str:
    proposal = data.kv(f"{inc.id}:proposal") or {}
    match = data.kv(f"{inc.id}:match") or {}
    diagnosis = data.kv(f"{inc.id}:diagnosis")
    discussion = data.kv(f"{inc.id}:discussion") or []

    summary_items = [
        ("Severity", h.severity_badge(inc.severity.value if inc.severity else None)),
        ("Customer impact", h.e(str(proposal.get("customer_impact") or (inc.customer_impact.value if inc.customer_impact else "—")).replace("_", " "))),
        ("Customers can see it", "Yes" if inc.customer_visible else "No"),
        ("Services", h.e(", ".join(inc.services)) + f' <span class="muted">({h.e(inc.environment.value)})</span>'),
        ("Opened", h.rel(inc.created_at)),
        ("Resolved", h.rel(inc.resolved_at) if inc.resolved_at else '<span class="muted">not yet</span>'),
    ]
    if inc.resolved_at:
        summary_items.append(("Time to resolve", h.duration((inc.resolved_at - inc.created_at).total_seconds())))
    esc = data.escalation(inc)
    if esc["pages"]:
        state = h.badge("resolved", "green") if esc["resolved_at"] else h.badge("open", "red")
        reasons = "".join(f'<div class="muted small">{h.e(h.clean(p.get("reason")))}</div>' for p in esc["pages"])
        summary_items.append(("PagerDuty", f"Paged on-call {state}{reasons}"))
    else:
        summary_items.append(("PagerDuty", '<span class="muted">not paged</span>'))
    if data.sentry_resolved(inc):
        summary_items.append(("Sentry", f"{len(data.sentry_resolved(inc))} issue(s) marked resolved"))
    pr = data.runbook_pr(inc)
    if pr:
        tone = {"open": "purple", "merged": "green", "rejected": "gray"}.get(pr["status"], "gray")
        summary_items.append(("Runbook update", f'<a href="{h.e(pr["url"])}" target="_blank" rel="noopener">GitHub PR '
                                                f'#{pr["number"]}</a> {h.badge(pr["status"], tone)}'))
    summary = '<dl class="kv">' + "".join(f"<dt>{h.e(k)}</dt><dd>{v}</dd>" for k, v in summary_items) + "</dl>"

    # timeline
    events = data.timeline(inc)
    tl = "".join(
        f'<li class="tl tl-{h.e(ev.tone)}"><span class="tl-icon">{h.icon(KIND_ICON.get(ev.kind, "dot"))}</span>'
        f'<div class="tl-body"><div class="tl-title">{h.e(h.clean(ev.title))}'
        f'<time class="tl-time" datetime="{h.e(h.when(ev.at))}">{h.clock(ev.at)} UTC</time></div>'
        + (f'<div class="tl-detail">{h.mrkdwn(ev.detail)}</div>' if ev.detail else "") + "</div></li>"
        for ev in events)
    timeline = f'<ol class="timeline">{tl}</ol>' if tl else h.empty("No events yet", "Events appear as the agent works.")

    # evidence
    evidence = []
    for s in data.sentry_links(inc):
        link = f' · <a href="{h.e(s["url"])}" target="_blank" rel="noopener">open in Sentry</a>' if s["url"] else ""
        evidence.append(f'<li>{h.icon("alert")}<div><strong>Sentry</strong> <code>{h.e(h.clean(s["title"]))}</code>'
                        f'<div class="muted small">{s["count"]} events · {s["users"]} users{link}</div></div></li>')
    for sig in data.store.signals_for(inc.id) if data.store else []:
        if sig.source == "slo":
            burn = f"{sig.burn_rate:.0f}× error budget rate" if sig.burn_rate else "firing"
            evidence.append(f'<li>{h.icon("dot")}<div><strong>SLO</strong> <code>{h.e(sig.slo_name)}</code>'
                            f'<div class="muted small">{h.e(burn)}</div></div></li>')
    evidence_html = f'<ul class="evidence">{"".join(evidence)}</ul>' if evidence else h.empty(
        "No signals recorded", "Sentry errors and SLO breaches that opened this incident appear here.")
    if match.get("runbook_id"):
        conds = "".join(
            f'<li>{"✅" if c.get("ok") else ("❌" if c.get("ok") is False else "❔")} {h.e(label(c["metric"]))} '
            f'<strong>{h.e(fmt_value(c["metric"], c.get("observed")))}</strong> '
            f'<span class="muted">needs {h.e(c["op"])} {h.e(fmt_value(c["metric"], c["value"]))}</span></li>'
            for c in match.get("condition_results", []) if c.get("check") == "metric")
        verdict = {True: ("matched", "green"), False: ("does not match", "red"), None: ("could not be checked", "gray")}[
            match.get("match_ok")]
        evidence_html += (f'<div class="subcard"><div class="subcard-head">{h.icon("book")}<a href="/wiki/runbooks/'
                          f'{h.e(match["runbook_id"])}">{h.e(match["runbook_id"])}</a> {h.badge(verdict[0], verdict[1])}'
                          f'<span class="muted small">found via {h.e(match.get("via"))}</span></div>'
                          f'<ul class="conds">{conds}</ul></div>')
    rationale = h.clean(proposal.get("rationale_internal"))
    judgment = (f'<p class="prose">{h.mrkdwn(rationale)}</p>' if rationale
                else '<p class="muted">The judge has not written a rationale yet.</p>')

    # diagnosis
    diag_html = ""
    if diagnosis:
        hyps = "".join(
            f'<li><div class="hyp-head"><strong>{h.e(x.get("cause"))}</strong>'
            f'<span class="meter"><span style="width:{int(float(x.get("confidence") or 0) * 100)}%"></span></span>'
            f'<span class="muted small">{int(float(x.get("confidence") or 0) * 100)}%</span></div>'
            f'<ul class="small">{"".join(f"<li>{h.e(ev)}</li>" for ev in x.get("evidence", []))}</ul></li>'
            for x in diagnosis.get("hypotheses", []))
        fix = diagnosis.get("recommended_fix")
        fix_html = ""
        if fix:
            params = ", ".join(f"{k}={v}" for k, v in (fix.get("params") or {}).items())
            fix_html = (f'<div class="subcard"><div class="subcard-head">{h.icon("wrench")}<strong>Recommended fix</strong>'
                        f'<code>{h.e(fix.get("action"))}({h.e(params)})</code> on <code>{h.e(fix.get("target_service"))}</code></div>'
                        f'<dl class="kv"><dt>Why it fixes it</dt><dd>{h.e(fix.get("why_this_fixes_it"))}</dd>'
                        f'<dt>Risk</dt><dd>{h.e(fix.get("risk"))}</dd><dt>How to verify</dt><dd>{h.e(fix.get("how_to_verify"))}</dd></dl></div>')
        cited = "".join(f'<a class="chip" href="/wiki/docs/{h.e(p)}">{h.e(p)}</a>' for p in diagnosis.get("docs_cited", []))
        questions = "".join(f"<li>{h.e(q)}</li>" for q in diagnosis.get("open_questions", []))
        diag_html = h.card("Diagnosis", (
            f'<p class="lead">{h.e(diagnosis.get("summary"))}</p>'
            f'<p>{h.e(diagnosis.get("why_it_happens"))}</p>'
            + (f'<h3>Hypotheses</h3><ol class="hyps">{hyps}</ol>' if hyps else "")
            + fix_html
            + (f'<h3>Docs used</h3><div class="chips">{cited}</div>' if cited else "")
            + (f'<h3>Open questions</h3><ul>{questions}</ul>' if questions else "")))

    # plans & verification
    plans_html = []
    for pv in data.plans(inc):
        params = ", ".join(f"{k}={v}" for k, v in pv.plan.params.items())
        changes = "".join(f'<li>{h.mrkdwn(c.get("change"))} <span class="muted small">rollback: {h.mrkdwn(c.get("rollback"))}'
                          f'</span></li>' for c in (data.kv(f"{inc.id}:changes") or []))
        charts = []
        for v in pv.verifications:
            for cond in pv.plan.verify.conditions:
                series = []
                for smp in v["samples"]:
                    obs = next((c.get("observed") for c in smp.get("conditions", []) if c.get("metric") == cond.metric), None)
                    series.append(obs)
                before = (pv.before.get("values") or {}).get(cond.metric)
                charts.append(f'<div class="chart-head"><strong>{h.e(label(cond.metric))}</strong> on '
                              f'<code>{h.e(pv.plan.target_service)}</code> {h.badge("verification " + v["result"], "green" if v["result"] == "pass" else "red")}</div>'
                              + h.line_chart(series, cond.metric, cond.value, cond.op, before))
        status_tone = {"done": "green", "rolled_back": "orange", "failed": "red", "cancelled": "gray"}.get(pv.status, "blue")
        plans_html.append(
            f'<div class="subcard"><div class="subcard-head">{h.icon("wrench")}<code>{h.e(pv.plan.action)}({h.e(params)})</code>'
            f' on <code>{h.e(pv.plan.target_service)}</code> {h.badge(pv.status.replace("_", " "), status_tone)}'
            f' <span class="muted small">autonomy {h.e(pv.plan.autonomy_level.value)}</span></div>'
            + (f'<ul class="changes">{changes}</ul>' if changes else "")
            + "".join(charts) + "</div>")
    fix_card = h.card("Fix & verification", "".join(plans_html)) if plans_html else h.card(
        "Fix & verification", h.empty("No fix proposed", "When a runbook or diagnosis proposes a fix, the exact change, "
                                                          "who approved it and the live verification chart appear here."))

    # discussion
    disc_html = ""
    if discussion:
        msgs = "".join(
            f'<div class="msg msg-{h.e(m.get("role", "human"))}"><div class="msg-head"><strong>'
            f'{h.e(m.get("author") or ("Incident Judge" if m.get("role") == "agent" else "on-call"))}</strong>'
            f'<span class="muted small">{h.clock(m.get("ts"))} UTC</span></div><div>{h.mrkdwn(m.get("text"))}</div></div>'
            for m in discussion)
        disc_html = h.card("Discussion", f'<div class="thread">{msgs}</div>')

    audit_rows = "".join(
        f'<tr><td class="mono small">{h.clock(a["ts"])}</td><td><code>{h.e(a["intent"])}</code></td>'
        f'<td>{h.badge(a["result"].replace("_", " ").lower(), {"ALLOW": "green", "DENY": "red", "REQUIRE_APPROVAL": "purple"}.get(a["result"], "gray"))}</td>'
        f'<td>{h.e(", ".join(a["rules"]))}</td><td class="small">{h.e(h.clean(a["explain"]))}</td></tr>'
        for a in data.audit(inc))
    audit = h.card("Policy decisions", (
        f'<p class="muted small">Every write went through the policy engine first. Routine Slack posts and ticket comments are omitted.</p>'
        f'<div class="table-wrap"><table class="table"><thead><tr><th>Time</th><th>Intent</th><th>Result</th><th>Rules</th>'
        f'<th>Why</th></tr></thead><tbody>{audit_rows}</tbody></table></div>') if audit_rows else h.empty(
        "No decisions", "Policy decisions are recorded before every write."))

    left = (h.card("Story", timeline) + fix_card + disc_html)
    right = (h.card("Summary", summary) + h.card("Evidence", evidence_html) + h.card("Judgment", judgment) + diag_html)
    return links_bar(data, inc) + f'<div class="grid-detail"><div>{left}</div><div>{right}</div></div>' + audit


def render_wiki(data: ConsoleData) -> str:
    rbs = data.runbooks()
    rows = "".join(
        f'<a class="tile" href="/wiki/runbooks/{h.e(r["id"])}"><div class="tile-head"><strong>{h.e(r["title"])}</strong>'
        f'{h.badge(r["level"], _level_tone(r["level"]))}</div>'
        f'<div class="muted small">{h.e(", ".join(r["services"]))} · action <code>{h.e(r["action"] or "none")}</code></div>'
        f'<div class="tile-foot"><span>✓ {r["success"]}</span><span>✗ {r["failure"]}</span>'
        f'{h.badge("needs review", "orange") if r["review"] else ""}</div></a>' for r in rbs)
    runbooks = f'<div class="tiles">{rows}</div>' if rows else h.empty(
        "No runbooks yet", "Runbooks are proposed after an incident class happens twice; a human reviews and merges them.")
    docs = data.docs()
    doc_rows = "".join(f'<li><a href="/wiki/docs/{h.e(p)}">{h.icon("book")}{h.e(p.removeprefix("wiki/"))}</a></li>' for p in docs)
    docs_html = f'<ul class="doclist">{doc_rows}</ul>' if doc_rows else h.empty(
        "No service docs", "Architecture and per-service docs let the agent explain incidents it has never seen.")
    props = []
    for item in data.proposals():
        p = item["proposal"]
        tone = {"open": "purple", "merged": "green", "rejected": "gray"}.get(p.status, "gray")
        diff = "".join(
            f'<div class="diff-file">{h.e(d["file"])}</div><pre class="diff">'
            + "".join(f'<span class="{ "add" if ln.startswith("+") and not ln.startswith("+++") else "del" if ln.startswith("-") and not ln.startswith("---") else "ctx"}">{h.e(ln)}</span>\n'
                      for ln in d["lines"][:400]) + "</pre>" for d in item["diffs"])
        pr = item.get("pr")
        pr_link = (f' · <a href="{h.e(pr["url"])}" target="_blank" rel="noopener">GitHub PR #{pr["number"]}</a>'
                   if pr else "")
        props.append(f'<details class="proposal"><summary>{h.badge(p.status, tone)} <strong>{h.e(p.title)}</strong>'
                     f'<span class="muted small"> · {h.rel(p.created_at)}{" · " + h.e(p.reason) if p.reason else ""}'
                     f'{pr_link}</span></summary>{diff}</details>')
    proposals = "".join(props) or h.empty("No proposals", "After an incident closes the agent proposes what it learned; it lands here for review.")
    return (h.card("Runbooks", runbooks) + '<div class="grid-2">' + h.card("Service docs", docs_html)
            + h.card("Proposed updates", proposals) + "</div>")


def render_runbook(data: ConsoleData, rb) -> str:
    fm = rb.frontmatter
    conds = "".join(f'<li>{h.e(label(c.metric))} {h.e(c.op)} {h.e(fmt_value(c.metric, c.value))}</li>'
                    for c in fm.match_conditions)
    action = (f'<code>{h.e(fm.action.name)}({h.e(", ".join(f"{k}={v}" for k, v in fm.action.params.items()))})</code>'
              if fm.action else '<span class="muted">none</span>')
    side = ('<dl class="kv">'
            f'<dt>Autonomy</dt><dd>{h.badge(fm.autonomy.level.value, _level_tone(fm.autonomy.level.value))} '
            f'<span class="muted small">cap {h.e(fm.autonomy.cap.value)}</span>'
            f'{" " + h.badge("needs review", "orange") if fm.autonomy.review_required else ""}</dd>'
            f'<dt>Verified outcomes</dt><dd>✓ {fm.stats.success} · ✗ {fm.stats.failure} · ? {fm.stats.inconclusive}</dd>'
            f'<dt>Last verified</dt><dd>{h.rel(fm.stats.last_verified) if fm.stats.last_verified else "never"}</dd>'
            f'<dt>Action</dt><dd>{action}</dd>'
            f'<dt>Services</dt><dd>{h.e(", ".join(fm.signatures.services))}</dd>'
            f'<dt>Must hold on live metrics</dt><dd><ul class="conds">{conds}</ul></dd></dl>'
            '<p class="muted small">Stats and autonomy are computed by code from verified outcomes; the LLM cannot edit them.</p>')
    body = "".join(f"<h2>{h.e(title)}</h2>{h.markdown(text)}" for title, text in rb.sections.items())
    return f'<div class="grid-detail"><div>{h.card("", f"<article class=prose>{body}</article>")}</div><div>{h.card("At a glance", side)}</div></div>'


def render_shoplab(sl: dict) -> str:
    buttons = (f'<div class="btn-row">{h.button(sl["storefront"], "Open the storefront", h.icon("link"), primary=True)}'
               f'{h.button(sl["ops"], "Ops control room", h.icon("wrench"))}</div>')
    if not sl["reachable"]:
        return buttons + h.card("Services", h.empty("ShopLab is not running", "Start the stack with `uv run judge dev`."))
    rows = "".join(
        f'<tr><td><strong>{h.e(s["name"])}</strong><div class="muted small">{h.e(s.get("environment"))}</div></td>'
        f'<td>{h.badge("up", "green") if s.get("alive") else h.badge("down", "red")}</td>'
        f'<td class="small">{h.e(", ".join(f"{k}={v}" for k, v in (s.get("flags") or {}).items()) or "—")}</td>'
        f'<td class="num">{h.e(s.get("pool_size", "—"))}</td><td>{h.e(s.get("version", "—"))}</td>'
        f'<td>{"".join(h.badge(f, "orange") for f in s.get("faults") or []) or "<span class=muted>none</span>"}</td></tr>'
        for s in sl["services"])
    services = (f'<div class="table-wrap"><table class="table"><thead><tr><th>Service</th><th>Status</th><th>Flags</th>'
                f'<th class="num">DB pool</th><th>Version</th><th>Injected faults</th></tr></thead><tbody>{rows}</tbody></table></div>')
    changes = "".join(
        f'<li><span class="mono small">{h.clock(c.get("ts"))}</span> <strong>{h.e(c.get("service"))}</strong> '
        f'{h.e(c.get("summary") or c.get("kind"))} <span class="muted small">by {h.e(c.get("actor"))}</span></li>'
        for c in sl["changes"])
    change_html = f'<ul class="changes">{changes}</ul>' if changes else h.empty(
        "No changes recorded", "Flag flips, pool resizes, deploys and restarts show up here with who made them.")
    return buttons + h.card("Services", services) + h.card("Recent changes", change_html)


def render_evals(reports: list[dict]) -> str:
    if not reports:
        return h.empty("No eval runs", "Run `uv run python -m evals.runner --scenarios core --k 3`.")
    main = next((r for r in reports if r.get("baseline") in (None, "full") and r.get("k", 1) >= 3), reports[0])
    m = main.get("metrics", {})
    pct = lambda v: "—" if v is None else f"{v * 100:.0f}%"  # noqa: E731
    stats = ('<div class="stats">' + h.stat("pass^k", pct(m.get("pass_all_k")), "scenarios passing every trial")
             + h.stat("Unsafe trials", pct(m.get("unsafe_rate")), "forbidden writes, leaks, duplicates")
             + h.stat("Mixed scenarios", str(len(m.get("mixed") or [])), "different outcomes across trials")
             + h.stat("Rollback correctness", pct(m.get("rollback_correctness")), "failed fixes rolled back") + "</div>")
    by = {}
    for r in main.get("results", []):
        by.setdefault(r["scenario_id"], []).append(r["verdict"])
    tone = {"pass": "green", "fail": "orange", "unsafe": "red", "error": "gray"}
    rows = "".join(
        f'<tr><td><strong>{h.e(sid)}</strong></td><td>{h.e(main.get("titles", {}).get(sid, ""))}</td>'
        f'<td>{h.badge(main.get("tiers", {}).get(sid, ""), "gray")}</td>'
        f'<td>{"".join(h.badge(v, tone.get(v, "gray")) for v in by.get(sid, []))}</td></tr>'
        for sid in main.get("scenarios", []))
    table = (f'<div class="table-wrap"><table class="table"><thead><tr><th>ID</th><th>Scenario</th><th>Tier</th>'
             f'<th>Trials</th></tr></thead><tbody>{rows}</tbody></table></div>')
    base_rows = "".join(
        f'<tr><td><strong>{h.e(r.get("baseline") or "full")}</strong> <span class="muted small">{h.e(r["dir"])}</span></td>'
        f'<td class="num">{len(r.get("scenarios", []))}</td><td class="num">{h.e(r.get("k"))}</td>'
        f'<td class="num">{pct((r.get("metrics") or {}).get("pass_rate"))}</td>'
        f'<td class="num">{pct((r.get("metrics") or {}).get("unsafe_rate"))}</td></tr>' for r in reports)
    compare = (f'<div class="table-wrap"><table class="table"><thead><tr><th>Run</th><th class="num">Scenarios</th>'
               f'<th class="num">k</th><th class="num">Pass</th><th class="num">Unsafe</th></tr></thead><tbody>{base_rows}'
               f'</tbody></table></div>')
    return (stats + h.card(f"Scenarios — run {main['dir']}", table)
            + h.card("All runs (baselines remove one component each)", compare))
