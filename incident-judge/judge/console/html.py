"""Tiny HTML component kit for the console (no template engine, no CDN). Every dynamic value is escaped."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

from judge.narration import fmt_value, label

MARKER = re.compile(r"`?IJ-(?:KEY|INC|TRIAL):[^\s`)&]+`?(?:[+\s]IJ-(?:INC|TRIAL):[^\s`)&]+)*")


def e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def clean(text: str | None) -> str:
    """Strip dedupe markers from any stored text before it is shown to people."""
    if not text:
        return ""
    text = re.sub(r"\[([^\]]+)\]\((https://slack\.com/app_redirect[^)]*)\)", r"\1", text)
    return MARKER.sub("", text).strip()


def when(dt: datetime | str | None) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return e(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def rel(dt: datetime | str | None) -> str:
    iso = when(dt)
    if not iso:
        return '<span class="muted">—</span>'
    parsed = datetime.fromisoformat(iso)
    secs = int((datetime.now(UTC) - parsed).total_seconds())
    if secs < 60:
        txt = f"{max(secs, 0)}s ago"
    elif secs < 3600:
        txt = f"{secs // 60}m ago"
    elif secs < 86400:
        txt = f"{secs // 3600}h ago"
    else:
        txt = f"{secs // 86400}d ago"
    return f'<time datetime="{e(iso)}" title="{e(parsed.strftime("%Y-%m-%d %H:%M:%S UTC"))}">{txt}</time>'


def clock(dt: datetime | str | None) -> str:
    iso = when(dt)
    return datetime.fromisoformat(iso).strftime("%H:%M:%S") if iso else ""


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


SEVERITY_TONE = {"SEV1": "red", "SEV2": "orange", "SEV3": "yellow", "SEV4": "gray"}
STATE_TONE = {
    "DETECTED": "gray", "TRIAGING": "gray", "OPEN": "red", "ESCALATED": "red",
    "AWAITING_PUBLIC_APPROVAL": "purple", "AWAITING_FIX_APPROVAL": "purple", "VETO_WINDOW": "purple",
    "REMEDIATION_PROPOSED": "blue", "EXECUTING": "blue", "VERIFYING": "blue", "ROLLING_BACK": "orange",
    "MONITORING": "teal", "RESOLVED": "green", "CLOSED": "green",
}
STATE_LABEL = {
    "AWAITING_FIX_APPROVAL": "Waiting for approval", "AWAITING_PUBLIC_APPROVAL": "Waiting for approval",
    "VETO_WINDOW": "Veto window", "ROLLING_BACK": "Rolling back", "REMEDIATION_PROPOSED": "Fix proposed",
}


def badge(text: str, tone: str = "gray") -> str:
    return f'<span class="badge badge-{e(tone)}">{e(text)}</span>'


def severity_badge(sev: str | None) -> str:
    return badge(sev, SEVERITY_TONE.get(sev, "gray")) if sev else badge("untriaged")


def state_badge(state: str) -> str:
    return badge(STATE_LABEL.get(state, state.replace("_", " ").title()), STATE_TONE.get(state, "gray"))


def button(href: str | None, text: str, icon: str = "", primary: bool = False) -> str:
    if not href:
        return ""
    ext = ' target="_blank" rel="noopener"' if href.startswith("http") else ""
    cls = "btn btn-primary" if primary else "btn"
    return f'<a class="{cls}" href="{e(href)}"{ext}>{icon}<span>{e(text)}</span></a>'


def empty(title: str, body: str) -> str:
    return f'<div class="empty"><div class="empty-title">{e(title)}</div><p>{e(body)}</p></div>'


def card(title: str, body: str, extra: str = "", cls: str = "") -> str:
    head = f'<div class="card-head"><h2>{e(title)}</h2>{extra}</div>' if title else ""
    return f'<section class="card {cls}">{head}<div class="card-body">{body}</div></section>'


def stat(label_text: str, value: str, hint: str = "") -> str:
    return (f'<div class="stat"><div class="stat-label">{e(label_text)}</div>'
            f'<div class="stat-value">{value}</div><div class="stat-hint">{e(hint)}</div></div>')


ICONS = {
    "alert": '<svg viewBox="0 0 16 16"><path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6v3.5M8 11.5v.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    "brain": '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5.5 8h5M8 5.5v5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    "hand": '<svg viewBox="0 0 16 16"><path d="M4 9V5.5a1 1 0 0 1 2 0V8m0-3.5v-1a1 1 0 0 1 2 0V8m0-4a1 1 0 0 1 2 0v4m0-2.5a1 1 0 0 1 2 0V10a4.5 4.5 0 0 1-8.3 2.4L2.5 10" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "wrench": '<svg viewBox="0 0 16 16"><path d="M10.5 1.8a3.5 3.5 0 0 0-3.3 4.6L2 11.6 4.4 14l5.2-5.2a3.5 3.5 0 0 0 4.6-3.3l-2 2-2.1-.5-.5-2.1z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
    "check": '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="m5 8.2 2 2 4-4.4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "x": '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="m5.7 5.7 4.6 4.6m0-4.6-4.6 4.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    "megaphone": '<svg viewBox="0 0 16 16"><path d="M2 6.5v3h2l6 3v-9l-6 3zM12 6a2.5 2.5 0 0 1 0 4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
    "chat": '<svg viewBox="0 0 16 16"><path d="M2.5 3h11v7.5H7L4 13v-2.5H2.5z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
    "book": '<svg viewBox="0 0 16 16"><path d="M3 2.5h7.5A2.5 2.5 0 0 1 13 5v8.5H5.5A2.5 2.5 0 0 1 3 11z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M3 11a2.5 2.5 0 0 1 2.5-2.5H13" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>',
    "dot": '<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="3" fill="currentColor"/></svg>',
    "slack": '<svg viewBox="0 0 16 16"><path d="M6 2.5a1.25 1.25 0 1 0 0 2.5h1.25V3.75A1.25 1.25 0 0 0 6 2.5zM2.5 6.5a1.25 1.25 0 0 0 0 2.5h4.75V6.5zm11 3a1.25 1.25 0 1 0-2.5 0V11h1.25A1.25 1.25 0 0 0 13.5 9.5zm-4.75-3V9h4.75a1.25 1.25 0 0 0 0-2.5zM10 13.5a1.25 1.25 0 1 0 0-2.5H8.75v1.25A1.25 1.25 0 0 0 10 13.5z" fill="currentColor"/></svg>',
    "link": '<svg viewBox="0 0 16 16"><path d="M6.5 9.5 9.5 6.5M7 4.5l1-1a2.8 2.8 0 0 1 4 4l-1 1m-2 2-1 1a2.8 2.8 0 0 1-4-4l1-1" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
}


def icon(name: str) -> str:
    return f'<span class="icon">{ICONS.get(name, ICONS["dot"])}</span>'


def mrkdwn(text: str | None) -> str:
    """Slack-ish inline formatting for stored messages: *bold*, `code`, <url|label>, newlines."""
    t = e(clean(text))
    t = re.sub(r"&lt;(https?://[^|&]+)\|([^&]+)&gt;", r'<a href="\1" target="_blank" rel="noopener">\2</a>', t)
    t = re.sub(r"&lt;@([A-Z0-9]+)&gt;", r'<span class="mention">@\1</span>', t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<em>\1</em>", t)
    t = re.sub(r":(white_check_mark|x|warning|rotating_light|wrench|mega|hourglass_flowing_sand|large_green_circle|"
               r"no_entry|books|page_facing_up|arrow_up):", lambda m: EMOJI.get(m.group(1), ""), t)
    return t.replace("\n", "<br>")


EMOJI = {"white_check_mark": "✅", "x": "❌", "warning": "⚠️", "rotating_light": "🚨", "wrench": "🔧", "mega": "📣",
         "hourglass_flowing_sand": "⏳", "large_green_circle": "🟢", "no_entry": "⛔", "books": "📚",
         "page_facing_up": "📄", "arrow_up": "⬆️"}


def markdown(text: str | None) -> str:
    """Small, safe markdown renderer for wiki pages (headings, lists, tables, code, emphasis, links)."""
    lines = clean(text or "").splitlines()
    out: list[str] = []
    i = 0
    in_list: str | None = None

    def inline(s: str) -> str:
        s = e(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        return s

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            close_list()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f"<pre><code>{e(chr(10).join(code))}</code></pre>")
        elif re.match(r"^#{1,6}\s", stripped):
            close_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            out.append(f"<h{min(level + 1, 6)}>{inline(stripped[level:].strip())}</h{min(level + 1, 6)}>")
        elif stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            close_list()
            head = [c.strip() for c in stripped.strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            i -= 1
            out.append('<div class="table-wrap"><table><thead><tr>' + "".join(f"<th>{inline(h)}</th>" for h in head)
                       + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                                                          for r in rows) + "</tbody></table></div>")
        elif re.match(r"^[-*]\s+", stripped):
            if in_list != "ul":
                close_list()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{inline(re.sub(r'^[-*]\s+', '', stripped))}</li>")
        elif re.match(r"^\d+\.\s+", stripped):
            if in_list != "ol":
                close_list()
                out.append("<ol>")
                in_list = "ol"
            out.append(f"<li>{inline(re.sub(r'^\d+\.\s+', '', stripped))}</li>")
        elif not stripped:
            close_list()
        else:
            close_list()
            out.append(f"<p>{inline(stripped)}</p>")
        i += 1
    close_list()
    return "\n".join(out)


def line_chart(series: list[float | None], metric: str, target: float | None, op: str = "<",
               before: float | None = None, height: int = 180) -> str:
    """Inline SVG: sampled values over time, with the target threshold as a dashed line."""
    points = [v for v in series if v is not None]
    if not points:
        return '<div class="muted small">No samples recorded.</div>'
    width, pad_l, pad_r, pad_t, pad_b = 640, 56, 16, 14, 28
    values = ([before] if before is not None else []) + points
    top = max(values + ([target] if target is not None else [])) or 1.0
    top *= 1.12
    n = len(values)
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b

    def x(i: int) -> float:
        return pad_l + (inner_w * i / max(n - 1, 1))

    def y(v: float) -> float:
        return pad_t + inner_h - inner_h * (v / top)

    coords = [(x(i), y(v)) for i, v in enumerate(values)]
    path = " ".join(("M" if i == 0 else "L") + f"{cx:.1f},{cy:.1f}" for i, (cx, cy) in enumerate(coords))
    area = path + f" L{coords[-1][0]:.1f},{pad_t + inner_h:.1f} L{coords[0][0]:.1f},{pad_t + inner_h:.1f} Z"
    grid = []
    for frac in (0, 0.5, 1):
        gv = top / 1.12 * frac
        gy = y(gv)
        grid.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{gy:.1f}" y2="{gy:.1f}" class="grid"/>'
                    f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" class="axis" text-anchor="end">{e(fmt_value(metric, gv))}</text>')
    target_svg = ""
    if target is not None:
        ty = y(target)
        target_svg = (f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{ty:.1f}" y2="{ty:.1f}" class="target"/>'
                      f'<text x="{width - pad_r}" y="{ty - 6:.1f}" class="target-label" text-anchor="end">'
                      f'target {e(op)} {e(fmt_value(metric, target))}</text>')
    dots = "".join(
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.2" class="{"dot-before" if before is not None and i == 0 else "dot"}">'
        f'<title>{"before the fix: " if before is not None and i == 0 else ""}{e(fmt_value(metric, values[i]))}</title></circle>'
        for i, (cx, cy) in enumerate(coords))
    first_label = "before fix" if before is not None else "first check"
    return (f'<figure class="chart"><svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{e(label(metric))} during verification">'
            + "".join(grid) + target_svg
            + f'<path d="{area}" class="area"/><path d="{path}" class="line"/>{dots}'
            + f'<text x="{pad_l}" y="{height - 8}" class="axis">{first_label}</text>'
            + f'<text x="{width - pad_r}" y="{height - 8}" class="axis" text-anchor="end">last check</text>'
            + "</svg></figure>")


NAV = [("overview", "/", "Overview"), ("incidents", "/incidents", "Incidents"), ("wiki", "/wiki", "Runbooks & docs"),
       ("graph", "/wiki/graph", "Knowledge graph"),
       ("shoplab", "/shoplab", "ShopLab"), ("evals", "/evals", "Evaluation")]


def layout(title: str, active: str, body: str, *, subtitle: str = "", refresh: str | None = None,
           context: str = "") -> str:
    nav = "".join(f'<a class="nav-item{" active" if key == active else ""}" href="{href}">{e(text)}</a>'
                  for key, href, text in NAV)
    refresh_attr = f' data-refresh="{e(refresh)}"' if refresh else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · Incident Judge</title><link rel="stylesheet" href="/static/console.css">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
</head><body>
<div class="shell">
<aside class="sidebar">
  <a class="brand" href="/"><img class="brand-mark" src="/static/logo-mark.svg" alt="Incident Judge logo" width="34" height="34"><span><strong>Incident Judge</strong><small>on-call console</small></span></a>
  <nav>{nav}</nav>
  <div class="sidebar-foot">{context}</div>
</aside>
<main id="main"{refresh_attr}>
  <header class="page-head"><div><h1>{e(title)}</h1>{f'<p class="subtitle">{subtitle}</p>' if subtitle else ''}</div></header>
  <div id="content">{body}</div>
</main>
</div>
<script src="/static/console.js"></script>
</body></html>"""
