"""judge CLI."""

from __future__ import annotations

from judge.paths import KNOWLEDGE_DIR

import asyncio
import json
import logging
import os

import typer
from rich.console import Console
from rich.table import Table

from judge.settings import ROOT, Config, Settings

import faulthandler

faulthandler.enable()  # a hard crash (segfault) prints the Python stack instead of dying silently

app = typer.Typer(no_args_is_help=True, add_completion=False)
memory_app = typer.Typer(no_args_is_help=True)
app.add_typer(memory_app, name="memory")
console = Console()


def _settings(trial_id: str | None) -> Settings:
    if trial_id:
        os.environ["IJ_TRIAL_ID"] = trial_id
    return Settings.from_env()


def _logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def run(trial_id: str = typer.Option(None, "--trial-id"), once: bool = False,
        max_seconds: float = typer.Option(None, "--max-seconds"), interval: float = 2.0, verbose: bool = False):
    """Run the agent loop."""
    _logging(verbose)
    settings = _settings(trial_id)

    async def main():
        from judge.runtime import build_agent

        agent = await build_agent(settings)
        await agent.run(max_seconds=max_seconds, once=once, interval_s=interval)

    asyncio.run(main())


@app.command()
def dev(time_scale: float = typer.Option(0.2, "--time-scale"), trial_id: str = typer.Option("demo", "--trial-id"),
        seed_fixtures: bool = typer.Option(True, "--seed/--no-seed"), verbose: bool = False):
    """One command local stack: sandbox (:8900) + ShopLab (:8800) + agent. Ctrl+C stops everything."""
    import subprocess
    import sys
    import time

    import httpx

    _logging(verbose)
    os.environ["TIME_SCALE"] = str(time_scale)
    settings = _settings(trial_id)
    settings.var_dir.mkdir(parents=True, exist_ok=True)
    logs = settings.var_dir / "dev"
    logs.mkdir(exist_ok=True)
    procs = []

    def spawn(name, args):
        fh = open(logs / f"{name}.log", "w", encoding="utf-8")
        p = subprocess.Popen([sys.executable, "-m", *args], cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        procs.append(p)
        return p

    def wait(url, seconds=60):
        for _ in range(seconds * 2):
            try:
                if httpx.get(url, timeout=1).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False

    try:
        if settings.backend == "real":
            dsn_prod, dsn_staging = os.environ.get("SENTRY_DSN_PROD", ""), os.environ.get("SENTRY_DSN_STAGING", "")
            if not dsn_prod or not dsn_staging:
                raise typer.Exit("real backend: SENTRY_DSN_PROD / SENTRY_DSN_STAGING missing — run `judge bootstrap`")
            spawn("shoplab", ["shoplab.supervisor", "--port-base", "8800", "--trial-id", trial_id,
                              "--sentry-dsn-prod", dsn_prod, "--sentry-dsn-staging", dsn_staging])
        else:
            spawn("sandbox", ["sandbox.app", "--port", "8900"])
            if not wait("http://127.0.0.1:8900/health"):
                raise typer.Exit("sandbox did not start")
            spawn("shoplab", ["shoplab.supervisor", "--port-base", "8800", "--trial-id", trial_id,
                              "--sandbox-url", "http://127.0.0.1:8900"])
        if not wait("http://127.0.0.1:8800/health"):
            raise typer.Exit("shoplab did not start")
        if seed_fixtures:
            from judge.core.store import Store
            from judge.memory.repo import MemoryRepo, seed_runbooks
            from judge.memory.stats import sync_code_owned
            from judge.core.models import Outcome, OutcomeResult, now
            from datetime import timedelta

            repo = MemoryRepo(settings.memory_dir, KNOWLEDGE_DIR)
            repo.ensure()
            if not repo.runbooks():
                seed_runbooks(repo, sorted((KNOWLEDGE_DIR / "wiki" / "runbooks").glob("*.md")))
                store = Store(settings.db_path)
                for rb in ("checkout-payment-v2-flag", "db-pool-starved"):
                    for i in range(3):
                        store.add_outcome(Outcome(runbook_id=rb, incident_id=f"inc_seed_{rb}_{i}",
                                                  plan_id=f"plan_seed_{rb}_{i}", action="seed",
                                                  result=OutcomeResult.success, ts=now() - timedelta(days=3 - i)))
                sync_code_owned(repo, store, Config(), now())
        if settings.backend == "real":
            console.print(f"[green]stack up (real apps)[/green]  ShopLab :8800 → Sentry; agent → Linear/Instatus/Slack. "
                          f"logs: {logs}")
        else:
            console.print(f"[green]stack up[/green]  Slack UI: http://127.0.0.1:8900/slack/ui   "
                          f"Status page: http://127.0.0.1:8900/status/page_shoplab   logs: {logs}")
        console.print("Inject a fault:  curl -X POST http://127.0.0.1:8800/faults -H 'X-Control-Token: dev-control-token' "
                      "-H 'Content-Type: application/json' -d '{\"fault\":\"bad_flag\",\"service\":\"checkout\"}'")

        # separate process: a UI bug or a slow page can never take the agent down with it
        spawn("console", ["judge.cli", "console", "--trial-id", trial_id, "--port", "8700"])
        console.print("Console:  http://127.0.0.1:8700")

        async def main():
            from judge.runtime import build_agent

            agent = await build_agent(settings)
            await agent.run()

        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        for p in reversed(procs):
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
            else:
                p.terminate()


def _start_console_thread(settings: Settings, port: int = 8700) -> None:
    """Serve the read-only console next to the agent (own thread + event loop; never blocks the agent)."""
    import threading

    import uvicorn

    from judge.console.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, name="console", daemon=True).start()


