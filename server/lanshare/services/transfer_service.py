"""Transfer orchestration: database rows, chunk files and WebSocket events.

Routers call into here; they do not touch the database or the filesystem.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

import aiosqlite
from starlette.concurrency import run_in_threadpool

from .. import timing
from ..config import MAX_CHUNK_SIZE
from ..db.repositories import DeviceRepository, TransferRepository
from ..ws.manager import ConnectionManager, event
from .auth import TRUSTED
from .errors import BadRequest, Conflict, Forbidden, NotFound, PayloadTooLarge
from .storage import Storage, StorageError, sanitize_filename

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {"pending", "uploading"}

#: How many chunks a transfer may be split into before the chunk size grows.
#:
#: ``Storage.received_chunks`` lists the chunk directory and stats every file,
#: and it runs once per uploaded chunk to emit progress - so the scanning cost
#: over a transfer grows with the *square* of the chunk count. Measured on a
#: local SSD: 4096 chunks costs ~150s of scanning spread across the transfer,
#: 16384 costs ~41 minutes. Holding the count here is what keeps a very large
#: file merely slow rather than unusable.
TARGET_MAX_CHUNKS: Final[int] = 4096


def chunk_size_for(size: int, *, base: int, maximum: int = MAX_CHUNK_SIZE) -> int:
    """Pick the chunk size for one file.

    Doubles ``base`` until the file fits in :data:`TARGET_MAX_CHUNKS` chunks,
    stopping at ``maximum``. Anything up to ``base * TARGET_MAX_CHUNKS`` is
    unaffected, so ordinary transfers keep the size they have always had - at
    the defaults that means every file up to 16 GiB still uses 4 MiB chunks.

    Bigger chunks cost resume granularity: a dropped connection re-sends the
    whole chunk. That is the trade being made, and it only kicks in for files
    where the alternative is thousands of directory scans.
    """
    chunk = base
    while chunk < maximum and math.ceil(size / chunk) > TARGET_MAX_CHUNKS:
        chunk = min(chunk * 2, maximum)
    return chunk


class TransferService:
    """Owns the lifecycle of a transfer, from init to download-ready."""

    def __init__(
        self,
        *,
        connection: aiosqlite.Connection,
        storage: Storage,
        connections: ConnectionManager,
        chunk_size: int,
        max_file_size: int,
    ) -> None:
        self._conn = connection
        self._storage = storage
        self._connections = connections
        self._chunk_size = chunk_size
        self._max_file_size = max_file_size
        # One lock per transfer being completed. Two concurrent completes both
        # pass the "already completed?" check, both assemble, and the loser
        # finds the chunks already cleaned up - so it marks a transfer that
        # succeeded as failed. See `_completing`.
        self._completion_locks: dict[str, asyncio.Lock] = {}
        self._completion_waiters: dict[str, int] = {}

    # -- helpers -------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _completing(self, transfer_id: str) -> AsyncIterator[None]:
        """Serialize completion of one transfer, without serializing all of them.

        Assembling a large file takes minutes, so a single global lock would
        make one user's transfer block another's. The bookkeeping drops the
        lock once nobody is waiting on it, so the table cannot grow with every
        transfer the server has ever seen. Everything here runs on the event
        loop thread with no await in between, so the counts cannot race.
        """
        lock = self._completion_locks.setdefault(transfer_id, asyncio.Lock())
        self._completion_waiters[transfer_id] = self._completion_waiters.get(transfer_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._completion_waiters[transfer_id] - 1
            if remaining <= 0:
                del self._completion_waiters[transfer_id]
                self._completion_locks.pop(transfer_id, None)
            else:
                self._completion_waiters[transfer_id] = remaining

    async def _require(self, transfer_id: str) -> dict[str, Any]:
        transfer = await TransferRepository.get(self._conn, transfer_id)
        if transfer is None:
            raise NotFound(f"No transfer {transfer_id}")
        return transfer

    def state_of(self, transfer: dict[str, Any]) -> tuple[list[int], list[int]]:
        """Chunk state for a transfer, or empty lists once it is finished."""
        if transfer["status"] not in ACTIVE_STATUSES:
            return [], []
        received = self._storage.received_chunks(transfer["id"])
        have = set(received)
        missing = [i for i in range(transfer["total_chunks"]) if i not in have]
        return received, missing

    async def _notify(self, transfer: dict[str, Any], message: dict[str, Any]) -> None:
        await self._connections.send_many([transfer["sender_id"], transfer["receiver_id"]], message)

    @staticmethod
    def _expected_chunk_size(transfer: dict[str, Any], index: int) -> int:
        remaining = transfer["size"] - index * transfer["chunk_size"]
        return min(transfer["chunk_size"], remaining)

    # -- lifecycle -----------------------------------------------------------

    async def create(
        self,
        *,
        sender_id: str,
        filename: str,
        size: int,
        receiver_id: str,
        mime_type: str | None,
    ) -> dict[str, Any]:
        if size > self._max_file_size:
            raise PayloadTooLarge(f"File is {size} bytes; the limit is {self._max_file_size} bytes")
        receiver = await DeviceRepository.get(self._conn, receiver_id)
        if receiver is None:
            raise NotFound(f"Unknown receiver {receiver_id}")
        if receiver.get("trust_state") != TRUSTED:
            # Being trusted yourself does not let you push a transfer at a
            # device the host has not approved. It could not download the file
            # anyway, so the only thing an unapproved target achieves is a row
            # in someone's history and a notification they cannot act on.
            raise Forbidden("That device has not been approved")

        safe_name = sanitize_filename(filename)
        # Per transfer, not global: the client uses whatever we return here.
        chunk_size = chunk_size_for(size, base=self._chunk_size)
        total_chunks = math.ceil(size / chunk_size)

        transfer = await TransferRepository.create(
            self._conn,
            transfer_id=str(uuid.uuid4()),
            filename=safe_name,
            mime_type=mime_type,
            size=size,
            sender_id=sender_id,
            receiver_id=receiver_id,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
        )

        sender = await DeviceRepository.get(self._conn, sender_id)
        await self._connections.send(
            receiver_id,
            event(
                "transfer.incoming",
                transfer_id=transfer["id"],
                filename=safe_name,
                size=size,
                sender_id=sender_id,
                sender_name=sender["name"] if sender else "Unknown device",
            ),
        )
        logger.info("Transfer %s created: %s (%d bytes)", transfer["id"], safe_name, size)
        return transfer

    async def write_chunk(
        self, *, transfer_id: str, index: int, sender_id: str, stream: AsyncIterator[bytes]
    ) -> tuple[int, int]:
        """Persist one chunk. Returns (bytes written, chunks received so far)."""
        transfer = await self._require(transfer_id)
        if transfer["sender_id"] != sender_id:
            raise Forbidden("Only the sender may upload chunks")
        if transfer["status"] not in ACTIVE_STATUSES:
            raise Conflict("Transfer is " + transfer["status"])
        if not 0 <= index < transfer["total_chunks"]:
            raise BadRequest(f"Chunk index {index} is outside 0..{transfer['total_chunks'] - 1}")

        # Worked out before the write, so it can bound it rather than audit it.
        expected = self._expected_chunk_size(transfer, index)

        timing.mark("body.read.start", tid=transfer_id[:8], index=index, expected=expected)
        try:
            written = await self._storage.write_chunk(transfer_id, index, stream, limit=expected)
        except StorageError as exc:
            raise BadRequest(str(exc)) from exc
        timing.mark("body.read.done", tid=transfer_id[:8], index=index, written=written)

        if written != expected:
            # Drop the short chunk so a resume re-sends it, rather than
            # assembling a corrupt file later.
            self._storage.chunk_path(transfer_id, index).unlink(missing_ok=True)
            raise BadRequest(f"Chunk {index} was {written} bytes; expected {expected}")

        if transfer["status"] == "pending":
            await TransferRepository.set_status(self._conn, transfer_id, "uploading")
        timing.mark("db.status.done", tid=transfer_id[:8], index=index)

        received = len(self._storage.received_chunks(transfer_id))
        timing.mark("notify.start", tid=transfer_id[:8], index=index, received=received)
        await self._notify(
            transfer,
            event(
                "transfer.progress",
                transfer_id=transfer_id,
                received_chunks=received,
                total_chunks=transfer["total_chunks"],
                bytes_received=min(received * transfer["chunk_size"], transfer["size"]),
                size=transfer["size"],
            ),
        )
        timing.mark("notify.done", tid=transfer_id[:8], index=index)
        return written, received

    async def get(self, transfer_id: str, *, device_id: str | None = None) -> dict[str, Any]:
        transfer = await self._require(transfer_id)
        if device_id is not None:
            self._require_participant(transfer, device_id)
        return transfer

    @staticmethod
    def _require_participant(transfer: dict[str, Any], device_id: str) -> None:
        """Being trusted is not the same as being involved.

        A transfer is between two devices; a third trusted device has no more
        business reading it than an untrusted one.
        """
        if device_id not in (transfer["sender_id"], transfer["receiver_id"]):
            raise Forbidden("This transfer does not involve your device")

    async def complete(self, *, transfer_id: str, sender_id: str) -> dict[str, Any]:
        """Assemble, hash and file the upload. Safe to call more than once."""
        async with self._completing(transfer_id):
            return await self._complete(transfer_id=transfer_id, sender_id=sender_id)

    async def _complete(self, *, transfer_id: str, sender_id: str) -> dict[str, Any]:
        # Read inside the lock: a complete that queued behind another must see
        # the state that one left, not the state it started with.
        transfer = await self._require(transfer_id)
        if transfer["sender_id"] != sender_id:
            raise Forbidden("Only the sender may complete a transfer")
        if transfer["status"] == "completed":
            return transfer  # idempotent: a retried complete is not an error
        if transfer["status"] not in ACTIVE_STATUSES:
            raise Conflict("Transfer is " + transfer["status"])

        missing = self._storage.missing_chunks(transfer_id, transfer["total_chunks"])
        if missing:
            raise Conflict(f"Still missing {len(missing)} chunk(s): {missing[:10]}")

        try:
            # Assembly and hashing are blocking CPU/disk work.
            destination, digest = await run_in_threadpool(
                self._storage.assemble,
                transfer_id,
                total_chunks=transfer["total_chunks"],
                filename=transfer["filename"],
            )
        except (StorageError, OSError) as exc:
            # The detail goes to the log, not to the client: OSError and
            # StorageError both carry absolute server paths, and the error is
            # also stored on the row and replayed in history.
            logger.exception("Assembly failed for transfer %s", transfer_id)
            detail = "Could not assemble the file"
            await TransferRepository.set_status(self._conn, transfer_id, "failed", error=detail)
            await self._notify(
                transfer, event("transfer.failed", transfer_id=transfer_id, error=detail)
            )
            raise Conflict(detail) from exc

        await TransferRepository.mark_completed(
            self._conn, transfer_id, sha256=digest, stored_name=destination.name
        )
        completed = await self._require(transfer_id)

        await self._notify(
            completed,
            event(
                "transfer.completed",
                transfer_id=transfer_id,
                filename=completed["filename"],
                stored_name=completed["stored_name"],
                size=completed["size"],
                sha256=digest,
                sender_id=completed["sender_id"],
                receiver_id=completed["receiver_id"],
                download_url=f"/api/files/{transfer_id}/download",
            ),
        )
        return completed

    async def cancel(self, *, transfer_id: str, device_id: str) -> dict[str, Any]:
        transfer = await self._require(transfer_id)
        if device_id not in (transfer["sender_id"], transfer["receiver_id"]):
            raise Forbidden("Only the sender or receiver may cancel a transfer")
        if transfer["status"] not in ACTIVE_STATUSES:
            raise Conflict("Transfer is already " + transfer["status"])

        await TransferRepository.set_status(self._conn, transfer_id, "cancelled")
        await run_in_threadpool(self._storage.cleanup, transfer_id)
        await self._notify(
            transfer, event("transfer.cancelled", transfer_id=transfer_id, by=device_id)
        )
        logger.info("Transfer %s cancelled by %s", transfer_id, device_id)
        return await self._require(transfer_id)

    async def history(self, *, device_id: str | None, limit: int) -> list[dict[str, Any]]:
        return await TransferRepository.history(self._conn, device_id=device_id, limit=limit)

    async def clear_history(self, *, device_id: str) -> int:
        """Forget this device's finished transfers. Returns how many went.

        Received files are deliberately untouched: this clears a list, and a
        list is not the files. Partial chunks are a different matter - once the
        row is gone nothing can find its temp directory again, so the directory
        goes with it.
        """
        removed = await TransferRepository.clear_history(
            self._conn, device_id=device_id, active=tuple(sorted(ACTIVE_STATUSES))
        )
        for transfer_id in removed:
            self._storage.cleanup(transfer_id)
        return len(removed)

    # -- download ------------------------------------------------------------

    async def resolve_download(
        self, transfer_id: str, *, device_id: str
    ) -> tuple[Path, dict[str, Any]]:
        transfer = await self._require(transfer_id)
        self._require_participant(transfer, device_id)
        if transfer["status"] != "completed" or not transfer["stored_name"]:
            raise Conflict("Transfer is not complete yet")

        path = self._storage.incoming_path(transfer["stored_name"])
        if not path.is_file():
            raise NotFound("The file is no longer on disk")
        return path, transfer
