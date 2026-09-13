"""ShopLab supervisor: spawns service processes, owns runtime config, injects faults, drives traffic.

  uv run python -m shoplab.supervisor --port-base 8800 [--trial-id T] [--sandbox-url http://127.0.0.1:8900]

The agent's control plane (judge.connectors.shoplab.ShopLabControl) talks to this API only."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from shoplab.common import (
    DEFAULT_CONTROL_TOKEN,
    ROOT,
    SERVICES,
    clone,
    data_dir,
    default_configs,
    sandbox_dsn,
)
from shoplab.changes import ChangeLog
from shoplab.faults import FaultError, apply_fault
from shoplab.traffic import Traffic

log = logging.getLogger("shoplab.supervisor")


class FlagBody(BaseModel):
    value: bool


class PoolBody(BaseModel):
    size: int


class DeployBody(BaseModel):
    version: str


class FaultSpec(BaseModel):
    fault: str
    service: str | None = None
    params: dict = {}


class TrafficBody(BaseModel):
    rps: float | None = None
    enabled: bool | None = None
    routes: dict[str, float] | None = None
    staging_rps: float | None = None


@dataclass
class Managed:
    id: str
    port: int
    proc: subprocess.Popen | None = None
    log_file: object | None = None
    restarted_at: str | None = None


@dataclass
class Supervisor:
    port_base: int = 8800
    trial_id: str | None = None
    dsn_prod: str = ""
    dsn_staging: str = ""
    control_token: str = DEFAULT_CONTROL_TOKEN
    rps: float = 30.0
    staging_rps: float = 6.0
    traffic_enabled: bool = True
    data: Path = field(default_factory=lambda: data_dir(None))
    configs: dict[str, dict] = field(default_factory=default_configs)
    faults: list[dict] = field(default_factory=list)
    services: dict[str, Managed] = field(default_factory=dict)
    traffic: Traffic | None = None
    changes: ChangeLog | None = None

    def __post_init__(self):
        for sid, sd in SERVICES.items():
            self.services[sid] = Managed(id=sid, port=sd.port(self.port_base))
        if self.changes is None:
            self.changes = ChangeLog(self.data / "changes.jsonl")

    # ------------------------------------------------------------ faults (shared by the API and the /ops console)

    def add_fault(self, fault: str, service: str | None, params: dict, actor: str = "operator") -> dict:
        from shoplab.web.faults_info import CHANGE_FAULTS

        before = clone(self.configs)
        try:
            touched = apply_fault(self.configs, fault, service, params)
        except FaultError as e:
            raise HTTPException(400, str(e)) from e
        self.faults.append({"fault": fault, "service": service, "params": params, "services": touched,
                            "at": datetime.now(UTC).isoformat()})
        if fault in CHANGE_FAULTS:  # models a real change someone/something shipped
            for sid in touched:
                self.changes.record_config_diff(sid, before[sid], self.configs[sid], actor="release-bot")
        return {"ok": True, "services": touched}

    def clear_faults(self, actor: str = "operator") -> dict:
        self.faults.clear()
        for sid, fresh in default_configs().items():
            before = clone(self.configs[sid])
            self.configs[sid].clear()
            self.configs[sid].update(clone(fresh))
            self.changes.record_config_diff(sid, before, self.configs[sid], actor=actor)
        return {"ok": True}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port_base}"

    def service_url(self, sid: str) -> str | None:
        m = self.services.get(sid)
        return f"http://127.0.0.1:{m.port}" if m else None

    # ------------------------------------------------------------ processes

    def spawn(self, sid: str) -> None:
        m, sd = self.services[sid], SERVICES[sid]
        self.data.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            SERVICE_ID=sid,
            SERVICE_NAME=sd.name,
            ENVIRONMENT=sd.environment,
            PORT=str(m.port),
            SUPERVISOR_URL=self.url,
            SENTRY_DSN=self.dsn_prod if sd.environment == "production" else self.dsn_staging,
            IJ_TRIAL_ID=self.trial_id or "",
            SHOPLAB_SERVICE_DATA_DIR=str(self.data),
            PYTHONPATH=str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
        )
        if m.log_file is None:
            m.log_file = open(self.data / f"{sid.replace('@', '_')}.log", "ab")
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        m.proc = subprocess.Popen([sys.executable, "-m", "shoplab.service"], cwd=str(ROOT), env=env,
                                  stdout=m.log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)

    def kill(self, sid: str, timeout: float = 5.0) -> None:
        m = self.services[sid]
        if m.proc and m.proc.poll() is None:
            m.proc.terminate()
            try:
                m.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                m.proc.kill()
                m.proc.wait(timeout)

    def kill_all(self) -> None:
        for sid in self.services:
            try:
                self.kill(sid)
            except Exception:  # noqa: BLE001
                pass
        for m in self.services.values():
            if m.log_file:
                try:
                    m.log_file.close()
                except Exception:  # noqa: BLE001
                    pass
                m.log_file = None

    def alive(self, sid: str) -> bool:
        m = self.services[sid]
        return bool(m.proc and m.proc.poll() is None)

    async def wait_healthy(self, sid: str, started_after: float = 0.0, timeout: float = 20.0) -> dict | None:
        """Health of a process started after `started_after` (epoch s). Note: on Windows the venv
        python.exe is a launcher, so the service's own pid differs from Popen.pid."""
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=1.0) as client:
            while time.monotonic() < deadline:
                if not self.alive(sid):
                    return None
                try:
                    r = await client.get(f"{self.service_url(sid)}/health")
                    if r.status_code == 200 and float(r.json().get("started_at", 0)) >= started_after:
                        return r.json()
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.2)
        return None

    async def restart(self, sid: str) -> dict:
        t0 = time.time()
        await asyncio.to_thread(self.kill, sid)
        self.spawn(sid)
        health = await self.wait_healthy(sid, started_after=t0)
        if not health:
            raise HTTPException(503, f"service {sid} did not become healthy after restart")
        m = self.services[sid]
        m.restarted_at = datetime.now(UTC).isoformat()
        return {"restarted_at": m.restarted_at, "pid": health["pid"]}


