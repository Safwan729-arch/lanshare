"""Pairing and token authentication (Phase 5).

These are the tests that matter most in the project: everything here is a
property someone could break without any other test noticing.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import approve, headers, register, send_file
from fastapi import FastAPI
from httpx import AsyncClient
from lanshare.models import DeviceResponse, DeviceWithAddressResponse
from lanshare.services.auth import (
    generate_token,
    hash_token,
    initial_trust_state,
    is_loopback,
    verify_token,
)

# -- the token primitives ----------------------------------------------------


def test_tokens_are_unique_and_long() -> None:
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 40 for t in tokens)


def test_hashing_is_stable_but_not_reversible() -> None:
    token = generate_token()
    assert hash_token(token) == hash_token(token)
    assert token not in hash_token(token)


def test_verify_accepts_the_right_token_only() -> None:
    token = generate_token()
    stored = hash_token(token)

    assert verify_token(token, stored) is True
    assert verify_token(generate_token(), stored) is False


@pytest.mark.parametrize(
    ("token", "stored"),
    [(None, "abc"), ("abc", None), (None, None), ("", "abc"), ("abc", "")],
)
def test_verify_rejects_missing_values(token: str | None, stored: str | None) -> None:
    """A device with no token stored must not be authenticated by an empty one."""
    assert verify_token(token, stored) is False


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("192.168.1.5", False),
        ("10.0.0.1", False),
        ("testclient", False),
        ("", False),
        (None, False),
    ],
)
def test_loopback_detection(address: str | None, expected: bool) -> None:
    assert is_loopback(address) is expected


def test_only_loopback_is_trusted_on_sight() -> None:
    assert initial_trust_state("127.0.0.1") == "trusted"
    assert initial_trust_state("192.168.1.5") == "pending"
    assert initial_trust_state(None) == "pending"


# -- who gets trusted --------------------------------------------------------


async def test_host_registers_as_trusted(client: AsyncClient) -> None:
    """The browser on the machine running the server needs no approval."""
    device_id = str(uuid.uuid4())
    response = await client.post(
        "/api/devices/register", json={"device_id": device_id, "name": "Host browser"}
    )
    assert response.json()["device"]["trust_state"] == "trusted"


async def test_lan_device_starts_pending(lan_client: AsyncClient) -> None:
    """A phone on the LAN has to be approved first."""
    device_id = str(uuid.uuid4())
    response = await lan_client.post(
        "/api/devices/register", json={"device_id": device_id, "name": "iPhone"}
    )
    body = response.json()
    assert body["device"]["trust_state"] == "pending"
    assert body["token"], "still issued a token - it just cannot do much yet"


async def test_pending_device_records_where_it_came_from(lan_client: AsyncClient) -> None:
    response = await lan_client.post(
        "/api/devices/register", json={"device_id": str(uuid.uuid4()), "name": "iPhone"}
    )
    assert response.json()["device"]["first_address"] == "192.168.1.50"


# -- what a pending device may not do ----------------------------------------


async def pending_device(lan_client: AsyncClient) -> tuple[str, dict[str, str]]:
    device_id = str(uuid.uuid4())
    response = await lan_client.post(
        "/api/devices/register", json={"device_id": device_id, "name": "iPhone"}
    )
    token = response.json()["token"]
    return device_id, {"X-Device-Id": device_id, "Authorization": f"Bearer {token}"}


async def test_pending_device_cannot_list_devices(lan_client: AsyncClient) -> None:
    _, auth = await pending_device(lan_client)
    response = await lan_client.get("/api/devices", headers=auth)
    assert response.status_code == 403


async def test_pending_device_cannot_start_a_transfer(
    lan_client: AsyncClient, client: AsyncClient, receiver: str
) -> None:
    _, auth = await pending_device(lan_client)
    response = await lan_client.post(
        "/api/transfers",
        json={"filename": "x.bin", "size": 10, "receiver_id": receiver},
        headers=auth,
    )
    assert response.status_code == 403


async def test_pending_device_cannot_approve_anyone(lan_client: AsyncClient) -> None:
    """Otherwise an unapproved device could simply approve itself."""
    device_id, auth = await pending_device(lan_client)
    response = await lan_client.post(
        f"/api/devices/{device_id}/trust", json={"decision": "approve"}, headers=auth
    )
    assert response.status_code == 403


async def test_pending_device_may_check_its_own_status(lan_client: AsyncClient) -> None:
    """The one thing it is allowed to do, so it knows when to stop waiting."""
    device_id, auth = await pending_device(lan_client)
    response = await lan_client.get("/api/devices/me", headers=auth)
    assert response.status_code == 200
    assert response.json()["id"] == device_id
    assert response.json()["trust_state"] == "pending"


# -- credentials -------------------------------------------------------------


async def test_a_request_without_a_token_is_rejected(client: AsyncClient, sender: str) -> None:
    response = await client.get("/api/devices", headers={"X-Device-Id": sender})
    assert response.status_code == 401


async def test_a_request_with_the_wrong_token_is_rejected(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """Knowing someone's device id must not be enough to act as them."""
    stolen = headers(receiver)["Authorization"]
    response = await client.get(
        "/api/devices", headers={"X-Device-Id": sender, "Authorization": stolen}
    )
    assert response.status_code == 401