@app.command("console")
def console_cmd(trial_id: str = typer.Option(None, "--trial-id"), port: int = typer.Option(8700, "--port"),
                host: str = typer.Option("127.0.0.1", "--host")):
    """Open the web console: incidents, evidence, fixes, runbooks, ShopLab and eval results."""
    import uvicorn

    from judge.console.app import create_app

    settings = _settings(trial_id)
    console.print(f"Incident Judge console: http://{host}:{port}  (run: {settings.trial_id or 'default'}, "
                  f"backend: {settings.backend})")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


def _print_report(rep, title: str) -> None:
    t = Table("app", "step", "ok", "detail", title=title)
    for c in rep.checks:
        t.add_row(c.app, c.step, "[green]yes[/green]" if c.ok else "[red]NO[/red]", c.detail)
    console.print(t)


@app.command()
def bootstrap(channel: str = typer.Option("incident-judge-oncall", "--channel", help="Slack on-call channel name"),
              linear_team_key: str = typer.Option(None, "--linear-team-key"),
              write: bool = typer.Option(True, "--write/--dry-run", help="write discovered ids to .env")):
    """Discover or create the ids the agent needs in your real accounts and write them to .env."""
    from judge.realapps import bootstrap as run_bootstrap, update_env_file

    settings = Settings.from_env()
    rep = asyncio.run(run_bootstrap(settings, Config(), channel_name=channel, team_key=linear_team_key))
    _print_report(rep, f"bootstrap ({settings.backend})")
    if rep.env_updates:
        console.print("ids: " + ", ".join(sorted(rep.env_updates)))
        if write and settings.backend == "real":
            update_env_file(rep.env_updates)
            console.print("[green].env updated[/green]")
        elif write:
            console.print("[yellow]sandbox backend: .env not modified[/yellow]")
    if not rep.ok:
        raise typer.Exit(1)


@app.command()
def doctor(skip_sentry_ingest: bool = typer.Option(False, "--skip-sentry-ingest")):
    """Prove every integration: auth, one write, read it back, clean up."""
    from judge.realapps import doctor as run_doctor

    settings = Settings.from_env()
    rep = asyncio.run(run_doctor(settings, Config(), skip_sentry_ingest=skip_sentry_ingest))
    _print_report(rep, f"doctor ({settings.backend})")
    console.print("[green]all integrations healthy[/green]" if rep.ok else "[red]some checks failed[/red]")
    if not rep.ok:
        raise typer.Exit(1)


@app.command()
def pause(trial_id: str = typer.Option(None, "--trial-id")):
    """Engage the kill switch (agent drops to L0, no public writes)."""
    from judge.core.store import Store

    Store(_settings(trial_id).db_path).put_kv("kill_switch", True)
    console.print("[red]kill switch ENGAGED[/red]")


@app.command()
def resume(trial_id: str = typer.Option(None, "--trial-id")):
    """Release the kill switch."""
    from judge.core.store import Store

    Store(_settings(trial_id).db_path).put_kv("kill_switch", False)
    console.print("[green]kill switch released[/green]")


