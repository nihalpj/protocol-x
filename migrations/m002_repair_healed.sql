-- m002: repair_incidents gains the 'healed' terminal state (F-030).
-- m001 shipped CHECK (state IN ('healing','rolled-back','quarantined'));
-- a successful repair had no honest terminal state. SQLite cannot alter a
-- CHECK constraint in place, so the table is rebuilt (data preserved).

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS repair_incidents_new (
  id                  INTEGER PRIMARY KEY,
  tool                TEXT    NOT NULL REFERENCES tools(name),
  error_class         TEXT    NOT NULL,
  input_json          TEXT,
  adapter_version_id  INTEGER REFERENCES adapter_versions(id),
  attempts            INTEGER NOT NULL DEFAULT 0,
  state               TEXT    NOT NULL
                      CHECK (state IN ('healing', 'healed', 'rolled-back', 'quarantined')),
  window_start_ts     REAL    NOT NULL
);
INSERT INTO repair_incidents_new SELECT * FROM repair_incidents;
DROP TABLE repair_incidents;
ALTER TABLE repair_incidents_new RENAME TO repair_incidents;
CREATE INDEX IF NOT EXISTS idx_incidents_tool_window
  ON repair_incidents(tool, window_start_ts);

PRAGMA foreign_keys = ON;
