"""Deterministic, offline stand-ins for the LLM memory roles. Used when no API key is configured
and in tests. Reports must label runs that used these."""

from __future__ import annotations

import re

from judge.memory.index import parse_index
from judge.memory.schema import REQUIRED_SECTIONS, Runbook, split_frontmatter


def _field(summary: str, name: str) -> list[str]:
    vals = []
    for m in re.finditer(rf"\b{name}=([^\s]+)", summary):
        vals.append(m.group(1).strip())
    return vals


class HeuristicChooser:
    """Picks the index entry whose services AND error types both match the incident's signals.
    Reads only the structured fields (never the untrusted message)."""

    async def choose(self, index_md: str, incident_summary: str) -> tuple[str | None, list[str]]:
        trusted = "\n".join(l for l in incident_summary.splitlines() if "untrusted_message" not in l)
        services = set(_field(trusted, "service"))
        m = re.search(r"^services: (.*)$", trusted, re.MULTILINE)
        if m:
            services |= {s.strip() for s in m.group(1).split(",") if s.strip()}
        errors = set(_field(trusted, "error_type"))
        best: tuple[int, str, list[str]] | None = None
        for e in parse_index(index_md):
            svc_hit = services & set(e["services"])
            err_hit = errors & set(e["errors"])
            if not svc_hit or not err_hit:
                continue
            score = len(svc_hit) + 2 * len(err_hit)
            ev = [f"index entry {e['id']}: services {sorted(svc_hit)}, error types {sorted(err_hit)}"]
            if best is None or score > best[0] or (score == best[0] and e["id"] < best[1]):
                best = (score, e["id"], ev)
        if best is None:
            return None, ["heuristic: no index entry matches both service and error type"]
        return best[1], best[2]


def _raw_facts(raw: str) -> dict:
    try:
        fm, _ = split_frontmatter(raw)
    except ValueError:
        return {}
    return fm


PLACEHOLDER = "none recorded yet"


class HeuristicWriter:
    """Builds prose from code-generated raw timelines. Keeps existing human/LLM prose and appends facts."""

    async def write(self, existing: Runbook | None, raw_timelines: list[str], agents_md: str) -> dict[str, str]:
        facts = [f for f in (_raw_facts(r) for r in raw_timelines) if f]
        services = sorted({s for f in facts for s in f.get("services", [])})
        errors = sorted({e for f in facts for e in f.get("error_types", [])})
        actions = [(f.get("incident_id"), a) for f in facts for a in f.get("actions", [])]
        worked = sorted({f"{a['action']} {a.get('params', {})}" for _, a in actions if a.get("result") == "success"})
        failed = sorted({f"{a['action']} {a.get('params', {})}" for _, a in actions
                         if a.get("result") in ("failure", "inconclusive")})
        occurrences = ", ".join(f"{f.get('incident_id')} ({f.get('created_at', '')[:10]})" for f in facts)

        generated = {
            "Summary": f"{', '.join(errors) or 'Unknown error'} on {', '.join(services) or 'unknown service'}; "
                       f"seen {len(facts)} times.",
            "Symptoms": "\n".join(f"- `{e}` on {', '.join(services)}" for e in errors) or f"- ({PLACEHOLDER})",
            "Known root causes": "Not yet determined — a human should fill this in after investigation.",
            "Remediation": "\n".join(f"- `{w}` — verified successful" for w in worked)
                           or f"- No verified remediation ({PLACEHOLDER}). Escalate to on-call.",
            "Tried and did not work": "\n".join(f"- `{w}` — verification failed / inconclusive" for w in failed)
                                      or f"- ({PLACEHOLDER})",
            "How to tell apart": "No distinguishing conditions yet. A reviewer must add `match_conditions` "
                                 "before this runbook can be used for automatic remediation.",
            "Notes": f"Occurrences: {occurrences or '-'}",
        }
        if existing is None:
            return generated
        out = {}
        for name in REQUIRED_SECTIONS:
            old = existing.sections.get(name, "").strip()
            if name in ("Remediation", "Tried and did not work"):
                new_lines = [l for l in generated[name].splitlines() if l not in old and PLACEHOLDER not in l]
                out[name] = "\n".join([old, *new_lines]).strip() if old else generated[name]
            elif name == "Notes":
                out[name] = "\n".join(x for x in [old, generated[name]] if x and generated[name] not in old).strip() \
                    or old
            else:
                out[name] = old or generated[name]
        return out
