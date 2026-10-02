"""Per-transfer consent: nothing is written until the recipient says yes.

See vault/04-Tasks/transfer-consent.md. These cover the state machine; the
sender's side of it is in tests/js/consent_harness.mjs.
"""

from __future__ import annotations

import pytest
from conftest import headers, send_file
from lanshare.db.repositories import TransferRepository


async def force_awaiting(app, transfer_id: str) -> None:
    """Put a transfer back into the state `create` will produce in Task 5."""
    connection = app.state.database.connection
    await TransferRepository.set_status(connection, transfer_id, "awaiting")


@pytest.mark.asyncio
async def test_a_waiting_transfer_can_still_be_cancelled(app, client, sender, receiver) -> None:
    """The sender must be able to give up while the recipient is deciding."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="a.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)

    cancelled = await client.delete(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
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
