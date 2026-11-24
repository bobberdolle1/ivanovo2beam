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
    if n < 2:
        return raw_z.copy()

    deltas = np.diff(points[:, :2], axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])

    smoothed = raw_z.copy().astype(np.float64)

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
    """Build terrain corridors for railways with gravel ballast and grade smoothing."""
    from ivanovo2beam.roads.build import sample_surface_z
    from ivanovo2beam.terrain.builder import RoadCorridor

    corridors: list[RoadCorridor] = []
    for rw in railways:
        surface_z: np.ndarray | None = None
        if build is not None and not (rw.bridge or rw.tunnel):
            try:
                # Mock a road-like object with rw.points for sample_surface_z
                class _Centerline:
                    points = rw.points

                raw_z = sample_surface_z(_Centerline(), build)  # type: ignore[arg-type]
                surface_z = smooth_rail_grades(rw.points, raw_z, max_grade=max_grade)
            except Exception:  # pragma: no cover
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
        self.add_quad(c001, c101, c111, c011, (0.0, 0.0, 1.0), material)
        self.add_quad(c000, c010, c110, c100, (0.0, 0.0, -1.0), material)
        self.add_quad(c000, c100, c101, c001, (0.0, -1.0, 0.0), material)
        self.add_quad(c110, c010, c011, c111, (0.0, 1.0, 0.0), material)
        self.add_quad(c010, c000, c001, c011, (-1.0, 0.0, 0.0), material)
        self.add_quad(c100, c110, c111, c101, (1.0, 0.0, 0.0), material)


def is_crossing_road(
    point: tuple[float, float],
    roads: Sequence[Road] | None,
) -> bool:
    """Check whether a 2D point lies within an intersecting road carriageway."""
    if not roads:
        return False
    px, py = point
    for road in roads:
        if not road.drivable or road.tunnel:
            continue
        # Fast bounding box check
        r_pts = road.points
        if px < r_pts[:, 0].min() - 10.0 or px > r_pts[:, 0].max() + 10.0:
            continue
        if py < r_pts[:, 1].min() - 10.0 or py > r_pts[:, 1].max() + 10.0:
            continue
        # Point-to-polyline segment distance
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
        if min_dist <= road.half_width_m + 1.0:
            return True
    return False


