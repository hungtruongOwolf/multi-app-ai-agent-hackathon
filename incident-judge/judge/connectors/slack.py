"""Slack Web API (real: https://slack.com/api/<method>, auth: Bearer <token>).

All calls are form-encoded POSTs (accepted by every Web API method; JSON bodies are not accepted by
read methods). Slack reports errors as HTTP 200 with ok=false."""

from __future__ import annotations

import json
from typing import Any

from judge.connectors.transport import ConnectorError, HttpClient, check
from judge.settings import Settings

APP = "slack"


class SlackClient:
    def __init__(self, settings: Settings, http: HttpClient, token: str | None = None):
        self.s = settings
        self.http = http
        self.token = token or settings.slack_bot_token

    async def _call(self, op: str, method: str, **params: Any) -> dict[str, Any]:
        data = {k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v).lower() if isinstance(v, bool) else v)
                for k, v in params.items() if v is not None}
        resp = await self.http.request(APP, op, "POST", f"{self.s.slack_base}/{method}", data=data,
                                       headers={"Authorization": f"Bearer {self.token}"})
        body = check(APP, op, resp)
        if not isinstance(body, dict) or not body.get("ok"):
            raise ConnectorError(APP, op, resp.status_code, body)
        return body

    async def auth_info(self) -> dict[str, Any]:
        """Full auth.test body: user_id, user, team, team_id and bot_id (for bot tokens)."""
        return await self._call("auth_info", "auth.test")

    async def update_message(self, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
        await self._call("update_message", "chat.update", channel=channel, ts=ts, text=text, blocks=blocks)

    async def delete_message(self, channel: str, ts: str) -> None:
        await self._call("delete_message", "chat.delete", channel=channel, ts=ts)

    async def auth_user(self) -> str:
        return (await self._call("auth_user", "auth.test"))["user_id"]

    async def post(self, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None,
                   ref: str | None = None) -> str:
        """`ref` (the dedupe marker) travels as Slack message metadata: invisible to people, readable by the agent."""
        metadata = {"event_type": "incident_judge", "event_payload": {"ref": ref}} if ref else None
        body = await self._call("post", "chat.postMessage", channel=channel, text=text, thread_ts=thread_ts,
                                blocks=blocks, metadata=metadata, unfurl_links=False, unfurl_media=False)
        return body["ts"]

    async def history(self, channel: str, oldest: str | None = None, limit: int = 200) -> list[dict]:
        body = await self._call("history", "conversations.history", channel=channel, oldest=oldest, limit=limit,
                                include_all_metadata=True)
        return body.get("messages", [])

    async def replies(self, channel: str, thread_ts: str, oldest: str | None = None) -> list[dict]:
        body = await self._call("replies", "conversations.replies", channel=channel, ts=thread_ts, oldest=oldest,
                                limit=200, include_all_metadata=True)
        return body.get("messages", [])

    async def find_message_by_marker(self, channel: str, marker: str, thread_ts: str | None = None) -> str | None:
        msgs = await (self.replies(channel, thread_ts) if thread_ts else self.history(channel))
        for m in msgs:
            ref = ((m.get("metadata") or {}).get("event_payload") or {}).get("ref") or ""
            if marker == ref or marker in (m.get("text") or ""):
                return m["ts"]
        return None

    async def find_channel(self, name: str) -> str | None:
        cursor: str | None = None
        while True:
            body = await self._call("find_channel", "conversations.list", exclude_archived=False, limit=1000,
                                    types="public_channel", cursor=cursor)
            for ch in body.get("channels", []):
                if ch.get("name") == name:
                    return ch["id"]
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                return None

    async def create_channel(self, name: str) -> str:
        try:
            body = await self._call("create_channel", "conversations.create", name=name)
            return body["channel"]["id"]
        except ConnectorError as e:
            if not (isinstance(e.body, dict) and e.body.get("error") == "name_taken"):
                raise
        channel_id = await self.find_channel(name)
        if channel_id is None:
            raise ConnectorError(APP, "create_channel", None, {"error": "name_taken_but_not_found", "name": name})
        try:
            await self._call("join_channel", "conversations.join", channel=channel_id)
        except ConnectorError as e:  # archived channels cannot be joined; caller decides
            if not (isinstance(e.body, dict) and e.body.get("error") in ("already_in_channel", "is_archived")):
                raise
        return channel_id

    async def join(self, channel: str) -> None:
        await self._call("join_channel", "conversations.join", channel=channel)

    async def invite(self, channel: str, user_ids: list[str]) -> None:
        """Best effort: 'already_in_channel' and 'cant_invite_self' are not failures."""
        if not user_ids:
            return
        try:
            await self._call("invite", "conversations.invite", channel=channel, users=",".join(user_ids))
        except ConnectorError as e:
            if not (isinstance(e.body, dict) and e.body.get("error") in ("already_in_channel", "cant_invite_self")):
                raise

    async def archive(self, channel: str) -> None:
        try:
            await self._call("archive", "conversations.archive", channel=channel)
        except ConnectorError as e:
            if not (isinstance(e.body, dict) and e.body.get("error") == "already_archived"):
                raise

    async def user_is_bot(self, user_id: str) -> bool:
        body = await self._call("users_info", "users.info", user=user_id)
        return bool(body["user"].get("is_bot"))
