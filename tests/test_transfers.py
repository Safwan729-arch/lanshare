"""The transfer protocol: upload, resume, complete, cancel, download."""

from __future__ import annotations

import hashlib
from pathlib import Path

from conftest import TEST_CHUNK_SIZE, accept, headers, send_file
from httpx import AsyncClient
from lanshare.config import Settings

# Two and a half chunks, so the last chunk is a partial one.
PAYLOAD = bytes(range(256)) * (TEST_CHUNK_SIZE * 5 // 512)


async def test_full_transfer_round_trip(
    client: AsyncClient, sender: str, receiver: str, settings: Settings
) -> None:
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="holiday.bin",
        payload=PAYLOAD,
    )

    complete = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert complete.status_code == 200, complete.text
    body = complete.json()
    assert body["status"] == "completed"
    assert body["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()

    # The file really is on disk, byte for byte.
    stored = settings.incoming_dir / "holiday.bin"
    assert stored.read_bytes() == PAYLOAD

    # And the receiver can download it.
    download = await client.get(f"/api/files/{transfer_id}/download", headers=headers(receiver))
    assert download.status_code == 200
    assert download.content == PAYLOAD
    assert "holiday.bin" in download.headers["content-disposition"]


async def test_temporary_files_are_cleaned_up(
    client: AsyncClient, sender: str, receiver: str, settings: Settings
) -> None:
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="a.bin", payload=PAYLOAD
    )
    await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert not (settings.temporary_dir / transfer_id).exists()


async def test_resume_sends_only_missing_chunks(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """The iOS-suspends-the-tab case: finish an upload that stalled mid-way."""
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="resumed.bin",
        payload=PAYLOAD,
        skip={1},
    )

    status = await client.get(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert status.status_code == 200
    assert status.json()["missing_chunks"] == [1]

    # Completing now must fail - we would otherwise assemble a file with a hole.
    early = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert early.status_code == 409

    chunk = PAYLOAD[TEST_CHUNK_SIZE : 2 * TEST_CHUNK_SIZE]
    resend = await client.put(
        f"/api/transfers/{transfer_id}/chunks/1", content=chunk, headers=headers(sender)
    )
    assert resend.status_code == 200

    complete = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert complete.status_code == 200
    assert complete.json()["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()


async def test_chunk_upload_is_idempotent(client: AsyncClient, sender: str, receiver: str) -> None:
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="twice.bin", payload=PAYLOAD
    )
    # Re-send chunk 0 exactly as before; it should simply overwrite.
    again = await client.put(
        f"/api/transfers/{transfer_id}/chunks/0",
        content=PAYLOAD[:TEST_CHUNK_SIZE],
        headers=headers(sender),
    )
    assert again.status_code == 200

    complete = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert complete.json()["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()


async def test_short_chunk_is_rejected_and_discarded(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """A truncated chunk must not be left on disk looking complete."""
    response = await client.post(
        "/api/transfers",
        json={"filename": "t.bin", "size": len(PAYLOAD), "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = response.json()["transfer_id"]
    await accept(transfer_id, receiver)

    bad = await client.put(
        f"/api/transfers/{transfer_id}/chunks/0",
        content=b"too short",
        headers=headers(sender),
    )
    assert bad.status_code == 400

    status = await client.get(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert 0 in status.json()["missing_chunks"]


async def test_chunk_index_out_of_range(client: AsyncClient, sender: str, receiver: str) -> None:
    response = await client.post(
        "/api/transfers",
        json={"filename": "t.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = response.json()["transfer_id"]
    await accept(transfer_id, receiver)

    bad = await client.put(
        f"/api/transfers/{transfer_id}/chunks/99", content=b"x", headers=headers(sender)
    )
    assert bad.status_code == 400


async def test_only_the_sender_may_upload(client: AsyncClient, sender: str, receiver: str) -> None:
    response = await client.post(
        "/api/transfers",
        json={"filename": "t.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = response.json()["transfer_id"]

    intruder = await client.put(
        f"/api/transfers/{transfer_id}/chunks/0", content=b"x" * 10, headers=headers(receiver)
    )
    assert intruder.status_code == 403


async def test_cancel_removes_partial_chunks(
    client: AsyncClient, sender: str, receiver: str, settings: Settings
) -> None:
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="abandoned.bin",
        payload=PAYLOAD,
        skip={2},
    )
    assert (settings.temporary_dir / transfer_id).exists()

    cancelled = await client.delete(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert not (settings.temporary_dir / transfer_id).exists()

    # A cancelled transfer cannot be resurrected.
    late = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert late.status_code == 409


async def test_name_collision_never_overwrites(
    client: AsyncClient, sender: str, receiver: str, settings: Settings
) -> None:
    first = b"first file"
    second = b"second file"

    for payload in (first, second):
        transfer_id = await send_file(
            client,
            sender_id=sender,
            receiver_id=receiver,
            filename="photo.jpg",
            payload=payload,
        )
        await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))

    assert (settings.incoming_dir / "photo.jpg").read_bytes() == first
    assert (settings.incoming_dir / "photo (1).jpg").read_bytes() == second


async def test_traversal_filename_stays_inside_incoming(
    client: AsyncClient, sender: str, receiver: str, settings: Settings
) -> None:
    payload = b"harmless"
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="../../escaped.txt",
        payload=payload,
    )
    await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))

    assert (settings.incoming_dir / "escaped.txt").read_bytes() == payload
    assert not (Path(settings.incoming_dir).parent.parent / "escaped.txt").exists()


async def test_empty_file_transfers(
    client: AsyncClient, sender: str, receiver: str, settings: Settings
) -> None:
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="empty.txt", payload=b""
    )
    complete = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert complete.status_code == 200
    assert (settings.incoming_dir / "empty.txt").read_bytes() == b""


async def test_unknown_receiver_is_rejected(client: AsyncClient, sender: str) -> None:
    response = await client.post(
        "/api/transfers",
        json={
            "filename": "t.bin",
            "size": 10,
            "receiver_id": "00000000-0000-0000-0000-000000000000",
        },
        headers=headers(sender),
    )
    assert response.status_code == 404


async def test_file_over_the_limit_is_rejected(
    client: AsyncClient, sender: str, receiver: str, settings: Settings
) -> None:
    response = await client.post(
        "/api/transfers",
        json={
            "filename": "huge.bin",
            "size": settings.max_file_size + 1,
            "receiver_id": receiver,
        },
        headers=headers(sender),
    )
    assert response.status_code == 413


async def test_download_before_complete_is_refused(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    response = await client.post(
        "/api/transfers",
        json={"filename": "t.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = response.json()["transfer_id"]

    download = await client.get(f"/api/files/{transfer_id}/download", headers=headers(receiver))
    assert download.status_code == 409


async def test_history_lists_newest_first(client: AsyncClient, sender: str, receiver: str) -> None:
    for name in ("one.txt", "two.txt"):
        transfer_id = await send_file(
            client, sender_id=sender, receiver_id=receiver, filename=name, payload=b"data"
        )
        await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))

    history = await client.get("/api/transfers", headers=headers(sender))
    assert history.status_code == 200
    names = [t["filename"] for t in history.json()["transfers"]]
    assert names == ["two.txt", "one.txt"]


async def test_missing_device_header_is_rejected(client: AsyncClient, receiver: str) -> None:
    """401, not 422 - a missing credential is not a malformed request, and the
    two must look the same to a caller probing for valid device ids."""
    response = await client.post(
        "/api/transfers", json={"filename": "t.bin", "size": 1, "receiver_id": receiver}
    )
    assert response.status_code == 401
