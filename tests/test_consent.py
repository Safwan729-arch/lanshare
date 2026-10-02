"""Per-transfer consent: nothing is written until the recipient says yes.

See vault/04-Tasks/transfer-consent.md. These cover the state machine; the
sender's side of it is in tests/js/consent_harness.mjs.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import approve, bring_online, headers, online, register, send_file
from lanshare.db.repositories import DeviceRepository, TransferRepository
from lanshare.main import create_app
from lanshare.services.errors import Forbidden


async def force_awaiting(app, transfer_id: str) -> None:
    """Put a transfer that `send_file` already accepted back into `awaiting`,
    to test the guards on their own."""
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

    `awaiting` is outside `ACTIVE_STATUSES`, so `write_chunk` refuses it, and
    that is the only reason a file cannot be pushed before the recipient has
    agreed to receive it.
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


async def test_a_host_may_answer_for_the_pc(app, client, lan_client, sender) -> None:
    """A file addressed to the PC has no browser behind it to accept.

    Only a host may stand in: a trusted phone is not one.
    """
    service = app.state.transfer_service
    phone = await register(lan_client, "Phone")
    await approve(client, sender, phone)
    transfer_id = await send_file(
        lan_client,
        sender_id=phone,
        receiver_id=app.state.server_device_id,
        filename="e.bin",
        payload=b"x" * 10,
    )
    transfer = await TransferRepository.get(app.state.database.connection, transfer_id)

    # `sender` registered over loopback, so it is a host device.
    await service.require_may_answer(transfer, sender)

    with pytest.raises(Forbidden):
        await service.require_may_answer(transfer, phone)


async def test_a_transfer_for_the_pc_is_announced_to_the_host(
    app, client, lan_client, sender
) -> None:
    """The host's page is a different device from the row the file is addressed to.

    Sent from a phone on the LAN deliberately: with a loopback sender the test
    cannot fail, because the sender is itself a host and is in the audience
    either way.
    """
    service = app.state.transfer_service
    phone = await register(lan_client, "Phone")
    bystander = await register(lan_client, "Another phone")
    await approve(client, sender, phone)
    await approve(client, sender, bystander)

    transfer_id = await send_file(
        lan_client,
        sender_id=phone,
        receiver_id=app.state.server_device_id,
        filename="f.bin",
        payload=b"x" * 10,
    )
    transfer = await TransferRepository.get(app.state.database.connection, transfer_id)

    audience = await service._audience(transfer)
    assert sender in audience, "the host's own page must hear about it"
    assert bystander not in audience, "a trusted device that is not the host must not"


async def test_accepting_unblocks_the_upload(app, client, sender, receiver) -> None:
    """The whole point: the bytes move only after someone says yes."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="g.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)
    sender_socket = bring_online(app, sender)

    blocked = await client.put(
        f"/api/transfers/{transfer_id}/chunks/0", content=b"x" * 10, headers=headers(sender)
    )
    assert blocked.status_code == 409
    assert "not accepted" in blocked.json()["detail"]

    accepted = await client.post(
        f"/api/transfers/{transfer_id}/consent",
        json={"decision": "accept"},
        headers=headers(receiver),
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "pending"
    told = [m for m in sender_socket.sent if m["type"] == "transfer.accepted"]
    assert len(told) == 1
    assert told[0]["data"]["transfer_id"] == transfer_id
    assert not [m for m in sender_socket.sent if m["type"] == "transfer.declined"]

    allowed = await client.put(
        f"/api/transfers/{transfer_id}/chunks/0", content=b"x" * 10, headers=headers(sender)
    )
    assert allowed.status_code == 200


async def test_declining_is_terminal(app, client, sender, receiver) -> None:
    """A refusal ends the transfer; it cannot be restarted by uploading anyway."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="h.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)
    sender_socket = bring_online(app, sender)

    declined = await client.post(
        f"/api/transfers/{transfer_id}/consent",
        json={"decision": "decline"},
        headers=headers(receiver),
    )
    assert declined.status_code == 200
    assert declined.json()["status"] == "declined"
    assert declined.json()["error"] == "Declined"
    told = [m for m in sender_socket.sent if m["type"] == "transfer.declined"]
    assert len(told) == 1
    assert told[0]["data"]["error"] == "Declined"
    assert not [m for m in sender_socket.sent if m["type"] == "transfer.accepted"]

    blocked = await client.put(
        f"/api/transfers/{transfer_id}/chunks/0", content=b"x" * 10, headers=headers(sender)
    )
    assert blocked.status_code == 409


async def test_a_third_device_cannot_answer(app, client, lan_client, sender, receiver) -> None:
    """Being trusted is not being involved."""
    stranger = await register(lan_client, "Stranger")
    await approve(client, sender, stranger)

    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="i.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)

    refused = await lan_client.post(
        f"/api/transfers/{transfer_id}/consent",
        json={"decision": "accept"},
        headers=headers(stranger),
    )
    assert refused.status_code == 403


async def test_answering_twice_is_refused(app, client, sender, receiver) -> None:
    """Two host pages could both be open; the first answer is the decision."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="j.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)
    body = {"decision": "accept"}

    first = await client.post(
        f"/api/transfers/{transfer_id}/consent", json=body, headers=headers(receiver)
    )
    second = await client.post(
        f"/api/transfers/{transfer_id}/consent", json=body, headers=headers(receiver)
    )
    assert first.status_code == 200
    assert second.status_code == 409


