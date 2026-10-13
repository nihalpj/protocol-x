"""SQLite state store (plan §3.4): system of record for registry state,
audit events, conversion runs, repair incidents, and converter checkpoints.

- stdlib sqlite3, WAL mode, foreign_keys=ON, busy_timeout=5000
- migrations/mNNN_*.sql applied in filename order, recorded in schema_migrations
- typed DAOs are the ONLY SQL call sites in the codebase (plan §3.4)
- server-side operational state only: no secret columns exist (INV-7), and
  tool statelessness (INV-5) is unaffected
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path = _MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations in filename order. Idempotent (AC-P1-08)."""
    applied: set[str] = set()
    if _table_exists(conn, "schema_migrations"):
        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
    done: list[str] = []
    for sql_file in sorted(migrations_dir.glob("m*.sql")):
        version = sql_file.stem
        if version in applied:
            continue
        # autocommit mode (isolation_level=None): executescript manages its own
        # transaction for the DDL; the ledger insert follows immediately.
        conn.executescript(sql_file.read_text())
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_ts) VALUES (?, ?)",
            (version, time.time()),
        )
        done.append(version)
    return done


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _now() -> float:
    return time.time()


class ToolRepo:
    """Registry state: live / draft / quarantined, survives restarts."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(
        self,
        name: str,
        state: str,
        adapter_module: str,
        backend: str = "python",
        version_id: int | None = None,
    ) -> None:
        now = _now()
        self.conn.execute(
            """INSERT INTO tools(name, state, adapter_module, backend, version_id, created_ts, updated_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 state=excluded.state, adapter_module=excluded.adapter_module,
                 backend=excluded.backend, version_id=excluded.version_id,
                 updated_ts=excluded.updated_ts""",
            (name, state, adapter_module, backend, version_id, now, now),
        )

    def get(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tools WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def set_state(self, name: str, state: str) -> None:
        self.conn.execute(
            "UPDATE tools SET state=?, updated_ts=? WHERE name=?", (state, _now(), name)
        )

    def list(self, state: str | None = None) -> list[dict]:
        if state:
            rows = self.conn.execute(
                "SELECT * FROM tools WHERE state=? ORDER BY name", (state,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM tools ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def delete(self, name: str) -> None:
        self.conn.execute("DELETE FROM tools WHERE name=?", (name,))


class VersionRepo:
    """adapter_versions rows; parent_id is the rollback chain (F-030)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(
        self,
        adapter_module: str,
        version_hash: str,
        source: str,
        parent_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO adapter_versions(adapter_module, version_hash, source, parent_id, created_ts)
               VALUES (?, ?, ?, ?, ?)""",
            (adapter_module, version_hash, source, parent_id, _now()),
        )
        return int(cur.lastrowid)

    def latest(self, adapter_module: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM adapter_versions WHERE adapter_module=? ORDER BY created_ts DESC, id DESC LIMIT 1",
            (adapter_module,),
        ).fetchone()
        return dict(row) if row else None

    def get(self, version_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM adapter_versions WHERE id=?", (version_id,)
        ).fetchone()
        return dict(row) if row else None

    def list(self, adapter_module: str | None = None) -> list[dict]:
        if adapter_module:
            rows = self.conn.execute(
                "SELECT * FROM adapter_versions WHERE adapter_module=? ORDER BY created_ts",
                (adapter_module,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM adapter_versions ORDER BY created_ts"
            ).fetchall()
        return [dict(r) for r in rows]


class AuditRepo:
    """audit_events ledger (queryable); JSONL export lives in core/audit.py."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append(self, kind: str, actor: str, payload: dict[str, Any], ts: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO audit_events(ts, kind, actor, payload_json) VALUES (?, ?, ?, ?)",
            (ts if ts is not None else _now(), kind, actor, json.dumps(payload, default=str)),
        )
        return int(cur.lastrowid)

    def query(
        self,
        kind: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 500,
    ) -> list[dict]:
        sql, params = "SELECT * FROM audit_events WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if since is not None:
            sql += " AND ts>=?"
            params.append(since)
        if until is not None:
            sql += " AND ts<=?"
            params.append(until)
        sql += " ORDER BY ts, id LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {**dict(r), "payload": json.loads(r["payload_json"])}
            for r in rows
        ]


