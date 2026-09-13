"""Knowledge graph: the LLM Wiki drawn as a layered graph.

Columns, left to right: dependencies → services (human docs) → incidents (raw timelines written by code) →
runbooks (LLM prose, code-owned stats) → catalog actions and runbook updates (GitHub pull requests).
Everything is read from the memory repo and the store; the SVG is rendered server-side (no JS, no CDN)."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

import yaml

W_NODE = 206
GAP_Y = 14
TOP = 74
COL_X = [30, 250, 500, 790, 1080]
WIDTH = 1320

TONES = {
    "dep": ("#5b6478", "dependency"),
    "service": ("#1d6ff2", "service (human docs)"),
    "incident_ok": ("#13c2b3", "incident fixed and verified"),
    "incident_manual": ("#2a8cf5", "incident resolved without an agent fix"),
    "incident_fail": ("#e5484d", "fix failed / rolled back"),
    "incident_open": ("#f59e0b", "incident open"),
    "runbook": ("#6f78f2", "runbook (LLM prose, code-owned stats)"),
    "action": ("#94a3b8", "catalog action"),
    "pr": ("#8b5cf6", "runbook update (pull request)"),
}


@dataclass
class Node:
    id: str
    col: int
    title: str
    sub: str = ""
    tone: str = "service"
    href: str | None = None
    badge: str = ""
    tip: str = ""
    h: int = 46
    y: float = 0


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, kind)

    def add(self, node: Node) -> None:
        self.nodes.setdefault(node.id, node)

    def link(self, a: str, b: str, kind: str = "") -> None:
        if a in self.nodes and b in self.nodes and (a, b, kind) not in self.edges:
            self.edges.append((a, b, kind))


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    try:
        return yaml.safe_load(text.split("---", 2)[1]) or {}
    except Exception:
        return {}


def build(data, max_incidents: int = 14) -> Graph:
    g = Graph()
    repo = data.repo()
    docs = set(data.docs()) if repo is not None else set()

    catalog = data.c.catalog
    for name, svc in catalog.items():
        doc = f"wiki/services/{name}.md"
        g.add(Node(f"svc:{name}", 1, name, f"tier {svc.tier} · {'public' if svc.public else 'internal'}",
                   "service", f"/wiki/docs/{doc}" if doc in docs else None,
                   tip=f"{svc.capability}. Depends on {', '.join(svc.depends_on) or 'nothing'}."))
        for dep in svc.depends_on:
            g.add(Node(f"dep:{dep}", 0, dep, "shared dependency", "dep", h=40))
            g.link(f"dep:{dep}", f"svc:{name}", "depends")

    runbooks = data.runbooks()
    for rb in runbooks:
        g.add(Node(f"rb:{rb['id']}", 3, rb["title"], f"✓ {rb['success']}  ✗ {rb['failure']}", "runbook",
                   f"/wiki/runbooks/{rb['id']}", badge=rb["level"], h=56,
                   tip=f"{rb['id']}: autonomy {rb['level']} (cap {rb['cap']}), earned from verified outcomes"))
        for s in rb["services"]:
            g.link(f"svc:{s}", f"rb:{rb['id']}", "covers")
        if rb["action"]:
            g.add(Node(f"act:{rb['action']}", 4, rb["action"], "catalog action", "action", h=40))
            g.link(f"rb:{rb['id']}", f"act:{rb['action']}", "fix")

    # incidents: raw timelines (what the wiki remembers) + incidents of this run that are still open
    seen: dict[str, dict] = {}
    if repo is not None:
        for rel in repo.list_files("raw/incidents"):
            if rel.endswith(".md"):
                fm = _frontmatter(repo.read(rel) or "")
                if fm.get("incident_id"):
                    seen[fm["incident_id"]] = fm
    store_ids = set()
    if data.store:
        for inc in data.store.incidents():
            store_ids.add(inc.id)
            seen.setdefault(inc.id, {"incident_id": inc.id, "services": inc.services,
                                     "severity": inc.severity.value if inc.severity else None,
                                     "runbook_id": inc.runbook_id, "created_at": inc.created_at.isoformat(),
                                     "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
                                     "error_types": [], "actions": [], "open": inc.resolved_at is None})
    recent = sorted(seen.values(), key=lambda f: str(f.get("created_at") or ""))[-max_incidents:]
    for fm in recent:
        iid = fm["incident_id"]
        results = [a.get("result") for a in fm.get("actions") or []]
        if fm.get("open"):
            tone = "incident_open"
        elif "failure" in results or "inconclusive" in results:
            tone = "incident_fail"
        elif "success" in results:
            tone = "incident_ok"
        else:
            tone = "incident_manual"
        services = fm.get("services") or []
        err = ", ".join(fm.get("error_types") or []) or "SLO burn"
        acts = ", ".join(a.get("action", "") for a in fm.get("actions") or [])
        g.add(Node(f"inc:{iid}", 2, f"{fm.get('severity') or 'SEV?'} · {', '.join(services)}", err, tone,
                   f"/incidents/{iid}" if iid in store_ids else None,
                   tip=f"{iid} · {str(fm.get('created_at') or '')[:16].replace('T', ' ')} UTC"
                       + (f" · fix: {acts} ({', '.join(r or '?' for r in results)})" if acts else "")))
        for s in services:
            g.link(f"svc:{s}", f"inc:{iid}", "incident")
        if fm.get("runbook_id"):
            g.link(f"inc:{iid}", f"rb:{fm['runbook_id']}", "matched")
        for a in fm.get("actions") or []:
            if a.get("action") and f"act:{a['action']}" not in g.nodes:
                g.add(Node(f"act:{a['action']}", 4, a["action"], "catalog action", "action", h=40))

    # runbook updates proposed from incidents, with their pull requests
    if repo is not None:
        prs = data.github_prs()
        for p in sorted(repo.proposals(), key=lambda p: p.created_at)[-8:]:
            pr = prs.get(p.id)
            label = f"PR #{pr['number']}" if pr else "proposal"
            g.add(Node(f"pr:{p.id}", 4, label, p.status, "pr", pr.get("url") if pr else "/wiki", h=40,
                       tip=p.title))
            for f in p.files:
                if f.startswith("wiki/runbooks/") and f.endswith(".md"):
                    g.link(f"rb:{f.rsplit('/', 1)[-1][:-3]}", f"pr:{p.id}", "updates")
            for line in (p.body or "").splitlines():
                if line.startswith("incident: "):
                    g.link(f"inc:{line.removeprefix('incident: ').strip()}", f"pr:{p.id}", "learned")
    return g


def _layout(g: Graph) -> int:
    cols: dict[int, list[Node]] = {}
    for n in g.nodes.values():
        cols.setdefault(n.col, []).append(n)
    # order: services by catalog order, others by the mean position of their neighbours to the left (fewer crossings)
    placed: dict[str, float] = {}
    heights = []
    for col in sorted(cols):
        nodes = cols[col]
        if col > 1:
            def key(n: Node) -> tuple:
                ys = [placed[a] for a, b, _ in g.edges if b == n.id and a in placed]
                ys += [placed[b] for a, b, _ in g.edges if a == n.id and b in placed]
                return (sum(ys) / len(ys) if ys else 1e9, n.title)
            nodes.sort(key=key)
        elif col == 0:
            def dkey(n: Node) -> tuple:
                order = [i for i, x in enumerate(cols.get(1, [])) for a, b, _ in g.edges if a == n.id and b == x.id]
                return (min(order) if order else 99, n.title)
            nodes.sort(key=dkey)
        y = TOP
        for n in nodes:
            n.y = y
            placed[n.id] = y + n.h / 2
            y += n.h + GAP_Y
        heights.append(y)
    # vertically centre shorter columns against the tallest
    tallest = max(heights) if heights else TOP
    for col, nodes in cols.items():
        if not nodes:
            continue
        span = nodes[-1].y + nodes[-1].h - TOP
        shift = (tallest - TOP - span) / 2
        for n in nodes:
            n.y += max(0, shift)
    return int(tallest + 78)


def _text(s: str, n: int) -> str:
    return escape(s if len(s) <= n else s[: n - 1] + "…")


def render_svg(g: Graph) -> str:
    height = _layout(g)
    out = [f'<svg class="kg" viewBox="0 0 {WIDTH} {height}" width="100%" role="img" '
           f'aria-label="Knowledge graph of services, incidents, runbooks, actions and runbook updates">',
           '<defs><marker id="kg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
           'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="kg-arrowhead"/></marker>'
           '<filter id="kg-shadow" x="-10%" y="-20%" width="120%" height="150%"><feDropShadow dx="0" dy="2" '
           'stdDeviation="3" flood-opacity="0.18"/></filter></defs>']
    heads = ["Dependencies", "Services · docs", "Incidents · raw timelines", "Runbooks · earned autonomy",
             "Fixes · runbook updates"]
    for i, text in enumerate(heads):
        out.append(f'<text x="{COL_X[i]}" y="36" class="kg-colhead">{escape(text.upper())}</text>')

    for a, b, kind in g.edges:
        na, nb = g.nodes[a], g.nodes[b]
        x1, y1 = COL_X[na.col] + W_NODE, na.y + na.h / 2
        x2, y2 = COL_X[nb.col] - 4, nb.y + nb.h / 2
        dx = max(40, (x2 - x1) * 0.45)
        skip = nb.col - na.col > 1
        cls = f"kg-edge kg-edge-{kind}" + (" kg-edge-skip" if skip else "")
        out.append(f'<path d="M{x1:.0f},{y1:.0f} C{x1 + dx:.0f},{y1:.0f} {x2 - dx:.0f},{y2:.0f} {x2:.0f},{y2:.0f}" '
                   f'class="{cls}" marker-end="url(#kg-arrow)"><title>{escape(na.title)} → {escape(nb.title)} '
                   f'({kind})</title></path>')

    for n in g.nodes.values():
        color = TONES[n.tone][0]
        x, y = COL_X[n.col], n.y
        body = [f'<g class="kg-node kg-{n.tone}" filter="url(#kg-shadow)">',
                f'<title>{escape(n.tip or n.title)}</title>',
                f'<rect x="{x}" y="{y:.0f}" width="{W_NODE}" height="{n.h}" rx="10" class="kg-box"/>',
                f'<rect x="{x}" y="{y:.0f}" width="5" height="{n.h}" rx="2.5" fill="{color}"/>',
                f'<text x="{x + 16}" y="{y + (20 if n.sub else n.h / 2 + 5):.0f}" class="kg-title">'
                f'{_text(n.title, 27)}</text>']
        if n.sub:
            body.append(f'<text x="{x + 16}" y="{y + 36:.0f}" class="kg-sub">{_text(n.sub, 30)}</text>')
        if n.badge:
            body.append(f'<rect x="{x + W_NODE - 42}" y="{y + n.h - 24:.0f}" width="32" height="17" rx="8.5" '
                        f'fill="{color}"/><text x="{x + W_NODE - 26}" y="{y + n.h - 12:.0f}" class="kg-badge" '
                        f'text-anchor="middle">{escape(n.badge)}</text>')
        body.append("</g>")
        node = "".join(body)
        if n.href:
            ext = ' target="_blank" rel="noopener"' if n.href.startswith("http") else ""
            node = f'<a href="{escape(n.href)}"{ext}>{node}</a>'
        out.append(node)

    rows = [("service", "runbook", "pr", "action"), ("incident_ok", "incident_manual", "incident_fail", "incident_open")]
    for r, keys in enumerate(rows):
        lx, ly = 30, height - 40 + r * 22
        for key in keys:
            color, label = TONES[key]
            out.append(f'<circle cx="{lx + 5}" cy="{ly - 4}" r="5" fill="{color}"/>'
                       f'<text x="{lx + 15}" y="{ly}" class="kg-legend">{escape(label)}</text>')
            lx += 290
    out.append("</svg>")
    return "".join(out)
