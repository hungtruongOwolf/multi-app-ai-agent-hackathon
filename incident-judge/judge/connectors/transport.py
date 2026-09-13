"""HTTP transport shared by all connectors, with scenario-driven tool fault injection.

Fault plan file (path in env IJ_TOOL_FAULTS), a JSON list of entries:
  {"app": "linear", "op": "create_issue", "mode": "error",       "count": 1, "status": 500}
  {"app": "linear", "op": "create_issue", "mode": "ghost_write", "count": 1}
  {"app": "slack",  "op": "*",            "mode": "latency",     "count": -1, "ms": 800}
mode error       -> the request is NOT sent; a synthetic HTTP `status` response is returned
mode ghost_write -> the request IS sent (the write lands), then httpx.ReadTimeout is raised
mode latency     -> sleep `ms` then send normally
count: number of matching calls affected (default 1); -1 = every call. `after`: skip the first N matches.
Counters are per process."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


class ConnectorError(Exception):
    def __init__(self, app: str, op: str, status: int | None, body: Any):
        super().__init__(f"{app}.{op} failed (status={status}): {str(body)[:300]}")
        self.app = app
        self.op = op
        self.status = status
        self.body = body

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status >= 500 or self.status == 429


@dataclass
class FaultEntry:
    app: str
    op: str
    mode: str
    count: int = 1
    after: int = 0
    status: int = 500
    ms: int = 0
    seen: int = 0
    applied: int = 0

    def matches(self, app: str, op: str) -> bool:
        return self.app in ("*", app) and self.op in ("*", op)


@dataclass
class ToolFaultPlan:
    entries: list[FaultEntry] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_list(cls, raw: list[dict[str, Any]]) -> "ToolFaultPlan":
        allowed = {"app", "op", "mode", "count", "after", "status", "ms"}
        entries = []
        for e in raw:
            if e.get("mode") not in ("error", "ghost_write", "latency"):
                raise ValueError(f"unknown fault mode: {e.get('mode')}")
            entries.append(FaultEntry(**{k: v for k, v in e.items() if k in allowed}))
        return cls(entries=entries)

    @classmethod
    def load(cls, path: Path | str) -> "ToolFaultPlan":
        return cls.from_list(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_env(cls) -> "ToolFaultPlan | None":
        path = os.environ.get("IJ_TOOL_FAULTS")
        if not path:
            return None
        return cls.load(path)

    def take(self, app: str, op: str) -> FaultEntry | None:
        with self._lock:
            for e in self.entries:
                if not e.matches(app, op):
                    continue
                e.seen += 1
                if e.seen <= e.after:
                    continue
                if e.count != -1 and e.applied >= e.count:
                    continue
                e.applied += 1
                return e
        return None


class HttpClient:
    def __init__(self, fault_plan: ToolFaultPlan | None = None, timeout: float = 10.0,
                 client: httpx.AsyncClient | None = None):
        self.fault_plan = fault_plan
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.calls: list[tuple[str, str, str, str]] = []  # (app, op, method, url) for debugging/tests

    @classmethod
    def from_env(cls, **kw: Any) -> "HttpClient":
        return cls(fault_plan=ToolFaultPlan.from_env(), **kw)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def request(self, app: str, op: str, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append((app, op, method, url))
        fault = self.fault_plan.take(app, op) if self.fault_plan else None
        if fault and fault.mode == "error":
            req = self._client.build_request(method, url, **kw)
            return httpx.Response(fault.status, json={"error": "injected fault", "app": app, "op": op}, request=req)
        if fault and fault.mode == "latency":
            await asyncio.sleep(fault.ms / 1000)
        try:
            resp = await self._client.request(method, url, **kw)
        except httpx.HTTPError:
            raise
        if fault and fault.mode == "ghost_write":
            raise httpx.ReadTimeout(f"injected ghost write on {app}.{op} (request was delivered)",
                                    request=resp.request)
        return resp


def check(app: str, op: str, resp: httpx.Response) -> Any:
    """Raise ConnectorError on non-2xx; return parsed JSON (or None for empty bodies)."""
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = resp.text
    if resp.status_code >= 400:
        raise ConnectorError(app, op, resp.status_code, body)
    return body
