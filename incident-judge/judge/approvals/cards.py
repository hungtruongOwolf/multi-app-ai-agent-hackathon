"""Internal Slack cards for approval requests. Internal surface: everything goes through the redactor.

Every card tells the human exactly what will happen, how it is verified, how it is rolled back,
and which hash they are approving (approval is bound to that hash)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from judge.core.models import AutonomyLevel, Incident, Plan
from judge.safety.redact import redact

if TYPE_CHECKING:
    from judge.memory.schema import Runbook


def _conditions(plan: Plan) -> str:
    if not plan.verify.conditions:
        return "no verify conditions (cannot be verified)"
    return ", ".join(f"{c.metric}{'(' + c.route + ')' if c.route else ''} {c.op} {c.value}"
                     for c in plan.verify.conditions)


def _buttons(kind: str, incident: Incident, subject_hash: str) -> dict:
    value = json.dumps({"kind": kind, "incident_id": incident.id, "subject_hash": subject_hash})
    return {
        "type": "actions",
        "block_id": f"ij_{kind}_{subject_hash[:8]}",
        "elements": [
            {"type": "button", "action_id": "ij_approve", "style": "primary",
             "text": {"type": "plain_text", "text": "Confirm"}, "value": value},
            {"type": "button", "action_id": "ij_reject", "style": "danger",
             "text": {"type": "plain_text", "text": "Reject"}, "value": value},
        ],
    }


def fix_card(incident: Incident, plan: Plan, runbook: "Runbook | None", level: AutonomyLevel) -> tuple[str, list]:
    h8 = plan.plan_hash[:8]
    fm = runbook.frontmatter if runbook is not None else None
    stats = fm.stats if fm is not None else None
    runbook_line = (f"Runbook `{fm.id}` — {fm.title} (success {stats.success}, failure {stats.failure})"
                    if fm is not None and stats is not None else "No runbook")
    prev = ", ".join(f"{k}={v}" for k, v in plan.prev_state.items()) or "captured before apply"
    if level == AutonomyLevel.L2:
        how = f"Runs automatically after the veto window unless someone replies `reject {h8}`."
    else:
        how = f"Reply `approve {h8}` to run it, or `reject {h8}`."
    params = ", ".join(f"{k}={v}" for k, v in plan.params.items()) or "none"
    text = redact(
        f":wrench: *Proposed fix* for incident {incident.id} ({incident.severity or 'SEV?'}, "
        f"{incident.environment.value})\n"
        f"• Action: `{plan.action}` on `{plan.target_service}` with {params}\n"
        f"• {runbook_line}\n"
        f"• Autonomy level: {level.value}\n"
        f"• Verify: {_conditions(plan)} over {plan.verify.window_s}s, min rps {plan.verify.min_rps}\n"
        f"• Rollback: automatic if verification fails (previous state: {prev})\n"
        f"• Plan hash: `{h8}`\n{how}"
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if level in (AutonomyLevel.L1, AutonomyLevel.L2):
        blocks.append(_buttons("fix", incident, plan.plan_hash))
    return text, blocks


def public_post_card(incident: Incident, impact: str, subject_hash: str) -> tuple[str, list]:
    h8 = subject_hash[:8]
    text = redact(
        f":mega: *Status page post needs approval* for incident {incident.id}\n"
        f"• Services: {', '.join(incident.services)} ({incident.environment.value})\n"
        f"• Severity: {incident.severity or 'SEV?'}, public impact: {impact}\n"
        f"• Text is rendered from the fixed public template (no free text).\n"
        f"• Subject hash: `{h8}`\n"
        f"Reply `approve {h8}` to publish, or `reject {h8}`. No answer = not published."
    )
    return text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
                  _buttons("public_post", incident, subject_hash)]


def denial_note(intent: str, rules: list[str], explain: str) -> str:
    return redact(f":no_entry: Blocked `{intent}` by policy {', '.join(rules)}: {explain}")
