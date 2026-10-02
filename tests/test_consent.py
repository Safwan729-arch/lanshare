"""Per-transfer consent: nothing is written until the recipient says yes.

See vault/04-Tasks/transfer-consent.md. These cover the state machine; the
sender's side of it is in tests/js/consent_harness.mjs.
"""

from __future__ import annotations

import pytest
from conftest import approve, headers, register, send_file
from lanshare.db.repositories import DeviceRepository, TransferRepository
from lanshare.services.errors import Forbidden


async def force_awaiting(app, transfer_id: str) -> None:
    """`create` still starts a transfer as `pending`, so put one into `awaiting`
    directly to test the guards on their own."""
    connection = app.state.database.connection
    await TransferRepository.set_status(connection, transfer_id, "awaiting")


async def test_a_waiting_transfer_can_still_be_cancelled(app, client, sender, receiver) -> None:
    """The sender must be able to give up while the recipient is deciding."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="a.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)

    cancelled = await client.delete(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


async def test_clearing_history_leaves_a_waiting_transfer_alone(
    app, client, sender, receiver
) -> None:
    """Deleting a row mid-decision would strand the sender waiting on nothing."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="b.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)

    cleared = await client.delete("/api/transfers", headers=headers(sender))
    assert cleared.status_code == 200
    assert cleared.json()["deleted"] == 0
    assert await TransferRepository.get(app.state.database.connection, transfer_id) is not None


async def test_a_waiting_transfer_refuses_chunks(app, client, sender, receiver) -> None:
    """The whole feature in one assertion.

    Nothing else stops an upload: `write_chunk` rejects any status outside
    `ACTIVE_STATUSES`, and `awaiting` being outside that set is the only reason
    a file cannot be pushed before the recipient has agreed to receive it.
    """
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="c.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)

    refused = await client.put(
        f"/api/transfers/{transfer_id}/chunks/0", content=b"x" * 10, headers=headers(sender)
    )
    assert refused.status_code == 409


async def test_the_repository_can_list_host_devices(app, client, lan_client, sender) -> None:
    """The service needs this and must not reach into `api/` to get it."""
    phone = await register(lan_client, "Phone")
    await approve(client, sender, phone)

    hosts = await DeviceRepository.list_hosts(app.state.database.connection)
    ids = [row["id"] for row in hosts]

    assert sender in ids, "a browser registered from loopback is the host"
    assert app.state.server_device_id in ids, "the server's own row is the host"
    assert phone not in ids


async def test_only_the_addressed_device_may_answer(app, client, sender, receiver) -> None:
    """Being trusted is not the same as being the one asked."""
    service = app.state.transfer_service
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="d.bin", payload=b"x" * 10
    )
    transfer = await TransferRepository.get(app.state.database.connection, transfer_id)

    await service.require_may_answer(transfer, receiver)  # must not raise

    with pytest.raises(Forbidden):
        await service.require_may_answer(transfer, sender)


async def test_a_host_may_answer_for_the_pc(app, client, sender) -> None:
    """A file addressed to the PC has no browser behind it to accept."""
    service = app.state.transfer_service
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=app.state.server_device_id,
        filename="e.bin",
        payload=b"x" * 10,
    )
    transfer = await TransferRepository.get(app.state.database.connection, transfer_id)

    # `sender` registered over loopback, so it is a host device.
    await service.require_may_answer(transfer, sender)


async def test_a_transfer_for_the_pc_is_announced_to_the_host(app, client, sender) -> None:
    """The host's page is a different device from the row the file is addressed to."""
    service = app.state.transfer_service
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=app.state.server_device_id,
        filename="f.bin",
        payload=b"x" * 10,
    )
    transfer = await TransferRepository.get(app.state.database.connection, transfer_id)

    audience = await service._audience(transfer)
    assert sender in audience, "the host's own page must hear about it"
