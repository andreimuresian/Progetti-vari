#!/usr/bin/env python3
"""Generate Aria's icon (.ico for Windows, .png for the web page).

Pure standard library on purpose: the repo needs no image dependency, and CI
can regenerate the icon from source at any time.
"""

import os
import struct
import zlib

SS = 3  # supersampling factor, for antialiased edges
GOLD_HI = (236, 193, 100)
GOLD_LO = (190, 126, 30)
INK = (28, 21, 8)


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def rounded_rect(x, y, size, radius):
    """Inside-ness test for a rounded square filling the canvas."""
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def in_triangle(x, y, size):
    """A play triangle, optically centred (its centroid sits left of centre)."""
    left = size * 0.365
    right = size * 0.735
    half = size * 0.215
    mid = size * 0.5
    if x < left or x > right:
        return False
    # shrink the vertical half-height linearly towards the tip
    span = half * (right - x) / (right - left)
    return abs(y - mid) <= span


def render(size):
    big = size * SS
    radius = big * 0.225
    pixels = bytearray(size * size * 4)

    for py in range(size):
        for px in range(size):
            hits_bg = hits_fg = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = px * SS + sx + 0.5
                    y = py * SS + sy + 0.5
                    if not rounded_rect(x, y, big, radius):
                        continue
                    hits_bg += 1
                    if in_triangle(x, y, big):
                        hits_fg += 1
            total = SS * SS
            if not hits_bg:
                continue
            alpha = round(255 * hits_bg / total)
            base = lerp(GOLD_HI, GOLD_LO, (px + py) / (2 * max(size - 1, 1)))
            colour = lerp(base, INK, hits_fg / hits_bg)
            offset = (py * size + px) * 4
            pixels[offset:offset + 4] = bytes(colour) + bytes([alpha])
    return bytes(pixels)


def png(size, rgba):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rgba[row * size * 4:(row + 1) * size * 4] for row in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico(images):
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, blobs = b"", b""
    for size, blob in images:
        entries += struct.pack("<BBBBHHII", size & 0xFF, size & 0xFF, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    return header + entries + blobs


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for size in sizes:
        blob = png(size, render(size))
        images.append((size, blob))
        print(f"  {size}x{size}: {len(blob)} bytes")

    with open(os.path.join(here, "aria", "icon.ico"), "wb") as fh:
        fh.write(ico(images))
    with open(os.path.join(here, "docs", "aria.png"), "wb") as fh:
        fh.write(dict(images)[256])
    print("wrote aria/icon.ico and docs/aria.png")


if __name__ == "__main__":
    main()
