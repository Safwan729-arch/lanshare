"""The transfer lifecycle: init, chunk upload, status, complete, cancel, history."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from ..models import (
    ChunkUploadResponse,
    TransferCreateRequest,
    TransferCreateResponse,
    TransferListResponse,
    TransferResponse,
)
from .deps import DeviceIdDep, ServiceDep

router = APIRouter(prefix="/transfers", tags=["transfers"])

TransferIdPath = Annotated[str, Path(min_length=8, max_length=64)]


def _response(service: ServiceDep, row: dict) -> TransferResponse:
    received, missing = service.state_of(row)
    return TransferResponse.from_row(row, received=received, missing=missing)


@router.post("", response_model=TransferCreateResponse, status_code=201)
async def create_transfer(
    payload: TransferCreateRequest,
    device_id: DeviceIdDep,
    service: ServiceDep,
) -> TransferCreateResponse:
    """Reserve a transfer. The client then uploads ``total_chunks`` chunks."""
    transfer = await service.create(
        sender_id=device_id,
        filename=payload.filename,
        size=payload.size,
        receiver_id=payload.receiver_id,
        mime_type=payload.mime_type,
    )
    return TransferCreateResponse(
        transfer_id=transfer["id"],
        chunk_size=transfer["chunk_size"],
        total_chunks=transfer["total_chunks"],
    )


@router.put("/{transfer_id}/chunks/{index}", response_model=ChunkUploadResponse)
async def upload_chunk(
    transfer_id: TransferIdPath,
    index: Annotated[int, Path(ge=0)],
    request: Request,
    device_id: DeviceIdDep,
    service: ServiceDep,
) -> ChunkUploadResponse:
    """Upload one chunk as a raw body.

    Idempotent: re-sending the same index overwrites it, which is what makes
    resume after a dropped connection safe.
    """
    written, received = await service.write_chunk(
        transfer_id=transfer_id,
        index=index,
        sender_id=device_id,
        stream=request.stream(),
    )
    transfer = await service.get(transfer_id)
    return ChunkUploadResponse(
        transfer_id=transfer_id,
        index=index,
        bytes_written=written,
        received_chunks=received,
        total_chunks=transfer["total_chunks"],
    )


@router.get("/{transfer_id}", response_model=TransferResponse)
async def get_transfer(
    transfer_id: TransferIdPath,
    device_id: DeviceIdDep,
    service: ServiceDep,
) -> TransferResponse:
    """Status plus which chunk indexes arrived. This is the resume endpoint.

    Restricted to the two devices involved: chunk state would otherwise let any
    trusted device watch a transfer it has nothing to do with.
    """
    return _response(service, await service.get(transfer_id, device_id=device_id))


@router.post("/{transfer_id}/complete", response_model=TransferResponse)
async def complete_transfer(
    transfer_id: TransferIdPath,
    device_id: DeviceIdDep,
    service: ServiceDep,
) -> TransferResponse:
    """Assemble the chunks, hash the result and move it into the incoming folder."""
    return _response(service, await service.complete(transfer_id=transfer_id, sender_id=device_id))


@router.delete("/{transfer_id}", response_model=TransferResponse)
async def cancel_transfer(
    transfer_id: TransferIdPath,
    device_id: DeviceIdDep,
    service: ServiceDep,
) -> TransferResponse:
    """Cancel an in-flight transfer and delete its partial chunks."""
    return _response(service, await service.cancel(transfer_id=transfer_id, device_id=device_id))


@router.get("", response_model=TransferListResponse)
async def list_transfers(
    device_id: DeviceIdDep,
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> TransferListResponse:
    """This device's transfer history, newest first.

    Always scoped to the caller. Taking the device from a query parameter would
    let any trusted device read another's history, filenames included.
    """
    rows = await service.history(device_id=device_id, limit=limit)
    return TransferListResponse(transfers=[_response(service, row) for row in rows])
