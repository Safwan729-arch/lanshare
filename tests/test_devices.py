"""Health, server info and device registration."""

from __future__ import annotations

import uuid

from conftest import headers, register
from fastapi import FastAPI
from httpx import AsyncClient


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["server_name"] == "Test PC"


async def test_server_info_reports_a_lan_url(
    client: AsyncClient, app: FastAPI, sender: str
) -> None:
    response = await client.get("/api/server-info", headers=headers(sender))
    assert response.status_code == 200
    body = response.json()
    assert body["lan_url"].startswith("http://")
    assert body["server_device_id"] == app.state.server_device_id
    assert body["chunk_size"] > 0


async def test_register_is_an_upsert(client: AsyncClient) -> None:
    device_id = str(uuid.uuid4())

    first = await client.post(
        "/api/devices/register",
        json={"device_id": device_id, "name": "iPhone", "user_agent": "Safari"},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["device"]["name"] == "iPhone"
    token = body["token"]
    assert token, "a new device is issued a token"

    # Same id, new name, presenting the token: one row, updated.
    second = await client.post(
        "/api/devices/register",
        json={"device_id": device_id, "name": "Sam's iPhone", "user_agent": "Safari"},
        headers={"X-Device-Id": device_id, "Authorization": f"Bearer {token}"},
    )
    assert second.status_code == 200
    assert second.json()["device"]["name"] == "Sam's iPhone"
    assert second.json()["device"]["id"] == device_id
    assert second.json()["token"] is None, "a token is issued once, not on every refresh"


async def test_register_rejects_a_bad_device_id(client: AsyncClient) -> None:
    response = await client.post(
        "/api/devices/register", json={"device_id": "../../evil", "name": "x"}
    )
    assert response.status_code == 422


async def test_register_rejects_an_empty_name(client: AsyncClient) -> None:
    response = await client.post(
        "/api/devices/register", json={"device_id": str(uuid.uuid4()), "name": ""}
    )
    assert response.status_code == 422


async def test_host_pc_is_always_listed(client: AsyncClient, app: FastAPI, sender: str) -> None:
    """You can always send to the machine running the server."""
    response = await client.get("/api/devices", headers=headers(sender))
    assert response.status_code == 200
    devices = response.json()["devices"]

    server = next(d for d in devices if d["id"] == app.state.server_device_id)
    assert server["kind"] == "server"
    assert server["online"] is True


async def test_registered_but_unconnected_devices_are_not_listed(
    client: AsyncClient, app: FastAPI, sender: str
) -> None:
    """Online means "holding a WebSocket", not "registered once"."""
    device_id = await register(client, "Ghost laptop")

    response = await client.get("/api/devices", headers=headers(sender))
    listed = {d["id"] for d in response.json()["devices"]}
    assert device_id not in listed
    assert app.state.server_device_id in listed
