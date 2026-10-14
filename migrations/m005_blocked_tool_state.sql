-- m005: judge-blocked tool state.
-- m001's tools.state CHECK had no terminal state for judge-blocked drafts:
-- judge_and_merge returned 'blocked' without touching the rows, so failed
-- generations stayed 'draft' forever and resurfaced in the console drafts
-- tray on every restart (registry discovery revives the stale rows). SQLite
-- cannot alter a CHECK constraint in place, so the table is rebuilt (data
-- preserved); foreign_keys is OFF because tools.version_id references
-- adapter_versions(id).

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS tools_new (
  name            TEXT    PRIMARY KEY,
  state           TEXT    NOT NULL
                  CHECK (state IN ('live', 'draft', 'quarantined', 'blocked')),
  adapter_module  TEXT    NOT NULL,
  backend         TEXT    NOT NULL DEFAULT 'python'
                  CHECK (backend IN ('python', 'node', 'go-bin', 'rust-bin')),
  version_id      INTEGER REFERENCES adapter_versions(id),
  created_ts      REAL    NOT NULL,
  updated_ts      REAL    NOT NULL
);
INSERT INTO tools_new SELECT * FROM tools;
DROP TABLE tools;
ALTER TABLE tools_new RENAME TO tools;
CREATE INDEX IF NOT EXISTS idx_tools_state ON tools(state);

PRAGMA foreign_keys = ON;
