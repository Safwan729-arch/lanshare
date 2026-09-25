"""WebSocket contract. Uses the sync TestClient - httpx cannot speak WS."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from lanshare.config import Settings
from lanshare.main import create_app
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

SUBPROTOCOL = "lanshare.v1"


@pytest.fixture
def test_client(settings: Settings) -> Iterator[TestClient]:
    # TestClient reports "testclient" as the peer address by default, which is
    # not an IP at all - a device registering through it would land as pending
    # and could not open a socket. Loopback mirrors the real trusted case: the
    # host's own browser at http://localhost:8080.
    with TestClient(create_app(settings), client=("127.0.0.1", 123)) as client:
        yield client


def register(client: TestClient, name: str) -> tuple[str, str]:
    """Register a device and return (id, token)."""
    device_id = str(uuid.uuid4())
    response = client.post("/api/devices/register", json={"device_id": device_id, "name": name})
    assert response.status_code == 200, response.text
    return device_id, response.json()["token"]


def protocols(token: str) -> list[str]:
    """The token travels as a subprotocol, never in the query string."""
    return [SUBPROTOCOL, f"token.{token}"]


def connect(client: TestClient, device_id: str, token: str) -> Any:
    return client.websocket_connect(f"/ws?device_id={device_id}", subprotocols=protocols(token))


def drain_until(websocket: Any, event_type: str, limit: int = 6) -> dict[str, Any]:
    """Read frames until the wanted type shows up."""
    for _ in range(limit):
        message = websocket.receive_json()
        if message["type"] == event_type:
            return message
    raise AssertionError(f"Never saw a {event_type} frame")


# -- authentication ----------------------------------------------------------


def test_unknown_device_is_closed(test_client: TestClient) -> None:
    """A device must register over REST before it can open a socket."""
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        test_client.websocket_connect(
            f"/ws?device_id={uuid.uuid4()}", subprotocols=protocols("nonsense")
        ) as websocket,
    ):
        websocket.receive_json()
    assert excinfo.value.code == 4404


def test_socket_without_a_token_is_closed(test_client: TestClient) -> None:
    """Knowing a device id is not enough; the credential has to be presented."""
    device_id, _ = register(test_client, "iPhone")
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        test_client.websocket_connect(
            f"/ws?device_id={device_id}", subprotocols=[SUBPROTOCOL]
        ) as websocket,
    ):
        websocket.receive_json()
    assert excinfo.value.code == 4404


def test_socket_with_the_wrong_token_is_closed(test_client: TestClient) -> None:
    device_id, _ = register(test_client, "iPhone")
    other_token = register(test_client, "Laptop")[1]

    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        connect(test_client, device_id, other_token) as websocket,
    ):
        websocket.receive_json()
    assert excinfo.value.code == 4404


def test_accepted_socket_echoes_the_subprotocol(test_client: TestClient) -> None:
    """The browser requires the server to select one of its subprotocols."""
    device_id, token = register(test_client, "iPhone")
    with connect(test_client, device_id, token) as websocket:
        drain_until(websocket, "device.list")
        assert websocket.accepted_subprotocol == SUBPROTOCOL


# -- behaviour ---------------------------------------------------------------


def test_connecting_receives_the_device_list(test_client: TestClient) -> None:
    device_id, token = register(test_client, "iPhone")
    with connect(test_client, device_id, token) as websocket:
        message = drain_until(websocket, "device.list")
        ids = {d["id"] for d in message["data"]["devices"]}
        assert device_id in ids


def test_ping_gets_a_pong(test_client: TestClient) -> None:
    device_id, token = register(test_client, "iPhone")
    with connect(test_client, device_id, token) as websocket:
        drain_until(websocket, "device.list")
        websocket.send_json({"type": "ping", "data": {}})
        assert drain_until(websocket, "pong")["type"] == "pong"


def test_rename_updates_the_device_list(test_client: TestClient) -> None:
    device_id, token = register(test_client, "iPhone")
    with connect(test_client, device_id, token) as websocket:
        drain_until(websocket, "device.list")
        websocket.send_json({"type": "device.rename", "data": {"name": "Sam's iPhone"}})

        message = drain_until(websocket, "device.list")
        names = {d["id"]: d["name"] for d in message["data"]["devices"]}
        assert names[device_id] == "Sam's iPhone"


def test_second_device_triggers_a_join_event(test_client: TestClient) -> None:
    first, first_token = register(test_client, "iPhone")
    second, second_token = register(test_client, "Laptop")

    with connect(test_client, first, first_token) as one:
        drain_until(one, "device.list")
        with connect(test_client, second, second_token) as two:
            drain_until(two, "device.list")
            joined = drain_until(one, "device.joined")
            assert joined["data"]["device"]["id"] == second


def test_unknown_message_type_is_ignored(test_client: TestClient) -> None:
    """An unrecognised frame must not kill the connection."""
    device_id, token = register(test_client, "iPhone")
    with connect(test_client, device_id, token) as websocket:
        drain_until(websocket, "device.list")
        websocket.send_json({"type": "nonsense", "data": {}})
        websocket.send_json({"type": "ping", "data": {}})
        assert drain_until(websocket, "pong")["type"] == "pong"
