"""Pure-Python/NumPy GeoTIFF reader, writer and Web Mercator warp fallback.

Used only when :mod:`rasterio` cannot be imported (for example when GDAL is
not installed or its native DLLs cannot initialise inside a restricted Windows
sandbox). On standard ``ivanovo2beam[all]`` installations :mod:`rasterio`
remains the primary GeoTIFF backend.

Supports the full GeoTIFF contract used by :mod:`ivanovo2beam.terrain.dem`:

* stripped and tiled single-band TIFFs (little- or big-endian);
* uncompressed (``1``) and deflate/zlib (``8``, ``32946``) payloads;
* predictors ``1`` (none), ``2`` (integer differencing) and ``3`` (Adobe TIFF
  Tech Note 3 floating-point byte-plane differencing, as written by GDAL);
* ``int16``, ``uint16``, ``int32``, ``float32`` and ``float64`` rasters;
* ``ModelPixelScaleTag`` (33550), ``ModelTiepointTag`` (33922),
  ``ModelTransformationTag`` (34264), ``GeoKeyDirectoryTag`` (34735),
  ``GDAL_METADATA`` (42112) and ``GDAL_NODATA`` (42113).
"""

from __future__ import annotations

import math
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

_TAG_WIDTH = 256
_TAG_LENGTH = 257
_TAG_BITS_PER_SAMPLE = 258
_TAG_COMPRESSION = 259
_TAG_PHOTOMETRIC = 262
_TAG_STRIP_OFFSETS = 273
_TAG_SAMPLES_PER_PIXEL = 277
_TAG_ROWS_PER_STRIP = 278
_TAG_STRIP_BYTE_COUNTS = 279
_TAG_PLANAR_CONFIG = 284
_TAG_PREDICTOR = 317
_TAG_TILE_WIDTH = 322
_TAG_TILE_LENGTH = 323
_TAG_TILE_OFFSETS = 324
_TAG_TILE_BYTE_COUNTS = 325
_TAG_SAMPLE_FORMAT = 339
_TAG_PIXEL_SCALE = 33550
_TAG_TIEPOINT = 33922
_TAG_TRANSFORMATION = 34264
_TAG_GEO_KEY_DIR = 34735
_TAG_GDAL_METADATA = 42112
_TAG_GDAL_NODATA = 42113

_TYPE_INFO: dict[int, tuple[int, str]] = {
    1: (1, "B"),
    2: (1, "s"),
    3: (2, "H"),
    4: (4, "I"),
    6: (1, "b"),
    8: (2, "h"),
    9: (4, "i"),
    11: (4, "f"),
    12: (8, "d"),
}

