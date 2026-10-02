-- End-to-end integrity: the hash the *sender* computed, before upload.
--
-- `sha256` is what the server made of the bytes it assembled, which proves
-- nothing about whether those are the bytes the sender picked. This column
-- holds what the sender said to expect, when it was able to say - the browser
-- can only hash a file in a secure context, so over plain http it stays NULL
-- and completion behaves exactly as before.

ALTER TABLE transfers ADD COLUMN expected_sha256 TEXT;
