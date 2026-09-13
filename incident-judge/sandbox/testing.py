"""Run the sandbox on a real socket inside the current process (tests, eval runner)."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import uvicorn

from sandbox.app import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_sandbox(db_path: Path | str, port: int | None = None) -> Iterator[str]:
    port = port or free_port()
    base = f"http://127.0.0.1:{port}"
    app = create_app(db_path, base_url=base)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:  # pragma: no cover
        server.should_exit = True
        raise RuntimeError("sandbox did not start")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        app.state.db.close()
