-- m003: conversion_runs vocabulary fix (F-029).
-- m001's reason_code CHECK omitted 'codegen-static-gate', which the converter
-- agent raises when the generated adapter fails the static gate — closing such
-- a run raised IntegrityError and the run stayed open. The outcome CHECK also
-- lacked 'error', so convert_cli's unexpected-exception path could not record
-- its terminal state either. SQLite cannot alter a CHECK constraint in place,
-- so the table is rebuilt (data preserved). Error runs carry reason_code NULL;
-- the exception detail lives in the workdir trace.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS conversion_runs_new (
  id            INTEGER PRIMARY KEY,
  package       TEXT    NOT NULL,
  workdir       TEXT    NOT NULL,
  trace_path    TEXT    NOT NULL,                         -- react_trace.jsonl (AC-F029-05)
  fix_cycles    INTEGER,
  judge_cycles  INTEGER,
  outcome       TEXT
                CHECK (outcome IN ('merged', 'rejected', 'error') OR outcome IS NULL),
  reason_code   TEXT                                      -- NULL on merge and on error
                CHECK (reason_code IN ('not-convertible', 'codegen-static-gate',
                                       'format-unhealable',
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
           WHEN outcome = 'error'    AND reason_code IS NULL     THEN 1
           WHEN outcome IS NULL      AND reason_code IS NULL     THEN 1
           ELSE 0 END)
);
INSERT INTO conversion_runs_new SELECT * FROM conversion_runs;
DROP TABLE conversion_runs;
ALTER TABLE conversion_runs_new RENAME TO conversion_runs;
CREATE INDEX IF NOT EXISTS idx_runs_outcome ON conversion_runs(outcome, started_ts);

PRAGMA foreign_keys = ON;
