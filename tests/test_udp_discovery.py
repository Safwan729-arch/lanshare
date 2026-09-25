"""UDP broadcast discovery between servers (Phase 4).

The parser gets the most attention here: it reads unsolicited datagrams from
the local network, so every malformed or hostile shape must be dropped quietly
rather than raise.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from lanshare.discovery.udp import (
    MAGIC,
    MAX_DATAGRAM_BYTES,
    PROTOCOL_VERSION,
    TYPE_ANNOUNCE,
    TYPE_GOODBYE,
    Peer,
    PeerRegistry,
    build_announcement,
    parse_announcement,
)

SENDER = "192.168.1.50"


def announcement(**overrides: Any) -> bytes:
    payload = {
        "magic": MAGIC,
        "v": PROTOCOL_VERSION,
        "type": TYPE_ANNOUNCE,
        "id": "aaaa-bbbb",
        "name": "Other PC",
        "port": 8080,
        "version": "0.1.0",
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


# -- round trip --------------------------------------------------------------


def test_announcement_round_trips() -> None:
    data = build_announcement(
        server_id="abc-123", name="Kitchen PC", http_port=8080, version="0.1.0"
    )
    parsed = parse_announcement(data, SENDER)
    assert parsed is not None

    message_type, peer = parsed
    assert message_type == TYPE_ANNOUNCE
    assert peer is not None
    assert peer.server_id == "abc-123"
    assert peer.name == "Kitchen PC"
    assert peer.port == 8080
    assert peer.url == f"http://{SENDER}:8080"


def test_goodbye_round_trips() -> None:
    data = build_announcement(
        server_id="abc-123", name="Kitchen PC", http_port=8080, version="0.1.0", goodbye=True
    )
    parsed = parse_announcement(data, SENDER)
    assert parsed is not None
    assert parsed[0] == TYPE_GOODBYE


def test_announcement_fits_in_one_small_datagram() -> None:
    data = build_announcement(
        server_id="a" * 64, name="n" * 64, http_port=65535, version="10.10.10"
    )
    assert len(data) <= MAX_DATAGRAM_BYTES


def test_announcement_does_not_carry_an_address() -> None:
    """Receivers must read the address off the packet, so we never send one."""
    data = build_announcement(server_id="abc", name="PC", http_port=8080, version="0.1.0")
    payload = json.loads(data)
    assert "address" not in payload
    assert "host" not in payload
    assert "url" not in payload


# -- the security property ---------------------------------------------------


def test_address_comes_from_the_packet_not_the_payload() -> None:
    """A peer claiming to live somewhere else must be ignored.

    Without this, anything on the LAN could broadcast a link pointing at a
    machine it does not own, and we would show it as a trusted LANShare server.
    """
    hostile = announcement(address="10.0.0.1", host="evil.example.com", url="http://evil")
    parsed = parse_announcement(hostile, SENDER)
    assert parsed is not None

    _, peer = parsed
    assert peer is not None
    assert peer.address == SENDER
    assert peer.url == f"http://{SENDER}:8080"


def test_public_sender_addresses_are_rejected() -> None:
    """LANShare is LAN-only; a packet from a routable address is not ours."""
    assert parse_announcement(announcement(), "8.8.8.8") is None


def test_private_ranges_are_accepted() -> None:
    for address in ("192.168.1.5", "10.1.2.3", "172.16.0.9", "127.0.0.1"):
        assert parse_announcement(announcement(), address) is not None, address


def test_nonsense_sender_address_is_rejected() -> None:
    assert parse_announcement(announcement(), "not-an-address") is None


# -- malformed input ---------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "data"),
    [
        ("not json", b"hello there"),
        ("invalid utf-8", b"\xff\xfe\x00bad"),
        ("empty", b""),
        ("json list", b"[1, 2, 3]"),
        ("json string", b'"just a string"'),
        ("json null", b"null"),
    ],
)
def test_garbage_is_dropped(label: str, data: bytes) -> None:
    assert parse_announcement(data, SENDER) is None, label


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("wrong magic", {"magic": "something-else"}),
        ("wrong protocol version", {"v": 99}),
        ("unknown type", {"type": "shutdown-everything"}),
        ("missing id", {"id": None}),
        ("empty id", {"id": "   "}),
        ("missing name", {"name": None}),
        ("empty name", {"name": ""}),
        ("name too long", {"name": "n" * 500}),
        ("id too long", {"id": "a" * 500}),
        ("port as string", {"port": "8080"}),
        ("port as bool", {"port": True}),
        ("port zero", {"port": 0}),
        ("port too high", {"port": 70000}),
        ("port negative", {"port": -1}),
        ("control chars in name", {"name": "evil\x00name"}),
        ("newline in name", {"name": "line\nbreak"}),
    ],
)
def test_invalid_fields_are_dropped(label: str, payload: dict[str, Any]) -> None:
    assert parse_announcement(announcement(**payload), SENDER) is None, label


def test_oversized_datagram_is_dropped_before_parsing() -> None:
    """Cap what a hostile sender can make us decode."""
    huge = announcement(name="x") + b" " * MAX_DATAGRAM_BYTES
    assert len(huge) > MAX_DATAGRAM_BYTES
    assert parse_announcement(huge, SENDER) is None


def test_missing_version_falls_back_rather_than_failing() -> None:
    parsed = parse_announcement(announcement(version=None), SENDER)
    assert parsed is not None
    assert parsed[1] is not None
    assert parsed[1].version == "unknown"


# -- the registry ------------------------------------------------------------


def peer(server_id: str = "one", *, name: str = "PC", last_seen: float = 100.0) -> Peer:
    return Peer(
        server_id=server_id,
        name=name,
        address="192.168.1.5",
        port=8080,
        version="0.1.0",
        last_seen=last_seen,
    )


def test_remember_reports_new_peers_once() -> None:
    registry = PeerRegistry(ttl_seconds=30)
    assert registry.remember(peer("one")) is True
    assert registry.remember(peer("one")) is False


def test_remember_refreshes_an_existing_peer() -> None:
    registry = PeerRegistry(ttl_seconds=30)
    registry.remember(peer("one", last_seen=100.0))
    registry.remember(peer("one", name="Renamed", last_seen=200.0))

    peers = registry.current(now=200.0)
    assert len(peers) == 1
    assert peers[0].name == "Renamed"


def test_forget_removes_a_peer() -> None:
    registry = PeerRegistry(ttl_seconds=30)
    registry.remember(peer("one"))
    assert registry.forget("one") is True
    assert registry.forget("one") is False
    assert registry.current(now=100.0) == []


def test_quiet_peers_expire() -> None:
    registry = PeerRegistry(ttl_seconds=30)
    registry.remember(peer("one", last_seen=100.0))

    assert len(registry.current(now=120.0)) == 1, "still inside the TTL"
    assert registry.current(now=200.0) == [], "past the TTL"


def test_prune_returns_what_it_removed() -> None:
    registry = PeerRegistry(ttl_seconds=30)
    registry.remember(peer("one", name="Gone", last_seen=100.0))
    registry.remember(peer("two", name="Here", last_seen=190.0))

    removed = registry.prune(now=200.0)
    assert [p.server_id for p in removed] == ["one"]
    assert [p.server_id for p in registry.current(now=200.0)] == ["two"]


def test_peers_are_sorted_by_name() -> None:
    registry = PeerRegistry(ttl_seconds=30)
    for index, name in enumerate(["zeta", "Alpha", "middle"]):
        registry.remember(peer(str(index), name=name, last_seen=100.0))

    assert [p.name for p in registry.current(now=100.0)] == ["Alpha", "middle", "zeta"]


# -- the endpoint ------------------------------------------------------------


async def test_peers_endpoint_reports_discovery_disabled(client: Any, sender: str) -> None:
    """Tests run with broadcasting off. An empty list because discovery is off
    must be distinguishable from an empty list because nobody is out there."""
    from conftest import headers

    response = await client.get("/api/peers", headers=headers(sender))
    assert response.status_code == 200

    body = response.json()
    assert body["peers"] == []
    assert body["discovery_enabled"] is False


# -- start / stop against a fake transport -----------------------------------


class FakeTransport:
    """Records what would have gone out on the wire."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        self.closed = True


