"""Device registration, pairing approval, and the list of devices you can send to."""

from __future__ import annotations

import logging
from typing import Annotated, Any

import aiosqlite
from fastapi import APIRouter, Path, Request

from ..db.repositories import DeviceRepository
from ..models import (
    DeviceListResponse,
    DeviceRegisterRequest,
    DeviceRegisterResponse,
    DeviceResponse,
    TrustDecisionRequest,
)
from ..services.auth import (
    BLOCKED,
    PENDING,
    TRUSTED,
    generate_token,
    hash_token,
    initial_trust_state,
    verify_token,
)
from ..services.errors import Forbidden, NotFound, TooManyRequests
from ..ws.manager import ConnectionManager, event
from .deps import (
    AddressDep,
    AuthedDeviceDep,
    ConnectionDep,
    ConnectionsDep,
    DeviceIdDep,
    SettingsDep,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["devices"])

DeviceIdPath = Annotated[str, Path(min_length=8, max_length=64)]


@router.post("/register", response_model=DeviceRegisterResponse)
async def register_device(
    payload: DeviceRegisterRequest,
    request: Request,
    conn: ConnectionDep,
    connections: ConnectionsDep,
    address: AddressDep,
    settings: SettingsDep,
) -> DeviceRegisterResponse:
    """Register a device, or refresh one that already exists.

    A new device is issued a token immediately and starts as ``pending``. The
    token says who it is; the trust state says what it may do.

    Refreshing an existing device requires its token. Without that check,
    anyone on the LAN who learned a device id could re-register it and be
    handed a fresh credential for an already-trusted device.
    """
    existing = await DeviceRepository.get(conn, payload.device_id)
    name = payload.name.strip()

    if existing is not None:
        presented = _token_from_request(request)
        if not verify_token(presented, existing.get("token_hash")):
            raise Forbidden("That device id is taken; clear local data and register again")

        refreshed = await DeviceRepository.refresh(
            conn, device_id=payload.device_id, name=name, user_agent=payload.user_agent
        )
        assert refreshed is not None
        return DeviceRegisterResponse(
            device=DeviceResponse.from_row(refreshed, online=connections.is_online(refreshed["id"]))
        )

    token = generate_token()
    trust_state = initial_trust_state(address)

    if trust_state == PENDING:
        # Registration cannot require a credential - a device needs an identity
        # before anyone can approve it - so the only protection against a LAN
        # neighbour inserting rows forever is a ceiling on how many may wait at
        # once. Loopback is exempt: it registers as trusted and never reaches
        # here, so a flood can never stop the host approving anything.
        waiting = await DeviceRepository.count_by_trust(conn, PENDING)
        if waiting >= settings.max_pending_devices:
            logger.warning(
                "Refusing registration from %s: %d devices already waiting", address, waiting
            )
            raise TooManyRequests(
                "Too many devices are already waiting for approval; "
                "approve or deny some on the host first"
            )

    device = await DeviceRepository.create(
        conn,
        device_id=payload.device_id,
        name=name,
        user_agent=payload.user_agent,
        trust_state=trust_state,
        token_hash=hash_token(token),
        first_address=address,
    )
    logger.info("Registered device %s (%s) as %s", name, address, trust_state)

    if trust_state == PENDING:
        await _notify_trusted(
            request,
            event(
                "device.pending",
                device=DeviceResponse.from_row(device, online=False).model_dump(),
            ),
        )

    return DeviceRegisterResponse(device=DeviceResponse.from_row(device, online=False), token=token)


def _token_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


@router.get("/me", response_model=DeviceResponse)
async def whoami(device: AuthedDeviceDep, connections: ConnectionsDep) -> DeviceResponse:
    """This device's own record, whatever its trust state.

    A pending device polls this while it waits to be approved — it is the one
    thing an unapproved device is allowed to ask for.
    """
    return DeviceResponse.from_row(device, online=connections.is_online(device["id"]))


