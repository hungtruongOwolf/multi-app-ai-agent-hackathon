"""One Slack card per decision point, with one-click buttons and plain-language explanations.

A card bundles everything that needs a human right now for one incident (e.g. "post to the status page" AND
"run the known fix"). Buttons record one Approval per item through the same ApprovalVerifier as typed commands;
each item stays bound to its own hash, so policy still checks P6 (public) and P8/P14 (fix) independently.

Every item line carries its machine tag and a typed fallback (`approve <hash8>`), so the card also works where
buttons can't (no Socket Mode, the local sandbox, the eval harness)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from judge.core.models import Incident
from judge.safety.redact import redact

TAGS = {"public_post": "[IJ-PUBLIC]", "fix": "[IJ-FIX]", "veto": "[IJ-VETO]", "memory_merge": "[IJ-MEMORY]"}
ACTION_ID = "ij_card"


@dataclass
class CardItem:
    kind: str  # public_post | fix | veto | memory_merge
    subject_hash: str
    summary: str  # one line: what will happen
    details: list[str] = field(default_factory=list)


@dataclass
class Choice:
    key: str
    label: str
    explain: str
    verdicts: dict[str, str]  # kind -> approve | reject
    style: str | None = None


def choices_for(items: list[CardItem]) -> list[Choice]:
    kinds = {i.kind for i in items}
    if kinds == {"public_post", "fix"}:
        return [
            Choice("all", "✅ Approve all", "post the status page update and run the fix",
                   {"public_post": "approve", "fix": "approve"}, "primary"),
            Choice("fix_only", "🔧 Fix only", "run the fix, keep the public status page quiet for now",
                   {"public_post": "reject", "fix": "approve"}),
            Choice("post_only", "📣 Post only", "tell customers, don't change the system",
                   {"public_post": "approve", "fix": "reject"}),
            Choice("reject", "✋ Reject", "do neither; the incident stays open for a human",
                   {"public_post": "reject", "fix": "reject"}, "danger"),
        ]
    if kinds == {"veto"}:
        return [
            Choice("run_now", "▶️ Run now", "don't wait for the veto window", {"veto": "approve"}, "primary"),
            Choice("veto", "🛑 Veto", "cancel the automatic fix", {"veto": "reject"}, "danger"),
        ]
    if kinds == {"memory_merge"}:
        return [
            Choice("merge", "📚 Merge", "accept the runbook update into the wiki", {"memory_merge": "approve"},
                   "primary"),
            Choice("reject", "🗑️ Reject", "discard the proposal", {"memory_merge": "reject"}, "danger"),
        ]
    kind = next(iter(kinds))
    what = {"public_post": "publish the status page update", "fix": "run the fix"}.get(kind, "go ahead")
    return [
        Choice("approve", "✅ Approve", what, {k: "approve" for k in kinds}, "primary"),
        Choice("reject", "✋ Reject", "don't do it", {k: "reject" for k in kinds}, "danger"),
    ]


def build_card(incident: Incident, items: list[CardItem], *, oncall: list[str], timeout_min: float,
               title: str) -> tuple[str, list[dict]]:
    """Returns (fallback text, Block Kit blocks)."""
    choices = choices_for(items)
    lines = [f"*{title}*",
             "*Status:* waiting for your decision — nothing has been posted or changed yet.", ""]
    lines.append("*Proposed response*")
    for n, item in enumerate(items, 1):
        lines.append(f"{n}. {TAGS[item.kind]} {item.summary} — `approve {item.subject_hash[:8]}`")
        lines.extend(f"      • {d}" for d in item.details)
    lines += ["", "*What each button does*"]
    lines += [f"{c.label} — {c.explain}" for c in choices]
    safe_default = ("the fix runs automatically when the window ends" if {i.kind for i in items} == {"veto"}
                    else "nothing is posted or changed (safe default)")
    lines += ["", f"No answer within {timeout_min:.0f} min → {safe_default}.",
              f"Only on-call can decide: {' '.join(f'<@{u}>' for u in oncall) or 'on-call allowlist'}.",
              "_No buttons? Reply in this thread with `approve <hash>` or `reject <hash>` for one item._"]
    text = redact("\n".join(lines), 3500)
    value_items = [{"kind": i.kind, "hash": i.subject_hash} for i in items]
    buttons = []
    for c in choices:
        btn = {"type": "button", "action_id": f"{ACTION_ID}:{c.key}",
               "text": {"type": "plain_text", "text": c.label, "emoji": True},
               "value": json.dumps({"incident_id": incident.id, "items": value_items, "choice": c.key})}
        if c.style:
            btn["style"] = c.style
        buttons.append(btn)
    # What people see (blocks) has buttons, so it drops machine tags, hashes and the typed-command fallback.
    # The plain `text` (notifications, clients without blocks, the eval harness) keeps them.
    human = "\n".join(line for line in _strip_machine(text).splitlines() if not line.startswith("_No buttons?"))
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": human}},
        {"type": "actions", "block_id": f"ij_card_{items[0].subject_hash[:8]}", "elements": buttons},
    ]
    return text, blocks


def _strip_machine(text: str) -> str:
    import re

    text = re.sub(r"\s+—\s+`(?:approve|reject) [0-9a-f]{8}`", "", text)
    return re.sub(r"\[IJ-[A-Z]+\]\s*", "", text)


def verdicts_for(items: list[dict], choice: str) -> dict[str, str]:
    kinds = [CardItem(i["kind"], i["hash"], "") for i in items]
    for c in choices_for(kinds):
        if c.key == choice:
            return c.verdicts
    return {}


def decided_summary(card_text: str) -> str:
    """After a decision the card keeps what was proposed but drops the typed-command hashes, the button legend,
    the timeout and fallback lines — they no longer apply and only add noise."""
    import re

    keep = []
    for line in card_text.splitlines():
        if line.startswith("*What each button does*"):
            break
        line = re.sub(r"\s+—\s+`(?:approve|reject) [0-9a-f]{8}`", "", line)
        line = re.sub(r"\[IJ-[A-Z]+\]\s*", "", line)
        line = line.replace("*Status:* waiting for your decision — nothing has been posted or changed yet.",
                            "*Status:* decided (see below).")
        keep.append(line)
    return "\n".join(keep).rstrip()


def decided_blocks(original_text: str, outcome_line: str) -> list[dict]:
    """Replace the buttons with who decided what, so the thread shows a clear audit trail."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": original_text}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": outcome_line}]},
    ]


STATUS_FLOW = ("Status flow: Triage → Waiting for approval → Fixing → Verifying (SLOs) → Monitoring → Resolved. "
               "Anything that can't be verified or isn't approved stops safely and waits for a human.")
