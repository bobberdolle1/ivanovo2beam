"""Converting a road network into terrain corridors and BeamNG DecalRoads.

Two different things are produced from the same :class:`~ivanovo2beam.roads.network.Road`
objects, and the distinction matters:

**Corridors** describe the ground the road needs. They are handed to the
terrain builder, which grades a smooth, grade-limited surface into the
heightmap. In BeamNG the terrain *is* the collision surface, so this step is
what makes the road physically exist.

**DecalRoads** are the visible ribbon drawn over that graded terrain. They
carry no collision; if the corridor were missing, the player would see asphalt
and drive on a bumpy field underneath it.

Bridges and tunnels get a corridor with ``grade_terrain=False``: their geometry
must not flatten the ground beneath them. Tunnels suppress DecalRoad export
entirely so no ghost ribbon floats above terrain.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ivanovo2beam.beamng.level import SceneTree
    from ivanovo2beam.config import RoadSettings
    from ivanovo2beam.roads.network import Road
    from ivanovo2beam.terrain.builder import RoadCorridor

#: Corridor half-width used per class, in metres, when the config does not
#: override it. A motorway needs far more graded ground either side than a
#: village track, and grading too wide wastes terrain detail and hides the
#: landscape.
CORRIDOR_HALF_WIDTH_M: dict[str, float] = {
    "motorway": 40.0,
    "motorway_link": 30.0,
    "trunk": 35.0,
    "trunk_link": 25.0,
    "primary": 25.0,
    "primary_link": 20.0,
    "secondary": 20.0,
    "secondary_link": 18.0,
    "tertiary": 16.0,
    "tertiary_link": 15.0,
    "unclassified": 12.0,
    "residential": 10.0,
    "living_street": 8.0,
    "service": 7.0,
    "track": 7.0,
}

#: Fallback corridor half-width for classes not listed above.
DEFAULT_CORRIDOR_HALF_WIDTH_M = 10.0

#: Bridge and tunnel carriageways sit at least this high above the terrain, in
#: metres, so the decal does not sink into the ground it crosses.
STRUCTURE_CLEARANCE_M = 4.0

#: BeamNG follows terrain between DecalRoad nodes. Nodes further apart than
#: this would let a road float above or sink below the ground between them.
MAX_DECAL_NODE_SPACING_M = 6.0

#: Maximum segment length in metres before splitting into chained segments.
MAX_SEGMENT_LENGTH_M = 1500.0

#: Maximum axis-aligned bounding-box span (X, Y, Z) per DecalRoad, in metres.
#: BeamNG's SceneStaticDecalRoadManager packs positions in a 2048 m local range;
#: capping each DecalRoad's bbox span at 800 m keeps every leaf safely inside.
MAX_SEGMENT_BBOX_SPAN_M = 800.0

#: Minimum XY separation between consecutive DecalRoad nodes, in metres.
_MIN_NODE_DIST_M = 1e-3

#: Tiered DecalRoad LOD settings ``(distance_fade, break_angle, detail)`` by highway class.
_DECAL_LOD_BY_CLASS: dict[str, tuple[tuple[float, float], float, float]] = {
    "motorway": ((650.0, 150.0), 3.0, 0.3),
    "motorway_link": ((650.0, 150.0), 3.0, 0.3),
    "trunk": ((650.0, 150.0), 3.0, 0.3),
    "trunk_link": ((650.0, 150.0), 3.0, 0.3),
    "primary": ((650.0, 150.0), 3.0, 0.3),
    "primary_link": ((650.0, 150.0), 3.0, 0.3),
    "secondary": ((450.0, 100.0), 3.5, 0.35),
    "secondary_link": ((450.0, 100.0), 3.5, 0.35),
    "tertiary": ((450.0, 100.0), 3.5, 0.35),
    "tertiary_link": ((450.0, 100.0), 3.5, 0.35),
    "residential": ((250.0, 75.0), 4.0, 0.4),
    "unclassified": ((250.0, 75.0), 4.0, 0.4),
    "service": ((120.0, 40.0), 4.5, 0.5),
    "living_street": ((120.0, 40.0), 4.5, 0.5),
    "track": ((120.0, 40.0), 4.5, 0.5),
    "path": ((120.0, 40.0), 4.5, 0.5),
}
_DEFAULT_DECAL_LOD: tuple[tuple[float, float], float, float] = ((120.0, 40.0), 4.5, 0.5)


def _decal_lod_for(highway: str) -> tuple[tuple[float, float], float, float]:
    """Return ``(distance_fade, break_angle, detail)`` for an OSM highway class."""
    return _DECAL_LOD_BY_CLASS.get(highway, _DEFAULT_DECAL_LOD)


def _simplify_3d_z_aware(
    pts: np.ndarray,
    *,
    tol_xy: float = 0.5,
    tol_z: float = 0.10,
    max_seg_m: float = 250.0,
) -> np.ndarray:
    """Simplify a 3D polyline while bounding both XY deviation and vertical Z error.

    Also preserves intermediate points so no simplified segment exceeds ``max_seg_m``,
    keeping subsequent bounding-box splitting accurate on long straightaways.
    """
    n = pts.shape[0]
    if n <= 2:
        return pts

    keep = np.zeros(n, dtype=bool)
    keep[0] = True
    keep[-1] = True
    stack: list[tuple[int, int]] = [(0, n - 1)]

    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 <= 1:
            continue
        ax, ay, az = float(pts[i0, 0]), float(pts[i0, 1]), float(pts[i0, 2])
        bx, by, bz = float(pts[i1, 0]), float(pts[i1, 1]), float(pts[i1, 2])
        abx = bx - ax
        aby = by - ay
        abz = bz - az
        len2 = abx * abx + aby * aby
        chord_len = math.sqrt(len2)

        sub = pts[i0 + 1 : i1]
        if len2 <= 1e-12:
            d_xy = np.hypot(sub[:, 0] - ax, sub[:, 1] - ay)
            dz = np.abs(sub[:, 2] - az)
        else:
            t = np.clip(((sub[:, 0] - ax) * abx + (sub[:, 1] - ay) * aby) / len2, 0.0, 1.0)
            proj_x = ax + t * abx
            proj_y = ay + t * aby
            proj_z = az + t * abz
            d_xy = np.hypot(sub[:, 0] - proj_x, sub[:, 1] - proj_y)
            dz = np.abs(sub[:, 2] - proj_z)

        scores = np.maximum(d_xy / tol_xy, dz / tol_z)
        rel_idx = int(np.argmax(scores))
        max_score = float(scores[rel_idx])
        if max_score > 1.0:
            split = i0 + 1 + rel_idx
            keep[split] = True
            stack.append((i0, split))
            stack.append((split, i1))
        elif max_seg_m > 0.0 and chord_len > max_seg_m:
            # Keep the vertex closest to the chord midpoint so long straightaways remain splittable
            mid_idx = i0 + 1 + (i1 - i0 - 1) // 2
            keep[mid_idx] = True
            stack.append((i0, mid_idx))
            stack.append((mid_idx, i1))

    return pts[keep]


def _sanitize_xy_points(
    points: np.ndarray,
    extra: np.ndarray | None = None,
    *,
    min_dist_m: float = _MIN_NODE_DIST_M,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Drop non-finite vertices and consecutive duplicate XY coordinates."""
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] == 0:
        return np.empty((0, 2), dtype=np.float64), (np.empty(0, dtype=np.float64) if extra is not None else None)

    valid = np.isfinite(pts[:, 0]) & np.isfinite(pts[:, 1])
    ext: np.ndarray | None = None
    if extra is not None and len(extra) == pts.shape[0]:
        ext = np.asarray(extra, dtype=np.float64)
        valid &= np.isfinite(ext)

    pts = pts[valid, :2]
    if ext is not None:
        ext = ext[valid]

    if pts.shape[0] < 2:
        return pts, ext

    keep = np.ones(pts.shape[0], dtype=bool)
    last_x, last_y = float(pts[0, 0]), float(pts[0, 1])
    for i in range(1, pts.shape[0]):
        dx = float(pts[i, 0]) - last_x
        dy = float(pts[i, 1]) - last_y
        if math.hypot(dx, dy) < min_dist_m:
            keep[i] = False
        else:
            last_x, last_y = float(pts[i, 0]), float(pts[i, 1])

    pts = pts[keep]
    if ext is not None:
        ext = ext[keep]
    return pts, ext


