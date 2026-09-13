"""Simulated humans. They act ONLY through the Slack Web API (sandbox user tokens), exactly like a
person would: read the channels, find the bot's cards for this trial, reply in thread.

Cards are recognized by tags the agent puts in card text ([IJ-FIX], [IJ-PUBLIC], [IJ-VETO],
[IJ-MEMORY]) plus an `approve <hash8>` hint; see graders.common.card_kind."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from evals.graders.common import HASH_HINT, TRIAL_MARK, card_items, card_kind, is_bot_message
from evals.scenario import HumanRule

log = logging.getLogger("evals.sim_human")

TOKENS = {"oncall_1": "xoxp-oncall1", "intruder": "xoxp-intruder"}
SHARED_CHANNEL = "C_ONCALL"
WHEN_TO_KIND = {"fix_approval_requested": "fix", "public_post_requested": "public_post", "veto_window": "veto",
                "memory_merge_requested": "memory_merge"}


@dataclass
class Card:
    kind: str
    hash8: str
    channel: str
    ts: str
    thread_ts: str
    seen_at: float


@dataclass
class _RuleState:
    fired: int = 0
    handled: set[str] = field(default_factory=set)


class SlackAPI:
    def __init__(self, base: str, http: httpx.AsyncClient):
        self.base = base.rstrip("/")
        self.http = http

    async def call(self, token: str, method: str, **params) -> dict:
        r = await self.http.post(f"{self.base}/{method}", json=params, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()


class SimHuman:
    def __init__(self, rules: list[HumanRule], trial_id: str, sandbox_url: str, t0: float | None = None,
                 poll_s: float = 1.0):
        self.rules = rules
        self.trial_id = trial_id
        self.slack_base = f"{sandbox_url.rstrip('/')}/slack/api"
        self.t0 = t0 or time.monotonic()
        self.poll_s = poll_s
        self.actions: list[dict] = []
        self.cards: list[Card] = []
        self._card_keys: set[tuple[str, str]] = set()
        self._state = [_RuleState() for _ in rules]
        self._joined: dict[str, set[str]] = {k: set() for k in TOKENS}
        self._trial_channels: set[str] = set()
        self._trial_threads: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ reading

    async def _join(self, api: SlackAPI, actor: str, channel: str) -> None:
        if channel not in self._joined[actor]:
            await api.call(TOKENS[actor], "conversations.join", channel=channel)
            self._joined[actor].add(channel)

    def _is_trial(self, text: str, channel: str, thread: str) -> bool:
        marks = TRIAL_MARK.findall(text)
        if marks:
            return self.trial_id in marks
        return channel in self._trial_channels or (channel, thread) in self._trial_threads

    async def scan(self, api: SlackAPI) -> None:
        reader = TOKENS["oncall_1"]
        listing = await api.call(reader, "conversations.list", types="public_channel", exclude_archived=False)
        for ch in listing.get("channels", []):
            cid = ch.get("id")
            await self._join(api, "oncall_1", cid)
            hist = await api.call(reader, "conversations.history", channel=cid, limit=200)
            for m in hist.get("messages", []):
                msgs = [m]
                if m.get("reply_count") or m.get("thread_ts") == m.get("ts") or is_bot_message(m):
                    rep = await api.call(reader, "conversations.replies", channel=cid, ts=m.get("ts"))
                    if rep.get("ok"):
                        msgs = rep.get("messages", []) or [m]
                for x in msgs:
                    self._consider(cid, x, parent_ts=m.get("ts"))

    def _consider(self, channel: str, m: dict, parent_ts: str) -> None:
        ref = ((m.get("metadata") or {}).get("event_payload") or {}).get("ref") or ""
        text = f"{m.get('text', '')}\n{ref}"
        thread = m.get("thread_ts") or parent_ts or m.get("ts")
        if self.trial_id in TRIAL_MARK.findall(text):
            self._trial_threads.add((channel, thread))
            if channel != SHARED_CHANNEL:  # per-incident war rooms belong to one trial
                self._trial_channels.add(channel)
        if not is_bot_message(m) or not self._is_trial(text, channel, thread):
            return
        for kind, hash8 in card_items(text):
            key = (kind, hash8)
            if key not in self._card_keys:
                self._card_keys.add(key)
                self.cards.append(Card(kind, hash8, channel, m.get("ts"), thread, time.monotonic()))

    # ------------------------------------------------------------ acting

    def _first_trial_thread(self) -> tuple[str, str] | None:
        return sorted(self._trial_threads, key=lambda x: x[1])[0] if self._trial_threads else None

    async def _post(self, api: SlackAPI, rule_idx: int, actor: str, channel: str, thread_ts: str, text: str) -> None:
        await self._join(api, actor, channel)
        res = await api.call(TOKENS[actor], "chat.postMessage", channel=channel, text=text, thread_ts=thread_ts)
        self.actions.append({"t": round(time.monotonic() - self.t0, 2), "rule": rule_idx, "actor": actor,
                             "channel": channel, "thread_ts": thread_ts, "text": text, "ok": res.get("ok")})

    async def act(self, api: SlackAPI) -> None:
        elapsed = time.monotonic() - self.t0
        for idx, (rule, st) in enumerate(zip(self.rules, self._state)):
            if st.fired >= rule.times:
                continue
            if rule.when == "at_time":
                target = self._first_trial_thread()
                if elapsed >= (rule.at_s or 0) and target:
                    st.fired += 1
                    await self._post(api, idx, rule.actor, target[0], target[1], rule.text or "")
                continue
            kind = WHEN_TO_KIND[rule.when]
            cards = [c for c in self.cards if c.kind == kind]
            if not cards:
                continue
            if rule.hash_mode == "first_seen":
                distinct = list(dict.fromkeys(c.hash8 for c in cards))
                if len(distinct) < 2:
                    continue  # stale approval only makes sense once the plan changed
                card = cards[-1]
                h = distinct[0]
            else:
                card = next((c for c in cards if c.hash8 not in st.handled), None)
                if card is None:
                    continue
                h = card.hash8 if rule.hash_mode == "current" else ("deadbeef" if card.hash8 != "deadbeef" else "cafebabe")
            if time.monotonic() - card.seen_at < rule.delay_s:
                continue
            st.handled.add(card.hash8)
            st.fired += 1
            text = rule.text if rule.reply == "text" else f"{rule.reply} {h}"
            await self._post(api, idx, rule.actor, card.channel, card.thread_ts, text)

    async def run(self, stop: asyncio.Event) -> None:
        if not self.rules:
            await stop.wait()
            return
        async with httpx.AsyncClient(timeout=10) as http:
            api = SlackAPI(self.slack_base, http)
            while not stop.is_set():
                try:
                    await self.scan(api)
                    await self.act(api)
                except (httpx.HTTPError, ValueError) as e:
                    log.debug("sim_human poll error: %r", e)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
                except asyncio.TimeoutError:
                    pass
