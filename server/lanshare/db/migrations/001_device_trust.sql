-- Phase 5: pairing and token auth.
--
-- Existing devices are deliberately reset to 'pending'. They predate tokens and
-- have no credential, so they could not authenticate anyway; making the user
-- re-approve them once is the correct posture for a security change. The host
-- PC's own row is the exception - it is the machine running the server.

ALTER TABLE devices ADD COLUMN trust_state TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE devices ADD COLUMN token_hash  TEXT;
ALTER TABLE devices ADD COLUMN approved_at TEXT;
ALTER TABLE devices ADD COLUMN first_address TEXT;

UPDATE devices SET trust_state = 'trusted' WHERE kind = 'server';

CREATE INDEX IF NOT EXISTS idx_devices_trust ON devices (trust_state);