@app.command()
def incidents(trial_id: str = typer.Option(None, "--trial-id")):
    """List incidents and their decisions."""
    from judge.core.store import Store

    store = Store(_settings(trial_id).db_path)
    t = Table("id", "state", "env", "services", "sev", "impact", "visible", "linear", "public", "runbook")
    for i in store.incidents():
        t.add_row(i.id, i.state.value, i.environment.value, ",".join(i.services), str(i.severity or ""),
                  str(i.customer_impact or ""), str(i.customer_visible), str(i.linear_issue_id or ""),
                  str(i.instatus_incident_id or ""), str(i.runbook_id or ""))
    console.print(t)


@app.command()
def decisions(incident_id: str = typer.Argument(None), trial_id: str = typer.Option(None, "--trial-id")):
    """Show the audit log of policy decisions."""
    from judge.core.store import Store

    store = Store(_settings(trial_id).db_path)
    t = Table("ts", "incident", "intent", "result", "rules", "explain")
    for d in store.decisions(incident_id):
        t.add_row(d.ts.strftime("%H:%M:%S"), d.incident_id or "", d.intent, d.result.value, ",".join(d.rules),
                  d.explain[:120])
    console.print(t)


@app.command("review-runbook")
def review_runbook(runbook_id: str, trial_id: str = typer.Option(None, "--trial-id")):
    """Human marks a demoted runbook as reviewed (clears review_required)."""
    from judge.core.models import now
    from judge.core.store import Store
    from judge.memory.repo import MemoryRepo
    from judge.memory.stats import mark_reviewed, sync_code_owned

    s = _settings(trial_id)
    store = Store(s.db_path)
    mark_reviewed(store, runbook_id, user_id=os.environ.get("USERNAME", "cli"), at=now())
    repo = MemoryRepo(s.memory_dir, KNOWLEDGE_DIR)
    repo.ensure()
    changed = sync_code_owned(repo, store, Config(), now())
    console.print(f"reviewed {runbook_id}; updated: {changed}")


@memory_app.command("proposals")
def memory_proposals(trial_id: str = typer.Option(None, "--trial-id")):
    from judge.memory.repo import MemoryRepo

    s = _settings(trial_id)
    repo = MemoryRepo(s.memory_dir, KNOWLEDGE_DIR)
    repo.ensure()
    for p in repo.proposals():
        console.print(json.dumps(p.model_dump(mode="json"), ensure_ascii=False))


@memory_app.command("merge")
def memory_merge(proposal_id: str, trial_id: str = typer.Option(None, "--trial-id")):
    """Human merge from CLI (validated first)."""
    from judge.memory.pr_validator import validate_proposal
    from judge.memory.repo import MemoryRepo

    s = _settings(trial_id)
    repo = MemoryRepo(s.memory_dir, KNOWLEDGE_DIR)
    repo.ensure()
    p = repo.proposal(proposal_id)
    if p is None:
        raise typer.BadParameter("unknown proposal")
    errors = validate_proposal(repo, p, Config())
    if errors:
        console.print(f"[red]refused: {errors}[/red]")
        raise typer.Exit(1)
    console.print(f"merged: {repo.merge_proposal(proposal_id)}")


@memory_app.command("reject")
def memory_reject(proposal_id: str, reason: str = "rejected via CLI", trial_id: str = typer.Option(None, "--trial-id")):
    from judge.memory.repo import MemoryRepo

    s = _settings(trial_id)
    repo = MemoryRepo(s.memory_dir, KNOWLEDGE_DIR)
    repo.ensure()
    repo.reject_proposal(proposal_id, reason)
    console.print("rejected")


@memory_app.command("lint")
def memory_lint(apply: bool = False, trial_id: str = typer.Option(None, "--trial-id")):
    from judge.core.models import now
    from judge.core.store import Store
    from judge.memory import lint as lint_mod
    from judge.memory.repo import MemoryRepo

    s = _settings(trial_id)
    repo = MemoryRepo(s.memory_dir, KNOWLEDGE_DIR)
    repo.ensure()
    store = Store(s.db_path)
    findings = lint_mod.lint(repo, Config(), store, now())
    for f in findings:
        console.print(f)
    if apply:
        lint_mod.apply_lint(repo, Config(), store, now())


if __name__ == "__main__":
    app()
