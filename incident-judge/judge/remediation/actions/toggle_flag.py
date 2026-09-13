from __future__ import annotations

from judge.core.models import Plan
from judge.remediation.actions.base import get_config_or_failure, register


class ToggleFlag:
    name = "toggle_flag"

    async def preconditions(self, plan: Plan, control) -> list[str]:
        cfg, failures = await get_config_or_failure(control, plan.target_service)
        if failures:
            return failures
        flag = plan.params.get("flag")
        if flag not in (cfg.get("flags") or {}):
            failures.append(f"flag_exists: flag {flag!r} not defined on {plan.target_service}")
        if not isinstance(plan.params.get("value"), bool):
            failures.append("value must be bool")
        return failures

    async def snapshot(self, plan: Plan, control) -> dict:
        cfg = await control.get_config(plan.target_service)
        flag = plan.params["flag"]
        return {"flag": flag, "value": cfg["flags"][flag]}

    async def apply(self, plan: Plan, control) -> dict:
        return await control.set_flag(plan.target_service, plan.params["flag"], plan.params["value"])

    async def revert(self, plan: Plan, control) -> dict | None:
        prev = plan.prev_state
        return await control.set_flag(plan.target_service, prev["flag"], prev["value"])


register(ToggleFlag())