def build_railway_geometry(
    railway: Railway,
    z_profile: np.ndarray | None = None,
    ground_sampler: Callable[[float, float], float] | None = None,
    roads: Sequence[Road] | None = None,
) -> RailwayMesh:
    """Generate 3D rails geometry (Russian 1520mm gauge) + ballast prism.

    At road crossings, rails and ballast are flush with the road carriageway surface.

    Args:
        railway: Railway line to extrude.
        z_profile: Pre-computed elevations along points.
        ground_sampler: Ground elevation query function.
        roads: Road network for grade-crossing detection.

    Returns:
        RailwayMesh containing ballast and steel geometry.
    """
    mesh = RailwayMesh()
    pts = railway.points
    n = pts.shape[0]
    if n < 2:
        return mesh

    diffs = np.diff(pts, axis=0)
    seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])
    seg_lens[seg_lens < 1e-6] = 1.0
    seg_tangents = diffs / seg_lens[:, None]

    normals = np.zeros((n, 2), dtype=np.float64)
    normals[0] = [-seg_tangents[0, 1], seg_tangents[0, 0]]
    normals[-1] = [-seg_tangents[-1, 1], seg_tangents[-1, 0]]
    if n > 2:
        mid_tangents = (seg_tangents[:-1] + seg_tangents[1:]) * 0.5
        mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
        mid_lens[mid_lens < 1e-6] = 1.0
        mid_tangents /= mid_lens[:, None]
        normals[1:-1] = np.column_stack((-mid_tangents[:, 1], mid_tangents[:, 0]))

    if z_profile is not None and len(z_profile) == n:
        base_z = np.asarray(z_profile, dtype=np.float64)
    elif ground_sampler is not None:
        base_z = np.array([ground_sampler(float(p[0]), float(p[1])) for p in pts], dtype=np.float64)
    else:
        base_z = np.zeros(n, dtype=np.float64)

    # Detect crossing status per vertex
    crossing_flags = [is_crossing_road((float(p[0]), float(p[1])), roads) for p in pts]

    rail_offset = railway.gauge_m * 0.5
    half_head = RAIL_HEAD_WIDTH_M * 0.5

    # 1. Ballast Prism (trapezoid beneath track)
    for i in range(n - 1):
        p0x, p0y = float(pts[i, 0]), float(pts[i, 1])
        p1x, p1y = float(pts[i + 1, 0]), float(pts[i + 1, 1])
        n0x, n0y = float(normals[i, 0]), float(normals[i, 1])
        n1x, n1y = float(normals[i + 1, 0]), float(normals[i + 1, 1])

        z0_top = float(base_z[i])
        z1_top = float(base_z[i + 1])
        z0_bot = z0_top - BALLAST_DEPTH_M
        z1_bot = z1_top - BALLAST_DEPTH_M

        # Ballast top corners
        bt_l0 = (p0x - BALLAST_TOP_HALF_WIDTH_M * n0x, p0y - BALLAST_TOP_HALF_WIDTH_M * n0y, z0_top)
        bt_r0 = (p0x + BALLAST_TOP_HALF_WIDTH_M * n0x, p0y + BALLAST_TOP_HALF_WIDTH_M * n0y, z0_top)
        bt_r1 = (p1x + BALLAST_TOP_HALF_WIDTH_M * n1x, p1y + BALLAST_TOP_HALF_WIDTH_M * n1y, z1_top)
        bt_l1 = (p1x - BALLAST_TOP_HALF_WIDTH_M * n1x, p1y - BALLAST_TOP_HALF_WIDTH_M * n1y, z1_top)

        # Ballast bottom corners
        bb_l0 = (p0x - BALLAST_BOTTOM_HALF_WIDTH_M * n0x, p0y - BALLAST_BOTTOM_HALF_WIDTH_M * n0y, z0_bot)
        bb_r0 = (p0x + BALLAST_BOTTOM_HALF_WIDTH_M * n0x, p0y + BALLAST_BOTTOM_HALF_WIDTH_M * n0y, z0_bot)
        bb_r1 = (p1x + BALLAST_BOTTOM_HALF_WIDTH_M * n1x, p1y + BALLAST_BOTTOM_HALF_WIDTH_M * n1y, z1_bot)
        bb_l1 = (p1x - BALLAST_BOTTOM_HALF_WIDTH_M * n1x, p1y - BALLAST_BOTTOM_HALF_WIDTH_M * n1y, z1_bot)

        # Ballast top
        mesh.add_quad(bt_l0, bt_r0, bt_r1, bt_l1, (0.0, 0.0, 1.0), MATERIAL_RAIL_BALLAST)
        # Ballast left slope
        mesh.add_quad(bb_l0, bt_l0, bt_l1, bb_l1, (-n0x, -n0y, 0.0), MATERIAL_RAIL_BALLAST)
        # Ballast right slope
        mesh.add_quad(bt_r0, bb_r0, bb_r1, bt_r1, (n0x, n0y, 0.0), MATERIAL_RAIL_BALLAST)

    # 2. Steel Rails (Left & Right)
    for i in range(n - 1):
        p0x, p0y = float(pts[i, 0]), float(pts[i, 1])
        p1x, p1y = float(pts[i + 1, 0]), float(pts[i + 1, 1])
        n0x, n0y = float(normals[i, 0]), float(normals[i, 1])
        n1x, n1y = float(normals[i + 1, 0]), float(normals[i + 1, 1])

        z0_base = float(base_z[i])
        z1_base = float(base_z[i + 1])

        # At road crossings, rail height is flush with the road (0 height above surface)
        h0 = 0.01 if crossing_flags[i] else RAIL_HEIGHT_M
        h1 = 0.01 if crossing_flags[i + 1] else RAIL_HEIGHT_M

        for offset in (-rail_offset, rail_offset):
            # Extrude 3D rail box
            c_bot_l0 = (p0x + (offset - half_head) * n0x, p0y + (offset - half_head) * n0y, z0_base)
            c_bot_r0 = (p0x + (offset + half_head) * n0x, p0y + (offset + half_head) * n0y, z0_base)
            c_top_r0 = (p0x + (offset + half_head) * n0x, p0y + (offset + half_head) * n0y, z0_base + h0)
            c_top_l0 = (p0x + (offset - half_head) * n0x, p0y + (offset - half_head) * n0y, z0_base + h0)

            c_bot_l1 = (p1x + (offset - half_head) * n1x, p1y + (offset - half_head) * n1y, z1_base)
            c_bot_r1 = (p1x + (offset + half_head) * n1x, p1y + (offset + half_head) * n1y, z1_base)
            c_top_r1 = (p1x + (offset + half_head) * n1x, p1y + (offset + half_head) * n1y, z1_base + h1)
            c_top_l1 = (p1x + (offset - half_head) * n1x, p1y + (offset - half_head) * n1y, z1_base + h1)

            # Top running surface (+Z)
            mesh.add_quad(c_top_l0, c_top_r0, c_top_r1, c_top_l1, (0.0, 0.0, 1.0), MATERIAL_RAIL_STEEL)
            # Left side
            mesh.add_quad(c_top_l0, c_top_l1, c_bot_l1, c_bot_l0, (-n0x, -n0y, 0.0), MATERIAL_RAIL_STEEL)
            # Right side
            mesh.add_quad(c_top_r0, c_bot_r0, c_bot_r1, c_top_r1, (n0x, n0y, 0.0), MATERIAL_RAIL_STEEL)

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
