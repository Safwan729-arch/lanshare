"""Regressions from the codebase review.

Each test here corresponds to something that was actually wrong, not to a
hypothetical. Where the bug was invisible to the existing tests, the test says
why, so nobody re-introduces it by "simplifying" the check.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from conftest import (
    LAN_CLIENT,
    LOOPBACK_CLIENT,
    TEST_CHUNK_SIZE,
    accept,
    headers,
    register,
    send_file,
)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from lanshare.config import Settings
from lanshare.discovery.udp import MAX_PEERS, Peer, PeerRegistry
from lanshare.main import create_app
from lanshare.services.storage import Storage, StorageError, unique_destination
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SUBPROTOCOL = "lanshare.v1"


# -- the WebSocket must be accepted before it is refused ---------------------


class RecordingApp:
    """Passes ASGI through while remembering what the app sent on a socket."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.websocket_messages: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def recording_send(message: Any) -> None:
            if scope["type"] == "websocket":
                self.websocket_messages.append(message["type"])
            await send(message)

        await self.app(scope, receive, recording_send)


def test_a_refused_socket_is_accepted_first(settings: Settings) -> None:
    """Closing *before* accept() loses the close code, and nothing caught it.

    An ASGI server turns a close-before-accept into an HTTP 403 handshake
    failure, so a real browser only ever sees code 1006 - indistinguishable
    from the server being down. The client would retry forever and the 4403 and
    4404 branches in `ws.js` would never run.

    Starlette's `TestClient` does not go through a handshake, so it reports the
    application code either way: the existing WebSocket tests asserted 4404 and
    passed all along, against a code path that did not exist in production.
    This test therefore checks the **message order**, which is the thing that
    actually differs, rather than the code the client ends up seeing.
    """
    recorder = RecordingApp(create_app(settings))

    with (
        TestClient(recorder, client=LOOPBACK_CLIENT) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            f"/ws?device_id={uuid.uuid4()}", subprotocols=[SUBPROTOCOL, "token.nonsense"]
        ) as websocket,
    ):
        websocket.receive_json()

    assert recorder.websocket_messages[:2] == ["websocket.accept", "websocket.close"], (
        f"expected accept then close, got {recorder.websocket_messages[:2]}"
    )


def test_our_subprotocol_is_only_echoed_when_offered(settings: Settings) -> None:
    """Returning a subprotocol the client never offered violates the protocol
    and a browser drops the connection. Only our own client sends it."""
    with TestClient(create_app(settings), client=LOOPBACK_CLIENT) as client:
        device_id = str(uuid.uuid4())
        response = client.post(
            "/api/devices/register", json={"device_id": device_id, "name": "Plain client"}
        )
        token = response.json()["token"]

        # No "lanshare.v1" offered - just the token.
        with client.websocket_connect(
            f"/ws?device_id={device_id}", subprotocols=[f"token.{token}"]
        ) as websocket:
            assert websocket.receive_json()["type"] in {"device.list", "device.joined"}


def test_a_non_object_frame_does_not_kill_the_connection(settings: Settings) -> None:
    """`receive_json` accepts any JSON value. A bare list used to reach
    `.get`, raising AttributeError - which the frame handler did not catch."""
    with TestClient(create_app(settings), client=LOOPBACK_CLIENT) as client:
        device_id = str(uuid.uuid4())
        token = client.post(
            "/api/devices/register", json={"device_id": device_id, "name": "iPhone"}
        ).json()["token"]

        with client.websocket_connect(
            f"/ws?device_id={device_id}", subprotocols=[SUBPROTOCOL, f"token.{token}"]
        ) as websocket:
            websocket.send_json([1, 2, 3])
            websocket.send_json("just a string")
            websocket.send_json({"type": "ping", "data": "not an object either"})

            for _ in range(8):
                if websocket.receive_json()["type"] == "pong":
                    return
            pytest.fail("the connection stopped answering after a malformed frame")


# -- a chunk upload is bounded while it streams ------------------------------


