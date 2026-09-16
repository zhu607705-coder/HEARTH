PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY, market_id TEXT NOT NULL, stall_id TEXT NOT NULL,
  config_json TEXT NOT NULL, state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(batch_id),
  latest_json TEXT
);
CREATE TABLE IF NOT EXISTS readings (
  event_id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id),
  boot_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence >= 0),
  observed_at TEXT NOT NULL, received_at TEXT NOT NULL, content_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL, disposition TEXT NOT NULL CHECK(disposition IN ('live','history')),
  UNIQUE(device_id, boot_id, sequence)
);
CREATE INDEX IF NOT EXISTS readings_stream ON readings(device_id, boot_id, sequence DESC);
CREATE TABLE IF NOT EXISTS ledger (
  seq INTEGER PRIMARY KEY, body_json TEXT NOT NULL, prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS outbox (
  seq INTEGER PRIMARY KEY REFERENCES ledger(seq), attempts INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(delivered_at, seq);