async def test_accepting_an_expired_request_says_so(app, client, sender, receiver) -> None:
    """A request that timed out must not look like a server fault."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="k.bin", payload=b"x" * 10
    )
    connection = app.state.database.connection
    await TransferRepository.set_status(connection, transfer_id, "declined", error="No answer")

    late = await client.post(
        f"/api/transfers/{transfer_id}/consent",
        json={"decision": "accept"},
        headers=headers(receiver),
    )
    assert late.status_code == 409
    assert "expired" in late.json()["detail"]


@pytest.mark.parametrize("decision", ["maybe", "", "APPROVE", "yes"])
async def test_a_decision_must_be_accept_or_decline(
    app, client, sender, receiver, decision
) -> None:
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="l.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)
    response = await client.post(
        f"/api/transfers/{transfer_id}/consent",
        json={"decision": decision},
        headers=headers(receiver),
    )
    assert response.status_code == 422


async def test_accepting_a_request_someone_declined_says_so(app, client, sender, receiver) -> None:
    """The second host page must not be told a manual refusal was a timeout."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="n.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)
    url = f"/api/transfers/{transfer_id}/consent"

    await client.post(url, json={"decision": "decline"}, headers=headers(receiver))
    late = await client.post(url, json={"decision": "accept"}, headers=headers(receiver))

    assert late.status_code == 409
    assert "already declined" in late.json()["detail"]
    assert "expired" not in late.json()["detail"]


async def test_completing_a_waiting_transfer_says_it_is_not_accepted(
    app, client, sender, receiver
) -> None:
    """A client that skipped the upload can still call complete."""
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="o.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)

    refused = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    assert refused.status_code == 409
    assert "not accepted" in refused.json()["detail"]


async def test_two_answers_at_once_produce_one_outcome(app, client, sender, receiver) -> None:
    """Two host pages can both be open, and a timer will later race them too.

    Both readers see `awaiting` before either writes, so without an atomic
    transition both would "succeed" and the loser's event would contradict the
    winner's.
    """
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="m.bin", payload=b"x" * 10
    )
    await force_awaiting(app, transfer_id)

    async def answer(decision: str):
        return await client.post(
            f"/api/transfers/{transfer_id}/consent",
            json={"decision": decision},
            headers=headers(receiver),
        )

    first, second = await asyncio.gather(answer("accept"), answer("decline"))
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409], [first.text, second.text]