async def test_an_oversized_chunk_is_refused(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """The size used to be checked only after the bytes were already on disk,
    so a sender ignoring the chunk size could fill the volume first."""
    response = await client.post(
        "/api/transfers",
        json={"filename": "x.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = response.json()["transfer_id"]
    await accept(transfer_id, receiver)

    upload = await client.put(
        f"/api/transfers/{transfer_id}/chunks/0",
        content=b"x" * (TEST_CHUNK_SIZE * 4),
        headers=headers(sender),
    )
    assert upload.status_code == 400


async def test_the_write_stops_early_rather_than_auditing_afterwards(tmp_path: Path) -> None:
    """The distinction the API-level tests cannot see.

    Without the limit the whole body still lands on disk and is only deleted
    once the size is compared - so "nothing left behind" holds either way. What
    actually changed is that the stream is abandoned part-way, which is the
    difference between refusing 200 GiB and storing it first.
    """
    storage = Storage(incoming_dir=tmp_path / "in", temporary_dir=tmp_path / "tmp")
    transfer_id = str(uuid.uuid4())
    block = b"x" * 1024
    offered = 512 * 1024
    consumed = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal consumed
        for _ in range(offered // len(block)):
            consumed += len(block)
            yield block

    with pytest.raises(StorageError):
        await storage.write_chunk(transfer_id, 0, body(), limit=4 * 1024)

    assert consumed < offered // 4, (
        f"read {consumed} of {offered} bytes - the stream was not cut short"
    )
    directory = storage.transfer_dir(transfer_id)
    assert list(directory.iterdir()) == []


async def test_an_oversized_chunk_leaves_nothing_behind(
    app: FastAPI, client: AsyncClient, sender: str, receiver: str
) -> None:
    """Rejecting it is only half the point; the bytes must not survive."""
    response = await client.post(
        "/api/transfers",
        json={"filename": "x.bin", "size": 10, "receiver_id": receiver},
        headers=headers(sender),
    )
    transfer_id = response.json()["transfer_id"]

    await client.put(
        f"/api/transfers/{transfer_id}/chunks/0",
        content=b"x" * (TEST_CHUNK_SIZE * 4),
        headers=headers(sender),
    )

    storage: Storage = app.state.storage
    directory = storage.transfer_dir(transfer_id)
    leftovers = list(directory.iterdir()) if directory.is_dir() else []
    assert leftovers == [], f"partial data left on disk: {leftovers}"


# -- completing twice at once ------------------------------------------------


async def test_completing_twice_at_once_still_completes(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """Both callers used to pass the "already completed?" check and assemble.

    The loser found the chunks cleaned up, raised, and wrote `failed` over a
    transfer that had in fact succeeded - with the file sitting on disk.
    """
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="race.bin",
        payload=b"contents" * 100,
    )

    first, second = await asyncio.gather(
        client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender)),
        client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender)),
    )

    assert {first.status_code, second.status_code} == {200}
    assert first.json()["sha256"] == second.json()["sha256"]

    final = await client.get(f"/api/transfers/{transfer_id}", headers=headers(sender))
    assert final.json()["status"] == "completed"


async def test_a_failed_assembly_does_not_leak_server_paths(
    client: AsyncClient, sender: str, receiver: str
) -> None:
    """The error is stored on the row and replayed in history, and OSError
    carries absolute paths. The detail belongs in the log."""
    transfer_id = await send_file(
        client,
        sender_id=sender,
        receiver_id=receiver,
        filename="gone.bin",
        payload=b"data" * 100,
        skip={0},
    )
    response = await client.post(f"/api/transfers/{transfer_id}/complete", headers=headers(sender))
    body = response.json()
    detail = str(body.get("detail", ""))
    assert "\\" not in detail and "/" not in detail, f"path leaked: {detail!r}"


# -- destination names are reserved, not guessed -----------------------------


def test_unique_destination_reserves_the_name(tmp_path: Path) -> None:
    """It must create the file, not merely report that it is free.

    Checking `exists()` and moving afterwards leaves a gap in which two
    transfers of the same name both see it free and the second overwrites the
    first. Never overwriting is a promise this project makes.
    """
    first = unique_destination(tmp_path, "photo.jpg")
    assert first.exists(), "the name was not claimed, only checked"

    second = unique_destination(tmp_path, "photo.jpg")
    assert second != first
    assert second.name == "photo (1).jpg"
    assert second.exists()


def test_two_transfers_of_the_same_name_both_survive(
    tmp_path: Path,
) -> None:
    names = {unique_destination(tmp_path, "report.pdf").name for _ in range(5)}
    assert len(names) == 5, "reservations collided"


# -- registration cannot be used to flood the approval list ------------------