def _sanitize_xyz_points(
    points: np.ndarray,
    *,
    min_dist_m: float = _MIN_NODE_DIST_M,
) -> np.ndarray:
    """Drop non-finite 3D vertices and consecutive duplicate XY coordinates."""
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] == 0:
        return np.empty((0, 3), dtype=np.float64)

    valid = np.all(np.isfinite(pts[:, :3]), axis=1)
    pts = pts[valid, :3]
    if pts.shape[0] < 2:
        return pts

    keep = np.ones(pts.shape[0], dtype=bool)
    last_x, last_y = float(pts[0, 0]), float(pts[0, 1])
    for i in range(1, pts.shape[0]):
        dx = float(pts[i, 0]) - last_x
        dy = float(pts[i, 1]) - last_y
        if math.hypot(dx, dy) < min_dist_m:
            keep[i] = False
        else:
            last_x, last_y = float(pts[i, 0]), float(pts[i, 1])
    return pts[keep]


def _bbox_span(pts: np.ndarray) -> float:
    """Return the maximum X/Y/Z bounding-box span of a point array."""
    if pts.shape[0] == 0:
        return 0.0
    mins = np.min(pts, axis=0)
    maxs = np.max(pts, axis=0)
    return float(np.max(maxs - mins))


def _split_points_by_span(
    pts: np.ndarray,
    max_segment_length_m: float,
    max_bbox_span_m: float,
) -> list[np.ndarray]:
    """Split a 3D polyline into contiguous slices bounded by arc length and bbox span."""
    n = pts.shape[0]
    if n < 2:
        return []

    deltas = np.diff(pts[:, :2], axis=0)
    seg_lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    total_len = float(seg_lengths.sum())

    if total_len <= max_segment_length_m and _bbox_span(pts) <= max_bbox_span_m:
        return [pts]

    slices: list[np.ndarray] = []
    start_idx = 0
    while start_idx < n - 1:
        current_len = 0.0
        min_xyz = pts[start_idx].copy()
        max_xyz = pts[start_idx].copy()
        end_idx = start_idx
        while end_idx < n - 1:
            step = float(seg_lengths[end_idx])
            nxt = pts[end_idx + 1]
            cand_min = np.minimum(min_xyz, nxt)
            cand_max = np.maximum(max_xyz, nxt)
            cand_span = float(np.max(cand_max - cand_min))
            if (current_len + step > max_segment_length_m or cand_span > max_bbox_span_m) and end_idx > start_idx:
                break
            current_len += step
            min_xyz = cand_min
            max_xyz = cand_max
            end_idx += 1
        slices.append(pts[start_idx : end_idx + 1])
        start_idx = end_idx
    return slices


