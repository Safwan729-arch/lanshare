"""Refuse requests that reach us under a hostname nobody on this LAN would use.

This exists for one attack: **DNS rebinding**. A page on the internet cannot
read a response from `http://127.0.0.1:8080` - the same-origin policy forbids
it - but it can own a domain, answer the first lookup with its own address and
the next one with `127.0.0.1`, and then call the server as
`http://evil.example.com:8080`. The browser considers that the same origin as
the attacker's page, so the page reads every response. The requests arrive from
loopback, and this server trusts loopback on sight, so the page would be issued
a token for a *trusted* device: the device list, the transfer history, and
every received file.

The Host header is the part the attacker cannot change. It is whatever the
browser connected to, which for them is a name they had to be able to register.
So the rule is about the shape of the name, not a list of addresses:

* an **IP literal** is fine - a browser never resolves one, so it cannot be
  rebound, and it is what the QR code and the banner actually hand out;
* a **single-label name** (`localhost`, `DESKTOP-7F2K1`) is fine - these resolve
  through the hosts file, NetBIOS or a local resolver, and cannot be bought;
* a name under a **reserved local suffix** (`.local`, `.lan`, ...) is fine, for
  the same reason: none of them can be registered on the public internet;
* anything else - any ordinary registrable domain - is refused unless the host
  has explicitly listed it in ``LANSHARE_ALLOWED_HOSTS``.

This is a defence in depth, not the only one: a device still has to be approved
on the host before it can move a file. It closes the path where approval is
skipped because the request looked like it came from the host's own browser.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Suffixes reserved for local use. ICANN will not delegate them, so an
#: attacker cannot own `something.local` and point it at this machine.
LOCAL_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".lan",
    ".home",
    ".home.arpa",
    ".internal",
    ".localdomain",
    ".localhost",
)

#: Sent instead of the real response. Deliberately plain: a browser showing
#: this is either misconfigured or hostile, and neither needs detail.
REFUSAL = (
    "This server only answers to its own address on the local network. "
    "Use the address shown on the host, or set LANSHARE_ALLOWED_HOSTS."
)


def hostname_of(host_header: str) -> str:
    """The name out of a Host header, without its port.

    Handles the bracketed form IPv6 requires (`[::1]:8080`) and tolerates the
    unbracketed one some clients send anyway.
    """
    value = host_header.strip().rstrip(".")  # a trailing dot is the same name
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value[1:]
    if value.count(":") > 1:
        return value  # a bare IPv6 literal: no port to strip
    return value.split(":", 1)[0]


def is_local_hostname(name: str, allowed: Iterable[str] = ()) -> bool:
    """Could this name only have come from the local network?"""
    if not name:
        return False

    lowered = name.lower()
    if lowered in {entry.strip().lower().rstrip(".") for entry in allowed if entry.strip()}:
        return True

    try:
        ipaddress.ip_address(lowered)
    except ValueError:
        pass
    else:
        return True  # an address the browser never resolved, so never rebound

    if "." not in lowered:
        return True  # single label: hosts file, NetBIOS, local resolver

    return lowered.endswith(LOCAL_SUFFIXES)


class LocalHostsOnly:
    """ASGI middleware applying :func:`is_local_hostname` to every request.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because that one only sees
    `http` scopes, and the WebSocket carries the transfer events - leaving it
    reachable would make the check theatre.
    """

    def __init__(self, app: ASGIApp, allowed: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed = tuple(allowed)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        host = ""
        for key, value in scope.get("headers", ()):
            if key == b"host":
                host = value.decode("latin-1")
                break

        if is_local_hostname(hostname_of(host), self.allowed):
            await self.app(scope, receive, send)
            return

        logger.warning("Refused a request for host %r", host[:100])
        if scope["type"] == "websocket":
            # Closing before accept fails the handshake, which is what a
            # browser needs to see.
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse(REFUSAL, status_code=400)(scope, receive, send)