@pytest_asyncio.fixture
async def small_cap(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, AsyncClient]]:
    """A server that allows only two devices to wait at once."""
    settings = Settings(
        server_name="Test PC",
        incoming_dir=tmp_path / "incoming",
        temporary_dir=tmp_path / "temporary",
        data_dir=tmp_path / "data",
        chunk_size=TEST_CHUNK_SIZE,
        max_pending_devices=2,
        enable_mdns=False,
        enable_udp_discovery=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        lan = AsyncClient(
            transport=ASGITransport(app=app, client=LAN_CLIENT), base_url="http://testserver"
        )
        host = AsyncClient(
            transport=ASGITransport(app=app, client=LOOPBACK_CLIENT), base_url="http://testserver"
        )
        async with lan, host:
            yield lan, host


async def register_raw(client: AsyncClient, name: str) -> int:
    response = await client.post(
        "/api/devices/register", json={"device_id": str(uuid.uuid4()), "name": name}
    )
    return response.status_code


async def test_pending_registrations_are_capped(
    small_cap: tuple[AsyncClient, AsyncClient],
) -> None:
    """Registration cannot require a credential, so the only defence against a
    LAN neighbour inserting rows forever is a ceiling."""
    lan, _ = small_cap
    assert await register_raw(lan, "phone 1") == 200
    assert await register_raw(lan, "phone 2") == 200
    assert await register_raw(lan, "phone 3") == 429


async def test_the_host_can_still_register_while_the_cap_is_hit(
    small_cap: tuple[AsyncClient, AsyncClient],
) -> None:
    """The cap must never be a way to lock the host out of its own machine.

    Loopback registers as trusted and so never joins the waiting list.
    """
    lan, host = small_cap
    await register_raw(lan, "flood 1")
    await register_raw(lan, "flood 2")
    assert await register_raw(lan, "flood 3") == 429

    assert await register_raw(host, "the host's browser") == 200


async def test_approving_a_device_frees_a_slot(
    small_cap: tuple[AsyncClient, AsyncClient],
) -> None:
    lan, host = small_cap
    approver = await register(host, "Host browser")

    waiting_id = str(uuid.uuid4())
    await lan.post("/api/devices/register", json={"device_id": waiting_id, "name": "phone 1"})
    await register_raw(lan, "phone 2")
    assert await register_raw(lan, "phone 3") == 429

    approved = await host.post(
        f"/api/devices/{waiting_id}/trust",
        json={"decision": "approve"},
        headers=headers(approver),
    )
    assert approved.status_code == 200
    assert await register_raw(lan, "phone 4") == 200


# -- a transfer cannot be aimed at an unapproved device ----------------------


async def test_a_transfer_to_an_unapproved_device_is_refused(
    client: AsyncClient, lan_client: AsyncClient, sender: str
) -> None:
    """Being trusted yourself does not let you push a transfer at a device the
    host has not approved."""
    waiting_id = str(uuid.uuid4())
    await lan_client.post(
        "/api/devices/register", json={"device_id": waiting_id, "name": "Unapproved phone"}
    )

    response = await client.post(
        "/api/transfers",
        json={"filename": "x.bin", "size": 4, "receiver_id": waiting_id},
        headers=headers(sender),
    )
    assert response.status_code == 403


# -- the peer table cannot be grown without limit ----------------------------


def make_peer(server_id: str, *, last_seen: float | None = None) -> Peer:
    # A real monotonic reading, not 0.0: `current()` prunes against the live
    # clock, so synthetic timestamps make every peer look expired and the
    # assertions below pass against an empty registry.
    return Peer(
        server_id=server_id,
        name=f"peer-{server_id}",
        address="192.168.1.9",
        port=8080,
        version="1",
        last_seen=time.monotonic() if last_seen is None else last_seen,
    )


def test_the_peer_table_is_capped() -> None:
    """Announcements are unauthenticated and the sender picks its own id, so
    one neighbour can otherwise mint a new peer per packet."""
    registry = PeerRegistry(ttl_seconds=30)
    for index in range(MAX_PEERS + 25):
        registry.remember(make_peer(f"id-{index}"))

    # Exactly at the cap, not merely "not more": an empty registry would also
    # satisfy <=, and that is how this test first passed while doing nothing.
    assert len(registry.current()) == MAX_PEERS


def test_a_known_peer_is_always_refreshed_even_at_the_cap() -> None:
    """A flood must not be able to evict a genuine neighbour - it may only
    fail to add itself."""
    registry = PeerRegistry(ttl_seconds=30)
    real = make_peer("the-real-one")
    registry.remember(real)

    for index in range(MAX_PEERS + 10):
        registry.remember(make_peer(f"junk-{index}"))

    registry.remember(make_peer("the-real-one"))
    known = {peer.server_id for peer in registry.current()}
    assert "the-real-one" in known
    assert len(known) == MAX_PEERS


# -- the chunk scan survives a file vanishing under it -----------------------


def test_the_chunk_scan_tolerates_a_file_disappearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a transfer deletes chunks while an upload is still scanning.

    The window is between the `glob` and the `stat`, so simply deleting the
    file first proves nothing - the glob would not return it. The stat itself
    has to fail, which is what this forces.
    """
    storage = Storage(incoming_dir=tmp_path / "in", temporary_dir=tmp_path / "tmp")
    transfer_id = str(uuid.uuid4())
    directory = storage.transfer_dir(transfer_id)
    directory.mkdir(parents=True)
    for index in range(3):
        (directory / f"{index}.part").write_bytes(b"data")

    real_stat = Path.stat

    def stat_with_one_gone(self: Path, **kwargs: Any) -> Any:
        if self.name == "1.part":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_with_one_gone)

    # The survivors are still reported, and the one that vanished is simply
    # treated as a chunk we do not have - which is exactly what it is.
    assert storage.received_chunks(transfer_id) == [0, 2]
