"""DirectScrapeBackend: scrapes ShopLab /metrics endpoints and computes named metrics
without a Prometheus server. Implements judge.signals.metrics.MetricsBackend."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import httpx
from prometheus_client.parser import text_string_to_metric_families

log = logging.getLogger("judge.scrape")

SeriesKey = tuple[str, tuple[tuple[str, str], ...]]  # (sample name, sorted labels)


@dataclass
class Snapshot:
    t: float
    values: dict[SeriesKey, float]


def parse_exposition(text: str) -> dict[SeriesKey, float]:
    out: dict[SeriesKey, float] = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name.endswith("_created"):
                continue
            out[(s.name, tuple(sorted(s.labels.items())))] = float(s.value)
    return out


def histogram_quantile(q: float, buckets: dict[float, float]) -> float | None:
    """Prometheus-style quantile from cumulative bucket counts {le: count}. None if no observations."""
    if not buckets:
        return None
    items = sorted(buckets.items())
    total = items[-1][1] if math.isinf(items[-1][0]) else max(c for _, c in items)
    if total <= 0:
        return None
    rank = q * total
    prev_le, prev_count = 0.0, 0.0
    for le, count in items:
        if count >= rank:
            if math.isinf(le):
                return prev_le  # highest finite bucket bound
            if count == prev_count:
                return le
            return prev_le + (le - prev_le) * (rank - prev_count) / (count - prev_count)
        prev_le, prev_count = le, count
    return prev_le


class DirectScrapeBackend:
    def __init__(self, service_urls: dict[str, str], interval_s: float = 2.0, stale_after_s: float = 10.0,
                 retention_s: float = 900.0, clock: Callable[[], float] = time.monotonic):
        self.service_urls = dict(service_urls)
        self.interval_s = interval_s
        self.stale_after_s = stale_after_s
        self.retention_s = retention_s
        self.clock = clock
        self._snaps: dict[str, deque[Snapshot]] = {s: deque() for s in service_urls}
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ lifecycle

    @classmethod
    async def from_supervisor(cls, supervisor_url: str, **kw) -> "DirectScrapeBackend":
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{supervisor_url.rstrip('/')}/services")
            r.raise_for_status()
            urls = {s["name"]: s["url"] for s in r.json()}
        return cls(urls, **kw)

    async def start(self) -> None:
        if self._task is None:
            self._client = httpx.AsyncClient(timeout=min(2.0, self.interval_s * 2))
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        while True:
            try:
                await self.scrape_once()
            except Exception:  # noqa: BLE001
                log.exception("scrape cycle failed")
            await asyncio.sleep(self.interval_s)

    async def scrape_once(self) -> None:
        client = self._client or httpx.AsyncClient(timeout=2.0)
        try:
            results = await asyncio.gather(
                *(client.get(f"{url.rstrip('/')}/metrics") for url in self.service_urls.values()),
                return_exceptions=True,
            )
            for service, r in zip(self.service_urls, results):
                if isinstance(r, BaseException) or r.status_code != 200:
                    continue
                try:
                    self.ingest(service, r.text)
                except Exception:  # noqa: BLE001
                    log.warning("could not parse metrics for %s", service)
        finally:
            if client is not self._client:
                await client.aclose()

    def ingest(self, service: str, text: str, t: float | None = None) -> None:
        t = self.clock() if t is None else t
        dq = self._snaps.setdefault(service, deque())
        dq.append(Snapshot(t, parse_exposition(text)))
        while dq and dq[0].t < t - self.retention_s:
            dq.popleft()

    # ------------------------------------------------------------ queries

    def available(self) -> bool:
        now = self.clock()
        return any(dq and now - dq[-1].t <= self.stale_after_s for dq in self._snaps.values())

    def _window(self, service: str, window_s: float) -> list[Snapshot]:
        dq = self._snaps.get(service)
        if not dq:
            return []
        now = self.clock()
        if now - dq[-1].t > self.stale_after_s:
            return []
        start = now - window_s
        snaps = list(dq)
        # baseline: the last snapshot at or before the window start, if any
        idx = 0
        for i, s in enumerate(snaps):
            if s.t <= start:
                idx = i
            else:
                break
        return snaps[idx:]

    @staticmethod
    def _match(labels: tuple[tuple[str, str], ...], want: dict[str, str | Callable[[str], bool]]) -> bool:
        d = dict(labels)
        for k, v in want.items():
            if k not in d:
                return False
            if callable(v):
                if not v(d[k]):
                    return False
            elif d[k] != v:
                return False
        return True

    def _counter_increase(self, snaps: list[Snapshot], name: str, want: dict) -> dict[tuple, float]:
        """Per-series increase across snapshots, reset-aware. Missing series count as 0."""
        keys = {k for s in snaps for k in s.values if k[0] == name and self._match(k[1], want)}
        out: dict[tuple, float] = {}
        for key in keys:
            inc, prev = 0.0, None
            for s in snaps:
                v = s.values.get(key, 0.0)
                if prev is not None:
                    inc += v - prev if v >= prev else v  # counter reset (process restart)
                prev = v
            out[key[1]] = inc
        return out

    def _requests(self, service: str, window_s: float, route: str | None, status_pred=None):
        snaps = self._window(service, window_s)
        if len(snaps) < 2:
            return None, None
        want: dict = {}
        if route:
            want["route"] = route
        if status_pred:
            want["status"] = status_pred
        incs = self._counter_increase(snaps, "http_requests_total", want)
        return sum(incs.values()), snaps[-1].t - snaps[0].t

    def _hist_quantile(self, service: str, window_s: float, name: str, q: float, route: str | None) -> float | None:
        snaps = self._window(service, window_s)
        if len(snaps) < 2:
            return None
        want = {"route": route} if route else {}
        incs = self._counter_increase(snaps, f"{name}_bucket", want)
        buckets: dict[float, float] = {}
        for labels, inc in incs.items():
            le = float(dict(labels)["le"])
            buckets[le] = buckets.get(le, 0.0) + inc
        return histogram_quantile(q, buckets)

    def _gauge_latest(self, service: str, name: str) -> float | None:
        snaps = self._window(service, 0)
        if not snaps:
            return None
        vals = [v for k, v in snaps[-1].values.items() if k[0] == name]
        return sum(vals) if vals else None

    def value(self, metric: str, service: str, window_s: int, route: str | None = None) -> float | None:
        if metric == "error_rate":
            total, _ = self._requests(service, window_s, route)
            if not total:
                return None
            errors, _ = self._requests(service, window_s, route, status_pred=lambda s: s.startswith("5"))
            return (errors or 0.0) / total
        if metric == "rps":
            total, dur = self._requests(service, window_s, route)
            if total is None or not dur:
                return None
            return total / dur
        if metric == "latency_p95":
            return self._hist_quantile(service, window_s, "http_request_duration_seconds", 0.95, route)
        if metric == "pool_wait_p95":
            return self._hist_quantile(service, window_s, "db_pool_wait_seconds", 0.95, None)
        if metric == "db_query_p95":
            return self._hist_quantile(service, window_s, "db_query_duration_seconds", 0.95, None)
        if metric == "pool_utilization":
            snaps = self._window(service, window_s)
            ratios = []
            for s in snaps:
                in_use = sum(v for k, v in s.values.items() if k[0] == "db_pool_in_use")
                size = sum(v for k, v in s.values.items() if k[0] == "db_pool_size")
                if size > 0:
                    ratios.append(in_use / size)
            return sum(ratios) / len(ratios) if ratios else None
        if metric == "memory_mb":
            v = self._gauge_latest(service, "process_resident_memory_bytes")
            return v / (1024 * 1024) if v is not None else None
        raise ValueError(f"unknown metric {metric!r}")
