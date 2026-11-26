"""Waterway and water body extraction from OpenStreetMap.

Extracts rivers, canals, streams, lakes, ponds and reservoirs from OSM data,
projects their geometry into local world coordinates, and computes realistic
hydrological elevation profiles:

1. Linear waterways (``waterway=river|canal|stream``):
   - Sample DEM along the course;
   - Smooth DEM samples with a 150 m moving-average filter to remove high-frequency
     washboard noise;
   - Enforce downstream monotonic profile: ``z_water[i] = min(z_water[i-1], z_smooth[i])``,
     guaranteeing water never flows uphill.

2. Water bodies (``natural=water``, ``water=*``, ``landuse=reservoir``):
   - Closed ways and ``type=multipolygon`` relations with outer boundaries and inner
     island rings;
   - Flat level water surface ``z_water`` derived from surrounding shoreline DEM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
    box,
)
from shapely.ops import linemerge, polygonize

from ivanovo2beam.terrain.builder import smooth_profile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

    from ivanovo2beam.gis.projection import RegionProjection
    from ivanovo2beam.osm.types import OsmData
    from ivanovo2beam.terrain.builder import DemSampler

#: Recognized linear waterways and their default metric dimensions.
WATERWAY_SPECS: dict[str, dict[str, float]] = {
    "river": {"width_m": 25.0, "depth_m": 2.5, "flow_speed": 1.2},
    "canal": {"width_m": 15.0, "depth_m": 2.0, "flow_speed": 0.5},
    "stream": {"width_m": 4.0, "depth_m": 0.8, "flow_speed": 0.8},
}

#: Default depth for lakes, ponds, and Volga reservoir.
DEFAULT_LAKE_DEPTH_M = 3.5

_NUMERIC_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:m|meter|meters)?\s*$", re.IGNORECASE)


def parse_width_m(value: str | None, default: float) -> float:
    """Parse an OSM width tag into metres, falling back to default."""
    if not value:
        return default
    m = _NUMERIC_RE.match(value)
    if m:
        try:
            val = float(m.group(1))
            if val > 0:
                return val
        except ValueError:
            pass
    return default


@dataclass(slots=True)
class RiverChannel:
    """A linear river, canal, or stream.

    Attributes:
        osm_id: OSM way ID.
        name: Name of the waterway (e.g. 'Uvod', 'Teza', 'Volga').
        waterway_type: Tag value ('river', 'canal', 'stream').
        points: (N, 2) array of (x, y) vertices in world coordinates.
        width_m: Water surface width in metres.
        depth_m: Water channel depth in metres.
        z_water: (N,) array of downstream monotonic water surface elevations.
        flow_speed: Flow velocity magnitude for BeamNG physics.
    """

    osm_id: int
    name: str
    waterway_type: str
    points: NDArray[np.float64]
    width_m: float
    depth_m: float
    z_water: NDArray[np.float64]
    flow_speed: float = 1.0

    def __post_init__(self) -> None:
        pts = np.asarray(self.points, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[1] != 2:
            raise ValueError(f"points must be (N, 2), got {pts.shape}")
        if pts.shape[0] < 2:
            raise ValueError(f"a RiverChannel needs at least 2 points, got {pts.shape[0]}")
        self.points = pts
        zw = np.asarray(self.z_water, dtype=np.float64)
        if zw.shape != (pts.shape[0],):
            raise ValueError(f"z_water length {zw.shape} does not match points {pts.shape[0]}")
        self.z_water = zw

    @property
    def length_m(self) -> float:
        """Total channel length in metres."""
        deltas = np.diff(self.points, axis=0)
        return float(np.hypot(deltas[:, 0], deltas[:, 1]).sum())


@dataclass(slots=True)
class WaterBody:
    """A standing water body (lake, pond, reservoir) with island rings.

    Attributes:
        osm_id: OSM element ID (way or relation).
        name: Water body name.
        water_type: Natural or landuse tag value ('water', 'reservoir', etc.).
        outer_ring: (N, 2) closed polygon boundary in world coordinates.
        inner_rings: List of (M, 2) closed island boundary rings.
        depth_m: Basin depth in metres.
        z_water: Flat level water surface elevation in metres.
        bounds: (min_x, min_y, max_x, max_y) bounding box.
    """

    osm_id: int
    name: str
    water_type: str
    outer_ring: NDArray[np.float64]
    inner_rings: list[NDArray[np.float64]] = field(default_factory=list)
    depth_m: float = DEFAULT_LAKE_DEPTH_M
    z_water: float = 0.0
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        outer = np.asarray(self.outer_ring, dtype=np.float64)
        if outer.ndim != 2 or outer.shape[1] != 2:
            raise ValueError(f"outer_ring must be (N, 2), got {outer.shape}")
        self.outer_ring = outer
        min_x = float(outer[:, 0].min())
        max_x = float(outer[:, 0].max())
        min_y = float(outer[:, 1].min())
        max_y = float(outer[:, 1].max())
        self.bounds = (min_x, min_y, max_x, max_y)

    @property
    def area_m2(self) -> float:
        """Area of the water body minus islands using shoelace formula."""
        x = self.outer_ring[:, 0]
        y = self.outer_ring[:, 1]
        area = 0.5 * abs(float(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1))))
        for inner in self.inner_rings:
            ix = inner[:, 0]
            iy = inner[:, 1]
            iarea = 0.5 * abs(float(np.dot(ix, np.roll(iy, 1)) - np.dot(iy, np.roll(ix, 1))))
            area = max(0.0, area - iarea)
        return area


@dataclass(slots=True)
class WaterNetwork:
    """The complete hydrology dataset for a level."""

    rivers: list[RiverChannel] = field(default_factory=list)
    water_bodies: list[WaterBody] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def total_river_length_m(self) -> float:
        return sum(r.length_m for r in self.rivers)

    @property
    def total_water_area_m2(self) -> float:
        return sum(b.area_m2 for b in self.water_bodies)


def _sample_dem_world(
    xs: NDArray[np.float64],
    ys: NDArray[np.float64],
    projection: RegionProjection,
    dem: DemSampler,
) -> NDArray[np.float64]:
    """Sample DEM elevations at world XY arrays, using fast world sampler if available."""
    if hasattr(dem, "sample_world_array"):
        return np.asarray(dem.sample_world_array(xs, ys), dtype=np.float64)
    lons, lats = projection.world_to_geo_array(xs, ys)
    return np.asarray(dem.sample_array(lons, lats), dtype=np.float64)


def _running_median(values: NDArray[np.float64], window: int = 5) -> NDArray[np.float64]:
    """Compute a centred running median with edge-shrinking symmetric windows.

    Adapted from MapNG (https://github.com/nikkiluzader/mapng), MIT License.
    """
    n = values.size
    if n < 3 or window < 3:
        return values.copy()
    half = window // 2
    out = np.empty_like(values)
    for i in range(n):
        h = min(half, i, n - 1 - i)
        out[i] = float(np.median(values[i - h : i + h + 1]))
    return out


def _is_dry_water_feature(tags: dict[str, str]) -> bool:
    """Return True if OSM tags mark a waterway/waterbody as dry, seasonal, or intermittent."""
    intermittent = tags.get("intermittent", "").strip().lower()
    seasonal = tags.get("seasonal", "").strip().lower()
    water = tags.get("water", "").strip().lower()
    regime = tags.get("water_regime", "").strip().lower()
    return (
        intermittent in {"yes", "true", "dry"}
        or seasonal in {"yes", "true"}
        or water == "dry"
        or regime in {"dry", "intermittent"}
    )


def _is_underground_waterway(tags: dict[str, str]) -> bool:
    """Return True if a linear waterway runs underground (culvert, tunnel, negative layer)."""
    tunnel = tags.get("tunnel", "").strip().lower()
    culvert = tags.get("culvert", "").strip().lower()
    location = tags.get("location", "").strip().lower()
    if tunnel in {"culvert", "yes", "flooded", "building_passage"} or culvert == "yes":
        return True
    if location == "underground":
        return True
    raw_layer = tags.get("layer", "").strip()
    if raw_layer:
        try:
            if int(raw_layer) < 0:
                return True
        except ValueError:
            pass
    return False


def compute_downstream_water_profile(
    points: NDArray[np.float64],
    projection: RegionProjection,
    dem: DemSampler | None,
    *,
    smooth_window_m: float = 150.0,
    default_elevation_m: float = 120.0,
    width_m: float = 12.0,
    surface_offset_m: float = 0.25,
) -> NDArray[np.float64]:
    """Compute smooth, downstream monotonic water elevations along a waterway.

    Probes across the channel bed (``+-3 m`` and across ``width_m``), applies a
    5-sample running median (``runningMedian(5)``), clamps long segments along
    sub-sampled interior points, enforces strictly non-increasing downstream flow
    (``med[i] = min(med[i-1], med[i])``), and places the water surface
    ``surface_offset_m`` (+0.25 m) above channel min.

    Channel probing and median filtering adapted from MapNG
    (https://github.com/nikkiluzader/mapng), MIT License.

    Args:
        points: (N, 2) world coordinates.
        projection: Region coordinate projection.
        dem: DEM sampler.
        smooth_window_m: Optional longitudinal smoothing scale in metres.
        default_elevation_m: Fallback elevation when DEM is not available.
        width_m: Channel width in metres for cross-channel probing.
        surface_offset_m: Vertical offset above channel bed minimum (default 0.25 m).

    Returns:
        (N,) array of monotonic water surface elevations.
    """
    n = points.shape[0]
    if dem is None or n < 2:
        return np.full(n, default_elevation_m, dtype=np.float64)

    # Compute unit left normals along the waterway
    deltas = np.diff(points, axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    safe_lens = np.where(seg_lens < 1e-6, 1.0, seg_lens)
    seg_tangents = deltas / safe_lens[:, None]
    tangents = np.zeros((n, 2), dtype=np.float64)
    tangents[0] = seg_tangents[0]
    tangents[-1] = seg_tangents[-1]
    if n > 2:
        mid_t = seg_tangents[:-1] + seg_tangents[1:]
        mid_l = np.hypot(mid_t[:, 0], mid_t[:, 1])
        tangents[1:-1] = np.where(mid_l[:, None] > 1e-6, mid_t / np.maximum(mid_l[:, None], 1e-9), seg_tangents[:-1])
    normals = np.column_stack((-tangents[:, 1], tangents[:, 0]))

    # Cross-channel probe at +-3m (plus centerline)
    probe_r = 3.0
    cx, cy = points[:, 0], points[:, 1]
    px_pos = cx + normals[:, 0] * probe_r
    py_pos = cy + normals[:, 1] * probe_r
    px_neg = cx - normals[:, 0] * probe_r
    py_neg = cy - normals[:, 1] * probe_r

    raw_c = _sample_dem_world(cx, cy, projection, dem)
    raw_p = _sample_dem_world(px_pos, py_pos, projection, dem)
    raw_n = _sample_dem_world(px_neg, py_neg, projection, dem)

    # Handle NaNs on centerline first
    invalid = ~np.isfinite(raw_c)
    if invalid.all():
        raw_c = np.full(n, default_elevation_m, dtype=np.float64)
    elif invalid.any():
        valid_indices = np.where(~invalid)[0]
        raw_c[invalid] = np.interp(np.where(invalid)[0], valid_indices, raw_c[valid_indices])

    raw_p = np.where(np.isfinite(raw_p), raw_p, raw_c)
    raw_n = np.where(np.isfinite(raw_n), raw_n, raw_c)
    ground = np.minimum(raw_c, np.minimum(raw_p, raw_n))

    med = _running_median(ground, window=5)

    # Clamp against lowest ground across full channel width (+1.5m bank margin) so water never floats
    half_w = max(probe_r, float(width_m) * 0.5 + 1.5)
    across_min = ground.copy()
    for frac in (-1.0, -0.5, 0.5, 1.0):
        ax = cx + normals[:, 0] * (frac * half_w)
        ay = cy + normals[:, 1] * (frac * half_w)
        az = _sample_dem_world(ax, ay, projection, dem)
        az = np.where(np.isfinite(az), az, across_min)
        across_min = np.minimum(across_min, az)
    med = np.minimum(med, across_min)

    # Probe intermediate points along segments (every ~15m, at least midpoint) so long spans
    # over curved bends or valleys cannot float > 0.5m above intermediate ground.
    for i in range(1, n):
        slen = float(seg_lens[i - 1])
        n_sub = max(2, min(16, round(slen / 15.0) + 1))
        ts = np.linspace(0.2, 0.8, max(1, n_sub - 1)) if n_sub > 2 else np.array([0.5])
        sx = cx[i - 1] + (cx[i] - cx[i - 1]) * ts
        sy = cy[i - 1] + (cy[i] - cy[i - 1]) * ts
        sz = _sample_dem_world(sx, sy, projection, dem)
        valid_sz = sz[np.isfinite(sz)]
        if valid_sz.size:
            for t_val, z_val in zip(ts, sz, strict=True):
                if not np.isfinite(z_val):
                    continue
                cap = float(z_val) + 0.5
                interp_z = float(med[i - 1] + t_val * (med[i] - med[i - 1]))
                if interp_z > cap:
                    med[i] = min(med[i], cap)
                    med[i - 1] = min(med[i - 1], cap)

    # Smooth gently if long spacing requested, while preserving channel minimum cap
    total_len = float(seg_lens.sum())
    avg_spacing = total_len / max(1, n - 1)
    if smooth_window_m > 0.0 and n >= 5:
        win = max(3, min(n, round(smooth_window_m / max(1.0, avg_spacing))))
        if win >= 3:
            med = np.minimum(med, smooth_profile(med, win))

    # Downstream monotonicity: water never flows uphill
    for i in range(1, n):
        med[i] = min(med[i - 1], med[i])

    return med + float(surface_offset_m)


def compute_lake_water_elevation(
    outer_ring: NDArray[np.float64],
    projection: RegionProjection,
    dem: DemSampler | None,
    *,
    default_elevation_m: float = 120.0,
) -> float:
    """Compute level water surface z_water for a lake/reservoir.

    Samples DEM across the interior basin (falling back to the perimeter ring for
    narrow polygons) and selects the 25th percentile so standing water sits cleanly
    inside its basin without floating above low banks or dropping below flat water.
    """
    if dem is None or outer_ring.shape[0] < 3:
        return default_elevation_m

    import shapely

    poly = Polygon(outer_ring)
    if poly.is_valid and not poly.is_empty:
        minx, miny, maxx, maxy = poly.bounds
        span = max(maxx - minx, maxy - miny)
        if span >= 6.0:
            grid_n = 16
            xs = np.linspace(minx + 0.5, maxx - 0.5, grid_n)
            ys = np.linspace(miny + 0.5, maxy - 0.5, grid_n)
            gx, gy = np.meshgrid(xs, ys)
            fx, fy = gx.ravel(), gy.ravel()
            inside = shapely.contains_xy(poly, fx, fy)
            if int(inside.sum()) >= 4:
                interior_samples = _sample_dem_world(fx[inside], fy[inside], projection, dem)
                valid_int = interior_samples[np.isfinite(interior_samples)]
                if valid_int.size >= 4:
                    return float(np.percentile(valid_int, 25))

    samples = _sample_dem_world(outer_ring[:, 0], outer_ring[:, 1], projection, dem)
    valid = samples[np.isfinite(samples)]
    if valid.size == 0:
        return default_elevation_m
    return float(np.percentile(valid, 20))


def _assemble_multipolygon(
    outer_ways: list[list[tuple[float, float]]],
    inner_ways: list[list[tuple[float, float]]],
) -> list[tuple[NDArray[np.float64], list[NDArray[np.float64]]]]:
    """Assemble outer and inner way coordinate lists into polygons with holes."""
    lines = [LineString(w) for w in outer_ways if len(w) >= 2]
    if not lines:
        return []

    merged = linemerge(lines)
    polys: list[Polygon] = []
    if merged.geom_type == "MultiLineString":
        polys = list(polygonize(merged.geoms))
    elif merged.geom_type == "LineString" and merged.is_ring:
        polys = [Polygon(merged)]
    else:
        polys = list(polygonize([merged]))

    inner_lines = [LineString(w) for w in inner_ways if len(w) >= 2]
    merged_inners = linemerge(inner_lines)
    inner_polys: list[Polygon] = []
    if merged_inners.geom_type == "MultiLineString":
        inner_polys = list(polygonize(merged_inners.geoms))
    elif merged_inners.geom_type == "LineString" and merged_inners.is_ring:
        inner_polys = [Polygon(merged_inners)]

    results: list[tuple[NDArray[np.float64], list[NDArray[np.float64]]]] = []
    for poly in polys:
        if not poly.is_valid:
            continue
        outer_coords = np.array(poly.exterior.coords, dtype=np.float64)
        holes: list[NDArray[np.float64]] = []
        for ipoly in inner_polys:
            if poly.contains(ipoly):
                holes.append(np.array(ipoly.exterior.coords, dtype=np.float64))
        results.append((outer_coords, holes))
    return results


def _dedupe_points(pts: NDArray[np.float64]) -> NDArray[np.float64]:
    """Drop consecutive duplicate points within 1 cm."""
    if pts.shape[0] < 2:
        return pts
    dists = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1]))
    keep = np.concatenate(([True], dists > 0.01))
    return pts[keep]


def _part_osm_id(base_id: int, part_idx: int) -> int:
    """Return a deterministic unique OSM-style ID for multi-part clipped geometry."""
    if part_idx == 0:
        return base_id
    return base_id * 1_000_000 + part_idx if base_id >= 0 else base_id * 1_000_000 - part_idx


def _points_bbox_disjoint(pts: NDArray[np.float64], bounds: tuple[float, float, float, float]) -> bool:
    """Return True if point array bbox is strictly outside `bounds`."""
    bx0, by0, bx1, by1 = bounds
    return bool(
        pts[:, 0].max() < bx0
        or pts[:, 0].min() > bx1
        or pts[:, 1].max() < by0
        or pts[:, 1].min() > by1
    )


def _clip_line_to_box(
    pts: NDArray[np.float64],
    clip_box: Polygon | None,
) -> list[NDArray[np.float64]]:
    """Clip a 2D polyline array to `clip_box`, returning >=2-point segments."""
    if clip_box is None:
        return [pts]
    bx0, by0, bx1, by1 = clip_box.bounds
    if _points_bbox_disjoint(pts, (bx0, by0, bx1, by1)):
        return []
    if (
        pts[:, 0].min() >= bx0
        and pts[:, 0].max() <= bx1
        and pts[:, 1].min() >= by0
        and pts[:, 1].max() <= by1
    ):
        return [pts]

    clipped = LineString(pts).intersection(clip_box)
    if clipped.is_empty:
        return []

    raw_lines: list[LineString] = []
    if isinstance(clipped, LineString):
        raw_lines = [clipped]
    elif isinstance(clipped, (MultiLineString, GeometryCollection)):
        raw_lines = [g for g in clipped.geoms if isinstance(g, LineString)]

    parts: list[NDArray[np.float64]] = []
    for line in raw_lines:
        if line.is_empty or line.length <= 0.0:
            continue
        arr = _dedupe_points(np.asarray(line.coords, dtype=np.float64)[:, :2])
        if arr.shape[0] >= 2:
            parts.append(arr)
    return parts


def _clip_polygon_to_box(poly: Polygon, clip_box: Polygon | None) -> list[Polygon]:
    """Clip a valid polygon (preserving holes) to `clip_box`."""
    if clip_box is None:
        return [poly]
    bx0, by0, bx1, by1 = clip_box.bounds
    px0, py0, px1, py1 = poly.bounds
    if px1 < bx0 or px0 > bx1 or py1 < by0 or py0 > by1:
        return []
    if px0 >= bx0 and px1 <= bx1 and py0 >= by0 and py1 <= by1:
        return [poly]
    if not poly.intersects(clip_box):
        return []

    clipped = poly.intersection(clip_box)
    if clipped.is_empty:
        return []
    if isinstance(clipped, Polygon):
        return [clipped]
    if isinstance(clipped, (MultiPolygon, GeometryCollection)):
        return [g for g in clipped.geoms if isinstance(g, Polygon) and not g.is_empty]
    return []


def _append_water_bodies(
    poly: Polygon,
    *,
    base_id: int,
    name: str,
    water_type: str,
    clip_box: Polygon | None,
    min_lake_area_m2: float,
    projection: RegionProjection,
    dem: DemSampler | None,
    water_bodies: list[WaterBody],
    part_offset: int = 0,
) -> int:
    """Clip `poly` if needed, filter by area, compute elevation, and append WaterBodies."""
    depth_m = DEFAULT_LAKE_DEPTH_M
    if "volga" in name.lower() or "волга" in name.lower():
        depth_m = 4.0

    added = 0
    for tp in _clip_polygon_to_box(poly, clip_box):
        if tp.is_empty or not tp.is_valid or tp.area < min_lake_area_m2:
            continue
        outer_coords = np.array(tp.exterior.coords, dtype=np.float64)
        if outer_coords.shape[0] < 4:
            continue
        inner_coords_list = [
            np.array(ring.coords, dtype=np.float64)
            for ring in tp.interiors
            if len(ring.coords) >= 4
        ]
        z_water = compute_lake_water_elevation(outer_coords, projection, dem)
        water_bodies.append(
            WaterBody(
                osm_id=_part_osm_id(base_id, part_offset + added),
                name=name,
                water_type=water_type,
                outer_ring=outer_coords,
                inner_rings=inner_coords_list,
                depth_m=depth_m,
                z_water=z_water,
            )
        )
        added += 1
    return added


def extract_water(
    osm_data: OsmData,
    projection: RegionProjection,
    dem: DemSampler | None = None,
    *,
    smooth_window_m: float = 150.0,
    min_lake_area_m2: float = 100.0,
    clip_bounds: tuple[float, float, float, float] | None = None,
) -> WaterNetwork:
    """Extract waterways and water bodies from OSM data.

    Args:
        osm_data: Parsed OSM extract.
        projection: World plane projection.
        dem: Optional DEM elevation source.
        smooth_window_m: River profile smoothing window in metres.
        min_lake_area_m2: Minimum area for retained water bodies.
        clip_bounds: Optional ``(min_x, min_y, max_x, max_y)`` world-metre bounds
            to clip extracted rivers and standing water polygons to.

    Returns:
        Extracted WaterNetwork.
    """
    rivers: list[RiverChannel] = []
    water_bodies: list[WaterBody] = []
    clip_box = box(*clip_bounds) if clip_bounds is not None else None

    all_ways = list(osm_data.ways.values()) if isinstance(osm_data.ways, dict) else list(osm_data.ways)
    all_relations = (
        list(osm_data.relations.values())
        if isinstance(osm_data.relations, dict)
        else list(osm_data.relations)
    )
    way_dict = {w.id: w for w in all_ways}

    # 1. Linear waterways (rivers, canals, streams)
    for way in all_ways:
        waterway = way.tags.get("waterway")
        if waterway in WATERWAY_SPECS and not way.is_area:
            if _is_dry_water_feature(way.tags) or _is_underground_waterway(way.tags):
                continue
            coords = []
            for nid in way.nodes:
                c = osm_data.node_coord(nid)
                if c is not None:
                    coords.append((c.lon, c.lat) if hasattr(c, "lon") else c)
            if len(coords) < 2:
                continue

            # Project to world coordinates
            geo_array = np.array(coords, dtype=np.float64)
            wx, wy = projection.geo_to_world_array(geo_array[:, 0], geo_array[:, 1])
            pts = _dedupe_points(np.column_stack((wx, wy)))
            if pts.shape[0] < 2:
                continue

            clipped_parts = _clip_line_to_box(pts, clip_box)
            if not clipped_parts:
                continue

            spec = WATERWAY_SPECS[waterway]
            width_m = parse_width_m(way.tags.get("width"), spec["width_m"])
            depth_m = spec["depth_m"]
            flow_speed = spec["flow_speed"]
            name = way.tags.get("name", "")

            for part_idx, part_pts in enumerate(clipped_parts):
                z_water = compute_downstream_water_profile(
                    part_pts,
                    projection,
                    dem,
                    smooth_window_m=smooth_window_m,
                    width_m=width_m,
                )

                rivers.append(
                    RiverChannel(
                        osm_id=_part_osm_id(way.id, part_idx),
                        name=name,
                        waterway_type=waterway,
                        points=part_pts,
                        width_m=width_m,
                        depth_m=depth_m,
                        z_water=z_water,
                        flow_speed=flow_speed,
                    )
                )

    # 2. Closed ways with natural=water, landuse=reservoir, waterway=riverbank
    for way in all_ways:
        natural = way.tags.get("natural")
        water = way.tags.get("water")
        waterway = way.tags.get("waterway")
        landuse = way.tags.get("landuse")

        is_water = (
            natural == "water"
            or water is not None
            or waterway in ("riverbank", "dock")
            or landuse in ("reservoir", "basin")
        )
        if is_water and way.is_closed:
            if _is_dry_water_feature(way.tags):
                continue
            coords = []
            for nid in way.nodes:
                c = osm_data.node_coord(nid)
                if c is not None:
                    coords.append((c.lon, c.lat) if hasattr(c, "lon") else c)
            if len(coords) < 4:
                continue

            geo_array = np.array(coords, dtype=np.float64)
            wx, wy = projection.geo_to_world_array(geo_array[:, 0], geo_array[:, 1])
            pts = np.column_stack((wx, wy))
            if clip_bounds is not None and _points_bbox_disjoint(pts, clip_bounds):
                continue

            poly = Polygon(pts)
            if not poly.is_valid or poly.area < min_lake_area_m2:
                continue

            name = way.tags.get("name", "")
            water_type = water or natural or landuse or waterway or "water"
            _append_water_bodies(
                poly,
                base_id=way.id,
                name=name,
                water_type=water_type,
                clip_box=clip_box,
                min_lake_area_m2=min_lake_area_m2,
                projection=projection,
                dem=dem,
                water_bodies=water_bodies,
            )

    # 3. Multipolygon relations with natural=water, etc.
    for rel in all_relations:
        if rel.tags.get("type") != "multipolygon":
            continue
        natural = rel.tags.get("natural")
        water = rel.tags.get("water")
        landuse = rel.tags.get("landuse")
        if not (natural == "water" or water is not None or landuse in ("reservoir", "basin")):
            continue

        outer_ways_pts: list[list[tuple[float, float]]] = []
        inner_ways_pts: list[list[tuple[float, float]]] = []

        for mtype, ref, role in rel.members:
            if mtype != "way":
                continue
            mway = way_dict.get(ref)
            if not mway:
                continue
            coords = []
            for nid in mway.nodes:
                c = osm_data.node_coord(nid)
                if c is not None:
                    coords.append((c.lon, c.lat) if hasattr(c, "lon") else c)
            if len(coords) < 2:
                continue
            geo_array = np.array(coords, dtype=np.float64)
            wx, wy = projection.geo_to_world_array(geo_array[:, 0], geo_array[:, 1])
            wpts = [(float(x), float(y)) for x, y in zip(wx, wy, strict=True)]

            if role == "inner":
                inner_ways_pts.append(wpts)
            else:
                outer_ways_pts.append(wpts)

        if not outer_ways_pts:
            continue

        assembled = _assemble_multipolygon(outer_ways_pts, inner_ways_pts)
        name = rel.tags.get("name", "")
        water_type = water or natural or landuse or "water"
        part_offset = 0
        for outer_coords, inner_coords_list in assembled:
            if clip_bounds is not None and _points_bbox_disjoint(outer_coords, clip_bounds):
                continue
            poly = Polygon(outer_coords, holes=[h for h in inner_coords_list if len(h) >= 4])
            if not poly.is_valid or poly.area < min_lake_area_m2:
                continue

            part_offset += _append_water_bodies(
                poly,
                base_id=rel.id,
                name=name,
                water_type=water_type,
                clip_box=clip_box,
                min_lake_area_m2=min_lake_area_m2,
                projection=projection,
                dem=dem,
                water_bodies=water_bodies,
                part_offset=part_offset,
            )

    stats = {
        "rivers_count": len(rivers),
        "total_river_length_km": round(sum(r.length_m for r in rivers) / 1000.0, 3),
        "water_bodies_count": len(water_bodies),
        "total_water_area_km2": round(sum(b.area_m2 for b in water_bodies) / 1e6, 4),
    }

    return WaterNetwork(rivers=rivers, water_bodies=water_bodies, stats=stats)
