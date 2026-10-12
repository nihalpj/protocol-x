-- =============================================================================
-- Protocol-X Tool MCP — m001_initial.sql
-- Implementation plan v2.1 §3.4 / Phase 1 task 13
--
-- SQLite system of record (stdlib sqlite3, WAL mode — runtime PRAGMAs are set
-- by core/db.py: journal_mode=WAL, foreign_keys=ON, busy_timeout=5000).
--
-- Scope: server-side OPERATIONAL state only — registry state, audit events,
-- conversion runs, repair incidents, converter checkpoints.
--   * INV-5: tool statelessness unaffected (no per-tool state here)
--   * INV-7: no secret columns exist — CI grep asserts no secret is written
--   * INV-8: embedded, zero external service
-- All access goes through typed DAOs in core/db.py — no raw SQL elsewhere.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- adapter_versions: every adapter file version; parent_id is the rollback chain
-- (F-030 self-repair rolls back to last-known-good via parent link).
-- Created before `tools` because tools.version_id references it.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS adapter_versions (
  id              INTEGER PRIMARY KEY,
  adapter_module  TEXT    NOT NULL,                       -- e.g. "images_pillow"
  version_hash    TEXT    NOT NULL,                       -- sha256 of adapter file
  source          TEXT    NOT NULL
                  CHECK (source IN ('built-in', 'generated', 'manual')),
  parent_id       INTEGER REFERENCES adapter_versions(id), -- last-known-good link
  created_ts      REAL    NOT NULL                        -- unix epoch seconds
);
CREATE INDEX IF NOT EXISTS idx_versions_module
  ON adapter_versions(adapter_module, created_ts);

-- -----------------------------------------------------------------------------
-- tools: registry state — survives restarts (AC-P1-08), drives the console's
-- Live/Draft/Quarantined badges (P5) and the F-030 quarantine (P8).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tools (
  name            TEXT    PRIMARY KEY,                    -- ToolSpec.name, snake_case
  state           TEXT    NOT NULL
                  CHECK (state IN ('live', 'draft', 'quarantined')),
  adapter_module  TEXT    NOT NULL,
  backend         TEXT    NOT NULL DEFAULT 'python'
                  CHECK (backend IN ('python', 'node', 'go-bin', 'rust-bin')),
  version_id      INTEGER REFERENCES adapter_versions(id),
  created_ts      REAL    NOT NULL,
  updated_ts      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tools_state ON tools(state);

-- -----------------------------------------------------------------------------
-- audit_events: the F-015 governance ledger, queryable. core/audit.py dual-writes
-- the JSONL line for humans/tests (Hermes-style trail); this table is the
-- system of record. payload_json holds the per-kind payload, e.g. the
-- JudgeVerdict {verdict, findings, model, prompt_hash, rubric_version, ts}.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_events (
  id            INTEGER PRIMARY KEY,
  ts            REAL    NOT NULL,
  kind          TEXT    NOT NULL
                CHECK (kind IN ('verdict', 'merge', 'repair', 'quarantine',
                                'config', 'install', 'rejection')),
  actor         TEXT    NOT NULL,                         -- e.g. "judge", "converter", "operator"
  payload_json  TEXT    NOT NULL                          -- JSON, validated by DAOs
);
CREATE INDEX IF NOT EXISTS idx_audit_kind_ts ON audit_events(kind, ts);

-- -----------------------------------------------------------------------------
-- conversion_runs: one row per converter-agent run (F-029). Opened at run start,
-- closed with outcome + reason_code at merge or rejection. reason_code uses the
-- F-016 vocabulary; rejection.json in the workdir is the file twin of this row.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversion_runs (
  id            INTEGER PRIMARY KEY,
  package       TEXT    NOT NULL,
  workdir       TEXT    NOT NULL,
  trace_path    TEXT    NOT NULL,                         -- react_trace.jsonl (AC-F029-05)
  fix_cycles    INTEGER,
  judge_cycles  INTEGER,
  outcome       TEXT
                CHECK (outcome IN ('merged', 'rejected') OR outcome IS NULL),
  reason_code   TEXT                                      -- NULL on merge
                CHECK (reason_code IN ('not-convertible', 'format-unhealable',
                                       'judge-blocked-unrepairable',
                                       'judge-blocked-exhausted')
                       OR reason_code IS NULL),
  started_ts    REAL    NOT NULL,
  ended_ts      REAL,
  -- cross-column invariant: a reason_code exists iff the run was rejected.
  -- CASE form because SQLite CHECK treats a NULL result as PASS.
  CHECK (CASE
           WHEN outcome = 'merged'   AND reason_code IS NULL     THEN 1
           WHEN outcome = 'rejected' AND reason_code IS NOT NULL THEN 1
           WHEN outcome IS NULL      AND reason_code IS NULL     THEN 1
           ELSE 0 END)
);
CREATE INDEX IF NOT EXISTS idx_runs_outcome ON conversion_runs(outcome, started_ts);

-- -----------------------------------------------------------------------------
-- repair_incidents: the F-030 stream. The circuit breaker counts same-class
-- failures over a 24 h window with SQL — state survives restarts (AC-P8-05):
--   SELECT COUNT(*) FROM repair_incidents
--   WHERE tool = :tool AND state != 'rolled-back'
--     AND window_start_ts > :now - 86400;
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS repair_incidents (
  id                  INTEGER PRIMARY KEY,
  tool                TEXT    NOT NULL REFERENCES tools(name),
  error_class         TEXT    NOT NULL,                   -- SANDBOX_TIMEOUT, ADAPTER_ERROR…
  input_json          TEXT,                               -- captured failing input (E-13 replay)
  adapter_version_id  INTEGER REFERENCES adapter_versions(id),
  attempts            INTEGER NOT NULL DEFAULT 0,
  state               TEXT    NOT NULL
                      CHECK (state IN ('healing', 'rolled-back', 'quarantined')),
  window_start_ts     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_tool_window
  ON repair_incidents(tool, window_start_ts);

-- -----------------------------------------------------------------------------
-- checkpoints: converter resume (E-13). The workdir checkpoint.json is retained
-- as fallback per the agent PRD contract; this row is the queryable twin and
-- survives workdir cleanup (AC-P7-05).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS checkpoints (
  run_id        INTEGER PRIMARY KEY REFERENCES conversion_runs(id),
  stage         TEXT    NOT NULL
                CHECK (stage IN ('imported', 'proposed', 'codegen', 'format', 'merged')),
  payload_json  TEXT    NOT NULL,
  ts            REAL    NOT NULL
);

-- -----------------------------------------------------------------------------
-- schema_migrations: the migration runner's ledger. core/db.py applies
-- migrations/mNNN_*.sql in filename order and records each here; re-runs are
-- no-ops (AC-P1-08).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT    PRIMARY KEY,                        -- e.g. "m001_initial"
  applied_ts  REAL    NOT NULL
);
