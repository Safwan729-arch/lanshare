"""App factory, lifespan wiring, the WebSocket endpoint and the static mount."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Scope

from . import __version__, timing
from .api import devices as devices_api
from .api import diag as diag_api
from .api import files as files_api
from .api import health as health_api
from .api import peers as peers_api
from .api import transfers as transfers_api
from .config import Settings, get_settings
from .db.database import Database
from .db.repositories import DeviceRepository, SettingsRepository
from .discovery.mdns import MdnsAdvertiser
from .discovery.qr import qr_svg, qr_terminal
from .discovery.udp import UdpDiscovery
from .middleware import LocalHostsOnly
from .models import DeviceResponse
from .services.auth import TRUSTED, verify_token
from .services.errors import LanShareError
from .services.network import build_lan_url, get_lan_ip
from .services.storage import Storage
from .services.transfer_service import TransferService
from .ws.manager import ConnectionManager, event

logger = logging.getLogger(__name__)

SERVER_DEVICE_KEY = "server_device_id"

#: Advertised on the socket so the browser can carry a token in
#: Sec-WebSocket-Protocol without it landing in uvicorn's access log.
WS_SUBPROTOCOL = "lanshare.v1"
TOKEN_SUBPROTOCOL_PREFIX = "token."


class RevalidatingStaticFiles(StaticFiles):
    """Serve the frontend with `Cache-Control: no-cache`.

    Starlette sends an ETag and Last-Modified but no Cache-Control at all, which
    leaves the browser free to invent a freshness lifetime of its own. Safari
    does exactly that, so a phone can keep running JavaScript from before the
    server was updated - reloading the page changes nothing and there is no way
    for the user to tell. That cost a full debugging round here.

    `no-cache` does not mean "do not store": the ETag still makes a revalidation
    a cheap 304. It means "ask before reusing", which is what a self-hosted app
    that gets edited between sessions actually needs.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


async def _ensure_server_device(app: FastAPI) -> str:
    """The host PC is a device too, with an id that survives restarts."""
    conn = app.state.database.connection
    settings: Settings = app.state.settings

    device_id = await SettingsRepository.get(conn, SERVER_DEVICE_KEY)
    if device_id is None:
        device_id = str(uuid.uuid4())
        await SettingsRepository.set(conn, SERVER_DEVICE_KEY, device_id)

    await DeviceRepository.upsert(
        conn,
        device_id=device_id,
        name=settings.server_name,
        user_agent=f"LANShare server {__version__}",
        kind="server",
        # The machine running the server is trusted by definition.
        trust_state=TRUSTED,
    )
    return device_id


def _scheme(settings: Settings) -> str:
    return "https" if settings.enable_https else "http"


def _print_banner(settings: Settings, lan_url: str, mdns_url: str | None = None) -> None:
    """The one place `print` is allowed: the user needs this on the terminal.

    The addresses come from the settings, so launching uvicorn with a ``--port``
    that differs from ``LANSHARE_PORT`` would print the wrong one. Use
    ``python -m lanshare`` (or keep the two in step) to avoid that.
    """
    lines = [
        "",
        "  LANShare is running",
        f"  On this PC      {_scheme(settings)}://localhost:{settings.port}",
        f"  On your phone   {lan_url}",
    ]
    if mdns_url:
        # Works on Apple devices out of the box; Android browsers usually
        # cannot resolve .local, so the IP above stays the address to trust.
        lines.append(f"  Or by name      {mdns_url}")
    lines += [
        f"  Saving files to {settings.incoming_dir}",
        "",
        "  Point your phone's camera at this code:",
        "",
        _banner_qr(lan_url),
        "",
        "  Same Wi-Fi, no cloud. Ctrl+C to stop.",
        "",
    ]
    # flush: stdout is block-buffered when piped, and the URL is the first
    # thing the user needs, not something to see minutes later.
    print("\n".join(lines), flush=True)


