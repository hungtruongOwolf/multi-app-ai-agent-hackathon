"""Text-command approvals in a Slack thread: `approve 1a2b3c4d` / `reject 1a2b3c4d`.

Same path in sandbox and real Slack; Socket Mode buttons (slack_socket.py) feed the same verifier."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from judge.approvals.verifier import ApprovalVerifier
from judge.core.models import Approval, Incident
from judge.core.store import Store

if TYPE_CHECKING:
    from judge.connectors.slack import SlackClient

log = logging.getLogger("judge.approvals")

COMMAND = re.compile(r"^\s*(?:<@[A-Z0-9_]+>\s*)?(approve|reject)\s+([0-9a-f]{8})\b", re.IGNORECASE)


def parse_command(text: str) -> tuple[Literal["approve", "reject"], str] | None:
    m = COMMAND.match(text or "")
    if not m:
        return None
    return m.group(1).lower(), m.group(2).lower()  # type: ignore[return-value]


def ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), UTC)


class ApprovalPoller:
    def __init__(self, store: Store, slack: "SlackClient", verifier: ApprovalVerifier):
        self.store = store
        self.slack = slack
        self.verifier = verifier
        self._bot_user: str | None = None

    async def _bot(self) -> str | None:
        if self._bot_user is None:
            try:
                self._bot_user = await self.slack.auth_user()
            except Exception:
                self._bot_user = None
        return self._bot_user

    async def poll(self, incident: Incident, kind, subject_hash: str, requested_at_ts: str,
                   channel: str | None = None, thread_ts: str | None = None) -> list[Approval]:
        """Read thread replies after the request; store every command attempt (valid or not); return new ones."""
        channel = channel or incident.slack_channel_id
        thread_ts = thread_ts or incident.slack_thread_ts or requested_at_ts
        if not channel:
            return []
        messages = await self.slack.replies(channel, thread_ts, oldest=requested_at_ts)
        bot_user = await self._bot()
        requested_at = ts_to_datetime(requested_at_ts)
        new: list[Approval] = []
        for msg in messages:
            ts = msg.get("ts")
            if not ts or float(ts) <= float(requested_at_ts) or ts == thread_ts:
                continue
            seen_key = f"approval_seen:{incident.id}:{kind}:{ts}"
            if self.store.get_kv(seen_key):
                continue
            cmd = parse_command(msg.get("text", ""))
            if cmd is None:
                continue
            verdict, hash8 = cmd
            user = msg.get("user") or ""
            is_bot = bool(msg.get("bot_id")) or msg.get("subtype") == "bot_message" or (
                bot_user is not None and user == bot_user)
            approval = self.verifier.verify(incident=incident, kind=kind, subject_hash=subject_hash, user_id=user,
                                            is_bot=is_bot, verdict=verdict, via="text", requested_at=requested_at,
                                            provided_hash=hash8, ts=ts_to_datetime(ts))
            self.store.add_approval(approval)
            self.store.put_kv(seen_key, approval.approval_id)
            log.info("approval attempt %s by %s valid=%s (%s)", verdict, user, approval.valid, approval.reason)
            new.append(approval)
        return new
