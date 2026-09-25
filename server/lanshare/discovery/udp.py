"""UDP broadcast discovery between LANShare servers (Phase 4).

**Server to server only.** Browsers cannot open raw sockets, so a phone can
never take part in this — phones find the server by QR, by `lanshare.local`, or
by typing the address. This is how two PCs each running LANShare notice each
other on the same LAN.

Each instance periodically broadcasts a small JSON datagram and listens for
other instances doing the same. Peers that stop announcing expire.

Every datagram is untrusted input from the network. The parser is strict: size
capped, magic and protocol version checked, every field validated, and anything
odd dropped silently rather than raised. Most importantly, a peer's **address
is taken from the packet's source, never from its payload** — otherwise any
machine on the LAN could advertise a link pointing anywhere it liked.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MAGIC = "lanshare"
PROTOCOL_VERSION = 1

TYPE_ANNOUNCE = "announce"
TYPE_GOODBYE = "goodbye"

# Nothing legitimate comes close to this; it caps what a hostile sender can
# make us allocate or parse.
MAX_DATAGRAM_BYTES = 1024
MAX_NAME_LENGTH = 64
MAX_ID_LENGTH = 64

#: How many peers to hold at once.
#:
#: Announcements are unauthenticated by nature - any host on the LAN can send
#: one - and the server id is chosen by the sender. Without a ceiling, a single
#: neighbour can mint a new id per packet and grow this table until the TTL
#: catches up, which it will not if the packets keep coming. Real networks do
#: not have dozens of LANShare servers on them.
MAX_PEERS = 32

BROADCAST_ADDRESS = "255.255.255.255"


@dataclass(frozen=True)
class Peer:
    """Another LANShare server seen on the network."""

    server_id: str
    name: str
    address: str
    port: int
    version: str
    last_seen: float

    @property
    def url(self) -> str:
        return f"http://{self.address}:{self.port}"


def build_announcement(
    *, server_id: str, name: str, http_port: int, version: str, goodbye: bool = False
) -> bytes:
    """Serialize what we broadcast about ourselves.

    The address is deliberately absent: receivers read it off the packet.
    """
    payload = {
        "magic": MAGIC,
        "v": PROTOCOL_VERSION,
        "type": TYPE_GOODBYE if goodbye else TYPE_ANNOUNCE,
        "id": server_id,
        "name": name[:MAX_NAME_LENGTH],
        "port": http_port,
        "version": version,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _valid_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        return None
    # Control characters have no business in a name we are going to display.
    if any(ord(ch) < 32 for ch in cleaned):
        return None
    return cleaned


def _valid_port(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 1 <= value <= 65535 else None


def parse_announcement(data: bytes, sender_address: str) -> tuple[str, Peer | None] | None:
    """Turn a datagram into ``(message type, peer)``, or None if it is not ours.

    A goodbye carries a peer too, so the caller knows which one to drop.
    Returns None — never raises — for anything malformed. This runs on
    unsolicited input from the local network.
    """
    if len(data) > MAX_DATAGRAM_BYTES:
        return None

    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("magic") != MAGIC or payload.get("v") != PROTOCOL_VERSION:
        return None

    message_type = payload.get("type")
    if message_type not in (TYPE_ANNOUNCE, TYPE_GOODBYE):
        return None

    server_id = _valid_text(payload.get("id"), MAX_ID_LENGTH)
    name = _valid_text(payload.get("name"), MAX_NAME_LENGTH)
    port = _valid_port(payload.get("port"))
    version = _valid_text(payload.get("version"), 32) or "unknown"
    if server_id is None or name is None or port is None:
        return None

    # The sender does not get to tell us where it lives.
    try:
        ip = ipaddress.ip_address(sender_address)
    except ValueError:
        return None
    # LAN-only by definition. Loopback counts as private, which is what lets
    # two instances on one machine find each other.
    if not ip.is_private:
        return None

    peer = Peer(
        server_id=server_id,
        name=name,
        address=sender_address,
        port=port,
        version=version,
        last_seen=time.monotonic(),
    )
    return message_type, peer


class PeerRegistry:
    """Who we have heard from lately."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._peers: dict[str, Peer] = {}

    def remember(self, peer: Peer) -> bool:
        """Record a peer. Returns True if this one is new.

        A peer already known is always refreshed, so a flood of invented ids can
        never push a genuine neighbour out - it can only fail to add itself.
        """
        if peer.server_id in self._peers:
            self._peers[peer.server_id] = peer
            return False

        if len(self._peers) >= MAX_PEERS:
            # Only now is it worth spending a pass to reclaim expired entries;
            # doing it on every insert would make `remember` depend on the
            # clock even when the table is nearly empty.
            self.prune()

        if len(self._peers) >= MAX_PEERS:
            logger.warning(
                "Ignoring peer %s: already tracking %d, the maximum", peer.server_id, MAX_PEERS
            )
            return False

        self._peers[peer.server_id] = peer
        return True

    def forget(self, server_id: str) -> bool:
        return self._peers.pop(server_id, None) is not None

    def prune(self, *, now: float | None = None) -> list[Peer]:
        """Drop peers that have gone quiet. Returns the ones removed."""
        moment = time.monotonic() if now is None else now
        stale = [p for p in self._peers.values() if moment - p.last_seen > self._ttl]
        for peer in stale:
            del self._peers[peer.server_id]
        return stale

    def current(self, *, now: float | None = None) -> list[Peer]:
        self.prune(now=now)
        return sorted(self._peers.values(), key=lambda p: p.name.lower())


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Hands datagrams to the owner; never lets one kill the transport."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def datagram_received(self, data: bytes, addr: tuple[str | Any, ...]) -> None:
        try:
            self._handler(data, str(addr[0]))
        except Exception:
            logger.debug("Discarded an unparseable datagram from %s", addr, exc_info=True)

    def error_received(self, exc: Exception) -> None:
        # ICMP port-unreachable from a host with nothing listening. Normal.
        logger.debug("UDP discovery error: %s", exc)


