"""A real bounded async connection pool whose size can change at runtime."""

from __future__ import annotations

import asyncio
import time


class PoolTimeout(Exception):
    pass


class Pool:
    def __init__(self, size: int, on_wait=None, on_change=None, critical_prefix=lambda: False):
        self.size = size
        self.in_use = 0
        self._cond = asyncio.Condition()
        self._on_wait = on_wait or (lambda s: None)
        self._on_change = on_change or (lambda in_use, size: None)
        self._critical_prefix = critical_prefix

    async def acquire(self, timeout: float) -> None:
        t0 = time.perf_counter()
        async with self._cond:
            try:
                await asyncio.wait_for(self._cond.wait_for(lambda: self.in_use < self.size), timeout)
            except TimeoutError:
                waited = time.perf_counter() - t0
                self._on_wait(waited)
                msg = f"connection pool exhausted: waited {waited:.2f}s (size={self.size}, in_use={self.in_use})"
                if self._critical_prefix():
                    msg = f"CRITICAL: database connection pool exhausted (waited {waited:.2f}s, size={self.size})"
                raise PoolTimeout(msg) from None
            self.in_use += 1
            self._on_change(self.in_use, self.size)
        self._on_wait(time.perf_counter() - t0)

    async def release(self) -> None:
        async with self._cond:
            self.in_use = max(0, self.in_use - 1)
            self._on_change(self.in_use, self.size)
            self._cond.notify_all()

    async def resize(self, size: int) -> None:
        async with self._cond:
            if size != self.size:
                self.size = size
                self._on_change(self.in_use, self.size)
                self._cond.notify_all()

    def slot(self, timeout: float) -> "_Slot":
        return _Slot(self, timeout)


class _Slot:
    def __init__(self, pool: Pool, timeout: float):
        self.pool, self.timeout = pool, timeout

    async def __aenter__(self):
        await self.pool.acquire(self.timeout)
        return self

    async def __aexit__(self, *exc):
        # shield so a cancelled request (timeout) still returns its connection
        await asyncio.shield(self.pool.release())
        return False
