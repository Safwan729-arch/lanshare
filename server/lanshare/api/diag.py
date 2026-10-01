"""A place for the browser to report what it observed.

Only mounted while ``LANSHARE_TIMING_LOG`` is set. The server can see that it
answered a request in 130ms; it cannot see whether the answer ever arrived.
Only the client knows that, so the client has to be able to say so.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body

from .. import timing

router = APIRouter(prefix="/diag", tags=["diag"])


@router.post("/client", status_code=204)
async def client_trace(payload: Annotated[dict[str, Any], Body()]) -> None:
    """Record one client-side trace. Deliberately unauthenticated and lenient:
    a diagnostic that only works when everything already works is useless."""
    label = str(payload.get("label", "client"))[:40]
    events = payload.get("events") or []
    timing.mark(
        f"client.{label}",
        index=payload.get("index"),
        size=payload.get("size"),
        outcome=str(payload.get("outcome"))[:40],
    )
    for entry in events[:80]:
        timing.mark(
            "  client.event",
            at=entry.get("t"),
            name=str(entry.get("name"))[:30],
            detail=str(entry.get("detail"))[:60],
        )
