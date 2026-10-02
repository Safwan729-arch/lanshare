"""Shared FastAPI dependencies, including authentication.

Everything long-lived (settings, database, storage, services) is built once at
startup and hung off ``app.state``; these helpers hand it to the routers.

Authentication is two questions, asked in order:

1. **Who are you?** The ``X-Device-Id`` header names a device; the
   ``Authorization: Bearer`` token proves it is really you.
2. **May you?** Only a ``trusted`` device may transfer files. A ``pending``
   device is waiting for approval; a ``blocked`` one has been refused.
"""

from __future__ import annotations

from typing import Annotated, Any

import aiosqlite
from fastapi import Depends, Header, Request

from ..config import Settings
from ..db.repositories import DeviceRepository
from ..services.auth import BLOCKED, PENDING, TRUSTED, is_host_device, verify_token
from ..services.errors import Forbidden, Unauthorized
from ..services.transfer_service import TransferService
from ..ws.manager import ConnectionManager


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_connection(request: Request) -> aiosqlite.Connection:
    connection: aiosqlite.Connection = request.app.state.database.connection
    return connection


def get_connections(request: Request) -> ConnectionManager:
    connections: ConnectionManager = request.app.state.connections
    return connections


def get_transfer_service(request: Request) -> TransferService:
    service: TransferService = request.app.state.transfer_service
    return service


def client_address(request: Request) -> str | None:
    """Where the request came from, as the server sees it.

    Taken from the connection, never from a header: `X-Forwarded-For` and
    friends are trivially spoofed, and LANShare sits behind no proxy.
    """
    return request.client.host if request.client else None


def bearer_token(authorization: str | None) -> str | None:
    """Pull the credential out of an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def authenticate_device(
    request: Request,
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id", max_length=64)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Identify the caller. Raises unless the token matches the device.

    The header is optional in the signature on purpose: declaring it required
    makes FastAPI answer a missing credential with 422 Unprocessable Entity,
    which says "your request was malformed" when the truth is "you are not
    authenticated". Absent and wrong both end up at the same 401 below.
    """
    conn: aiosqlite.Connection = request.app.state.database.connection

    device = await DeviceRepository.get(conn, x_device_id) if x_device_id else None
    token = bearer_token(authorization)

    # One message for "no such device" and "wrong token" alike, so this cannot
    # be used to find out which device ids exist.
    if device is None or not verify_token(token, device.get("token_hash")):
        raise Unauthorized("Unknown device or invalid token")

    return device


async def require_trusted_device(
    device: Annotated[dict[str, Any], Depends(authenticate_device)],
) -> str:
    """The caller must be approved. Returns the device id."""
    state = device.get("trust_state")

    if state == BLOCKED:
        raise Forbidden("This device has been blocked")
    if state == PENDING:
        raise Forbidden("This device is waiting for approval on the host")
    if state != TRUSTED:
        raise Forbidden("This device is not trusted")

    return str(device["id"])


async def require_host_device(
    request: Request,
    device: Annotated[dict[str, Any], Depends(authenticate_device)],
) -> str:
    """The caller must be the machine running the server. Returns its id.

    Approving a device is the one decision that grants access to everything
    else, so it belongs to the person at the keyboard of the host - which is
    what the README has always told them to do. A trusted phone approving a
    stranger would let one approval beget another with nobody at the host ever
    seeing it.

    Set ``LANSHARE_APPROVAL_FROM_HOST_ONLY=false`` to go back to any trusted
    device deciding.
    """
    device_id = await require_trusted_device(device)
    settings: Settings = request.app.state.settings
    if settings.approval_from_host_only and not is_host_device(device):
        raise Forbidden("Approve devices on the host, at http://localhost:" + str(settings.port))
    return device_id


SettingsDep = Annotated[Settings, Depends(get_settings)]
ConnectionDep = Annotated[aiosqlite.Connection, Depends(get_connection)]
ConnectionsDep = Annotated[ConnectionManager, Depends(get_connections)]
ServiceDep = Annotated[TransferService, Depends(get_transfer_service)]
AddressDep = Annotated[str | None, Depends(client_address)]

#: An authenticated device of any trust state - use only where a pending
#: device legitimately needs an answer, such as polling its own status.
AuthedDeviceDep = Annotated[dict[str, Any], Depends(authenticate_device)]

#: An approved device. This is what everything that moves files requires.
DeviceIdDep = Annotated[str, Depends(require_trusted_device)]

#: The host itself. Pairing decisions only.
HostDeviceDep = Annotated[str, Depends(require_host_device)]
