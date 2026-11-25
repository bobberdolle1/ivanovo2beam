"""Railways and trams subsystem for BeamNG.drive.

Handles extraction, embankment terrain grading, and 3D Collada track export:
1. Extraction: extracts ``railway=rail|light_rail|tram`` from OSM data,
   parsing standard Russian 1520mm gauge, bridges, tunnels and electrification.
2. Embankment grading: produces ``RoadCorridor`` instances with gravel ballast
   and smooth longitudinal grades limited to 3.5% (max_grade = 0.035).
3. 3D procedural rails and ballast prism (Russian 1520mm gauge) with
   ``collisionType: "Visible Mesh Final"``, flush at road grade crossings.
4. Material definition emission to ``art/shapes/railways/main.materials.json``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import numpy as np
from numpy.typing import NDArray
from shapely import LineString, STRtree
from shapely import box as shapely_box

from ivanovo2beam.beamng.level import stable_uuid
from ivanovo2beam.roads.network import _clip_points, is_truthy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ivanovo2beam.gis.projection import RegionProjection
    from ivanovo2beam.osm.types import OsmData
    from ivanovo2beam.roads.network import Road
    from ivanovo2beam.terrain.builder import RoadCorridor

#: Fixed timestamp for byte-identical deterministic COLLADA outputs.
_FIXED_TIMESTAMP = "2026-01-01T00:00:00Z"

#: Standard Russian railway gauge (1520 mm)
RUSSIAN_GAUGE_M = 1.520

#: Maximum longitudinal gradient on railways (3.5% mainline standard)
DEFAULT_MAX_RAIL_GRADE = 0.035

#: Material keys for rail steel and gravel ballast
MATERIAL_RAIL_STEEL = "ivanovo_rail_steel"
MATERIAL_RAIL_BALLAST = "ivanovo_rail_ballast"

#: Rail cross-section dimensions (standard R65 rail in metres)
RAIL_HEIGHT_M = 0.180
RAIL_HEAD_WIDTH_M = 0.075

#: Ballast prism cross-section dimensions (in metres)
BALLAST_TOP_HALF_WIDTH_M = 1.7
BALLAST_BOTTOM_HALF_WIDTH_M = 2.4
BALLAST_DEPTH_M = 0.35

#: Default spatial sector size for batched railway DAE chunks, in metres.
DEFAULT_RAIL_CHUNK_SIZE_M = 4000.0

_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


@dataclass(frozen=True, slots=True)
class Railway:
    """One OSM way interpreted as a railway or tram line, in world metres.

    Attributes:
        osm_id: The OSM way id.
        railway: Railway class (``rail``, ``light_rail``, ``tram``).
        points: (n, 2) centreline vertices in world metres.
        gauge_m: Track gauge in metres (defaults to 1.520m Russian gauge).
        electrified: Whether the track has overhead contact lines.
        bridge: Whether the way is on a bridge structure.
        tunnel: Whether the way is in a tunnel.
        layer: OSM layer integer.
        name: Name of the railway or line.
        service: Service tag (e.g. ``siding``, ``spur``, ``yard``).
        maxspeed_kph: Speed limit in km/h.
        priority: Priority for terrain grading corridors.
    """

    osm_id: int
    railway: str
    points: NDArray[np.float64]
    gauge_m: float = RUSSIAN_GAUGE_M
    electrified: bool = False
    bridge: bool = False
    tunnel: bool = False
    layer: int = 0
    name: str = ""
    service: str = ""
    maxspeed_kph: float = 80.0
    priority: int = 65

    @property
    def length_m(self) -> float:
        """Centreline length in metres."""
        if self.points.shape[0] < 2:
            return 0.0
        deltas = np.diff(self.points, axis=0)
        return float(np.hypot(deltas[:, 0], deltas[:, 1]).sum())


def parse_gauge(value: str | None) -> float:
    """Parse track gauge tag into metres, defaulting to 1.520m (Russian standard)."""
    if not value:
        return RUSSIAN_GAUGE_M
    text = value.strip().lower()
    match = _FLOAT_RE.search(text)
    if not match:
        return RUSSIAN_GAUGE_M
    val = float(match.group().replace(",", "."))
    # Millimetres (e.g. 1520, 1524, 1435, 1000)
    if val > 100.0:
        return round(val / 1000.0, 3)
    # Metres (e.g. 1.52)
    if 0.5 <= val <= 3.0:
        return round(val, 3)
    return RUSSIAN_GAUGE_M


def extract_railways(
    osm: OsmData,
    projection: RegionProjection,
    *,
    clip_bounds: tuple[float, float, float, float] | None = None,
    min_length_m: float = 1.0,
) -> list[Railway]:
    """Extract railways and tram lines from parsed OSM data.

    Args:
        osm: Parsed OSM data.
        projection: World plane coordinate transform.
        clip_bounds: (x0, y0, x1, y1) bounding box in world metres.
        min_length_m: Minimum length threshold.

    Returns:
        List of Railway objects.
    """
    railways: list[Railway] = []
    valid_classes = {"rail", "light_rail", "tram"}

    for way in osm.ways:
        rw_class = way.tags.get("railway")
        if not rw_class or rw_class not in valid_classes:
            continue

        coords = osm.way_coords(way)
        if len(coords) < 2:
            continue

        lons = np.asarray([c[0] for c in coords], dtype=np.float64)
        lats = np.asarray([c[1] for c in coords], dtype=np.float64)
        xs, ys = projection.geo_to_world_array(lons, lats)
        points = np.column_stack((xs, ys))

        clipped = _clip_points(points, clip_bounds)
        if clipped is None:
            continue

        # Filter duplicate points
        keep = np.ones(clipped.shape[0], dtype=bool)
        if clipped.shape[0] > 2:
            deltas = np.diff(clipped, axis=0)
            keep[1:] = np.hypot(deltas[:, 0], deltas[:, 1]) > 1e-6
        clipped = clipped[keep]
        if clipped.shape[0] < 2:
            continue

        gauge = parse_gauge(way.tags.get("gauge"))
        electrified = is_truthy(way.tags.get("electrified")) or (
            (way.tags.get("electrified") or "").strip().lower() in {"contact_line", "overhead"}
        )
        layer_text = way.tags.get("layer") or "0"
        layer_match = _INT_RE.search(layer_text)

        speed_text = way.tags.get("maxspeed")
        maxspeed = 80.0
        if speed_text:
            match = _FLOAT_RE.search(speed_text)
            if match:
                maxspeed = float(match.group().replace(",", "."))

        rw = Railway(
            osm_id=way.id,
            railway=rw_class,
            points=clipped,
            gauge_m=gauge,
            electrified=electrified,
            bridge=is_truthy(way.tags.get("bridge")),
            tunnel=is_truthy(way.tags.get("tunnel")),
            layer=int(layer_match.group()) if layer_match else 0,
            name=way.tags.get("name", ""),
            service=way.tags.get("service", ""),
            maxspeed_kph=maxspeed,
            priority=65,
        )

        if rw.length_m >= min_length_m:
            railways.append(rw)

    return railways


def smooth_rail_grades(
    points: NDArray[np.float64],
    raw_z: NDArray[np.float64],
    max_grade: float = DEFAULT_MAX_RAIL_GRADE,
) -> NDArray[np.float64]:
    """Smooth longitudinal elevations along track to enforce maximum railway grade."""
    n = points.shape[0]
    smoothed = np.asarray(raw_z, dtype=np.float64).copy()
    if n == 0:
        return smoothed

    # Replace any non-finite samples before min/max sweeps so NaNs do not propagate
    finite_mask = np.isfinite(smoothed)
    if not np.all(finite_mask):
        if np.any(finite_mask):
            idx_all = np.arange(n, dtype=np.float64)
            smoothed = np.interp(idx_all, idx_all[finite_mask], smoothed[finite_mask])
        else:
            smoothed.fill(0.0)

    if n < 2:
        return smoothed

    deltas = np.diff(points[:, :2], axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])

    # Forward pass
    for i in range(1, n):
        ds = float(seg_lens[i - 1])
        max_dz = max_grade * ds
        smoothed[i] = min(smoothed[i], smoothed[i - 1] + max_dz)
        smoothed[i] = max(smoothed[i], smoothed[i - 1] - max_dz)

    # Backward pass
    for i in range(n - 2, -1, -1):
        ds = float(seg_lens[i])
        max_dz = max_grade * ds
        smoothed[i] = min(smoothed[i], smoothed[i + 1] + max_dz)
        smoothed[i] = max(smoothed[i], smoothed[i + 1] - max_dz)

    return smoothed


def railways_to_corridors(
    railways: Sequence[Railway],
    *,
    build: object | None = None,
    ballast_half_width_m: float = 2.5,
    corridor_half_width_m: float = 7.0,
    max_grade: float = DEFAULT_MAX_RAIL_GRADE,
) -> list[RoadCorridor]:
    """Build terrain corridors for railways with gravel ballast and grade smoothing.

    Street-running tramways (``railway == "tram"``) are skipped so they do not
    overwrite paved city streets with gravel embankments.
    """
    from ivanovo2beam.roads.build import sample_surface_z
    from ivanovo2beam.terrain.builder import RoadCorridor

    corridors: list[RoadCorridor] = []
    for rw in railways:
        if rw.railway == "tram":
            continue

        surface_z: np.ndarray | None = None
        if build is not None and not (rw.bridge or rw.tunnel):
            try:
                # Mock a road-like object with rw.points for sample_surface_z
                class _Centerline:
                    points = rw.points

                raw_z = sample_surface_z(_Centerline(), build)  # type: ignore[arg-type]
                surface_z = smooth_rail_grades(rw.points, raw_z, max_grade=max_grade)
            except (AttributeError, IndexError, TypeError, ValueError):  # pragma: no cover
                surface_z = None

        corridors.append(
            RoadCorridor(
                points=rw.points,
                half_width_m=ballast_half_width_m,
                corridor_half_width_m=corridor_half_width_m,
                priority=rw.priority,
                surface_z=surface_z,
                grade_terrain=not (rw.bridge or rw.tunnel),
                material_key="Gravel",
                shoulder_material_key="Dirt",
                key=f"railway_way{rw.osm_id}",
            )
        )
    return corridors


def _quad_face_normal(
    v0: tuple[float, float, float],
    v1: tuple[float, float, float],
    v2: tuple[float, float, float],
    fallback: tuple[float, float, float] = (0.0, 0.0, 1.0),
) -> tuple[float, float, float]:
    """Compute unit outward normal for a CCW quad ``(v0, v1, v2, v3)``."""
    ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
    bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
    nx = ay * bz - az * by
    ny = az * bx - ax * bz
    nz = ax * by - ay * bx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-9:
        return fallback
    return (nx / length, ny / length, nz / length)


@dataclass(slots=True)
class RailwayMesh:
    """Multi-material 3D mesh container for procedural railway tracks."""

    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    # Triangles grouped by material name
    ballast_triangles: list[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=list
    )
    steel_triangles: list[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=list
    )

    def add_triangle(
        self,
        v0: tuple[float, float, float],
        v1: tuple[float, float, float],
        v2: tuple[float, float, float],
        n: tuple[float, float, float],
        material: str,
        uv0: tuple[float, float] = (0.0, 0.0),
        uv1: tuple[float, float] = (1.0, 0.0),
        uv2: tuple[float, float] = (0.0, 1.0),
    ) -> None:
        """Add a single triangle with specified material."""
        idx = len(self.positions)
        self.positions.extend([v0, v1, v2])
        self.normals.extend([n, n, n])
        self.uvs.extend([uv0, uv1, uv2])
        tri = ((idx, idx, idx), (idx + 1, idx + 1, idx + 1), (idx + 2, idx + 2, idx + 2))
        if material == MATERIAL_RAIL_BALLAST:
            self.ballast_triangles.append(tri)
        else:
            self.steel_triangles.append(tri)

    def add_quad(
        self,
        v0: tuple[float, float, float],
        v1: tuple[float, float, float],
        v2: tuple[float, float, float],
        v3: tuple[float, float, float],
        n: tuple[float, float, float],
        material: str,
        uv0: tuple[float, float] = (0.0, 0.0),
        uv1: tuple[float, float] = (1.0, 0.0),
        uv2: tuple[float, float] = (1.0, 1.0),
        uv3: tuple[float, float] = (0.0, 1.0),
    ) -> None:
        """Add a quad split into 2 triangles with specified material."""
        idx = len(self.positions)
        self.positions.extend([v0, v1, v2, v3])
        self.normals.extend([n, n, n, n])
        self.uvs.extend([uv0, uv1, uv2, uv3])
        tri1 = ((idx, idx, idx), (idx + 1, idx + 1, idx + 1), (idx + 2, idx + 2, idx + 2))
        tri2 = ((idx, idx, idx), (idx + 2, idx + 2, idx + 2), (idx + 3, idx + 3, idx + 3))
        if material == MATERIAL_RAIL_BALLAST:
            self.ballast_triangles.extend([tri1, tri2])
        else:
            self.steel_triangles.extend([tri1, tri2])

    def add_box(
        self,
        c000: tuple[float, float, float],
        c100: tuple[float, float, float],
        c110: tuple[float, float, float],
        c010: tuple[float, float, float],
        c001: tuple[float, float, float],
        c101: tuple[float, float, float],
        c111: tuple[float, float, float],
        c011: tuple[float, float, float],
        material: str,
    ) -> None:
        """Add a 6-sided solid box."""
        faces = (
            (c001, c101, c111, c011, (0.0, 0.0, 1.0)),
            (c000, c010, c110, c100, (0.0, 0.0, -1.0)),
            (c000, c100, c101, c001, (0.0, -1.0, 0.0)),
            (c110, c010, c011, c111, (0.0, 1.0, 0.0)),
            (c010, c000, c001, c011, (-1.0, 0.0, 0.0)),
            (c100, c110, c111, c101, (1.0, 0.0, 0.0)),
        )
        for v0, v1, v2, v3, fallback in faces:
            self.add_quad(v0, v1, v2, v3, _quad_face_normal(v0, v1, v2, fallback), material)

    def merge(self, other: RailwayMesh, *, offset_xy: tuple[float, float] = (0.0, 0.0)) -> None:
        """Append another ``RailwayMesh`` into this mesh, subtracting ``offset_xy``."""
        if not other.positions:
            return
        base = len(self.positions)
        ox, oy = float(offset_xy[0]), float(offset_xy[1])
        if ox == 0.0 and oy == 0.0:
            self.positions.extend(other.positions)
        else:
            self.positions.extend((p[0] - ox, p[1] - oy, p[2]) for p in other.positions)
        self.normals.extend(other.normals)
        self.uvs.extend(other.uvs)
        self.ballast_triangles.extend(
            (
                (v0 + base, n0 + base, uv0 + base),
                (v1 + base, n1 + base, uv1 + base),
                (v2 + base, n2 + base, uv2 + base),
            )
            for (v0, n0, uv0), (v1, n1, uv1), (v2, n2, uv2) in other.ballast_triangles
        )
        self.steel_triangles.extend(
            (
                (v0 + base, n0 + base, uv0 + base),
                (v1 + base, n1 + base, uv1 + base),
                (v2 + base, n2 + base, uv2 + base),
            )
            for (v0, n0, uv0), (v1, n1, uv1), (v2, n2, uv2) in other.steel_triangles
        )


@dataclass(slots=True)
class RoadCrossingIndex:
    """Spatial index over active at-grade roads for fast crossing queries."""

    roads: tuple[Road, ...]
    bboxes: NDArray[np.float64]
    max_pad_m: float
    tree: STRtree | None

    @classmethod
    def from_roads(cls, roads: Sequence[Road] | None) -> RoadCrossingIndex:
        """Precompute bounding boxes and an ``STRtree`` for at-grade roads."""
        if not roads:
            return cls(roads=(), bboxes=np.empty((0, 4), dtype=np.float64), max_pad_m=0.0, tree=None)

        active: list[Road] = []
        geoms: list[LineString] = []
        bbox_rows: list[tuple[float, float, float, float]] = []
        max_pad = 0.0

        for road in roads:
            if not road.drivable or road.tunnel or road.bridge or road.points.shape[0] < 2:
                continue
            pts = road.points
            min_x = float(pts[:, 0].min())
            max_x = float(pts[:, 0].max())
            min_y = float(pts[:, 1].min())
            max_y = float(pts[:, 1].max())
            active.append(road)
            geoms.append(LineString(pts))
            bbox_rows.append((min_x, min_y, max_x, max_y))
            pad = float(road.half_width_m + 1.0)
            if pad > max_pad:
                max_pad = pad

        if not active:
            return cls(roads=(), bboxes=np.empty((0, 4), dtype=np.float64), max_pad_m=0.0, tree=None)

        return cls(
            roads=tuple(active),
            bboxes=np.asarray(bbox_rows, dtype=np.float64),
            max_pad_m=max_pad,
            tree=STRtree(geoms),
        )

    def query_point(self, px: float, py: float) -> list[int]:
        """Return candidate road indices whose buffered bounds contain ``(px, py)``."""
        if self.tree is None:
            return []
        pad = self.max_pad_m
        hits = self.tree.query(shapely_box(px - pad, py - pad, px + pad, py + pad))
        return [int(idx) for idx in hits]

    def query_box(self, min_x: float, min_y: float, max_x: float, max_y: float) -> list[int]:
        """Return candidate road indices intersecting a query bounding box."""
        if self.tree is None:
            return []
        pad = self.max_pad_m
        hits = self.tree.query(shapely_box(min_x - pad, min_y - pad, max_x + pad, max_y + pad))
        return [int(idx) for idx in hits]


_CROSSING_INDEX_CACHE: dict[int, tuple[tuple[int, ...], RoadCrossingIndex]] = {}


def _get_road_crossing_index(roads: Sequence[Road] | None) -> RoadCrossingIndex | None:
    """Return a cached :class:`RoadCrossingIndex` for ``roads`` when possible."""
    if not roads:
        return None
    sig = tuple(id(r) for r in roads)
    key = id(roads)
    cached = _CROSSING_INDEX_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    index = RoadCrossingIndex.from_roads(roads)
    if len(_CROSSING_INDEX_CACHE) >= 4:
        _CROSSING_INDEX_CACHE.clear()
    _CROSSING_INDEX_CACHE[key] = (sig, index)
    return index


def is_crossing_road(
    point: tuple[float, float],
    roads: Sequence[Road] | None,
    *,
    road_index: RoadCrossingIndex | None = None,
) -> bool:
    """Check whether a 2D point lies within an at-grade intersecting road carriageway."""
    idx = road_index if road_index is not None else _get_road_crossing_index(roads)
    if idx is None or idx.tree is None:
        return False
    px, py = point
    for road_i in idx.query_point(px, py):
        road = idx.roads[road_i]
        pad = road.half_width_m + 1.0
        min_x, min_y, max_x, max_y = idx.bboxes[road_i]
        if px < min_x - pad or px > max_x + pad or py < min_y - pad or py > max_y + pad:
            continue
        r_pts = road.points
        min_dist = float("inf")
        for k in range(r_pts.shape[0] - 1):
            ax, ay = float(r_pts[k, 0]), float(r_pts[k, 1])
            bx, by = float(r_pts[k + 1, 0]), float(r_pts[k + 1, 1])
            dx, dy = bx - ax, by - ay
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 1e-8:
                dist = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))
                dist = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if dist < min_dist:
                min_dist = dist
        if min_dist <= pad:
            return True
    return False


def _segment_road_crossing_ts(
    p0: tuple[float, float],
    p1: tuple[float, float],
    roads: Sequence[Road] | None,
    *,
    road_index: RoadCrossingIndex | None = None,
) -> list[float]:
    """Find parameter values ``t in (0, 1)`` where segment ``p0->p1`` enters/exits road corridors."""
    idx = road_index if road_index is not None else _get_road_crossing_index(roads)
    if idx is None or idx.tree is None:
        return []

    ax, ay = p0
    bx, by = p1
    sx, sy = bx - ax, by - ay
    seg_len = math.hypot(sx, sy)
    if seg_len < 1e-6:
        return []

    min_x, max_x = min(ax, bx), max(ax, bx)
    min_y, max_y = min(ay, by), max(ay, by)

    ts: list[float] = []
    for road_i in idx.query_box(min_x, min_y, max_x, max_y):
        road = idx.roads[road_i]
        pad = road.half_width_m + 1.0
        r_min_x, r_min_y, r_max_x, r_max_y = idx.bboxes[road_i]
        if r_max_x < min_x - pad or r_min_x > max_x + pad:
            continue
        if r_max_y < min_y - pad or r_min_y > max_y + pad:
            continue

        r_pts = road.points
        pad_t = pad / seg_len
        for k in range(r_pts.shape[0] - 1):
            cx, cy = float(r_pts[k, 0]), float(r_pts[k, 1])
            dx, dy = float(r_pts[k + 1, 0]), float(r_pts[k + 1, 1])
            rx, ry = dx - cx, dy - cy
            denom = sx * ry - sy * rx
            if abs(denom) < 1e-9:
                continue
            qpx, qpy = cx - ax, cy - ay
            t = (qpx * ry - qpy * rx) / denom
            u = (qpx * sy - qpy * sx) / denom
            if -0.05 <= u <= 1.05 and -pad_t <= t <= 1.0 + pad_t:
                for cand in (t - pad_t, t, t + pad_t):
                    if 1e-3 < cand < 1.0 - 1e-3:
                        ts.append(cand)
    return sorted(ts)


def _subdivide_at_road_crossings(
    pts: NDArray[np.float64],
    base_z: NDArray[np.float64],
    roads: Sequence[Road] | None,
    *,
    road_index: RoadCrossingIndex | None = None,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Insert intermediate vertices around road intersections so crossings sit flush."""
    idx = road_index if road_index is not None else _get_road_crossing_index(roads)
    if idx is None or idx.tree is None or pts.shape[0] < 2:
        return pts, base_z

    out_pts: list[tuple[float, float]] = [(float(pts[0, 0]), float(pts[0, 1]))]
    out_z: list[float] = [float(base_z[0])]
    for i in range(pts.shape[0] - 1):
        p0 = (float(pts[i, 0]), float(pts[i, 1]))
        p1 = (float(pts[i + 1, 0]), float(pts[i + 1, 1]))
        z0, z1 = float(base_z[i]), float(base_z[i + 1])
        ts = _segment_road_crossing_ts(p0, p1, roads, road_index=idx)
        last_t = 0.0
        for t in ts:
            if t - last_t > 1e-3 and 1.0 - t > 1e-3:
                out_pts.append((p0[0] + t * (p1[0] - p0[0]), p0[1] + t * (p1[1] - p0[1])))
                out_z.append(z0 + t * (z1 - z0))
                last_t = t
        out_pts.append(p1)
        out_z.append(z1)
    return np.asarray(out_pts, dtype=np.float64), np.asarray(out_z, dtype=np.float64)