async def test_a_new_transfer_waits_for_the_recipient(app, client, sender, receiver) -> None:
    """The feature, in one test: no bytes until someone says yes."""
    created = await client.post(
        "/api/transfers",
        json={"filename": "n.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    assert created.status_code == 201
    assert created.json()["status"] == "awaiting"

    blocked = await client.put(
        f"/api/transfers/{created.json()['transfer_id']}/chunks/0",
        content=b"x" * 10,
        headers=headers(sender),
    )
    assert blocked.status_code == 409


async def test_sending_to_a_device_that_is_not_there_is_refused(
    app, client, sender, settings
) -> None:
    """Nothing is created, so there is nothing to clean up afterwards."""
    absent = await register(client, "A phone with no page open")
    # registered over loopback, so trusted, but no socket: nobody can answer
    created = await client.post(
        "/api/transfers",
        json={"filename": "o.bin", "size": 10, "receiver_id": absent},
        headers=headers(sender),
    )
    assert created.status_code == 409
    assert "isn't connected" in created.json()["detail"]
    assert (await client.get("/api/transfers", headers=headers(sender))).json()["transfers"] == []
    assert list(settings.incoming_dir.iterdir()) == []
    assert list(settings.temporary_dir.iterdir()) == []


async def test_a_file_for_the_pc_reaches_the_host_page(app, client, lan_client, sender) -> None:
    """The server's row holds no socket; the host's page must hear instead.

    Sent from a LAN phone on purpose. The sender is left out of the
    `transfer.incoming` audience (you do not need telling about your own
    send), and a loopback sender *is* the host page, so with one the host would
    correctly hear nothing and the test could not tell a working route from a
    broken one.
    """
    host_page = online(sender)  # the loopback browser *is* the host page
    phone = await register(lan_client, "Phone")
    await approve(client, sender, phone)
    online(phone)

    created = await lan_client.post(
        "/api/transfers",
        json={"filename": "p.bin", "size": 10, "receiver_id": app.state.server_device_id},
        headers=headers(phone),
    )
    assert created.status_code == 201
    assert created.json()["status"] == "awaiting"
    incoming = [m for m in host_page.sent if m["type"] == "transfer.incoming"]
    assert len(incoming) == 1
    assert incoming[0]["data"]["status"] == "awaiting"


async def test_a_phone_sending_to_the_pc_with_no_host_page_open_is_refused(
    app, client, lan_client
) -> None:
    """The PC's row holds no socket, so with no host page open nobody can answer."""
    offline_host = await register(client, "Host page, closed")  # a host with no socket
    phone = await register(lan_client, "Phone")
    await approve(client, offline_host, phone)
    online(phone)
    created = await lan_client.post(
        "/api/transfers",
        json={"filename": "q.bin", "size": 10, "receiver_id": app.state.server_device_id},
        headers=headers(phone),
    )
    assert created.status_code == 409
    assert "isn't connected" in created.json()["detail"]


async def test_the_sender_is_not_told_about_its_own_transfer(
    app, client, lan_client, sender, receiver
) -> None:
    """Without the exclusion the sender would be prompted to answer its own send."""
    receiver_socket = online(receiver)
    phone = await register(lan_client, "Phone")
    await approve(client, sender, phone)
    phone_socket = online(phone)
    created = await lan_client.post(
        "/api/transfers",
        json={"filename": "r.bin", "size": 10, "receiver_id": receiver},
        headers=headers(phone),
    )
    assert created.status_code == 201
    assert [m for m in receiver_socket.sent if m["type"] == "transfer.incoming"]
    assert not [m for m in phone_socket.sent if m["type"] == "transfer.incoming"]


async def test_a_file_you_send_to_your_own_pc_needs_no_permission(app, client, sender) -> None:
    """Consent protects you from other people's files, not from your own.

    The host's page and the PC are different device rows, so the UI offers this
    send - and the person who just pressed Send is the only one who could
    answer the prompt. Asking them is ceremony, and worse, nobody is listening:
    the sender is excluded from the announcement.
    """
    created = await client.post(
        "/api/transfers",
        json={"filename": "self.bin", "size": 10, "receiver_id": app.state.server_device_id},
        headers=headers(sender),
    )
    assert created.status_code == 201
    assert created.json()["status"] == "pending"

    uploaded = await client.put(
        f"/api/transfers/{created.json()['transfer_id']}/chunks/0",
        content=b"x" * 10,
        headers=headers(sender),
    )
    assert uploaded.status_code == 200


async def test_an_unanswered_request_expires(app, client, sender, receiver) -> None:
    """A sender must never be left waiting on somebody who walked away."""
    app.state.transfer_service._consent_timeout = 0.05
    created = await client.post(
        "/api/transfers",
        json={"filename": "q.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = created.json()["transfer_id"]
    assert created.json()["status"] == "awaiting"

    await asyncio.sleep(0.4)

    status = await client.get(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert status.json()["status"] == "declined"
    assert status.json()["error"] == "No answer"


async def test_answering_in_time_beats_the_timer(app, client, sender, receiver) -> None:
    """The timer must not fire on a request that was answered."""
    app.state.transfer_service._consent_timeout = 5
    created = await client.post(
        "/api/transfers",
        json={"filename": "r.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = created.json()["transfer_id"]

    accepted = await client.post(
        f"/api/transfers/{transfer_id}/consent",
        json={"decision": "accept"},
        headers=headers(receiver),
    )
    assert accepted.json()["status"] == "pending"
    assert transfer_id not in app.state.transfer_service._consent_timers


async def test_an_accepted_transfer_is_not_expired_later(app, client, sender, receiver) -> None:
    """The timer fires after the decision; it must not undo it.

    Worth its own test because the timer and the answer race by design: the
    timer is armed at creation and the person answers while it is running.
    """
    app.state.transfer_service._consent_timeout = 0.05
    created = await client.post(
        "/api/transfers",
        json={"filename": "s.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = created.json()["transfer_id"]
    await client.post(
        f"/api/transfers/{transfer_id}/consent",
        json={"decision": "accept"},
        headers=headers(receiver),
    )

    await asyncio.sleep(0.4)

    status = await client.get(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert status.json()["status"] == "pending", "an answered request was expired anyway"


async def test_a_restart_declines_what_was_still_waiting(
    app, client, sender, receiver, settings
) -> None:
    """After a restart nobody is waiting for an answer - the sender gave up."""
    created = await client.post(
        "/api/transfers",
        json={"filename": "t.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = created.json()["transfer_id"]

    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted):
        row = await TransferRepository.get(restarted.state.database.connection, transfer_id)

    assert row is not None
    assert row["status"] == "declined"


async def test_the_timers_stop_with_the_server(settings) -> None:
    """A timer outliving its server is a leak and a traceback in the log."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        service = application.state.transfer_service
        service._consent_timeout = 60
        service._arm_consent_timer("11111111-1111-1111-1111-111111111111")
        assert service._consent_timers

    assert not service._consent_timers


async def test_giving_up_while_waiting_stops_the_timer(app, client, sender, receiver) -> None:
    """A sender cancelling is an ordinary way out of `awaiting`.

    The timer would otherwise sit there until it fired on a row it can no
    longer change - harmless, but it is a task per abandoned send held for the
    whole window.
    """
    app.state.transfer_service._consent_timeout = 60
    created = await client.post(
        "/api/transfers",
        json={"filename": "u.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = created.json()["transfer_id"]
    assert transfer_id in app.state.transfer_service._consent_timers

    cancelled = await client.delete(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert cancelled.status_code == 200
    assert transfer_id not in app.state.transfer_service._consent_timers


async def test_a_reconnecting_page_is_told_what_it_missed(app, client, lan_client, sender) -> None:
    """A prompt must survive the recipient reloading the page.

    A file addressed to the PC names the server's own row, and `GET
    /api/transfers` is scoped to the caller - so the host's page, which is
    neither sender nor receiver, can never find it by asking. Without a replay
    on connect, a reload loses the prompt and the sender waits out the full
    window for an answer nobody can give.
    """
    phone = await register(lan_client, "Phone")
    await approve(client, sender, phone)
    online(sender)

    created = await lan_client.post(
        "/api/transfers",
        json={"filename": "v.bin", "size": 10, "receiver_id": app.state.server_device_id},
        headers=headers(phone),
    )
    assert created.json()["status"] == "awaiting"

    # The host's page comes back: a fresh socket, where the old one knew everything.
    reconnected = online(sender)
    await app.state.transfer_service.replay_requests(sender, reconnected)

    offered = [m for m in reconnected.sent if m["type"] == "transfer.incoming"]
    assert [m["data"]["transfer_id"] for m in offered] == [created.json()["transfer_id"]]
    assert offered[0]["data"]["status"] == "awaiting"


async def test_a_reconnecting_page_is_not_told_about_other_peoples_files(
    app, client, lan_client, sender, receiver
) -> None:
    """The replay must not become a way to learn what others are being sent."""
    phone = await register(lan_client, "Phone")
    await approve(client, sender, phone)
    online(receiver)

    await lan_client.post(
        "/api/transfers",
        json={"filename": "w.bin", "size": 10, "receiver_id": receiver},
        headers=headers(phone),
    )

    socket = online(phone)
    await app.state.transfer_service.replay_requests(phone, socket)
    assert [m for m in socket.sent if m["type"] == "transfer.incoming"] == []


async def test_one_dead_socket_does_not_stop_a_broadcast(app, client, sender, receiver) -> None:
    """A device that drops off the Wi-Fi mid-broadcast must not take others with it.

    `send` used to catch only RuntimeError and OSError, but every transport
    reports a dead peer in its own way - Starlette's test transport raises
    ClosedResourceError and `websockets` raises ConnectionClosed, neither of
    which is either of those. The exception escaped the loop, so one closed
    socket stopped the message reaching everyone after it.
    """

    class ExoticallyDeadSocket:
        async def send_json(self, message: dict) -> None:
            raise LookupError("some transport's own idea of a closed stream")

    connections = app.state.connections
    # A device of its own: the fixtures already hold a live socket each, and a
    # device stays online while any one of its sockets does.
    gone = await register(client, "A phone that walked away")
    connections.add(gone, ExoticallyDeadSocket())
    alive = online(receiver)

    await connections.broadcast({"type": "device.left", "data": {}})

    assert [m["type"] for m in alive.sent] == ["device.left"]
    assert not connections.is_online(gone), "a socket that cannot be sent to is dropped"
