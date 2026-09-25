"""QR codes for the LAN URL (Phase 2).

Discovery is a separate subsystem from transfer: nothing here knows about
transfers, and the transfer code never imports this module.

A phone's browser cannot find the server by itself, so pairing is: the PC shows
a QR of its LAN URL, the phone's camera opens it.
"""

from __future__ import annotations

import io
import logging

import segno

logger = logging.getLogger(__name__)

# Medium error correction: survives a little glare on a phone screen without
# pushing the code to a larger, denser version.
ERROR_LEVEL = "m"

# Explicit background colours, not reverse-video. segno's own terminal output
# uses \x1b[7m, which inherits the terminal's theme - on a dark-themed terminal
# that renders the code inverted, and phone cameras are unreliable at reading an
# inverted QR. Setting black and white outright makes the render theme-proof.
_BG_DARK = "\x1b[40m"
_BG_LIGHT = "\x1b[47m"
_RESET = "\x1b[0m"

# Two characters per module, so a code is square in a terminal's tall cells.
_MODULE_WIDTH = 2
ASCII_DARK = "##"
ASCII_LIGHT = "  "


def _code(data: str) -> segno.QRCode:
    return segno.make(data, error=ERROR_LEVEL)


def qr_svg(data: str, *, scale: int = 4, border: int = 2) -> str:
    """SVG markup for the web page.

    Returned as markup rather than a file so `/api/server-info` can carry it in
    one response, per the REST contract.

    The namespace (``svgns=True``) is not optional. The page loads this through
    an ``<img>`` data URI, and a browser parses that as a *standalone* SVG
    document, which must declare its namespace or it renders nothing at all.
    segno's ``svg_inline()`` omits it deliberately - that output is only valid
    when pasted straight into an HTML document, where the parser infers the
    namespace.
    """
    buffer = io.BytesIO()
    _code(data).save(
        buffer,
        kind="svg",
        scale=scale,
        border=border,
        svgns=True,
        xmldecl=False,  # no <?xml?> prolog; the data URI does not need one
        dark="#16191d",
        light="#ffffff",
    )
    return buffer.getvalue().decode("utf-8")


def _ansi_row(row: list[int]) -> str:
    """One terminal row, emitting a colour code only when the run changes."""
    parts: list[str] = []
    current: str | None = None
    for module in row:
        colour = _BG_DARK if module else _BG_LIGHT
        if colour != current:
            parts.append(colour)
            current = colour
        parts.append(" " * _MODULE_WIDTH)
    parts.append(_RESET)
    return "".join(parts)


def qr_terminal(data: str, *, border: int = 2, ascii_only: bool = False) -> str:
    """Terminal art for the startup banner.

    Rendered with spaces on coloured backgrounds rather than Unicode half-
    blocks: U+2580 and friends crash on a cp1252 Windows console, which is
    still the default encoding for python.exe on Windows.

    ``ascii_only`` drops the colour for logs, redirected output and tests. It is
    always visible, but its dark/light reading depends on the terminal theme, so
    the coloured version is preferred whenever the output is a real terminal.
    """
    rows = _code(data).matrix_iter(border=border)

    if ascii_only:
        return "\n".join(
            "".join(ASCII_DARK if module else ASCII_LIGHT for module in row) for row in rows
        )

    return "\n".join(_ansi_row(list(row)) for row in rows)