def _straighten_shallow_bridge_xy(pts: np.ndarray, *, max_lat_dev_m: float = 0.05, min_turn_deg: float = 0.3) -> np.ndarray:
    """Snap near-collinear bridge vertices in XY onto their chord to avoid degenerate miter joints."""
    if pts.shape[0] < 3:
        return pts
    out = pts.copy()
    cos_thresh = math.cos(math.radians(min_turn_deg))
    for i in range(1, out.shape[0] - 1):
        ax, ay = float(out[i - 1, 0]), float(out[i - 1, 1])
        px, py = float(out[i, 0]), float(out[i, 1])
        bx, by = float(out[i + 1, 0]), float(out[i + 1, 1])
        v1x, v1y = px - ax, py - ay
        v2x, v2y = bx - px, by - py
        l1 = math.hypot(v1x, v1y)
        l2 = math.hypot(v2x, v2y)
        abx, aby = bx - ax, by - ay
        lab2 = abx * abx + aby * aby
        if l1 < 1e-6 or l2 < 1e-6 or lab2 < 1e-12:
            continue
        cos_turn = (v1x * v2x + v1y * v2y) / (l1 * l2)
        t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / lab2))
        proj_x = ax + t * abx
        proj_y = ay + t * aby
        lat_dev = math.hypot(px - proj_x, py - proj_y)
        if lat_dev < max_lat_dev_m and cos_turn >= cos_thresh:
            out[i, 0] = proj_x
            out[i, 1] = proj_y
    return out


def _prepare_road_points_3d(
    road: Road,
    *,
    ground_z: np.ndarray | None = None,
    terrain_build: object | None = None,
    max_node_spacing_m: float = MAX_DECAL_NODE_SPACING_M,
    max_bbox_span_m: float = MAX_SEGMENT_BBOX_SPAN_M,
) -> np.ndarray:
    """Sanitize, densify, attach Z coordinates, and Z-aware simplify a road polyline."""
    pts_2d, gz = _sanitize_xy_points(road.points, ground_z)
    if pts_2d.shape[0] < 2:
        return np.empty((0, 3), dtype=np.float64)

    if terrain_build is not None:
        if road.bridge:
            from ivanovo2beam.roads.bridges import compute_bridge_deck_profile

            raw_gz = sample_points_z(pts_2d, terrain_build)
            if gz is not None and len(gz) == pts_2d.shape[0]:
                raw_gz = gz
            # Temporarily compute deck profile on sanitized bridge vertices
            from dataclasses import replace

            bridge_road = replace(road, points=pts_2d)
            deck_z = compute_bridge_deck_profile(bridge_road, ground_z=raw_gz)
            pts_3d = _densify_with_z(pts_2d, deck_z, max_node_spacing_m)
        else:
            from ivanovo2beam.roads.network import densify

            d2 = densify(pts_2d, max_node_spacing_m)
            z_vals = sample_points_z(d2, terrain_build)
            pts_3d = np.column_stack((d2, z_vals))
    elif gz is not None and len(gz) == pts_2d.shape[0]:
        pts_3d = _densify_with_z(pts_2d, gz, max_node_spacing_m)
    else:
        from ivanovo2beam.roads.network import densify

        d2 = densify(pts_2d, max_node_spacing_m)
        # No elevation known: place the decal at zero and let the terrain own it.
        pts_3d = np.column_stack((d2, np.zeros(d2.shape[0])))

    clean = _sanitize_xyz_points(pts_3d)
    max_seg = min(250.0, max(1.0, max_bbox_span_m * 0.5))
    tol_z = 0.15 if road.bridge else 0.10
    simplified = _simplify_3d_z_aware(clean, tol_xy=0.5, tol_z=tol_z, max_seg_m=max_seg)
    if road.bridge and simplified.shape[0] >= 3:
        simplified = _straighten_shallow_bridge_xy(simplified)
    return simplified