_AREA_OR_POINT_RE = re.compile(r'name="AREA_OR_POINT"[^>]*>\s*([^<\s]+)\s*<', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FallbackAffine:
    """2-D affine transform compatible with :class:`affine.Affine`."""

    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    @classmethod
    def translation(cls, xoff: float, yoff: float) -> FallbackAffine:
        return cls(1.0, 0.0, float(xoff), 0.0, 1.0, float(yoff))

    @classmethod
    def scale(cls, *scaling: float) -> FallbackAffine:
        sx = float(scaling[0])
        sy = float(scaling[1]) if len(scaling) > 1 else sx
        return cls(sx, 0.0, 0.0, 0.0, sy, 0.0)

    def __matmul__(self, other: FallbackAffine) -> FallbackAffine:
        return FallbackAffine(
            a=self.a * other.a + self.b * other.d,
            b=self.a * other.b + self.b * other.e,
            c=self.a * other.c + self.b * other.f + self.c,
            d=self.d * other.a + self.e * other.d,
            e=self.d * other.b + self.e * other.e,
            f=self.d * other.c + self.e * other.f + self.f,
        )

    def __mul__(self, other: FallbackAffine | tuple[float, float]) -> Any:
        if isinstance(other, FallbackAffine):
            return self @ other
        col, row = other
        return (self.a * col + self.b * row + self.c, self.d * col + self.e * row + self.f)


def fallback_from_bounds(
    west: float,
    south: float,
    east: float,
    north: float,
    width: int,
    height: int,
) -> FallbackAffine:
    """Build an area-registered north-up transform from bounding coordinates."""
    return FallbackAffine.translation(west, north) @ FallbackAffine.scale(
        (east - west) / width,
        -(north - south) / height,
    )


@dataclass(frozen=True, slots=True)
class FallbackBounds:
    """Bounding box tuple compatible with ``rasterio.coords.BoundingBox``."""

    left: float
    bottom: float
    right: float
    top: float


@dataclass(frozen=True, slots=True)
class FallbackWindow:
    """Pixel window compatible with ``rasterio.windows.Window``."""

    col_off: int
    row_off: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class FallbackGeoCRS:
    """Minimal EPSG CRS object compatible with ``rasterio.crs.CRS``."""

    epsg: int | None

    @property
    def is_geographic(self) -> bool:
        return self.epsg in {4326, 4269, 4267, 4979} or (
            self.epsg is not None and 4000 <= self.epsg < 5000
        )

    def to_epsg(self) -> int | None:
        return self.epsg

    @classmethod
    def from_epsg(cls, code: int) -> FallbackGeoCRS:
        return cls(int(code))

    @classmethod
    def from_any(cls, value: Any) -> FallbackGeoCRS:
        if isinstance(value, FallbackGeoCRS):
            return value
        if isinstance(value, int):
            return cls(value)
        text = str(value).strip().upper()
        if text.startswith("EPSG:"):
            return cls(int(text.split(":", 1)[1]))
        return cls(None)

    def __str__(self) -> str:
        return f"EPSG:{self.epsg}" if self.epsg is not None else "CRS(unknown)"


class FallbackResampling:
    """Stub enum matching ``rasterio.warp.Resampling``."""

    bilinear = "bilinear"


def _parse_tiff_tags(raw: bytes) -> tuple[str, dict[int, Any]]:
    if len(raw) < 8 or raw[:2] not in {b"II", b"MM"}:
        raise OSError("not a valid Classic TIFF file")
    endian = "<" if raw[:2] == b"II" else ">"
    magic, ifd_offset = struct.unpack_from(f"{endian}HI", raw, 2)
    if magic != 42 or ifd_offset + 2 > len(raw):
        raise OSError(f"unsupported TIFF header (magic={magic})")
    entry_count = struct.unpack_from(f"{endian}H", raw, ifd_offset)[0]
    pos = ifd_offset + 2
    if pos + entry_count * 12 > len(raw):
        raise OSError("truncated TIFF IFD")

    tags: dict[int, Any] = {}
    for _ in range(entry_count):
        tag, field_type, count, val_or_off = struct.unpack_from(f"{endian}HHII", raw, pos)
        pos += 12
        info = _TYPE_INFO.get(field_type)
        if info is None:
            continue
        item_size, fmt_char = info
        byte_len = count * item_size
        if byte_len <= 4:
            inline = raw[pos - 4 : pos]
            payload = inline[:byte_len]
        else:
            if val_or_off + byte_len > len(raw):
                raise OSError(f"truncated TIFF tag {tag}")
            payload = raw[val_or_off : val_or_off + byte_len]
        if field_type == 2:
            tags[tag] = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
        else:
            tags[tag] = struct.unpack(f"{endian}{count}{fmt_char}", payload)
    return endian, tags


def _decode_chunk(
    chunk: bytes,
    *,
    rows: int,
    cols: int,
    compression: int,
    predictor: int,
    sample_format: int,
    bits_per_sample: int,
    endian: str,
) -> np.ndarray:
    if compression == 1:
        buf = chunk
    elif compression in {8, 32946}:
        buf = zlib.decompress(chunk)
    else:
        raise OSError(f"unsupported TIFF compression {compression}")

    bps_bytes = bits_per_sample // 8
    expected = rows * cols * bps_bytes
    if len(buf) < expected:
        raise OSError(f"TIFF chunk too short: {len(buf)} < {expected}")
    buf = buf[:expected]

    if sample_format == 3:
        base_char = "f" if bps_bytes == 4 else "d"
    elif sample_format == 2:
        base_char = {1: "b", 2: "h", 4: "i"}[bps_bytes]
    else:
        base_char = {1: "B", 2: "H", 4: "I"}[bps_bytes]

    if predictor == 3:
        # Adobe TIFF Tech Note 3 floating-point horizontal differencing:
        # byte-stream cumsum per row, then un-shuffle MSB-first byte planes.
        u8 = np.frombuffer(buf, dtype=np.uint8).reshape(rows, cols * bps_bytes)
        restored = np.cumsum(u8, axis=1, dtype=np.uint64).astype(np.uint8)
        unshuffled = restored.reshape(rows, bps_bytes, cols).transpose(0, 2, 1).copy()
        return unshuffled.view(f">{base_char}{bps_bytes}").reshape(rows, cols)

    arr = np.frombuffer(buf, dtype=f"{endian}{base_char}{bps_bytes}").reshape(rows, cols).copy()
    if predictor == 2:
        arr = np.cumsum(arr, axis=1, dtype=arr.dtype)
    return arr


class FallbackDatasetReader:
    """GeoTIFF reader matching the subset of ``rasterio.DatasetReader`` used here."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._file = self._path.open("rb")
        try:
            raw_head = self._file.read()
        except Exception:
            self._file.close()
            raise
        self._raw = raw_head
        self._endian, tags = _parse_tiff_tags(self._raw)

        self.width = int(tags[_TAG_WIDTH][0])
        self.height = int(tags[_TAG_LENGTH][0])
        self.count = int(tags.get(_TAG_SAMPLES_PER_PIXEL, (1,))[0])
        self._bps = int(tags.get(_TAG_BITS_PER_SAMPLE, (32,))[0])
        self._compression = int(tags.get(_TAG_COMPRESSION, (1,))[0])
        self._predictor = int(tags.get(_TAG_PREDICTOR, (1,))[0])
        self._sample_format = int(tags.get(_TAG_SAMPLE_FORMAT, (3,))[0])

        self._tiled = _TAG_TILE_WIDTH in tags
        if self._tiled:
            self._tile_w = int(tags[_TAG_TILE_WIDTH][0])
            self._tile_h = int(tags[_TAG_TILE_LENGTH][0])
            self._offsets = tuple(int(v) for v in tags[_TAG_TILE_OFFSETS])
            self._byte_counts = tuple(int(v) for v in tags[_TAG_TILE_BYTE_COUNTS])
        else:
            self._rows_per_strip = int(tags.get(_TAG_ROWS_PER_STRIP, (self.height,))[0])
            self._offsets = tuple(int(v) for v in tags[_TAG_STRIP_OFFSETS])
            self._byte_counts = tuple(int(v) for v in tags[_TAG_STRIP_BYTE_COUNTS])

        # Parse GeoKeys (34735).
        epsg: int | None = None
        raster_type = 1  # 1 = PixelIsArea, 2 = PixelIsPoint
        geo_keys = tags.get(_TAG_GEO_KEY_DIR)
        if geo_keys and len(geo_keys) >= 4:
            num_keys = int(geo_keys[3])
            for idx in range(num_keys):
                base = 4 + idx * 4
                if base + 4 > len(geo_keys):
                    break
                key_id, tiff_loc, _cnt, val_off = geo_keys[base : base + 4]
                if tiff_loc == 0:
                    if key_id == 1025:
                        raster_type = int(val_off)
                    elif key_id in {2048, 3072} and int(val_off) > 0:
                        epsg = int(val_off)
        self.crs: FallbackGeoCRS | None = FallbackGeoCRS(epsg) if epsg is not None else None

        self._tags_dict: dict[str, str] = {}
        if raster_type == 2:
            self._tags_dict["AREA_OR_POINT"] = "Point"
        gdal_meta = tags.get(_TAG_GDAL_METADATA)
        if isinstance(gdal_meta, str):
            match = _AREA_OR_POINT_RE.search(gdal_meta)
            if match:
                self._tags_dict["AREA_OR_POINT"] = match.group(1)

        # Parse geotransform.
        if _TAG_TRANSFORMATION in tags and len(tags[_TAG_TRANSFORMATION]) >= 16:
            m = tags[_TAG_TRANSFORMATION]
            self.transform = FallbackAffine(
                float(m[0]), float(m[1]), float(m[3]), float(m[4]), float(m[5]), float(m[7])
            )
        else:
            scale = tags.get(_TAG_PIXEL_SCALE, (0.0, 0.0, 0.0))
            tie = tags.get(_TAG_TIEPOINT, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            sx = float(scale[0])
            sy = float(scale[1])
            tp_i, tp_j, _, tp_x, tp_y, _ = (float(v) for v in tie[:6])
            # GDAL/rasterio convention: for PixelIsPoint GeoTIFFs without an
            # inline shift in the tiepoint, tiepoint (0, 0) is the first pixel
            # centre and rasterio reports transform.c / .f at that tiepoint.
            left = tp_x - tp_i * sx
            top = tp_y + tp_j * sy
            self.transform = FallbackAffine(sx, 0.0, left, 0.0, -sy, top)

        self.res = (abs(self.transform.a), abs(self.transform.e))
        right = self.transform.c + self.width * self.transform.a
        bottom = self.transform.f + self.height * self.transform.e
        self.bounds = FallbackBounds(
            left=min(self.transform.c, right),
            bottom=min(self.transform.f, bottom),
            right=max(self.transform.c, right),
            top=max(self.transform.f, bottom),
        )

        nodata_raw = tags.get(_TAG_GDAL_NODATA)
        if isinstance(nodata_raw, str) and nodata_raw.strip():
            self.nodata: float | None = float(nodata_raw.strip())
        else:
            self.nodata = None

        self._chunk_cache: dict[int, np.ndarray] = {}

    def tags(self) -> dict[str, str]:
        return dict(self._tags_dict)

    def _get_chunk(self, index: int, rows: int, cols: int) -> np.ndarray:
        cached = self._chunk_cache.get(index)
        if cached is not None:
            return cached
        off = self._offsets[index]
        bc = self._byte_counts[index]
        decoded = _decode_chunk(
            self._raw[off : off + bc],
            rows=rows,
            cols=cols,
            compression=self._compression,
            predictor=self._predictor,
            sample_format=self._sample_format,
            bits_per_sample=self._bps,
            endian=self._endian,
        )
        if len(self._chunk_cache) >= 32:
            self._chunk_cache.clear()
        self._chunk_cache[index] = decoded
        return decoded

    def read(
        self,
        band: int = 1,
        *,
        window: FallbackWindow | None = None,
        masked: bool = False,
    ) -> np.ndarray:
        _ = (band, masked)
        if window is None:
            col0, row0, w, h = 0, 0, self.width, self.height
        else:
            col0 = int(window.col_off)
            row0 = int(window.row_off)
            w = int(window.width)
            h = int(window.height)
        col1 = col0 + w
        row1 = row0 + h

        out_dtype = np.float32 if (self._sample_format == 3 and self._bps == 32) else np.float64
        out = np.empty((h, w), dtype=out_dtype)

        if self._tiled:
            tiles_across = math.ceil(self.width / self._tile_w)
            ty0 = row0 // self._tile_h
            ty1 = (row1 - 1) // self._tile_h
            tx0 = col0 // self._tile_w
            tx1 = (col1 - 1) // self._tile_w
            for ty in range(ty0, ty1 + 1):
                for tx in range(tx0, tx1 + 1):
                    idx = ty * tiles_across + tx
                    tile = self._get_chunk(idx, self._tile_h, self._tile_w)
                    r_start = max(row0, ty * self._tile_h)
                    r_end = min(row1, min(self.height, (ty + 1) * self._tile_h))
                    c_start = max(col0, tx * self._tile_w)
                    c_end = min(col1, min(self.width, (tx + 1) * self._tile_w))
                    out[r_start - row0 : r_end - row0, c_start - col0 : c_end - col0] = tile[
                        r_start - ty * self._tile_h : r_end - ty * self._tile_h,
                        c_start - tx * self._tile_w : c_end - tx * self._tile_w,
                    ]
        else:
            rps = self._rows_per_strip
            s0 = row0 // rps
            s1 = (row1 - 1) // rps
            for s in range(s0, s1 + 1):
                strip_rows = min(rps, self.height - s * rps)
                strip = self._get_chunk(s, strip_rows, self.width)
                r_start = max(row0, s * rps)
                r_end = min(row1, s * rps + strip_rows)
                out[r_start - row0 : r_end - row0, :] = strip[
                    r_start - s * rps : r_end - s * rps,
                    col0:col1,
                ]
        return out

    def close(self) -> None:
        self._chunk_cache.clear()
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> FallbackDatasetReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FallbackDatasetWriter:
    """Single-band float32 GeoTIFF writer compatible with ``rasterio.open(..., 'w')``."""

    def __init__(self, path: Path, **profile: Any) -> None:
        self._path = Path(path)
        self._width = int(profile["width"])
        self._height = int(profile["height"])
        self._crs = FallbackGeoCRS.from_any(profile.get("crs", 4326))
        self._transform: FallbackAffine = profile["transform"]
        self._nodata: float | None = (
            None if profile.get("nodata") is None else float(profile["nodata"])
        )
        self._tags: dict[str, str] = {}
        self._data: np.ndarray | None = None

    def write(self, data: np.ndarray, band: int = 1) -> None:
        _ = band
        arr = np.asarray(data, dtype="<f4")
        if arr.shape != (self._height, self._width):
            raise ValueError(f"expected {(self._height, self._width)}, got {arr.shape}")
        self._data = arr.copy()

    def update_tags(self, **tags: str) -> None:
        for key, value in tags.items():
            self._tags[str(key)] = str(value)

    def close(self) -> None:
        if self._data is None:
            return
        data = self._data
        self._data = None

        rows_per_strip = max(1, min(self._height, max(1, 65536 // max(1, self._width * 4))))
        strips: list[bytes] = []
        for r0 in range(0, self._height, rows_per_strip):
            chunk = data[r0 : min(self._height, r0 + rows_per_strip)].tobytes()
            strips.append(zlib.compress(chunk, level=6))

        point_reg = self._tags.get("AREA_OR_POINT", "").strip().lower() == "point"
        epsg = self._crs.to_epsg() or 4326
        is_geo = self._crs.is_geographic
        model_type = 2 if is_geo else 1
        raster_type = 2 if point_reg else 1
        crs_key = 2048 if is_geo else 3072
        geo_keys = (
            1, 1, 0, 3,
            1024, 0, 1, model_type,
            1025, 0, 1, raster_type,
            crs_key, 0, 1, int(epsg),
        )

        # Build Classic little-endian TIFF.
        body = bytearray()
        strip_offsets: list[int] = []
        strip_counts: list[int] = []
        header_size = 8
        for blob in strips:
            if len(body) % 2:
                body.append(0)
            strip_offsets.append(header_size + len(body))
            strip_counts.append(len(blob))
            body.extend(blob)

        entries: list[tuple[int, int, int, bytes]] = []

        def add_short(tag: int, values: tuple[int, ...]) -> None:
            entries.append((tag, 3, len(values), struct.pack(f"<{len(values)}H", *values)))

        def add_long(tag: int, values: tuple[int, ...]) -> None:
            entries.append((tag, 4, len(values), struct.pack(f"<{len(values)}I", *values)))

        def add_double(tag: int, values: tuple[float, ...]) -> None:
            entries.append((tag, 12, len(values), struct.pack(f"<{len(values)}d", *values)))

        def add_ascii(tag: int, value: str) -> None:
            raw_s = value.encode("ascii") + b"\x00"
            entries.append((tag, 2, len(raw_s), raw_s))

        add_long(_TAG_WIDTH, (self._width,))
        add_long(_TAG_LENGTH, (self._height,))
        add_short(_TAG_BITS_PER_SAMPLE, (32,))
        add_short(_TAG_COMPRESSION, (8,))
        add_short(_TAG_PHOTOMETRIC, (1,))
        add_long(_TAG_STRIP_OFFSETS, tuple(strip_offsets))
        add_short(_TAG_SAMPLES_PER_PIXEL, (1,))
        add_long(_TAG_ROWS_PER_STRIP, (rows_per_strip,))
        add_long(_TAG_STRIP_BYTE_COUNTS, tuple(strip_counts))
        add_short(_TAG_PLANAR_CONFIG, (1,))
        add_short(_TAG_PREDICTOR, (1,))
        add_short(_TAG_SAMPLE_FORMAT, (3,))
        add_double(_TAG_PIXEL_SCALE, (abs(self._transform.a), abs(self._transform.e), 0.0))
        add_double(_TAG_TIEPOINT, (0.0, 0.0, 0.0, self._transform.c, self._transform.f, 0.0))
        add_short(_TAG_GEO_KEY_DIR, geo_keys)
        if point_reg:
            add_ascii(
                _TAG_GDAL_METADATA,
                '<GDALMetadata><Item name="AREA_OR_POINT">Point</Item></GDALMetadata>',
            )
        if self._nodata is not None:
            nodata_str = "nan" if math.isnan(self._nodata) else repr(float(self._nodata))
            add_ascii(_TAG_GDAL_NODATA, nodata_str)

        entries.sort(key=lambda item: item[0])

        if len(body) % 2:
            body.append(0)
        ifd_offset = header_size + len(body)
        ifd_size = 2 + len(entries) * 12 + 4
        extra_offset = ifd_offset + ifd_size
        ifd_buf = bytearray(struct.pack("<H", len(entries)))
        extra_buf = bytearray()

        for tag, ftype, count, raw_val in entries:
            if len(raw_val) <= 4:
                padded = raw_val + b"\x00" * (4 - len(raw_val))
                ifd_buf.extend(struct.pack("<HHI4s", tag, ftype, count, padded))
            else:
                if len(extra_buf) % 2:
                    extra_buf.append(0)
                off = extra_offset + len(extra_buf)
                extra_buf.extend(raw_val)
                ifd_buf.extend(struct.pack("<HHII", tag, ftype, count, off))
        ifd_buf.extend(struct.pack("<I", 0))

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("wb") as handle:
            handle.write(struct.pack("<2sHI", b"II", 42, ifd_offset))
            handle.write(body)
            handle.write(ifd_buf)
            handle.write(extra_buf)

    def __enter__(self) -> FallbackDatasetWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_geotiff(path: str | Path, mode: str = "r", **kwargs: Any) -> Any:
    """Open a GeoTIFF for reading or writing without :mod:`rasterio`."""
    if mode == "r":
        try:
            return FallbackDatasetReader(Path(path))
        except OSError as exc:
            raise ValueError(f"unreadable GeoTIFF {path}: {exc}") from exc
    if mode == "w":
        return FallbackDatasetWriter(Path(path), **kwargs)
    raise ValueError(f"unsupported mode {mode!r}")


_WEB_MERCATOR_R = 6378137.0


def fallback_transform_bounds(
    src_crs: Any,
    dst_crs: Any,
    west: float,
    south: float,
    east: float,
    north: float,
    densify_pts: int = 21,
) -> tuple[float, float, float, float]:
    """Transform bounding box between EPSG:4326 and EPSG:3857."""
    _ = densify_pts
    src = FallbackGeoCRS.from_any(src_crs).to_epsg()
    dst = FallbackGeoCRS.from_any(dst_crs).to_epsg()
    if src == 4326 and dst == 3857:
        clamp_s = max(-85.05112878, min(85.05112878, south))
        clamp_n = max(-85.05112878, min(85.05112878, north))
        x0 = _WEB_MERCATOR_R * math.radians(west)
        x1 = _WEB_MERCATOR_R * math.radians(east)
        y0 = _WEB_MERCATOR_R * math.log(math.tan(math.pi / 4.0 + math.radians(clamp_s) / 2.0))
        y1 = _WEB_MERCATOR_R * math.log(math.tan(math.pi / 4.0 + math.radians(clamp_n) / 2.0))
        return (x0, y0, x1, y1)
    raise ValueError(f"unsupported fallback transform_bounds {src} -> {dst}")


def fallback_warp_reproject(
    *,
    source: np.ndarray,
    destination: np.ndarray,
    src_transform: FallbackAffine,
    src_crs: Any,
    src_nodata: float,
    dst_transform: FallbackAffine,
    dst_crs: Any,
    dst_nodata: float,
    resampling: Any = None,
    init_dest_nodata: bool = True,
) -> None:
    """Bilinearly warp an EPSG:3857 raster block onto an EPSG:4326 destination."""
    _ = (src_nodata, resampling)
    if FallbackGeoCRS.from_any(src_crs).to_epsg() != 3857 or (
        FallbackGeoCRS.from_any(dst_crs).to_epsg() != 4326
    ):
        raise ValueError("fallback warp_reproject only supports EPSG:3857 -> EPSG:4326")
    if init_dest_nodata:
        destination.fill(dst_nodata)

    dst_h, dst_w = destination.shape
    src_h, src_w = source.shape
    cols = np.arange(dst_w, dtype=np.float64) + 0.5
    rows = np.arange(dst_h, dtype=np.float64) + 0.5
    lon = dst_transform.c + cols * dst_transform.a
    lat = dst_transform.f + rows * dst_transform.e
    lon_grid, lat_grid = np.meshgrid(lon, lat)

    merc_x = _WEB_MERCATOR_R * np.radians(lon_grid)
    merc_y = _WEB_MERCATOR_R * np.log(
        np.tan(math.pi / 4.0 + np.radians(np.clip(lat_grid, -85.05112878, 85.05112878)) / 2.0)
    )
    src_col = (merc_x - src_transform.c) / src_transform.a - 0.5
    src_row = (merc_y - src_transform.f) / src_transform.e - 0.5

    inside = (src_col >= 0.0) & (src_col <= src_w - 1) & (src_row >= 0.0) & (src_row <= src_h - 1)
    if not inside.any():
        return
    c = src_col[inside]
    r = src_row[inside]
    c0 = np.clip(np.floor(c).astype(np.intp), 0, max(src_w - 2, 0))
    r0 = np.clip(np.floor(r).astype(np.intp), 0, max(src_h - 2, 0))
    c1 = np.minimum(c0 + 1, src_w - 1)
    r1 = np.minimum(r0 + 1, src_h - 1)
    tx = c - c0
    ty = r - r0
    val = np.zeros(c.shape, dtype=np.float64)
    for w_corner, px in (
        ((1.0 - tx) * (1.0 - ty), source[r0, c0]),
        (tx * (1.0 - ty), source[r0, c1]),
        ((1.0 - tx) * ty, source[r1, c0]),
        (tx * ty, source[r1, c1]),
    ):
        val += np.where(w_corner > 0.0, px * w_corner, 0.0)
    destination[inside] = val
