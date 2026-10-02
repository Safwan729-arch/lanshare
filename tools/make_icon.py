"""Draws ``tools/lanshare.ico``, the icon on the desktop shortcut.

Checked in as a script, not just as the .ico it produces, so the icon can be
changed by editing numbers rather than by opening an image editor. Pure standard
library: an icon is decoration and must not cost the project a dependency.

Run it from the repo root after editing:

    .venv/Scripts/python.exe tools/make_icon.py

Format note: sizes up to 128 are written as DIBs and 256 as a PNG. That split is
what Windows has always handled; PNG-compressed small icons are accepted by
modern Explorer but fall back to a black square in some older shell surfaces.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

#: Icon sizes Windows picks between (taskbar, desktop, large thumbnails).
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Supersamples per axis. 4 means 16 coverage tests per pixel, which is enough
#: to keep the 16px arrow from looking chewed.
SAMPLES = 4

#: The badge gradient is the app's own accent (``--accent`` in styles.css) run
#: slightly light-to-dark so the icon reads as a solid object, not a flat tile.
TOP = (0x5B, 0x93, 0xFF)
BOTTOM = (0x2A, 0x62, 0xDC)
GLYPH = (0xFF, 0xFF, 0xFF)

INSET = 0.045
RADIUS = 0.235

#: An upward arrow: this app's single verb is "send".
SHAFT = (0.435, 0.565, 0.420, 0.790)  # left, right, top, bottom
APEX = (0.500, 0.205)
BASE_Y = 0.470
BASE_X = (0.272, 0.728)


def inside_badge(x: float, y: float) -> bool:
    """A rounded square, by distance to the corner arc centres."""
    half = 0.5 - INSET
    dx, dy = abs(x - 0.5), abs(y - 0.5)
    ex, ey = dx - (half - RADIUS), dy - (half - RADIUS)
    if ex <= 0:
        return dy <= half
    if ey <= 0:
        return dx <= half
    return ex * ex + ey * ey <= RADIUS * RADIUS


def inside_glyph(x: float, y: float) -> bool:
    left, right, top, bottom = SHAFT
    if left <= x <= right and top <= y <= bottom:
        return True
    # The head: a triangle, tested by which side of each edge the point falls.
    ax, ay = APEX
    bx, by = BASE_X[0], BASE_Y
    cx, cy = BASE_X[1], BASE_Y
    d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
    d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
    d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
    return (d1 >= 0 and d2 >= 0 and d3 >= 0) or (d1 <= 0 and d2 <= 0 and d3 <= 0)


def render(size: int) -> bytes:
    """One icon as straight-alpha RGBA rows, top row first."""
    rows = bytearray()
    step = 1.0 / (size * SAMPLES)
    total = SAMPLES * SAMPLES
    for py in range(size):
        for px in range(size):
            badge = glyph = 0
            for sy in range(SAMPLES):
                y = (py * SAMPLES + sy + 0.5) * step
                for sx in range(SAMPLES):
                    x = (px * SAMPLES + sx + 0.5) * step
                    if not inside_badge(x, y):
                        continue
                    badge += 1
                    if inside_glyph(x, y):
                        glyph += 1
            if badge == 0:
                rows += b"\x00\x00\x00\x00"
                continue
            t = (py + 0.5) / size
            mix = glyph / badge
            channels = []
            for top, bottom, white in zip(TOP, BOTTOM, GLYPH, strict=True):
                base = top + (bottom - top) * t
                channels.append(round(base + (white - base) * mix))
            rows += bytes(channels) + bytes((round(255 * badge / total),))
    return bytes(rows)


def as_png(rgba: bytes, size: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    stride = size * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def as_dib(rgba: bytes, size: int) -> bytes:
    """A BITMAPINFOHEADER icon: BGRA, bottom-up, with the legacy AND mask.

    The mask is all zeros because the alpha channel decides transparency, but it
    cannot be omitted - a 32bpp icon without one renders as a black box.
    """
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    stride = size * 4
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        row = rgba[y * stride : (y + 1) * stride]
        for i in range(0, stride, 4):
            r, g, b, a = row[i : i + 4]
            pixels += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    return bytes(header) + bytes(pixels) + bytes(mask_stride * size)


def build() -> bytes:
    images = []
    for size in SIZES:
        rgba = render(size)
        images.append(as_png(rgba, size) if size >= 256 else as_dib(rgba, size))
    offset = 6 + 16 * len(images)
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for size, image in zip(SIZES, images, strict=True):
        # 256 is stored as 0: the width field is a single byte.
        dimension = 0 if size >= 256 else size
        out += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    for image in images:
        out += image
    return bytes(out)


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "lanshare.ico"
    target.write_bytes(build())
    print(f"wrote {target} ({target.stat().st_size:,} bytes, {len(SIZES)} sizes)")
