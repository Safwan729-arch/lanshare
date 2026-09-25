"""mDNS advertising (Phase 3).

The service description is checked directly. Registration is checked through
fakes rather than real multicast: a test that needed a working LAN would fail
in CI, on a VPN, or on a machine with multicast blocked.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from conftest import headers
from lanshare.discovery.mdns import SERVICE_TYPE, MdnsAdvertiser, build_service_info
from zeroconf import NonUniqueNameException

ADDRESS = "192.168.1.20"
PORT = 8080


def make_advertiser(**overrides: Any) -> MdnsAdvertiser:
    kwargs: dict[str, Any] = {
        "instance_name": "LANShare PC",
        "hostname": "lanshare",
        "address": ADDRESS,
        "port": PORT,
    }
    kwargs.update(overrides)
    return MdnsAdvertiser(**kwargs)


class FakeZeroconf:
    """Stands in for AsyncZeroconf, recording what it was asked to do."""

    def __init__(self, *, register_error: Exception | None = None, **_: Any) -> None:
        self._register_error = register_error
        self.registered: list[Any] = []
        self.unregistered: list[Any] = []
        self.closed = False

    async def async_register_service(self, info: Any, **_: Any) -> None:
        if self._register_error is not None:
            raise self._register_error
        self.registered.append(info)

    async def async_unregister_service(self, info: Any) -> None:
        self.unregistered.append(info)

    async def async_close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_zeroconf(monkeypatch: pytest.MonkeyPatch) -> list[FakeZeroconf]:
    """Swap in the fake and hand back every instance created."""
    created: list[FakeZeroconf] = []

    def factory(**kwargs: Any) -> FakeZeroconf:
        instance = FakeZeroconf(**kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr("lanshare.discovery.mdns.AsyncZeroconf", factory)
    return created


def failing_zeroconf(monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[FakeZeroconf]:
    created: list[FakeZeroconf] = []

    def factory(**kwargs: Any) -> FakeZeroconf:
        instance = FakeZeroconf(register_error=error, **kwargs)
        created.append(instance)
        return instance

    monkeypatch.setattr("lanshare.discovery.mdns.AsyncZeroconf", factory)
    return created


# -- the service description -------------------------------------------------


def test_service_info_describes_an_http_service() -> None:
    info = build_service_info(
        instance_name="LANShare PC", hostname="lanshare", address=ADDRESS, port=PORT
    )
    assert info.type == SERVICE_TYPE
    assert info.name == f"LANShare PC.{SERVICE_TYPE}"
    assert info.port == PORT


def test_service_info_publishes_the_dotted_hostname() -> None:
    """`lanshare` has to become `lanshare.local.` - fully qualified and
    dot-terminated - or the A record is not published and the name will not
    resolve."""
    info = build_service_info(
        instance_name="LANShare PC", hostname="lanshare", address=ADDRESS, port=PORT
    )
    assert info.server == "lanshare.local."


def test_service_info_carries_the_ipv4_address() -> None:
    info = build_service_info(
        instance_name="LANShare PC", hostname="lanshare", address=ADDRESS, port=PORT
    )
    assert [socket.inet_ntoa(a) for a in info.addresses] == [ADDRESS]


def test_service_info_advertises_a_path() -> None:
    """DNS-SD browsers use `path` to know what URL to open."""
    info = build_service_info(
        instance_name="LANShare PC", hostname="lanshare", address=ADDRESS, port=PORT
    )
    assert info.properties[b"path"] == b"/"


def test_rejects_an_address_that_is_not_ipv4() -> None:
    with pytest.raises(OSError):
        build_service_info(
            instance_name="LANShare PC", hostname="lanshare", address="not-an-ip", port=PORT
        )


def test_hostname_url_is_what_a_user_would_type() -> None:
    assert make_advertiser().hostname_url == "http://lanshare.local:8080"


def test_hostname_url_follows_a_custom_hostname() -> None:
    advertiser = make_advertiser(hostname="sams-pc", port=9000)
    assert advertiser.hostname_url == "http://sams-pc.local:9000"


# -- start / stop ------------------------------------------------------------


async def test_start_registers_the_service(fake_zeroconf: list[FakeZeroconf]) -> None:
    advertiser = make_advertiser()
    assert await advertiser.start() is True
    assert len(fake_zeroconf[0].registered) == 1
    assert advertiser.registered_name == f"LANShare PC.{SERVICE_TYPE}"


async def test_stop_withdraws_and_closes(fake_zeroconf: list[FakeZeroconf]) -> None:
    advertiser = make_advertiser()
    await advertiser.start()
    await advertiser.stop()

    fake = fake_zeroconf[0]
    assert len(fake.unregistered) == 1
    assert fake.closed is True


async def test_stop_without_start_is_harmless() -> None:
    await make_advertiser().stop()


async def test_stop_twice_is_harmless(fake_zeroconf: list[FakeZeroconf]) -> None:
    advertiser = make_advertiser()
    await advertiser.start()
    await advertiser.stop()
    await advertiser.stop()
    assert len(fake_zeroconf[0].unregistered) == 1


# -- failure is never fatal --------------------------------------------------


async def test_blocked_multicast_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A firewall or VPN blocking 5353 must not take the server down."""
    created = failing_zeroconf(monkeypatch, OSError("multicast blocked"))
    advertiser = make_advertiser()

    assert await advertiser.start() is False
    assert advertiser.registered_name is None
    assert created[0].closed is True, "a failed start must not leak the socket"


async def test_name_collision_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second LANShare on the same network is a warning, not a crash."""
    created = failing_zeroconf(monkeypatch, NonUniqueNameException())
    assert await make_advertiser().start() is False
    assert created[0].closed is True


async def test_start_allows_zeroconf_to_rename_on_conflict(
    fake_zeroconf: list[FakeZeroconf], monkeypatch: pytest.MonkeyPatch
) -> None:
    """allow_name_change lets a second instance pick another name instead of
    failing outright."""
    seen: dict[str, Any] = {}

    async def capture(self: Any, info: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        self.registered.append(info)

    monkeypatch.setattr(FakeZeroconf, "async_register_service", capture)
    await make_advertiser().start()
    assert seen.get("allow_name_change") is True


# -- wiring into the app -----------------------------------------------------


async def test_server_info_reports_no_mdns_url_when_disabled(client: Any, sender: str) -> None:
    """Tests run with advertising off, so the field must be null - not missing,
    and not a URL that nothing is answering."""
    body = (await client.get("/api/server-info", headers=headers(sender))).json()
    assert "mdns_url" in body
    assert body["mdns_url"] is None
