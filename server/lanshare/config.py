"""Application settings.

Values come from environment variables prefixed with ``LANSHARE_`` or from a
``.env`` file at the repo root. See ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py lives at <root>/server/lanshare/config.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]

MIB = 1024 * 1024
GIB = 1024 * MIB

#: Largest chunk the server will ever ask a client to send. Chunks are streamed
#: to disk, so this is not a memory bound - it is a resume-granularity one. A
#: dropped connection re-sends the whole chunk, and iOS Safari drops the socket
#: every time the user switches app, so oversized chunks make phones worse.
MAX_CHUNK_SIZE = 64 * MIB

#: Hard ceiling on ``max_file_size``.
#:
#: Resume state is derived by listing the chunk directory, which is O(chunks)
#: and runs once per uploaded chunk - so scanning cost grows with the *square*
#: of the chunk count. ``chunk_size_for`` keeps the count bounded by scaling the
#: chunk size up with the file, but chunks stop at ``MAX_CHUNK_SIZE``, and
#: 64 MiB x 4096 chunks is exactly 256 GiB. Past that the count climbs again and
#: the scan starts to dominate the transfer.
#:
#: Raising this is a code change, not a config one: make resume state cheap
#: first (cache the received set per transfer, rebuilt from disk on restart).
MAX_SUPPORTED_FILE_SIZE = 256 * GIB


class Settings(BaseSettings):
    """Runtime configuration for the server."""

    model_config = SettingsConfigDict(
        env_prefix="LANSHARE_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    server_name: str = "LANShare PC"

    #: Extra hostnames this server may be reached under.
    #:
    #: Addresses, single-label machine names and reserved local suffixes
    #: (.local, .lan, ...) are always accepted. A real registrable domain is
    #: not, because that is what a DNS rebinding attack needs - so if you front
    #: this with one, name it here. Comma-separated.
    allowed_hosts: list[str] = Field(default_factory=list)

    incoming_dir: Path = Path("storage/incoming")
    temporary_dir: Path = Path("storage/temporary")
    data_dir: Path = Path("data")

    #: The *smallest* chunk the server will use. Large files get a bigger one,
    #: chosen per transfer by ``chunk_size_for``; small files are unaffected.
    chunk_size: int = Field(default=4 * MIB, ge=64 * 1024, le=MAX_CHUNK_SIZE)

    #: How long an idle connection is kept open for reuse.
    #:
    #: Uvicorn's own default is 5 seconds, which is shorter than it takes to pick
    #: a video out of a phone's gallery. The browser pools the connection, the
    #: server closes it, and the `POST` that starts the next transfer goes out on
    #: a dead socket. A browser will not retry a POST - it is not safe to repeat -
    #: so the upload hangs with no error. Proven on the wire: a POST sent 1s after
    #: the previous request succeeds, the same POST at 7s is refused.
    #:
    #: This only costs an idle socket per device on a LAN with a handful of them.
    keep_alive_timeout: int = Field(default=120, ge=5, le=3600)

    #: Per-file ceiling. Note the disk cost: assembly needs roughly twice the
    #: file size free, or three times when ``incoming_dir`` and
    #: ``temporary_dir`` are on different drives.
    max_file_size: int = Field(default=256 * GIB, gt=0, le=MAX_SUPPORTED_FILE_SIZE)

    # A device is "online" only while it holds a WebSocket connection, but we
    # keep rows around this long so history can still name the device.
    device_retention_days: int = 30

    #: How long an unfinished transfer keeps its chunks.
    #:
    #: Resume lives in the page: the browser holds the transfer id in memory
    #: and never writes it down, so a closed tab can never pick an upload back
    #: up - it starts a new transfer instead. The chunks of the old one would
    #: otherwise sit on disk for ever. The window exists only so that
    #: restarting the server under an open page does not throw away an upload
    #: that page can still finish.
    stale_transfer_hours: int = Field(default=24, ge=1)

    #: How often the chunk sweep runs while the server is up, in hours.
    #:
    #: The sweep used to run only at startup, which is fine for a PC that gets
    #: shut down and useless for one that does not. Set to 0 to run it only at
    #: startup.
    sweep_interval_hours: float = Field(default=6.0, ge=0)

    #: Must pairing decisions come from the host machine?
    #:
    #: Approving a device grants it everything, so the decision belongs at the
    #: host's own keyboard - which is where the README has always sent people.
    #: With this off, any trusted device may approve another, and one approval
    #: can quietly beget the next.
    approval_from_host_only: bool = True

    #: How many devices may sit unapproved at once.
    #:
    #: Registration has to be open - a device needs an identity before anyone
    #: can approve it - so without a cap anyone on the LAN can insert rows
    #: forever and bury the real device in the approval list. Loopback is
    #: exempt, so the host can never be locked out by a flood.
    max_pending_devices: int = Field(default=20, ge=1)

    # mDNS: advertise the server as <mdns_hostname>.local on the LAN.
    enable_mdns: bool = True
    mdns_hostname: str = "lanshare"

    # UDP broadcast, for finding *other LANShare servers* on the LAN.
    # Browsers cannot take part in this; phones use the QR or the mDNS name.
    enable_udp_discovery: bool = True
    discovery_port: int = 8079

    # HTTPS is opt-in and off by default. A self-signed certificate makes every
    # browser show a warning, and iOS needs the CA installed and trusted, so
    # turning it on by default would make first-run considerably worse. Enable
    # it when you want a secure context (crypto.subtle, the Clipboard API).
    # Certificates are supplied, not generated: generating them would mean a
    # new dependency. See README for the openssl command.
    enable_https: bool = False
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None
    announce_interval_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    # Comfortably more than three announce intervals, so one dropped broadcast
    # never makes a healthy peer flicker out of the list.
    peer_ttl_seconds: float = Field(default=35.0, ge=5.0, le=600.0)

    frontend_dir: Path = Path("frontend")

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        """Accept `a.example.com, b.example.com` as well as a JSON list."""
        if isinstance(value, str):
            return [entry.strip() for entry in value.split(",") if entry.strip()]
        return value

    @field_validator("incoming_dir", "temporary_dir", "data_dir", "frontend_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        """Resolve relative paths against the repo root, not the cwd."""
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "lanshare.db"

    def ensure_directories(self) -> None:
        """Create the directories the server writes to."""
        for path in (self.incoming_dir, self.temporary_dir, self.data_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
