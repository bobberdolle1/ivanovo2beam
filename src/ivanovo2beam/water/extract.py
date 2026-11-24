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
from shapely.geometry import Polygon
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


def compute_downstream_water_profile(
    points: NDArray[np.float64],
    projection: RegionProjection,
    dem: DemSampler | None,
    *,
    smooth_window_m: float = 150.0,
    default_elevation_m: float = 120.0,
) -> NDArray[np.float64]:
    """Compute smooth, downstream monotonic water elevations along a waterway.

    Smooths raw DEM elevations over `smooth_window_m` (default 150m) and then
    enforces strictly non-increasing downstream profile:
        z_water[i] = min(z_water[i-1], z_smooth[i])

    Args:
        points: (N, 2) world coordinates.
        projection: Region coordinate projection.
        dem: DEM sampler.
        smooth_window_m: Distance over which moving average smoothing is applied.
        default_elevation_m: Fallback elevation when DEM is not available.

    Returns:
        (N,) array of monotonic water surface elevations.
    """
    n = points.shape[0]
    if dem is None or n < 2:
        return np.full(n, default_elevation_m, dtype=np.float64)

    lons, lats = projection.world_to_geo_array(points[:, 0], points[:, 1])
    raw = np.asarray(dem.sample_array(lons, lats), dtype=np.float64)

    # Handle NaNs
    invalid = ~np.isfinite(raw)
    if invalid.all():
        raw = np.full(n, default_elevation_m, dtype=np.float64)
    elif invalid.any():
        valid_indices = np.where(~invalid)[0]
        raw[invalid] = np.interp(np.where(invalid)[0], valid_indices, raw[valid_indices])

    # Calculate average spacing to determine window size in samples
    deltas = np.diff(points, axis=0)
    seg_lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    total_len = float(seg_lengths.sum())
    avg_spacing = total_len / max(1, n - 1)
    window = max(3, round(smooth_window_m / max(1.0, avg_spacing)))

    smoothed = smooth_profile(raw, window)

    # Downstream monotonicity: water only flows downhill or stays level
    z_water = np.empty_like(smoothed)
    z_water[0] = smoothed[0]
    for i in range(1, n):
        z_water[i] = min(z_water[i - 1], smoothed[i])

    return z_water


def compute_lake_water_elevation(
    outer_ring: NDArray[np.float64],
    projection: RegionProjection,
    dem: DemSampler | None,
    *,
    default_elevation_m: float = 120.0,
) -> float:
    """Compute level water surface z_water for a lake/reservoir.

    Samples DEM along the perimeter and selects a representative level surface.
    """
    if dem is None or outer_ring.shape[0] < 3:
        return default_elevation_m

    lons, lats = projection.world_to_geo_array(outer_ring[:, 0], outer_ring[:, 1])
    samples = np.asarray(dem.sample_array(lons, lats), dtype=np.float64)
    valid = samples[np.isfinite(samples)]
    if valid.size == 0:
        return default_elevation_m
    # Use 10th percentile / min to sit at or slightly below the natural bank
    return float(np.percentile(valid, 10))


def _assemble_multipolygon(
    outer_ways: list[list[tuple[float, float]]],
    inner_ways: list[list[tuple[float, float]]],
) -> list[tuple[NDArray[np.float64], list[NDArray[np.float64]]]]:
    """Assemble outer and inner way coordinate lists into polygons with holes."""
    from shapely.geometry import LineString

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


def extract_water(
    osm_data: OsmData,
    projection: RegionProjection,
    dem: DemSampler | None = None,
    *,
    smooth_window_m: float = 150.0,
    min_lake_area_m2: float = 100.0,
) -> WaterNetwork:
    """Extract waterways and water bodies from OSM data.

    Args:
        osm_data: Parsed OSM extract.
        projection: World plane projection.
        dem: Optional DEM elevation source.
        smooth_window_m: River profile smoothing window in metres.
        min_lake_area_m2: Minimum area for retained water bodies.

    Returns:
        Extracted WaterNetwork.
    """
    rivers: list[RiverChannel] = []
    water_bodies: list[WaterBody] = []

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
            pts = np.column_stack((wx, wy))

            # Deduplicate consecutive identical points
            dists = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1]))
            keep = np.concatenate(([True], dists > 0.01))
            pts = pts[keep]
            if pts.shape[0] < 2:
                continue

            spec = WATERWAY_SPECS[waterway]
            width_m = parse_width_m(way.tags.get("width"), spec["width_m"])
            depth_m = spec["depth_m"]
            flow_speed = spec["flow_speed"]
            name = way.tags.get("name", "")

            z_water = compute_downstream_water_profile(
                pts,
                projection,
                dem,
                smooth_window_m=smooth_window_m,
            )

            rivers.append(
                RiverChannel(
                    osm_id=way.id,
                    name=name,
                    waterway_type=waterway,
                    points=pts,
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

            poly = Polygon(pts)
            if not poly.is_valid:
                continue
            if poly.area < min_lake_area_m2:
                continue

            closed_ring = np.array(poly.exterior.coords, dtype=np.float64)
            z_water = compute_lake_water_elevation(closed_ring, projection, dem)
            name = way.tags.get("name", "")
            water_type = water or natural or landuse or waterway or "water"

            # Check for deeper basins (e.g. Volga reservoir)
            depth_m = DEFAULT_LAKE_DEPTH_M
            if "volga" in name.lower() or "волга" in name.lower():
                depth_m = 4.0

            water_bodies.append(
                WaterBody(
                    osm_id=way.id,
                    name=name,
                    water_type=water_type,
                    outer_ring=closed_ring,
                    inner_rings=[],
                    depth_m=depth_m,
                    z_water=z_water,
                )
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
        for outer_coords, inner_coords_list in assembled:
            poly = Polygon(outer_coords, holes=[h for h in inner_coords_list if len(h) >= 4])
            if not poly.is_valid or poly.area < min_lake_area_m2:
                continue

            z_water = compute_lake_water_elevation(outer_coords, projection, dem)
            name = rel.tags.get("name", "")
            water_type = water or natural or landuse or "water"

            depth_m = DEFAULT_LAKE_DEPTH_M
            if "volga" in name.lower() or "волга" in name.lower():
                depth_m = 4.0

            water_bodies.append(
                WaterBody(
                    osm_id=rel.id,
                    name=name,
                    water_type=water_type,
                    outer_ring=outer_coords,
                    inner_rings=inner_coords_list,
                    depth_m=depth_m,
                    z_water=z_water,
                )
            )

    stats = {
        "rivers_count": len(rivers),
        "total_river_length_km": round(sum(r.length_m for r in rivers) / 1000.0, 3),
        "water_bodies_count": len(water_bodies),
        "total_water_area_km2": round(sum(b.area_m2 for b in water_bodies) / 1e6, 4),
    }

    return WaterNetwork(rivers=rivers, water_bodies=water_bodies, stats=stats)
