from __future__ import annotations

from types import SimpleNamespace

from judge.core.models import (
    AutonomyLevel,
    Decision,
    DecisionResult,
    Env,
    Incident,
    Plan,
    RunbookStats,
    Severity,
    VerifySpec,
)
from judge.core.store import Store
from judge.settings import Config, Settings


class FakeControl:
    def __init__(self, store: Store | None = None):
        self.store = store
        self.configs = {
            "checkout": {"service": "checkout", "environment": "production", "flags": {"payment_v2": True},
                         "pool_size": 2, "app_version": "1.4.2", "known_versions": ["1.4.1", "1.4.2"], "faults": {}},
        }
        self.calls: list[tuple] = []
        self.on_apply = None  # hook(plan_status) to observe persisted state at apply time

    async def get_config(self, service):
        if service not in self.configs:
            raise KeyError(service)
        return dict(self.configs[service], flags=dict(self.configs[service]["flags"]))

    async def services(self):
        return [{"name": n, "pid": 100, "alive": True} for n in self.configs]

    def _hook(self):
        if self.on_apply:
            self.on_apply()

    async def set_flag(self, service, flag, value):
        self._hook()
        prev = self.configs[service]["flags"].get(flag)
        self.configs[service]["flags"][flag] = value
        self.calls.append(("set_flag", service, flag, value))
        return {"prev": prev, "value": value}

    async def set_pool_size(self, service, size):
        self._hook()
        prev = self.configs[service]["pool_size"]
        self.configs[service]["pool_size"] = size
        self.calls.append(("set_pool_size", service, size))
        return {"prev": prev, "value": size}

    async def deploy(self, service, version):
        self._hook()
        prev = self.configs[service]["app_version"]
        self.configs[service]["app_version"] = version
        self.calls.append(("deploy", service, version))
        return {"prev": prev, "value": version}

    async def restart(self, service):
        self._hook()
        self.calls.append(("restart", service))
        return {"restarted_at": "now", "pid": 101}


class FakeMetrics:
    """values: metric -> float | list[float] (consumed per call, last value sticks)."""

    def __init__(self, values: dict, available: bool = True):
        self.values = values
        self._available = available
        self.calls: dict[str, int] = {}

    def available(self) -> bool:
        return self._available

    def value(self, metric, service, window_s, route=None):
        v = self.values.get(metric)
        if isinstance(v, list):
            i = self.calls.get(metric, 0)
            self.calls[metric] = i + 1
            return v[min(i, len(v) - 1)]
        return v


class Sleeps:
    def __init__(self, crash_at: int | None = None):
        self.calls: list[float] = []
        self.crash_at = crash_at

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.crash_at is not None and len(self.calls) == self.crash_at:
            raise SimulatedCrash()


class SimulatedCrash(Exception):
    pass


def make_runbook(action="scale_pool", params=None, level="L1", cap="L2", review_required=False,
                 runbook_id="db-pool-starved"):
    return SimpleNamespace(frontmatter=SimpleNamespace(
        id=runbook_id, title="DB pool starved",
        action=SimpleNamespace(name=action, params=params if params is not None else {"size": 20}),
        autonomy=SimpleNamespace(level=AutonomyLevel(level), cap=AutonomyLevel(cap), review_required=review_required),
        stats=RunbookStats(success=3),
    ))


