# Migrations

`schema.sql` is the **baseline** shape and never changes. Every change after it
is a numbered file here, applied in order and recorded in `schema_migrations`.

Both paths converge on the same schema:

- **Fresh database** — `schema.sql` creates the baseline, then every migration runs.
- **Existing database** — `schema.sql` is a no-op (`CREATE TABLE IF NOT EXISTS`),
  then only the unapplied migrations run.

That is why the new columns are *not* also added to `schema.sql`: a fresh
database would then hit "duplicate column name" when the migration ran.

Name files `NNN_short_description.sql`. Each one is a decision worth recording:
write down why the shape changed, not just what changed.
