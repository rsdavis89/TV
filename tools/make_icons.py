#!/usr/bin/env python3
"""Generate the PWA icons without pulling in an image library.

Draws a rounded 'screen' with a play triangle, supersampled 4x for smooth
edges, and writes plain RGBA PNGs.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "web" / "icons"

BG = (47, 102, 232)
FG = (255, 255, 255)
SS = 4  # supersampling factor


def rounded_rect(x: float, y: float, w: float, h: float, r: float, px: float, py: float) -> bool:
    if not (x <= px <= x + w and y <= py <= y + h):
        return False
    # Clamp to the rectangle's corner-centre box; the distance to that point is
    # only meaningful inside a corner, and is zero everywhere else.
    cx = min(max(px, x + r), x + w - r)
    cy = min(max(py, y + r), y + h - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def in_triangle(size: float, px: float, py: float) -> bool:
    # Rightward-pointing play glyph centred in the screen area.
    left = size * 0.415
    right = size * 0.635
    top = size * 0.375
    bottom = size * 0.625
    if not (left <= px <= right and top <= py <= bottom):
        return False
    progress = (px - left) / (right - left)
    half = (1 - progress) * (bottom - top) / 2
    return abs(py - size / 2) <= half


def render(size: int) -> bytes:
    rows = []
    screen = (size * 0.16, size * 0.22, size * 0.68, size * 0.5, size * 0.07)
    stroke = max(size * 0.045, 2.0)
    inner = (
        screen[0] + stroke,
        screen[1] + stroke,
        screen[2] - 2 * stroke,
        screen[3] - 2 * stroke,
        max(screen[4] - stroke, 1.0),
    )
    stand_w, stand_h = size * 0.30, size * 0.045
    stand_x, stand_y = (size - stand_w) / 2, size * 0.76

    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = 0
            for sy in range(SS):
                for sx in range(SS):
                    px = x + (sx + 0.5) / SS
                    py = y + (sy + 0.5) / SS
                    on_screen = rounded_rect(*screen, px, py)
                    in_inner = rounded_rect(*inner, px, py)
                    on_stand = rounded_rect(stand_x, stand_y, stand_w, stand_h, stand_h / 2, px, py)
                    neck = abs(px - size / 2) <= size * 0.022 and screen[1] + screen[3] <= py <= stand_y
                    lit = (on_screen and not in_inner) or on_stand or neck or in_triangle(size, px, py)
                    color = FG if lit else BG
                    r += color[0]
                    g += color[1]
                    b += color[2]
            samples = SS * SS
            row += bytes((r // samples, g // samples, b // samples, 255))
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + row for row in rows)
    return png(size, size, raw)


def png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" fill="#2f66e8"/>
  <rect x="82" y="113" width="348" height="256" rx="36" fill="none" stroke="#fff" stroke-width="24"/>
  <path d="M213 192 L325 256 L213 320 Z" fill="#fff"/>
  <rect x="250" y="369" width="12" height="30" fill="#fff"/>
  <rect x="179" y="389" width="154" height="23" rx="11" fill="#fff"/>
</svg>
"""


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon.svg").write_text(SVG)
    for size in (192, 512):
        (OUT / f"icon-{size}.png").write_bytes(render(size))
        print(f"wrote icon-{size}.png")


if __name__ == "__main__":
    main()
