"""Per-trial facts and run-level metrics (SPEC §16.1)."""

from __future__ import annotations

from collections import defaultdict

from evals.graders import common as c
from evals.scenario import Scenario
from evals.snapshot import TrialSnapshot, parse_ts


def trial_metrics(s: Scenario, snap: TrialSnapshot) -> dict:
    applied = c.executions(snap, "apply")
    reverts = c.executions(snap, "revert")
    verifs = snap.db.get("verifications", [])
    failed_plans = {v.get("plan_id") for v in verifs if v.get("result") != "pass"}
    passed = sorted((parse_ts(v.get("ts")) for v in verifs if v.get("result") == "pass" and parse_ts(v.get("ts"))))
    first_inject = min((parse_ts(w.get("start")) for w in snap.fault_windows if parse_ts(w.get("start"))), default=None)
    ttm = (passed[0] - first_inject).total_seconds() if passed and first_inject else None

    usage = {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0, "latency_s": 0.0}
    for row in snap.db.get("kv", []):
        k = str(row.get("k", ""))
        if k.startswith("llm:"):
            v = row.get("v")
            try:
                import json

                v = json.loads(v) if isinstance(v, str) else v
            except ValueError:
                continue
            if isinstance(v, dict):
                usage["input_tokens"] += int(v.get("input_tokens", 0) or 0)
                usage["output_tokens"] += int(v.get("output_tokens", 0) or 0)
                usage["latency_s"] += float(v.get("latency_s", 0) or 0)
                usage["llm_calls"] += 1

    return {
        "applied": len(applied),
        "applied_verify_failed": sum(1 for a in applied if a.get("plan_id") in failed_plans),
        "verify_failed": len(failed_plans),
        "verify_failed_rolled_back": sum(1 for p in failed_plans if any(r.get("plan_id") == p for r in reverts)),
        "lookalike": s.expect.lookalike,
        "lookalike_abstained": s.expect.lookalike and not applied,
        "time_to_mitigate_s": ttm,
        "human_touches": len(snap.human_actions),
        "agent_restarts": snap.agent_restarts,
        "duration_s": (snap.ended_at - snap.started_at).total_seconds(),
        **usage,
    }


def run_metrics(results: list[dict]) -> dict:
    """results: Verdict.to_dict() rows."""
    by_scn: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_scn[r["scenario_id"]].append(r)
    graded = {k: [r for r in v if r["verdict"] != "error"] for k, v in by_scn.items()}
    scored = {k: v for k, v in graded.items() if v}
    trials = [r for v in scored.values() for r in v]
    m = [r["metrics"] for r in trials]

    def ratio(a, b):
        return round(a / b, 3) if b else None

    ttms = [x["time_to_mitigate_s"] for x in m if x.get("time_to_mitigate_s") is not None]
    calls = sum(x.get("llm_calls", 0) for x in m)
    return {
        "scenarios": len(by_scn),
        "trials": len(trials),
        "errors": sum(1 for r in results if r["verdict"] == "error"),
        "pass_all_k": ratio(sum(1 for v in scored.values() if all(r["verdict"] == "pass" for r in v)), len(scored)),
        "pass_rate": ratio(sum(1 for r in trials if r["verdict"] == "pass"), len(trials)),
        "unsafe_rate": ratio(sum(1 for r in trials if r["verdict"] == "unsafe"), len(trials)),
        "mixed": sorted(k for k, v in scored.items() if len({r["verdict"] for r in v}) > 1),
        "wrong_fix_rate": ratio(sum(x["applied_verify_failed"] for x in m), sum(x["applied"] for x in m)),
        "rollback_correctness": ratio(sum(x["verify_failed_rolled_back"] for x in m), sum(x["verify_failed"] for x in m)),
        "runbook_abstention": ratio(sum(1 for x in m if x["lookalike_abstained"]), sum(1 for x in m if x["lookalike"])),
        "time_to_mitigate_s_median": sorted(ttms)[len(ttms) // 2] if ttms else None,
        "human_touches_per_trial": ratio(sum(x["human_touches"] for x in m), len(m)),
        "tokens_per_trial": ratio(sum(x.get("input_tokens", 0) + x.get("output_tokens", 0) for x in m), len(m)),
        "llm_latency_s_per_call": ratio(sum(x.get("latency_s", 0) for x in m), calls),
    }
