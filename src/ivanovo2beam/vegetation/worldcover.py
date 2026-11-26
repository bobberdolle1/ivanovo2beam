"""ESA WorldCover 2021 (10 m) cached land-cover lookup and canopy polygon extraction.

Tile-naming scheme and land-cover class constants adapted from MapNG
(``services/worldCover.js``, MIT License) for ESA WorldCover 2021 v200
(CC-BY-4.0, https://esa-worldcover.org).

Because sandboxed ``.venv`` builds may lack OpenSSL / WMI DLLs for live
``/vsicurl/`` reads, land-cover grids are persisted as lightweight pure-NumPy
``.npz`` + ``.json`` pairs inside ``.cache/landcover/`` so builds and unit tests
run 100% offline with zero native GDAL dependency at load time.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely import unary_union
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.validation import make_valid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

    from ivanovo2beam.gis.projection import RegionProjection

WORLDCOVER_BUCKET = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"

#: ESA WorldCover 2021 v200 discrete class codes.
WC_TREES = 10
WC_SHRUBS = 20
WC_GRASS = 30
WC_CROPS = 40
WC_BUILT = 50
WC_BARE = 60
WC_SNOW = 70
WC_WATER = 80
WC_WETLAND = 90
WC_MANGROVES = 95
WC_MOSS = 100

#: Default cache directory relative to repo root / cwd.
DEFAULT_WORLDCOVER_CACHE_DIR = Path(".cache") / "landcover"


def worldcover_tile_name(lat: float, lon: float) -> str:
    """Return the 3x3 degree ESA WorldCover v200 GeoTIFF filename containing ``(lat, lon)``."""
    la = math.floor(lat / 3.0) * 3
    lo = math.floor(lon / 3.0) * 3
    ns = f"{'S' if la < 0 else 'N'}{abs(la):02d}"
    ew = f"{'W' if lo < 0 else 'E'}{abs(lo):03d}"
    return f"ESA_WorldCover_10m_2021_v200_{ns}{ew}_Map.tif"


@dataclass(frozen=True, slots=True)
class WorldCoverGrid:
    """North-to-south ``uint8`` ESA WorldCover grid over a geographic ``bbox``."""

    grid: NDArray[np.uint8]
    bbox: tuple[float, float, float, float]  # (west, south, east, north)

    @property
    def shape(self) -> tuple[int, int]:
        """Return ``(height, width)`` of the raster grid."""
        return (int(self.grid.shape[0]), int(self.grid.shape[1]))

    def sample_classes(
        self, lons: NDArray[np.float64], lats: NDArray[np.float64]
    ) -> NDArray[np.uint8]:
        """Sample WorldCover class codes at ``(lons, lats)`` (0 outside bounds)."""
        west, south, east, north = self.bbox
        h, w = self.shape
        out = np.zeros(lons.shape, dtype=np.uint8)
        if h == 0 or w == 0 or east <= west or north <= south:
            return out
        inside = (lons >= west) & (lons <= east) & (lats >= south) & (lats <= north)
        if not np.any(inside):
            return out
        cols = np.clip(
            np.floor(((lons[inside] - west) / (east - west)) * w).astype(np.int64), 0, w - 1
        )
        rows = np.clip(
            np.floor(((north - lats[inside]) / (north - south)) * h).astype(np.int64), 0, h - 1
        )
        out[inside] = self.grid[rows, cols]
        return out


def _resolve_cache_dirs(cache_dir: str | Path | None) -> list[Path]:
    """Return candidate landcover cache directories to probe."""
    if cache_dir is not None:
        return [Path(cache_dir)]
    candidates = [DEFAULT_WORLDCOVER_CACHE_DIR]
    repo_root = Path(__file__).resolve().parents[3]
    repo_cache = repo_root / ".cache" / "landcover"
    if repo_cache not in candidates:
        candidates.append(repo_cache)
    return candidates


def _bbox_contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    tol: float = 1e-5,
) -> bool:
    return (
        outer[0] <= inner[0] + tol
        and outer[1] <= inner[1] + tol
        and outer[2] >= inner[2] - tol
        and outer[3] >= inner[3] - tol
    )


def load_worldcover_cache(
    bbox: tuple[float, float, float, float],
    *,
    cache_dir: str | Path | None = None,
) -> WorldCoverGrid | None:
    """Load a cached ESA WorldCover ``.npz``/``.json`` covering ``bbox`` if present."""
    for d in _resolve_cache_dirs(cache_dir):
        if not d.is_dir():
            continue
        for meta_path in sorted(d.glob("*.json")):
            npz_path = meta_path.with_suffix(".npz")
            if not npz_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                raw_bbox = meta.get("bbox")
                if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
                    continue
                cached_bbox = (
                    float(raw_bbox[0]),
                    float(raw_bbox[1]),
                    float(raw_bbox[2]),
                    float(raw_bbox[3]),
                )
                if not _bbox_contains(cached_bbox, bbox):
                    continue
                with np.load(npz_path) as data:
                    grid = np.asarray(data["grid"], dtype=np.uint8)
                if grid.ndim != 2 or grid.size == 0:
                    continue
                return WorldCoverGrid(grid=grid, bbox=cached_bbox)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
    return None


def fetch_and_cache_worldcover(
    bbox: tuple[float, float, float, float],
    *,
    cache_dir: str | Path | None = None,
    timeout_s: int = 20,
) -> WorldCoverGrid | None:
    """Fetch a windowed ESA WorldCover 10m COG slice via GDAL ``/vsicurl/`` and cache it.

    Checks :func:`load_worldcover_cache` first. If no cached slice covers ``bbox``,
    attempts a windowed ``rasterio`` read from :data:`WORLDCOVER_BUCKET`; returns
    ``None`` cleanly when offline or when ``rasterio``/SSL is unavailable.
    """
    cached = load_worldcover_cache(bbox, cache_dir=cache_dir)
    if cached is not None:
        return cached

    try:  # pragma: no cover - optional network / GDAL path
        import rasterio
        from rasterio.windows import from_bounds
    except (ImportError, OSError, Exception):
        return None

    west, south, east, north = bbox
    tile = worldcover_tile_name((south + north) * 0.5, (west + east) * 0.5)
    url = f"/vsicurl/{WORLDCOVER_BUCKET}{tile}"
    try:  # pragma: no cover - network COG fetch
        with (
            rasterio.Env(
                GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_TIMEOUT=str(timeout_s),
            ),
            rasterio.open(url) as src,
        ):
            win = from_bounds(west, south, east, north, transform=src.transform)
            win = win.round_offsets().round_lengths()
            arr = src.read(1, window=win).astype(np.uint8)
            wt = src.window_transform(win)
            aw, an = wt * (0, 0)
            ae, as_ = wt * (arr.shape[1], arr.shape[0])
    except Exception:
        return None

    out_dir = _resolve_cache_dirs(cache_dir)[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{west:.5f},{south:.5f},{east:.5f},{north:.5f}".encode()).hexdigest()[:16]
    npz_path = out_dir / f"wc2021_{key}.npz"
    meta_path = out_dir / f"wc2021_{key}.json"
    np.savez_compressed(npz_path, grid=arr)
    actual_bbox = (float(aw), float(as_), float(ae), float(an))
    meta = {
        "source": tile,
        "bbox": list(actual_bbox),
        "shape": list(arr.shape),
        "orientation": "north_to_south",
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return WorldCoverGrid(grid=arr, bbox=actual_bbox)


def _extract_poly_list(geom: Any) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 0 else []
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty and g.area > 0]
    if hasattr(geom, "geoms"):
        out: list[Polygon] = []
        for part in geom.geoms:
            out.extend(_extract_poly_list(part))
        return out
    return []


def worldcover_to_forest_polygons(
    wc: WorldCoverGrid,
    projection: RegionProjection,
    *,
    clip_bounds: tuple[float, float, float, float] | None = None,
    exclude_geoms: list[Polygon] | None = None,
    min_area_m2: float = 400.0,
    simplify_tolerance_m: float = 6.0,
) -> list[Polygon]:
    """Vectorize ``WC_TREES`` cells into clean world-space forest ``Polygon``s.

    Run-length encodes contiguous horizontal tree rows into boxes, unions them
    in Shapely, subtracts existing OSM forest/farmland/meadow geometries so
    explicit OSM landuse tags stay authoritative, and drops tiny noise speckles.
    """
    west, south, east, north = wc.bbox
    h, w = wc.shape
    if h == 0 or w == 0 or east <= west or north <= south:
        return []

    r0, r1, c0, c1 = 0, h, 0, w
    clip_box: Polygon | None = None
    if clip_bounds is not None:
        cminx, cminy, cmaxx, cmaxy = clip_bounds
        clip_box = box(cminx, cminy, cmaxx, cmaxy)
        corner_lons, corner_lats = [], []
        for wx, wy in ((cminx, cminy), (cmaxx, cminy), (cmaxx, cmaxy), (cminx, cmaxy)):
            lon, lat = projection.world_to_geo(wx, wy)
            corner_lons.append(lon)
            corner_lats.append(lat)
        g_w = max(west, min(corner_lons))
        g_e = min(east, max(corner_lons))
        g_s = max(south, min(corner_lats))
        g_n = min(north, max(corner_lats))
        if g_e <= g_w or g_n <= g_s:
            return []
        c0 = max(0, math.floor(((g_w - west) / (east - west)) * w) - 1)
        c1 = min(w, math.ceil(((g_e - west) / (east - west)) * w) + 1)
        r0 = max(0, math.floor(((north - g_n) / (north - south)) * h) - 1)
        r1 = min(h, math.ceil(((north - g_s) / (north - south)) * h) + 1)
        if c1 <= c0 or r1 <= r0:
            return []

    sub = wc.grid[r0:r1, c0:c1] == WC_TREES
    if not np.any(sub):
        return []

    dlon = (east - west) / w
    dlat = (north - south) / h
    sub_h, sub_w = sub.shape
    boxes: list[Polygon] = []

    # Project cell corners for the cropped subgrid once (vectorized)
    lon_edges = west + (np.arange(c0, c1 + 1, dtype=np.float64)) * dlon
    lat_edges = north - (np.arange(r0, r1 + 1, dtype=np.float64)) * dlat
    glon, glat = np.meshgrid(lon_edges, lat_edges)
    gx, gy = projection.geo_to_world_array(glon.ravel(), glat.ravel())
    gx = gx.reshape(sub_h + 1, sub_w + 1)
    gy = gy.reshape(sub_h + 1, sub_w + 1)

    for r in range(sub_h):
        row = sub[r]
        if not np.any(row):
            continue
        padded = np.empty(sub_w + 2, dtype=np.int8)
        padded[0] = 0
        padded[1:-1] = row.astype(np.int8)
        padded[-1] = 0
        diff = np.diff(padded)
        starts = np.flatnonzero(diff == 1)
        ends = np.flatnonzero(diff == -1)
        for cs, ce in zip(starts.tolist(), ends.tolist(), strict=True):
            # Conservative axis-aligned world bounds of this pixel run
            x_min = float(min(gx[r, cs], gx[r + 1, cs]))
            x_max = float(max(gx[r, ce], gx[r + 1, ce]))
            y_min = float(min(gy[r + 1, cs], gy[r + 1, ce]))
            y_max = float(max(gy[r, cs], gy[r, ce]))
            if x_max > x_min and y_max > y_min:
                boxes.append(box(x_min, y_min, x_max, y_max))

    if not boxes:
        return []

    merged: Any = unary_union(boxes)
    if not merged.is_valid:
        merged = make_valid(merged)
    if clip_box is not None:
        merged = merged.intersection(clip_box)
        if not merged.is_valid:
            merged = make_valid(merged)

    if exclude_geoms:
        excl = unary_union([g for g in exclude_geoms if g is not None and not g.is_empty])
        if not excl.is_empty:
            merged = merged.difference(excl)
            if not merged.is_valid:
                merged = make_valid(merged)

    polys: list[Polygon] = []
    for p in _extract_poly_list(merged):
        if p.area < min_area_m2:
            continue
        if simplify_tolerance_m > 0:
            sp: Any = p.simplify(simplify_tolerance_m, preserve_topology=True)
            if not sp.is_valid:
                sp = make_valid(sp)
            for part in _extract_poly_list(sp):
                if part.area >= min_area_m2:
                    polys.append(part)
        else:
            polys.append(p)
    return polys
