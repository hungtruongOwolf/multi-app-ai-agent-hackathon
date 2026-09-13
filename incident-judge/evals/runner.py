"""Eval runner (SPEC §14.3, CONTRACTS §7).

  uv run python -m evals.runner --scenarios core --k 3 --parallel 3 [--baseline B0]

Per trial: reset sandbox (trial) -> preflight clean -> seed memory + outcomes -> start ShopLab
supervisor on its own port_base -> start agent -> run timeline / sim humans / crash plan ->
wait terminal or quiescent or timeout -> stop agent -> snapshot (apps, config, audit, memory)
-> grade -> teardown."""

from __future__ import annotations

from judge.paths import reports_dir
import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from evals.baselines import baseline_env
from evals.graders import Verdict, grade
from evals.metrics import run_metrics
from evals.scenario import SERVICES, Scenario, TimelineStep, select
from evals.seed import seed_trial
from evals.sim_human import SimHuman
from evals.snapshot import TrialSnapshot, load_db, load_memory, normalize_sandbox
from judge.settings import ROOT, Settings

log = logging.getLogger("evals.runner")

SANDBOX_URL = "http://127.0.0.1:8900"
PORT_BASE0 = 9000  # trials use 9000, 9100, ...; never collides with the sandbox on 8900


def new_trial_id(scenario_id: str) -> str:
    return f"{re.sub('[^a-z0-9]', '', scenario_id.lower())[:3]}{uuid.uuid4().hex[:7]}"


def now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- processes


class Proc:
    def __init__(self, name: str, cmd: list[str], env: dict[str, str], log_path: Path):
        self.name, self.cmd, self.env, self.log_path = name, cmd, env, log_path
        self.p: subprocess.Popen | None = None
        self.exit_codes: list[int | None] = []

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.log_path, "ab")
        kw: dict = {}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        self.p = subprocess.Popen(self.cmd, env={**os.environ, **self.env}, stdout=fh, stderr=subprocess.STDOUT,
                                  cwd=str(ROOT), **kw)
        fh.close()

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def kill(self) -> None:
        if not self.p:
            return
        if self.p.poll() is None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(self.p.pid), "/T", "/F"], capture_output=True)
            else:
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.exit_codes.append(self.p.returncode)
        self.p = None


async def wait_http(url: str, timeout_s: float, proc: Proc | None = None) -> bool:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=2) as http:
        while time.monotonic() < deadline:
            if proc is not None and proc.p is not None and proc.p.poll() is not None:
                return False
            try:
                if (await http.get(url)).status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    return False


async def wait_services(supervisor_url: str, timeout_s: float) -> bool:
    """Supervisor /health is up before its children are. Faults armed before a service process starts would
    silently not apply (e.g. worker_hang only corrupts processes alive when armed), so wait for every service."""
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=2) as http:
        while time.monotonic() < deadline:
            try:
                services = (await http.get(f"{supervisor_url}/services")).json()
                if services and all(s.get("alive") for s in services):
                    oks = [(await http.get(f"{s['url']}/health")).status_code == 200 for s in services]
                    if all(oks):
                        return True
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            await asyncio.sleep(0.5)
    return False


async def ensure_sandbox(url: str, run_dir: Path) -> Proc | None:
    """Reuse a running sandbox; otherwise start one for the whole run."""
    probe = f"{url}/__admin/state?trial_id=__probe__"
    if await wait_http(probe, 1.5):
        return None
    port = url.rsplit(":", 1)[-1]
    proc = Proc("sandbox", [sys.executable, "-m", "sandbox.app", "--port", port], {}, run_dir / "sandbox.log")
    proc.start()
    if not await wait_http(probe, 30, proc):
        proc.kill()
        raise RuntimeError(f"sandbox did not start at {url} (see {run_dir / 'sandbox.log'})")
    return proc


# ---------------------------------------------------------------- audit events


