"""ShopLab topology and defaults shared by supervisor and services."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONTROL_TOKEN = "dev-control-token"
DEFAULT_VERSION = "1.4.1"
BAD_VERSION = "1.4.2"
KNOWN_VERSIONS = [DEFAULT_VERSION, BAD_VERSION]
DEFAULT_POOL_SIZE = 10

REQUEST_TIMEOUT_S = 1.0
POOL_ACQUIRE_TIMEOUT_S = 0.5

# seconds a request holds a DB connection doing non-query transaction work
HOLD_S = {"checkout": 0.25, "search": 0.05, "catalog": 0.03, "internal-batch": 0.1}

CANARY_USERS = [f"u{i:04d}" for i in range(1, 9)]  # the 8 users hit by pay_few_users
USER_POOL = [f"u{i:04d}" for i in range(1, 2001)]


@dataclass(frozen=True)
class ServiceDef:
    id: str            # identity used for control/metrics: "checkout", "checkout@staging"
    name: str          # service name (Sentry tag, metric label)
    environment: str   # production | staging
    offset: int        # port = port_base + offset
    routes: tuple[str, ...] = field(default_factory=tuple)

    def port(self, port_base: int) -> int:
        return port_base + self.offset


SERVICES: dict[str, ServiceDef] = {
    s.id: s
    for s in [
        ServiceDef("checkout", "checkout", "production", 1, ("/pay",)),
        ServiceDef("search", "search", "production", 2, ("/search",)),
        ServiceDef("catalog", "catalog", "production", 3, ("/products/{id}", "/profile/avatar")),
        ServiceDef("internal-batch", "internal-batch", "production", 4, ()),
        ServiceDef("checkout@staging", "checkout", "staging", 11, ("/pay",)),
    ]
}


def default_config(sd: ServiceDef) -> dict:
    flags = {"payment_v2": False} if sd.name == "checkout" else {}
    return {
        "service": sd.id,
        "environment": sd.environment,
        "flags": flags,
        "pool_size": DEFAULT_POOL_SIZE,
        "app_version": DEFAULT_VERSION,
        "known_versions": list(KNOWN_VERSIONS),
        "faults": {},
    }


def default_configs() -> dict[str, dict]:
    return {sid: default_config(sd) for sid, sd in SERVICES.items()}


def sandbox_dsn(sandbox_url: str, environment: str) -> str:
    u = urlparse(sandbox_url)
    project = 1 if environment == "production" else 2
    return f"{u.scheme}://sandboxkey@{u.hostname}:{u.port or 80}/{project}"


def data_dir(trial_id: str | None) -> Path:
    from judge.paths import runtime_dir

    base = Path(os.environ.get("SHOPLAB_DATA_DIR") or runtime_dir() / "shoplab")
    return base / (trial_id or "default")


def clone(obj):
    return copy.deepcopy(obj)
