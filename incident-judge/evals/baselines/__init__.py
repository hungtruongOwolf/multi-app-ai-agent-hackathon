"""Baselines (SPEC §16.2). Same scenarios, same graders; the runner only changes the agent's env.

The agent honors IJ_BASELINE:
  B0 naive-agent          no policy engine, no templates: the LLM's proposal is executed directly
                          (public text may be free text, remediation without approval/verify gates)
  B1 no-memory            runbook query always returns none; no ingest
  B2 no-verify            remediation marks success and resolves right after apply (no SLO verify, no rollback)
  B3 no-match-conditions  runbook chosen by LLM/fingerprint is trusted without metric discriminators
Unset / "full" = the real system.
"""

from __future__ import annotations

BASELINES: dict[str, dict] = {
    "full": {"name": "Incident Judge (full)", "env": {}, "proves": "—"},
    "B0": {"name": "naive-agent", "env": {"IJ_BASELINE": "B0"}, "proves": "policy engine + templates"},
    "B1": {"name": "no-memory", "env": {"IJ_BASELINE": "B1"}, "proves": "LLM Wiki memory"},
    "B2": {"name": "no-verify", "env": {"IJ_BASELINE": "B2"}, "proves": "verifier + rollback"},
    "B3": {"name": "no-match-conditions", "env": {"IJ_BASELINE": "B3"}, "proves": "code-checked discriminators"},
}


def baseline_env(name: str | None) -> dict[str, str]:
    if not name or name == "full":
        return {}
    if name not in BASELINES:
        raise KeyError(f"unknown baseline {name!r}; choose from {sorted(BASELINES)}")
    return dict(BASELINES[name]["env"])
