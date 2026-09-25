"""Streaming a completed file back to the receiving device."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Path
from fastapi.responses import FileResponse

from .deps import DeviceIdDep, ServiceDep

router = APIRouter(prefix="/files", tags=["files"])

DEFAULT_MIME = "application/octet-stream"


def content_disposition(filename: str) -> str:
    """Build a header iOS Safari and desktop browsers both handle.

    ASCII fallback first for old clients, then the RFC 5987 ``filename*`` form
    so non-ASCII names (emoji, accents, CJK) survive the trip.
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/{transfer_id}/download")
async def download_file(
    transfer_id: Annotated[str, Path(min_length=8, max_length=64)],
    device_id: DeviceIdDep,
    service: ServiceDep,
) -> FileResponse:
    """Stream the assembled file. ``FileResponse`` handles range requests.

    Restricted to the sender and receiver. This endpoint serves real file
    contents, so a trusted-but-uninvolved device must not be able to fetch it
    by guessing or overhearing a transfer id.
    """
    path, transfer = await service.resolve_download(transfer_id, device_id=device_id)
    return FileResponse(
        path,
        media_type=transfer["mime_type"] or DEFAULT_MIME,
        headers={"Content-Disposition": content_disposition(transfer["filename"])},
    )
