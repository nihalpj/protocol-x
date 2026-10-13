"""Audit ledger (F-016): dual-write façade.

Every governance event lands in `audit_events` (SQLite, system of record)
AND as one JSON line in audit.jsonl (human-readable Hermes-style trail).
Append-only, self-hosted (INV-8).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from core.db import AuditRepo

log = logging.getLogger("protox.audit")

_KINDS = ("verdict", "merge", "repair", "quarantine", "config", "install", "rejection")


class AuditLedger:
    def __init__(self, repo: AuditRepo, jsonl_path: str | Path):
        self.repo = repo
        self.jsonl_path = Path(jsonl_path)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, kind: str, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in _KINDS:
            raise ValueError(f"unknown audit kind: {kind}")
        ts = time.time()
        entry = {"ts": ts, "kind": kind, "actor": actor, "payload": payload}
        with self._lock:
            row_id = self.repo.append(kind, actor, payload, ts)
            with self.jsonl_path.open("a") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        log.debug("audit %s by %s: %s", kind, actor, json.dumps(payload, default=str)[:500])
        return {"id": row_id, **entry}

    def query(self, **kwargs) -> list[dict]:
        return self.repo.query(**kwargs)
