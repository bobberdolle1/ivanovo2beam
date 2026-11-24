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
        corridors.append(
            RoadCorridor(
                points=road.points,
                half_width_m=half_width,
                corridor_half_width_m=corridor_half,
                priority=road.priority,
                # Bridges and tunnels must not flatten the ground beneath them.
                grade_terrain=not (road.bridge or road.tunnel),
                material_key=road.material_key,
                shoulder_material_key="Gravel",
                key=f"way{road.osm_id}",
            )
        )
    return corridors


def road_to_decal_object(
    road: Road,
    level_id: str,
    *,
    ground_z: np.ndarray | None = None,
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

    from ivanovo2beam.beamng.level import build_road_object

    if points_3d is not None:
        points = points_3d
    else:
        points = road.points
        if ground_z is not None and len(ground_z) == points.shape[0]:
            points = _densify_with_z(points, np.asarray(ground_z, dtype=np.float64), max_node_spacing_m)
        else:
            from ivanovo2beam.roads.network import densify

            d2 = densify(points, max_node_spacing_m)
            # No elevation known: place the decal at zero and let the terrain own it.
            points = np.column_stack((d2, np.zeros(d2.shape[0])))

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
    obj: dict[str, Any] = build_road_object(
        level_id,
        key=object_key,
        nodes=nodes,
        material=road.decal_material,
        drivability=drivability,
        parent=parent,
        over_objects=True,
        gated_road=gated,
    )

    # Populate BeamNG AI navgraph properties
    obj["name"] = object_key
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
    max_node_spacing_m: float = MAX_DECAL_NODE_SPACING_M,
    max_segment_length_m: float = MAX_SEGMENT_LENGTH_M,
    parent: str = "road_network",
) -> list[dict[str, object]]:
    """Build DecalRoad scene object(s) for a road, splitting if > 1500m.

    Highways longer than 1500m are split into chained segments (way<id>_0, way<id>_1)
    to prevent packed-vertex-range overflow in BeamNG.

    Args:
        road: The road to export.
        level_id: Level identifier for deterministic UUIDs.
        ground_z: Elevation along vertices.
        max_node_spacing_m: Spacing between nodes.
        max_segment_length_m: Maximum segment length before splitting (default 1500m).
        parent: Owning group name.

    Returns:
        List of DecalRoad scene objects (empty if suppressed/tunnel or too short).
    """
    if road.tunnel:
        return []

    points = road.points
    if ground_z is not None and len(ground_z) == points.shape[0]:
        pts = _densify_with_z(points, np.asarray(ground_z, dtype=np.float64), max_node_spacing_m)
    else:
        from ivanovo2beam.roads.network import densify

        d2 = densify(points, max_node_spacing_m)
        pts = np.column_stack((d2, np.zeros(d2.shape[0])))

    if pts.shape[0] < 2:
        return []

    deltas = np.diff(pts[:, :2], axis=0)
    seg_lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    total_len = float(seg_lengths.sum())

    if total_len <= max_segment_length_m:
        obj = road_to_decal_object(
            road,
            level_id,
            points_3d=pts,
            key=f"way{road.osm_id}",
            parent=parent,
        )
        return [obj] if obj is not None else []

    # Split into chained segments
    objects: list[dict[str, object]] = []
    start_idx = 0
    seg_idx = 0
    n = pts.shape[0]

    while start_idx < n - 1:
        current_len = 0.0
        end_idx = start_idx
        while end_idx < n - 1:
            step = float(seg_lengths[end_idx])
            if current_len + step > max_segment_length_m and end_idx > start_idx:
                break
            current_len += step
            end_idx += 1
        sub_pts = pts[start_idx : end_idx + 1]
        obj = road_to_decal_object(
            road,
            level_id,
            points_3d=sub_pts,
            key=f"way{road.osm_id}_{seg_idx}",
            parent=parent,
        )
        if obj is not None:
            objects.append(obj)
        start_idx = end_idx
        seg_idx += 1

    return objects


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
) -> list[dict[str, object]]:
    """Generate multi-lane GOST markings for roads with lanes >= 4.

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

    Returns:
        List of overlay DecalRoad scene objects.
    """
    if road.lanes < 4 or not road.drivable or road.tunnel:
        return []
    if points_3d.shape[0] < 2:
        return []

    from ivanovo2beam.beamng.level import build_road_object

    deltas = np.diff(points_3d[:, :2], axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    total_len = float(seg_lens.sum())

    start_clip = clip_distance_m if is_start_junction else 0.0
    end_clip = total_len - (clip_distance_m if is_end_junction else 0.0)

    if end_clip - start_clip < 2.0:
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
        p0 = points_3d[idx]
        p1 = points_3d[idx + 1]
        return (
            float(p0[0] + t * (p1[0] - p0[0])),
            float(p0[1] + t * (p1[1] - p0[1])),
            float(p0[2] + t * (p1[2] - p0[2])),
        )

    resampled_pts.append(point_at_dist(start_clip))
    for i in range(len(cum_dist)):
        d = float(cum_dist[i])
        if start_clip < d < end_clip:
            resampled_pts.append((float(points_3d[i, 0]), float(points_3d[i, 1]), float(points_3d[i, 2])))
    resampled_pts.append(point_at_dist(end_clip))

    clipped_pts = np.asarray(resampled_pts, dtype=np.float64)
    if clipped_pts.shape[0] < 2:
        return []

    # Calculate tangent and 2D unit normal vectors
    p_diff = np.diff(clipped_pts[:, :2], axis=0)
    p_lens = np.hypot(p_diff[:, 0], p_diff[:, 1])
    p_lens[p_lens < 1e-6] = 1.0
    tangents_seg = p_diff / p_lens[:, None]

    normals = np.zeros((clipped_pts.shape[0], 2), dtype=np.float64)
    normals[0] = [-tangents_seg[0, 1], tangents_seg[0, 0]]
    normals[-1] = [-tangents_seg[-1, 1], tangents_seg[-1, 0]]
    if clipped_pts.shape[0] > 2:
        mid_tangents = (tangents_seg[:-1] + tangents_seg[1:]) * 0.5
        mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
        mid_lens[mid_lens < 1e-6] = 1.0
        mid_tangents /= mid_lens[:, None]
        normals[1:-1] = np.column_stack((-mid_tangents[:, 1], mid_tangents[:, 0]))

    num_lanes = road.lanes
    lane_width = road.width_m / float(num_lanes)
    base_key = parent_key or f"way{road.osm_id}"

    markings: list[dict[str, object]] = []

    for k in range(1, num_lanes):
        offset = -road.width_m * 0.5 + k * lane_width
        is_center = (not road.oneway) and (k == num_lanes // 2)
        material = "m_line_white" if is_center else "m_line_white_discontinue"

        line_nodes: list[list[float]] = []
        for j in range(clipped_pts.shape[0]):
            nx, ny = float(normals[j, 0]), float(normals[j, 1])
            px = float(clipped_pts[j, 0] + offset * nx)
            py = float(clipped_pts[j, 1] + offset * ny)
            pz = float(clipped_pts[j, 2] + 0.01)  # 1cm above road surface
            line_nodes.append([px, py, pz, 0.15])  # 15cm marking width

        line_key = f"{base_key}_mark_{k}"
        line_obj: dict[str, Any] = build_road_object(
            level_id,
            key=line_key,
            nodes=line_nodes,
            material=material,
            drivability=0.0,
            parent=parent,
            over_objects=True,
            gated_road=False,
        )
        line_obj["decalBias"] = 0.01
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
    elevations = build.elevations  # type: ignore[attr-defined]
    origin_x = build.origin_x  # type: ignore[attr-defined]
    origin_y = build.origin_y  # type: ignore[attr-defined]
    cell = build.cell_size_m  # type: ignore[attr-defined]
    n = elevations.shape[0]

    cols = np.clip(np.rint((road.points[:, 0] - origin_x) / cell).astype(np.int64), 0, n - 1)
    rows = np.clip(np.rint((road.points[:, 1] - origin_y) / cell).astype(np.int64), 0, n - 1)
    return elevations[rows, cols]


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

    # Build vertex connection map to identify junctions for marking clipping
    vertex_counts: dict[tuple[int, int], int] = {}
    for r in roads:
        if r.drivable and not r.tunnel and r.points.shape[0] >= 2:
            p0 = (round(float(r.points[0, 0] * 2)), round(float(r.points[0, 1] * 2)))
            p1 = (round(float(r.points[-1, 0] * 2)), round(float(r.points[-1, 1] * 2)))
            vertex_counts[p0] = vertex_counts.get(p0, 0) + 1
            vertex_counts[p1] = vertex_counts.get(p1, 0) + 1

    for road in roads:
        try:
            ground_z = sample_surface_z(road, build)
        except Exception:  # pragma: no cover - defensive; shape mismatches only
            ground_z = None

        objs = road_to_decal_objects(
            road,
            tree.level_id,
            ground_z=ground_z,
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

        # Multi-lane GOST markings for roads with lanes >= 4
        if road.lanes >= 4 and road.drivable and not road.tunnel:
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
