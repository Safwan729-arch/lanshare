"""Static checks on the desktop launcher.

There is no way to double-click a shortcut from pytest, so these cover the two
things that would silently rot instead: a launcher that drifts out of step with
the files it points at, and the one line that matters for correctness - it must
start the server through `python -m lanshare`, because a bare `uvicorn` command
gets a 5 second keep-alive and reintroduces ADR-0011's "stuck on Starting..."
bug on every launch.

The icon is compared against its generator, so editing one without rerunning the
other fails here rather than shipping a stale picture.
"""

from __future__ import annotations

import importlib.util
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
LAUNCHER = TOOLS / "lanshare-launcher.ps1"
SHIM = TOOLS / "LANShare.cmd"
INSTALLER = TOOLS / "install-shortcut.ps1"
ICON = TOOLS / "lanshare.ico"

powershell = shutil.which("powershell")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_icon_generator():
    spec = importlib.util.spec_from_file_location("make_icon", TOOLS / "make_icon.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_launcher_file_is_present() -> None:
    for path in (LAUNCHER, SHIM, INSTALLER, ICON, TOOLS / "make_icon.py"):
        assert path.is_file(), f"missing {path.name}"


def code_lines(script: str) -> str:
    """The script with comments dropped, so prose about a command is not a call."""
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def test_the_launcher_starts_the_server_through_the_module() -> None:
    """ADR-0011: only `python -m lanshare` applies `keep_alive_timeout`."""
    script = read(LAUNCHER)
    assert "'-m', 'lanshare'" in script
    assert "uvicorn" not in code_lines(script).lower()


def test_the_launcher_resolves_the_port_the_way_the_server_does() -> None:
    script = read(LAUNCHER)
    assert "LANSHARE_PORT" in script
    assert "'.env'" in script
    assert "8080" in script


def test_the_page_is_opened_only_after_the_server_answers() -> None:
    """Opening the page first races the port bind and shows a browser error."""
    script = read(LAUNCHER)
    assert "/api/health" in script
    first_open = script.index("Start-Process $page")
    assert script.index("Test-Health") < first_open


def test_the_launcher_reports_a_missing_virtualenv() -> None:
    """The likeliest failure on a fresh clone, and the one a console flash hides."""
    script = read(LAUNCHER)
    assert "py -3 -m venv .venv" in script


def test_the_shim_points_at_the_launcher_that_exists() -> None:
    shim = read(SHIM)
    assert LAUNCHER.name in shim
    assert "-ExecutionPolicy Bypass" in shim
    assert "pause" in shim, "a failed launch must not close before it is read"


def test_the_shim_uses_windows_line_endings() -> None:
    """cmd.exe parses a label or a continued line wrongly without CRLF."""
    raw = SHIM.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_the_installer_points_at_files_that_exist() -> None:
    installer = read(INSTALLER)
    for name in (SHIM.name, ICON.name):
        assert name in installer
    assert "Desktop" in installer


def test_the_icon_matches_its_generator() -> None:
    assert ICON.read_bytes() == load_icon_generator().build()


def test_the_icon_is_a_well_formed_windows_icon() -> None:
    raw = ICON.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    assert (reserved, kind) == (0, 1)
    assert count == len(load_icon_generator().SIZES)

    seen = []
    for index in range(count):
        width, _height, _colors, _pad, _planes, bpp, length, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + 16 * index
        )
        assert bpp == 32
        assert offset + length <= len(raw), "entry points past the end of the file"
        image = raw[offset : offset + length]
        if width == 0:  # 256, which cannot fit in one byte
            assert image.startswith(b"\x89PNG\r\n\x1a\n")
        else:
            # A DIB, whose header must declare double the height for the mask.
            header_size, dib_width, dib_height = struct.unpack_from("<Iii", image, 0)
            assert header_size == 40
            assert (dib_width, dib_height) == (width, width * 2)
        seen.append(width)
    assert 0 in seen, "no 256px entry: Explorer's large view would scale up a 128"


@pytest.mark.skipif(powershell is None, reason="powershell is not installed")
@pytest.mark.parametrize("script", [LAUNCHER, INSTALLER], ids=lambda p: p.name)
def test_the_powershell_scripts_parse(script: Path) -> None:
    """A syntax error here is only visible when a person double-clicks."""
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop';"
            f"$null = [scriptblock]::Create((Get-Content -Raw -LiteralPath '{script}'))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
