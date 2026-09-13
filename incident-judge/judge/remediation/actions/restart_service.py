from __future__ import annotations

from judge.core.models import Plan
from judge.remediation.actions.base import get_config_or_failure, register


class RestartService:
    """Not reversible, but safe_to_repeat (catalog caps autonomy at L2)."""

    name = "restart_service"

    async def preconditions(self, plan: Plan, control) -> list[str]:
        _, failures = await get_config_or_failure(control, plan.target_service)
        return failures

    async def snapshot(self, plan: Plan, control) -> dict:
        try:
            services = await control.services()
        except Exception:
            services = []
        for s in services:
            if s.get("name") == plan.target_service:
                return {"pid": s.get("pid"), "alive": s.get("alive")}
        return {}

    async def apply(self, plan: Plan, control) -> dict:
        return await control.restart(plan.target_service)

    async def revert(self, plan: Plan, control) -> dict | None:
        return None


register(RestartService())
