"""Chunk size scales with the file, so the chunk count stays bounded.

Why this exists: resume state is derived by listing the chunk directory, which
is O(chunks) and runs once per uploaded chunk. The scanning cost over a transfer
therefore grows with the *square* of the chunk count. Fixing the chunk size
meant a 64 GiB file spent tens of minutes doing nothing but directory listings.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from conftest import CURRENT_APP, LOOPBACK_CLIENT, TEST_CHUNK_SIZE, headers, online, register
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from lanshare.config import GIB, MAX_CHUNK_SIZE, MAX_SUPPORTED_FILE_SIZE, MIB, Settings
from lanshare.main import create_app
from lanshare.services.transfer_service import TARGET_MAX_CHUNKS, chunk_size_for
from pydantic import ValidationError

BASE = 4 * MIB

# Below this, nothing changes: the whole point is that ordinary transfers keep
# behaving exactly as they did before.
UNCHANGED_UP_TO = BASE * TARGET_MAX_CHUNKS  # 16 GiB at the defaults


# -- the pure function -------------------------------------------------------


@pytest.mark.parametrize(
    "size",
    [0, 1, MIB, 100 * MIB, GIB, UNCHANGED_UP_TO - 1, UNCHANGED_UP_TO],
)
def test_ordinary_files_keep_the_base_chunk_size(size: int) -> None:
    """A regression guard, not a property: every file that worked before must
    be split exactly as it was, or Phase 1's testing is invalidated."""
    assert chunk_size_for(size, base=BASE) == BASE


def test_the_chunk_size_grows_only_once_the_count_would_exceed_the_target() -> None:
    assert chunk_size_for(UNCHANGED_UP_TO, base=BASE) == BASE
    assert chunk_size_for(UNCHANGED_UP_TO + 1, base=BASE) == BASE * 2


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (16 * GIB, 4 * MIB),
        (32 * GIB, 8 * MIB),
        (64 * GIB, 16 * MIB),
        (128 * GIB, 32 * MIB),
        (256 * GIB, 64 * MIB),
    ],
)
def test_the_published_schedule(size: int, expected: int) -> None:
    """The table in the README and the vault. If this changes, they must too."""
    assert chunk_size_for(size, base=BASE) == expected


@pytest.mark.parametrize("gigabytes", [1, 7, 16, 17, 33, 100, 200, 256])
def test_the_chunk_count_never_exceeds_the_target(gigabytes: int) -> None:
    """The property the whole change exists to hold."""
    size = gigabytes * GIB
    chunk = chunk_size_for(size, base=BASE)
    assert math.ceil(size / chunk) <= TARGET_MAX_CHUNKS


@pytest.mark.parametrize("gigabytes", [1, 16, 64, 256])
def test_the_chunk_size_never_exceeds_the_maximum(gigabytes: int) -> None:
    """Chunks are streamed to disk, so the cap is about resume granularity:
    a dropped connection re-sends the whole chunk."""
    assert chunk_size_for(gigabytes * GIB, base=BASE) <= MAX_CHUNK_SIZE


def test_the_ceiling_is_exactly_where_the_chunk_size_runs_out() -> None:
    """``MAX_SUPPORTED_FILE_SIZE`` is not arbitrary: it is the largest file that
    still fits in ``TARGET_MAX_CHUNKS`` chunks of ``MAX_CHUNK_SIZE``. If someone
    raises one constant without the other, this is the test that notices."""
    assert MAX_CHUNK_SIZE * TARGET_MAX_CHUNKS == MAX_SUPPORTED_FILE_SIZE
    chunk = chunk_size_for(MAX_SUPPORTED_FILE_SIZE, base=BASE)
    assert chunk == MAX_CHUNK_SIZE
    assert math.ceil(MAX_SUPPORTED_FILE_SIZE / chunk) == TARGET_MAX_CHUNKS


def test_a_larger_file_never_gets_a_smaller_chunk() -> None:
    previous = 0
    for gigabytes in range(0, 260, 4):
        chunk = chunk_size_for(gigabytes * GIB, base=BASE)
        assert chunk >= previous
        previous = chunk


def test_an_empty_file_still_gets_a_usable_chunk_size() -> None:
    assert chunk_size_for(0, base=BASE) == BASE


