"""The transfer lifecycle: init, chunk upload, status, complete, cancel, history."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from .. import timing
from ..models import (
    ChunkUploadResponse,
    ClearHistoryResponse,
    ConsentDecisionRequest,
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
    timing.mark("create.enter", device=device_id[:8], size=payload.size, name=payload.filename[:28])
    transfer = await service.create(
        sender_id=device_id,
        filename=payload.filename,
        size=payload.size,
        receiver_id=payload.receiver_id,
        mime_type=payload.mime_type,
        expected_sha256=payload.sha256,
    )
    timing.mark("create.return", tid=str(transfer["id"])[:8], chunks=transfer["total_chunks"])
    return TransferCreateResponse(
        transfer_id=transfer["id"],
        chunk_size=transfer["chunk_size"],
        total_chunks=transfer["total_chunks"],
        status=transfer["status"],
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
    timing.mark("endpoint.enter", tid=transfer_id[:8], index=index)
    # How the browser framed the request body. A phone that switches from a
    # counted body to a streamed one above some size would explain why only
    # large chunks hang, and nothing else can show that.
    timing.mark(
        "request.framing",
        tid=transfer_id[:8],
        index=index,
        length=request.headers.get("content-length", "-"),
        encoding=request.headers.get("transfer-encoding", "-"),
        http=request.scope.get("http_version", "-"),
        conn=request.headers.get("connection", "-"),
    )
    written, received = await service.write_chunk(
        transfer_id=transfer_id,
        index=index,
        sender_id=device_id,
        stream=request.stream(),
    )
    transfer = await service.get(transfer_id)
    timing.mark("endpoint.return", tid=transfer_id[:8], index=index, written=written)
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


@router.post("/{transfer_id}/consent", response_model=TransferResponse)
async def consent_to_transfer(
    transfer_id: TransferIdPath,
    payload: ConsentDecisionRequest,
    device_id: DeviceIdDep,
    service: ServiceDep,
) -> TransferResponse:
    """Accept or refuse a file someone is sending you.

    Deliberately shaped like `POST /api/devices/{id}/trust`: this app asks a
    person two questions, and they should read the same way.
    """
    answered = await service.consent(
        transfer_id=transfer_id, device_id=device_id, accept=payload.decision == "accept"
    )
    return _response(service, answered)


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


@router.delete("", response_model=ClearHistoryResponse)
async def clear_history(
    device_id: DeviceIdDep,
    service: ServiceDep,
) -> ClearHistoryResponse:
    """Forget this device's finished transfers.

    Scoped to the caller, exactly like the history it clears - there is no
    device_id parameter, so no device can clear another's.

    Two things this deliberately does not do. It does not delete received
    files: clearing a list should not destroy what the transfers delivered.
    And it does not touch a transfer still running, which would 404 its next
    chunk and leave its chunks stranded.

    One consequence worth knowing: a transfer row is shared by its sender and
    its receiver, so clearing here also removes those entries from the other
    device's history.
    """
    deleted = await service.clear_history(device_id=device_id)
    return ClearHistoryResponse(deleted=deleted)


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
