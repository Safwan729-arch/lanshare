"""Liveness and the information a client needs to describe this server."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__
from ..models import HealthResponse, ServerInfoResponse
from .deps import DeviceIdDep, SettingsDep

router = APIRouter(tags=["server"])


@router.get("/health", response_model=HealthResponse)
async def health(settings: SettingsDep) -> HealthResponse:
    """Liveness. Deliberately open: it is how you check the server is up at
    all, including before any device has registered. It returns only what the
    startup banner already prints on screen."""
    return HealthResponse(server_name=settings.server_name, version=__version__)


@router.get("/server-info", response_model=ServerInfoResponse)
async def server_info(
    request: Request, settings: SettingsDep, _: DeviceIdDep
) -> ServerInfoResponse:
    """Everything a paired client needs to describe this server.

    Trusted-only. It carries the host's own device id, the LAN URL and the
    pairing QR, which together describe the network to anyone who asks - and
    nothing unapproved needs it: the waiting screen does not call this.
    """
    return ServerInfoResponse(
        server_name=settings.server_name,
        version=__version__,
        lan_url=request.app.state.lan_url,
        port=settings.port,
        server_device_id=request.app.state.server_device_id,
        chunk_size=settings.chunk_size,
        max_file_size=settings.max_file_size,
        # Built once at startup: the URL cannot change while the server runs.
        qr_svg=request.app.state.qr_svg,
        # None when mDNS could not advertise - blocked multicast, name clash.
        mdns_url=request.app.state.mdns_url,
    )
