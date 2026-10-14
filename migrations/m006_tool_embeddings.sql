-- m006: F-040 semantic tool search — persisted tool embeddings
CREATE TABLE IF NOT EXISTS tool_embeddings (
  name TEXT PRIMARY KEY,          -- tool name
  adapter_module TEXT NOT NULL,
  text TEXT NOT NULL,             -- embedded text: "name: description (module)"
  content_hash TEXT NOT NULL,     -- sha256 of text; skip re-embed when unchanged
  model TEXT NOT NULL,            -- provider identifier, e.g. "tokenfactory:BAAI/bge-m3"
  dims INTEGER NOT NULL,
  embedding BLOB NOT NULL,        -- float32 little-endian, array('f').tobytes()
  updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_embeddings_module ON tool_embeddings(adapter_module);
