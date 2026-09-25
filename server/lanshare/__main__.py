"""``python -m lanshare`` - run the server using the configured host and port.

Preferred over a bare uvicorn command because the address in the startup banner
and the address uvicorn actually binds come from the same settings object.
"""

from __future__ import annotations

import logging

import uvicorn

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


def ssl_paths(settings: Settings) -> tuple[str | None, str | None]:
    """Certificate and key for uvicorn, or ``(None, None)`` for plain http.

    Refuses to start rather than quietly falling back to http: someone who
    turned HTTPS on and silently got http anyway would have no idea their
    traffic was in the clear.
    """
    if not settings.enable_https:
        return None, None

    certfile, keyfile = settings.ssl_certfile, settings.ssl_keyfile
    if certfile is None or keyfile is None:
        raise SystemExit(
            "LANSHARE_ENABLE_HTTPS is on but LANSHARE_SSL_CERTFILE and "
            "LANSHARE_SSL_KEYFILE are not both set. See the README for how to "
            "generate a certificate."
        )

    missing = [str(p) for p in (certfile, keyfile) if not p.is_file()]
    if missing:
        raise SystemExit(f"Certificate file(s) not found: {', '.join(missing)}")

    return str(certfile), str(keyfile)


def main() -> None:
    settings = get_settings()
    certfile, keyfile = ssl_paths(settings)
    uvicorn.run(
        "lanshare.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
    )


if __name__ == "__main__":
    main()
