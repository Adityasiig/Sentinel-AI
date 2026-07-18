"""SQLite state store.

Single-file, WAL-mode, on a persistent volume so state survives redeploys.
Phase 1 uses `hosts`, `probe_runs`, and `audit`. `incidents`/`actions` are
created now so the remediation phases slot in without a migration.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import settings

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    id        TEXT PRIMARY KEY,          -- hostname
    ip        TEXT NOT NULL,
    role      TEXT NOT NULL,             -- IVG | OPS | VOSS
    ssh_port  INTEGER NOT NULL DEFAULT 22,
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS probe_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id TEXT NOT NULL,
    probe   TEXT NOT NULL,
    status  TEXT NOT NULL,               -- ok | warn | crit | unknown
    value   TEXT,
    ts      REAL NOT NULL,
    FOREIGN KEY (host_id) REFERENCES hosts(id)
);
CREATE INDEX IF NOT EXISTS idx_probe_latest ON probe_runs (host_id, probe, ts DESC);

CREATE TABLE IF NOT EXISTS incidents (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    status     TEXT NOT NULL,            -- open | remediating | resolved | needs-human
    opened_ts  REAL NOT NULL,
    closed_ts  REAL
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER,
    playbook    TEXT,
    cmd         TEXT,
    destructive INTEGER NOT NULL DEFAULT 0,
    dry_run     INTEGER NOT NULL DEFAULT 1,
    approved_by TEXT,
    result      TEXT,
    before      TEXT,
    after       TEXT,
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,
    verb        TEXT NOT NULL,
    target      TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    ts          REAL NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=8000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def conn() -> sqlite3.Connection:
    """One connection per thread (FastAPI thread-pool + prober are separate)."""
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        c = _LOCAL.conn = _connect()
    return c


@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def init_db() -> None:
    with tx() as c:
        c.executescript(SCHEMA)


# ── writes ───────────────────────────────────────────────────────────────
def upsert_hosts(hosts: Iterable[dict]) -> None:
    with tx() as c:
        for h in hosts:
            c.execute(
                "INSERT INTO hosts (id, ip, role, ssh_port, meta_json) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET ip=excluded.ip, role=excluded.role, "
                "ssh_port=excluded.ssh_port, meta_json=excluded.meta_json",
                (h["hostname"], h["ip"], h["role"], h.get("ssh_port", 22),
                 json.dumps(h.get("meta", {}))),
            )


def record_probes(host_id: str, results: list[dict], ts: float | None = None) -> None:
    ts = ts if ts is not None else time.time()
    with tx() as c:
        c.executemany(
            "INSERT INTO probe_runs (host_id, probe, status, value, ts) VALUES (?,?,?,?,?)",
            [(host_id, r["probe"], r["status"], str(r.get("value", "")), ts) for r in results],
        )


def audit(actor: str, verb: str, target: str = "", detail: dict | None = None) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO audit (actor, verb, target, detail_json, ts) VALUES (?,?,?,?,?)",
            (actor, verb, target, json.dumps(detail or {}), time.time()),
        )


# ── incidents / advisories (Phase 2) ─────────────────────────────────────
def open_incident(host_id: str, kind: str, status: str = "open") -> tuple[int, bool]:
    """Idempotently open an incident. Returns (incident_id, newly_created).

    One live incident per (host, kind): a failure that persists across sweeps
    does not spawn a new incident every 60s.
    """
    row = conn().execute(
        "SELECT id FROM incidents WHERE host_id=? AND kind=? AND status!='resolved' "
        "ORDER BY id DESC LIMIT 1",
        (host_id, kind),
    ).fetchone()
    if row:
        return row["id"], False
    with tx() as c:
        cur = c.execute(
            "INSERT INTO incidents (host_id, kind, status, opened_ts) VALUES (?,?,?,?)",
            (host_id, kind, status, time.time()),
        )
        return cur.lastrowid, True


def record_advisory(incident_id: int, playbook_id: str, cmd: str, destructive: bool) -> None:
    """Snapshot the proposed remediation at detection time (dry_run, never run)."""
    with tx() as c:
        c.execute(
            "INSERT INTO actions (incident_id, playbook, cmd, destructive, dry_run, result, ts) "
            "VALUES (?,?,?,?,1,'proposed',?)",
            (incident_id, playbook_id, cmd, 1 if destructive else 0, time.time()),
        )


def resolve_stale_incidents(host_id: str, active_kinds: set[str]) -> list[str]:
    """Resolve any open incident for this host whose condition no longer fires."""
    rows = conn().execute(
        "SELECT id, kind FROM incidents WHERE host_id=? AND status!='resolved'",
        (host_id,),
    ).fetchall()
    resolved: list[str] = []
    now = time.time()
    with tx() as c:
        for r in rows:
            if r["kind"] not in active_kinds:
                c.execute(
                    "UPDATE incidents SET status='resolved', closed_ts=? WHERE id=?",
                    (now, r["id"]),
                )
                resolved.append(r["kind"])
    return resolved


def open_incidents() -> list[dict]:
    """All live incidents joined with host info, newest first."""
    rows = conn().execute(
        "SELECT i.id, i.host_id, i.kind, i.status, i.opened_ts, h.ip, h.role "
        "FROM incidents i JOIN hosts h ON h.id = i.host_id "
        "WHERE i.status != 'resolved' ORDER BY i.opened_ts DESC",
    ).fetchall()
    return [_row(r) for r in rows]


# ── reads ────────────────────────────────────────────────────────────────
def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def all_hosts() -> list[dict]:
    rows = conn().execute("SELECT * FROM hosts ORDER BY role, id").fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        out.append(d)
    return out


def latest_probes(host_id: str) -> list[dict]:
    rows = conn().execute(
        "SELECT probe, status, value, ts FROM probe_runs p "
        "WHERE host_id=? AND ts=(SELECT MAX(ts) FROM probe_runs WHERE host_id=p.host_id AND probe=p.probe) "
        "GROUP BY probe ORDER BY probe",
        (host_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def probe_history(host_id: str, probe: str, limit: int = 100) -> list[dict]:
    rows = conn().execute(
        "SELECT status, value, ts FROM probe_runs WHERE host_id=? AND probe=? ORDER BY ts DESC LIMIT ?",
        (host_id, probe, limit),
    ).fetchall()
    return [_row(r) for r in rows]
