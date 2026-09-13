"""Turns an LLM ActionProposal + a reviewed runbook into a concrete, hashable Plan.

The planner does not decide whether the plan may run (that is policy P7/P8/P9/P15). It always
produces a plan — even for an action outside the catalog — so the policy engine can DENY it and the
denial is recorded."""

from __future__ import annotations

from typing import TYPE_CHECKING

from judge.core.models import ActionProposal, AutonomyLevel, Incident, Plan, VerifySpec

if TYPE_CHECKING:
    from judge.memory.schema import Runbook
    from judge.settings import Config


def build_plan(incident: Incident, proposal: ActionProposal, runbook: "Runbook | None", config: "Config",
               autonomy: AutonomyLevel) -> Plan:
    fm = runbook.frontmatter if runbook is not None else None
    runbook_action = fm.action if fm is not None else None

    # Reviewed runbook params win; the proposal may only fill params the runbook leaves open.
    params = dict(proposal.params)
    if runbook_action is not None and runbook_action.name == proposal.name:
        params.update(runbook_action.params or {})

    spec = config.actions.get(proposal.name)
    if spec is not None:
        verify = VerifySpec(
            conditions=[c.bind(proposal.target_service) for c in spec.verify.conditions],
            window_s=spec.verify.window_s,
            min_rps=spec.verify.min_rps,
        )
    else:
        verify = VerifySpec(conditions=[])  # unverifiable -> policy denies, verifier would say inconclusive

    return Plan(
        incident_id=incident.id,
        runbook_id=fm.id if fm is not None else None,
        action=proposal.name,
        params=params,
        target_service=proposal.target_service,
        verify=verify,
        autonomy_level=autonomy,
    )
