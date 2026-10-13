#!/usr/bin/env python3
"""
Generates a crisp 256x256 PNG extension icon matching clay-logo.svg
Uses only Python standard library (zlib, struct) with zero external pip dependencies.
"""
import zlib
import struct
from pathlib import Path

def create_png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = len(data)
    crc = zlib.crc32(chunk_type + data) & 0xffffffff
    return struct.pack(">I", length) + chunk_type + data + struct.pack(">I", crc)

def build_clay_icon_png(output_path: Path, width: int = 256, height: int = 256):
    # Background: #FAF6F0 (250, 246, 240)
    bg_color = (250, 246, 240)
    # Terracotta: #C15C3D (193, 92, 61)
    fg_color = (193, 92, 61)

    # 2D pixel grid initialized to background
    pixels = [[bg_color for _ in range(width)] for _ in range(height)]

    # Matching clay-logo.svg coordinates:
    # 512x512 coordinates: translate(64, 148), scale(24)
    # For 256x256 coordinates: scale by 0.5 -> translate(32, 74), scale(12)
    scale = 12
    tx = 32
    ty = 74

    # Rectangles from clay-logo.svg:
    rectangles = [
        # Chevron '>'
        (1, 1, 2, 1),
        (2, 2, 2, 1),
        (3, 3, 2, 1),
        (4, 4, 2, 1),
        (5, 5, 2, 1),
        (4, 6, 2, 1),
        (3, 7, 2, 1),
        (2, 8, 2, 1),
        (1, 9, 2, 1),
        # Underscore '_'
        (8, 8, 6, 2),
    ]

    for rx, ry, rw, rh in rectangles:
        x0 = tx + rx * scale
        y0 = ty + ry * scale
        x1 = x0 + rw * scale
        y1 = y0 + rh * scale
        for y in range(max(0, y0), min(height, y1)):
            for x in range(max(0, x0), min(width, x1)):
                pixels[y][x] = fg_color

    # Build raw scanlines (each row begins with filter type byte 0x00)
    raw_scanlines = bytearray()
    for row in pixels:
        raw_scanlines.append(0)  # Filter byte: None
        for r, g, b in row:
            raw_scanlines.extend((r, g, b))

    # PNG Signature
    png_bytes = bytearray(b"\x89PNG\r\n\x1a\n")

    # IHDR Chunk: 256x256, 8-bit depth, Truecolor (2)
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png_bytes.extend(create_png_chunk(b"IHDR", ihdr_data))

    # IDAT Chunk: compressed image scanlines
    idat_data = zlib.compress(bytes(raw_scanlines), level=9)
    png_bytes.extend(create_png_chunk(b"IDAT", idat_data))

    # IEND Chunk
    png_bytes.extend(create_png_chunk(b"IEND", b""))

    output_path.write_bytes(bytes(png_bytes))
    print(f"[OK] Generated 256x256 PNG icon at {output_path.resolve()}")

if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "icon.png"
    build_clay_icon_png(out, 256, 256)
