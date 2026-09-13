from __future__ import annotations

from judge.core.models import Plan
from judge.remediation.actions.base import get_config_or_failure, register


class RollbackDeploy:
    """Global blast radius, not auto-reversible (catalog caps autonomy at L1)."""

    name = "rollback_deploy"

    async def preconditions(self, plan: Plan, control) -> list[str]:
        cfg, failures = await get_config_or_failure(control, plan.target_service)
        if failures:
            return failures
        to = plan.params.get("to_version")
        if to not in (cfg.get("known_versions") or []):
            failures.append(f"version_known: {to!r} not in known_versions")
        elif to == cfg.get("app_version"):
            failures.append(f"version_known: already running {to!r}")
        return failures

    async def snapshot(self, plan: Plan, control) -> dict:
        cfg = await control.get_config(plan.target_service)
        return {"app_version": cfg["app_version"]}

    async def apply(self, plan: Plan, control) -> dict:
        return await control.deploy(plan.target_service, plan.params["to_version"])

    async def revert(self, plan: Plan, control) -> dict | None:
        return None


register(RollbackDeploy())
