"""Generate preview image for BeamNG level selection menu.

BeamNG 0.39/0.40 requires each preview listed in info.json['previews']
to exist on disk and inside the zip; missing previews cause spawnpoint
validation failure in freeroamConfigurator.lua.

This module generates a valid 1920x1080 PNG using pure-Python zlib
without depending on native C extensions.
"""

from __future__ import annotations

import binascii
import struct
import zlib
from pathlib import Path


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Pack a single PNG chunk with CRC32."""
    length = len(data)
    crc = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", length) + chunk_type + data + struct.pack(">I", crc)


def generate_level_preview_png(
    level_dir: Path,
    level_id: str,
    *,
    width: int = 1920,
    height: int = 1080,
    force: bool = False,
) -> Path:
    """Generate a 1920x1080 preview PNG for the level.

    Args:
        level_dir: Level directory.
        level_id: Level identifier.
        width: Image width in pixels (standard 1920).
        height: Image height in pixels (standard 1080).
        force: Overwrite if file already exists and is non-empty.

    Returns:
        Path to the generated preview PNG.
    """
    preview_path = level_dir / f"{level_id}_preview.png"
    if preview_path.exists() and preview_path.stat().st_size > 1000 and not force:
        return preview_path

    preview_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. PNG Header
    header = b"\x89PNG\r\n\x1a\n"

    # 2. IHDR Chunk: 1920x1080, 8-bit RGB (color type 2), compression 0, filter 0, interlace 0
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_chunk = _png_chunk(b"IHDR", ihdr_data)

    # 3. Scanlines: generate a smooth landscape gradient (sky -> horizon -> field)
    raw_rows: list[bytes] = []
    horizon_y = int(height * 0.58)

    for y in range(height):
        if y < horizon_y:
            # Sky: soft Russian plain blue gradient
            t = y / horizon_y
            r = int(140 + (195 - 140) * t)
            g = int(180 + (215 - 180) * t)
            b = int(225 + (235 - 225) * t)
        else:
            # Ground: temperate grassland / meadow gradient
            t = (y - horizon_y) / (height - horizon_y)
            r = int(90 + (55 - 90) * t)
            g = int(115 + (75 - 115) * t)
            b = int(70 + (45 - 70) * t)

        pixel = bytes([r, g, b])
        # Filter byte 0 (None) followed by pixel data
        row = bytes([0]) + pixel * width
        raw_rows.append(row)

    raw_data = b"".join(raw_rows)
    compressed_data = zlib.compress(raw_data, level=6)
    idat_chunk = _png_chunk(b"IDAT", compressed_data)

    # 4. IEND Chunk
    iend_chunk = _png_chunk(b"IEND", b"")

    png_bytes = header + ihdr_chunk + idat_chunk + iend_chunk
    preview_path.write_bytes(png_bytes)
    return preview_path