def attach(discovery: Any) -> FakeTransport:
    """Give a discovery object a transport without touching the network."""
    transport = FakeTransport()
    discovery._transport = transport
    return transport


def make_discovery(**overrides: Any) -> Any:
    from lanshare.discovery.udp import UdpDiscovery

    kwargs: dict[str, Any] = {
        "server_id": "me-123",
        "server_name": "This PC",
        "http_port": 8080,
        "discovery_port": 8079,
        "version": "0.1.0",
    }
    kwargs.update(overrides)
    return UdpDiscovery(**kwargs)


async def test_start_failure_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked or busy UDP port must not stop the server."""
    discovery = make_discovery()
    monkeypatch.setattr(
        type(discovery),
        "_make_socket",
        lambda self: (_ for _ in ()).throw(OSError("port busy")),
    )
    assert await discovery.start() is False
    assert discovery.peers() == []


async def test_stop_broadcasts_a_goodbye() -> None:
    """Peers should drop us immediately, not after the TTL."""
    discovery = make_discovery()
    transport = attach(discovery)

    await discovery.stop()

    assert len(transport.sent) == 1, "exactly one goodbye"
    payload = json.loads(transport.sent[0][0])
    assert payload["type"] == TYPE_GOODBYE
    assert payload["id"] == "me-123"
    assert transport.closed is True


async def test_stop_without_start_is_harmless() -> None:
    await make_discovery().stop()


def test_broadcast_goes_to_the_broadcast_address() -> None:
    from lanshare.discovery.udp import BROADCAST_ADDRESS

    discovery = make_discovery()
    transport = attach(discovery)
    discovery._broadcast()

    _, addr = transport.sent[0]
    assert addr == (BROADCAST_ADDRESS, 8079)


def test_our_own_announcement_is_ignored() -> None:
    """Broadcasts come back to the sender; we must not list ourselves."""
    discovery = make_discovery(server_id="me-123")
    own = build_announcement(server_id="me-123", name="This PC", http_port=8080, version="0.1.0")
    discovery._on_datagram(own, "192.168.1.5")
    assert discovery.peers() == []


def test_another_server_is_recorded() -> None:
    discovery = make_discovery(server_id="me-123")
    other = build_announcement(
        server_id="them-456", name="Other PC", http_port=8081, version="0.1.0"
    )
    discovery._on_datagram(other, "192.168.1.9")

    peers = discovery.peers()
    assert len(peers) == 1
    assert peers[0].name == "Other PC"
    assert peers[0].url == "http://192.168.1.9:8081"


def test_goodbye_removes_a_known_peer() -> None:
    discovery = make_discovery(server_id="me-123")
    hello = build_announcement(
        server_id="them-456", name="Other PC", http_port=8081, version="0.1.0"
    )
    bye = build_announcement(
        server_id="them-456", name="Other PC", http_port=8081, version="0.1.0", goodbye=True
    )

    discovery._on_datagram(hello, "192.168.1.9")
    assert len(discovery.peers()) == 1

    discovery._on_datagram(bye, "192.168.1.9")
    assert discovery.peers() == []


def test_a_malformed_datagram_never_raises() -> None:
    """The listener must survive whatever arrives on the port."""
    discovery = make_discovery()
    for junk in (b"", b"not json", b"\xff\xfe", b"{}", b'{"magic":"wrong"}'):
        discovery._on_datagram(junk, "192.168.1.9")
    assert discovery.peers() == []
