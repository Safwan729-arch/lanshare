"""Run the JavaScript upload tests, if node is available.

The upload client is the one place where a pure-Python suite is blind. Two bugs
in a row proved it: a 47 MiB transfer that froze at 8% with every server-side
test passing, and then the read failure underneath it, which never produced a
single byte for the server to observe.

`tests/js/` holds harnesses with a fake XMLHttpRequest; this module is the hook
that lets `pytest` run them. Node is not a project dependency and nothing is
installed - the harnesses use only the standard library, and these tests skip
when node is absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = Path(__file__).parent / "js"
STALL_HARNESS = JS / "upload_stall_harness.mjs"
FALLBACK_HARNESS = JS / "upload_fallback_harness.mjs"
TIMEOUT_HARNESS = JS / "request_timeout_harness.mjs"
API_JS = ROOT / "frontend" / "js" / "api.js"
UPLOAD_JS = ROOT / "frontend" / "js" / "upload.js"

node = shutil.which("node")
requires_node = pytest.mark.skipif(node is None, reason="node is not installed")


@pytest.fixture
def frontend_as_modules(tmp_path: Path) -> Path:
    """The shipped frontend, copied somewhere node will treat it as ESM.

    The copy keeps the original filenames so `upload.js`'s `import './api.js'`
    still resolves; the `package.json` is what makes node read plain `.js` as
    modules. Putting that file in `frontend/` instead would turn the frontend
    into a package, which the no-build-step rule rules out.
    """
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    for source in (API_JS, UPLOAD_JS):
        (tmp_path / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def run_harness(harness: Path, module: Path) -> None:
    assert node is not None
    result = subprocess.run(
        [node, str(harness), module.as_uri()],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"\n{result.stdout}\n{result.stderr}"


@requires_node
def test_the_upload_client_survives_a_silent_connection(frontend_as_modules: Path) -> None:
    """A hung request must reject, stay retryable, and not look like a cancel."""
    run_harness(STALL_HARNESS, frontend_as_modules / API_JS.name)


@requires_node
def test_a_file_that_stops_yielding_slices_still_uploads(frontend_as_modules: Path) -> None:
    """The iPhone failure: chunk 0 lands, every later slice is unreadable, and
    the server never sees a byte of chunk 1. The recovery is to read the file
    once rather than slicing it again and again."""
    run_harness(FALLBACK_HARNESS, frontend_as_modules / UPLOAD_JS.name)


@requires_node
def test_a_call_to_a_dead_connection_fails_instead_of_hanging(frontend_as_modules: Path) -> None:
    """The server closes an idle keep-alive connection after a few seconds. The
    browser reuses it anyway and will not retry the POST, so without a timeout
    the upload sits on "Starting..." forever and the queue stalls behind it."""
    run_harness(TIMEOUT_HARNESS, frontend_as_modules / API_JS.name)