class RunRepo:
    """conversion_runs: one row per converter run (F-029)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def open(self, package: str, workdir: str, trace_path: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO conversion_runs(package, workdir, trace_path, started_ts) VALUES (?, ?, ?, ?)",
            (package, workdir, trace_path, _now()),
        )
        return int(cur.lastrowid)

    def close(
        self,
        run_id: int,
        outcome: str,
        reason_code: str | None = None,
        fix_cycles: int | None = None,
        judge_cycles: int | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE conversion_runs
               SET outcome=?, reason_code=?, fix_cycles=?, judge_cycles=?, ended_ts=?
               WHERE id=?""",
            (outcome, reason_code, fix_cycles, judge_cycles, _now(), run_id),
        )

    def get(self, run_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM conversion_runs WHERE id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def query(
        self,
        outcome: str | None = None,
        reason_code: str | None = None,
        package: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql, params = "SELECT * FROM conversion_runs WHERE 1=1", []
        if outcome:
            sql += " AND outcome=?"
            params.append(outcome)
        if reason_code:
            sql += " AND reason_code=?"
            params.append(reason_code)
        if package:
            sql += " AND package=?"
            params.append(package)
        sql += " ORDER BY started_ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def latest_open(self, package: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM conversion_runs WHERE package=? AND outcome IS NULL ORDER BY started_ts DESC LIMIT 1",
            (package,),
        ).fetchone()
        return dict(row) if row else None


class IncidentRepo:
    """repair_incidents stream (F-030); breaker counts via SQL windows."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(
        self,
        tool: str,
        error_class: str,
        input_json: str | None = None,
        adapter_version_id: int | None = None,
        state: str = "healing",
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO repair_incidents(tool, error_class, input_json, adapter_version_id, attempts, state, window_start_ts)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (tool, error_class, input_json, adapter_version_id, state, _now()),
        )
        return int(cur.lastrowid)

    def get(self, incident_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM repair_incidents WHERE id=?", (incident_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_state(self, incident_id: int, state: str, attempts: int | None = None) -> None:
        if attempts is None:
            self.conn.execute(
                "UPDATE repair_incidents SET state=? WHERE id=?", (state, incident_id)
            )
        else:
            self.conn.execute(
                "UPDATE repair_incidents SET state=?, attempts=? WHERE id=?",
                (state, attempts, incident_id),
            )

    def count_same_class(self, tool: str, error_class: str, window_s: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM repair_incidents WHERE tool=? AND error_class=? AND window_start_ts > ?",
            (tool, error_class, _now() - window_s),
        ).fetchone()
        return int(row[0])

    def count_failed_repairs(
        self, tool: str, window_s: float = 86400.0, since_ts: float | None = None
    ) -> int:
        """Circuit-breaker count: failed repairs (rolled-back) in the window.
        `since_ts` (console re-enable) raises the floor of the window."""
        threshold = _now() - window_s
        if since_ts is not None:
            threshold = max(threshold, since_ts)
        row = self.conn.execute(
            "SELECT COUNT(*) FROM repair_incidents WHERE tool=? AND state='rolled-back' AND window_start_ts > ?",
            (tool, threshold),
        ).fetchone()
        return int(row[0])

    def list(self, tool: str | None = None, state: str | None = None, limit: int = 200) -> list[dict]:
        sql, params = "SELECT * FROM repair_incidents WHERE 1=1", []
        if tool:
            sql += " AND tool=?"
            params.append(tool)
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY window_start_ts DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]


class CheckpointRepo:
    """checkpoints: converter resume state (E-13), survives restarts (AC-P7-05)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, run_id: int, stage: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO checkpoints(run_id, stage, payload_json, ts) VALUES (?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                 stage=excluded.stage, payload_json=excluded.payload_json, ts=excluded.ts""",
            (run_id, stage, json.dumps(payload, default=str), _now()),
        )

    def load(self, run_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM checkpoints WHERE run_id=?", (run_id,)
        ).fetchone()
        if not row:
            return None
        return {**dict(row), "payload": json.loads(row["payload_json"])}


class Store:
    """One handle over the DB: connection + all DAOs."""

    def __init__(self, db_path: str | Path, migrations_dir: Path = _MIGRATIONS_DIR):
        self.conn = connect(db_path)
        migrate(self.conn, migrations_dir)
        self.tools = ToolRepo(self.conn)
        self.versions = VersionRepo(self.conn)
        self.audit = AuditRepo(self.conn)
        self.runs = RunRepo(self.conn)
        self.incidents = IncidentRepo(self.conn)
        self.checkpoints = CheckpointRepo(self.conn)

    def close(self) -> None:
        self.conn.close()
