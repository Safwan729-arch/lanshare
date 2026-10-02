"""All SQL lives here. Parameterized queries only.

Every function takes the connection explicitly so callers stay testable and the
query being run is always visible at the call site.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from ..services.auth import TRUSTED, is_host_device


def utc_now() -> str:
    """Timestamps are ISO-8601 UTC strings so SQLite can sort them lexically."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _rows(cursor_rows: Iterable[aiosqlite.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor_rows]


class DeviceRepository:
    """Reads and writes the ``devices`` table."""

    @staticmethod
    async def create(
        conn: aiosqlite.Connection,
        *,
        device_id: str,
        name: str,
        user_agent: str | None = None,
        kind: str = "browser",
        trust_state: str = "pending",
        token_hash: str | None = None,
        first_address: str | None = None,
    ) -> dict[str, Any]:
        """Insert a brand new device. Fails if the id is already taken."""
        now = utc_now()
        await conn.execute(
            """
            INSERT INTO devices (
                id, name, user_agent, kind, created_at, last_seen,
                trust_state, token_hash, approved_at, first_address
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                name,
                user_agent,
                kind,
                now,
                now,
                trust_state,
                token_hash,
                now if trust_state == "trusted" else None,
                first_address,
            ),
        )
        await conn.commit()
        device = await DeviceRepository.get(conn, device_id)
        assert device is not None
        return device

    @staticmethod
    async def refresh(
        conn: aiosqlite.Connection,
        *,
        device_id: str,
        name: str,
        user_agent: str | None = None,
    ) -> dict[str, Any] | None:
        """Update the mutable details of a device that already exists.

        Deliberately never touches ``trust_state`` or ``token_hash``:
        re-registering must not be a way to escalate trust or mint a new
        credential for someone else's device id.
        """
        await conn.execute(
            "UPDATE devices SET name = ?, user_agent = ?, last_seen = ? WHERE id = ?",
            (name, user_agent, utc_now(), device_id),
        )
        await conn.commit()
        return await DeviceRepository.get(conn, device_id)

    @staticmethod
    async def upsert(
        conn: aiosqlite.Connection,
        *,
        device_id: str,
        name: str,
        user_agent: str | None = None,
        kind: str = "browser",
        trust_state: str = "pending",
    ) -> dict[str, Any]:
        """Insert or update. Used for the host's own row at startup."""
        now = utc_now()
        await conn.execute(
            """
            INSERT INTO devices (
                id, name, user_agent, kind, created_at, last_seen, trust_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name        = excluded.name,
                user_agent  = excluded.user_agent,
                last_seen   = excluded.last_seen,
                trust_state = excluded.trust_state
            """,
            (device_id, name, user_agent, kind, now, now, trust_state),
        )
        await conn.commit()
        device = await DeviceRepository.get(conn, device_id)
        assert device is not None
        return device

    @staticmethod
    async def set_trust(
        conn: aiosqlite.Connection, device_id: str, trust_state: str
    ) -> dict[str, Any] | None:
        await conn.execute(
            """
            UPDATE devices
            SET trust_state = ?,
                approved_at = CASE WHEN ? = 'trusted' THEN ? ELSE NULL END
            WHERE id = ?
            """,
            (trust_state, trust_state, utc_now(), device_id),
        )
        await conn.commit()
        return await DeviceRepository.get(conn, device_id)

    @staticmethod
    async def list_by_trust(
        conn: aiosqlite.Connection, trust_state: str, *, only_with_token: bool = False
    ) -> list[dict[str, Any]]:
        """Devices in a given trust state.

        ``only_with_token`` drops rows that have no credential. Devices created
        before Phase 5 have a NULL ``token_hash`` and can never authenticate, so
        offering them for approval is a dead end: the browser re-registers under
        a fresh id, and approving the old row changes nothing. They stay in the
        table because transfer history refers to them.
        """
        sql = "SELECT * FROM devices WHERE trust_state = ?"
        if only_with_token:
            sql += " AND token_hash IS NOT NULL"
        async with conn.execute(sql + " ORDER BY created_at DESC", (trust_state,)) as cur:
            return _rows(await cur.fetchall())

    @staticmethod
    async def count_by_trust(conn: aiosqlite.Connection, trust_state: str) -> int:
        async with conn.execute(
            "SELECT COUNT(*) FROM devices WHERE trust_state = ?", (trust_state,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def delete(conn: aiosqlite.Connection, device_id: str) -> bool:
        async with conn.execute("DELETE FROM devices WHERE id = ?", (device_id,)) as cur:
            deleted = cur.rowcount > 0
        await conn.commit()
        return deleted

    @staticmethod
    async def get(conn: aiosqlite.Connection, device_id: str) -> dict[str, Any] | None:
        async with conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    async def list_by_ids(
        conn: aiosqlite.Connection, device_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not device_ids:
            return []
        placeholders = ",".join("?" * len(device_ids))
        async with conn.execute(
            f"SELECT * FROM devices WHERE id IN ({placeholders}) ORDER BY name COLLATE NOCASE",
            device_ids,
        ) as cur:
            return _rows(await cur.fetchall())

    @staticmethod
    async def list_all(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
        async with conn.execute("SELECT * FROM devices ORDER BY name COLLATE NOCASE") as cur:
            return _rows(await cur.fetchall())

    @staticmethod
    async def list_hosts(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
        """Trusted devices that are the host machine itself.

        The loopback test is a Python one rather than SQL because
        `first_address` holds an address, not a flag, and `is_host_device` is the
        single place that decides what counts. The table is tiny - one row per
        browser that has ever paired - so the scan is not worth optimising.
        """
        rows = await DeviceRepository.list_by_trust(conn, TRUSTED)
        return [row for row in rows if is_host_device(row)]

    @staticmethod
    async def rename(conn: aiosqlite.Connection, device_id: str, name: str) -> None:
        await conn.execute(
            "UPDATE devices SET name = ?, last_seen = ? WHERE id = ?",
            (name, utc_now(), device_id),
        )
        await conn.commit()

    @staticmethod
    async def touch(conn: aiosqlite.Connection, device_id: str) -> None:
        await conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (utc_now(), device_id))
        await conn.commit()


class TransferRepository:
    """Reads and writes the ``transfers`` table."""

    @staticmethod
    async def create(
        conn: aiosqlite.Connection,
        *,
        transfer_id: str,
        filename: str,
        mime_type: str | None,
        size: int,
        sender_id: str,
        receiver_id: str,
        chunk_size: int,
        total_chunks: int,
        expected_sha256: str | None = None,
        status: str = "awaiting",
    ) -> dict[str, Any]:
        await conn.execute(
            """
            INSERT INTO transfers (
                id, filename, mime_type, size, sender_id, receiver_id,
                status, chunk_size, total_chunks, created_at, expected_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transfer_id,
                filename,
                mime_type,
                size,
                sender_id,
                receiver_id,
                status,
                chunk_size,
                total_chunks,
                utc_now(),
                expected_sha256,
            ),
        )
        await conn.commit()
        transfer = await TransferRepository.get(conn, transfer_id)
        assert transfer is not None
        return transfer

    @staticmethod
    async def get(conn: aiosqlite.Connection, transfer_id: str) -> dict[str, Any] | None:
        async with conn.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    async def history(
        conn: aiosqlite.Connection, *, device_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if device_id:
            query = """
                SELECT * FROM transfers
                WHERE sender_id = ? OR receiver_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ?
            """
            params: tuple[Any, ...] = (device_id, device_id, limit)
        else:
            query = "SELECT * FROM transfers ORDER BY created_at DESC, rowid DESC LIMIT ?"
            params = (limit,)
        async with conn.execute(query, params) as cur:
            return _rows(await cur.fetchall())

    @staticmethod
    async def clear_history(
        conn: aiosqlite.Connection, *, device_id: str, live: tuple[str, ...]
    ) -> list[str]:
        """Delete this device's finished transfers. Returns the ids removed.

        Scoped the same way as ``history``: a device clears only rows it is a
        party to. Transfers still running, or waiting for the recipient to decide,
        are left alone - deleting one would make its next chunk 404, strand the
        partial chunks on disk, or leave the sender waiting on a row that is gone.

        The ids come back so the caller can clean up anything on disk that only
        the row knew about.
        """
        placeholders = ", ".join("?" * len(live))
        query = f"""
            DELETE FROM transfers
            WHERE (sender_id = ? OR receiver_id = ?)
              AND status NOT IN ({placeholders})
            RETURNING id
        """
        async with conn.execute(query, (device_id, device_id, *live)) as cur:
            removed = [str(row[0]) for row in await cur.fetchall()]
        await conn.commit()
        return removed

    @staticmethod
    async def stale_active(
        conn: aiosqlite.Connection, *, active: tuple[str, ...], before: str
    ) -> list[dict[str, Any]]:
        """Unfinished transfers older than ``before`` (an ISO-8601 UTC string).

        Timestamps are stored as ISO-8601 UTC, which sorts lexically, so a
        string comparison is a date comparison here.
        """
        placeholders = ", ".join("?" * len(active))
        query = f"""
            SELECT * FROM transfers
            WHERE status IN ({placeholders}) AND created_at < ?
            ORDER BY created_at
        """
        async with conn.execute(query, (*active, before)) as cur:
            return _rows(await cur.fetchall())

    @staticmethod
    async def set_status(
        conn: aiosqlite.Connection, transfer_id: str, status: str, *, error: str | None = None
    ) -> None:
        await conn.execute(
            "UPDATE transfers SET status = ?, error = ? WHERE id = ?",
            (status, error, transfer_id),
        )
        await conn.commit()

    @staticmethod
    async def set_status_if(
        conn: aiosqlite.Connection,
        transfer_id: str,
        *,
        expected: str,
        status: str,
        error: str | None = None,
    ) -> bool:
        """Change status only if the row is still where the caller thinks it is.

        Returns whether it moved. Two host pages can answer the same request in
        the same moment, and a timer can expire one while a person is answering
        it: all of them read `awaiting` before any of them writes. Letting the
        database decide the winner is the only way the loser can be told it lost.
        """
        async with conn.execute(
            "UPDATE transfers SET status = ?, error = ? WHERE id = ? AND status = ?",
            (status, error, transfer_id, expected),
        ) as cursor:
            moved = cursor.rowcount > 0
        await conn.commit()
        return moved

    @staticmethod
    async def mark_completed(
        conn: aiosqlite.Connection, transfer_id: str, *, sha256: str, stored_name: str
    ) -> None:
        await conn.execute(
            """
            UPDATE transfers
            SET status = 'completed', sha256 = ?, stored_name = ?, completed_at = ?, error = NULL
            WHERE id = ?
            """,
            (sha256, stored_name, utc_now(), transfer_id),
        )
        await conn.commit()


class SettingsRepository:
    """Key/value settings that must survive a restart (e.g. the host device id)."""

    @staticmethod
    async def get(conn: aiosqlite.Connection, key: str) -> str | None:
        async with conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return str(row["value"]) if row else None

    @staticmethod
    async def set(conn: aiosqlite.Connection, key: str, value: str) -> None:
        await conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await conn.commit()