def create_app(sup: Supervisor) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        atexit.register(sup.kill_all)
        for sid in SERVICES:
            sup.spawn(sid)
        sup.traffic = Traffic(sup.service_url, rps=sup.rps, staging_rps=sup.staging_rps,
                              enabled=sup.traffic_enabled)
        await sup.traffic.start()
        try:
            yield
        finally:
            await sup.traffic.stop()
            close = getattr(app.state, "web_close", None)
            if close is not None:
                await close()
            await asyncio.to_thread(sup.kill_all)

    app = FastAPI(title="shoplab-supervisor", lifespan=lifespan)
    app.state.sup = sup

    def auth(x_control_token: str | None = Header(default=None)):
        if x_control_token != sup.control_token:
            raise HTTPException(401, "invalid control token")

    def known(service: str) -> dict:
        if service not in sup.configs:
            raise HTTPException(404, f"unknown service {service!r}")
        return sup.configs[service]

    @app.get("/health")
    async def health():
        return {"ok": True, "trial_id": sup.trial_id, "port_base": sup.port_base}

    @app.get("/services")
    async def services():
        out = []
        for sid, m in sup.services.items():
            sd = SERVICES[sid]
            out.append({"name": sid, "service": sd.name, "environment": sd.environment, "port": m.port,
                        "pid": m.proc.pid if m.proc else None, "url": sup.service_url(sid),
                        "alive": sup.alive(sid), "restarted_at": m.restarted_at})
        return out

    @app.get("/config/{service}")
    async def get_config(service: str):
        return known(service)

    @app.put("/config/{service}/flags/{flag}", dependencies=[Depends(auth)])
    async def set_flag(service: str, flag: str, body: FlagBody, x_actor: str | None = Header(default=None)):
        cfg = known(service)
        if flag not in cfg["flags"]:
            raise HTTPException(404, f"unknown flag {flag!r} for {service}")
        prev = cfg["flags"][flag]
        cfg["flags"][flag] = body.value
        if prev != body.value:
            sup.changes.record(service=service, kind="flag", actor=x_actor or "operator",
                               summary=f"{flag}: {str(prev).lower()} → {str(body.value).lower()}",
                               detail={"flag": flag, "prev": prev, "value": body.value})
        return {"prev": prev, "value": body.value}

    @app.put("/config/{service}/pool_size", dependencies=[Depends(auth)])
    async def set_pool(service: str, body: PoolBody, x_actor: str | None = Header(default=None)):
        cfg = known(service)
        if not 1 <= body.size <= 200:
            raise HTTPException(422, "pool size must be within [1..200]")
        prev = cfg["pool_size"]
        cfg["pool_size"] = body.size
        if prev != body.size:
            sup.changes.record(service=service, kind="pool_size", actor=x_actor or "operator",
                               summary=f"DB pool size: {prev} → {body.size}", detail={"prev": prev, "value": body.size})
        return {"prev": prev, "value": body.size}

    @app.post("/deploy/{service}", dependencies=[Depends(auth)])
    async def deploy(service: str, body: DeployBody, x_actor: str | None = Header(default=None)):
        cfg = known(service)
        if body.version not in cfg["known_versions"]:
            raise HTTPException(400, f"unknown version {body.version!r}")
        prev = cfg["app_version"]
        cfg["app_version"] = body.version
        if prev != body.version:
            sup.changes.record(service=service, kind="deploy", actor=x_actor or "operator",
                               summary=f"deployed {body.version} (was {prev})",
                               detail={"prev": prev, "value": body.version})
        return {"prev": prev, "value": body.version}

    @app.post("/services/{service}/restart", dependencies=[Depends(auth)])
    async def restart(service: str, x_actor: str | None = Header(default=None)):
        known(service)
        result = await sup.restart(service)
        sup.changes.record(service=service, kind="restart", actor=x_actor or "operator",
                           summary="process restarted", detail=result)
        return result

    @app.get("/faults")
    async def list_faults():
        return sup.faults

    @app.post("/faults", dependencies=[Depends(auth)])
    async def add_fault(spec: FaultSpec):
        return sup.add_fault(spec.fault, spec.service, spec.params)

    @app.delete("/faults", dependencies=[Depends(auth)])
    async def clear_faults(x_actor: str | None = Header(default=None)):
        return sup.clear_faults(actor=x_actor or "operator")

    @app.get("/changes")
    async def list_changes(since: str | None = None, service: str | None = None):
        return sup.changes.list(since=since, service=service)

    @app.get("/traffic")
    async def get_traffic():
        return sup.traffic.state() if sup.traffic else {}

    @app.put("/traffic", dependencies=[Depends(auth)])
    async def put_traffic(body: TrafficBody):
        try:
            return sup.traffic.update(rps=body.rps, enabled=body.enabled, routes=body.routes,
                                      staging_rps=body.staging_rps)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/shutdown", dependencies=[Depends(auth)])
    async def shutdown():
        server = getattr(app.state, "server", None)
        if server is None:
            raise HTTPException(501, "shutdown unavailable when not started via main()")
        server.should_exit = True
        return {"ok": True}

    from shoplab.web.routes import attach_web

    attach_web(app, sup)
    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="ShopLab supervisor")
    p.add_argument("--port-base", type=int, default=8800)
    p.add_argument("--trial-id", default=os.environ.get("IJ_TRIAL_ID") or None)
    p.add_argument("--sandbox-url", default=None, help="derive Sentry DSNs for the local sandbox")
    p.add_argument("--sentry-dsn-prod", default=os.environ.get("SENTRY_DSN_PROD", ""))
    p.add_argument("--sentry-dsn-staging", default=os.environ.get("SENTRY_DSN_STAGING", ""))
    p.add_argument("--control-token", default=os.environ.get("SHOPLAB_CONTROL_TOKEN", DEFAULT_CONTROL_TOKEN))
    p.add_argument("--rps", type=float, default=30.0)
    p.add_argument("--staging-rps", type=float, default=6.0)
    p.add_argument("--no-traffic", action="store_true")
    p.add_argument("--data-dir", default=None)
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    dsn_prod, dsn_staging = a.sentry_dsn_prod, a.sentry_dsn_staging
    if a.sandbox_url:
        # --sandbox-url is an explicit choice: never let real DSNs inherited from the environment (.env) leak
        # sandbox/eval traffic into a real Sentry project.
        dsn_prod = sandbox_dsn(a.sandbox_url, "production")
        dsn_staging = sandbox_dsn(a.sandbox_url, "staging")
    sup = Supervisor(
        port_base=a.port_base, trial_id=a.trial_id, dsn_prod=dsn_prod, dsn_staging=dsn_staging,
        control_token=a.control_token, rps=a.rps, staging_rps=a.staging_rps, traffic_enabled=not a.no_traffic,
        data=Path(a.data_dir) if a.data_dir else data_dir(a.trial_id),
    )
    app = create_app(sup)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port_base, log_level="warning",
                                           access_log=False))
    app.state.server = server
    try:
        server.run()
    finally:
        sup.kill_all()


if __name__ == "__main__":
    main()
