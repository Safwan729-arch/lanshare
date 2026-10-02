"""Shared fixtures. Every test gets its own storage dirs and database."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from lanshare.config import Settings
from lanshare.main import create_app

# Small enough that a modest test file still spans several chunks.
TEST_CHUNK_SIZE = 64 * 1024

# Loopback, so devices registering through it are trusted automatically -
# exactly like the host's own browser at http://localhost:8080.
LOOPBACK_CLIENT = ("127.0.0.1", 123)

# A stand-in for a phone on the LAN, which must be approved before it can act.
LAN_CLIENT = ("192.168.1.50", 5000)

#: device id -> token, filled in by `register`. Keeping it here means every
#: existing `headers(device_id)` call site keeps working now that requests
#: have to carry a credential.
TOKENS: dict[str, str] = {}

#: The app the current test is running against, so helpers that need the
#: connection registry can reach it without every call site passing it down.
#: Set by the `app` fixture, in the same spirit as TOKENS above.
CURRENT_APP: list[FastAPI] = []


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        server_name="Test PC",
        incoming_dir=tmp_path / "incoming",
        temporary_dir=tmp_path / "temporary",
        data_dir=tmp_path / "data",
        chunk_size=TEST_CHUNK_SIZE,
        max_file_size=8 * 1024 * 1024,
        # Never advertise from a test: it would put real records on the tester's
        # network, collide between tests, and make the suite depend on multicast.
        enable_mdns=False,
        # Likewise, no broadcasting - and two tests binding the same UDP port
        # would fight over it.
        enable_udp_discovery=False,
    )


@pytest_asyncio.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    # Runs the same lifespan uvicorn would, so app.state is fully wired.
    CURRENT_APP.append(application)
    try:
        async with application.router.lifespan_context(application):
            yield application
    finally:
        CURRENT_APP.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client that looks like it is on the host machine."""
    transport = ASGITransport(app=app, client=LOOPBACK_CLIENT)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
        TOKENS.clear()


@pytest_asyncio.fixture
async def lan_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client that looks like a phone on the LAN, so it starts out pending."""
    transport = ASGITransport(app=app, client=LAN_CLIENT)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest_asyncio.fixture
async def sender(client: AsyncClient) -> str:
    device_id = await register(client, "iPhone - Safari")
    online(device_id)  # a device that can be sent a file has its page open
    return device_id


@pytest_asyncio.fixture
async def receiver(client: AsyncClient) -> str:
    device_id = await register(client, "Desktop - Firefox")
    online(device_id)
    return device_id


async def register(client: AsyncClient, name: str) -> str:
    """Register a device, remember its token, and return its id."""
    device_id = str(uuid.uuid4())
    response = await client.post(
        "/api/devices/register",
        json={"device_id": device_id, "name": name, "user_agent": "pytest"},
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["token"], "a new device must be issued a token"
    TOKENS[device_id] = body["token"]
    return device_id


def headers(device_id: str) -> dict[str, str]:
    """Identity plus credential, as every acting request needs."""
    built = {"X-Device-Id": device_id}
    token = TOKENS.get(device_id)
    if token:
        built["Authorization"] = f"Bearer {token}"
    return built


async def approve(client: AsyncClient, approver_id: str, device_id: str) -> None:
    response = await client.post(
        f"/api/devices/{device_id}/trust",
        json={"decision": "approve"},
        headers=headers(approver_id),
    )
    assert response.status_code == 200, response.text


async def accept(transfer_id: str, receiver_id: str) -> None:
    """Answer yes to a transfer, as the device it was sent to.

    A file addressed to the PC has no browser behind its row, so it is the host's
    page that answers: a loopback device, which the app treats as the host.
    """
    app = CURRENT_APP[-1]
    if receiver_id == app.state.server_device_id:
        transport = ASGITransport(app=app, client=LOOPBACK_CLIENT)
        async with AsyncClient(transport=transport, base_url="http://testserver") as host:
            host_id = await register(host, "Host page")
            online(host_id)
            response = await host.post(
                f"/api/transfers/{transfer_id}/consent",
                json={"decision": "accept"},
                headers=headers(host_id),
            )
    else:
        transport = ASGITransport(app=app, client=LOOPBACK_CLIENT)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.post(
                f"/api/transfers/{transfer_id}/consent",
                json={"decision": "accept"},
                headers=headers(receiver_id),
            )
    assert response.status_code == 200, response.text


async def send_file(
    client: AsyncClient,
    *,
    sender_id: str,
    receiver_id: str,
    filename: str,
    payload: bytes,
    mime_type: str = "application/octet-stream",
    skip: set[int] | None = None,
) -> str:
    """Init a transfer and upload its chunks. Returns the transfer id.

    ``skip`` leaves those chunk indexes unsent, which is how the resume tests
    create a half-finished upload.
    """
    skip = skip or set()
    if receiver_id != CURRENT_APP[-1].state.server_device_id:
        online(receiver_id)
    response = await client.post(
        "/api/transfers",
        json={
            "filename": filename,
            "size": len(payload),
            "receiver_id": receiver_id,
            "mime_type": mime_type,
        },
        headers=headers(sender_id),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    transfer_id = body["transfer_id"]
    chunk_size = body["chunk_size"]

    await accept(transfer_id, receiver_id)

    for index in range(body["total_chunks"]):
        if index in skip:
            continue
        chunk = payload[index * chunk_size : (index + 1) * chunk_size]
        upload = await client.put(
            f"/api/transfers/{transfer_id}/chunks/{index}",
            content=chunk,
            headers=headers(sender_id),
        )
        assert upload.status_code == 200, upload.text

    return transfer_id


class FakeSocket:
    """A stand-in for a browser's WebSocket.

    The suite talks HTTP, so no test device is ever "connected". Registering one
    of these is how a test says "this device has its page open", and it doubles
    as a record of what the server sent there.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


def bring_online(app, device_id: str) -> FakeSocket:
    """Give a device a connection, as if its page were open."""
    socket = FakeSocket()
    app.state.connections.add(device_id, socket)
    return socket


def online(device_id: str) -> FakeSocket:
    """Bring a device online in the app the current test is running against."""
    return bring_online(CURRENT_APP[-1], device_id)
