"""Wires concrete dependencies for the agent from Settings."""

from __future__ import annotations

from judge.paths import KNOWLEDGE_DIR

import logging
import os

from judge.agent import Agent, Deps
from judge.core.store import Store
from judge.settings import ROOT, Config, Settings

log = logging.getLogger("judge.runtime")


async def build_agent(settings: Settings | None = None, config: Config | None = None) -> Agent:
    settings = settings or Settings.from_env()
    config = config or Config()
    settings.var_dir.mkdir(parents=True, exist_ok=True)

    from judge.approvals.slack_commands import ApprovalPoller
    from judge.approvals.verifier import ApprovalVerifier
    from judge.connectors.instatus import InstatusClient
    from judge.connectors.linear import LinearClient
    from judge.connectors.sentry import SentryClient
    from judge.connectors.shoplab import ShopLabControl
    from judge.connectors.slack import SlackClient
    from judge.connectors.transport import HttpClient
    from judge.memory.ingest import Ingestor
    from judge.memory.query import RunbookQuery
    from judge.memory.repo import MemoryRepo
    from judge.memory.schema import REQUIRED_SECTIONS
    from judge.reasoning.judge import make_judge
    from judge.remediation.runner import RemediationRunner
    from judge.signals.pollers import SentryPoller, SloPoller

    store = Store(settings.db_path)
    from judge import testhooks

    fault_path = os.environ.get("IJ_TOOL_FAULTS")
    fault_plan = testhooks.ReloadingFaultPlan(fault_path) if fault_path else None
    http = HttpClient(fault_plan)

    sentry = SentryClient(settings, http)
    linear = LinearClient(settings, http)
    instatus = InstatusClient(settings, http)
    slack = SlackClient(settings, http)
    control = ShopLabControl(settings, http)

    if settings.trial_id:  # never act on someone else's ShopLab (stale process on a reused port)
        import httpx

        health = httpx.get(f"{settings.shoplab_supervisor_url.rstrip('/')}/health", timeout=5).json()
        if health.get("trial_id") != settings.trial_id:
            raise RuntimeError(f"ShopLab at {settings.shoplab_supervisor_url} belongs to trial "
                               f"{health.get('trial_id')!r}, not {settings.trial_id!r}; refusing to start")
    metrics = await testhooks.FaultyScrapeBackend.from_supervisor(settings.shoplab_supervisor_url)
    metrics.fault_plan = fault_plan
    await metrics.start()

    repo = MemoryRepo(settings.memory_dir, KNOWLEDGE_DIR)
    repo.ensure()

    if settings.judge_impl == "claude" and settings.anthropic_api_key:
        from judge.reasoning.memory_llm import ClaudeChooser, ClaudeWriter

        chooser = ClaudeChooser(settings.anthropic_api_key, settings.judge_model)
        writer = ClaudeWriter(settings.anthropic_api_key, settings.memory_model, REQUIRED_SECTIONS)
    else:
        from judge.memory.heuristic import HeuristicChooser, HeuristicWriter

        chooser, writer = HeuristicChooser(), HeuristicWriter()

    deps = Deps(
        settings=settings, config=config, store=store,
        sentry=sentry, linear=linear, instatus=instatus, slack=slack, control=control,
        metrics=metrics, repo=repo,
        query=RunbookQuery(repo, chooser, metrics),
        ingestor=Ingestor(repo, store, config, writer),
        runner=RemediationRunner(store, config, settings, control, metrics),
        approval_poller=ApprovalPoller(store, slack, ApprovalVerifier(config, settings, config.catalog)),
        judge=(testhooks.OverrideJudge(make_judge(settings), testhooks.judge_override())
               if testhooks.judge_override() else make_judge(settings)),
        sentry_poller=SentryPoller(sentry, store, settings),
        slo_poller=SloPoller(metrics, config, settings, store),
        baseline=os.environ.get("IJ_BASELINE") or None,
        extras={"http": http},
    )
    log.info("agent built: backend=%s judge=%s trial=%s baseline=%s time_scale=%s", settings.backend,
             deps.judge.name, settings.trial_id, deps.baseline, settings.time_scale)
    from judge.connectors.pagerduty import PagerDutyClient

    deps.extras["pagerduty"] = PagerDutyClient(settings, http)
    if settings.backend == "real" and settings.github_token and settings.github_memory_repo:
        from judge.connectors.github import GitHubClient
        from judge.memory.github_mirror import GitHubMirror

        deps.extras["github"] = GitHubMirror(repo, GitHubClient(settings.github_token, settings.github_memory_repo, http))
        log.info("knowledge base mirrored to GitHub %s (wiki proposals become pull requests)",
                 settings.github_memory_repo)
    agent = Agent(deps)
    if settings.backend == "real" and settings.slack_app_token:
        from judge.approvals.slack_socket import SocketApprovals

        agent.socket = SocketApprovals(settings, store, ApprovalVerifier(config, settings, config.catalog),
                                       store.get_incident, agent.current_subject)
    elif settings.backend == "real":
        log.warning("SLACK_APP_TOKEN not set: card buttons are disabled, typed `approve <hash>` still works")
    return agent