class UdpDiscovery:
    """Announces this server and tracks the others.

    Best effort, exactly like mDNS: if the socket cannot be opened the server
    still runs, it simply will not see or be seen by other instances.
    """

    def __init__(
        self,
        *,
        server_id: str,
        server_name: str,
        http_port: int,
        discovery_port: int,
        version: str,
        announce_interval: float = 10.0,
        peer_ttl: float = 35.0,
    ) -> None:
        self._server_id = server_id
        self._server_name = server_name
        self._http_port = http_port
        self._discovery_port = discovery_port
        self._version = version
        self._announce_interval = announce_interval
        self._registry = PeerRegistry(peer_ttl)

        self._transport: asyncio.DatagramTransport | None = None
        self._announce_task: asyncio.Task[None] | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> bool:
        """Open the socket and start announcing. False if it could not."""
        try:
            sock = self._make_socket()
        except OSError as exc:
            logger.warning("Could not open the UDP discovery socket: %s", exc)
            return False

        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _DiscoveryProtocol(self._on_datagram), sock=sock
            )
        except OSError as exc:
            logger.warning("Could not start UDP discovery: %s", exc)
            sock.close()
            return False

        self._transport = transport  # type: ignore[assignment]
        self._announce_task = asyncio.create_task(self._announce_loop())
        logger.info("UDP discovery listening on port %d", self._discovery_port)
        return True

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Lets a second instance on this machine share the port instead of
        # failing to bind, which matters when testing two servers on one PC.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self._discovery_port))
        sock.setblocking(False)
        return sock

    async def stop(self) -> None:
        """Say goodbye so peers drop us now rather than after the TTL."""
        if self._announce_task is not None:
            self._announce_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._announce_task
            self._announce_task = None

        if self._transport is not None:
            self._broadcast(goodbye=True)
            self._transport.close()
            self._transport = None

    # -- announcing ----------------------------------------------------------

    async def _announce_loop(self) -> None:
        while True:
            self._broadcast()
            self._registry.prune()
            await asyncio.sleep(self._announce_interval)

    def _broadcast(self, *, goodbye: bool = False) -> None:
        if self._transport is None:
            return
        payload = build_announcement(
            server_id=self._server_id,
            name=self._server_name,
            http_port=self._http_port,
            version=self._version,
            goodbye=goodbye,
        )
        try:
            self._transport.sendto(payload, (BROADCAST_ADDRESS, self._discovery_port))
        except OSError as exc:
            # No route, interface just went down: try again next tick.
            logger.debug("Could not broadcast: %s", exc)

    # -- receiving -----------------------------------------------------------

    def _on_datagram(self, data: bytes, sender_address: str) -> None:
        parsed = parse_announcement(data, sender_address)
        if parsed is None:
            return

        message_type, peer = parsed
        if peer is None or peer.server_id == self._server_id:
            return  # our own broadcast coming back to us

        if message_type == TYPE_GOODBYE:
            if self._registry.forget(peer.server_id):
                logger.info("Peer %s went away", peer.name)
            return

        if self._registry.remember(peer):
            logger.info("Found peer %s at %s", peer.name, peer.url)

    def peers(self) -> list[Peer]:
        return self._registry.current()
