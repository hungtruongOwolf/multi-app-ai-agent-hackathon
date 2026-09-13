"""Change log: every change to ShopLab's runtime config (flags, pool size, deploys, restarts), who made it and when.
Incident responders — and the agent — read it to connect "errors started at 10:14" with "payment_v2 was turned on
at 10:13". Kept in memory and appended to a JSONL file in the ShopLab data dir."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

KINDS = ("flag", "pool_size", "deploy", "restart")


def _fmt(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


class ChangeLog:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._items: list[dict] = []
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    self._items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    def record(self, *, service: str, kind: str, actor: str, summary: str, detail: dict | None = None) -> dict:
        item = {"ts": datetime.now(UTC).isoformat(), "service": service, "kind": kind,
                "actor": actor or "operator", "summary": summary, "detail": detail or {}}
        with self._lock:
            self._items.append(item)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(item) + "\n")
        return item

    def list(self, since: str | None = None, service: str | None = None) -> list[dict]:
        cutoff = None
        if since:
            try:
                cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if cutoff.tzinfo is None:
                    cutoff = cutoff.replace(tzinfo=UTC)
            except ValueError:
                cutoff = None
        with self._lock:
            items = list(self._items)
        out = []
        for it in items:
            if service and it["service"] != service:
                continue
            if cutoff and datetime.fromisoformat(it["ts"]) < cutoff:
                continue
            out.append(it)
        return out

    def record_config_diff(self, service: str, before: dict, after: dict, actor: str) -> list[dict]:
        """One change entry per field that differs (flags, pool size, version)."""
        recorded = []
        for flag, new in (after.get("flags") or {}).items():
            old = (before.get("flags") or {}).get(flag)
            if old != new:
                recorded.append(self.record(service=service, kind="flag", actor=actor,
                                            summary=f"{flag}: {_fmt(old)} → {_fmt(new)}",
                                            detail={"flag": flag, "prev": old, "value": new}))
        if before.get("pool_size") != after.get("pool_size"):
            recorded.append(self.record(service=service, kind="pool_size", actor=actor,
                                        summary=f"DB pool size: {before.get('pool_size')} → {after.get('pool_size')}",
                                        detail={"prev": before.get("pool_size"), "value": after.get("pool_size")}))
        if before.get("app_version") != after.get("app_version"):
            recorded.append(self.record(service=service, kind="deploy", actor=actor,
                                        summary=f"deployed {after.get('app_version')} (was {before.get('app_version')})",
                                        detail={"prev": before.get("app_version"), "value": after.get("app_version")}))
        return recorded
