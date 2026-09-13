"""Continuous synthetic traffic (Poisson arrivals) so 'no errors' is meaningful."""

from __future__ import annotations

import asyncio
import random
from typing import Callable

import httpx

from shoplab.common import CANARY_USERS, USER_POOL

DEFAULT_ROUTES = {
    "checkout:POST:/pay": 0.40,
    "search:GET:/search": 0.25,
    "catalog:GET:/products": 0.25,
    "catalog:POST:/profile/avatar": 0.10,
}
STAGING_ROUTES = {"checkout@staging:POST:/pay": 1.0}
WORDS = ["shoe", "lamp", "product", "desk", "cable", "phone", "mug", "chair", "1", "2"]


class Traffic:
    def __init__(self, url_of: Callable[[str], str | None], rps: float = 30.0, staging_rps: float = 6.0,
                 enabled: bool = True):
        self.url_of = url_of
        self.rps = rps
        self.staging_rps = staging_rps
        self.enabled = enabled
        self.routes = dict(DEFAULT_ROUTES)
        self._sem = asyncio.Semaphore(300)
        self._inflight: set[asyncio.Task] = set()
        self._tasks: list[asyncio.Task] = []
        self._client: httpx.AsyncClient | None = None
        self.sent = 0

    def state(self) -> dict:
        return {"rps": self.rps, "staging_rps": self.staging_rps, "enabled": self.enabled, "routes": self.routes}

    def update(self, rps: float | None = None, enabled: bool | None = None, routes: dict | None = None,
               staging_rps: float | None = None) -> dict:
        if rps is not None:
            self.rps = max(0.0, float(rps))
        if staging_rps is not None:
            self.staging_rps = max(0.0, float(staging_rps))
        if enabled is not None:
            self.enabled = bool(enabled)
        if routes:
            unknown = set(routes) - set(DEFAULT_ROUTES)
            if unknown:
                raise ValueError(f"unknown routes {sorted(unknown)}")
            self.routes = {k: float(v) for k, v in routes.items()}
        return self.state()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=2.0, limits=httpx.Limits(max_connections=300))
        self._tasks = [
            asyncio.create_task(self._loop(lambda: self.rps, lambda: self.routes)),
            asyncio.create_task(self._loop(lambda: self.staging_rps, lambda: STAGING_ROUTES)),
        ]

    async def stop(self) -> None:
        for t in self._tasks + list(self._inflight):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._inflight, return_exceptions=True)
        if self._client:
            await self._client.aclose()

    async def _loop(self, rate_fn, routes_fn) -> None:
        while True:
            rate = rate_fn()
            if not self.enabled or rate <= 0:
                await asyncio.sleep(0.2)
                continue
            await asyncio.sleep(random.expovariate(rate))
            if self._sem.locked():
                continue  # overloaded client side: shed rather than queue unboundedly
            routes = routes_fn()
            key = random.choices(list(routes), weights=list(routes.values()))[0]
            task = asyncio.create_task(self._fire(key))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _fire(self, key: str) -> None:
        service, method, path = key.split(":", 2)
        base = self.url_of(service)
        if not base or self._client is None:
            return
        user = random.choice(CANARY_USERS) if random.random() < 0.03 else random.choice(USER_POOL)
        headers = {"x-user-id": user}
        async with self._sem:
            try:
                if path == "/pay":
                    await self._client.post(f"{base}/pay", json={"amount": random.randint(5, 300), "currency": "USD"},
                                            headers=headers)
                elif path == "/search":
                    await self._client.get(f"{base}/search", params={"q": random.choice(WORDS)}, headers=headers)
                elif path == "/products":
                    await self._client.get(f"{base}/products/{random.randint(1, 520)}", headers=headers)
                elif path == "/profile/avatar":
                    await self._client.post(f"{base}/profile/avatar", content=b"\x89PNG....", headers=headers)
                self.sent += 1
            except (httpx.HTTPError, OSError):
                pass
