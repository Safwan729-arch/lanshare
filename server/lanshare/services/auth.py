"""Device tokens and trust states (Phase 5).

The trust model, in one paragraph: a device registers and is issued a **token**
straight away, but starts out `pending` and can do almost nothing. The token
says *who you are*; the trust state says *what you may do*. An already-trusted
device approves it, and the same token then works for everything. That split is
what avoids the awkward "how does an approved device collect its credential
later" problem — there is nothing to collect.

The host PC is trusted automatically, but only when it registers over loopback.
We cannot tell the host's own browser from anyone else's when the request
arrives over the LAN, so loopback is the only signal we can safely trust.
"""

from __future__ import annotations

import hashlib
import ipaddress
import secrets
from collections.abc import Mapping
from typing import Any, Final, Literal

TrustState = Literal["pending", "trusted", "blocked"]

PENDING: Final[TrustState] = "pending"
TRUSTED: Final[TrustState] = "trusted"
BLOCKED: Final[TrustState] = "blocked"

VALID_TRUST_STATES: Final[frozenset[str]] = frozenset({PENDING, TRUSTED, BLOCKED})

# 32 bytes of urandom, ~43 characters once base64url encoded.
TOKEN_BYTES: Final[int] = 32


def generate_token() -> str:
    """A fresh device credential. Shown once, then only its hash is kept."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Hash a token for storage.

    Plain SHA-256 rather than bcrypt or argon2, deliberately. Those exist to
    make *guessable* secrets expensive to attack. These tokens are 256 bits of
    urandom, so there is no dictionary and no rainbow table to defend against -
    a slow hash would only cost us latency on every request. It would also mean
    a new dependency.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str | None, stored_hash: str | None) -> bool:
    """Constant-time check of a presented token against the stored hash."""
    if not token or not stored_hash:
        return False
    # compare_digest, not ==, so a wrong token cannot be narrowed down by
    # timing how long the comparison took.
    return secrets.compare_digest(hash_token(token), stored_hash)


def is_loopback(address: str | None) -> bool:
    """Did this request come from the machine running the server?"""
    if not address:
        return False
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def initial_trust_state(client_address: str | None) -> TrustState:
    """Loopback registers as trusted; everyone else waits for approval."""
    return TRUSTED if is_loopback(client_address) else PENDING


def is_host_device(device: Mapping[str, Any]) -> bool:
    """Is this the machine running the server, rather than a device on the LAN?

    Two ways to be the host: be the server's own row, or be a browser that
    first registered over loopback - which is the only way a device becomes
    trusted without anyone approving it.

    This is what "approval happens on the host" is enforced against. The
    alternative - checking the address of the request doing the approving -
    sounds stronger but is not: the host's browser reaches the server over the
    LAN address just as easily, and would then be refused for no good reason.
    What matters is which machine the device *is*.
    """
    if device.get("kind") == "server":
        return True
    return is_loopback(device.get("first_address"))
