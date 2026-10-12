"""AC-P1-08: SQLite store — migrations, DAOs, WAL recovery, restart persistence."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

from core.db import Store


def test_migration_idempotent(tmp_path):
    db = tmp_path / "protox.db"
    store = Store(db)
    store.close()
    store2 = Store(db)  # re-run must be a no-op
    rows = store2.conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert [r[0] for r in rows] == ["m001_initial"]
    store2.close()


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
