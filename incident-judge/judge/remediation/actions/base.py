"""Typed remediation actions. The only code allowed to mutate ShopLab (no free-form shell).

Each action is idempotent at the target: apply() sets an absolute state (flag value, pool size,
version) or is explicitly safe_to_repeat (restart), so re-applying after a crash is harmless."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from judge.core.models import Plan

if TYPE_CHECKING:
    from judge.connectors.shoplab import ShopLabControl


@runtime_checkable
class ActionImpl(Protocol):
    name: str

    async def preconditions(self, plan: Plan, control: "ShopLabControl") -> list[str]:
        """Human-readable failures; empty list = ok."""
        ...

    async def snapshot(self, plan: Plan, control: "ShopLabControl") -> dict:
        """State needed to revert. Persisted before apply()."""
        ...

    async def apply(self, plan: Plan, control: "ShopLabControl") -> dict:
        ...

    async def revert(self, plan: Plan, control: "ShopLabControl") -> dict | None:
        """None if the action is not reversible."""
        ...


REGISTRY: dict[str, ActionImpl] = {}


def register(impl: ActionImpl) -> ActionImpl:
    REGISTRY[impl.name] = impl
    return impl


async def get_config_or_failure(control: "ShopLabControl", service: str) -> tuple[dict | None, list[str]]:
    try:
        return await control.get_config(service), []
    except Exception as e:  # unknown service or control plane down
        return None, [f"service_known: cannot read config for {service!r}: {e!r}"[:300]]


# populate registry (modules call register() at import time)
from judge.remediation.actions import restart_service, rollback_deploy, scale_pool, toggle_flag  # noqa: E402,F401
