"""AC-P1-08: SQLite store — migrations, DAOs, WAL recovery, restart persistence."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from core.db import Store


def test_migration_idempotent(tmp_path):
    from core.db import _MIGRATIONS_DIR

    expected = sorted(p.stem for p in _MIGRATIONS_DIR.glob("m*.sql"))
    db = tmp_path / "protox.db"
    store = Store(db)
    store.close()
    store2 = Store(db)  # re-run must be a no-op
    rows = store2.conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert sorted(r[0] for r in rows) == expected
    store2.close()


def test_m002_upgrade_preserves_incidents(tmp_path):
    """A pre-m002 database upgrades in place: rows survive the table rebuild
    and the new 'healed' state becomes writable."""
    from core.db import _MIGRATIONS_DIR, connect

    db = tmp_path / "protox.db"
    conn = connect(db)
    conn.executescript((_MIGRATIONS_DIR / "m001_initial.sql").read_text())
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_ts) VALUES ('m001_initial', 1.0)"
    )
    conn.execute(
        "INSERT INTO tools(name, state, adapter_module, created_ts, updated_ts) "
        "VALUES ('t1', 'live', 'm1', 1.0, 1.0)"
    )
    conn.execute(
        "INSERT INTO repair_incidents(tool, error_class, state, window_start_ts) "
        "VALUES ('t1', 'ADAPTER_ERROR', 'rolled-back', 1.0)"
    )
    conn.close()

    store = Store(db)  # applies m002
    rows = store.incidents.list()
    assert len(rows) == 1 and rows[0]["state"] == "rolled-back"
    iid = store.incidents.add("t1", "ADAPTER_ERROR")
    store.incidents.set_state(iid, "healed", 1)
    assert store.incidents.get(iid)["state"] == "healed"
    store.close()


def test_m003_upgrade_preserves_runs(tmp_path):
    """A pre-m003 database upgrades in place: run rows survive the rebuild and
    the widened outcome/reason vocabulary becomes writable."""
    from core.db import _MIGRATIONS_DIR, connect

    db = tmp_path / "protox.db"
    conn = connect(db)
    conn.executescript((_MIGRATIONS_DIR / "m001_initial.sql").read_text())
    conn.executescript((_MIGRATIONS_DIR / "m002_repair_healed.sql").read_text())
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_ts) VALUES "
        "('m001_initial', 1.0), ('m002_repair_healed', 1.0)"
    )
    conn.execute(
        "INSERT INTO conversion_runs(package, workdir, trace_path, started_ts) "
        "VALUES ('pyfiglet', '/tmp/wd', '/tmp/wd/trace.jsonl', 1.0)"
    )
    conn.close()

    store = Store(db)  # applies m003
    run = store.runs.query(package="pyfiglet")[0]
    assert run["outcome"] is None
    # 'codegen-static-gate' rejection is now recordable
    store.runs.close(run["id"], "rejected", "codegen-static-gate")
    assert store.runs.get(run["id"])["reason_code"] == "codegen-static-gate"
    # unexpected-error terminal state: outcome 'error', reason_code NULL
    rid = store.runs.open("boom", "/tmp/wd2", "/tmp/wd2/trace.jsonl")
    store.runs.close(rid, "error")
    assert store.runs.get(rid)["outcome"] == "error"
    # vocabulary is still enforced
    rid2 = store.runs.open("bad", "/tmp/wd3", "/tmp/wd3/trace.jsonl")
    with pytest.raises(sqlite3.IntegrityError):
        store.runs.close(rid2, "error", "RuntimeError")
    store.close()


def test_dao_round_trips(tmp_path):
    store = Store(tmp_path / "protox.db")
    vid = store.versions.add("images_pillow", "sha256-x", "built-in")
    store.tools.upsert("image_info", "live", "images_pillow", "python", vid)
    tool = store.tools.get("image_info")
    assert tool["state"] == "live" and tool["version_id"] == vid

    event_id = store.audit.append("verdict", "judge", {"verdict": "pass", "model": "m"})
    events = store.audit.query(kind="verdict", since=time.time() - 10)
    assert events[0]["id"] == event_id
    assert events[0]["payload"]["verdict"] == "pass"

    run_id = store.runs.open("requests", "/tmp/wd", "/tmp/wd/trace.jsonl")
    store.checkpoints.save(run_id, "proposed", {"tools": ["a"]})
    store.runs.close(run_id, "rejected", "judge-blocked-exhausted", fix_cycles=1, judge_cycles=3)
    run = store.runs.query(reason_code="judge-blocked-exhausted")[0]
    assert run["id"] == run_id
    cp = store.checkpoints.load(run_id)
    assert cp["stage"] == "proposed" and cp["payload"]["tools"] == ["a"]

    inc_id = store.incidents.add("image_info", "ADAPTER_ERROR", '{"x":1}', vid)
    assert store.incidents.count_same_class("image_info", "ADAPTER_ERROR", 600) == 1
    store.incidents.set_state(inc_id, "rolled-back", attempts=2)
    assert store.incidents.count_failed_repairs("image_info") == 1
    store.close()


def test_quarantine_survives_restart(tmp_path):
    db = tmp_path / "protox.db"
    store = Store(db)
    store.tools.upsert("image_info", "live", "images_pillow")
    store.tools.set_state("image_info", "quarantined")
    store.close()
    store2 = Store(db)
    assert store2.tools.get("image_info")["state"] == "quarantined"
    store2.close()


def test_wal_recovery_after_sigkill(tmp_path):
    db = tmp_path / "protox.db"
    Store(db).close()
    script = (
        "import sys; sys.path.insert(0, '.');"
        "from core.db import connect, migrate; migrate(connect(sys.argv[1]));"
        "c = connect(sys.argv[1]);"
        "c.execute('BEGIN');"
        "c.execute(\"INSERT INTO audit_events(ts,kind,actor,payload_json) VALUES (1,'config','x','{}')\");"
        "import os, signal, time; time.sleep(0.2); os.kill(os.getpid(), signal.SIGKILL)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script, str(db)], cwd=_root())
    proc.wait(timeout=15)
    assert proc.returncode == -signal.SIGKILL
    store = Store(db)
    integrity = store.conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"
    store.close()


def test_audit_dual_write(tmp_path):
    store = Store(tmp_path / "protox.db")
    jsonl = tmp_path / "audit.jsonl"
    from core.audit import AuditLedger

    ledger = AuditLedger(store.audit, jsonl)
    entry = ledger.record("merge", "converter", {"adapter": "x"})
    line = json.loads(jsonl.read_text().strip())
    assert line["kind"] == "merge" and line["payload"] == {"adapter": "x"}
    rows = store.audit.query(kind="merge", since=entry["ts"] - 1, until=entry["ts"] + 1)
    assert len(rows) == 1
    store.close()


def _root():
    import core

    return os.path.dirname(os.path.dirname(os.path.abspath(core.__file__)))