def _banner_qr(lan_url: str) -> str:
    """Terminal QR for the banner.

    Colour needs a real terminal: the coloured form draws the code as spaces on
    black and white backgrounds, so once the escape codes are stripped - piped
    to a file, captured by a service manager - it collapses to a blank box.
    Redirected output therefore gets the ASCII form, which stays readable.
    """
    try:
        is_terminal = sys.stdout.isatty()
    except (AttributeError, ValueError):
        is_terminal = False

    try:
        return qr_terminal(lan_url, ascii_only=not is_terminal)
    except (OSError, ValueError) as exc:  # pragma: no cover - defensive
        logger.warning("Could not render the QR code: %s", exc)
        return f"  (no QR available - open {lan_url} by hand)"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.ensure_directories()

    database = Database(settings.database_path)
    await database.connect()
    app.state.database = database

    app.state.connections = ConnectionManager()
    app.state.storage = Storage(
        incoming_dir=settings.incoming_dir,
        temporary_dir=settings.temporary_dir,
    )
    app.state.transfer_service = TransferService(
        connection=database.connection,
        storage=app.state.storage,
        connections=app.state.connections,
        chunk_size=settings.chunk_size,
        max_file_size=settings.max_file_size,
        stale_after_hours=settings.stale_transfer_hours,
    )
    swept = await app.state.transfer_service.sweep_orphaned_chunks()
    if swept:
        logger.info("Removed chunks for %d transfer(s) that cannot be resumed", swept)

    app.state.server_device_id = await _ensure_server_device(app)
    app.state.lan_ip = get_lan_ip()
    app.state.lan_url = build_lan_url(settings.port, app.state.lan_ip, secure=settings.enable_https)
    app.state.qr_svg = qr_svg(app.state.lan_url)

    app.state.mdns = await _start_mdns(settings, app.state.lan_ip)
    app.state.mdns_url = app.state.mdns.hostname_url if app.state.mdns else None
    app.state.udp_discovery = await _start_udp_discovery(settings, app.state.server_device_id)

    _print_banner(settings, app.state.lan_url, app.state.mdns_url)
    try:
        yield
    finally:
        if app.state.udp_discovery is not None:
            await app.state.udp_discovery.stop()
        if app.state.mdns is not None:
            await app.state.mdns.stop()
        await database.close()


async def _start_mdns(settings: Settings, address: str) -> MdnsAdvertiser | None:
    """Advertise over mDNS, or return None if we could not.

    Never fatal: the server is perfectly usable over its IP address, and mDNS
    is blocked often enough (VPNs, guest networks, firewalls) that a failure
    here must not take the app down with it.
    """
    if not settings.enable_mdns:
        logger.info("mDNS advertising is disabled")
        return None

    advertiser = MdnsAdvertiser(
        instance_name=settings.server_name,
        hostname=settings.mdns_hostname,
        address=address,
        port=settings.port,
        secure=settings.enable_https,
    )
    return advertiser if await advertiser.start() else None


