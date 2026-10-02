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

import contextlib
import importlib.util
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
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


def test_the_installer_writes_into_the_project_by_default() -> None:
    """Writing outside the repo is opt-in: the icon belongs to the project."""
    installer = read(INSTALLER)
    default = next(ln for ln in installer.splitlines() if "$locations = @(" in ln)
    assert default.strip() == "$locations = @($root)"
    for switch in ("if ($Desktop)", "if ($StartMenu)"):
        assert switch in installer, f"{switch} must guard a write outside the repo"


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


# --- Stopping -------------------------------------------------------------
#
# Starting was one click and stopping was "find the console window", so there is
# a second shortcut. These cover the part that could do damage: it must stop
# this project's server and nothing else that happens to hold the port.

STOPPER = TOOLS / "lanshare-stop.ps1"
STOP_SHIM = TOOLS / "LANShare-Stop.cmd"


def test_every_stopper_file_is_present() -> None:
    for path in (STOPPER, STOP_SHIM):
        assert path.is_file(), f"missing {path.name}"


def test_the_stopper_scopes_what_it_stops_to_this_project() -> None:
    """Killing by name or by whoever holds the port would hit innocent processes."""
    code = code_lines(read(STOPPER))
    assert r".venv\Scripts\python.exe" in code, "must match this project's interpreter"
    assert "CommandLine" in code, "the match has to read the command line"
    assert "taskkill" not in code.lower()
    assert "-Name" not in code, "Stop-Process -Name python would stop unrelated work"


def test_the_stopper_resolves_the_port_the_way_the_server_does() -> None:
    code = code_lines(read(STOPPER))
    assert "LANSHARE_PORT" in code
    assert "'.env'" in code
    assert "8080" in code


def test_the_stop_shim_points_at_the_stopper_that_exists() -> None:
    shim = read(STOP_SHIM)
    assert STOPPER.name in shim
    assert "-ExecutionPolicy Bypass" in shim
    assert "pause" in shim, "a refusal must not close before it is read"


def test_the_stop_shim_uses_windows_line_endings() -> None:
    raw = STOP_SHIM.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_the_installer_creates_both_shortcuts() -> None:
    installer = read(INSTALLER)
    for name in (SHIM.name, STOP_SHIM.name, "LANShare.lnk", "LANShare Stop.lnk"):
        assert name in installer, f"the installer never mentions {name}"


def test_the_launcher_titles_the_server_window() -> None:
    """The window is the only way to stop it by hand; it has to be findable."""
    assert "LANShare server" in code_lines(read(LAUNCHER))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def run_stopper(port: int) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "LANSHARE_PORT": str(port)}
    return subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-File", str(STOPPER)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.mark.skipif(powershell is None, reason="powershell is not installed")
def test_stopping_when_nothing_is_running_is_not_an_error() -> None:
    """Double-clicking Stop twice is normal and must not look like a failure."""
    result = run_stopper(free_port())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not running" in result.stdout.lower()
    assert "stopping" not in result.stdout.lower(), "it stopped something on another port"


@pytest.mark.skipif(powershell is None, reason="powershell is not installed")
def test_the_stopper_leaves_another_program_on_the_port_alone() -> None:
    """The port is the clue, not the warrant: only a LANShare process is stopped."""
    port = free_port()
    listener = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket,sys,time\n"
            "s=socket.socket()\n"
            f"s.bind(('127.0.0.1',{port}))\n"
            "s.listen(1)\n"
            "print('up',flush=True)\n"
            "time.sleep(120)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert listener.stdout is not None
        assert listener.stdout.readline().strip() == "up"

        result = run_stopper(port)

        assert listener.poll() is None, "it stopped a program that was not LANShare"
        assert result.returncode != 0, "refusing to stop must not report success"
        assert "not LANShare" in result.stdout, "the refusal has to say what it found"
        assert re.search(r"pid \d+", result.stdout), "the refusal has to name the pid"
    finally:
        listener.kill()
        listener.wait(timeout=30)


VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as answer:
            return bool(answer.status == 200)
    except Exception:
        return False


