"""Local sandbox emulating the external apps the agent uses (Sentry, Linear, Instatus, Slack).

Run: uv run python -m sandbox.app --port 8900
The agent reaches it by setting IJ_BACKEND=sandbox (default). Request/response shapes follow the real APIs."""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from sandbox import admin_api, instatus_api, linear_api, sentry_api, slack_api
from sandbox.db import DocStore

from judge.paths import runtime_dir

DEFAULT_DB = runtime_dir() / "sandbox.db"


def create_app(db_path: Path | str = DEFAULT_DB, base_url: str = "http://127.0.0.1:8900") -> FastAPI:
    db = DocStore(db_path)
    sentry = sentry_api.SentryEmulator(db, base_url)
    linear = linear_api.LinearEmulator(db)
    instatus = instatus_api.InstatusEmulator(db)
    slack = slack_api.SlackEmulator(db)
    admin = admin_api.Admin(sentry, linear, instatus, slack)
    admin.seed()

    app = FastAPI(title="Incident Judge sandbox", docs_url="/__docs")
    app.state.db = db
    app.state.admin = admin
    app.include_router(sentry_api.build_router(sentry))
    app.include_router(linear_api.build_router(linear))
    app.include_router(instatus_api.build_router(instatus))
    app.include_router(slack_api.build_router(slack))
    app.include_router(admin_api.build_router(admin))

    @app.get("/", response_class=HTMLResponse)
    def index():
        return ("<!doctype html><meta charset=utf-8><title>Sandbox</title><body style='font-family:sans-serif;padding:16px'>"
                "<h1>Incident Judge sandbox</h1><ul>"
                "<li><a href='/slack/ui'>Slack UI</a></li>"
                "<li><a href='/status/page_shoplab'>Public status page</a></li>"
                "<li><a href='/__admin/state'>Admin state (JSON)</a></li>"
                "<li><a href='/__docs'>API docs</a></li></ul>")

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8900)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--reset", action="store_true", help="wipe sandbox state on start")
    args = parser.parse_args()

    import uvicorn

    app = create_app(args.db, base_url=f"http://{args.host}:{args.port}")
    if args.reset:
        app.state.admin.reset(None)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