def test_a_custom_base_is_respected() -> None:
    """``chunk_size`` remains the floor, whatever the operator set it to."""
    assert chunk_size_for(MIB, base=MIB) == MIB
    assert chunk_size_for(64 * GIB, base=MIB) <= MAX_CHUNK_SIZE


# -- the configured ceiling --------------------------------------------------


def test_max_file_size_cannot_be_raised_past_the_supported_ceiling() -> None:
    """Past 256 GiB the chunk count climbs again and scanning dominates, so the
    limit is enforced rather than documented."""
    with pytest.raises(ValidationError):
        Settings(max_file_size=MAX_SUPPORTED_FILE_SIZE + 1)


def test_the_ceiling_itself_is_allowed() -> None:
    assert Settings(max_file_size=MAX_SUPPORTED_FILE_SIZE).max_file_size == MAX_SUPPORTED_FILE_SIZE


# -- end to end --------------------------------------------------------------


@pytest_asyncio.fixture
async def big_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """A server that accepts very large files, so `create` can be exercised.

    Nothing is written: creating a transfer only records metadata, so a
    declared 200 GiB costs no disk.
    """
    settings = Settings(
        server_name="Test PC",
        incoming_dir=tmp_path / "incoming",
        temporary_dir=tmp_path / "temporary",
        data_dir=tmp_path / "data",
        chunk_size=TEST_CHUNK_SIZE,
        max_file_size=MAX_SUPPORTED_FILE_SIZE,
        enable_mdns=False,
        enable_udp_discovery=False,
    )
    app: FastAPI = create_app(settings)
    CURRENT_APP.append(app)
    try:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, client=LOOPBACK_CLIENT)
            async with AsyncClient(transport=transport, base_url="http://testserver") as http:
                yield http
    finally:
        CURRENT_APP.clear()


async def create_transfer(client: AsyncClient, sender: str, receiver: str, size: int) -> dict:
    online(receiver)
    response = await client.post(
        "/api/transfers",
        json={"filename": "huge.iso", "size": size, "receiver_id": receiver},
        headers=headers(sender),
    )
    assert response.status_code == 201, response.text
    body: dict = response.json()
    return body


async def test_a_huge_transfer_is_split_into_a_bounded_number_of_chunks(
    big_client: AsyncClient,
) -> None:
    sender = await register(big_client, "Desktop")
    receiver = await register(big_client, "Laptop")

    body = await create_transfer(big_client, sender, receiver, 200 * GIB)

    assert body["total_chunks"] <= TARGET_MAX_CHUNKS
    assert body["chunk_size"] > TEST_CHUNK_SIZE, "a 200 GiB file must not use the base chunk size"
    assert body["chunk_size"] <= MAX_CHUNK_SIZE
    assert math.ceil(200 * GIB / body["chunk_size"]) == body["total_chunks"]


async def test_a_small_transfer_still_uses_the_configured_chunk_size(
    big_client: AsyncClient,
) -> None:
    """Raising the ceiling must not change how a normal file is sent."""
    sender = await register(big_client, "Desktop")
    receiver = await register(big_client, "Laptop")

    body = await create_transfer(big_client, sender, receiver, 3 * TEST_CHUNK_SIZE)
    assert body["chunk_size"] == TEST_CHUNK_SIZE
    assert body["total_chunks"] == 3


async def test_the_chunk_size_is_recorded_on_the_transfer(big_client: AsyncClient) -> None:
    """Resume depends on it: the client re-reads it from the status endpoint
    rather than remembering what it was told at creation."""
    sender = await register(big_client, "Desktop")
    receiver = await register(big_client, "Laptop")

    body = await create_transfer(big_client, sender, receiver, 200 * GIB)
    status = await big_client.get(f"/api/transfers/{body['transfer_id']}", headers=headers(sender))

    assert status.status_code == 200
    assert status.json()["chunk_size"] == body["chunk_size"]


async def test_a_file_over_the_ceiling_is_refused(big_client: AsyncClient) -> None:
    sender = await register(big_client, "Desktop")
    receiver = await register(big_client, "Laptop")

    response = await big_client.post(
        "/api/transfers",
        json={
            "filename": "too-big.iso",
            "size": MAX_SUPPORTED_FILE_SIZE + 1,
            "receiver_id": receiver,
        },
        headers=headers(sender),
    )
    assert response.status_code == 413