def audit_event_seen(db_path: Path, event: str) -> bool:
    if not db_path.exists():
        return False
    kind, _, rest = event.partition(":")
    parts = rest.split(":")
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            if kind == "execution":
                q, a = "SELECT 1 FROM executions WHERE kind=? LIMIT 1", [parts[0]]
            elif kind == "plan_status":
                q, a = "SELECT 1 FROM plans WHERE status=? LIMIT 1", [parts[0]]
            elif kind == "decision":
                q, a = "SELECT 1 FROM decisions WHERE intent=?", [parts[0]]
                if len(parts) > 1:
                    q += " AND result=?"
                    a.append(parts[1])
            elif kind == "step":
                q, a = "SELECT 1 FROM steps WHERE kind=?", [parts[0]]
                if len(parts) > 1:
                    q += " AND status=?"
                    a.append(parts[1])
            elif kind == "state":
                q, a = "SELECT 1 FROM incidents WHERE state=? LIMIT 1", [parts[0]]
            else:
                return False
            return conn.execute(q, a).fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def activity_signature(db_path: Path) -> tuple:
    if not db_path.exists():
        return ()
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            sig = []
            for t in ("decisions", "steps", "executions", "approvals", "verifications", "outcomes", "plans"):
                sig.append(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            sig.append(conn.execute("SELECT COALESCE(MAX(updated_at),'') FROM steps").fetchone()[0])
            sig += [tuple(r) for r in conn.execute("SELECT id, state FROM incidents ORDER BY id")]
            return tuple(sig)
        finally:
            conn.close()
    except sqlite3.Error:
        return ()


def primary_states(db_path: Path) -> list[str]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute("SELECT state, data FROM incidents").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    out = []
    for state, data in rows:
        try:
            if json.loads(data).get("related_incident_id"):
                continue
        except (TypeError, ValueError):
            pass
        out.append(state)
    return out


# ---------------------------------------------------------------- trial


class Trial:
    def __init__(self, s: Scenario, k: int, slot: int, run_dir: Path, sandbox_url: str, baseline: str | None,
                 keep_state: bool = False):
        self.s, self.k, self.slot = s, k, slot
        self.trial_id = new_trial_id(s.id)
        self.port_base = PORT_BASE0 + 100 * slot
        self.supervisor_url = f"http://127.0.0.1:{self.port_base}"
        self.sandbox_url = sandbox_url
        self.baseline = baseline
        self.keep_state = keep_state
        self.dir = run_dir / "trials" / f"{s.id}-{k}-{self.trial_id}"
        self.settings = Settings.from_env(trial_id=self.trial_id, time_scale=s.time_scale)
        self.control_token = self.settings.shoplab_control_token
        self.fault_windows: list[dict] = []
        self.tool_faults = [tf.model_dump() for tf in s.tool_faults]
        self.tool_faults_path = self.dir / "tool_faults.json"
        self.errors: list[str] = []
        self.restarts = 0
        self.t0 = time.monotonic()
        self.last_step_at = max([st.at_s or 0 for st in s.timeline()] + [h.at_s or 0 for h in s.humans] + [0])

    # -- env / processes

    def agent_env(self) -> dict[str, str]:
        env = {
            "IJ_BACKEND": "sandbox",
            "IJ_TRIAL_ID": self.trial_id,
            "TIME_SCALE": str(self.s.time_scale),
            "IJ_SANDBOX_URL": self.sandbox_url,
            "SHOPLAB_SUPERVISOR_URL": self.supervisor_url,
            "IJ_TOOL_FAULTS": str(self.tool_faults_path),
            "IJ_VAR_DIR": str(self.settings.var_dir),
            "PYTHONUNBUFFERED": "1",
        }
        env.update(baseline_env(self.baseline))
        env.update(self.s.agent_env)
        return env

    def write_tool_faults(self) -> None:
        self.tool_faults_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.tool_faults_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tool_faults, indent=2), encoding="utf-8")
        tmp.replace(self.tool_faults_path)

    def make_agent(self) -> Proc:
        return Proc("agent", [sys.executable, "-m", "judge.cli", "run", "--trial-id", self.trial_id],
                    self.agent_env(), self.dir / "agent.log")

    def make_supervisor(self) -> Proc:
        return Proc("supervisor",
                    [sys.executable, "-m", "shoplab.supervisor", "--port-base", str(self.port_base),
                     "--trial-id", self.trial_id, "--sandbox-url", self.sandbox_url],
                    {"SHOPLAB_CONTROL_TOKEN": self.control_token, "IJ_TRIAL_ID": self.trial_id},
                    self.dir / "supervisor.log")

    # -- control helpers

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    async def _control(self, http: httpx.AsyncClient, method: str, path: str, body: dict | None = None) -> None:
        r = await http.request(method, f"{self.supervisor_url}{path}", json=body,
                               headers={"X-Control-Token": self.control_token})
        if r.status_code >= 400:
            self.errors.append(f"control {method} {path} -> {r.status_code} {r.text[:200]}")

    async def apply_step(self, http: httpx.AsyncClient, st: TimelineStep) -> None:
        ts = now().isoformat()
        if st.action == "inject":
            await self._control(http, "POST", "/faults", {"fault": st.fault, "service": st.service, "params": st.params})
            self.fault_windows.append({"fault": st.fault, "service": st.service, "start": ts, "end": None})
        elif st.action == "clear_faults":
            await self._control(http, "DELETE", "/faults")
            for w in self.fault_windows:
                w["end"] = w["end"] or ts
        elif st.action == "traffic":
            body = {k: v for k, v in {"rps": st.rps, "enabled": st.enabled}.items() if v is not None}
            await self._control(http, "PUT", "/traffic", body)
        elif st.action == "tool_faults_add":
            self.tool_faults += [tf.model_dump() for tf in st.tool_faults]
            self.write_tool_faults()
        elif st.action == "tool_faults_clear":
            self.tool_faults = []
            self.write_tool_faults()

    async def timeline(self, http: httpx.AsyncClient, stop: asyncio.Event) -> None:
        async def one(st: TimelineStep):
            if st.at_s is not None:
                delay = st.at_s - self.elapsed()
                if delay > 0:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                        return
                    except asyncio.TimeoutError:
                        pass
            else:
                while not audit_event_seen(self.settings.db_path, st.on):
                    if stop.is_set():
                        return
                    await asyncio.sleep(0.5)
            if not stop.is_set():
                await self.apply_step(http, st)

        steps = sorted(self.s.timeline(), key=lambda x: (x.at_s is None, x.at_s or 0))
        await asyncio.gather(*(one(st) for st in steps))

    async def crash_monitor(self, holder: dict, stop: asyncio.Event) -> None:
        plan = self.s.crash_plan
        if not plan:
            return
        done = 0
        while not stop.is_set() and done < plan.times:
            if audit_event_seen(self.settings.db_path, plan.after):
                holder["agent"].kill()
                self.restarts += 1
                done += 1
                await asyncio.sleep(plan.restart_after_s)
                if stop.is_set():
                    return
                holder["agent"].start()
                # a later crash must be triggered by a NEW occurrence; single-shot per event is the common case
                return
            await asyncio.sleep(0.25)

    async def wait_done(self, stop: asyncio.Event) -> str:
        w = self.s.wait
        last_sig, last_change = None, time.monotonic()
        min_run = max(w.min_runtime_s, self.last_step_at + 5)
        while True:
            el = self.elapsed()
            if el >= self.s.timeout_s:
                return "timeout"
            sig = activity_signature(self.settings.db_path)
            if sig != last_sig:
                last_sig, last_change = sig, time.monotonic()
            states = primary_states(self.settings.db_path)
            if el >= min_run and not w.require_incident and not w.until_states                     and time.monotonic() - last_change >= w.settle_s:
                return "quiescent"
            if el >= min_run and states:
                if w.until_states and all(st in w.until_states for st in states):
                    await asyncio.sleep(3)
                    return "terminal"
                if not w.until_states and time.monotonic() - last_change >= w.settle_s:
                    return "quiescent"
                if w.until_states and time.monotonic() - last_change >= max(w.settle_s * 4, 90):
                    return "stalled"
            await asyncio.sleep(1)

    # -- snapshot

    async def snapshot(self, http: httpx.AsyncClient, started: datetime, human: SimHuman, memory_seed: dict,
                       agent: Proc) -> TrialSnapshot:
        config_end: dict[str, dict] = {}
        for svc in sorted(SERVICES):
            try:
                r = await http.get(f"{self.supervisor_url}/config/{svc}")
                if r.status_code == 200:
                    config_end[svc] = r.json()
            except httpx.HTTPError:
                pass
        sandbox_raw: dict = {}
        try:
            r = await http.get(f"{self.sandbox_url}/__admin/state", params={"trial_id": self.trial_id})
            r.raise_for_status()
            sandbox_raw = r.json()
        except httpx.HTTPError as e:
            self.errors.append(f"sandbox state unavailable: {e!r}")
        (self.dir / "sandbox_state.json").write_text(json.dumps(sandbox_raw, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
        return TrialSnapshot(
            trial_id=self.trial_id, scenario_id=self.s.id, started_at=started, ended_at=now(),
            sandbox=normalize_sandbox(sandbox_raw), db=load_db(self.settings.db_path),
            memory=load_memory(self.settings.memory_dir), memory_seed=memory_seed, config_end=config_end,
            fault_windows=self.fault_windows, human_actions=human.actions, agent_restarts=self.restarts,
            agent_exit_codes=agent.exit_codes, errors=list(self.errors),
        )

    # -- main

    async def ensure_port_free(self, http: httpx.AsyncClient, timeout_s: float = 30) -> None:
        """If anything still answers on this slot's supervisor port, shut it down (token) and wait."""
        deadline = time.monotonic() + timeout_s
        asked = False
        while time.monotonic() < deadline:
            try:
                await http.get(f"{self.supervisor_url}/health", timeout=1)
            except httpx.HTTPError:
                return
            if not asked:
                try:
                    await http.post(f"{self.supervisor_url}/shutdown", headers={"X-Control-Token": self.control_token},
                                    timeout=2)
                except httpx.HTTPError:
                    pass
                asked = True
            await asyncio.sleep(0.5)
        raise RuntimeError(f"port {self.port_base} still in use by a stale supervisor")

    async def run(self) -> Verdict:
        self.dir.mkdir(parents=True, exist_ok=True)
        started = now()
        supervisor = self.make_supervisor()
        holder = {"agent": self.make_agent()}
        stop = asyncio.Event()
        human = SimHuman(self.s.humans, self.trial_id, self.sandbox_url)
        memory_seed: dict = {}
        async with httpx.AsyncClient(timeout=10) as http:
            try:
                await http.post(f"{self.sandbox_url}/__admin/reset", params={"trial_id": self.trial_id})
                pre = normalize_sandbox((await http.get(f"{self.sandbox_url}/__admin/state",
                                                        params={"trial_id": self.trial_id})).json())
                dirty = {k: len(v) for k, v in pre.items() if v and k != "slack_channels"}
                if dirty:
                    raise RuntimeError(f"preflight not clean: {dirty}")

                memory_seed = seed_trial(self.s, self.settings)
                self.write_tool_faults()

                await self.ensure_port_free(http)
                supervisor.start()
                if not await wait_http(f"{self.supervisor_url}/health", 60, supervisor):
                    raise RuntimeError(f"supervisor failed to start (see {supervisor.log_path})")
                health = (await http.get(f"{self.supervisor_url}/health")).json()
                if health.get("trial_id") != self.trial_id:
                    raise RuntimeError(f"stale supervisor on {self.supervisor_url}: trial {health.get('trial_id')} "
                                       f"!= {self.trial_id}")
                if not await wait_services(self.supervisor_url, 60):
                    raise RuntimeError("shoplab services did not become healthy")
                await self._control(http, "DELETE", "/faults")
                await self._control(http, "PUT", "/traffic", dict(self.s.traffic))

                self.t0 = time.monotonic()
                human.t0 = self.t0
                holder["agent"].start()
                tasks = [asyncio.create_task(self.timeline(http, stop)),
                         asyncio.create_task(human.run(stop)),
                         asyncio.create_task(self.crash_monitor(holder, stop))]
                reason = await self.wait_done(stop)
                (self.dir / "end_reason.txt").write_text(reason, encoding="utf-8")
                stop.set()
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:  # harness error, not an agent failure
                self.errors.append(f"harness: {e!r}")
                stop.set()
            finally:
                # Teardown must survive snapshot failures: a leaked supervisor keeps the port and the NEXT trial
                # in this slot would silently talk to it (wrong trial id on every Sentry event).
                holder["agent"].kill()
                try:
                    snap = await self.snapshot(http, started, human, memory_seed, holder["agent"])
                finally:
                    supervisor.kill()
                    await self.ensure_port_free(http)
                    if not self.keep_state:
                        try:
                            await http.post(f"{self.sandbox_url}/__admin/reset", params={"trial_id": self.trial_id})
                        except httpx.HTTPError:
                            pass
        verdict = grade(self.s, snap)
        (self.dir / "snapshot.json").write_text(json.dumps(snap.to_json(), indent=2, ensure_ascii=False, default=str),
                                                encoding="utf-8")
        (self.dir / "verdict.json").write_text(json.dumps(verdict.to_dict(), indent=2, default=str), encoding="utf-8")
        return verdict


# ---------------------------------------------------------------- run


async def run_all(scenarios: list[Scenario], k: int, parallel: int, run_dir: Path, sandbox_url: str,
                  baseline: str | None, keep_state: bool) -> list[dict]:
    run_dir.mkdir(parents=True, exist_ok=True)
    sandbox = await ensure_sandbox(sandbox_url, run_dir)
    slots: asyncio.Queue[int] = asyncio.Queue()
    for i in range(1, parallel + 1):
        slots.put_nowait(i)
    results: list[dict] = []

    async def one(s: Scenario, trial_k: int):
        slot = await slots.get()
        try:
            v = await Trial(s, trial_k, slot, run_dir, sandbox_url, baseline, keep_state).run()
            row = v.to_dict() | {"k": trial_k}
            results.append(row)
            print(f"[{s.id} #{trial_k}] {v.verdict.upper():6} missing={v.missing} unsafe={v.unsafe} errors={v.errors}",
                  flush=True)
        except Exception as e:  # one broken trial must not abort the whole run
            results.append({"scenario": s.id, "k": trial_k, "verdict": "error", "missing": [], "unsafe": [],
                            "errors": [f"runner: {e!r}"]})
            print(f"[{s.id} #{trial_k}] ERROR  runner exception: {e!r}", flush=True)
        finally:
            slots.put_nowait(slot)

    try:
        await asyncio.gather(*(one(s, i) for s in scenarios for i in range(1, k + 1)))
    finally:
        if sandbox:
            sandbox.kill()
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Incident Judge eval runner")
    ap.add_argument("--scenarios", default="core", help="core | extended | all | ID,ID")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--baseline", default=None, help="full | B0 | B1 | B2 | B3")
    ap.add_argument("--sandbox-url", default=SANDBOX_URL)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--keep-state", action="store_true", help="do not reset sandbox state after trials")
    ap.add_argument("--validate", action="store_true", help="only parse scenarios and exit")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    scenarios = select(args.scenarios)
    if args.validate:
        for s in scenarios:
            print(f"{s.id:4} {s.tier:8} {s.category:11} {s.title}")
        return 0
    baseline_env(args.baseline)  # validate early
    run_id = args.run_id or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.baseline or 'full'}"
    run_dir = reports_dir() / run_id
    results = asyncio.run(run_all(scenarios, args.k, args.parallel, run_dir, args.sandbox_url, args.baseline,
                                  args.keep_state))
    summary = {"run_id": run_id, "baseline": args.baseline or "full", "k": args.k,
               "scenarios": [s.id for s in scenarios], "tiers": {s.id: s.tier for s in scenarios},
               "titles": {s.id: s.title for s in scenarios},
               "results": sorted(results, key=lambda r: (r["scenario_id"], r["k"])), "metrics": run_metrics(results)}
    (run_dir / "results.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    from evals.report import write_report

    md, html = write_report(run_dir)
    print(f"\nreport: {md}\n        {html}")
    return 0 if all(r["verdict"] == "pass" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
