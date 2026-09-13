"""Slack Web API emulation (the subset we use) + a small web UI for humans to approve/reject."""

from __future__ import annotations

import html
import json
import re
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from sandbox.db import DocStore

TOKENS: dict[str, dict[str, Any]] = {
    "xoxb-sandbox": {"user_id": "U_BOT", "name": "incident-judge", "is_bot": True, "bot_id": "B_BOT"},
    "xoxp-oncall1": {"user_id": "U_ONCALL_1", "name": "oncall-1", "is_bot": False},
    "xoxp-intruder": {"user_id": "U_INTRUDER", "name": "intruder", "is_bot": False},
}
USERS = {t["user_id"]: t for t in TOKENS.values()}
TOKEN_BY_USER = {t["user_id"]: tok for tok, t in TOKENS.items()}
NAME_RE = re.compile(r"^[a-z0-9_-]{1,80}$")
APPROVE_HINT_RE = re.compile(r"\b(?:approve|reject)\s+`?([0-9a-f]{8})`?", re.I)


class SlackError(Exception):
    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


class SlackEmulator:
    def __init__(self, db: DocStore):
        self.db = db
        self._ts_lock = threading.Lock()
        self._last_ts = 0.0

    def seed(self) -> None:
        if self.db.get("slack_channel", "C_ONCALL") is None:
            self.db.put("slack_channel", "C_ONCALL", {
                "id": "C_ONCALL", "name": "oncall", "created": int(time.time()), "creator": "U_ONCALL_1",
                "is_archived": False, "members": ["U_BOT", "U_ONCALL_1", "U_INTRUDER"],
            })

    def next_ts(self) -> str:
        with self._ts_lock:
            t = max(time.time(), self._last_ts + 0.000001)
            self._last_ts = t
            return f"{t:.6f}"

    # ------------------------------------------------------------ helpers

    def _channel(self, ref: str | None) -> dict[str, Any]:
        if not ref:
            raise SlackError("channel_not_found")
        ch = self.db.get("slack_channel", ref)
        if ch is None:
            name = ref.lstrip("#")
            ch = next(iter(self.db.all("slack_channel", lambda c: c["name"] == name)), None)
        if ch is None:
            raise SlackError("channel_not_found")
        return ch

    def _channel_json(self, ch: dict[str, Any], user_id: str) -> dict[str, Any]:
        return {"id": ch["id"], "name": ch["name"], "created": ch["created"], "creator": ch["creator"],
                "is_channel": True, "is_archived": ch["is_archived"], "is_member": user_id in ch["members"],
                "num_members": len(ch["members"])}

    def _messages(self, channel_id: str) -> list[dict[str, Any]]:
        msgs = self.db.all("slack_message", lambda m: m["channel"] == channel_id)
        return sorted(msgs, key=lambda m: float(m["ts"]))

    @staticmethod
    def _msg_json(m: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in m.items() if k not in ("channel", "doc_id")}
        return out

    # ------------------------------------------------------------ methods

    def call(self, method: str, who: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
        uid = who["user_id"]
        if method == "auth.test":
            out = {"user_id": uid, "user": who["name"], "team": "ShopLab", "team_id": "T_SHOPLAB",
                   "url": "https://shoplab.slack.com/"}
            if who.get("bot_id"):
                out["bot_id"] = who["bot_id"]
            return out

        if method == "users.info":
            u = USERS.get(p.get("user", ""))
            if not u:
                raise SlackError("user_not_found")
            return {"user": {"id": u["user_id"], "name": u["name"], "is_bot": u["is_bot"]}}

        if method == "conversations.create":
            name = str(p.get("name", ""))
            if not NAME_RE.match(name):
                raise SlackError("invalid_name_specials")
            if self.db.all("slack_channel", lambda c: c["name"] == name):
                raise SlackError("name_taken")
            cid = f"C{self.db.next('slack_channel'):08d}"
            ch = {"id": cid, "name": name, "created": int(time.time()), "creator": uid, "is_archived": False,
                  "members": [uid]}
            self.db.put("slack_channel", cid, ch)
            return {"channel": self._channel_json(ch, uid)}

        if method == "conversations.list":
            exclude_archived = str(p.get("exclude_archived", "false")).lower() == "true"
            chans = [self._channel_json(c, uid) for c in self.db.all("slack_channel")
                     if not (exclude_archived and c["is_archived"])]
            return {"channels": chans, "response_metadata": {"next_cursor": ""}}

        if method == "conversations.info":
            ch = self._channel(p.get("channel"))
            return {"channel": self._channel_json(ch, uid)}

        if method == "conversations.join":
            ch = self._channel(p.get("channel"))
            if ch["is_archived"]:
                raise SlackError("is_archived")
            if uid not in ch["members"]:
                ch["members"].append(uid)
                self.db.put("slack_channel", ch["id"], ch)
            return {"channel": self._channel_json(ch, uid)}

        if method == "conversations.archive":
            ch = self._channel(p.get("channel"))
            if ch["is_archived"]:
                raise SlackError("already_archived")
            if uid not in ch["members"]:
                raise SlackError("not_in_channel")
            ch["is_archived"] = True
            self.db.put("slack_channel", ch["id"], ch)
            return {}

        if method == "chat.postMessage":
            ch = self._channel(p.get("channel"))
            if ch["is_archived"]:
                raise SlackError("is_archived")
            if uid not in ch["members"]:
                raise SlackError("not_in_channel")
            text = str(p.get("text") or "")
            blocks = p.get("blocks")
            if isinstance(blocks, str) and blocks:
                try:
                    blocks = json.loads(blocks)
                except json.JSONDecodeError:
                    raise SlackError("invalid_blocks")
            if not text and not blocks:
                raise SlackError("no_text")
            thread_ts = p.get("thread_ts") or None
            if thread_ts:
                parent = self.db.get("slack_message", f"{ch['id']}:{thread_ts}")
                if parent is None:
                    raise SlackError("thread_not_found")
                if parent.get("thread_ts") and parent["thread_ts"] != parent["ts"]:
                    thread_ts = parent["thread_ts"]
            ts = self.next_ts()
            msg: dict[str, Any] = {"type": "message", "user": uid, "text": text, "ts": ts, "channel": ch["id"]}
            if who.get("bot_id"):
                msg["bot_id"] = who["bot_id"]
            if blocks:
                msg["blocks"] = blocks
            metadata = p.get("metadata")
            if metadata:
                msg["metadata"] = json.loads(metadata) if isinstance(metadata, str) else metadata
            if thread_ts:
                msg["thread_ts"] = thread_ts
                parent = self.db.get("slack_message", f"{ch['id']}:{thread_ts}")
                parent["thread_ts"] = thread_ts
                parent["reply_count"] = parent.get("reply_count", 0) + 1
                self.db.put("slack_message", f"{ch['id']}:{thread_ts}", parent)
            self.db.put("slack_message", f"{ch['id']}:{ts}", msg)
            return {"channel": ch["id"], "ts": ts, "message": self._msg_json(msg)}

        if method == "chat.update":
            ch = self._channel(p.get("channel"))
            key = f"{ch['id']}:{p.get('ts')}"
            msg = self.db.get("slack_message", key)
            if msg is None:
                raise SlackError("message_not_found")
            if msg.get("user") != uid:
                raise SlackError("cant_update_message")
            if p.get("text") is not None:
                msg["text"] = str(p.get("text"))
            blocks = p.get("blocks")
            if isinstance(blocks, str) and blocks:
                blocks = json.loads(blocks)
            if blocks:
                msg["blocks"] = blocks
            msg["edited"] = {"user": uid, "ts": self.next_ts()}
            self.db.put("slack_message", key, msg)
            return {"channel": ch["id"], "ts": msg["ts"], "text": msg["text"]}

        if method == "chat.delete":
            ch = self._channel(p.get("channel"))
            key = f"{ch['id']}:{p.get('ts')}"
            msg = self.db.get("slack_message", key)
            if msg is None:
                raise SlackError("message_not_found")
            if msg.get("user") != uid:  # bots and users may only delete their own messages
                raise SlackError("cant_delete_message")
            self.db.delete("slack_message", key)
            parent_ts = msg.get("thread_ts")
            if parent_ts and parent_ts != msg["ts"]:
                parent = self.db.get("slack_message", f"{ch['id']}:{parent_ts}")
                if parent is not None:
                    parent["reply_count"] = max(0, parent.get("reply_count", 1) - 1)
                    self.db.put("slack_message", f"{ch['id']}:{parent_ts}", parent)
            return {"channel": ch["id"], "ts": msg["ts"]}

        if method in ("conversations.history", "conversations.replies"):
            ch = self._channel(p.get("channel"))
            if uid not in ch["members"]:
                raise SlackError("not_in_channel")
            oldest = float(p["oldest"]) if p.get("oldest") else None
            latest = float(p["latest"]) if p.get("latest") else None
            inclusive = str(p.get("inclusive", "false")).lower() in ("true", "1")
            limit = int(p.get("limit") or 100)

            def in_range(m: dict[str, Any]) -> bool:
                t = float(m["ts"])
                if oldest is not None and (t < oldest or (t == oldest and not inclusive)):
                    return False
                if latest is not None and (t > latest or (t == latest and not inclusive)):
                    return False
                return True

            msgs = self._messages(ch["id"])
            if method == "conversations.history":
                top = [m for m in msgs if not m.get("thread_ts") or m["thread_ts"] == m["ts"]]
                sel = [self._msg_json(m) for m in reversed(top) if in_range(m)][:limit]
                return {"messages": sel, "has_more": False, "response_metadata": {"next_cursor": ""}}
            ts = p.get("ts")
            parent = self.db.get("slack_message", f"{ch['id']}:{ts}") if ts else None
            if parent is None:
                raise SlackError("thread_not_found")
            root = parent.get("thread_ts") or parent["ts"]
            root_msg = self.db.get("slack_message", f"{ch['id']}:{root}")
            replies = [m for m in msgs if m.get("thread_ts") == root and m["ts"] != root and in_range(m)]
            sel = [self._msg_json(root_msg)] + [self._msg_json(m) for m in replies]
            return {"messages": sel[:limit], "has_more": False, "response_metadata": {"next_cursor": ""}}

        raise SlackError("unknown_method")


def build_router(emu: SlackEmulator) -> APIRouter:
    r = APIRouter()

    async def params(request: Request) -> dict[str, Any]:
        p: dict[str, Any] = dict(request.query_params)
        ctype = request.headers.get("content-type", "")
        if request.method == "POST":
            if "application/json" in ctype:
                body = await request.body()
                if body:
                    p.update(json.loads(body))
            else:
                form = await request.form()
                p.update({k: v for k, v in form.items()})
        return p

    @r.api_route("/slack/api/{method}", methods=["GET", "POST"])
    async def api(method: str, request: Request):
        p = await params(request)
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else str(p.pop("token", ""))
        p.pop("token", None)
        if not token:
            return {"ok": False, "error": "not_authed"}
        who = TOKENS.get(token)
        if who is None:
            return {"ok": False, "error": "invalid_auth"}
        try:
            return {"ok": True, **emu.call(method, who, p)}
        except SlackError as e:
            return {"ok": False, "error": e.error}

    # ------------------------------------------------------------ web UI

    @r.get("/slack/ui", response_class=HTMLResponse)
    def ui_index():
        chans = emu.db.all("slack_channel")
        items = "".join(
            f"<li><a href='/slack/ui/channel/{c['id']}'>#{html.escape(c['name'])}</a>"
            f"{' (archived)' if c['is_archived'] else ''}</li>" for c in chans)
        return f"<!doctype html><meta charset=utf-8><title>Sandbox Slack</title><body style='font-family:sans-serif;padding:16px'><h1>Sandbox Slack</h1><ul>{items}</ul>"

    @r.get("/slack/ui/channel/{channel_id}", response_class=HTMLResponse)
    def ui_channel(channel_id: str):
        ch = emu.db.get("slack_channel", channel_id)
        if ch is None:
            return HTMLResponse("channel not found", status_code=404)
        msgs = emu._messages(channel_id)
        tops = [m for m in msgs if not m.get("thread_ts") or m["thread_ts"] == m["ts"]]
        parts = []
        for m in reversed(tops):
            thread = [x for x in msgs if x.get("thread_ts") == m["ts"] and x["ts"] != m["ts"]]
            parts.append(_render_msg(channel_id, m, thread))
        return (f"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3>"
                f"<title>#{html.escape(ch['name'])}</title>"
                f"<body style='font-family:sans-serif;max-width:900px;margin:auto;padding:16px'>"
                f"<p><a href='/slack/ui'>&larr; channels</a></p><h1>#{html.escape(ch['name'])}</h1>{''.join(parts)}")

    @r.post("/slack/ui/act")
    async def ui_act(request: Request):
        form = await request.form()
        user = str(form.get("as_user", "U_ONCALL_1"))
        verb = "approve" if form.get("action") == "approve" else "reject"
        token = TOKEN_BY_USER.get(user)
        channel = str(form.get("channel"))
        if token:
            ch = emu.db.get("slack_channel", channel)
            who = TOKENS[token]
            if ch and user not in ch["members"] and not ch["is_archived"]:
                emu.call("conversations.join", who, {"channel": channel})
            try:
                emu.call("chat.postMessage", who, {"channel": channel, "thread_ts": form.get("thread_ts"),
                                                   "text": f"{verb} {form.get('hash')}"})
            except SlackError:
                pass
        return RedirectResponse(f"/slack/ui/channel/{channel}", status_code=303)

    return r


def _render_msg(channel_id: str, m: dict[str, Any], thread: list[dict[str, Any]]) -> str:
    def one(x: dict[str, Any]) -> str:
        who = USERS.get(x["user"], {}).get("name", x["user"])
        text = html.escape(x.get("text", ""))
        buttons = ""
        hints = set(APPROVE_HINT_RE.findall(x.get("text", ""))) if x.get("bot_id") else set()
        thread_ts = x.get("thread_ts") or x["ts"]
        for h in sorted(hints):
            for action, user, label in (("approve", "U_ONCALL_1", "Approve as oncall-1"),
                                        ("reject", "U_ONCALL_1", "Reject as oncall-1"),
                                        ("approve", "U_INTRUDER", "Approve as intruder")):
                buttons += (f"<form method=post action='/slack/ui/act' style='display:inline'>"
                            f"<input type=hidden name=channel value='{channel_id}'>"
                            f"<input type=hidden name=thread_ts value='{thread_ts}'>"
                            f"<input type=hidden name=hash value='{h}'><input type=hidden name=action value='{action}'>"
                            f"<input type=hidden name=as_user value='{user}'><button>{label} ({h})</button></form> ")
        return (f"<div style='border-left:3px solid #ccc;padding:4px 8px;margin:6px 0'>"
                f"<b>{html.escape(who)}</b> <small>{x['ts']}</small><pre style='white-space:pre-wrap'>{text}</pre>"
                f"{buttons}</div>")

    replies = "".join(one(t) for t in thread)
    return f"<section style='margin:12px 0'>{one(m)}<div style='margin-left:24px'>{replies}</div></section>"
