"""mDNS / DNS-SD advertising (Phase 3).

Publishes two things on the local network:

1. A ``_http._tcp`` service, so anything doing service discovery (macOS Finder,
   Bonjour browsers, a future LANShare CLI) can find this server.
2. An A record for ``lanshare.local``, so a phone can reach the server by name
   instead of by an IP address that changes with the DHCP lease.

Discovery is a separate subsystem: nothing here knows about transfers.

Advertising is **best effort**. mDNS needs UDP multicast on 224.0.0.251:5353,
which a firewall, a VPN adapter or a locked-down network may block, and a
second instance on the same LAN will collide on the name. None of that should
stop the server from serving, so every failure here is logged and swallowed.
"""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion, NonUniqueNameException, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from .. import __version__

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_http._tcp.local."


def build_service_info(
    *,
    instance_name: str,
    hostname: str,
    address: str,
    port: int,
) -> ServiceInfo:
    """Describe this server for the network.

    ``hostname`` is the bare label ("lanshare"); mDNS names are fully qualified
    and dot-terminated, so it becomes "lanshare.local." here.
    """
    return ServiceInfo(
        SERVICE_TYPE,
        f"{instance_name}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(address)],
        port=port,
        # `path` is the DNS-SD convention for where the service lives on the
        # host; browsers that offer to open a discovered service use it.
        properties={"path": "/", "version": __version__},
        server=f"{hostname}.local.",
    )


class MdnsAdvertiser:
    """Registers the service on start, withdraws it on stop.

    Safe to stop without having started, and safe to stop twice - shutdown
    paths should never have to check.
    """

    def __init__(
        self,
        *,
        instance_name: str,
        hostname: str,
        address: str,
        port: int,
        secure: bool = False,
    ) -> None:
        self._scheme = "https" if secure else "http"
        self._info = build_service_info(
            instance_name=instance_name,
            hostname=hostname,
            address=address,
            port=port,
        )
        self._zeroconf: AsyncZeroconf | None = None
        self.registered_name: str | None = None

    @property
    def hostname_url(self) -> str:
        """The address to hand a user, e.g. ``http://lanshare.local:8080``."""
        host = str(self._info.server).rstrip(".")
        return f"{self._scheme}://{host}:{self._info.port}"

    async def start(self) -> bool:
        """Advertise. Returns False if it could not, without raising."""
        try:
            # V4Only: we advertise a single IPv4 address, and mixing in IPv6
            # link-local records here just creates addresses phones cannot use.
            self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
            # allow_name_change lets zeroconf pick "LANShare PC (2)" rather
            # than fail outright when a second instance is already advertising.
            await self._zeroconf.async_register_service(self._info, allow_name_change=True)
        except NonUniqueNameException:
            logger.warning(
                "mDNS name %s is already taken on this network; not advertising",
                self._info.name,
            )
            await self._shutdown()
            return False
        except (OSError, RuntimeError) as exc:
            # Multicast blocked, no usable interface, event loop trouble.
            logger.warning("Could not advertise over mDNS: %s", exc)
            await self._shutdown()
            return False

        self.registered_name = str(self._info.name)
        logger.info("Advertising %s as %s", self.registered_name, self.hostname_url)
        return True

    async def stop(self) -> None:
        """Withdraw the advertisement so browsers drop us immediately."""
        if self._zeroconf is None:
            return
        try:
            await self._zeroconf.async_unregister_service(self._info)
        except (OSError, RuntimeError) as exc:
            logger.debug("Could not cleanly unregister the mDNS service: %s", exc)
        await self._shutdown()

    async def _shutdown(self) -> None:
        if self._zeroconf is None:
            return
        try:
            await self._zeroconf.async_close()
        except (OSError, RuntimeError) as exc:
            logger.debug("Could not close zeroconf: %s", exc)
        finally:
            self._zeroconf = None