@router.get("/pending", response_model=DeviceListResponse)
async def list_pending(_: DeviceIdDep, conn: ConnectionDep) -> DeviceListResponse:
    """Devices waiting for approval. Only a trusted device may look."""
    rows = await DeviceRepository.list_by_trust(conn, PENDING, only_with_token=True)
    return DeviceListResponse(devices=[DeviceResponse.from_row(r, online=False) for r in rows])


@router.post("/{device_id}/trust", response_model=DeviceResponse)
async def decide_trust(
    device_id: DeviceIdPath,
    payload: TrustDecisionRequest,
    request: Request,
    approver_id: DeviceIdDep,
    conn: ConnectionDep,
    connections: ConnectionsDep,
) -> DeviceResponse:
    """Approve, deny or block a device. Only a trusted device may decide."""
    target = await DeviceRepository.get(conn, device_id)
    if target is None:
        raise NotFound(f"No device {device_id}")
    if target["kind"] == "server":
        raise Forbidden("The host device cannot be changed")

    if payload.decision == "approve":
        updated = await DeviceRepository.set_trust(conn, device_id, TRUSTED)
        assert updated is not None
        logger.info("Device %s approved by %s", target["name"], approver_id)
        await connections.send(device_id, event("device.approved", device_id=device_id))
    elif payload.decision == "block":
        updated = await DeviceRepository.set_trust(conn, device_id, BLOCKED)
        assert updated is not None
        logger.info("Device %s blocked by %s", target["name"], approver_id)
        await connections.send(device_id, event("device.denied", device_id=device_id))
        await connections.disconnect_device(device_id)
    else:  # deny
        await connections.send(device_id, event("device.denied", device_id=device_id))
        await connections.disconnect_device(device_id)
        await DeviceRepository.delete(conn, device_id)
        logger.info("Device %s denied by %s", target["name"], approver_id)
        # Deleted, so report the state it was in when the decision was made.
        updated = {**target, "trust_state": "blocked"}

    await _notify_trusted(request, await _pending_event(conn))
    return DeviceResponse.from_row(updated, online=connections.is_online(device_id))


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    request: Request,
    _: DeviceIdDep,
    conn: ConnectionDep,
    connections: ConnectionsDep,
) -> DeviceListResponse:
    """Devices you can send to right now.

    Online means "holds a WebSocket connection", and only trusted devices can
    hold one. The host PC is always listed: it is the machine running the
    server, so it is reachable by definition.
    """
    server_id: str = request.app.state.server_device_id
    online = set(connections.online_ids()) | {server_id}

    rows = await DeviceRepository.list_by_ids(conn, sorted(online))
    return DeviceListResponse(
        devices=[
            DeviceResponse.from_row(row, online=True)
            for row in rows
            if row.get("trust_state") == TRUSTED
        ]
    )


async def device_list_event(
    conn: aiosqlite.Connection, connections: ConnectionManager, server_id: str
) -> dict[str, Any]:
    """Build the ``device.list`` event body. Shared with the WebSocket endpoint."""
    online = set(connections.online_ids()) | {server_id}
    rows = await DeviceRepository.list_by_ids(conn, sorted(online))
    return event(
        "device.list",
        devices=[
            DeviceResponse.from_row(row, online=True).model_dump()
            for row in rows
            if row.get("trust_state") == TRUSTED
        ],
    )


async def _pending_event(conn: aiosqlite.Connection) -> dict[str, Any]:
    rows = await DeviceRepository.list_by_trust(conn, PENDING, only_with_token=True)
    return event(
        "device.pending.list",
        devices=[DeviceResponse.from_row(r, online=False).model_dump() for r in rows],
    )


async def _notify_trusted(request: Request, message: dict[str, Any]) -> None:
    """Tell every connected device about a pairing change.

    Only trusted devices hold a socket, so this reaches exactly the devices
    entitled to approve.
    """
    connections: ConnectionManager = request.app.state.connections
    await connections.broadcast(message)