async def _start_udp_discovery(settings: Settings, server_id: str) -> UdpDiscovery | None:
    """Find other LANShare servers on the LAN, or return None if we cannot.

    Non-fatal for the same reason mDNS is: this finds *other servers*, which
    has nothing to do with whether this one can transfer files.
    """
    if not settings.enable_udp_discovery:
        logger.info("UDP discovery is disabled")
        return None

    discovery = UdpDiscovery(
        server_id=server_id,
        server_name=settings.server_name,
        http_port=settings.port,
        discovery_port=settings.discovery_port,
        version=__version__,
        announce_interval=settings.announce_interval_seconds,
        peer_ttl=settings.peer_ttl_seconds,
    )
    return discovery if await discovery.start() else None


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Tests call this with their own settings."""
    app = FastAPI(
        title="LANShare",
        version=__version__,
        description="Send files between devices on the same local network.",
        lifespan=lifespan,
    )
    app.state.settings = settings or get_settings()

    # Outermost check, before routing: a page that reached us under someone
    # else's domain name is a DNS rebinding attempt. See `middleware.py`.
    app.add_middleware(LocalHostsOnly, allowed=app.state.settings.allowed_hosts)

    @app.exception_handler(LanShareError)
    async def _handle_service_error(_: Request, exc: LanShareError) -> JSONResponse:
        """Service errors become clean JSON. Stack traces never reach a client."""
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(health_api.router, prefix="/api")
    app.include_router(devices_api.router, prefix="/api")
    app.include_router(transfers_api.router, prefix="/api")
    app.include_router(files_api.router, prefix="/api")
    app.include_router(peers_api.router, prefix="/api")
    if timing.enabled():
        # Diagnostics only exist while stage timing is switched on.
        app.include_router(diag_api.router, prefix="/api")

    _register_websocket(app)

    # Mounted last so it never shadows /api.
    frontend_dir = app.state.settings.frontend_dir
    if frontend_dir.is_dir():
        app.mount("/", RevalidatingStaticFiles(directory=frontend_dir, html=True), name="frontend")
    else:  # pragma: no cover - only hit if the checkout is incomplete
        logger.warning("No frontend directory at %s", frontend_dir)

    return app


def _token_from_subprotocols(websocket: WebSocket) -> str | None:
    """Read the device token out of the requested WebSocket subprotocols.

    The browser sends `new WebSocket(url, ["lanshare.v1", "token.<value>"])`,
    which arrives as a comma-separated `Sec-WebSocket-Protocol` header.
    """
    header = websocket.headers.get("sec-websocket-protocol")
    if not header:
        return None
    for entry in header.split(","):
        candidate = entry.strip()
        if candidate.startswith(TOKEN_SUBPROTOCOL_PREFIX):
            token = candidate[len(TOKEN_SUBPROTOCOL_PREFIX) :]
            return token or None
    return None


def _offered_subprotocols(websocket: WebSocket) -> list[str]:
    header = websocket.headers.get("sec-websocket-protocol") or ""
    return [entry.strip() for entry in header.split(",") if entry.strip()]


def _negotiated_subprotocol(websocket: WebSocket) -> str | None:
    """Echo our subprotocol only if the client actually offered it.

    Returning a subprotocol the client did not ask for is a protocol
    violation, and a browser fails the connection outright.
    """
    return WS_SUBPROTOCOL if WS_SUBPROTOCOL in _offered_subprotocols(websocket) else None


def _register_websocket(app: FastAPI) -> None:
    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        device_id: str = Query(min_length=8, max_length=64),
    ) -> None:
        """Live events for one device. Survives reconnects; the client backs off.

        The token travels in ``Sec-WebSocket-Protocol``, not the query string:
        uvicorn writes the full request line to its access log, so a token in
        the URL would end up in plain text in every log file. The subprotocol
        header is the standard way around that in a browser, since the
        WebSocket API cannot set arbitrary headers.
        """
        conn = app.state.database.connection
        connections: ConnectionManager = app.state.connections

        token = _token_from_subprotocols(websocket)
        device = await DeviceRepository.get(conn, device_id)

        # Accept before rejecting, so the application close code survives.
        #
        # Closing *before* accept() makes the ASGI server fail the handshake
        # with HTTP 403, and every client then sees close code 1006 - which is
        # indistinguishable from the server being down. The browser would retry
        # forever instead of showing the waiting screen, and 4403/4404 would be
        # dead code. Nothing is read from the socket before it is authorised.
        await websocket.accept(subprotocol=_negotiated_subprotocol(websocket))

        if device is None or not verify_token(token, device.get("token_hash")):
            # 4404: application-level "register first", distinct from a network close.
            await websocket.close(code=4404, reason="Unknown device; register first")
            return

        if device.get("trust_state") != TRUSTED:
            # 4403: registered, but not approved. The client shows the waiting
            # screen and polls instead of retrying the socket forever.
            await websocket.close(code=4403, reason="Device is not approved")
            return

        became_online = connections.add(device_id, websocket)
        await DeviceRepository.touch(conn, device_id)

        if became_online:
            joined = DeviceResponse.from_row(device, online=True)
            await connections.broadcast(
                event("device.joined", device=joined.model_dump()),
                exclude=device_id,
            )
        await _push_device_list(app)

        try:
            while True:
                message = await websocket.receive_json()
                await _handle_client_message(app, device_id, websocket, message)
        except WebSocketDisconnect:
            logger.debug("Device %s disconnected", device_id)
        except (ValueError, TypeError) as exc:  # malformed frame
            logger.debug("Bad frame from %s: %s", device_id, exc)
        finally:
            if connections.remove(device_id, websocket):
                await connections.broadcast(event("device.left", device_id=device_id))
                await _push_device_list(app)


async def _handle_client_message(
    app: FastAPI, device_id: str, websocket: WebSocket, message: dict[str, Any]
) -> None:
    """Client -> server messages. Anything unknown is ignored, not fatal."""
    if not isinstance(message, dict):
        # receive_json accepts any JSON value, so a bare list or string is
        # possible. Without this, `.get` raises AttributeError, which is not
        # caught by the frame handler and kills the connection with a traceback.
        logger.debug("Ignoring non-object frame from %s", device_id)
        return

    message_type = message.get("type")
    data = message.get("data")
    if not isinstance(data, dict):
        data = {}

    if message_type == "ping":
        await websocket.send_json(event("pong"))
        await DeviceRepository.touch(app.state.database.connection, device_id)
        return

    if message_type == "device.rename":
        name = str(data.get("name", "")).strip()[:64]
        if name:
            await DeviceRepository.rename(app.state.database.connection, device_id, name)
            await _push_device_list(app)
        return

    logger.debug("Ignoring unknown message type %r from %s", message_type, device_id)


async def _push_device_list(app: FastAPI) -> None:
    """Send the current device list to everyone connected."""
    payload = await devices_api.device_list_event(
        app.state.database.connection,
        app.state.connections,
        app.state.server_device_id,
    )
    await app.state.connections.broadcast(payload)


app = create_app()