async def test_unknown_device_and_bad_token_look_the_same(client: AsyncClient, sender: str) -> None:
    """The error must not reveal whether a device id exists."""
    unknown = await client.get(
        "/api/devices",
        headers={"X-Device-Id": str(uuid.uuid4()), "Authorization": "Bearer nope"},
    )
    wrong = await client.get(
        "/api/devices", headers={"X-Device-Id": sender, "Authorization": "Bearer nope"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


@pytest.mark.parametrize("value", ["", "Bearer", "Bearer ", "Basic abc", "abc", "bearer"])
async def test_malformed_authorization_headers_are_rejected(
    client: AsyncClient, sender: str, value: str
) -> None:
    response = await client.get(
        "/api/devices", headers={"X-Device-Id": sender, "Authorization": value}
    )
    assert response.status_code == 401


async def test_re_registering_someone_elses_id_is_refused(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    """The escalation this protects against.

    Without it, anyone who learned a trusted device's id could re-register it,
    be handed a fresh token, and inherit its trust.
    """
    response = await lan_client.post(
        "/api/devices/register", json={"device_id": sender, "name": "Impostor"}
    )
    assert response.status_code == 403
    assert "clear local data" in response.json()["detail"]


async def test_refreshing_does_not_change_trust_or_mint_a_token(
    client: AsyncClient, lan_client: AsyncClient
) -> None:
    """Re-registering must not be a back door to becoming trusted."""
    device_id, auth = await pending_device(lan_client)

    # Re-register through the *loopback* client, which would normally be
    # trusted on sight. The existing row must win.
    response = await client.post(
        "/api/devices/register",
        json={"device_id": device_id, "name": "iPhone"},
        headers=auth,
    )
    assert response.status_code == 200
    assert response.json()["device"]["trust_state"] == "pending"
    assert response.json()["token"] is None


# -- approving ---------------------------------------------------------------


async def test_approval_lets_a_device_act(
    client: AsyncClient, lan_client: AsyncClient, sender: str, receiver: str
) -> None:
    device_id, auth = await pending_device(lan_client)

    blocked = await lan_client.get("/api/devices", headers=auth)
    assert blocked.status_code == 403

    await approve(client, sender, device_id)

    allowed = await lan_client.get("/api/devices", headers=auth)
    assert allowed.status_code == 200


async def test_pending_list_shows_waiting_devices(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    device_id, _ = await pending_device(lan_client)

    response = await client.get("/api/devices/pending", headers=headers(sender))
    assert response.status_code == 200
    assert device_id in {d["id"] for d in response.json()["devices"]}


async def test_approved_device_leaves_the_pending_list(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    device_id, _ = await pending_device(lan_client)
    await approve(client, sender, device_id)

    response = await client.get("/api/devices/pending", headers=headers(sender))
    assert device_id not in {d["id"] for d in response.json()["devices"]}


async def test_denying_removes_the_device_entirely(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    """A denied device is deleted, so its token stops working immediately."""
    device_id, auth = await pending_device(lan_client)

    response = await client.post(
        f"/api/devices/{device_id}/trust",
        json={"decision": "deny"},
        headers=headers(sender),
    )
    assert response.status_code == 200

    after = await lan_client.get("/api/devices/me", headers=auth)
    assert after.status_code == 401


async def test_blocking_keeps_the_record_but_refuses_access(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    device_id, auth = await pending_device(lan_client)
    await approve(client, sender, device_id)

    response = await client.post(
        f"/api/devices/{device_id}/trust",
        json={"decision": "block"},
        headers=headers(sender),
    )
    assert response.status_code == 200
    assert response.json()["trust_state"] == "blocked"

    refused = await lan_client.get("/api/devices", headers=auth)
    assert refused.status_code == 403
    assert "blocked" in refused.json()["detail"].lower()


async def test_the_host_device_cannot_be_blocked(
    client: AsyncClient, sender: str, app: object
) -> None:
    """Locking yourself out of your own machine should not be possible."""
    server_id = app.state.server_device_id  # type: ignore[attr-defined]
    response = await client.post(
        f"/api/devices/{server_id}/trust",
        json={"decision": "block"},
        headers=headers(sender),
    )
    assert response.status_code == 403


async def test_deciding_on_an_unknown_device_is_404(client: AsyncClient, sender: str) -> None:
    response = await client.post(
        f"/api/devices/{uuid.uuid4()}/trust",
        json={"decision": "approve"},
        headers=headers(sender),
    )
    assert response.status_code == 404


async def test_invalid_decision_is_rejected(client: AsyncClient, sender: str) -> None:
    response = await client.post(
        f"/api/devices/{uuid.uuid4()}/trust",
        json={"decision": "make-admin"},
        headers=headers(sender),
    )
    assert response.status_code == 422


# -- untrusted devices stay out of the device list ---------------------------


async def test_pending_devices_are_not_listed_as_send_targets(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    """You should never be offered a device that cannot receive."""
    device_id, _ = await pending_device(lan_client)

    response = await client.get("/api/devices", headers=headers(sender))
    assert device_id not in {d["id"] for d in response.json()["devices"]}


async def test_health_is_the_only_open_endpoint(client: AsyncClient) -> None:
    """Liveness stays open - it is how you check the server is up before any
    device exists, and it returns only what the startup banner prints."""
    assert (await client.get("/api/health")).status_code == 200


async def test_server_info_is_not_open(client: AsyncClient) -> None:
    """It carries the host's device id, the LAN URL and the pairing QR.

    Nothing unapproved needs it: the browser registers first and the waiting
    screen never calls it. It used to be open on the theory that a device needs
    it before it has a token, which was simply not true.
    """
    assert (await client.get("/api/server-info")).status_code == 401


async def test_a_pending_device_cannot_read_server_info(lan_client: AsyncClient) -> None:
    _, auth = await pending_device(lan_client)
    assert (await lan_client.get("/api/server-info", headers=auth)).status_code == 403


async def test_a_trusted_device_can_read_server_info(client: AsyncClient, sender: str) -> None:
    assert (await client.get("/api/server-info", headers=headers(sender))).status_code == 200


async def test_register_is_open(lan_client: AsyncClient) -> None:
    """Registration cannot require a token - it is where tokens come from."""
    response = await lan_client.post(
        "/api/devices/register", json={"device_id": str(uuid.uuid4()), "name": "New"}
    )
    assert response.status_code == 200


# -- file access is restricted to the two devices involved -------------------


async def test_download_requires_a_credential(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """The endpoint serves real file contents; it cannot be open."""
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="private.txt",
        payload=b"secret",
    )
    await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))

    response = await client.get(f"/api/files/{transfer_id}/download")
    # 401, not 422: no credential is an authentication failure, not a malformed
    # request. Absent and wrong must be indistinguishable from outside.
    assert response.status_code == 401


async def test_a_third_device_cannot_download_someone_elses_file(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """Trusted is not the same as involved."""
    bystander = await register(client, "Nosy laptop")
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="private.txt",
        payload=b"secret",
    )
    await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))

    theirs = await client.get(f"/api/files/{transfer_id}/download", headers=headers(receiver))
    assert theirs.status_code == 200

    nosy = await client.get(f"/api/files/{transfer_id}/download", headers=headers(bystander))
    assert nosy.status_code == 403


async def test_a_third_device_cannot_watch_someone_elses_transfer(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    bystander = await register(client, "Nosy laptop")
    transfer_id = await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="x.bin", payload=b"data"
    )

    assert (
        await client.get(f"/api/transfers/{transfer_id}", headers=headers(sender))
    ).status_code == 200
    assert (
        await client.get(f"/api/transfers/{transfer_id}", headers=headers(bystander))
    ).status_code == 403


async def test_history_is_scoped_to_the_caller(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """Filenames leak. One device must not see another's history."""
    bystander = await register(client, "Nosy laptop")
    await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="confidential.pdf",
        payload=b"data",
    )

    mine = await client.get("/api/transfers", headers=headers(sender))
    assert "confidential.pdf" in {t["filename"] for t in mine.json()["transfers"]}

    theirs = await client.get("/api/transfers", headers=headers(bystander))
    assert theirs.json()["transfers"] == []


async def test_history_cannot_be_widened_by_a_query_parameter(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """Passing someone else's id must not widen the scope."""
    bystander = await register(client, "Nosy laptop")
    await send_file(
        client, sender_id=sender, receiver_id=receiver, filename="x.pdf", payload=b"data"
    )

    response = await client.get(
        "/api/transfers", params={"device_id": sender}, headers=headers(bystander)
    )
    assert response.json()["transfers"] == []


async def test_peers_requires_trust(lan_client: AsyncClient) -> None:
    _, auth = await pending_device(lan_client)
    assert (await lan_client.get("/api/peers", headers=auth)).status_code == 403


# -- devices that predate tokens ---------------------------------------------

#: Any timestamp will do; a fixed one keeps the rows recognisable.
PRE_MIGRATION = "2026-09-01T00:00:00+00:00"


async def test_pending_list_hides_devices_that_have_no_token(
    app: FastAPI, client: AsyncClient, sender: str
) -> None:
    """A pre-migration row can never authenticate, so approving it is a no-op.

    Migration 001 left existing devices `pending` with a NULL `token_hash`.
    They showed up in the approval panel, but the real browser has long since
    reset to a fresh id, so pressing Allow changes nothing visible - a trap
    worth keeping out of the UI.
    """
    ghost_id = str(uuid.uuid4())
    conn = app.state.database.connection
    await conn.execute(
        "INSERT INTO devices "
        "(id, name, kind, user_agent, created_at, last_seen, trust_state, token_hash) "
        "VALUES (?, 'Old iPhone', 'browser', 'safari', ?, ?, 'pending', NULL)",
        (ghost_id, PRE_MIGRATION, PRE_MIGRATION),
    )
    await conn.commit()

    listed = await client.get("/api/devices/pending", headers=headers(sender))
    assert listed.status_code == 200
    assert ghost_id not in {d["id"] for d in listed.json()["devices"]}


async def test_a_tokenless_device_cannot_authenticate_at_all(
    app: FastAPI, client: AsyncClient
) -> None:
    """The reason the row is hidden: no credential can ever match NULL."""
    ghost_id = str(uuid.uuid4())
    conn = app.state.database.connection
    await conn.execute(
        "INSERT INTO devices "
        "(id, name, kind, user_agent, created_at, last_seen, trust_state, token_hash) "
        "VALUES (?, 'Old iPhone', 'browser', 'safari', ?, ?, 'pending', NULL)",
        (ghost_id, PRE_MIGRATION, PRE_MIGRATION),
    )
    await conn.commit()

    for auth in ({"X-Device-Id": ghost_id}, {"X-Device-Id": ghost_id, "Authorization": "Bearer x"}):
        assert (await client.get("/api/devices/me", headers=auth)).status_code == 401


async def test_a_real_pending_device_is_still_listed(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    """The filter must not hide devices that are genuinely waiting."""
    waiting, _ = await pending_device(lan_client)

    listed = await client.get("/api/devices/pending", headers=headers(sender))
    assert waiting in {d["id"] for d in listed.json()["devices"]}


# -- an address is told to whoever can act on it, and no one else -------------
#
# `first_address` is how the host tells its own phone from a neighbour's before
# approving it. It is of no use to a device that cannot approve anyone, so it
# belongs on the host's pending views and on a device's own record - not in the
# list every paired device reads, where nothing displays it anyway.


async def test_the_device_list_does_not_carry_addresses(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    listed = await client.get("/api/devices", headers=headers(sender))
    assert listed.status_code == 200

    devices = listed.json()["devices"]
    assert devices, "nothing was listed, so nothing was proved"
    for device in devices:
        assert "first_address" not in device, device


async def test_the_device_list_event_does_not_carry_addresses(
    app: FastAPI, client: AsyncClient, sender: str
) -> None:
    """`device.list` reaches every paired device."""
    from lanshare.api.devices import device_list_event

    body = await device_list_event(
        app.state.database.connection, app.state.connections, app.state.server_device_id
    )

    assert body["data"]["devices"], "nothing was listed, so nothing was proved"
    for device in body["data"]["devices"]:
        assert "first_address" not in device, device


def test_the_plain_device_model_has_no_address() -> None:
    """Pins every consumer of it at once - `device.joined` among them."""
    assert "first_address" not in DeviceResponse.model_fields
    assert "first_address" in DeviceWithAddressResponse.model_fields


async def test_the_host_still_sees_where_a_waiting_device_came_from(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    waiting, _ = await pending_device(lan_client)

    listed = await client.get("/api/devices/pending", headers=headers(sender))
    waiting_row = next(d for d in listed.json()["devices"] if d["id"] == waiting)
    assert waiting_row["first_address"] == "192.168.1.50"
