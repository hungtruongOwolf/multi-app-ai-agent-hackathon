"""Slack Socket Mode handler for one-click card buttons (real mode, no public URL needed).

A click records one Approval per card item through the SAME ApprovalVerifier as typed commands, bound to the
item's current hash (a stale card cannot approve a changed plan). The card is then updated in place to show who
decided what. The agent loop picks the approvals up from the store exactly like typed approvals."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable

from judge.approvals.response_card import ACTION_ID, CardItem, choices_for, decided_blocks, decided_summary, verdicts_for
from judge.approvals.verifier import ApprovalVerifier
from judge.core.models import Approval, Incident
from judge.core.store import Store

log = logging.getLogger("judge.approvals.socket")

try:  # pragma: no cover - optional at import time
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    SLACK_BOLT_AVAILABLE = True
except Exception:  # pragma: no cover
    AsyncApp = None  # type: ignore[assignment]
    AsyncSocketModeHandler = None  # type: ignore[assignment]
    SLACK_BOLT_AVAILABLE = False


def handle_card_action(store: Store, verifier: ApprovalVerifier, incident_lookup: Callable[[str], Incident | None],
                       current_subject: Callable[[Incident, str, str], str | None],
                       body: dict) -> tuple[list[Approval], str]:
    """Pure handler, testable without Slack. current_subject(incident, kind, clicked_hash) returns the hash that
    is live for that kind right now (or None). Returns (recorded approvals, outcome line for the card)."""
    try:
        action = body["actions"][0]
        value = json.loads(action["value"])
        items, choice = value["items"], value["choice"]
    except Exception:
        log.warning("malformed card action payload")
        return [], ":warning: malformed button payload — nothing recorded"
    incident = incident_lookup(value.get("incident_id", ""))
    if incident is None:
        return [], ":warning: incident not found — nothing recorded"
    user = body.get("user") or {}
    user_id = user.get("id", "")
    verdicts = verdicts_for(items, choice)
    label = next((c.label for c in choices_for([CardItem(i["kind"], i["hash"], "") for i in items])
                  if c.key == choice), choice)
    approvals: list[Approval] = []
    for item in items:
        kind = item["kind"]
        verdict = verdicts.get(kind)
        if verdict is None:
            continue
        live = current_subject(incident, kind, item["hash"]) or ""
        a = verifier.verify(incident=incident, kind="fix" if kind == "veto" else kind, subject_hash=live,
                            user_id=user_id, is_bot=bool(user.get("is_bot")), verdict=verdict, via="button",
                            provided_hash=item["hash"])
        store.add_approval(a)
        approvals.append(a)
    stamp = datetime.now().strftime("%H:%M")
    invalid = [a for a in approvals if not a.valid]
    if invalid:
        reasons = "; ".join(sorted({a.reason for a in invalid if a.reason}))
        return approvals, f":warning: <@{user_id}> clicked *{label}* at {stamp} but it was not accepted: {reasons}"
    return approvals, f"*{label}* — decided by <@{user_id}> at {stamp}. The agent is acting on it now."


class SocketApprovals:  # pragma: no cover - needs a live Slack connection
    def __init__(self, settings, store: Store, verifier: ApprovalVerifier,
                 incident_lookup: Callable[[str], Incident | None],
                 current_subject: Callable[[Incident, str, str], str | None]):
        if not SLACK_BOLT_AVAILABLE:
            raise RuntimeError("slack-bolt is not installed")
        if not settings.slack_app_token:
            raise RuntimeError("SLACK_APP_TOKEN (xapp-…) is required for Socket Mode")
        self.app = AsyncApp(token=settings.slack_bot_token)
        self.handler = AsyncSocketModeHandler(self.app, settings.slack_app_token)

        import re

        @self.app.action(re.compile(rf"^{ACTION_ID}:"))
        async def _on_click(ack, body, client):
            await ack()
            try:
                approvals, outcome = handle_card_action(store, verifier, incident_lookup, current_subject, body)
                message = body.get("message") or {}
                channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")
                original = next((b["text"]["text"] for b in message.get("blocks", [])
                                 if b.get("type") == "section"), message.get("text", ""))
                original = decided_summary(original)
                if channel and message.get("ts") and approvals and all(a.valid for a in approvals):
                    await client.chat_update(channel=channel, ts=message["ts"], text=original,
                                             blocks=decided_blocks(original, outcome))
                elif channel:
                    await client.chat_postEphemeral(channel=channel, user=(body.get("user") or {}).get("id"),
                                                    thread_ts=message.get("thread_ts") or message.get("ts"),
                                                    text=outcome)
            except Exception:
                log.exception("card action failed")

    async def start(self) -> None:
        await self.handler.connect_async()
        log.info("Slack Socket Mode connected: card buttons are live")

    async def stop(self) -> None:
        await self.handler.close_async()
