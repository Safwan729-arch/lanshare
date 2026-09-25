"""QR generation for pairing (Phase 2)."""

from __future__ import annotations

from urllib.parse import quote, unquote
from xml.etree import ElementTree

import pytest
from conftest import headers
from httpx import AsyncClient
from lanshare.discovery.qr import qr_svg, qr_terminal

URL = "http://192.168.1.20:8080"


def test_svg_is_embeddable_markup() -> None:
    markup = qr_svg(URL)
    assert markup.startswith("<svg")
    assert markup.rstrip().endswith("</svg>")
    # No prolog needed inside a data URI.
    assert "<?xml" not in markup
    assert "<!DOCTYPE" not in markup


def test_svg_declares_its_namespace() -> None:
    """Without this the page shows a broken image.

    The QR is loaded through an <img> data URI, which a browser parses as a
    standalone SVG document. A standalone SVG with no xmlns renders nothing.
    segno's svg_inline() omits the namespace on purpose - that form only works
    pasted directly into HTML - so we must not use it here.
    """
    assert 'xmlns="http://www.w3.org/2000/svg"' in qr_svg(URL)


def test_svg_parses_as_standalone_xml() -> None:
    """A browser will parse the data URI as XML, so it has to be well formed."""
    root = ElementTree.fromstring(qr_svg(URL))
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert int(root.attrib["width"]) > 0
    assert int(root.attrib["height"]) > 0


def test_svg_survives_percent_encoding_into_a_data_uri() -> None:
    """The markup contains '#' in its colours, which would end the URI early."""
    markup = qr_svg(URL)
    assert "#" in markup

    encoded = quote(markup, safe="")
    assert "#" not in encoded

    assert unquote(encoded) == markup
    ElementTree.fromstring(unquote(encoded))


def test_svg_scale_changes_size() -> None:
    small = qr_svg(URL, scale=2)
    large = qr_svg(URL, scale=8)
    assert 'transform="scale(2)"' in small
    assert 'transform="scale(8)"' in large


def test_terminal_art_is_encodable_on_a_windows_console() -> None:
    """The banner must not crash on a cp1252 console.

    Unicode half-blocks (U+2580 and friends) cannot be encoded in cp1252, which
    is still the default for python.exe on Windows. Our render must survive it.
    """
    qr_terminal(URL).encode("cp1252")


def test_terminal_art_sets_explicit_colours() -> None:
    """Not reverse-video.

    \x1b[7m inherits the terminal theme, so on a dark-themed terminal the code
    comes out inverted and many phone cameras will not read it. Dark and light
    modules must be stated outright.
    """
    art = qr_terminal(URL)
    assert "\x1b[40m" in art, "dark modules should set a black background"
    assert "\x1b[47m" in art, "light modules should set a white background"
    assert "\x1b[7m" not in art, "reverse-video is theme-dependent"


def test_terminal_rows_are_reset_so_colour_does_not_bleed() -> None:
    for line in qr_terminal(URL).splitlines():
        assert line.endswith("\x1b[0m")


def test_ascii_art_is_pure_ascii() -> None:
    art = qr_terminal(URL, ascii_only=True)
    art.encode("ascii")
    assert set(art) <= {"#", " ", "\n"}


def test_ascii_art_is_square() -> None:
    """Two characters per module, so it is not squashed horizontally."""
    lines = qr_terminal(URL, ascii_only=True).splitlines()
    assert len({len(line) for line in lines}) == 1
    assert len(lines[0]) == len(lines) * 2


def test_border_widens_the_code() -> None:
    narrow = qr_terminal(URL, border=1, ascii_only=True).splitlines()
    wide = qr_terminal(URL, border=4, ascii_only=True).splitlines()
    assert len(wide) == len(narrow) + 6  # 3 extra modules per side


FINDER = [
    "#######",
    "#     #",
    "# ### #",
    "# ### #",
    "# ### #",
    "#     #",
    "#######",
]


def _grid(data: str) -> list[str]:
    """ASCII art back down to one character per module."""
    lines = qr_terminal(data, border=0, ascii_only=True).splitlines()
    return ["".join(line[i] for i in range(0, len(line), 2)) for line in lines]


def _corner(grid: list[str], top: int, left: int) -> list[str]:
    return [row[left : left + 7] for row in grid[top : top + 7]]


def test_finder_patterns_are_in_the_right_corners() -> None:
    """Catches a transposed or mirrored render.

    A QR has finder squares at three corners and none at the fourth. If the
    matrix were flipped or rotated this still "looks like" a QR but will not
    scan, and no other test here would notice.
    """
    grid = _grid(URL)
    size = len(grid)

    assert _corner(grid, 0, 0) == FINDER, "top-left"
    assert _corner(grid, 0, size - 7) == FINDER, "top-right"
    assert _corner(grid, size - 7, 0) == FINDER, "bottom-left"
    assert _corner(grid, size - 7, size - 7) != FINDER, "bottom-right must be empty"


def test_grid_is_square_and_a_valid_qr_size() -> None:
    grid = _grid(URL)
    assert all(len(row) == len(grid) for row in grid)
    # QR versions are 21, 25, 29, ... modules across.
    assert (len(grid) - 21) % 4 == 0


@pytest.mark.parametrize("data", ["http://10.0.0.5:8080", "http://192.168.1.20:9000"])
def test_different_urls_give_different_codes(data: str) -> None:
    assert qr_terminal(data, ascii_only=True) != qr_terminal(URL, ascii_only=True)


def test_same_url_is_deterministic() -> None:
    assert qr_svg(URL) == qr_svg(URL)


async def test_server_info_carries_a_qr(client: AsyncClient, sender: str) -> None:
    response = await client.get("/api/server-info", headers=headers(sender))
    assert response.status_code == 200
    body = response.json()
    assert body["qr_svg"].startswith("<svg")


async def test_server_info_qr_matches_the_lan_url(client: AsyncClient, sender: str) -> None:
    """The QR must encode the address it is displayed next to."""
    body = (await client.get("/api/server-info", headers=headers(sender))).json()
    assert body["qr_svg"] == qr_svg(body["lan_url"])
