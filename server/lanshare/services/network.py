"""Work out which address other devices on the LAN should use to reach us."""

from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)

# RFC 5737 documentation address. Nothing is ever sent to it - connecting a UDP
# socket only asks the OS routing table which local interface it *would* use,
# which is how we find the adapter that actually carries LAN traffic. Using a
# reserved address rather than a public one keeps this honestly offline.
_ROUTE_PROBE = ("192.0.2.1", 9)


def _is_usable(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback and not ip.is_link_local


def _from_routing_table() -> str | None:
    """Ask the OS which interface serves the default route."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(_ROUTE_PROBE)
        address: str = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return address if _is_usable(address) else None


def _from_hostname() -> str | None:
    """Fallback for machines with no default route (Wi-Fi with no gateway)."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return None
    for info in infos:
        address = str(info[4][0])
        if _is_usable(address):
            return address
    return None


def get_lan_ip() -> str:
    """Best guess at this machine's LAN address, or loopback if there is none.

    A VPN, Hyper-V or WSL adapter can win the default route and make this pick
    an address phones cannot reach. When that happens the user should set the
    address by hand rather than have us guess harder.
    """
    address = _from_routing_table() or _from_hostname()
    if address is None:
        logger.warning("No private LAN address found; falling back to 127.0.0.1")
        return "127.0.0.1"
    return address


def build_lan_url(port: int, host: str | None = None, *, secure: bool = False) -> str:
    scheme = "https" if secure else "http"
    return f"{scheme}://{host or get_lan_ip()}:{port}"
