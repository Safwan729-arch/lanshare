-- LANShare schema. Applied on every startup; must stay idempotent.
-- Any change here needs a numbered migration + an ADR (CLAUDE.md 7.4).

CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    user_agent  TEXT,
    kind        TEXT NOT NULL DEFAULT 'browser',   -- 'browser' | 'server'
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,   -- sanitized name the sender asked for
    stored_name  TEXT,            -- actual name on disk, set once assembled
    mime_type    TEXT,
    size         INTEGER NOT NULL,
    sha256       TEXT,
    sender_id    TEXT NOT NULL,
    receiver_id  TEXT NOT NULL,
    status       TEXT NOT NULL,   -- pending|uploading|completed|failed|cancelled
    chunk_size   INTEGER NOT NULL,
    total_chunks INTEGER NOT NULL,
    error        TEXT,
    created_at   TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_transfers_created_at ON transfers (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transfers_receiver   ON transfers (receiver_id, status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
