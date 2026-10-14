-- m007: converter test-stage persistence.
-- The converter agent checkpoints the 'tested' stage (adapter tool-testing
-- loop, MAX_TEST_CYCLES fix rounds before the judge), but m001's stage CHECK
-- omitted it — every production run with a tester wired died with
-- IntegrityError at that checkpoint and closed as 'error'. SQLite cannot
-- alter a CHECK constraint in place, so the table is rebuilt (data
-- preserved). conversion_runs also gains test_cycles so the run record shows
-- how many test/fix rounds the adapter needed.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS checkpoints_new (
  run_id        INTEGER PRIMARY KEY REFERENCES conversion_runs(id),
  stage         TEXT    NOT NULL
                CHECK (stage IN ('imported', 'proposed', 'codegen', 'format',
                                 'tested', 'merged')),
  payload_json  TEXT    NOT NULL,
  ts            REAL    NOT NULL
);
INSERT INTO checkpoints_new SELECT * FROM checkpoints;
DROP TABLE checkpoints;
ALTER TABLE checkpoints_new RENAME TO checkpoints;

ALTER TABLE conversion_runs ADD COLUMN test_cycles INTEGER;

PRAGMA foreign_keys = ON;
