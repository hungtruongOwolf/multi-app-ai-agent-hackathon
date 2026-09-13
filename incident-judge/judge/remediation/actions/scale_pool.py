from __future__ import annotations

from judge.core.models import Plan
from judge.remediation.actions.base import get_config_or_failure, register

MIN_POOL, MAX_POOL = 5, 50


class ScalePool:
    name = "scale_pool"

    async def preconditions(self, plan: Plan, control) -> list[str]:
        _, failures = await get_config_or_failure(control, plan.target_service)
        if failures:
            return failures
        size = plan.params.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not MIN_POOL <= size <= MAX_POOL:
            failures.append(f"pool_size_in_bounds: size={size!r} not in [{MIN_POOL}..{MAX_POOL}]")
        return failures

    async def snapshot(self, plan: Plan, control) -> dict:
        cfg = await control.get_config(plan.target_service)
        return {"pool_size": cfg["pool_size"]}

    async def apply(self, plan: Plan, control) -> dict:
        return await control.set_pool_size(plan.target_service, plan.params["size"])

    async def revert(self, plan: Plan, control) -> dict | None:
        return await control.set_pool_size(plan.target_service, plan.prev_state["pool_size"])


register(ScalePool())
