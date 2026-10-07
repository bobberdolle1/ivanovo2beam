"""DEM (terrain elevation) acquisition and sampling.

Why this module exists
----------------------
The game world is built at 1:1 scale, so the terrain under every road must be
the *real* terrain, in metres above sea level. This module is the single place
where elevation data enters the pipeline: it downloads tiles from open,
anonymous providers, mosaics them into one plain GeoTIFF in ``EPSG:4326``, and
serves bilinear samples from it.

Contract with the rest of the pipeline
--------------------------------------
``fetch_dem`` guarantees a **single GeoTIFF in EPSG:4326 (degrees), float32,
elevation in metres above sea level**, plus a sidecar JSON that records exactly
where the data came from. ``DemReader`` reads that file back::

    path = fetch_dem(bbox, cache_dir)          # cached, offline on the 2nd call
    dem = DemReader(path)
    z = dem.sample(40.97393, 56.99987)         # metres above sea level
    grid, cell_m = dem.window_array(bbox, 1024)  # what the terrain builder eats

Source policy: a chain with honest fallback
-------------------------------------------
Elevation tiles come and go, mirrors change their authentication rules, and a
pipeline that hard-fails because one bucket moved is not reproducible. So the
module walks a chain and *records which link actually produced the data*:

1. ``copernicus_glo30`` -- Copernicus DEM GLO-30 on the AWS Open Data mirror.
   Two endpoints are tried in order: the global
   ``copernicus-dem-30m.s3.amazonaws.com`` bucket and the regional
   ``copernicus-dem-30m.s3.eu-central-1.amazonaws.com`` one.
   **Verified 2026-10-07: no AWS credentials are required.** An anonymous
   ``HEAD``/``GET`` returns ``200`` with ``Content-Length: 32103651`` for the
   Ivanovo tile ``.../Copernicus_DSM_COG_10_N56_00_E041_00_DEM.tif`` on *both*
   endpoints (identical ``ETag``), i.e. plain public HTTPS. Objects are 1x1
   degree COGs; a bbox spanning several degrees is downloaded tile by tile and
   mosaicked.
2. ``aws_terrain_tiles`` -- the AWS Open Data "Terrain Tiles" GeoTIFF tiles
   (``s3://elevation-tiles-prod``), anonymous, no auth. They are ``EPSG:3857``
   with elevations in metres, so this module reprojects them to ``EPSG:4326``
   itself. Verified 2026-10-07: ``200`` for
   ``.../geotiff/12/2514/1254.tif`` (59314 bytes, int16, ``nodata=-32768``)
   and ``.../geotiff/13/5028/2509.tif`` (325490 bytes, float32,
   ``nodata=-3.4e38``); both read back 100-150 m ASL over Ivanovo, which
   matches the Copernicus tile, so the units are metres and not centimetres.
3. ``srtm_gl1`` -- NASA SRTM GL1 1-arc-second ``.hgt.gz`` tiles from the same
   anonymous bucket. Verified 2026-10-07: ``200`` for
   ``.../skadi/N57/N57E041.hgt.gz`` (4341833 bytes -> 3601x3601 int16
   big-endian, ``nodata=-32768``, no void pixels in that tile). Coverage stops
   at 60 deg N and is land-only, so it is a last resort.

A source that cannot deliver raises :class:`DemSourceUnavailable`, and the next
link in the chain is tried. The sidecar records ``requested_source`` next to
``source`` so a fallback is never silent: callers that *must* have Copernicus
can pass ``allow_fallback=False`` and get a hard error instead.

Honesty about units
-------------------
Every provider is read as metres above sea level and the module asserts nothing
about a source it has not measured. The stored rasters are also validated for
plausibility while being built: the sidecar carries the min/max/mean elevation
of the mosaic, so a scaling mistake (e.g. millimetres, or a 0-4000 int16
terrarium encoding) shows up immediately. For Ivanovo Oblast the sane band is
roughly 75-170 m ASL.

Caching
-------
The cache key is ``sha256`` over a canonical string built from a schema tag, the
requested source, the requested resolution, and the bbox **rounded to 1e-6 deg
(~0.11 m)** with a fixed ``%.6f`` format -- never from a raw ``repr`` of the
bbox, whose float formatting is not reproducible across callers. Cache hits are
decided by the presence of *both* the ``.tif`` and its ``.json`` sidecar (the
sidecar is written last and therefore acts as the commit marker), plus a size
check against the recorded byte count.

Writes are atomic (``os.replace`` of a uniquely named temporary file inside the
cache directory) and each fetch works in its own scratch directory, so
concurrent calls -- for different bboxes or even the same one -- cannot corrupt
each other or observe a half-written file.

Downloaded data is never committed: the cache directory lives outside version
control (``.cache/`` is gitignored in this repository).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import shutil
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Geod
from rasterio.crs import CRS
from rasterio.transform import Affine
from rasterio.warp import Resampling, transform_bounds
from rasterio.warp import reproject as warp_reproject
from rasterio.windows import Window

from ivanovo2beam import __version__
from ivanovo2beam.gis.projection import BBox

__all__ = [
    "COPERNICUS_ENDPOINTS",
    "TERRAIN_TILE_PIXELS",
    "DemError",
    "DemFetchError",
    "DemReader",
    "DemSourceInfo",
    "DemSourceUnavailable",
    "available_sources",
    "cache_key",
    "copernicus_tile_ids",
    "copernicus_tile_name",
    "copernicus_tile_url",
    "fetch_dem",
    "mercator_ground_resolution_m",
    "mercator_tile_index",
    "read_sidecar",
    "source_info",
    "srtm_tile_name",
    "terrain_tile_pixel_res_m",
    "terrain_tile_span_m",
    "zoom_for_resolution",
]

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

#: Identifies this tool to the open-data mirrors we hammer; polite HTTP.
USER_AGENT = f"ivanovo2beam/{__version__} (+https://github.com/bobberdolle1/ivanovo2beam)"

#: Copernicus DEM GLO-30 mirrors, tried in this order. Both are anonymous.
COPERNICUS_ENDPOINTS: tuple[str, ...] = (
    "https://copernicus-dem-30m.s3.amazonaws.com",
    "https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com",
)

#: AWS Open Data "Terrain Tiles" (Mapzen Joerd) GeoTIFF tiles, EPSG:3857.
TERRAIN_TILE_BASE = "https://s3.amazonaws.com/elevation-tiles-prod"
TERRAIN_TILE_URL_TEMPLATE = f"{TERRAIN_TILE_BASE}/geotiff/{{z}}/{{x}}/{{y}}.tif"

#: AWS Open Data SRTM GL1 (skadi) tiles, gzipped 1-arc-second ``.hgt``.
SRTM_TILE_BASE = f"{TERRAIN_TILE_BASE}/skadi"
SRTM_URL_TEMPLATE = f"{SRTM_TILE_BASE}/{{lat}}/{{lat}}{{lon}}.hgt.gz"

#: Seconds of arc per SRTM GL1 sample (1 arc-second ~ 30 m).
SRTM_SAMPLES_PER_DEGREE = 3600

#: Half the Web Mercator world extent (EPSG:3857), in metres.
WEB_MERCATOR_HALF_WORLD = 20037508.342789244

#: Ground sample distance of a *standard* (256 px) EPSG:3857 tile at zoom 0, in
#: metres per pixel (``2 * pi * 6378137 / 256``). The Terrain Tiles GeoTIFF
#: objects are 512 px wide over the same standard tile extent, so one of their
#: pixels is half this: see :func:`terrain_tile_pixel_res_m`.
WEB_MERCATOR_RES_AT_Z0 = 156543.03392804097

#: The Terrain Tiles GeoTIFF objects are 512x512 pixels, not the usual 256, and
#: still cover exactly one standard XYZ tile extent. Measured on a real tile
#: (z=12/2514/1254): 19.10925707 m per pixel, i.e. a 9783.94 m tile.
TERRAIN_TILE_PIXELS = 512

#: Nominal Copernicus/SRTM ground resolution used when the caller does not say.
DEFAULT_RESOLUTION_M = 30.0

#: Refuse to download an absurd number of 1x1 degree tiles in one call.
MAX_1DEG_TILES = 64

#: Refuse to download an absurd number of Web Mercator tiles in one call.
MAX_MERCATOR_TILES = 1500

#: Zoom bounds for the Terrain Tiles fallback.
MIN_ZOOM = 4
MAX_ZOOM = 13

#: Quantisation of the bbox in the cache key: 1e-6 deg is ~0.11 m on the ground,
#: far below any DEM's resolution, but coarse enough to absorb float noise.
KEY_QUANTUM_DEG = 1e-6

#: Tolerance, in pixels, for treating a position as on the raster edge. The
#: degree-to-pixel division can land at 2.0000000000000004 for the last column
#: of a raster, which must not read as "outside".
_PIXEL_TOL = 1e-9

#: The provider chain, in preference order.
SOURCE_CHAIN: tuple[str, ...] = ("copernicus_glo30", "aws_terrain_tiles", "srtm_gl1")

_GEOD = Geod(ellps="WGS84")

_ATTRIBUTION_COPERNICUS = (
    "(c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018 "
    "provided under COPERNICUS by the European Union and ESA; all rights reserved."
)
_LICENCE_COPERNICUS = (
    "Copernicus DEM Instrument Licence (free, open, attribution required): "
    "https://docs.sentinel-hub.com/api/latest/static/copernicus-dem/Copernicus_DEM_Instrument_Licence.pdf"
)
_ATTRIBUTION_TERRAIN_TILES = (
    "Terrain Tiles (Mapzen Joerd) by tilezen, hosted on AWS Open Data; "
    "SRTM (NASA/USGS), GMTED, ETOPO1 and national elevation models."
)
_LICENCE_TERRAIN_TILES = (
    "Mixed: SRTM/GMTED/ETOPO1 are public domain, some national datasets are CC-BY; "
    "see https://github.com/tilezen/joerd/blob/master/docs/attribution.md"
)
_ATTRIBUTION_SRTM = "NASA SRTM GL1 v003 (public domain), mirrored by AWS Open Data (skadi bucket)."
_LICENCE_SRTM = "Public domain (NASA/USGS); no restrictions."

_COPERNICUS_TILE_TEMPLATE = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM/Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM.tif"
)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class DemError(RuntimeError):
    """Base class for every failure raised by this module."""


class DemSourceUnavailable(DemError):
    """One provider could not deliver (HTTP error, missing tile, bad payload).

    Raised inside providers. :func:`fetch_dem` catches it and moves to the next
    source in the chain; it never escapes to the caller on its own.
    """


class DemFetchError(DemError):
    """No source in the chain could deliver the requested bbox.

    Attributes:
        attempts: One human-readable line per provider that was tried.
    """

    def __init__(self, message: str, attempts: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.attempts: tuple[str, ...] = tuple(attempts)


# --------------------------------------------------------------------------- #
# source catalogue
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DemSourceInfo:
    """Static description of one DEM provider.

    Attributes:
        key: Stable identifier accepted by :func:`fetch_dem`.
        title: Human-readable name.
        resolution_m: Nominal ground resolution in metres.
        attribution: Exact attribution string to ship with the data.
        licence: Licence name/summary.
        requires_auth: Whether credentials are needed (all shipped sources: no).
        url_template: Documentation-only URL pattern.
        notes: Operational notes: what was actually verified, known limits and
            the fallback behaviour.
    """

    key: str
    title: str
    resolution_m: float
    attribution: str
    licence: str
    requires_auth: bool
    url_template: str
    notes: str = ""


_SOURCES: dict[str, DemSourceInfo] = {
    "copernicus_glo30": DemSourceInfo(
        key="copernicus_glo30",
        title="Copernicus DEM GLO-30 (AWS Open Data mirror)",
        resolution_m=30.0,
        attribution=_ATTRIBUTION_COPERNICUS,
        licence=_LICENCE_COPERNICUS,
        requires_auth=False,
        url_template=_COPERNICUS_TILE_TEMPLATE,
        notes=(
            "Primary source. NO AWS credentials are required or accepted: verified "
            "2026-10-07 with anonymous HEAD/GET against both the global and the "
            "eu-central-1 endpoint (HTTP 200, 32103651 bytes for N56/E041, identical "
            "ETag). 1x1 degree COG tiles, EPSG:4326, float32, metres ASL, POINT pixel "
            "convention; the mirror ships 3600x2400 px per tile (1 arcsec N-S, "
            "1.5 arcsec E-W ~ 25-31 m ground). Ocean-only tiles are absent from the "
            "dataset and answer HTTP 404, which makes the whole source fail for such a "
            "bbox and hands over to aws_terrain_tiles. {lat} is e.g. N56 or S01, "
            "{lon} is e.g. E041 or W001."
        ),
    ),
    "aws_terrain_tiles": DemSourceInfo(
        key="aws_terrain_tiles",
        title="AWS Open Data Terrain Tiles (GeoTIFF, EPSG:3857)",
        resolution_m=30.0,
        attribution=_ATTRIBUTION_TERRAIN_TILES,
        licence=_LICENCE_TERRAIN_TILES,
        requires_auth=False,
        url_template=TERRAIN_TILE_URL_TEMPLATE,
        notes=(
            "Fallback for Copernicus. Anonymous, no auth: verified 2026-10-07 "
            "(HTTP 200 for z=12/2514/1254, 59314 bytes int16 nodata=-32768, and "
            "z=13/5028/2509, 325490 bytes float32 nodata=-3.4e38). Elevations are in "
            "METRES and the objects are 512x512 EPSG:3857 tiles covering one standard "
            "XYZ tile extent (so a z=12 pixel is 19.109 m, not 38.2 m); this module "
            "reprojects them to EPSG:4326. The zoom is chosen from the requested "
            "resolution_m and the bbox latitude: the coarsest zoom whose ground "
            "resolution still meets the target, i.e. zoom 11 / 20.8 m at 57 N and "
            "zoom 12 / 19.1 m at the equator for the default 30 m."
        ),
    ),
    "srtm_gl1": DemSourceInfo(
        key="srtm_gl1",
        title="NASA SRTM GL1 (1 arc-second) via the AWS Open Data skadi mirror",
        resolution_m=30.0,
        attribution=_ATTRIBUTION_SRTM,
        licence=_LICENCE_SRTM,
        requires_auth=False,
        url_template=SRTM_URL_TEMPLATE,
        notes=(
            "Last resort. Anonymous, no auth: verified 2026-10-07 (HTTP 200, 4341833 "
            "bytes for N57/N57E041.hgt.gz -> 3601x3601 int16 big-endian, nodata=-32768, "
            "metres ASL, EPSG:4326). Land only between 60 N and 56 S, so it cannot "
            "cover polar or open-ocean bboxes; those raise DemFetchError once every "
            "link in the chain is exhausted."
        ),
    ),
}


def available_sources() -> list[DemSourceInfo]:
    """Return the known DEM sources, best first.

    Returns:
        A new list of :class:`DemSourceInfo`, ordered exactly like the provider
        chain :func:`fetch_dem` walks.
    """
    return [_SOURCES[key] for key in SOURCE_CHAIN]


def source_info(key: str) -> DemSourceInfo:
    """Return the :class:`DemSourceInfo` for ``key``.

    Raises:
        ValueError: If ``key`` is not a known source.
    """
    try:
        return _SOURCES[key]
    except KeyError as exc:
        known = ", ".join(SOURCE_CHAIN)
        raise ValueError(f"unknown DEM source {key!r}; known sources: {known}") from exc


# --------------------------------------------------------------------------- #
# naming arithmetic (pure functions, unit-tested without network)
# --------------------------------------------------------------------------- #


def _hemisphere_tag(index: int, size: int, positive: str, negative: str) -> str:
    """Format an integer degree index as e.g. ``N56`` / ``S01`` / ``E041`` / ``W001``."""
    if index >= 0:
        return f"{positive}{index:0{size}d}"
    return f"{negative}{-index:0{size}d}"


def copernicus_tile_name(lat_index: int, lon_index: int) -> str:
    """Return the Copernicus DEM object name for the tile whose corner is given.

    The dataset is tiled on whole degrees and named after the tile's **lower
    left** corner: ``Copernicus_DSM_COG_10_N56_00_E041_00_DEM`` covers
    56..57 N, 41..42 E. Zero is positive (``N00``/``E000``), and the southern /
    western hemispheres use ``S``/``W`` with the absolute value, zero padded to
    two (latitude) and three (longitude) digits: latitude index -1 is ``S01``,
    longitude index -1 is ``W001``.

    Args:
        lat_index: Latitude of the tile's south edge, in whole degrees.
        lon_index: Longitude of the tile's west edge, in whole degrees.

    Returns:
        The object name, without directory or ``.tif`` suffix.

    Examples:
        >>> copernicus_tile_name(56, 41)
        'Copernicus_DSM_COG_10_N56_00_E041_00_DEM'
        >>> copernicus_tile_name(-1, -1)
        'Copernicus_DSM_COG_10_S01_00_W001_00_DEM'
    """
    lat = _hemisphere_tag(lat_index, 2, "N", "S")
    lon = _hemisphere_tag(lon_index, 3, "E", "W")
    return f"Copernicus_DSM_COG_10_{lat}_00_{lon}_00_DEM"


def copernicus_tile_ids(bbox: BBox) -> list[tuple[int, int]]:
    """Return the ``(lat_index, lon_index)`` of every 1x1 degree tile a bbox touches.

    A bbox edge that lands exactly on a whole degree touches the tile on both
    sides, because the raster's pixel centres -- not its area bounds -- carry
    the data: sampling exactly at 57.0 N needs the ``N57`` tile's first row.
    This is also the tiling used by SRTM GL1.

    Args:
        bbox: Geographic extent in degrees.

    Returns:
        Tile indices ordered south to north, then west to east.
    """
    lat0, lat1 = math.floor(bbox.south), math.floor(bbox.north)
    lon0, lon1 = math.floor(bbox.west), math.floor(bbox.east)
    return [(lat, lon) for lat in range(lat0, lat1 + 1) for lon in range(lon0, lon1 + 1)]


def copernicus_tile_url(tile_name: str, endpoint: str = COPERNICUS_ENDPOINTS[0]) -> str:
    """Return the download URL of a Copernicus DEM tile object."""
    return f"{endpoint.rstrip('/')}/{tile_name}/{tile_name}.tif"


def srtm_tile_name(lat_index: int, lon_index: int) -> str:
    """Return the SRTM GL1 (skadi) tile stem, e.g. ``N57E041``.

    Uses the same 1x1 degree, lower-left-corner convention as
    :func:`copernicus_tile_name`.
    """
    lat = _hemisphere_tag(lat_index, 2, "N", "S")
    lon = _hemisphere_tag(lon_index, 3, "E", "W")
    return f"{lat}{lon}"


def mercator_tile_index(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Return the ``(x, y)`` XYZ tile index containing a WGS84 point.

    ``y`` grows southwards, as in the slippy-map / XYZ convention used by the
    ``elevation-tiles-prod`` bucket. Latitudes are clamped to the Web Mercator
    limit (``+/-85.05112878``) and the result is clamped to the grid, so the
    function is total.

    Args:
        lon: Longitude in degrees.
        lat: Latitude in degrees.
        zoom: Zoom level (0..23).

    Returns:
        The ``(x, y)`` tile index.

    Examples:
        >>> mercator_tile_index(41.0, 57.0, 12)
        (2514, 1254)
    """
    if not 0 <= zoom <= 23:
        raise ValueError(f"zoom must be within 0..23, got {zoom}")
    n = 1 << zoom
    lat = max(-85.05112878, min(85.05112878, float(lat)))
    x = int((float(lon) + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return (min(max(x, 0), n - 1), min(max(y, 0), n - 1))


def terrain_tile_span_m(zoom: int) -> float:
    """Return the ground extent of one XYZ tile at ``zoom``, in metres.

    This is the standard slippy-map extent (``2 * pi * R / 2**zoom``, 256
    standard pixels wide); the ``elevation-tiles-prod`` GeoTIFF objects are
    512 px across that same extent.
    """
    if not 0 <= zoom <= 23:
        raise ValueError(f"zoom must be within 0..23, got {zoom}")
    return WEB_MERCATOR_RES_AT_Z0 * 256.0 / (1 << zoom)


def terrain_tile_pixel_res_m(zoom: int) -> float:
    """Return one Terrain Tiles pixel in EPSG:3857 metres.

    Measured against the live dataset: 19.10925707 m at zoom 12, i.e. the
    standard tile extent divided by :data:`TERRAIN_TILE_PIXELS`.
    """
    return terrain_tile_span_m(zoom) / TERRAIN_TILE_PIXELS


def mercator_ground_resolution_m(zoom: int, lat: float) -> float:
    """Return the ground sample distance of a Terrain Tiles pixel, in metres.

    Web Mercator is conformal with a point scale factor of ``1 / cos(lat)``, so
    the 19.109 m pixel of zoom 12 is only ~10.4 m on the ground at Ivanovo's
    latitude (57 N) and 38.2 m at zoom 11.
    """
    return terrain_tile_pixel_res_m(zoom) * math.cos(math.radians(lat))


def zoom_for_resolution(resolution_m: float, lat: float) -> int:
    """Pick the Terrain Tiles zoom that matches a requested ground resolution.

    Returns the **coarsest** zoom whose ground resolution still meets the
    target -- the smallest download that satisfies the request -- clamped to
    ``MIN_ZOOM..MAX_ZOOM``. Resolution is latitude dependent
    (:func:`mercator_ground_resolution_m`), so a 30 m request means zoom 12
    (19.1 m) at the equator but zoom 11 (20.8 m) at Ivanovo's 57 N, where the
    underlying ~30 m source data is already fully resolved.

    Args:
        resolution_m: Desired ground resolution in metres.
        lat: Latitude the bbox sits at.

    Returns:
        The zoom level.

    Examples:
        >>> zoom_for_resolution(30.0, 57.0)
        11
        >>> zoom_for_resolution(30.0, 0.0)
        12
    """
    target = max(float(resolution_m), 1.0)
    zoom = MAX_ZOOM
    for candidate in range(MIN_ZOOM, MAX_ZOOM + 1):
        if mercator_ground_resolution_m(candidate, lat) <= target:
            zoom = candidate
            break
    return zoom


# --------------------------------------------------------------------------- #
# cache key
# --------------------------------------------------------------------------- #


def cache_key(bbox: BBox, source: str, resolution_m: float | None = None) -> str:
    """Return the stable sha256 cache key of a DEM request.

    The key is the sha256 of a canonical, schema-tagged string. Coordinates are
    rounded to :data:`KEY_QUANTUM_DEG` (1e-6 deg, ~0.11 m) and formatted with a
    fixed ``%.6f``, so two callers that compute the "same" bbox through
    different arithmetic (``40.1 + 0.2`` vs ``40.3``) share one cache entry,
    while a real change of a single pixel (~3e-4 deg at 30 m) still produces a
    different key. The key never contains the raw bbox text.

    Args:
        bbox: Requested extent.
        source: Requested source key.
        resolution_m: Requested ground resolution, or ``None`` for native.

    Returns:
        A 64-character lowercase hex digest.
    """
    coords = "|".join(f"{round(float(v), 6):.6f}" for v in bbox.as_tuple())
    res = "native" if resolution_m is None else f"{round(float(resolution_m), 6):.6f}"
    canonical = "|".join(("ivanovo2beam.dem.v1", str(source), res, coords))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Payload:
    """One downloaded object, remembered for the sidecar metadata."""

    url: str
    path: Path
    n_bytes: int
    sha256: str
    etag: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable record written into the sidecar."""
        return {
            "url": self.url,
            "bytes": self.n_bytes,
            "sha256": self.sha256,
            "etag": self.etag,
        }


def _download(url: str, dest: Path, timeout: float) -> _Payload:
    """Stream ``url`` to ``dest``, hashing as we go.

    Args:
        url: Object to fetch.
        dest: File to write (its directory must exist).
        timeout: Socket timeout in seconds, applied per read.

    Returns:
        The payload record: URL, byte count, sha256, ETag.

    Raises:
        DemSourceUnavailable: On any HTTP/transport failure or empty body. The
            caller decides whether to fall back.
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    digest = hashlib.sha256()
    n_bytes = 0
    with dest.open("wb") as handle:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                etag = response.headers.get("ETag")
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
                    n_bytes += len(chunk)
        except urllib.error.HTTPError as exc:  # 404 tile, 403 policy change, ...
            raise DemSourceUnavailable(f"HTTP {exc.code} {exc.reason} for {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DemSourceUnavailable(f"{type(exc).__name__} for {url}: {exc}") from exc
    if n_bytes == 0:
        raise DemSourceUnavailable(f"empty response body for {url}")
    return _Payload(url=url, path=dest, n_bytes=n_bytes, sha256=digest.hexdigest(), etag=etag)


# --------------------------------------------------------------------------- #
# raster helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _TileGrid:
    """Georeferencing of one downloaded tile on disk."""

    path: Path
    left_center: float
    top_center: float
    xres: float
    yres: float
    width: int
    height: int
    nodata: float | None


def _read_tile_grid(path: Path) -> _TileGrid:
    """Describe a geographic tile, honouring its AREA_OR_POINT convention.

    Copernicus COGs are tagged ``AREA_OR_POINT=Point``: their transform origin
    is the centre of the top-left pixel. Rasters written by this module are
    area-registered. Both are normalised here to "coordinate of the first pixel
    centre", which is what sampling needs.

    Raises:
        DemSourceUnavailable: If the file is not a single-band geographic raster.
    """
    with rasterio.open(path) as dataset:
        if dataset.count != 1:
            raise DemSourceUnavailable(f"{path.name}: expected 1 band, got {dataset.count}")
        if dataset.crs is None or not dataset.crs.is_geographic:
            raise DemSourceUnavailable(f"{path.name}: expected a geographic CRS, got {dataset.crs}")
        xres, yres = (abs(float(value)) for value in dataset.res)
        if xres == 0.0 or yres == 0.0:
            raise DemSourceUnavailable(f"{path.name}: degenerate resolution {dataset.res}")
        point_registered = str(dataset.tags().get("AREA_OR_POINT", "")).strip().lower() == "point"
        shift_x = 0.0 if point_registered else 0.5 * xres
        shift_y = 0.0 if point_registered else 0.5 * yres
        nodata = None if dataset.nodata is None else float(dataset.nodata)
        return _TileGrid(
            path=path,
            left_center=float(dataset.transform.c) + shift_x,
            top_center=float(dataset.transform.f) - shift_y,
            xres=xres,
            yres=yres,
            width=int(dataset.width),
            height=int(dataset.height),
            nodata=nodata,
        )


def _apply_nodata(data: np.ndarray, nodata: float | None) -> np.ndarray:
    """Return ``data`` as float64 with the nodata value replaced by NaN."""
    out = np.asarray(data, dtype=np.float64)
    if nodata is not None and not math.isnan(nodata):
        out[out == nodata] = np.nan
    return out


def _read_tile_window(tile: _TileGrid, col: int, row: int, width: int, height: int) -> np.ndarray:
    """Read a window of a tile, as float64 with NaN for nodata."""
    with rasterio.open(tile.path) as dataset:
        data = dataset.read(1, window=Window(col, row, width, height))
    return _apply_nodata(data, tile.nodata)


def _write_geotiff(
    path: Path,
    array: np.ndarray,
    transform: Affine,
    *,
    nodata: float | None = None,
    crs_epsg: int = 4326,
    point_registered: bool = False,
) -> None:
    """Write a single-band float32 GeoTIFF with sensible compression.

    NaN is written as the nodata value when present, so downstream tools see
    holes instead of numbers like -3.4e38.

    Args:
        path: Output file.
        array: 2-D elevation array in metres.
        transform: Affine mapping pixel (col, row) corners to coordinates.
        nodata: Explicit nodata value; ``None`` means "NaN if any, else none".
        crs_epsg: EPSG code of the (geographic) CRS.
        point_registered: Tag the file ``AREA_OR_POINT=Point``.
    """
    height, width = array.shape
    data = np.asarray(array, dtype=np.float32)
    has_nan = bool(np.isnan(data).any())
    if nodata is None and has_nan:
        nodata = float("nan")
    profile: dict[str, Any] = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": CRS.from_epsg(crs_epsg),
        "transform": transform,
        "nodata": nodata,
        "compress": "deflate",
        "predictor": 3,  # floating-point predictor
        "BIGTIFF": "IF_SAFER",
    }
    if width >= 512 and height >= 512:
        profile |= {"tiled": True, "blockxsize": 512, "blockysize": 512}
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dataset:
        dataset.write(data, 1)
        if point_registered:
            dataset.update_tags(AREA_OR_POINT="Point")


def _decimate_block_mean(array: np.ndarray, factor: int) -> np.ndarray:
    """Downsample by ``factor`` with a NaN-aware block mean.

    The array is padded with NaN on the right/bottom to a whole number of
    blocks, so the output still covers the input's full extent. Edge blocks
    therefore average whatever real pixels they contain. A block with no data
    at all stays NaN.

    Args:
        array: 2-D input.
        factor: Integer decimation factor (``< 2`` returns the input unchanged).

    Returns:
        The decimated array, ``ceil(rows/factor) x ceil(cols/factor)``.
    """
    if factor < 2:
        return array
    rows, cols = array.shape
    pad_rows = (-rows) % factor
    pad_cols = (-cols) % factor
    if pad_rows or pad_cols:
        array = np.pad(array, ((0, pad_rows), (0, pad_cols)), constant_values=np.nan)
    out_rows, out_cols = array.shape[0] // factor, array.shape[1] // factor
    blocks = (
        array.reshape(out_rows, factor, out_cols, factor)
        .transpose(0, 2, 1, 3)
        .reshape(out_rows, out_cols, factor * factor)
    )
    finite = np.isfinite(blocks)
    counts = finite.sum(axis=2)
    totals = np.where(finite, blocks, 0.0).sum(axis=2)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(counts > 0, totals / np.maximum(counts, 1), np.nan)


def _bbox_metric_span_m(bbox: BBox) -> tuple[float, float]:
    """Return the geodesic ``(east-west, north-south)`` size of a bbox in metres."""
    mid_lon, mid_lat = bbox.center
    _, _, span_x = _GEOD.inv(bbox.west, mid_lat, bbox.east, mid_lat)
    _, _, span_y = _GEOD.inv(mid_lon, bbox.south, mid_lon, bbox.north)
    return (float(span_x), float(span_y))


def _native_resolution_m(xres_deg: float, yres_deg: float, lat: float) -> float:
    """Return the mean ground size of a pixel, in metres, at a latitude."""
    _, _, dx = _GEOD.inv(0.0, lat, xres_deg, lat)
    _, _, dy = _GEOD.inv(0.0, lat, 0.0, lat + yres_deg)
    return 0.5 * (float(dx) + float(dy))


def _decimation_factor(resolution_m: float | None, native_resolution_m: float) -> int:
    """Return the block factor implementing a requested resolution, or 1.

    Coarser than native is honoured by averaging blocks whole; finer than native
    is impossible (the detail is not in the data), so the native grid is kept
    and the sidecar records what actually happened.
    """
    if resolution_m is None or native_resolution_m <= 0.0:
        return 1
    requested = float(resolution_m)
    if requested < 1.5 * native_resolution_m:
        return 1
    return int(min(64, max(1, round(requested / native_resolution_m))))


# --------------------------------------------------------------------------- #
# geographic mosaic (Copernicus, SRTM)
# --------------------------------------------------------------------------- #


def _mosaic_geographic_tiles(
    tiles: Sequence[_TileGrid],
    bbox: BBox,
    out_path: Path,
    *,
    decimate: int = 1,
) -> dict[str, float]:
    """Mosaic pixel-centre-registered geographic tiles and crop to ``bbox``.

    All tiles must share one resolution and lie on a common pixel grid (they do:
    every tile spans exactly one degree). Tiles are copied by integer offsets,
    so no resampling -- and therefore no seam -- is introduced; values outside
    the data are NaN and only the part of the mosaic that covers ``bbox`` (plus
    one pixel of margin on each side, so that bilinear resampling of ``bbox``
    never runs off the data) is kept in memory and written.

    Args:
        tiles: Downloaded tiles, in any order.
        bbox: Requested extent in degrees.
        out_path: GeoTIFF to write.
        decimate: Block-mean decimation factor applied after mosaicking.

    Returns:
        ``{"min": ..., "max": ..., "mean": ...}`` elevation statistics in metres
        over the written raster (NaN-aware; all-NaN gives NaNs).

    Raises:
        DemSourceUnavailable: If the tiles disagree about their grid, or the
            requested bbox does not overlap them at all.
    """
    if not tiles:
        raise DemSourceUnavailable("no tiles to mosaic")
    reference = tiles[0]
    for tile in tiles:
        if (tile.width, tile.height) != (reference.width, reference.height):
            raise DemSourceUnavailable(
                f"tile size mismatch: {tile.path.name} is {tile.width}x{tile.height}, "
                f"{reference.path.name} is {reference.width}x{reference.height}"
            )
        if not (
            math.isclose(tile.xres, reference.xres, rel_tol=1e-9)
            and math.isclose(tile.yres, reference.yres, rel_tol=1e-9)
        ):
            raise DemSourceUnavailable(
                f"tile resolution mismatch: {tile.path.name} {tile.xres},{tile.yres} vs "
                f"{reference.path.name} {reference.xres},{reference.yres}"
            )

    xres, yres = reference.xres, reference.yres
    lon_origin = min(tile.left_center for tile in tiles)
    top_origin = max(tile.top_center for tile in tiles)
    lon_end = max(tile.left_center + (tile.width - 1) * xres for tile in tiles)
    lat_end = min(tile.top_center - (tile.height - 1) * yres for tile in tiles)
    full_width = round((lon_end - lon_origin) / xres) + 1
    full_height = round((top_origin - lat_end) / yres) + 1

    # Crop to the bbox rounded *outwards* to whole pixels, plus one pixel of
    # margin, so both the bbox and a bilinear kernel around it are covered.
    col_start = min(max(math.floor((bbox.west - lon_origin) / xres) - 1, 0), full_width)
    col_stop = min(max(math.ceil((bbox.east - lon_origin) / xres) + 2, 0), full_width)
    row_start = min(max(math.floor((top_origin - bbox.north) / yres) - 1, 0), full_height)
    row_stop = min(max(math.ceil((top_origin - bbox.south) / yres) + 2, 0), full_height)
    if col_stop <= col_start or row_stop <= row_start:
        raise DemSourceUnavailable(
            f"bbox {bbox.as_tuple()} does not overlap the downloaded tiles "
            f"({lon_origin:.4f}..{lon_end:.4f} E, {lat_end:.4f}..{top_origin:.4f} N)"
        )

    mosaic = np.full((row_stop - row_start, col_stop - col_start), np.nan, dtype=np.float64)
    placed = 0
    for tile in tiles:
        col_offset = round((tile.left_center - lon_origin) / xres)
        row_offset = round((top_origin - tile.top_center) / yres)
        if (
            abs(tile.left_center - (lon_origin + col_offset * xres)) > 1e-9
            or abs(tile.top_center - (top_origin - row_offset * yres)) > 1e-9
        ):
            raise DemSourceUnavailable(f"tile {tile.path.name} is not aligned to the shared pixel grid")
        src_col0 = max(col_offset, col_start)
        src_col1 = min(col_offset + tile.width, col_stop)
        src_row0 = max(row_offset, row_start)
        src_row1 = min(row_offset + tile.height, row_stop)
        if src_col1 <= src_col0 or src_row1 <= src_row0:
            continue
        data = _read_tile_window(
            tile,
            src_col0 - col_offset,
            src_row0 - row_offset,
            src_col1 - src_col0,
            src_row1 - src_row0,
        )
        mosaic[src_row0 - row_start : src_row1 - row_start, src_col0 - col_start : src_col1 - col_start] = (
            data
        )
        placed += 1
    if placed == 0:
        raise DemSourceUnavailable("no tile overlapped the requested bbox")

    if decimate > 1:
        mosaic = _decimate_block_mean(mosaic, decimate)

    # Area-registered transform of the written raster: decimation keeps the
    # first pixel's outer corner fixed, so the origin does not depend on it.
    transform = Affine.translation(
        lon_origin + col_start * xres - 0.5 * xres,
        top_origin - row_start * yres + 0.5 * yres,
    ) @ Affine.scale(xres * decimate, -yres * decimate)
    _write_geotiff(out_path, mosaic, transform)
    return _elevation_stats(mosaic)


def _elevation_stats(array: np.ndarray) -> dict[str, float]:
    """Return NaN-aware min/max/mean of an elevation array, in metres."""
    if array.size == 0:
        return {"min": float("nan"), "max": float("nan"), "mean": float("nan")}
    count = int(np.isfinite(array).sum())
    if count == 0:
        return {"min": float("nan"), "max": float("nan"), "mean": float("nan")}
    return {
        "min": float(np.nanmin(array)),
        "max": float(np.nanmax(array)),
        "mean": float(np.nansum(array, dtype=np.float64) / count),
    }


def _sha256_file(path: Path, chunk: int = 1 << 22) -> str:
    """Return the sha256 of a file, read in chunks so huge rasters stay off-heap."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _FetchResult:
    """What a provider hands back to :func:`fetch_dem`."""

    path: Path
    payloads: tuple[_Payload, ...]
    detail: dict[str, Any]
    stats: dict[str, float]


def _fetch_copernicus(
    bbox: BBox,
    resolution_m: float | None,
    timeout: float,
    work: Path,
) -> _FetchResult:
    """Download and mosaic Copernicus DEM GLO-30 tiles from the AWS mirror.

    Tries :data:`COPERNICUS_ENDPOINTS` in order; each endpoint must serve *every*
    tile of the bbox, otherwise the next endpoint is tried.
    """
    tile_ids = copernicus_tile_ids(bbox)
    if len(tile_ids) > MAX_1DEG_TILES:
        raise DemSourceUnavailable(
            f"bbox spans {len(tile_ids)} Copernicus 1x1 degree tiles "
            f"(limit {MAX_1DEG_TILES}); use a smaller bbox"
        )
    work.mkdir(parents=True, exist_ok=True)

    tiles: list[_TileGrid] = []
    payloads: list[_Payload] = []
    failures: list[str] = []
    endpoint_used: str | None = None
    for endpoint in COPERNICUS_ENDPOINTS:
        tiles, payloads = [], []
        for lat_index, lon_index in tile_ids:
            name = copernicus_tile_name(lat_index, lon_index)
            url = copernicus_tile_url(name, endpoint)
            try:
                payloads.append(_download(url, work / f"{name}.tif", timeout))
                tiles.append(_read_tile_grid(payloads[-1].path))
            except DemSourceUnavailable as exc:
                failures.append(f"{endpoint}: {exc}")
                tiles, payloads = [], []
                break
        if tiles:
            endpoint_used = endpoint
            break
    if not tiles or endpoint_used is None:
        raise DemSourceUnavailable("; ".join(failures))

    native = _native_resolution_m(tiles[0].xres, tiles[0].yres, bbox.center[1])
    factor = _decimation_factor(resolution_m, native)
    out = work / "mosaic.tif"
    stats = _mosaic_geographic_tiles(tiles, bbox, out, decimate=factor)
    return _FetchResult(
        path=out,
        payloads=tuple(payloads),
        detail={
            "endpoint": endpoint_used,
            "tiles": [tile.path.name for tile in tiles],
            "native_resolution_m": native,
            "decimation": factor,
        },
        stats=stats,
    )


def _fetch_srtm(
    bbox: BBox,
    resolution_m: float | None,
    timeout: float,
    work: Path,
) -> _FetchResult:
    """Download and mosaic SRTM GL1 (skadi) ``.hgt.gz`` tiles.

    The gzipped tiles are raw 3601x3601 big-endian int16 grids with no
    georeferencing, so each one is wrapped into a temporary GeoTIFF (tagged
    ``AREA_OR_POINT=Point``, i.e. pixel centres on whole arc-seconds) and then
    handed to the same mosaic code as the Copernicus tiles.
    """
    tile_ids = copernicus_tile_ids(bbox)
    if len(tile_ids) > MAX_1DEG_TILES:
        raise DemSourceUnavailable(
            f"bbox spans {len(tile_ids)} SRTM 1x1 degree tiles (limit {MAX_1DEG_TILES})"
        )
    work.mkdir(parents=True, exist_ok=True)

    samples = SRTM_SAMPLES_PER_DEGREE
    expected_bytes = (samples + 1) ** 2 * 2
    payloads: list[_Payload] = []
    tiles: list[_TileGrid] = []
    for lat_index, lon_index in tile_ids:
        if not -56 <= lat_index <= 59:
            raise DemSourceUnavailable(f"srtm_gl1 covers 56 S .. 60 N only; tile {lat_index} is outside")
        name = srtm_tile_name(lat_index, lon_index)
        url = f"{SRTM_TILE_BASE}/{name[:3]}/{name}.hgt.gz"
        payload = _download(url, work / f"{name}.hgt.gz", timeout)
        payloads.append(payload)
        raw = gzip.decompress(payload.path.read_bytes())
        if len(raw) != expected_bytes:
            raise DemSourceUnavailable(f"{name}: {len(raw)} bytes after gunzip, expected {expected_bytes}")
        heights = np.frombuffer(raw, dtype=">i2").reshape(samples + 1, samples + 1)
        data = heights.astype(np.float64)
        data[(data == -32768) | (data == -32767)] = np.nan  # void / nodata
        tif = work / f"{name}.tif"
        transform = Affine.translation(lon_index, lat_index + 1) @ Affine.scale(1.0 / samples, -1.0 / samples)
        _write_geotiff(tif, data, transform, point_registered=True)
        tiles.append(_read_tile_grid(tif))

    native = _native_resolution_m(1.0 / samples, 1.0 / samples, bbox.center[1])
    factor = _decimation_factor(resolution_m, native)
    out = work / "mosaic.tif"
    stats = _mosaic_geographic_tiles(tiles, bbox, out, decimate=factor)
    return _FetchResult(
        path=out,
        payloads=tuple(payloads),
        detail={
            "tiles": [tile.path.name for tile in tiles],
            "native_resolution_m": native,
            "decimation": factor,
        },
        stats=stats,
    )


def _fetch_terrain_tiles(
    bbox: BBox,
    resolution_m: float | None,
    timeout: float,
    work: Path,
) -> _FetchResult:
    """Download EPSG:3857 Terrain Tiles and reproject them to EPSG:4326.

    The zoom follows the requested resolution (default
    :data:`DEFAULT_RESOLUTION_M`) and the bbox latitude, the tiles are mosaicked
    in their native CRS by integer offsets, and the block is warped to a
    geographic grid whose pixel size matches the tiles' ground resolution.
    """
    work.mkdir(parents=True, exist_ok=True)
    target_m = DEFAULT_RESOLUTION_M if resolution_m is None else float(resolution_m)
    lat_center = bbox.center[1]
    zoom = zoom_for_resolution(target_m, lat_center)
    tile_span = terrain_tile_span_m(zoom)

    merc = transform_bounds("EPSG:4326", "EPSG:3857", *bbox.as_tuple(), densify_pts=21)
    pad = 2.0 * terrain_tile_pixel_res_m(zoom)  # two pixels of slack for the kernel
    x_min = math.floor((merc[0] - pad + WEB_MERCATOR_HALF_WORLD) / tile_span)
    x_max = math.floor((merc[2] + pad + WEB_MERCATOR_HALF_WORLD) / tile_span)
    y_min = math.floor((WEB_MERCATOR_HALF_WORLD - merc[3] - pad) / tile_span)
    y_max = math.floor((WEB_MERCATOR_HALF_WORLD - merc[1] + pad) / tile_span)
    n_tiles = 1 << zoom
    x_min, x_max = max(0, x_min), min(n_tiles - 1, x_max)
    y_min, y_max = max(0, y_min), min(n_tiles - 1, y_max)
    columns = x_max - x_min + 1
    rows = y_max - y_min + 1
    if columns * rows > MAX_MERCATOR_TILES:
        raise DemSourceUnavailable(
            f"bbox needs {columns * rows} Terrain Tiles at zoom {zoom} (limit {MAX_MERCATOR_TILES})"
        )

    block = np.full((rows * TERRAIN_TILE_PIXELS, columns * TERRAIN_TILE_PIXELS), np.nan, dtype=np.float64)
    payloads: list[_Payload] = []
    # The mosaic grid is anchored on the first tile's *actual* transform rather
    # than on the ideal XYZ formula: this dataset's pixel size is 19.10925707 m
    # at zoom 12 instead of the textbook 19.10925976 m, a 0.1 ppm difference
    # that still accumulates to ~2.5 m across 2500 tiles, i.e. a quarter pixel
    # at the tile we care about. Anchoring on real coordinates keeps the mosaic
    # honest and the later crop exact.
    block_left: float | None = None
    block_top: float | None = None
    res_x = res_y = terrain_tile_pixel_res_m(zoom)
    for x_index in range(x_min, x_max + 1):
        for y_index in range(y_min, y_max + 1):
            url = f"{TERRAIN_TILE_BASE}/geotiff/{zoom}/{x_index}/{y_index}.tif"
            payload = _download(url, work / f"{zoom}_{x_index}_{y_index}.tif", timeout)
            payloads.append(payload)
            with rasterio.open(payload.path) as dataset:
                if dataset.crs is None or dataset.crs.to_epsg() != 3857:
                    raise DemSourceUnavailable(f"{payload.path.name}: expected EPSG:3857, got {dataset.crs}")
                if (dataset.width, dataset.height) != (
                    TERRAIN_TILE_PIXELS,
                    TERRAIN_TILE_PIXELS,
                ):
                    raise DemSourceUnavailable(
                        f"{payload.path.name}: expected {TERRAIN_TILE_PIXELS}px tiles, "
                        f"got {dataset.width}x{dataset.height}"
                    )
                if block_left is None:
                    block_left = float(dataset.transform.c)
                    block_top = float(dataset.transform.f)
                    res_x, res_y = (abs(float(value)) for value in dataset.res)
                else:
                    expected_x = block_left + (x_index - x_min) * res_x * TERRAIN_TILE_PIXELS
                    expected_y = block_top - (y_index - y_min) * res_y * TERRAIN_TILE_PIXELS
                    if (
                        abs(dataset.transform.c - expected_x) > 0.1 * res_x
                        or abs(dataset.transform.f - expected_y) > 0.1 * res_y
                    ):
                        raise DemSourceUnavailable(
                            f"{payload.path.name}: tile is not aligned to the {zoom}/"
                            f"{x_min}/{y_min} tile grid"
                        )
                data = _apply_nodata(dataset.read(1), dataset.nodata)
            row0 = (y_index - y_min) * TERRAIN_TILE_PIXELS
            col0 = (x_index - x_min) * TERRAIN_TILE_PIXELS
            block[row0 : row0 + TERRAIN_TILE_PIXELS, col0 : col0 + TERRAIN_TILE_PIXELS] = data
    assert block_left is not None and block_top is not None  # columns >= 1

    # Crop the block to the requested bbox (whole pixels) before warping: the
    # warp itself would do this anyway, but doing it first keeps peak memory to
    # the size of the bbox rather than the size of the tile block.
    col0 = min(max(math.floor((merc[0] - block_left) / res_x) - 1, 0), block.shape[1] - 1)
    col1 = min(max(math.ceil((merc[2] - block_left) / res_x) + 2, col0 + 1), block.shape[1])
    row0 = min(max(math.floor((block_top - merc[3]) / res_y) - 1, 0), block.shape[0] - 1)
    row1 = min(max(math.ceil((block_top - merc[1]) / res_y) + 2, row0 + 1), block.shape[0])
    source = block[row0:row1, col0:col1].copy()
    source_transform = Affine.translation(block_left + col0 * res_x, block_top - row0 * res_y) @ Affine.scale(
        res_x, -res_y
    )
    del block

    ground_m = res_x * math.cos(math.radians(lat_center))
    span_x_m, span_y_m = _bbox_metric_span_m(bbox)
    width = max(2, math.ceil(span_x_m / ground_m) + 1)
    height = max(2, math.ceil(span_y_m / ground_m) + 1)
    # The destination grid is grown by one pixel on each side, so that its
    # *pixel-centre* extent covers the requested bbox with half a pixel of
    # margin. Every source then shares one contract: DemReader.bounds always
    # contains the requested bbox, so sample() works on its edges.
    step_x = (bbox.east - bbox.west) / width
    step_y = (bbox.north - bbox.south) / height
    destination_transform = Affine.translation(bbox.west - step_x, bbox.north + step_y) @ Affine.scale(
        step_x, -step_y
    )
    destination = np.full((height + 2, width + 2), np.nan, dtype=np.float64)
    warp_reproject(
        source=source,
        destination=destination,
        src_transform=source_transform,
        src_crs=CRS.from_epsg(3857),
        src_nodata=float("nan"),
        dst_transform=destination_transform,
        dst_crs=CRS.from_epsg(4326),
        dst_nodata=float("nan"),
        resampling=Resampling.bilinear,
        init_dest_nodata=True,
    )
    out = work / "mosaic.tif"
    _write_geotiff(out, destination, destination_transform)
    return _FetchResult(
        path=out,
        payloads=tuple(payloads),
        detail={
            "zoom": zoom,
            "tiles": [payload.path.name for payload in payloads],
            "tile_resolution_m": [res_x, res_y],
            "tile_ground_resolution_m": ground_m,
            "reprojected_from": "EPSG:3857",
        },
        stats=_elevation_stats(destination),
    )


_PROVIDERS: dict[str, Callable[[BBox, float | None, float, Path], _FetchResult]] = {
    "copernicus_glo30": _fetch_copernicus,
    "aws_terrain_tiles": _fetch_terrain_tiles,
    "srtm_gl1": _fetch_srtm,
}


# --------------------------------------------------------------------------- #
# public fetch entry point
# --------------------------------------------------------------------------- #


def fetch_dem(
    bbox: BBox,
    cache_dir: Path,
    *,
    source: str = "copernicus_glo30",
    resolution_m: float | None = None,
    timeout: float = 120.0,
    force: bool = False,
    allow_fallback: bool = True,
) -> Path:
    """Download (or reuse from cache) a DEM covering ``bbox``.

    Returns a path to a **single GeoTIFF in EPSG:4326** (Geographic WGS84,
    degrees) with elevations in **metres above sea level**, float32, nodata
    NaN, plus a sidecar ``<key>.json`` next to it recording the source key, the
    exact source URL(s), the fetch timestamp (UTC), the bbox, the byte size,
    the sha256 of every downloaded payload and of the written file, the
    attribution and the licence.

    The raster always covers the requested ``bbox`` with a margin: its valid
    (pixel-centre) extent contains ``bbox``, so ``DemReader.sample`` works on
    the bbox edges as well as inside it.

    Providers are tried as ``source`` first, then -- unless
    ``allow_fallback=False`` -- the rest of :data:`SOURCE_CHAIN`; the sidecar
    distinguishes ``requested_source`` from the ``source`` that actually
    delivered, so a silent downgrade is impossible. A cache hit performs no
    network access at all, which is what makes repeated pipeline runs work
    offline.

    Resolution semantics:

    * geographic providers (Copernicus, SRTM) always deliver their native grid;
      if ``resolution_m`` is at least 1.5x coarser, the mosaic is block-mean
      decimated by the nearest integer factor (finer than native is impossible
      and silently keeps the native grid -- the sidecar records the truth);
    * ``aws_terrain_tiles`` picks its zoom from ``resolution_m`` (default 30 m)
      and the bbox latitude, then reprojects to a metric-matched degree grid.

    Args:
        bbox: Geographic extent to cover, in degrees.
        cache_dir: Directory for cached GeoTIFFs and sidecars (created if
            missing). Keep it inside the gitignored ``.cache/``.
        source: Requested source key; see :func:`available_sources`.
        resolution_m: Desired ground resolution in metres, or ``None`` for the
            source's native resolution.
        timeout: Per-request socket timeout in seconds.
        force: Re-download even if a valid cache entry exists.
        allow_fallback: Fall through to the next source when the requested one
            cannot deliver. ``False`` makes an unavailable source an error.

    Returns:
        Path of the cached GeoTIFF.

    Note:
        Concurrent calls are safe: every fetch downloads into its own scratch
        directory, the GeoTIFF is published with an atomic ``os.replace``, and
        the sidecar -- written last -- is what marks a cache entry as complete.

    Raises:
        ValueError: On an unknown source key or an invalid ``resolution_m``.
        DemFetchError: When every source that was allowed to run has failed.
            The message lists each attempt.
    """
    if source not in _PROVIDERS:
        known = ", ".join(SOURCE_CHAIN)
        raise ValueError(f"unknown DEM source {source!r}; known sources: {known}")
    if resolution_m is not None and not (float(resolution_m) > 0.0):
        raise ValueError(f"resolution_m must be positive, got {resolution_m!r}")
    if not (float(timeout) > 0.0):
        raise ValueError(f"timeout must be positive, got {timeout!r}")

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(bbox, source, resolution_m)
    tif_path = cache_dir / f"{key}.tif"
    json_path = cache_dir / f"{key}.json"

    if not force:
        entry = _valid_cache_entry(tif_path, json_path)
        if entry is not None:
            return tif_path

    chain = [source]
    if allow_fallback:
        chain += [candidate for candidate in SOURCE_CHAIN if candidate != source]

    work = cache_dir / f".work-{key}-{uuid.uuid4().hex}"
    work.mkdir(parents=True, exist_ok=True)
    attempts: list[str] = []
    try:
        result: _FetchResult | None = None
        resolved = source
        for candidate in chain:
            try:
                result = _PROVIDERS[candidate](bbox, resolution_m, float(timeout), work / candidate)
            except DemSourceUnavailable as exc:
                attempts.append(f"{candidate}: {exc}")
                continue
            resolved = candidate
            break
        if result is None:
            raise DemFetchError(
                f"no DEM source could deliver bbox {bbox.as_tuple()}: " + " | ".join(attempts),
                attempts=attempts,
            )

        os.replace(result.path, tif_path)
        sidecar = _build_sidecar(
            key=key,
            bbox=bbox,
            requested_source=source,
            resolved_source=resolved,
            resolution_m=resolution_m,
            result=result,
            attempts=attempts,
            tif_path=tif_path,
        )
        _write_json_atomic(json_path, sidecar)
        return tif_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _valid_cache_entry(tif_path: Path, json_path: Path) -> dict[str, Any] | None:
    """Return the sidecar of a complete, plausible cache entry, else ``None``.

    The sidecar is written after the GeoTIFF and therefore acts as the commit
    marker: a ``.tif`` without a parseable sidecar is a leftover from a crashed
    or concurrent run and is simply re-fetched.
    """
    if not (tif_path.is_file() and json_path.is_file()):
        return None
    try:
        entry = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or "sha256" not in entry:
        return None
    recorded = entry.get("bytes")
    if isinstance(recorded, int) and tif_path.stat().st_size != recorded:
        return None
    return entry


def _build_sidecar(
    *,
    key: str,
    bbox: BBox,
    requested_source: str,
    resolved_source: str,
    resolution_m: float | None,
    result: _FetchResult,
    attempts: Sequence[str],
    tif_path: Path,
) -> dict[str, Any]:
    """Assemble the provenance sidecar written next to a cached GeoTIFF."""
    info = source_info(resolved_source)
    with rasterio.open(tif_path) as dataset:
        xres, yres = (float(abs(value)) for value in dataset.res)
        actual_bounds = dataset.bounds
    return {
        "schema": "ivanovo2beam.dem/1",
        "cache_key": key,
        "requested_source": requested_source,
        "source": resolved_source,
        "source_title": info.title,
        "source_attribution": info.attribution,
        "source_licence": info.licence,
        "source_requires_auth": info.requires_auth,
        "source_url_template": info.url_template,
        "source_urls": [record.url for record in result.payloads],
        "fallback_used": resolved_source != requested_source,
        "source_attempts": list(attempts),
        "fetched_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requested_bbox": {
            "west": bbox.west,
            "south": bbox.south,
            "east": bbox.east,
            "north": bbox.north,
        },
        "bbox": {
            "west": float(actual_bounds.left),
            "south": float(actual_bounds.bottom),
            "east": float(actual_bounds.right),
            "north": float(actual_bounds.top),
        },
        "crs_epsg": 4326,
        "resolution_deg": [xres, yres],
        "resolution_m": resolution_m,
        "bytes": int(tif_path.stat().st_size),
        "sha256": _sha256_file(tif_path),
        "sha256_payload": hashlib.sha256(
            "".join(sorted(record.sha256 for record in result.payloads)).encode("ascii")
        ).hexdigest(),
        "payloads": [record.as_dict() for record in result.payloads],
        "elevation_min_m": result.stats["min"],
        "elevation_max_m": result.stats["max"],
        "elevation_mean_m": result.stats["mean"],
        "provider_detail": result.detail,
        "ivanovo2beam_version": __version__,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON through a unique temporary file, then ``os.replace`` it."""
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.part")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def read_sidecar(path: Path) -> dict[str, Any]:
    """Return the provenance sidecar of a cached DEM, or ``{}`` if absent.

    Args:
        path: The GeoTIFF (or its sidecar) produced by :func:`fetch_dem`.

    Returns:
        The parsed sidecar mapping, or an empty mapping when there is none.
    """
    candidate = Path(path)
    if candidate.suffix.lower() != ".json":
        candidate = candidate.with_suffix(".json")
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #


def _pixel_inside(col: np.ndarray, row: np.ndarray, cols: int, rows: int) -> np.ndarray:
    """Return which fractional pixel-centre indices lie inside a raster.

    The single definition of "inside" used by every sampling entry point, with
    a :data:`_PIXEL_TOL` slack so that float error in the degree-to-pixel
    division cannot turn an exact corner sample into NaN.
    """
    return (
        (col >= -_PIXEL_TOL)
        & (col <= cols - 1 + _PIXEL_TOL)
        & (row >= -_PIXEL_TOL)
        & (row <= rows - 1 + _PIXEL_TOL)
    )


def _bilinear(array: np.ndarray, col: np.ndarray, row: np.ndarray) -> np.ndarray:
    """Bilinearly interpolate ``array`` at fractional ``(col, row)`` positions.

    Positions outside the array's pixel-centre extent come back as NaN, and a
    NaN in any of the four *contributing* pixels propagates (a corner with zero
    weight does not), so a sample next to a nodata hole is NaN rather than an
    average invented from three corners.

    Args:
        array: 2-D source raster.
        col: Fractional column indices (0 == first pixel centre).
        row: Fractional row indices (0 == first pixel centre).

    Returns:
        Float64 array with the same shape as ``col``/``row``.
    """
    rows, cols = array.shape
    result = np.full(col.shape, np.nan, dtype=np.float64)
    inside = _pixel_inside(col, row, cols, rows)
    if not inside.any():
        return result
    c = np.clip(col[inside], 0.0, cols - 1)
    r = np.clip(row[inside], 0.0, rows - 1)
    c0 = np.clip(np.floor(c).astype(np.intp), 0, max(cols - 2, 0))
    r0 = np.clip(np.floor(r).astype(np.intp), 0, max(rows - 2, 0))
    c1 = np.minimum(c0 + 1, cols - 1)
    r1 = np.minimum(r0 + 1, rows - 1)
    tx = c - c0
    ty = r - r0
    # Weighted sum, skipping zero-weight corners explicitly: a nodata neighbour
    # must not poison a sample that lands exactly on a valid pixel (`nan * 0`
    # is `nan`), yet must still propagate when it really contributes.
    value = np.zeros(c.shape, dtype=np.float64)
    for weight, corner in (
        ((1.0 - tx) * (1.0 - ty), array[r0, c0]),
        (tx * (1.0 - ty), array[r0, c1]),
        ((1.0 - tx) * ty, array[r1, c0]),
        (tx * ty, array[r1, c1]),
    ):
        value += np.where(weight > 0.0, corner * weight, 0.0)
    result[inside] = value
    return result


class DemReader:
    """Reads a GeoTIFF produced by :func:`fetch_dem`.

    The file must be in a geographic CRS (normally EPSG:4326) with elevations
    in metres. Its AREA_OR_POINT convention is honoured: the Copernicus COGs
    are point-registered (transform origin at a pixel centre) while everything
    this module writes is area-registered, and both are normalised to the same
    contract here:

    * :attr:`bounds` is the extent of the **pixel centres**, i.e. exactly the
      region where :meth:`sample` is defined and does not raise;
    * :meth:`sample` raises :exc:`ValueError` strictly outside that extent.

    The dataset is opened lazily on first use and cached, so reading a window of
    a 500 MB mosaic costs a window read, not a load. Use the object as a context
    manager (or call :meth:`close`) when a long-lived reader is done.

    Args:
        path: GeoTIFF written by :func:`fetch_dem` (or any compatible
            single-band geographic elevation raster).
    """

    __slots__ = (
        "_bounds",
        "_dataset",
        "_epsg",
        "_half_pixel",
        "_height",
        "_left",
        "_metadata",
        "_nodata",
        "_path",
        "_top",
        "_width",
        "_xres",
        "_yres",
    )

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        if not self._path.is_file():
            raise FileNotFoundError(f"DEM raster not found: {self._path}")
        with rasterio.open(self._path) as dataset:
            if dataset.count < 1:
                raise ValueError(f"{self._path.name}: raster has no bands")
            if dataset.crs is None or not dataset.crs.is_geographic:
                raise ValueError(f"{self._path.name}: expected a geographic CRS, got {dataset.crs}")
            epsg = dataset.crs.to_epsg()
            if epsg is None:
                raise ValueError(f"{self._path.name}: CRS {dataset.crs} has no EPSG code")
            self._epsg = int(epsg)
            self._xres, self._yres = (abs(float(value)) for value in dataset.res)
            self._width, self._height = int(dataset.width), int(dataset.height)
            self._nodata = None if dataset.nodata is None else float(dataset.nodata)
            point_registered = str(dataset.tags().get("AREA_OR_POINT", "")).strip().lower() == "point"
            self._half_pixel = 0.0 if point_registered else 0.5
            self._left = float(dataset.transform.c) + self._half_pixel * self._xres
            self._top = float(dataset.transform.f) - self._half_pixel * self._yres
        self._bounds = BBox(
            west=self._left,
            south=self._top - (self._height - 1) * self._yres,
            east=self._left + (self._width - 1) * self._xres,
            north=self._top,
        )
        self._dataset: rasterio.DatasetReader | None = None
        self._metadata = read_sidecar(self._path)

    # ------------------------------------------------------------- lifecycle

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"DemReader({self._path.name!r}, {self._width}x{self._height}, "
            f"{self._xres:.8f} deg, bounds={self._bounds.as_tuple()})"
        )

    def close(self) -> None:
        """Close the underlying dataset if it is open."""
        if self._dataset is not None:
            self._dataset.close()
            self._dataset = None

    def __enter__(self) -> DemReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def _handle(self) -> rasterio.DatasetReader:
        """The lazily opened rasterio dataset."""
        if self._dataset is None:
            self._dataset = rasterio.open(self._path)
        return self._dataset

    # ------------------------------------------------------------ properties

    @property
    def path(self) -> Path:
        """Path of the GeoTIFF this reader wraps."""
        return self._path

    @property
    def bounds(self) -> BBox:
        """Extent of the valid pixel centres, in WGS84 degrees."""
        return self._bounds

    @property
    def resolution_deg(self) -> float:
        """Mean pixel size in degrees (use :attr:`resolution_xy_deg` for the pair)."""
        return 0.5 * (self._xres + self._yres)

    @property
    def resolution_xy_deg(self) -> tuple[float, float]:
        """Pixel size as ``(x, y)`` in degrees; they differ for Copernicus tiles."""
        return (self._xres, self._yres)

    @property
    def crs_epsg(self) -> int:
        """EPSG code of the raster's CRS."""
        return self._epsg

    @property
    def shape(self) -> tuple[int, int]:
        """Raster shape as ``(height, width)`` in pixels."""
        return (self._height, self._width)

    @property
    def nodata(self) -> float | None:
        """The raster's nodata value, if it declares one."""
        return self._nodata

    @property
    def metadata(self) -> dict[str, Any]:
        """The provenance sidecar written by :func:`fetch_dem` (``{}`` if none)."""
        return dict(self._metadata)

    @property
    def source_key(self) -> str | None:
        """Key of the source that actually delivered this raster, if recorded."""
        value = self._metadata.get("source")
        return str(value) if value is not None else None

    # -------------------------------------------------------------- sampling

    def _window_values(self, col0: int, row0: int, col1: int, row1: int) -> np.ndarray:
        """Read an inclusive-exclusive pixel window as float64 with NaN holes."""
        data = self._handle.read(1, window=Window(col0, row0, col1 - col0, row1 - row0), masked=False)
        return _apply_nodata(data, self._nodata)

    def _fractional_indices(self, lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Map WGS84 degrees to fractional pixel-centre indices.

        ``self._left``/``self._top`` are already the coordinates of the first
        pixel *centre*, with the AREA_OR_POINT convention folded in, so the
        mapping is a plain scaling in both conventions.
        """
        col = (lon - self._left) / self._xres
        row = (self._top - lat) / self._yres
        return (col, row)

    def sample(self, lon: float, lat: float) -> float:
        """Return the bilinearly interpolated elevation at a WGS84 point.

        Args:
            lon: Longitude in degrees.
            lat: Latitude in degrees.

        Returns:
            Elevation in metres above sea level. NaN is returned for a point
            whose contributing pixels are nodata.

        Raises:
            ValueError: If the point lies outside :attr:`bounds`.
        """
        lon = float(lon)
        lat = float(lat)
        col, row = self._fractional_indices(np.array([lon]), np.array([lat]))
        if not bool(_pixel_inside(col, row, self._width, self._height)[0]):
            raise ValueError(f"({lon}, {lat}) is outside the DEM raster bounds {self._bounds.as_tuple()}")
        col = np.clip(col, 0.0, self._width - 1)
        row = np.clip(row, 0.0, self._height - 1)
        col0 = max(0, int(np.floor(col[0])) - 1)
        row0 = max(0, int(np.floor(row[0])) - 1)
        col1 = min(self._width, col0 + 3)
        row1 = min(self._height, row0 + 3)
        values = self._window_values(col0, row0, col1, row1)
        return float(_bilinear(values, col - col0, row - row0)[0])

    def sample_array(self, lons: Any, lats: Any) -> np.ndarray:
        """Vectorised :meth:`sample`; ``NaN`` outside the raster, shape preserved.

        Args:
            lons: Longitudes in degrees (array-like, any shape).
            lats: Latitudes in degrees, the same shape as ``lons``.

        Returns:
            Float64 array with the shape of ``lons``: metres above sea level,
            NaN outside the raster or over nodata.

        Raises:
            ValueError: If ``lons`` and ``lats`` have different shapes.
        """
        lon = np.asarray(lons, dtype=np.float64)
        lat = np.asarray(lats, dtype=np.float64)
        if lon.shape != lat.shape:
            raise ValueError(f"shape mismatch: lons {lon.shape} vs lats {lat.shape}")
        result = np.full(lon.shape, np.nan, dtype=np.float64)
        if lon.size == 0:
            return result
        flat_lon = lon.reshape(-1)
        flat_lat = lat.reshape(-1)
        col, row = self._fractional_indices(flat_lon, flat_lat)
        inside = _pixel_inside(col, row, self._width, self._height)
        if not inside.any():
            return result
        col0 = max(0, int(np.floor(col[inside].min())) - 1)
        row0 = max(0, int(np.floor(row[inside].min())) - 1)
        col1 = min(self._width, int(np.ceil(col[inside].max())) + 2)
        row1 = min(self._height, int(np.ceil(row[inside].max())) + 2)
        values = self._window_values(col0, row0, col1, row1)
        sampled = _bilinear(values, col[inside] - col0, row[inside] - row0)
        flat = result.reshape(-1)
        flat[inside] = sampled
        return result

    def window_array(self, bbox: BBox, width: int) -> tuple[np.ndarray, float]:
        """Resample the DEM onto a ``width`` x ``width`` grid covering ``bbox``.

        This is the function the terrain builder uses. The destination grid has
        ``width`` cells in each direction whose *centre* points span ``bbox``
        exactly; values come from bilinear interpolation of the source pixels,
        and everything outside the source raster is NaN.

        Because degrees are not metres, "square" is meant in the array sense: a
        bbox that is not square in degrees gets slightly rectangular cells on
        the ground. ``cell_size_metres`` is the mean of the east-west and
        north-south cell sizes measured geodesically at the bbox centre, which
        is the number to use for a metric terrain grid.

        Args:
            bbox: Extent to cover, in WGS84 degrees.
            width: Number of cells per side (``>= 2``).

        Returns:
            ``(elevations_metres, cell_size_metres)``: a float64 ``width`` x
            ``width`` array with NaN outside the raster, and the mean cell size
            in metres.

        Raises:
            ValueError: If ``width`` is not an integer >= 2, or ``bbox`` does
                not overlap the raster at all.
        """
        if isinstance(width, bool) or not isinstance(width, (int, np.integer)):
            raise ValueError(f"width must be an integer, got {width!r}")
        width = int(width)
        if width < 2:
            raise ValueError(f"width must be >= 2, got {width}")
        if not self._overlaps(bbox):
            raise ValueError(
                f"bbox {bbox.as_tuple()} does not overlap the DEM raster bounds {self._bounds.as_tuple()}"
            )
        step_x = (bbox.east - bbox.west) / width
        step_y = (bbox.north - bbox.south) / width
        lons = bbox.west + (np.arange(width, dtype=np.float64) + 0.5) * step_x
        elevations = np.full((width, width), np.nan, dtype=np.float64)
        # Sample in row blocks so that a 4096^2 request does not allocate a
        # dozen full-size temporary arrays at once.
        block = max(1, 1_048_576 // width)
        for row0 in range(0, width, block):
            row1 = min(row0 + block, width)
            lats = bbox.north - (np.arange(row0, row1, dtype=np.float64) + 0.5) * step_y
            grid_lon, grid_lat = np.meshgrid(lons, lats)
            elevations[row0:row1, :] = self.sample_array(grid_lon, grid_lat)
        span_x_m, span_y_m = _bbox_metric_span_m(bbox)
        cell_size_m = 0.5 * (span_x_m + span_y_m) / width
        return (elevations, cell_size_m)

    def _overlaps(self, bbox: BBox) -> bool:
        """Whether ``bbox`` intersects the raster's valid extent at all."""
        return not (
            bbox.east < self._bounds.west
            or bbox.west > self._bounds.east
            or bbox.north < self._bounds.south
            or bbox.south > self._bounds.north
        )
