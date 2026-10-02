"""WebSocket registry and broadcast helpers.

A device may hold several sockets at once (two tabs, a reconnect that raced the
old socket's close), so connections are tracked as a set per device id.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

from .. import timing

logger = logging.getLogger(__name__)


def event(event_type: str, **data: Any) -> dict[str, Any]:
    """Build the wire envelope every message uses."""
    return {"type": event_type, "data": data}


class ConnectionManager:
    """Who is connected, and how to reach them."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    # -- registry ------------------------------------------------------------

    def add(self, device_id: str, websocket: WebSocket) -> bool:
        """Register a socket. Returns True if this device just came online."""
        sockets = self._connections.setdefault(device_id, set())
        was_offline = not sockets
        sockets.add(websocket)
        return was_offline

    def remove(self, device_id: str, websocket: WebSocket) -> bool:
        """Drop a socket. Returns True if this device is now fully offline."""
        sockets = self._connections.get(device_id)
        if not sockets:
            return False
        sockets.discard(websocket)
        if sockets:
            return False
        del self._connections[device_id]
        return True

    def is_online(self, device_id: str) -> bool:
        return bool(self._connections.get(device_id))

    def online_ids(self) -> list[str]:
        return sorted(self._connections)

    # -- sending -------------------------------------------------------------

    async def send(self, device_id: str, message: dict[str, Any]) -> None:
        """Send to every socket a device holds. Dead sockets are dropped."""
        sockets = list(self._connections.get(device_id, ()))
        timing.mark(
            "ws.send.start", device=device_id[:8], sockets=len(sockets), kind=message.get("type")
        )
        for number, websocket in enumerate(sockets):
            try:
                await websocket.send_json(message)
                timing.mark("ws.send.ok", device=device_id[:8], socket=number)
            # Broad on purpose. Every transport reports a dead peer in its own
            # way - `websockets` raises ConnectionClosed, Starlette's test
            # transport raises anyio's ClosedResourceError, and neither is a
            # RuntimeError or an OSError. Naming them one at a time means the
            # next one escapes and takes down the endpoint that was merely
            # telling everybody something. CancelledError is a BaseException,
            # so shutdown still interrupts this.
            except Exception as exc:
                timing.mark("ws.send.fail", device=device_id[:8], socket=number, error=repr(exc))
                logger.debug("Dropping dead socket for %s: %s", device_id, exc)
                self.remove(device_id, websocket)

    async def send_many(self, device_ids: list[str], message: dict[str, Any]) -> None:
        for device_id in dict.fromkeys(device_ids):  # de-duplicate, keep order
            await self.send(device_id, message)

    async def broadcast(self, message: dict[str, Any], *, exclude: str | None = None) -> None:
        for device_id in list(self._connections):
            if device_id != exclude:
                await self.send(device_id, message)

    async def disconnect_device(self, device_id: str, *, code: int = 4403) -> None:
        """Close every socket a device holds.

        Revoking trust has to hang up as well as flip the flag — an already
        open socket would otherwise keep receiving events, since the trust
        check happens at connect time.
        """
        for websocket in list(self._connections.get(device_id, ())):
            try:
                await websocket.close(code=code, reason="Access revoked")
            except (RuntimeError, OSError) as exc:
                logger.debug("Could not close socket for %s: %s", device_id, exc)
            self.remove(device_id, websocket)
