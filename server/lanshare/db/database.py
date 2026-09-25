"""SQLite connection handling.

One shared ``aiosqlite`` connection for the whole app. aiosqlite runs every
statement on its own worker thread and serializes them, which is plenty for a
single-PC LAN tool and avoids a pool we would have to reason about.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    """Owns the connection and applies the schema on connect."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self._connection

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path)
        self._connection.row_factory = aiosqlite.Row
        # WAL lets a reader run while a writer holds the file.
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.execute("PRAGMA synchronous=NORMAL")
        await self._connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._connection.commit()
        await self._apply_migrations()
        logger.info("Database ready at %s", self._path)

    async def _apply_migrations(self) -> None:
        """Run numbered migrations that have not been applied yet.

        `schema.sql` is the baseline and never changes; everything after it is a
        migration. A fresh database therefore gets the baseline plus every
        migration, and an existing one gets only what it is missing — both end
        up with the same schema. See `migrations/README.md`.
        """
        connection = self.connection
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        await connection.commit()

        async with connection.execute("SELECT version FROM schema_migrations") as cursor:
            applied = {int(row[0]) for row in await cursor.fetchall()}

        for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in applied:
                continue
            logger.info("Applying migration %s", path.name)
            await connection.executescript(path.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat(timespec="seconds")),
            )
            await connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