def corridor_half_width_for(road: Road, settings: RoadSettings) -> float:
    """Return the graded corridor half-width for a road, in metres.

    Args:
        road: The road.
        settings: Region road settings, whose ``corridor_half_width_m`` acts as
            a floor so a level can force a wider corridor than the class needs.

    Returns:
        Half-width in metres, always at least the carriageway half-width.
    """
    base = CORRIDOR_HALF_WIDTH_M.get(road.highway, DEFAULT_CORRIDOR_HALF_WIDTH_M)
    return max(base, settings.corridor_half_width_m, road.half_width_m + 1.0)


def roads_to_corridors(
    roads: Sequence[Road],
    settings: RoadSettings,
    *,
    drivable_only: bool = True,
) -> list[RoadCorridor]:
    """Build terrain corridors from a road network.

    Args:
        roads: Extracted roads, ideally sorted by ascending priority.
        settings: Region road settings.
        drivable_only: Skip footways, steps and other undrivable classes.

    Returns:
        Corridors ready for the terrain builder. Bridge and tunnel corridors
        carry ``grade_terrain=False``.
    """
    from ivanovo2beam.terrain.builder import RoadCorridor

    corridors: list[RoadCorridor] = []
    for road in roads:
        if drivable_only and not road.drivable:
            continue
        half_width = road.half_width_m
        corridor_half = corridor_half_width_for(road, settings)
        shoulder_material = (
            "Grass"
            if road.material_key in {"Asphalt", "Asphalt_worn", "Concrete"}
            else ("Dirt" if road.material_key == "Dirt" else "Gravel")
        )
        corridors.append(
            RoadCorridor(
                points=road.points,
                half_width_m=half_width,
                corridor_half_width_m=corridor_half,
                priority=road.priority,
                # Bridges and tunnels must not flatten the ground beneath them.
                grade_terrain=not (road.bridge or road.tunnel),
                material_key=road.material_key,
                shoulder_material_key=shoulder_material,
                key=f"way{road.osm_id}",
            )
        )
    return corridors


