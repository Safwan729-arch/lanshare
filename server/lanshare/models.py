"""Pydantic request/response schemas. The REST contract in one file."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# Device ids are UUIDs generated in the browser; accept any hex/dash token so a
# hand-written client is not forced into a specific UUID version.
DeviceId = Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{8,64}$")]

# A media type the sender picked, which the download echoes back as the
# response's Content-Type. RFC 9110 tokens, with unquoted parameters - which is
# all a browser's `File.type` ever produces.
#
# This is not pedantry. uvicorn refuses to put a CR or LF in a header value, but
# it refuses at *send* time: an upload with a newline in its media type finishes
# happily and then every download of it dies, for ever, for the receiver. The
# value is checked where it arrives instead.
_TOKEN = r"[A-Za-z0-9!#$%&'*+.^_`|~-]+"
MEDIA_TYPE_PATTERN = rf"^{_TOKEN}/{_TOKEN}(?: *; *{_TOKEN}={_TOKEN})*$"
_MEDIA_TYPE = re.compile(MEDIA_TYPE_PATTERN)


def is_sendable_media_type(value: str | None) -> bool:
    """Can this value safely become a ``Content-Type`` header?

    Used on the way out as well as the way in: a row stored before this check
    existed must still be downloadable, just not with its broken value.
    """
    return value is not None and _MEDIA_TYPE.match(value) is not None


TransferStatus = Literal[
    "awaiting", "pending", "uploading", "completed", "failed", "cancelled", "declined"
]
TrustState = Literal["pending", "trusted", "blocked"]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    server_name: str
    version: str


class ServerInfoResponse(BaseModel):
    server_name: str
    version: str
    lan_url: str
    port: int
    server_device_id: str
    chunk_size: int
    max_file_size: int
    qr_svg: str  # inline SVG of lan_url, for pairing a new device
    mdns_url: str | None = None  # http://lanshare.local:PORT, if advertising worked


class PeerResponse(BaseModel):
    """Another LANShare server discovered over UDP broadcast."""

    server_id: str
    name: str
    address: str
    port: int
    version: str
    url: str


class PeerListResponse(BaseModel):
    peers: list[PeerResponse]
    discovery_enabled: bool


class DeviceRegisterRequest(BaseModel):
    device_id: DeviceId
    name: Annotated[str, Field(min_length=1, max_length=64)]
    user_agent: Annotated[str | None, Field(default=None, max_length=512)] = None


class DeviceResponse(BaseModel):
    id: str
    name: str
    kind: Literal["browser", "server"]
    online: bool
    last_seen: str
    trust_state: TrustState = "pending"
    first_address: str | None = None
    approved_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any], *, online: bool) -> DeviceResponse:
        return cls(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            online=online,
            last_seen=row["last_seen"],
            trust_state=row.get("trust_state", "pending"),
            first_address=row.get("first_address"),
            approved_at=row.get("approved_at"),
        )


class DeviceRegisterResponse(BaseModel):
    """What a device gets back when it registers.

    ``token`` is present only on first registration - it is the one and only
    time the plaintext exists anywhere outside the client, since the server
    keeps just a hash. A device that loses it must register as a new device.
    """

    device: DeviceResponse
    token: str | None = None


class TrustDecisionRequest(BaseModel):
    """Approve or refuse a waiting device."""

    decision: Literal["approve", "deny", "block"]


class ConsentDecisionRequest(BaseModel):
    """Accept or refuse a file someone is trying to send you."""

    decision: Literal["accept", "decline"]


class DeviceListResponse(BaseModel):
    devices: list[DeviceResponse]


class TransferCreateRequest(BaseModel):
    filename: Annotated[str, Field(min_length=1, max_length=512)]
    size: Annotated[int, Field(ge=0)]
    receiver_id: DeviceId
    #: What the sender says the file hashes to, if it was able to work it out.
    #:
    #: Optional because a browser cannot hash a file on plain http - the Web
    #: Crypto API only exists in a secure context - so most phones cannot
    #: supply it. When it is present the server checks the assembled file
    #: against it and refuses a mismatch.
    sha256: Annotated[str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")] = None
    mime_type: Annotated[
        str | None, Field(default=None, max_length=255, pattern=MEDIA_TYPE_PATTERN)
    ] = None


class ClearHistoryResponse(BaseModel):
    """How many transfers the caller just forgot."""

    deleted: int


class TransferCreateResponse(BaseModel):
    transfer_id: str
    chunk_size: int
    total_chunks: int
    status: TransferStatus = "awaiting"


class TransferResponse(BaseModel):
    id: str
    filename: str
    size: int
    mime_type: str | None
    sha256: str | None
    sender_id: str
    receiver_id: str
    status: TransferStatus
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    missing_chunks: list[int]
    created_at: str
    completed_at: str | None
    error: str | None

    @classmethod
    def from_row(
        cls,
        row: dict[str, Any],
        *,
        received: list[int] | None = None,
        missing: list[int] | None = None,
    ) -> TransferResponse:
        return cls(
            id=row["id"],
            filename=row["filename"],
            size=row["size"],
            mime_type=row["mime_type"],
            sha256=row["sha256"],
            sender_id=row["sender_id"],
            receiver_id=row["receiver_id"],
            status=row["status"],
            chunk_size=row["chunk_size"],
            total_chunks=row["total_chunks"],
            received_chunks=received or [],
            missing_chunks=missing or [],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            error=row["error"],
        )


class TransferListResponse(BaseModel):
    transfers: list[TransferResponse]


class ChunkUploadResponse(BaseModel):
    transfer_id: str
    index: int
    bytes_written: int
    received_chunks: int
    total_chunks: int
