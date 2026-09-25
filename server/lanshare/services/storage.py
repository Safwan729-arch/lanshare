"""Filesystem side of a transfer: safe names, chunk files, assembly, hashing.

Nothing here trusts the client. Filenames are sanitized, transfer ids are
validated, and every resolved path is checked to still sit inside its root.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from collections.abc import AsyncIterator
from itertools import chain
from pathlib import Path

import aiofiles

logger = logging.getLogger(__name__)

# Names Windows refuses regardless of extension.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
RESERVED_NAMES |= {f"COM{i}" for i in range(1, 10)}
RESERVED_NAMES |= {f"LPT{i}" for i in range(1, 10)}

_ILLEGAL_CHARS = re.compile(r'[<>:"/\|?*\x00-\x1f]')
_TRANSFER_ID = re.compile(r"\A[0-9a-fA-F-]{8,64}\Z")

MAX_FILENAME_LENGTH = 180
ASSEMBLY_BUFFER = 1024 * 1024


class StorageError(Exception):
    """Raised when a path or id fails validation."""


def sanitize_filename(raw: str) -> str:
    """Turn a client-supplied name into something safe to write on Windows.

    Strips directory components, illegal and control characters, reserved
    device names and trailing dots/spaces, then caps the length while keeping
    the extension.
    """
    # Only ever keep the final component: defeats "../../x" and "C:\x".
    name = raw.replace("\\", "/").split("/")[-1]
    name = _ILLEGAL_CHARS.sub("_", name)
    name = name.strip().strip(".").strip()

    if not name:
        return "file"

    stem, dot, suffix = name.partition(".")
    if stem.upper() in RESERVED_NAMES:
        stem = f"_{stem}"
    name = f"{stem}{dot}{suffix}"

    if len(name) > MAX_FILENAME_LENGTH:
        extension = Path(name).suffix[:32]
        keep = MAX_FILENAME_LENGTH - len(extension)
        name = name[:keep] + extension

    return name or "file"


def unique_destination(directory: Path, filename: str) -> Path:
    """Reserve a free path: ``photo.jpg`` -> ``photo (1).jpg``.

    The returned file is **created empty**, which is what makes the name a
    reservation rather than a guess. Testing ``exists()`` and moving afterwards
    leaves a window in between: two transfers of the same filename finishing at
    the same moment both see the name free, and the second silently destroys
    the first. Never overwriting is a promise this project makes, so the check
    and the claim have to be one atomic step.

    The caller owns the reserved file and must remove it if it does not go on
    to fill it.
    """
    stem = Path(filename).stem
    suffix = Path(filename).suffix

    names = chain([filename], (f"{stem} ({counter}){suffix}" for counter in range(1, 10_000)))
    for name in names:
        candidate = directory / name
        try:
            # O_CREAT | O_EXCL - the filesystem decides the winner, not us.
            candidate.touch(exist_ok=False)
        except FileExistsError:
            continue
        return candidate

    raise StorageError(f"Could not find a free filename for {filename!r}")


def _resolve_within(root: Path, *parts: str) -> Path:
    """Join and resolve, refusing anything that escapes ``root``."""
    resolved = root.joinpath(*parts).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise StorageError(f"Path escapes its root: {resolved}")
    return resolved


class Storage:
    """Chunk files under ``temporary``, finished files under ``incoming``."""

    def __init__(self, *, incoming_dir: Path, temporary_dir: Path) -> None:
        self.incoming_dir = incoming_dir
        self.temporary_dir = temporary_dir

    # -- paths ---------------------------------------------------------------

    def transfer_dir(self, transfer_id: str) -> Path:
        if not _TRANSFER_ID.match(transfer_id):
            raise StorageError(f"Invalid transfer id: {transfer_id!r}")
        return _resolve_within(self.temporary_dir, transfer_id)

    def chunk_path(self, transfer_id: str, index: int) -> Path:
        if index < 0:
            raise StorageError(f"Invalid chunk index: {index}")
        return self.transfer_dir(transfer_id) / f"{index}.part"

    def incoming_path(self, stored_name: str) -> Path:
        return _resolve_within(self.incoming_dir, sanitize_filename(stored_name))

    # -- chunks --------------------------------------------------------------

    async def write_chunk(
        self,
        transfer_id: str,
        index: int,
        stream: AsyncIterator[bytes],
        *,
        limit: int | None = None,
    ) -> int:
        """Stream one chunk to disk. Idempotent: re-sending an index overwrites.

        Written to a ``.tmp`` sibling first, then renamed, so an interrupted
        upload never leaves a short chunk that looks complete.

        ``limit`` stops the write *as it happens*. Checking the size afterwards
        is too late: the bytes are already on disk, so a sender that ignores the
        chunk size it was given could fill the volume before being told no.
        """
        directory = self.transfer_dir(transfer_id)
        directory.mkdir(parents=True, exist_ok=True)

        final = self.chunk_path(transfer_id, index)
        staging = final.with_suffix(".tmp")
        written = 0

        try:
            async with aiofiles.open(staging, "wb") as handle:
                async for block in stream:
                    written += len(block)
                    if limit is not None and written > limit:
                        raise StorageError(f"Chunk {index} exceeds the expected {limit} bytes")
                    await handle.write(block)
        except BaseException:
            # Includes the limit breach and any client disconnect mid-upload.
            staging.unlink(missing_ok=True)
            raise

        staging.replace(final)
        return written

    def received_chunks(self, transfer_id: str) -> list[int]:
        """Which chunk indexes are on disk. Derived from the filesystem so a
        crash can never leave the database claiming a chunk we do not have."""
        directory = self.transfer_dir(transfer_id)
        if not directory.is_dir():
            return []

        indexes = []
        for path in directory.glob("*.part"):
            if not path.stem.isdigit():
                continue
            try:
                if path.stat().st_size > 0:
                    indexes.append(int(path.stem))
            except OSError:
                # The file went away between the glob and the stat - a cancel
                # racing an upload. Treat it as a chunk we do not have, which
                # is exactly what it is.
                continue
        return sorted(indexes)

    def missing_chunks(self, transfer_id: str, total_chunks: int) -> list[int]:
        have = set(self.received_chunks(transfer_id))
        return [index for index in range(total_chunks) if index not in have]

    def cleanup(self, transfer_id: str) -> None:
        """Delete the temp directory for a transfer. Safe to call twice."""
        try:
            directory = self.transfer_dir(transfer_id)
        except StorageError:
            return
        shutil.rmtree(directory, ignore_errors=True)

    # -- assembly ------------------------------------------------------------

    def assemble(self, transfer_id: str, *, total_chunks: int, filename: str) -> tuple[Path, str]:
        """Concatenate chunks in order, hash while writing, move into incoming.

        Blocking: call it through ``run_in_threadpool``. Returns the final path
        and the SHA-256 of the assembled file.
        """
        missing = self.missing_chunks(transfer_id, total_chunks)
        if missing:
            raise StorageError(f"Missing chunks: {missing[:10]}")

        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        directory = self.transfer_dir(transfer_id)
        # A zero-byte file has no chunks, so the temp directory may not exist yet.
        directory.mkdir(parents=True, exist_ok=True)
        staging = directory / "assembled.tmp"
        digest = hashlib.sha256()

        with staging.open("wb") as output:
            for index in range(total_chunks):
                with self.chunk_path(transfer_id, index).open("rb") as part:
                    while block := part.read(ASSEMBLY_BUFFER):
                        digest.update(block)
                        output.write(block)

        destination = unique_destination(self.incoming_dir, sanitize_filename(filename))
        try:
            # The destination already exists - it is our empty reservation - so
            # Path.replace overwrites, unlike a plain rename which would
            # refuse on Windows. Atomic within a volume.
            staging.replace(destination)
        except OSError:
            # Different volumes: replace cannot cross them. Copy into the
            # reservation instead, so the name stays ours throughout.
            try:
                shutil.copyfile(staging, destination)
            except OSError:
                destination.unlink(missing_ok=True)
                raise
            staging.unlink(missing_ok=True)
        self.cleanup(transfer_id)

        logger.info("Assembled %s -> %s", transfer_id, destination.name)
        return destination, digest.hexdigest()
