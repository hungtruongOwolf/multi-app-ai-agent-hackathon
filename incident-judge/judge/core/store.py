"""Durable agent state (SQLite, WAL). The single source of truth for incidents, the outbox,
decisions, approvals, executions and outcomes. Survives crashes; the agent resumes from here."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from judge.core.models import (
    Approval,
    Decision,
    Incident,
    IncidentState,
    Outcome,
    OutcomeResult,
    Plan,
    Signal,
    now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, incident_key TEXT NOT NULL, trial_id TEXT, state TEXT NOT NULL,
  environment TEXT, data TEXT NOT NULL, created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_inc_key ON incidents(incident_key, state);

CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY, incident_id TEXT, fingerprint TEXT, service TEXT, source TEXT,
  data TEXT NOT NULL, ts TEXT
);
CREATE INDEX IF NOT EXISTS ix_sig_inc ON signals(incident_id);

CREATE TABLE IF NOT EXISTS steps (
  step_id TEXT PRIMARY KEY, incident_id TEXT, kind TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL, external_ref TEXT, attempts INTEGER DEFAULT 0, decision_id TEXT,
  error TEXT, created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, incident_id TEXT, intent TEXT, result TEXT, data TEXT NOT NULL, ts TEXT
);
CREATE INDEX IF NOT EXISTS ix_dec_inc ON decisions(incident_id);

CREATE TABLE IF NOT EXISTS external_writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, decision_id TEXT, app TEXT, op TEXT,
  ref TEXT, idempotency_key TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY, incident_id TEXT, plan_hash TEXT, status TEXT, data TEXT NOT NULL, ts TEXT
);

CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, incident_id TEXT, kind TEXT, service TEXT,
  action TEXT, params TEXT, prev_state TEXT, result TEXT, decision_id TEXT, approval_id TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, incident_id TEXT, result TEXT, samples TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY, incident_id TEXT, kind TEXT, subject_hash TEXT, verdict TEXT,
  valid INTEGER, data TEXT NOT NULL, ts TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
  outcome_id TEXT PRIMARY KEY, runbook_id TEXT, incident_id TEXT, plan_id TEXT, action TEXT,
  result TEXT, trial_id TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS locks (
  name TEXT PRIMARY KEY, holder TEXT, expires_at TEXT
);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY, v TEXT
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(args))

    # ------------------------------------------------------------ incidents

    def save_incident(self, inc: Incident) -> None:
        inc.updated_at = now()
        self._exec(
            """INSERT INTO incidents(id, incident_key, trial_id, state, environment, data, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET state=excluded.state, data=excluded.data, updated_at=excluded.updated_at""",
            (inc.id, inc.incident_key, inc.trial_id, inc.state.value, inc.environment.value,
             inc.model_dump_json(), _iso(inc.created_at), _iso(inc.updated_at)),
        )

    def get_incident(self, incident_id: str) -> Incident | None:
        row = self._exec("SELECT data FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return Incident.model_validate_json(row["data"]) if row else None

    def open_incident_by_key(self, key: str) -> Incident | None:
        row = self._exec(
            "SELECT data FROM incidents WHERE incident_key=? AND state NOT IN ('RESOLVED','CLOSED') "
            "ORDER BY created_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        return Incident.model_validate_json(row["data"]) if row else None

    def incidents(self, states: list[IncidentState] | None = None) -> list[Incident]:
        if states:
            q = ",".join("?" for _ in states)
            rows = self._exec(f"SELECT data FROM incidents WHERE state IN ({q}) ORDER BY created_at",
                              [s.value for s in states]).fetchall()
        else:
            rows = self._exec("SELECT data FROM incidents ORDER BY created_at").fetchall()
        return [Incident.model_validate_json(r["data"]) for r in rows]

    def transition(self, inc: Incident, to: IncidentState) -> Incident:
        if inc.state != to:
            self.put_kv(f"transition:{inc.id}:{now().timestamp()}", f"{inc.state}->{to}")
            inc.state = to
            inc.state_entered_at = now()
        self.save_incident(inc)
        return inc

    # ------------------------------------------------------------ signals

    def add_signal(self, sig: Signal, incident_id: str | None) -> None:
        self._exec(
            "INSERT OR REPLACE INTO signals(signal_id, incident_id, fingerprint, service, source, data, ts) VALUES(?,?,?,?,?,?,?)",
            (sig.signal_id, incident_id, sig.fingerprint, sig.service, sig.source, sig.model_dump_json(), _iso(now())),
        )

    def signals_for(self, incident_id: str) -> list[Signal]:
        rows = self._exec("SELECT data FROM signals WHERE incident_id=? ORDER BY ts", (incident_id,)).fetchall()
        return [Signal.model_validate_json(r["data"]) for r in rows]

    # ------------------------------------------------------------ outbox steps

    def get_step(self, idempotency_key: str) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM steps WHERE idempotency_key=?", (idempotency_key,)).fetchone()

    def upsert_step(self, *, step_id: str, incident_id: str | None, kind: str, key: str, status: str,
                    external_ref: str | None = None, decision_id: str | None = None, error: str | None = None,
                    bump_attempt: bool = False) -> None:
        ts = _iso(now())
        self._exec(
            """INSERT INTO steps(step_id, incident_id, kind, idempotency_key, status, external_ref, attempts,
                                 decision_id, error, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(idempotency_key) DO UPDATE SET status=excluded.status,
                 external_ref=COALESCE(excluded.external_ref, steps.external_ref),
                 attempts=steps.attempts + ?, decision_id=COALESCE(excluded.decision_id, steps.decision_id),
                 error=excluded.error, updated_at=excluded.updated_at""",
            (step_id, incident_id, kind, key, status, external_ref, 1 if bump_attempt else 0, decision_id, error,
             ts, ts, 1 if bump_attempt else 0),
        )

    def steps_in_flight(self) -> list[sqlite3.Row]:
        return self._exec("SELECT * FROM steps WHERE status='in_flight'").fetchall()

    # ------------------------------------------------------------ audit

    def add_decision(self, d: Decision) -> None:
        self._exec(
            "INSERT OR REPLACE INTO decisions(decision_id, incident_id, intent, result, data, ts) VALUES(?,?,?,?,?,?)",
            (d.decision_id, d.incident_id, d.intent, d.result.value, d.model_dump_json(), _iso(d.ts)),
        )

    def decisions(self, incident_id: str | None = None) -> list[Decision]:
        if incident_id:
            rows = self._exec("SELECT data FROM decisions WHERE incident_id=? ORDER BY ts", (incident_id,)).fetchall()
        else:
            rows = self._exec("SELECT data FROM decisions ORDER BY ts").fetchall()
        return [Decision.model_validate_json(r["data"]) for r in rows]

    def add_external_write(self, *, incident_id: str | None, decision_id: str | None, app: str, op: str,
                           ref: str | None, idempotency_key: str | None) -> None:
        self._exec(
            "INSERT INTO external_writes(incident_id, decision_id, app, op, ref, idempotency_key, ts) VALUES(?,?,?,?,?,?,?)",
            (incident_id, decision_id, app, op, ref, idempotency_key, _iso(now())),
        )

    def external_writes(self) -> list[dict]:
        return [dict(r) for r in self._exec("SELECT * FROM external_writes ORDER BY id").fetchall()]

    # ------------------------------------------------------------ plans / executions / verifications

    def save_plan(self, plan: Plan, status: str) -> None:
        self._exec(
            """INSERT INTO plans(plan_id, incident_id, plan_hash, status, data, ts) VALUES(?,?,?,?,?,?)
               ON CONFLICT(plan_id) DO UPDATE SET status=excluded.status, data=excluded.data, plan_hash=excluded.plan_hash""",
            (plan.plan_id, plan.incident_id, plan.plan_hash, status, plan.model_dump_json(), _iso(now())),
        )

    def get_plan(self, plan_id: str) -> tuple[Plan, str] | None:
        row = self._exec("SELECT data, status FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return (Plan.model_validate_json(row["data"]), row["status"]) if row else None

    def active_plan(self, incident_id: str) -> tuple[Plan, str] | None:
        row = self._exec(
            "SELECT data, status FROM plans WHERE incident_id=? AND status NOT IN ('done','cancelled','rolled_back','failed') "
            "ORDER BY ts DESC LIMIT 1",
            (incident_id,),
        ).fetchone()
        return (Plan.model_validate_json(row["data"]), row["status"]) if row else None

    def add_execution(self, *, plan: Plan, kind: str, result: str, decision_id: str | None,
                      approval_id: str | None) -> None:
        self._exec(
            """INSERT INTO executions(plan_id, incident_id, kind, service, action, params, prev_state, result,
                                      decision_id, approval_id, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (plan.plan_id, plan.incident_id, kind, plan.target_service, plan.action, json.dumps(plan.params),
             json.dumps(plan.prev_state), result, decision_id, approval_id, _iso(now())),
        )

    def executions(self, service: str | None = None, since: datetime | None = None) -> list[dict]:
        sql, args = "SELECT * FROM executions WHERE 1=1", []
        if service:
            sql += " AND service=?"
            args.append(service)
        if since:
            sql += " AND ts>=?"
            args.append(_iso(since))
        return [dict(r) for r in self._exec(sql + " ORDER BY id", args).fetchall()]

    def add_verification(self, plan_id: str, incident_id: str, result: str, samples: list[dict]) -> None:
        self._exec(
            "INSERT INTO verifications(plan_id, incident_id, result, samples, ts) VALUES(?,?,?,?,?)",
            (plan_id, incident_id, result, json.dumps(samples, default=str), _iso(now())),
        )

    def verifications(self, incident_id: str) -> list[dict]:
        return [dict(r) for r in self._exec(
            "SELECT plan_id, result, samples, ts FROM verifications WHERE incident_id=? ORDER BY id",
            (incident_id,)).fetchall()]

    # ------------------------------------------------------------ approvals

    def add_approval(self, a: Approval) -> None:
        self._exec(
            "INSERT OR REPLACE INTO approvals(approval_id, incident_id, kind, subject_hash, verdict, valid, data, ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (a.approval_id, a.incident_id, a.kind, a.subject_hash, a.verdict, int(a.valid), a.model_dump_json(),
             _iso(a.ts)),
        )

    def approvals(self, incident_id: str, kind: str | None = None) -> list[Approval]:
        sql, args = "SELECT data FROM approvals WHERE incident_id=?", [incident_id]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return [Approval.model_validate_json(r["data"]) for r in self._exec(sql + " ORDER BY ts", args).fetchall()]

    # ------------------------------------------------------------ outcomes

    def add_outcome(self, o: Outcome) -> None:
        self._exec(
            "INSERT OR IGNORE INTO outcomes(outcome_id, runbook_id, incident_id, plan_id, action, result, trial_id, ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (o.outcome_id, o.runbook_id, o.incident_id, o.plan_id, o.action, o.result.value, o.trial_id, _iso(o.ts)),
        )

    def outcomes(self, runbook_id: str | None = None) -> list[Outcome]:
        if runbook_id:
            rows = self._exec("SELECT * FROM outcomes WHERE runbook_id=? ORDER BY ts", (runbook_id,)).fetchall()
        else:
            rows = self._exec("SELECT * FROM outcomes ORDER BY ts").fetchall()
        return [Outcome(outcome_id=r["outcome_id"], runbook_id=r["runbook_id"], incident_id=r["incident_id"],
                        plan_id=r["plan_id"], action=r["action"], result=OutcomeResult(r["result"]),
                        trial_id=r["trial_id"], ts=datetime.fromisoformat(r["ts"])) for r in rows]

    # ------------------------------------------------------------ locks (lease-based)

    def acquire_lock(self, name: str, holder: str, lease_s: float) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT holder, expires_at FROM locks WHERE name=?", (name,)).fetchone()
            t = now()
            if row and row["holder"] != holder and datetime.fromisoformat(row["expires_at"]) > t:
                return False
            self.conn.execute(
                "INSERT INTO locks(name, holder, expires_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, expires_at=excluded.expires_at",
                (name, holder, _iso(t + timedelta(seconds=lease_s))),
            )
            return True

    def release_lock(self, name: str, holder: str) -> None:
        self._exec("DELETE FROM locks WHERE name=? AND holder=?", (name, holder))

    def lock_holder(self, name: str) -> str | None:
        row = self._exec("SELECT holder, expires_at FROM locks WHERE name=?", (name,)).fetchone()
        if row and datetime.fromisoformat(row["expires_at"]) > now():
            return row["holder"]
        return None

    # ------------------------------------------------------------ kv (kill switch, breakers, cursors)

    def put_kv(self, k: str, v: Any) -> None:
        self._exec("INSERT INTO kv(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def get_kv(self, k: str, default: Any = None) -> Any:
        row = self._exec("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row["v"]) if row else default

    def kill_switch(self) -> bool:
        return bool(self.get_kv("kill_switch", False))
