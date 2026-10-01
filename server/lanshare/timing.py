"""Opt-in stage timing for one request path.

Set ``LANSHARE_TIMING_LOG`` to a file path to record how long each stage of a
chunk upload takes. Off unless the variable is set, so the ordinary run pays
nothing. This exists to answer "the body arrived but the response did not -
where did the time go?", which no ordinary log can show.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_PATH = os.environ.get("LANSHARE_TIMING_LOG")
_lock = threading.Lock()


def enabled() -> bool:
    return bool(_PATH)


def mark(label: str, **fields: object) -> None:
    """Append one timestamped line. Never raises: diagnostics must not break a transfer."""
    if not _PATH:
        return
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    line = f"{time.time():.3f} {time.monotonic():12.3f} {label} {detail}\n"
    try:
        with _lock, Path(_PATH).open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
