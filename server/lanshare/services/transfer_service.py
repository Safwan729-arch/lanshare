"""Transfer orchestration: database rows, chunk files and WebSocket events.

Routers call into here; they do not touch the database or the filesystem.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import aiosqlite
from starlette.concurrency import run_in_threadpool

from .. import timing
from ..config import MAX_CHUNK_SIZE
from ..db.repositories import DeviceRepository, TransferRepository
from ..ws.manager import ConnectionManager, event
from .auth import TRUSTED, is_host_device
from .errors import BadRequest, Conflict, Forbidden, NotFound, PayloadTooLarge
from .storage import Storage, StorageError, sanitize_filename

logger = logging.getLogger(__name__)

#: A transfer whose chunks may flow right now.
ACTIVE_STATUSES = {"pending", "uploading"}

#: Waiting for the recipient to accept. Deliberately *not* in ACTIVE_STATUSES,
#: so the upload paths refuse it, and they say so in words a sender can act on.
AWAITING = "awaiting"

#: Live, meaning a decision or an upload is still in progress. Wider than
#: ACTIVE_STATUSES because a transfer waiting for consent can be cancelled by
#: its sender and must not be deleted by a history clear.
LIVE_STATUSES = ACTIVE_STATUSES | {AWAITING}

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
        server_device_id: str,
        stale_after_hours: int = 24,
        consent_timeout_seconds: int = 120,
    ) -> None:
        self._conn = connection
        self._storage = storage
        self._connections = connections
        self._chunk_size = chunk_size
        self._max_file_size = max_file_size
        self._stale_after_hours = stale_after_hours
        self._server_device_id = server_device_id
        self._consent_timeout: float = consent_timeout_seconds
        #: One timer per waiting request, cancelled by an answer or by shutdown.
        self._consent_timers: dict[str, asyncio.Task[None]] = {}
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
        await self._connections.send_many(await self._audience(transfer), message)

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
        expected_sha256: str | None = None,
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

        # Somebody has to be there to be asked. Refusing here means a send
        # nobody could have answered leaves no row, no chunk directory and
        # nothing to sweep later.
        if not await self._anyone_can_answer(receiver):
            raise Conflict(
                f"{receiver['name']} isn't connected. Open LANShare on it and try again."
            )

        # Consent protects a person from *other people's* files. If the only
        # device that could answer is the sender itself - the host's page
        # sending into the PC's own storage - then the person who pressed Send
        # is the person who would be asked, so the send already is the consent.
        # Prompting would be ceremony, and worse, nobody would see it: the
        # sender is left out of the announcement below, so the request would sit
        # `awaiting` until it expired. Do not "simplify" this into always
        # starting as awaiting.
        parties = {"sender_id": sender_id, "receiver_id": receiver_id}
        audience = [d for d in await self._audience(parties) if d != sender_id]
        status = "awaiting"
        # "Anyone to ask" means someone with a page open: the audience for a
        # PC-addressed file lists the server's own row too, which has no socket.
        if not any(self._connections.is_online(d) for d in audience):
            try:
                await self.require_may_answer(parties, sender_id)
                status = "pending"
            except Forbidden:
                pass

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
            expected_sha256=expected_sha256,
            status=status,
        )

        if status == "pending":
            logger.info(
                "Transfer %s created: %s (%d bytes), self-sent", transfer["id"], safe_name, size
            )
            return transfer

        # Armed only here: a self-sent transfer came out `pending` above and
        # has nobody to wait for.
        self._arm_consent_timer(transfer["id"])
        sender = await DeviceRepository.get(self._conn, sender_id)
        await self._connections.send_many(
            audience,
            event(
                "transfer.incoming",
                transfer_id=transfer["id"],
                filename=safe_name,
                size=size,
                sender_id=sender_id,
                sender_name=sender["name"] if sender else "Unknown device",
                status=transfer["status"],
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
        if transfer["status"] == AWAITING:
            raise Conflict("The recipient has not accepted this transfer yet")
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

    async def consent(self, *, transfer_id: str, device_id: str, accept: bool) -> dict[str, Any]:
        """Answer a waiting transfer. Returns the row in its new state."""
        transfer = await self._require(transfer_id)
        await self.require_may_answer(transfer, device_id)

        if transfer["status"] != AWAITING:
            if transfer["status"] == "declined":
                if transfer["error"] == "No answer":
                    raise Conflict("That request has expired")
                raise Conflict("That request was already declined")
            raise Conflict("Transfer is " + transfer["status"])

        if accept:
            moved = await TransferRepository.set_status_if(
                self._conn, transfer_id, expected=AWAITING, status="pending"
            )
            if not moved:
                raise Conflict("That request was already answered")
            self._cancel_consent_timer(transfer_id)
            await self._notify(
                transfer, event("transfer.accepted", transfer_id=transfer_id, by=device_id)
            )
            logger.info("Transfer %s accepted by %s", transfer_id, device_id)
        elif not await self._decline(transfer, reason="Declined", by=device_id):
            raise Conflict("That request was already answered")
        else:
            self._cancel_consent_timer(transfer_id)

        return await self._require(transfer_id)

    def _arm_consent_timer(self, transfer_id: str) -> None:
        """Decline a request nobody answers, so the sender is never left hanging."""

        async def expire() -> None:
            try:
                await asyncio.sleep(self._consent_timeout)
                transfer = await TransferRepository.get(self._conn, transfer_id)
                if transfer is not None and transfer["status"] == AWAITING:
                    await self._decline(transfer, reason="No answer")
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - a timer must not kill the loop
                logger.exception("Consent timer failed for %s", transfer_id)
            finally:
                self._consent_timers.pop(transfer_id, None)

        self._consent_timers[transfer_id] = asyncio.create_task(
            expire(), name=f"consent-{transfer_id[:8]}"
        )

    def _cancel_consent_timer(self, transfer_id: str) -> None:
        task = self._consent_timers.pop(transfer_id, None)
        if task is not None:
            task.cancel()

    async def shutdown(self) -> None:
        """Stop the timers, so none outlives the server that armed it."""
        tasks = list(self._consent_timers.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._consent_timers.clear()

    async def decline_abandoned_requests(self, *, grace: bool = True) -> int:
        """Decline requests nobody answered. Runs at startup and in the sweep.

        A row left `awaiting` by a restart has nobody waiting on it: the
        sender's page gave up long ago, and the timer that would have expired it
        died with the old process. Showing the recipient a prompt for a file
        that is no longer coming is worse than dropping it.

        `grace=False` at startup, because a row that survived a restart is stale
        whatever its age - nothing is left to catch it. The periodic sweep keeps
        the grace period, so it never cancels a decision being made right now.
        """
        cutoff = datetime.now(UTC)
        if grace:
            cutoff -= timedelta(seconds=self._consent_timeout)
        else:
            # created_at has one-second resolution and the query is a strict
            # `<`, so "now" would miss a row written in this very second.
            cutoff += timedelta(seconds=1)
        stale = await TransferRepository.stale_active(
            self._conn, active=(AWAITING,), before=cutoff.isoformat(timespec="seconds")
        )
        declined = 0
        for transfer in stale:
            if await self._decline(transfer, reason="No answer"):
                declined += 1
        return declined

    async def _decline(
        self, transfer: dict[str, Any], *, reason: str, by: str | None = None
    ) -> bool:
        """Refuse a waiting transfer. Nothing is on disk yet, so nothing is deleted.

        Returns False when someone else answered first, so a timer can ignore
        the loss quietly while `consent` turns it into an error.
        """
        moved = await TransferRepository.set_status_if(
            self._conn, transfer["id"], expected=AWAITING, status="declined", error=reason
        )
        if not moved:
            return False
        await self._notify(
            transfer,
            event("transfer.declined", transfer_id=transfer["id"], error=reason, by=by),
        )
        logger.info("Transfer %s declined (%s)", transfer["id"], reason)
        return True

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

    async def require_may_answer(self, transfer: Mapping[str, Any], device_id: str) -> None:
        """Only the device a file was sent to may accept or refuse it.

        With one exception, and it is not a loophole: a file addressed to the PC
        names the server's own device row, which holds no WebSocket and has no
        browser behind it. The host's page at localhost is a *different* device,
        so it answers on the server row's behalf - the same definition of "host"
        that decides who may approve a pairing.
        """
        if device_id == transfer["receiver_id"]:
            return
        if transfer["receiver_id"] == self._server_device_id:
            device = await DeviceRepository.get(self._conn, device_id)
            if device is not None and is_host_device(device):
                return
        raise Forbidden("Only the device a file was sent to may answer for it")

    async def _anyone_can_answer(self, receiver: dict[str, Any]) -> bool:
        """Is there a page open that could accept this?

        A file addressed to the PC names the server's own row, which holds no
        socket of its own, so the question becomes whether any host page is
        open.
        """
        if receiver["id"] != self._server_device_id:
            return self._connections.is_online(receiver["id"])
        hosts = await DeviceRepository.list_hosts(self._conn)
        return any(self._connections.is_online(str(row["id"])) for row in hosts)

    async def _audience(self, transfer: Mapping[str, Any]) -> list[str]:
        """Which devices hear about this transfer.

        Normally the two parties. For a transfer addressed to the PC the
        receiver is a row nothing is listening on, so the host devices hear it
        instead - otherwise the page where the decision is made would never be
        told there was one to make.
        """
        if transfer["receiver_id"] != self._server_device_id:
            return [transfer["sender_id"], transfer["receiver_id"]]
        hosts = await DeviceRepository.list_hosts(self._conn)
        return [transfer["sender_id"], *(str(row["id"]) for row in hosts)]

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
        if transfer["status"] == AWAITING:
            raise Conflict("The recipient has not accepted this transfer yet")
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

        expected = transfer["expected_sha256"]
        if expected and not secrets.compare_digest(expected, digest):
            # The sender hashed the file before sending it and the bytes we
            # assembled are not those bytes. Something between the two - a
            # retry that overlapped itself, a bad disk, a client bug - lost
            # data silently. Keeping the file would be the worst outcome: it
            # looks delivered and it is wrong.
            destination.unlink(missing_ok=True)
            detail = "The file that arrived does not match what the sender hashed"
            logger.error("Integrity check failed for transfer %s", transfer_id)
            await TransferRepository.set_status(self._conn, transfer_id, "failed", error=detail)
            await self._notify(
                transfer, event("transfer.failed", transfer_id=transfer_id, error=detail)
            )
            raise Conflict(detail)

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
        if transfer["status"] not in LIVE_STATUSES:
            raise Conflict("Transfer is already " + transfer["status"])

        # A sender giving up while the recipient decides is an ordinary way out
        # of `awaiting`, and it leaves a timer behind that would sit there until
        # it fired on a row it can no longer change.
        self._cancel_consent_timer(transfer_id)
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
            self._conn, device_id=device_id, live=tuple(sorted(LIVE_STATUSES))
        )
        for transfer_id in removed:
            # rmtree is blocking, and clearing a long history is many of them.
            await run_in_threadpool(self._storage.cleanup, transfer_id)
        return len(removed)

    async def sweep_orphaned_chunks(self) -> int:
        """Delete chunks no transfer can still claim. Returns how many went.

        A cancelled transfer cleans up after itself, but a browser that is
        closed mid-upload cancels nothing: the row stays `uploading` and its
        partial chunks sit on disk until someone notices. Directories whose row
        has gone entirely - cleared history, a deleted database - are worse,
        because nothing will ever look for them again.

        Called at startup, which is the one moment when nothing can be in
        flight. A transfer that is still active and still young is left alone:
        a server restarted under an open page can still be resumed by it, and
        its chunks are the whole point.
        """
        await self._fail_abandoned()

        swept = 0
        for directory in self._storage.transfer_dirs():
            transfer = await TransferRepository.get(self._conn, directory.name)
            if transfer is not None and transfer["status"] in ACTIVE_STATUSES:
                continue
            await run_in_threadpool(self._storage.cleanup, directory.name)
            swept += 1
        return swept

    async def _fail_abandoned(self) -> None:
        """Give up on uploads nobody can finish, so the sweep can collect them.

        Resume lives in the page: the browser keeps the transfer id in memory
        and never writes it down, so once the tab is gone the upload is gone
        too - the next attempt creates a new transfer. Without this, every
        interrupted upload keeps its partial chunks for the life of the
        installation, and the row sits in the history claiming to be in
        progress for ever.

        The window is generous on purpose. The only thing it protects is an
        upload whose page is still open while the server restarts underneath
        it, which is a real case - it happened twice while this project was
        being debugged.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=self._stale_after_hours)
        stale = await TransferRepository.stale_active(
            self._conn,
            active=tuple(sorted(ACTIVE_STATUSES)),
            before=cutoff.isoformat(timespec="seconds"),
        )
        for transfer in stale:
            await TransferRepository.set_status(
                self._conn,
                transfer["id"],
                "failed",
                error="Abandoned before it finished",
            )
            logger.info(
                "Transfer %s was still %s from %s; marking it failed",
                transfer["id"],
                transfer["status"],
                transfer["created_at"],
            )

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
