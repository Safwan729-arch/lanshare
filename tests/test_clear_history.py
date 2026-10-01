"""Clearing the transfer history.

History is one row per transfer, shared by its sender and its receiver, so
"clear mine" deletes rows the other participant can also see. That is accepted
(see the endpoint docstring) - what must not happen is clearing someone else's
history, destroying received files, or yanking a transfer that is still running.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import approve, headers, register, send_file
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_it_clears_this_devices_history(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="a.txt", payload=b"hello"
    )
    done = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert done.status_code == 200, done.text

    response = await client.delete("/api/transfers", headers=headers(sender))
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1

    remaining = await client.get("/api/transfers", headers=headers(sender))
    assert remaining.json()["transfers"] == []


async def test_it_leaves_another_devices_history_alone(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """The whole point of scoping. A third device clearing must not touch these."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="theirs.txt", payload=b"hi"
    )
    await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))

    outsider = await register(client, "Outsider")
    await approve(client, sender, outsider)

    response = await client.delete("/api/transfers", headers=headers(outsider))
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 0, "an uninvolved device cleared rows that are not its own"

    survived = await client.get("/api/transfers", headers=headers(sender))
    assert [t["filename"] for t in survived.json()["transfers"]] == ["theirs.txt"]


async def test_it_does_not_cancel_a_transfer_in_progress(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """Deleting a running transfer's row would 404 its next chunk and strand
    the partial chunks on disk. Clearing a list must not kill an upload."""
    payload = b"x" * (5 * 1024 * 1024)
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="busy.bin",
        payload=payload,
        skip={1},
    )

    response = await client.delete("/api/transfers", headers=headers(sender))
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 0, "an in-flight transfer was cleared"

    # The upload must still be able to finish.
    still_there = await client.get(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert still_there.status_code == 200
    state = still_there.json()
    chunk_size = state["chunk_size"]
    missing = state["missing_chunks"]
    assert missing, "the test meant to leave a chunk outstanding"

    index = missing[0]
    resumed = await client.put(
        f"/api/transfers/{transfer_id}/chunks/{index}",
        content=payload[index * chunk_size : (index + 1) * chunk_size],
        headers=headers(sender),
    )
    assert resumed.status_code == 200, resumed.text


async def test_it_never_deletes_a_received_file(
    client: AsyncClient, sender: str, receiver: str, settings
) -> None:
    """Clearing a list must not destroy files. This is the promise the whole
    feature rests on."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="keep.txt", payload=b"precious"
    )
    done = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert done.status_code == 200, done.text

    incoming = list(Path(settings.incoming_dir).iterdir())
    assert incoming, "nothing was written to incoming; the test proves nothing"

    await client.delete("/api/transfers", headers=headers(sender))

    assert list(Path(settings.incoming_dir).iterdir()) == incoming, (
        "clearing the history deleted a received file"
    )


async def test_it_takes_the_partial_chunks_with_it(
    client: AsyncClient, sender: str, receiver: str, settings
) -> None:
    """A transfer's row is the only thing that knows its temp directory exists.
    Delete the row and leave the directory behind and nothing can ever find it
    again - the orphaned-directory leak becomes permanent.

    Cancelling already removes the chunks, so the directory is put back by hand
    here: this has to test the clearing, not the cancelling.
    """
    payload = b"y" * (5 * 1024 * 1024)
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="halfway.bin",
        payload=payload,
        skip={1},
    )
    cancelled = await client.delete(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert cancelled.status_code == 200, cancelled.text

    # Stand in for a transfer whose chunks outlived it - a failure mid-upload,
    # or a crash between writing a chunk and recording it.
    leftover = Path(settings.temporary_dir) / transfer_id
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "0.part").write_bytes(b"debris")

    response = await client.delete("/api/transfers", headers=headers(sender))
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1

    assert not leftover.exists(), "the cleared transfer left its chunk directory behind"


async def test_an_untrusted_device_cannot_clear(lan_client: AsyncClient) -> None:
    """From the LAN, not loopback: loopback registers as trusted by design, so
    only a LAN device can be pending."""
    stranger = await register(lan_client, "Stranger")  # registered, never approved
    response = await lan_client.delete("/api/transfers", headers=headers(stranger))
    assert response.status_code == 403, response.text


async def test_it_is_safe_to_clear_an_empty_history(client: AsyncClient, sender: str) -> None:
    response = await client.delete("/api/transfers", headers=headers(sender))
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 0