def road_to_decal_object(
    road: Road,
    level_id: str,
    *,
    ground_z: np.ndarray | None = None,
    terrain_build: object | None = None,
    max_node_spacing_m: float = MAX_DECAL_NODE_SPACING_M,
    key: str | None = None,
    points_3d: np.ndarray | None = None,
    parent: str = "road_network",
) -> dict[str, object] | None:
    """Build a DecalRoad scene object for one road.

    Args:
        road: The road to export.
        level_id: Level identifier for the deterministic UUID.
        ground_z: Optional elevation for each input vertex, in metres. When
            given, nodes follow the graded terrain surface, which is what makes
            the decal sit exactly on the road instead of floating.
        terrain_build: Optional built terrain used to sample Z at every
            densified node instead of linearly interpolating sparse vertices.
        max_node_spacing_m: Maximum spacing between exported nodes.
        key: Stable object key override. Defaults to ``f"way{road.osm_id}"``.
        points_3d: Pre-computed ``(n, 3)`` node coordinates.
        parent: Owning scene group name.

    Returns:
        The scene object, or ``None`` when the road is too short to draw or is
        a tunnel (suppressed to prevent ghost asphalt ribbons over terrain).
    """
    if road.tunnel:
        return None
    if not math.isfinite(road.width_m) or road.width_m <= 0.0:
        return None

    from ivanovo2beam.beamng.level import build_road_object

    if points_3d is not None:
        points = _sanitize_xyz_points(points_3d)
    else:
        points = _prepare_road_points_3d(
            road,
            ground_z=ground_z,
            terrain_build=terrain_build,
            max_node_spacing_m=max_node_spacing_m,
        )

    if points.shape[0] < 2:
        return None

    # Public roads are not gated; only private/no access or driveways/parking aisles are gated.
    gated = (road.access in {"private", "no"}) or (road.service in {"driveway", "parking_aisle"})

    # Drivability: 1.0 for paved, 0.5 for unpaved/track, 0.0 if undrivable.
    if not road.drivable:
        drivability = 0.0
    elif road.highway in {"track", "path"} or road.surface in {
        "dirt",
        "ground",
        "earth",
        "mud",
        "unpaved",
        "gravel",
        "fine_gravel",
        "pebblestone",
        "compacted",
        "sand",
    } or road.material_key in {"Dirt", "Gravel", "Sand"}:
        drivability = 0.5
    else:
        drivability = 1.0

    # On bridges, lift nodes by +0.02m so the decal projects directly onto the 3D bridge deck.
    z_offset = 0.02 if road.bridge else 0.0
    # BeamNG node format is [x, y, z, width], where width is the full width.
    nodes = [[float(p[0]), float(p[1]), float(p[2]) + z_offset, float(road.width_m)] for p in points]

    object_key = key if key is not None else f"way{road.osm_id}"
    distance_fade, break_angle, detail = _decal_lod_for(road.highway)
    if road.bridge:
        break_angle = min(break_angle, 1.5)
        detail = min(detail, 0.25)
    obj: dict[str, Any] = build_road_object(
        level_id,
        key=object_key,
        nodes=nodes,
        material=road.decal_material,
        drivability=drivability,
        parent=parent,
        over_objects=bool(road.bridge),
        gated_road=gated,
        texture_length=100.0,
        render_priority=10,
        break_angle=break_angle,
        distance_fade=distance_fade,
    )

    # Populate BeamNG AI navgraph and LOD properties
    obj["name"] = object_key
    obj["detail"] = detail
    obj["oneWay"] = bool(road.oneway)
    obj["autoLanes"] = False
    obj["lanesLeft"] = 0 if road.oneway else max(1, road.lanes // 2)
    obj["lanesRight"] = road.lanes if road.oneway else max(1, road.lanes - road.lanes // 2)
    obj["autoJunction"] = not road.bridge
    obj["speedLimit"] = round(road.maxspeed_kph / 3.6, 2)
    if road.bridge:
        obj["overObjects"] = True
        obj["decalBias"] = 0.02

    return obj


def road_to_decal_objects(
    road: Road,
    level_id: str,
    *,
    ground_z: np.ndarray | None = None,
    terrain_build: object | None = None,
    max_node_spacing_m: float = MAX_DECAL_NODE_SPACING_M,
    max_segment_length_m: float = MAX_SEGMENT_LENGTH_M,
    max_bbox_span_m: float = MAX_SEGMENT_BBOX_SPAN_M,
    parent: str = "road_network",
) -> list[dict[str, object]]:
    """Build DecalRoad scene object(s) for a road, splitting by length and bbox span.

    Highways exceeding ``max_segment_length_m`` or ``max_bbox_span_m`` are split
    into chained segments (``way<id>_0``, ``way<id>_1``) to prevent
    packed-vertex-range overflow in BeamNG's ``SceneStaticDecalRoadManager``.

    Args:
        road: The road to export.
        level_id: Level identifier for deterministic UUIDs.
        ground_z: Elevation along vertices.
        terrain_build: Optional built terrain used to sample Z at every
            densified node.
        max_node_spacing_m: Spacing between nodes.
        max_segment_length_m: Maximum segment arc length before splitting.
        max_bbox_span_m: Maximum axis-aligned bounding-box span before splitting.
        parent: Owning group name.

    Returns:
        List of DecalRoad scene objects (empty if suppressed/tunnel or too short).
    """
    if road.tunnel:
        return []

    pts = _prepare_road_points_3d(
        road,
        ground_z=ground_z,
        terrain_build=terrain_build,
        max_node_spacing_m=max_node_spacing_m,
        max_bbox_span_m=max_bbox_span_m,
    )
    if pts.shape[0] < 2:
        return []

    slices = _split_points_by_span(pts, max_segment_length_m, max_bbox_span_m)
    if len(slices) == 1:
        obj = road_to_decal_object(
            road,
            level_id,
            points_3d=slices[0],
            key=f"way{road.osm_id}",
            parent=parent,
        )
        return [obj] if obj is not None else []

    objects: list[dict[str, object]] = []
    for seg_idx, sub_pts in enumerate(slices):
        obj = road_to_decal_object(
            road,
            level_id,
            points_3d=sub_pts,
            key=f"way{road.osm_id}_{seg_idx}",
            parent=parent,
        )
        if obj is not None:
            objects.append(obj)
    return objects


def _should_generate_markings(road: Road) -> bool:
    """Return whether a road receives GOST lane/centerline decal markings."""
    if not road.drivable or road.tunnel:
        return False
    if road.lanes >= 4:
        return True
    return (
        not road.oneway
        and road.lanes == 2
        and road.highway in {"motorway", "trunk", "primary"}
        and road.width_m >= 8.0
    )


def generate_lane_markings(
    road: Road,
    level_id: str,
    points_3d: np.ndarray,
    *,
    is_start_junction: bool = True,
    is_end_junction: bool = True,
    clip_distance_m: float = 8.0,
    parent_key: str | None = None,
    parent: str = "road_network",
    max_segment_length_m: float = MAX_SEGMENT_LENGTH_M,
    max_bbox_span_m: float = MAX_SEGMENT_BBOX_SPAN_M,
) -> list[dict[str, object]]:
    """Generate GOST markings for multi-lane roads (lanes >= 4) and wide 2-lane highways.

    Following the MapNG pattern, white center and lane overlay DecalRoads
    (``m_line_white`` / ``m_line_white_discontinue``) are clipped 8m back from junctions.

    Args:
        road: Road specification.
        level_id: Level ID for UUID generation.
        points_3d: (n, 3) 3D points along the road centerline.
        is_start_junction: Whether road start connects to another road/junction.
        is_end_junction: Whether road end connects to another road/junction.
        clip_distance_m: Distance in metres to clip markings back from junctions.
        parent_key: Base key for naming overlay lines.
        parent: Scene parent group.
        max_segment_length_m: Maximum marking segment arc length.
        max_bbox_span_m: Maximum marking axis-aligned bounding-box span.

    Returns:
        List of overlay DecalRoad scene objects.
    """
    if not _should_generate_markings(road):
        return []
    if not math.isfinite(road.width_m) or road.width_m <= 0.0:
        return []

    clean_pts = _sanitize_xyz_points(points_3d)
    if clean_pts.shape[0] < 2:
        return []

    from ivanovo2beam.beamng.level import build_road_object

    deltas = np.diff(clean_pts[:, :2], axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    total_len = float(seg_lens.sum())

    start_clip = clip_distance_m if is_start_junction else 0.0
    end_clip = total_len - (clip_distance_m if is_end_junction else 0.0)

    if end_clip - start_clip < 12.0:
        return []

    cum_dist = np.concatenate(([0.0], np.cumsum(seg_lens)))

    # Sample clipped polyline
    resampled_pts: list[tuple[float, float, float]] = []

    def point_at_dist(target_d: float) -> tuple[float, float, float]:
        idx = int(np.searchsorted(cum_dist, target_d, side="right") - 1)
        idx = max(0, min(idx, len(seg_lens) - 1))
        d0 = float(cum_dist[idx])
        seg_len = float(seg_lens[idx])
        t = 0.0 if seg_len <= 1e-6 else (target_d - d0) / seg_len
        p0 = clean_pts[idx]
        p1 = clean_pts[idx + 1]
        return (
            float(p0[0] + t * (p1[0] - p0[0])),
            float(p0[1] + t * (p1[1] - p0[1])),
            float(p0[2] + t * (p1[2] - p0[2])),
        )

    resampled_pts.append(point_at_dist(start_clip))
    for i in range(len(cum_dist)):
        d = float(cum_dist[i])
        if start_clip < d < end_clip:
            resampled_pts.append((float(clean_pts[i, 0]), float(clean_pts[i, 1]), float(clean_pts[i, 2])))
    resampled_pts.append(point_at_dist(end_clip))

    clipped_pts = _sanitize_xyz_points(np.asarray(resampled_pts, dtype=np.float64))
    if clipped_pts.shape[0] < 2:
        return []

    # Calculate tangent and 2D unit normal vectors
    p_diff = np.diff(clipped_pts[:, :2], axis=0)
    p_lens = np.hypot(p_diff[:, 0], p_diff[:, 1])
    safe_p_lens = np.where(p_lens < 1e-6, 1.0, p_lens)
    tangents_seg = p_diff / safe_p_lens[:, None]

    normals = np.zeros((clipped_pts.shape[0], 2), dtype=np.float64)
    normals[0] = [-tangents_seg[0, 1], tangents_seg[0, 0]]
    normals[-1] = [-tangents_seg[-1, 1], tangents_seg[-1, 0]]
    if clipped_pts.shape[0] > 2:
        mid_tangents = (tangents_seg[:-1] + tangents_seg[1:]) * 0.5
        mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
        # Fall back to the incoming segment tangent on 180-degree reversals
        degenerate = mid_lens < 1e-6
        if np.any(degenerate):
            mid_tangents[degenerate] = tangents_seg[:-1][degenerate]
            mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
            mid_lens = np.where(mid_lens < 1e-6, 1.0, mid_lens)
        mid_tangents /= mid_lens[:, None]
        normals[1:-1] = np.column_stack((-mid_tangents[:, 1], mid_tangents[:, 0]))

    num_lanes = road.lanes
    lane_width = road.width_m / float(num_lanes)
    base_key = parent_key or f"way{road.osm_id}"

    markings: list[dict[str, object]] = []

    for k in range(1, num_lanes):
        offset = -road.width_m * 0.5 + k * lane_width
        is_center = (num_lanes >= 4) and (not road.oneway) and (k == num_lanes // 2)
        material = "m_line_white" if is_center else "m_line_white_discontinue"

        raw_line = np.column_stack(
            (
                clipped_pts[:, 0] + offset * normals[:, 0],
                clipped_pts[:, 1] + offset * normals[:, 1],
                clipped_pts[:, 2] + 0.01,
            )
        )
        clean_line = _sanitize_xyz_points(raw_line)
        if clean_line.shape[0] < 2:
            continue

        line_slices = _split_points_by_span(clean_line, max_segment_length_m, max_bbox_span_m)
        tex_len = 8.0 if "discontinue" in material else 10.0

        for seg_idx, sub_line in enumerate(line_slices):
            line_nodes = [[float(p[0]), float(p[1]), float(p[2]), 0.15] for p in sub_line]
            line_key = f"{base_key}_mark_{k}" if len(line_slices) == 1 else f"{base_key}_mark_{k}_{seg_idx}"
            line_obj: dict[str, Any] = build_road_object(
                level_id,
                key=line_key,
                nodes=line_nodes,
                material=material,
                drivability=0.0,
                parent=parent,
                over_objects=bool(road.bridge),
                gated_road=False,
                texture_length=tex_len,
                render_priority=15,
                distance_fade=(150.0, 50.0),
            )
            line_obj["name"] = line_key
            line_obj["decalBias"] = 0.03
            line_obj["autoJunction"] = False
            line_obj["autoLanes"] = False
            line_obj["oneWay"] = bool(road.oneway)
            markings.append(line_obj)

    return markings


def _densify_with_z(
    points: np.ndarray,
    z: np.ndarray,
    max_spacing_m: float,
) -> np.ndarray:
    """Densify a polyline and linearly interpolate elevations alongside it.

    Args:
        points: ``(n, 2)`` polyline in world metres.
        z: ``(n,)`` elevations matching ``points``.
        max_spacing_m: Maximum segment length in metres.

    Returns:
        ``(m, 3)`` array of ``[x, y, z]``.
    """
    if points.shape[0] < 2:
        return np.column_stack((points, z))
    deltas = np.diff(points, axis=0)
    lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    out: list[tuple[float, float, float]] = [(float(points[0, 0]), float(points[0, 1]), float(z[0]))]
    for i, length in enumerate(lengths):
        ax, ay = float(points[i, 0]), float(points[i, 1])
        bx, by = float(points[i + 1, 0]), float(points[i + 1, 1])
        az, bz = float(z[i]), float(z[i + 1])
        if length <= max_spacing_m or length <= 0:
            out.append((bx, by, bz))
            continue
        steps = max(2, math.ceil(length / max_spacing_m))
        for step in range(1, steps + 1):
            t = step / steps
            out.append((ax + (bx - ax) * t, ay + (by - ay) * t, az + (bz - az) * t))
    return np.asarray(out, dtype=np.float64)


def sample_points_z(points: np.ndarray, build: object) -> np.ndarray:
    """Sample built terrain elevations at arbitrary ``(n, 2)`` world XY points."""
    elevations = build.elevations  # type: ignore[attr-defined]
    origin_x = build.origin_x  # type: ignore[attr-defined]
    origin_y = build.origin_y  # type: ignore[attr-defined]
    cell = build.cell_size_m  # type: ignore[attr-defined]
    rows, cols = elevations.shape[:2]

    if points.shape[0] == 0:
        return np.empty(0, dtype=np.float64)
    if rows < 2 or cols < 2:
        fill = float(elevations[0, 0]) if elevations.size else 0.0
        return np.full(points.shape[0], fill, dtype=np.float64)

    gx = (points[:, 0] - origin_x) / cell
    gy = (points[:, 1] - origin_y) / cell

    c0 = np.clip(np.floor(gx).astype(np.int64), 0, cols - 2)
    r0 = np.clip(np.floor(gy).astype(np.int64), 0, rows - 2)
    c1 = c0 + 1
    r1 = r0 + 1

    tx = np.clip(gx - c0, 0.0, 1.0)
    ty = np.clip(gy - r0, 0.0, 1.0)

    z00 = elevations[r0, c0]
    z01 = elevations[r0, c1]
    z10 = elevations[r1, c0]
    z11 = elevations[r1, c1]

    z0 = z00 * (1.0 - tx) + z01 * tx
    z1 = z10 * (1.0 - tx) + z11 * tx
    return z0 * (1.0 - ty) + z1 * ty


def sample_surface_z(road: Road, build: object) -> np.ndarray:
    """Sample the built terrain along a road's centreline.

    DecalRoad nodes must lie on the graded surface, otherwise the ribbon
    visibly floats over dips or disappears into crests.

    Args:
        road: The road whose centreline to sample.
        build: A :class:`~ivanovo2beam.terrain.builder.TerrainBuild`.

    Returns:
        ``(n,)`` elevations in metres, one per centreline vertex.
    """
    return sample_points_z(road.points, build)


def add_network_to_scene(
    tree: SceneTree,
    roads: Sequence[Road],
    build: object,
    *,
    group_path: Sequence[str] = ("level_objects", "road_network"),
    max_node_spacing_m: float = MAX_DECAL_NODE_SPACING_M,
) -> dict[str, int]:
    """Add every road in a network to a level scene as a DecalRoad.

    Args:
        tree: Scene to populate.
        roads: Roads to export.
        build: The built terrain, used to place decals on the graded surface.
        group_path: Group that should own the road objects.
        max_node_spacing_m: Maximum spacing between decal nodes.

    Returns:
        Counts of exported, skipped and total nodes.
    """
    exported = 0
    skipped = 0
    nodes = 0

    # Build vertex connection map (including interior vertices for T-junctions,
    # deduplicating per road so closed loops do not self-flag as junctions).
    vertex_counts: dict[tuple[int, int], int] = {}
    for r in roads:
        if r.drivable and not r.tunnel and r.points.shape[0] >= 2:
            seen_verts: set[tuple[int, int]] = set()
            for pt in r.points:
                if math.isfinite(float(pt[0])) and math.isfinite(float(pt[1])):
                    seen_verts.add((round(float(pt[0] * 2)), round(float(pt[1] * 2))))
            for v in seen_verts:
                vertex_counts[v] = vertex_counts.get(v, 0) + 1

    for road in roads:
        terrain_arg: object | None = build if hasattr(build, "elevations") else None
        objs = road_to_decal_objects(
            road,
            tree.level_id,
            terrain_build=terrain_arg,
            max_node_spacing_m=max_node_spacing_m,
            parent=group_path[-1] if group_path else "road_network",
        )
        if not objs:
            skipped += 1
            continue

        for obj in objs:
            tree.add_object(group_path, obj)
            exported += 1
            nodes += len(obj["nodes"])  # type: ignore[arg-type]

        # Multi-lane and wide 2-lane highway GOST markings
        if _should_generate_markings(road):
            p0 = (round(float(road.points[0, 0] * 2)), round(float(road.points[0, 1] * 2)))
            p1 = (round(float(road.points[-1, 0] * 2)), round(float(road.points[-1, 1] * 2)))
            is_start_junc = vertex_counts.get(p0, 0) > 1
            is_end_junc = vertex_counts.get(p1, 0) > 1

            for seg_idx, obj in enumerate(objs):
                obj_nodes = np.asarray(obj["nodes"], dtype=np.float64)[:, :3]
                seg_is_start = is_start_junc if seg_idx == 0 else False
                seg_is_end = is_end_junc if seg_idx == len(objs) - 1 else False
                base_k = f"way{road.osm_id}_{seg_idx}" if len(objs) > 1 else f"way{road.osm_id}"
                markings = generate_lane_markings(
                    road,
                    tree.level_id,
                    obj_nodes,
                    is_start_junction=seg_is_start,
                    is_end_junction=seg_is_end,
                    parent_key=base_k,
                    parent=group_path[-1] if group_path else "road_network",
                )
                for mark_obj in markings:
                    tree.add_object(group_path, mark_obj)
                    exported += 1
                    nodes += len(mark_obj["nodes"])  # type: ignore[arg-type]

    return {"exported": exported, "skipped": skipped, "nodes": nodes}