@contextlib.contextmanager
def real_server(port: int, tmp_path: Path, like_the_launcher: bool = False):
    """A real `python -m lanshare`, on its own port and its own directories.

    The stopper reads the live process table, so nothing less than a real
    process exercises it. Discovery is off: this server is a stop target, not a
    LAN citizen, and it should not advertise itself to the user's phone.

    `like_the_launcher` reproduces how the shortcut starts it - through cmd, to
    title the window - because that command line is not the obvious one: cmd
    quotes the interpreter and leaves *two* spaces before `-m`.
    """
    env = {
        **os.environ,
        "LANSHARE_PORT": str(port),
        "LANSHARE_DATA_DIR": str(tmp_path / "data"),
        "LANSHARE_INCOMING_DIR": str(tmp_path / "incoming"),
        "LANSHARE_TEMPORARY_DIR": str(tmp_path / "temporary"),
        "LANSHARE_ENABLE_MDNS": "false",
        "LANSHARE_ENABLE_UDP_DISCOVERY": "false",
    }
    command: str | list[str] = [str(VENV_PYTHON), "-m", "lanshare"]
    if like_the_launcher:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        # A string, not a list: the doubled space has to survive into the real
        # command line rather than be normalised away by list quoting.
        command = f'{comspec} /c title LANShare server & "{VENV_PYTHON}"  -m lanshare'
    process = subprocess.Popen(command, cwd=str(ROOT), env=env)
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if health(port):
                break
            assert process.poll() is None, "the server exited before it answered"
            time.sleep(0.3)
        else:
            raise AssertionError(f"the server never answered on port {port}")
        yield process
    finally:
        if process.poll() is None:
            # Through cmd there is a child to take with it; kill() would orphan
            # the server on the port and the next test would inherit it.
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
            )
        process.wait(timeout=30)


@pytest.mark.skipif(powershell is None, reason="powershell is not installed")
@pytest.mark.skipif(
    not VENV_PYTHON.is_file() or Path(sys.executable).resolve() != VENV_PYTHON.resolve(),
    reason="needs pytest running on the project venv's interpreter",
)
def test_the_stopper_ignores_a_server_on_a_different_port(tmp_path: Path) -> None:
    """Matching only the command line would stop the wrong LANShare."""
    port = free_port()
    with real_server(port, tmp_path):
        result = run_stopper(free_port())

        assert result.returncode == 0, result.stdout + result.stderr
        assert "not running" in result.stdout.lower()
        assert health(port), "it stopped a server it was not pointed at"


@pytest.mark.skipif(powershell is None, reason="powershell is not installed")
@pytest.mark.skipif(
    not VENV_PYTHON.is_file() or Path(sys.executable).resolve() != VENV_PYTHON.resolve(),
    reason="needs pytest running on the project venv's interpreter",
)
def test_the_stopper_stops_the_server_on_its_port(tmp_path: Path) -> None:
    port = free_port()
    with real_server(port, tmp_path) as process:
        result = run_stopper(port)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "stopped" in result.stdout.lower()
        assert not health(port), "the server still answers"
        # The venv's python.exe is a stub holding the real interpreter; both go.
        assert process.wait(timeout=30) is not None


@pytest.mark.skipif(powershell is None, reason="powershell is not installed")
@pytest.mark.skipif(
    not VENV_PYTHON.is_file() or Path(sys.executable).resolve() != VENV_PYTHON.resolve(),
    reason="needs pytest running on the project venv's interpreter",
)
def test_the_stopper_stops_a_server_started_by_the_shortcut(tmp_path: Path) -> None:
    """The shape that matters: the one the Start icon actually produces."""
    port = free_port()
    with real_server(port, tmp_path, like_the_launcher=True):
        result = run_stopper(port)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "not LANShare" not in result.stdout, "it did not recognise its own server"
        assert not health(port), "the server still answers"


# --- A port is a number ---------------------------------------------------
#
# Only `.env` was validated. An unvalidated value reaches a URL as
# "http://127.0.0.1:<value>/api/health", and a value like `8080@elsewhere`
# makes `elsewhere` the host and the loopback address the userinfo - so the
# probe leaves the machine, and a 200 from it would be read as "already
# running" and opened in the browser. `.invalid` never resolves (RFC 2606),
# so these tests cannot reach anything even if the check is removed.

BAD_PORTS = ["8080@localhost.invalid", "80 80", "0", "99999", "-1", "8080/x"]
# Never the empty string: that is an *unset* port, which falls back to the real
# 8080 - and a test that runs either script against the real port starts or
# stops the user's own server. The fallback chain is covered statically.


def run_script(script: Path, port: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "LANSHARE_PORT": port}
    return subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


@pytest.mark.skipif(powershell is None, reason="powershell is not installed")
@pytest.mark.parametrize("script", [LAUNCHER, STOPPER], ids=lambda p: p.name)
@pytest.mark.parametrize("port", BAD_PORTS)
def test_a_port_that_is_not_a_number_is_refused(script: Path, port: str) -> None:
    result = run_script(script, port)

    assert result.returncode != 0, f"{port!r} was accepted:\n{result.stdout}"
    assert "LANSHARE_PORT" in result.stdout, "the refusal has to name the variable"
    # Refused before anything is probed, started or stopped.
    for verb in ("starting the server", "opening", "stopping pid"):
        assert verb not in result.stdout.lower()


def test_the_scripts_do_not_take_the_shell_from_the_environment() -> None:
    """ComSpec is writable; whoever sets it would choose what the launcher runs."""
    code = code_lines(read(LAUNCHER))
    assert "ComSpec" not in code
    assert "SystemDirectory" in code, "take cmd.exe from the OS, not from an env var"
