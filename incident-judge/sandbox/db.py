"""Tiny JSON document store on SQLite for the sandbox. Volumes are small; filtering happens in Python."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, seq INTEGER NOT NULL,
  PRIMARY KEY (kind, id)
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str | float | int | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class DocStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def next(self, name: str) -> int:
        with self.lock:
            row = self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
            value = (row[0] if row else 0) + 1
            self.conn.execute(
                "INSERT INTO counters(name, value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, value),
            )
            return value

    def put(self, kind: str, id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute("SELECT seq FROM docs WHERE kind=? AND id=?", (kind, id)).fetchone()
            seq = row[0] if row else self.next("seq")
            self.conn.execute(
                "INSERT INTO docs(kind, id, data, seq) VALUES(?,?,?,?) "
                "ON CONFLICT(kind, id) DO UPDATE SET data=excluded.data",
                (kind, id, json.dumps(data, default=str), seq),
            )
        return data

    def get(self, kind: str, id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT data FROM docs WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, kind: str, id: str) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM docs WHERE kind=? AND id=?", (kind, id))
        return cur.rowcount > 0

    def all(self, kind: str, where: Callable[[dict[str, Any]], bool] | None = None) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT data FROM docs WHERE kind=? ORDER BY seq", (kind,)).fetchall()
        docs = [json.loads(r[0]) for r in rows]
        return [d for d in docs if where is None or where(d)]

    def wipe(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM docs")
            self.conn.execute("DELETE FROM counters")