def build_railway_geometry(
    railway: Railway,
    z_profile: np.ndarray | None = None,
    ground_sampler: Callable[[float, float], float] | None = None,
    roads: Sequence[Road] | None = None,
    *,
    road_index: RoadCrossingIndex | None = None,
) -> RailwayMesh:
    """Generate 3D rails geometry (Russian 1520mm gauge) + ballast prism.

    At road crossings, rails and ballast are flush with the road carriageway surface.

    Args:
        railway: Railway line to extrude.
        z_profile: Pre-computed elevations along points.
        ground_sampler: Ground elevation query function.
        roads: Road network for grade-crossing detection.
        road_index: Optional precomputed :class:`RoadCrossingIndex`.

    Returns:
        RailwayMesh containing ballast and steel geometry.
    """
    mesh = RailwayMesh()
    pts = railway.points
    n = pts.shape[0]
    if n < 2:
        return mesh

    if z_profile is not None and len(z_profile) == n:
        base_z = np.asarray(z_profile, dtype=np.float64)
    elif ground_sampler is not None:
        base_z = np.array([ground_sampler(float(p[0]), float(p[1])) for p in pts], dtype=np.float64)
    else:
        base_z = np.zeros(n, dtype=np.float64)

    idx = road_index if road_index is not None else _get_road_crossing_index(roads)
    if idx is not None and idx.tree is not None and not railway.bridge:
        pts, base_z = _subdivide_at_road_crossings(pts, base_z, roads, road_index=idx)
        if z_profile is None and ground_sampler is not None:
            base_z = np.array([ground_sampler(float(p[0]), float(p[1])) for p in pts], dtype=np.float64)
        n = pts.shape[0]

    diffs = np.diff(pts, axis=0)
    raw_seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])
    safe_seg_lens = np.where(raw_seg_lens < 1e-6, 1.0, raw_seg_lens)
    seg_tangents = diffs / safe_seg_lens[:, None]

    normals = np.zeros((n, 2), dtype=np.float64)
    normals[0] = [-seg_tangents[0, 1], seg_tangents[0, 0]]
    normals[-1] = [-seg_tangents[-1, 1], seg_tangents[-1, 0]]
    if n > 2:
        mid_tangents = (seg_tangents[:-1] + seg_tangents[1:]) * 0.5
        mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
        degenerate = mid_lens < 1e-6
        if np.any(degenerate):
            mid_tangents[degenerate] = seg_tangents[:-1][degenerate]
            mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
            mid_lens = np.where(mid_lens < 1e-6, 1.0, mid_lens)
        mid_tangents /= mid_lens[:, None]
        normals[1:-1] = np.column_stack((-mid_tangents[:, 1], mid_tangents[:, 0]))

    # Detect crossing status per vertex (skipped if railway itself is on a bridge)
    if railway.bridge or idx is None or idx.tree is None:
        crossing_flags = [False] * n
    else:
        crossing_flags = [
            is_crossing_road((float(p[0]), float(p[1])), None, road_index=idx) for p in pts
        ]

    rail_offset = railway.gauge_m * 0.5
    half_head = RAIL_HEAD_WIDTH_M * 0.5

    # 1. Ballast Prism (trapezoid beneath track; suppressed across road crossings)
    cum_s = 0.0
    for i in range(n - 1):
        ds = float(raw_seg_lens[i])
        s0, s1 = cum_s * 0.25, (cum_s + ds) * 0.25
        cum_s += ds
        if crossing_flags[i] and crossing_flags[i + 1]:
            continue

        p0x, p0y = float(pts[i, 0]), float(pts[i, 1])
        p1x, p1y = float(pts[i + 1, 0]), float(pts[i + 1, 1])
        n0x, n0y = float(normals[i, 0]), float(normals[i, 1])
        n1x, n1y = float(normals[i + 1, 0]), float(normals[i + 1, 1])

        z0_top = float(base_z[i])
        z1_top = float(base_z[i + 1])
        z0_bot = z0_top - BALLAST_DEPTH_M
        z1_bot = z1_top - BALLAST_DEPTH_M

        # Ballast top corners (-N and +N)
        bt_l0 = (p0x - BALLAST_TOP_HALF_WIDTH_M * n0x, p0y - BALLAST_TOP_HALF_WIDTH_M * n0y, z0_top)
        bt_r0 = (p0x + BALLAST_TOP_HALF_WIDTH_M * n0x, p0y + BALLAST_TOP_HALF_WIDTH_M * n0y, z0_top)
        bt_r1 = (p1x + BALLAST_TOP_HALF_WIDTH_M * n1x, p1y + BALLAST_TOP_HALF_WIDTH_M * n1y, z1_top)
        bt_l1 = (p1x - BALLAST_TOP_HALF_WIDTH_M * n1x, p1y - BALLAST_TOP_HALF_WIDTH_M * n1y, z1_top)

        # Ballast bottom corners (-N and +N)
        bb_l0 = (p0x - BALLAST_BOTTOM_HALF_WIDTH_M * n0x, p0y - BALLAST_BOTTOM_HALF_WIDTH_M * n0y, z0_bot)
        bb_r0 = (p0x + BALLAST_BOTTOM_HALF_WIDTH_M * n0x, p0y + BALLAST_BOTTOM_HALF_WIDTH_M * n0y, z0_bot)
        bb_r1 = (p1x + BALLAST_BOTTOM_HALF_WIDTH_M * n1x, p1y + BALLAST_BOTTOM_HALF_WIDTH_M * n1y, z1_bot)
        bb_l1 = (p1x - BALLAST_BOTTOM_HALF_WIDTH_M * n1x, p1y - BALLAST_BOTTOM_HALF_WIDTH_M * n1y, z1_bot)

        # Ballast top (+Z, CCW outward)
        mesh.add_quad(
            bt_l0, bt_l1, bt_r1, bt_r0,
            _quad_face_normal(bt_l0, bt_l1, bt_r1, (0.0, 0.0, 1.0)),
            MATERIAL_RAIL_BALLAST,
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )
        # Ballast -N slope (outward -N / +Z)
        mesh.add_quad(
            bb_l0, bb_l1, bt_l1, bt_l0,
            _quad_face_normal(bb_l0, bb_l1, bt_l1, (-n0x, -n0y, 0.0)),
            MATERIAL_RAIL_BALLAST,
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )
        # Ballast +N slope (outward +N / +Z)
        mesh.add_quad(
            bt_r0, bt_r1, bb_r1, bb_r0,
            _quad_face_normal(bt_r0, bt_r1, bb_r1, (n0x, n0y, 0.0)),
            MATERIAL_RAIL_BALLAST,
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )

    # 2. Steel Rails (-rail_offset and +rail_offset)
    cum_s = 0.0
    for i in range(n - 1):
        ds = float(raw_seg_lens[i])
        s0, s1 = cum_s * 0.25, (cum_s + ds) * 0.25
        cum_s += ds

        p0x, p0y = float(pts[i, 0]), float(pts[i, 1])
        p1x, p1y = float(pts[i + 1, 0]), float(pts[i + 1, 1])
        n0x, n0y = float(normals[i, 0]), float(normals[i, 1])
        n1x, n1y = float(normals[i + 1, 0]), float(normals[i + 1, 1])

        z0_base = float(base_z[i])
        z1_base = float(base_z[i + 1])

        # At road crossings, rail height is flush with the road (0 height above surface)
        h0 = 0.0 if crossing_flags[i] else RAIL_HEIGHT_M
        h1 = 0.0 if crossing_flags[i + 1] else RAIL_HEIGHT_M

        for offset in (-rail_offset, rail_offset):
            c_bot_l0 = (p0x + (offset - half_head) * n0x, p0y + (offset - half_head) * n0y, z0_base)
            c_bot_r0 = (p0x + (offset + half_head) * n0x, p0y + (offset + half_head) * n0y, z0_base)
            c_top_r0 = (p0x + (offset + half_head) * n0x, p0y + (offset + half_head) * n0y, z0_base + h0)
            c_top_l0 = (p0x + (offset - half_head) * n0x, p0y + (offset - half_head) * n0y, z0_base + h0)

            c_bot_l1 = (p1x + (offset - half_head) * n1x, p1y + (offset - half_head) * n1y, z1_base)
            c_bot_r1 = (p1x + (offset + half_head) * n1x, p1y + (offset + half_head) * n1y, z1_base)
            c_top_r1 = (p1x + (offset + half_head) * n1x, p1y + (offset + half_head) * n1y, z1_base + h1)
            c_top_l1 = (p1x + (offset - half_head) * n1x, p1y + (offset - half_head) * n1y, z1_base + h1)

            # Top running surface (+Z, CCW outward)
            mesh.add_quad(
                c_top_l0, c_top_l1, c_top_r1, c_top_r0,
                _quad_face_normal(c_top_l0, c_top_l1, c_top_r1, (0.0, 0.0, 1.0)),
                MATERIAL_RAIL_STEEL,
                (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
            )
            if h0 > 1e-6 and h1 > 1e-6:
                # -N side (outward -N)
                mesh.add_quad(
                    c_bot_l0, c_bot_l1, c_top_l1, c_top_l0,
                    _quad_face_normal(c_bot_l0, c_bot_l1, c_top_l1, (-n0x, -n0y, 0.0)),
                    MATERIAL_RAIL_STEEL,
                    (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
                )
                # +N side (outward +N)
                mesh.add_quad(
                    c_top_r0, c_top_r1, c_bot_r1, c_bot_r0,
                    _quad_face_normal(c_top_r0, c_top_r1, c_bot_r1, (n0x, n0y, 0.0)),
                    MATERIAL_RAIL_STEEL,
                    (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
                )
            elif h0 > 1e-6:
                # Tapered down to flush crossing at i+1 (single triangle per side)
                mesh.add_triangle(
                    c_bot_l0, c_bot_l1, c_top_l0,
                    _quad_face_normal(c_bot_l0, c_bot_l1, c_top_l0, (-n0x, -n0y, 0.0)),
                    MATERIAL_RAIL_STEEL,
                    (0.0, s0), (0.0, s1), (1.0, s0),
                )
                mesh.add_triangle(
                    c_top_r0, c_top_r1, c_bot_r0,
                    _quad_face_normal(c_top_r0, c_top_r1, c_bot_r0, (n0x, n0y, 0.0)),
                    MATERIAL_RAIL_STEEL,
                    (0.0, s0), (0.0, s1), (1.0, s0),
                )
            elif h1 > 1e-6:
                # Tapered up from flush crossing at i (single triangle per side)
                mesh.add_triangle(
                    c_bot_l0, c_bot_l1, c_top_l1,
                    _quad_face_normal(c_bot_l0, c_bot_l1, c_top_l1, (-n0x, -n0y, 0.0)),
                    MATERIAL_RAIL_STEEL,
                    (0.0, s0), (0.0, s1), (1.0, s1),
                )
                mesh.add_triangle(
                    c_top_r0, c_top_r1, c_bot_r1,
                    _quad_face_normal(c_top_r0, c_top_r1, c_bot_r1, (n0x, n0y, 0.0)),
                    MATERIAL_RAIL_STEEL,
                    (0.0, s0), (0.0, s1), (1.0, s1),
                )

    return mesh


def _format_floats(values: Sequence[float]) -> str:
    """Format a sequence of floats compactly."""
    return " ".join(f"{v:.4f}".rstrip("0").rstrip(".") or "0" for v in values)


def export_railway_dae(path: Path, mesh: RailwayMesh, shape_name: str) -> int:
    """Export RailwayMesh into a BeamNG-compatible multi-material COLLADA 1.4.1 file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    pos_flat = [c for p in mesh.positions for c in p]
    norm_flat = [c for n in mesh.normals for c in n]
    uv_flat = [c for uv in mesh.uvs for c in uv]

    pos_str = _format_floats(pos_flat)
    norm_str = _format_floats(norm_flat)
    uv_str = _format_floats(uv_flat)

    p_ballast = " ".join(f"{vi} {ni} {uvi}" for tri in mesh.ballast_triangles for vi, ni, uvi in tri)
    p_steel = " ".join(f"{vi} {ni} {uvi}" for tri in mesh.steel_triangles for vi, ni, uvi in tri)

    safe_name = escape(shape_name)

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor>
      <author>ivanovo2beam</author>
      <authoring_tool>ivanovo2beam railway generator</authoring_tool>
    </contributor>
    <created>{_FIXED_TIMESTAMP}</created>
    <modified>{_FIXED_TIMESTAMP}</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="{safe_name}-ballast-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert><diffuse><color>0.5 0.5 0.5 1</color></diffuse></lambert>
        </technique>
      </profile_COMMON>
    </effect>
    <effect id="{safe_name}-steel-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert><diffuse><color>0.8 0.8 0.85 1</color></diffuse></lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{MATERIAL_RAIL_BALLAST}" name="{MATERIAL_RAIL_BALLAST}">
      <instance_effect url="#{safe_name}-ballast-effect"/>
    </material>
    <material id="{MATERIAL_RAIL_STEEL}" name="{MATERIAL_RAIL_STEEL}">
      <instance_effect url="#{safe_name}-steel-effect"/>
    </material>
  </library_materials>
  <library_geometries>
    <geometry id="{safe_name}-mesh" name="{safe_name}">
      <mesh>
        <source id="{safe_name}-mesh-positions">
          <float_array id="{safe_name}-mesh-positions-array" count="{len(pos_flat)}">{pos_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-positions-array" count="{len(mesh.positions)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{safe_name}-mesh-normals">
          <float_array id="{safe_name}-mesh-normals-array" count="{len(norm_flat)}">{norm_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-normals-array" count="{len(mesh.normals)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{safe_name}-mesh-map-0">
          <float_array id="{safe_name}-mesh-map-0-array" count="{len(uv_flat)}">{uv_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-map-0-array" count="{len(mesh.uvs)}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{safe_name}-mesh-vertices">
          <input semantic="POSITION" source="#{safe_name}-mesh-positions"/>
        </vertices>
        <triangles material="{MATERIAL_RAIL_BALLAST}" count="{len(mesh.ballast_triangles)}">
          <input semantic="VERTEX" source="#{safe_name}-mesh-vertices" offset="0"/>
          <input semantic="NORMAL" source="#{safe_name}-mesh-normals" offset="1"/>
          <input semantic="TEXCOORD" source="#{safe_name}-mesh-map-0" offset="2" set="0"/>
          <p>{p_ballast}</p>
        </triangles>
        <triangles material="{MATERIAL_RAIL_STEEL}" count="{len(mesh.steel_triangles)}">
          <input semantic="VERTEX" source="#{safe_name}-mesh-vertices" offset="0"/>
          <input semantic="NORMAL" source="#{safe_name}-mesh-normals" offset="1"/>
          <input semantic="TEXCOORD" source="#{safe_name}-mesh-map-0" offset="2" set="0"/>
          <p>{p_steel}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="{safe_name}" name="{safe_name}" type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <instance_geometry url="#{safe_name}-mesh" name="{safe_name}">
          <bind_material>
            <technique_common>
              <instance_material symbol="{MATERIAL_RAIL_BALLAST}" target="#{MATERIAL_RAIL_BALLAST}"/>
              <instance_material symbol="{MATERIAL_RAIL_STEEL}" target="#{MATERIAL_RAIL_STEEL}"/>
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#Scene"/>
  </scene>
</COLLADA>
"""
    data = xml.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def emit_railways_materials(level_dir: Path, level_id: str) -> Path:
    """Emit ``levels/<id>/art/shapes/railways/main.materials.json``.

    Maps rail steel and ballast to native BeamNG PBR textures.
    """
    materials_path = level_dir / "art" / "shapes" / "railways" / "main.materials.json"
    materials_path.parent.mkdir(parents=True, exist_ok=True)

    materials_doc = {
        MATERIAL_RAIL_BALLAST: {
            "name": MATERIAL_RAIL_BALLAST,
            "mapTo": MATERIAL_RAIL_BALLAST,
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", MATERIAL_RAIL_BALLAST),
            "Stages": [
                {
                    "ambientOcclusionMap": "/assets/materials/decalroad/dirt/dirt_road_gravels/t_dirt_road_gravels_ao.data.dds",
                    "baseColorMap": "/assets/materials/decalroad/dirt/dirt_road_gravels/t_dirt_road_gravels_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/dirt/dirt_road_gravels/t_dirt_road_gravels_nm.normal.dds",
                    "roughnessMap": "/assets/materials/decalroad/dirt/dirt_road_gravels/t_dirt_road_gravels_r.data.dds",
                },
            ],
            "version": 1.5,
        },
        MATERIAL_RAIL_STEEL: {
            "name": MATERIAL_RAIL_STEEL,
            "mapTo": MATERIAL_RAIL_STEEL,
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", MATERIAL_RAIL_STEEL),
            "Stages": [
                {
                    "baseColorMap": "/assets/materials/tileable/metal/aircraft_corrugated_metal/aircraft_corrugated_metal_d.dds",
                    "normalMap": "/assets/materials/tileable/metal/aircraft_corrugated_metal/aircraft_corrugated_metal_n.dds",
                    "roughnessMap": "/assets/materials/tileable/metal/aircraft_corrugated_metal/aircraft_corrugated_metal_r.dds",
                    "metallicFactor": 0.95,
                    "roughnessFactor": 0.25,
                },
            ],
            "version": 1.5,
        },
    }

    materials_path.write_text(json.dumps(materials_doc, indent=2), encoding="utf-8")
    return materials_path


def build_railway_scene_object(
    railway: Railway,
    level_id: str,
    *,
    shape_path: str | None = None,
    parent: str = "railways",
) -> dict[str, Any]:
    """Place procedural railway track as a BeamNG ``TSStatic`` scene object.

    Uses ``collisionType: "Visible Mesh Final"`` so trains and crossing vehicles
    have full physical mesh collisions.
    """
    key = f"railway_way{railway.osm_id}"
    if shape_path is None:
        shape_path = f"/levels/{level_id}/art/shapes/railways/{key}.dae"

    return {
        "class": "TSStatic",
        "persistentId": stable_uuid(level_id, "TSStatic", key),
        "__parent": parent,
        "position": [0.0, 0.0, 0.0],
        "shapeName": shape_path,
        "scale": [1.0, 1.0, 1.0],
        "collisionType": "Visible Mesh Final",
        "decalType": "None",
    }


def export_railways(
    railways: Sequence[Railway],
    level_id: str,
    level_dir: Path,
    *,
    roads: Sequence[Road] | None = None,
    ground_sampler: Callable[[float, float], float] | None = None,
    chunk_size_m: float = DEFAULT_RAIL_CHUNK_SIZE_M,
    batch_chunks: bool = True,
    parent: str = "railways",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Export railways into sector-chunked COLLADA files and ``TSStatic`` objects.

    When ``batch_chunks=True``, tracks are batched by 4 km spatial sector
    (``rail_chunk_<sx>_<sy>.dae``) to cut scene draw calls ~10x while keeping
    chunk-local coordinates for float32 GPU precision.
    """
    valid_railways = [rw for rw in railways if rw.points.shape[0] >= 2]
    if not valid_railways:
        return [], {"exported": 0, "chunks": 0, "railways": 0, "triangles": 0}

    railways_dir = level_dir / "art" / "shapes" / "railways"
    railways_dir.mkdir(parents=True, exist_ok=True)
    road_index = RoadCrossingIndex.from_roads(roads) if roads else None

    scene_objects: list[dict[str, Any]] = []
    total_tris = 0

    if not batch_chunks:
        for rw in valid_railways:
            mesh = build_railway_geometry(
                rw,
                ground_sampler=ground_sampler,
                road_index=road_index,
            )
            if not mesh.ballast_triangles and not mesh.steel_triangles:
                continue
            dae_path = railways_dir / f"railway_way{rw.osm_id}.dae"
            export_railway_dae(dae_path, mesh, f"railway_way{rw.osm_id}")
            scene_objects.append(build_railway_scene_object(rw, level_id, parent=parent))
            total_tris += len(mesh.ballast_triangles) + len(mesh.steel_triangles)
        emit_railways_materials(level_dir, level_id)
        return scene_objects, {
            "exported": len(scene_objects),
            "chunks": 0,
            "railways": len(valid_railways),
            "triangles": total_tris,
        }

    chunks: dict[tuple[int, int], list[Railway]] = {}
    for rw in valid_railways:
        mid_x = 0.5 * (float(rw.points[:, 0].min()) + float(rw.points[:, 0].max()))
        mid_y = 0.5 * (float(rw.points[:, 1].min()) + float(rw.points[:, 1].max()))
        sx = math.floor(mid_x / chunk_size_m)
        sy = math.floor(mid_y / chunk_size_m)
        chunks.setdefault((sx, sy), []).append(rw)

    chunk_count = 0
    for (sx, sy), sector_rws in sorted(chunks.items()):
        chunk_name = f"rail_chunk_{sx}_{sy}"
        chunk_cx = (sx + 0.5) * chunk_size_m
        chunk_cy = (sy + 0.5) * chunk_size_m
        chunk_mesh = RailwayMesh()

        for rw in sector_rws:
            sub = build_railway_geometry(
                rw,
                ground_sampler=ground_sampler,
                road_index=road_index,
            )
            chunk_mesh.merge(sub, offset_xy=(chunk_cx, chunk_cy))

        if not chunk_mesh.ballast_triangles and not chunk_mesh.steel_triangles:
            continue

        dae_path = railways_dir / f"{chunk_name}.dae"
        export_railway_dae(dae_path, chunk_mesh, chunk_name)
        chunk_count += 1
        total_tris += len(chunk_mesh.ballast_triangles) + len(chunk_mesh.steel_triangles)

        scene_objects.append(
            {
                "name": chunk_name,
                "class": "TSStatic",
                "persistentId": stable_uuid(level_id, "TSStatic", chunk_name),
                "__parent": parent,
                "position": [round(chunk_cx, 4), round(chunk_cy, 4), 0.0],
                "shapeName": f"/levels/{level_id}/art/shapes/railways/{chunk_name}.dae",
                "scale": [1.0, 1.0, 1.0],
                "collisionType": "Visible Mesh Final",
                "decalType": "None",
                "meshCulling": True,
            }
        )

    emit_railways_materials(level_dir, level_id)
    return scene_objects, {
        "exported": len(scene_objects),
        "chunks": chunk_count,
        "railways": len(valid_railways),
        "triangles": total_tris,
    }
