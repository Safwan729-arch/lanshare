"""Other LANShare servers found on the LAN (Phase 4).

Discovery only. These are separate servers with their own devices and their own
storage; nothing here sends anything to them.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..models import PeerListResponse, PeerResponse
from .deps import DeviceIdDep

router = APIRouter(prefix="/peers", tags=["peers"])


@router.get("", response_model=PeerListResponse)
async def list_peers(request: Request, _: DeviceIdDep) -> PeerListResponse:
    """LANShare servers that have announced themselves recently.

    Empty when discovery is off, when the broadcast socket could not open, or
    simply when this is the only instance on the network — the
    ``discovery_enabled`` flag is what tells those apart.
    """
    discovery = request.app.state.udp_discovery

    if discovery is None:
        return PeerListResponse(peers=[], discovery_enabled=False)

    return PeerListResponse(
        peers=[
            PeerResponse(
                server_id=peer.server_id,
                name=peer.name,
                address=peer.address,
                port=peer.port,
                version=peer.version,
                url=peer.url,
            )
            for peer in discovery.peers()
        ],
        discovery_enabled=True,
    )
