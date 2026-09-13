"""Human-readable narration for the incident thread: what the agent saw, exactly what it will change,
live verification progress, and a final report. The goal is that an engineer can follow — and audit — every
step without reading the agent's database. Internal surface: everything goes through the redactor."""

from __future__ import annotations

from datetime import datetime

from judge.core.models import Incident, Plan, Signal
from judge.safety.redact import redact

METRIC_LABELS = {
    "error_rate": ("error rate", "pct"),
    "latency_p95": ("p95 latency", "s"),
    "rps": ("traffic", "rps"),
    "pool_utilization": ("DB pool in use", "pct"),
    "pool_wait_p95": ("DB pool wait p95", "s"),
    "db_query_p95": ("DB query p95", "s"),
    "memory_mb": ("memory", "mb"),
}


def fmt_value(metric: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    unit = METRIC_LABELS.get(metric.split(":")[0], (metric, ""))[1]
    if unit == "pct":
        return f"{value * 100:.1f}%"
    if unit == "s":
        return f"{value * 1000:.0f} ms" if value < 1 else f"{value:.2f} s"
    if unit == "rps":
        return f"{value:.1f} rps"
    if unit == "mb":
        return f"{value:.0f} MB"
    return f"{value:.3f}"


def label(metric: str) -> str:
    base, _, route = metric.partition(":")
    name = METRIC_LABELS.get(base, (base, ""))[0]
    return f"{name} on {route}" if route else name


def hhmmss(dt: datetime | str | None) -> str:
    if dt is None:
        return "--:--:--"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt.strftime("%H:%M:%S")


def evidence_lines(signals: list[Signal], metrics: dict[str, dict[str, float | None]],
                   sentry_meta: dict[str, dict]) -> list[str]:
    """What the agent actually observed (measured facts, not the LLM's opinion)."""
    lines: list[str] = []
    for s in signals:
        if s.source == "sentry":
            meta = sentry_meta.get(s.external_id or "", {})
            link = f" · <{meta['permalink']}|open in Sentry>" if meta.get("permalink") else ""
            title = meta.get("title") or f"{s.error_type} at {s.culprit}"
            lines.append(f"Sentry: `{redact(title, 120)}` — {s.count} events, {s.user_count} users, "
                         f"first seen {hhmmss(s.first_seen)}{link}")
        elif s.source == "slo":
            burn = f"{s.burn_rate:.0f}× the error budget rate" if s.burn_rate else "firing"
            lines.append(f"SLO `{s.slo_name}` is burning: {burn}")
    for service, values in metrics.items():
        shown = []
        for key in ("error_rate", "latency_p95", "rps", "pool_utilization", "db_query_p95"):
            if values.get(key) is not None:
                shown.append(f"{label(key)} {fmt_value(key, values[key])}")
        routes = [k for k in values if ":" in k and k.startswith("error_rate:") and values[k] is not None]
        shown += [f"{label(k)} {fmt_value(k, values[k])}" for k in routes]
        if shown:
            lines.append(f"Metrics `{service}` (last minute): " + ", ".join(shown))
    return lines


def match_lines(match: dict) -> list[str]:
    if not match.get("runbook_id"):
        return ["No runbook matched this incident."]
    ok = match.get("match_ok")
    head = {True: "matched", False: "looks similar but does NOT match", None: "could not be checked"}[ok]
    lines = [f"Runbook `{match['runbook_id']}` {head} (found via {match.get('via')}):"]
    for c in match.get("condition_results", []):
        if c.get("check") != "metric":
            continue
        mark = "✅" if c.get("ok") else ("❌" if c.get("ok") is False else "❔")
        lines.append(f"   {mark} {label(c['metric'])} {fmt_value(c['metric'], c.get('observed'))} "
                     f"(needs {c['op']} {fmt_value(c['metric'], c['value'])})")
    return lines


def change_preview(plan: Plan, current: dict | None) -> tuple[str, str]:
    """(exact change, exact rollback) in terms an engineer can check against the system."""
    svc, p = plan.target_service, plan.params
    cfg = current or {}
    if plan.action == "toggle_flag":
        before = (cfg.get("flags") or {}).get(p["flag"], "?")
        return (f"`{svc}` feature flag `{p['flag']}`: *{str(before).lower()} → {str(p['value']).lower()}*",
                f"set `{p['flag']}` back to `{str(before).lower()}`")
    if plan.action == "scale_pool":
        before = cfg.get("pool_size", "?")
        return (f"`{svc}` DB connection pool size: *{before} → {p['size']}*", f"set pool size back to `{before}`")
    if plan.action == "restart_service":
        return (f"restart the `{svc}` process (in-memory state is lost; no config change)",
                "none — a restart cannot be undone, but it is safe to repeat")
    if plan.action == "rollback_deploy":
        before = cfg.get("app_version", "?")
        return (f"`{svc}` deployed version: *{before} → {p['to_version']}*", "none — requires a new deploy")
    return (f"`{plan.action}` {p} on `{svc}`", "see action catalog")


def verify_targets(plan: Plan) -> str:
    return ", ".join(f"{label(c.metric)} {c.op} {fmt_value(c.metric, c.value)}" for c in plan.verify.conditions)


def progress_text(plan: Plan, samples: list[dict], n: int, interval: float, done: str | None = None,
                  before: dict[str, float | None] | None = None, applied_at: datetime | str | None = None) -> str:
    """One message, edited in place, that tells the whole verification story to someone who wasn't watching:
    value before the change, every sample with its time since the change, the target, and a one-line verdict."""
    before = before or {}
    t0 = _parse(applied_at) or (_parse(samples[0].get("t")) if samples else None)
    head = done or f":hourglass_flowing_sand: *Status: verifying* — sample {len(samples)} of {n}, one every {interval:.0f}s"
    lines = [head]
    per_metric: dict[str, list[tuple[datetime | None, float | None, bool | None, dict]]] = {}
    for smp in samples:
        for c in smp.get("conditions", []):
            per_metric.setdefault(c["metric"], []).append((_parse(smp.get("t")), c.get("observed"), c.get("holds"), c))
    for metric, series in per_metric.items():
        cond = series[-1][3]
        target = f"{cond['op']} {fmt_value(metric, cond['value'])}"
        now_value = series[-1][1]
        mark = "✅" if series[-1][2] else ("❌" if series[-1][2] is False else "❔")
        was = fmt_value(metric, before.get(metric)) if before.get(metric) is not None else fmt_value(metric, series[0][1])
        was_label = "before the fix" if before.get(metric) is not None else "at the first check"
        lines.append(f"{mark} *{label(metric)}* on `{plan.target_service}`: {was} {was_label} → "
                     f"*{fmt_value(metric, now_value)}* now (target {target})")
        story = _recovery_sentence(series, t0)
        if story:
            lines.append(f"      {story}")
        lines.append("      " + _timeline(metric, series, t0))
    if samples:
        rps = samples[-1].get("rps")
        lines.append(f"Traffic during the check: {fmt_value('rps', rps)} "
                     f"(needs at least {plan.verify.min_rps:.0f} rps for the numbers to mean something)")
    if not done:
        lines.append("_Passes when every target holds on the last 2 samples; otherwise the change is rolled back._")
    return "\n".join(lines)


def _parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed(t: datetime | None, t0: datetime | None) -> str:
    if t is None or t0 is None:
        return "?"
    return f"+{max(0, int((t - t0).total_seconds()))}s"


def _timeline(metric: str, series, t0) -> str:
    """'88.6% → 68.4% → 49.3% → 32.9% → 15.6% → 0.0%': every distinct value in order, repeats collapsed."""
    values: list[str] = []
    for _, v, _, _ in series:
        text = fmt_value(metric, v)
        if not values or values[-1] != text:
            values.append(text)
    return " → ".join(values)


def _recovery_sentence(series, t0) -> str | None:
    first_ok = next((i for i, (_, _, holds, _) in enumerate(series) if holds), None)
    if first_ok is None:
        return "Not below target yet." if series else None
    if all(h for _, _, h, _ in series[first_ok:]):
        t_ok, t_last = series[first_ok][0], series[-1][0]
        held = int((t_last - t_ok).total_seconds()) if t_ok and t_last else 0
        return f"Reached the target {_elapsed(t_ok, t0).lstrip('+')} after the change and has held for {held}s."
    return "Touched the target but did not stay there."


def compress_series(metric: str, values: list[float | None], max_points: int = 8) -> str:
    """'88.6% (first) → 68.4% → 49.3% → 0.0% ×7' — the whole trajectory, repeats collapsed, long runs thinned."""
    if not values:
        return "n/a"
    runs: list[list] = []
    for v in values:
        text = fmt_value(metric, v)
        if runs and runs[-1][0] == text:
            runs[-1][1] += 1
        else:
            runs.append([text, 1])
    if len(runs) > max_points:  # keep first, last and evenly spaced middle points
        step = (len(runs) - 2) / (max_points - 2)
        runs = [runs[0]] + [runs[1 + round(i * step)] for i in range(max_points - 2)] + [runs[-1]]
    parts = [f"{t} ×{n}" if n > 1 else t for t, n in runs]
    parts[0] += " (first sample)"
    return " → ".join(parts)


def linear_description(*, summary: str, severity: str, impact: str, visible: bool, services: list[str], env: str,
                       evidence: list[str], metrics: dict[str, dict[str, float | None]], rationale: str,
                       match: dict, proposal_line: str | None, slack_hint: str, judge: str) -> str:
    """A ticket an engineer can read cold: what, how bad, evidence, diagnosis, what we will do."""
    out = [f"## Summary", summary, "",
           "## Impact",
           f"* **Severity:** {severity}",
           f"* **Customer impact:** {impact.replace('_', ' ')} — {'customers can see it' if visible else 'not customer-visible'}",
           f"* **Service:** {', '.join(services)} ({env})", "",
           "## Evidence"]
    out += [f"* {line}" for line in evidence] or ["* (no signals recorded)"]
    rows = []
    for service, values in metrics.items():
        for key in ("error_rate", "latency_p95", "rps", "pool_utilization", "db_query_p95"):
            if values.get(key) is not None:
                rows.append(f"| {service} | {label(key)} | {fmt_value(key, values[key])} |")
        for key, v in values.items():
            if ":" in key and v is not None:
                rows.append(f"| {service} | {label(key)} | {fmt_value(key, v)} |")
    if rows:
        out += ["", "| Service | Metric (last minute) | Value |", "|---|---|---|", *rows]
    out += ["", "## Diagnosis", f"_{judge} judge:_ {rationale or '-'}"]
    if match.get("runbook_id"):
        out += ["", *[line.replace("   ", "* ", 1) if line.startswith("   ") else line for line in match_lines(match)]]
        out.append(f"* Runbook file: `wiki/runbooks/{match['runbook_id']}.md`")
    out += ["", "## Plan", proposal_line or "No automatic fix: a human needs to investigate.", "",
            "## How this ticket is updated",
            f"Every decision (approval, change applied, verification, resolution) is added as a comment. {slack_hint}"]
    return redact("\n".join(out), 7000)


def diagnosis_text(dg: dict, had_runbook: bool) -> str:
    """Explain a first-time (or look-alike) failure the way a senior engineer would in the thread."""
    head = (":mag: *Diagnosis* — the matching runbook doesn't fit this one, so I read the service docs, the change "
            "log and the metrics" if had_runbook else
            ":mag: *Diagnosis* — no runbook for this failure yet, so I read the service docs, the change log and the metrics")
    lines = [head, f"*In short:* {dg.get('summary', '-')}"]
    hyps = sorted(dg.get("hypotheses") or [], key=lambda h: -float(h.get("confidence") or 0))
    if hyps:
        lines.append("*Most likely causes*")
        for h in hyps[:3]:
            lines.append(f"• {h.get('cause')} — {round(float(h.get('confidence') or 0) * 100)}% confident")
            lines += [f"      ◦ {e}" for e in (h.get("evidence") or [])[:3]]
    if dg.get("why_it_happens"):
        lines.append(f"*Why it happens:* {dg['why_it_happens']}")
    fix = dg.get("recommended_fix")
    if fix:
        params = ", ".join(f"{k}={v}" for k, v in (fix.get("params") or {}).items()) or "no parameters"
        lines.append(f"*Recommended fix:* `{fix.get('action')}` ({params}) on `{fix.get('target_service')}` — "
                     f"{fix.get('why_this_fixes_it', '')}")
        if fix.get("risk"):
            lines.append(f"*Risk:* {fix['risk']}")
        lines.append("_It won't run until someone confirms it on the card. You can also reply with a different fix._")
    else:
        lines.append("*Recommended fix:* none I'm allowed to run safely — this needs an engineer. "
                     "Reply in the thread if you want me to try a specific action.")
    if dg.get("open_questions"):
        lines.append("*Open questions:* " + "; ".join(dg["open_questions"][:3]))
    if dg.get("docs_cited"):
        lines.append("*Docs used:* " + ", ".join(f"`{d}`" for d in dg["docs_cited"][:5]))
    return redact("\n".join(lines), 3500)


def incident_report(inc: Incident, timeline: list[tuple[datetime, str]], changes: list[str],
                    links: list[str], memory_line: str | None) -> str:
    timeline = sorted(timeline, key=lambda x: x[0])
    lines = [f":page_facing_up: *Incident report — {inc.severity or ''} {', '.join(inc.services)} "
             f"({inc.environment.value})*", "", "*Timeline (UTC)*"]
    lines += [f"• {hhmmss(t)} — {what}" for t, what in timeline]
    start = timeline[0][0] if timeline else inc.created_at
    mitigated = next((t for t, w in timeline if w.startswith("Verification passed")), None)
    if inc.resolved_at:
        lines.append("")
        parts = []
        if mitigated:
            parts.append(f"time to mitigate {_dur(mitigated - start)}")
        parts.append(f"time to resolve {_dur(inc.resolved_at - start)}")
        lines.append("*Durations:* " + ", ".join(parts))
    lines += ["", "*What changed*"] + ([f"• {c}" for c in changes] or ["• Nothing was changed by the agent."])
    if links:
        lines += ["", "*Links:* " + " · ".join(links)]
    if memory_line:
        lines += ["", f"*Memory:* {memory_line}"]
    return redact("\n".join(lines), 3800)


def _dur(delta) -> str:
    s = int(delta.total_seconds())
    return f"{s // 60}m{s % 60:02d}s"
