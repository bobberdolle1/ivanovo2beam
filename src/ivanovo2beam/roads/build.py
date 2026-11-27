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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from pathlib import Path

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

#: Bridge DecalRoads cap arc length at 250 m (BeamNG heroRoadEditor maxDecalRoadLenM)
#: so 16-bit index batches on dense 2 m bridge splines never overflow.
MAX_BRIDGE_SEGMENT_LENGTH_M = 250.0

#: Drape height (m) added to overObjects=True bridge DecalRoads so downward
#: projection cleanly hits the bridge deck mesh instead of slipping under/below.
_BRIDGE_DECAL_Z_OFFSET_M = 0.50

#: Maximum axis-aligned bounding-box span (X, Y, Z) per DecalRoad, in metres.
#: BeamNG's SceneStaticDecalRoadManager packs positions in a 2048 m local range;
#: capping each DecalRoad's bbox span at 800 m keeps every leaf safely inside.
MAX_SEGMENT_BBOX_SPAN_M = 800.0

#: Minimum XY separation between consecutive DecalRoad nodes, in metres
#: (adapted from MapNG exportBeamNGLevel.js, MIT License, Copyright (c) 2026 Nikki Luzader).
MIN_ROAD_NODE_SPACING_M = 0.15
_MIN_NODE_DIST_M = MIN_ROAD_NODE_SPACING_M

#: Normalized dot-product threshold below which an interior vertex is treated as
#: a hairline spike reversal and dropped so improvedSpline cannot blow up.
SPIKE_REVERSAL_DOT = -0.99

#: Tighter hairpin threshold for offset edge/marking strips where tight curve radii
#: (R < lateral_offset) self-fold at dot < -0.65 (e.g. -0.967 on loops).
_OFFSET_STRIP_REVERSAL_DOT = -0.65

#: Default BeamNG DecalRoad spline sampling detail (0.5 avoids 2 m-node dropout).
DECAL_ROAD_DETAIL = 0.5

#: BeamNG 0.40 renderPriority convention: LOWER values render ON TOP.
EDGE_LINE_RENDER_PRIORITY = 1
CENTER_LINE_RENDER_PRIORITY = 2
EDGE_GRASS_RENDER_PRIORITY = 9
AI_INVISIBLE_RENDER_PRIORITY = 25

SURFACE_RENDER_PRIORITY: dict[str, int] = {
    "motorway": 11,
    "trunk": 12,
    "motorway_link": 13,
    "trunk_link": 13,
    "primary": 13,
    "primary_link": 14,
    "secondary": 15,
    "secondary_link": 16,
    "tertiary": 17,
    "tertiary_link": 18,
    "unclassified": 19,
    "road": 19,
    "busway": 19,
    "raceway": 19,
    "residential": 20,
    "living_street": 21,
    "service": 22,
    "track": 23,
    "path": 24,
}
_DEFAULT_SURFACE_RENDER_PRIORITY = 19

_MAJOR_MARKING_HIGHWAYS = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
    }
)
_MAJOR_EDGE_HIGHWAYS = _MAJOR_MARKING_HIGHWAYS
_MINOR_UNCUT_HIGHWAYS = frozenset({"service", "track", "path", "living_street", "footway", "cycleway", "steps"})

_SURFACE_TEXTURE_LENGTH: dict[str, float] = {
    "road_asphalt_2lane": 11.0,
    "AsphaltRoad_variation_two_way": 11.0,
    "road1_concrete": 40.0,
    "m_dirt_road": 88.0,
    "m_dirt_road_gravels": 88.0,
    "road_invisible": 10.0,
}

_MARKING_TEXTURE_LENGTH = 6.4
_EDGE_GRASS_TEXTURE_LENGTH = 8.0
_EDGE_LINE_INSET_M = 0.3

#: Tiered DecalRoad LOD settings ``(distance_fade, break_angle, detail)`` by highway class.
_DECAL_LOD_BY_CLASS: dict[str, tuple[tuple[float, float], float, float]] = {
    "motorway": ((750.0, 180.0), 1.5, DECAL_ROAD_DETAIL),
    "motorway_link": ((750.0, 180.0), 1.5, DECAL_ROAD_DETAIL),
    "trunk": ((750.0, 180.0), 1.5, DECAL_ROAD_DETAIL),
    "trunk_link": ((750.0, 180.0), 1.5, DECAL_ROAD_DETAIL),
    "primary": ((750.0, 180.0), 1.5, DECAL_ROAD_DETAIL),
    "primary_link": ((750.0, 180.0), 1.5, DECAL_ROAD_DETAIL),
    "secondary": ((550.0, 120.0), 1.5, DECAL_ROAD_DETAIL),
    "secondary_link": ((550.0, 120.0), 1.5, DECAL_ROAD_DETAIL),
    "tertiary": ((550.0, 120.0), 1.5, DECAL_ROAD_DETAIL),
    "tertiary_link": ((550.0, 120.0), 1.5, DECAL_ROAD_DETAIL),
    "residential": ((300.0, 80.0), 2.0, DECAL_ROAD_DETAIL),
    "unclassified": ((350.0, 90.0), 2.0, DECAL_ROAD_DETAIL),
    "service": ((180.0, 60.0), 2.5, DECAL_ROAD_DETAIL),
    "living_street": ((180.0, 60.0), 2.5, DECAL_ROAD_DETAIL),
    "track": ((200.0, 60.0), 2.5, DECAL_ROAD_DETAIL),
    "path": ((180.0, 60.0), 2.5, DECAL_ROAD_DETAIL),
}
_DEFAULT_DECAL_LOD: tuple[tuple[float, float], float, float] = ((200.0, 60.0), 2.5, DECAL_ROAD_DETAIL)


def surface_render_priority_for(road: Road) -> int:
    """Return the BeamNG ``renderPriority`` for a road base surface (lower draws on top)."""
    if road.decal_material == "road_invisible":
        return AI_INVISIBLE_RENDER_PRIORITY
    return SURFACE_RENDER_PRIORITY.get(road.highway, _DEFAULT_SURFACE_RENDER_PRIORITY)


def texture_length_for(road: Road) -> float:
    """Return the ground repeat length in metres for a road's base decal material."""
    return _SURFACE_TEXTURE_LENGTH.get(road.decal_material, 11.0)


def edge_blend_overlap(edge_width_m: float) -> float:
    """Overlap distance (m) reaching past the asphalt rim fade into the edge texture."""
    return 0.2 + edge_width_m / 16.0 + 0.05


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


def _collapse_close_nodes(pts: np.ndarray, min_spacing_m: float) -> np.ndarray:
    """Drop vertices closer than ``min_spacing_m`` while preserving the real terminus."""
    if pts.shape[0] < 2:
        return pts
    kept: list[np.ndarray] = [pts[0]]
    n = pts.shape[0]
    for i in range(1, n):
        curr = pts[i]
        prev = kept[-1]
        if math.hypot(float(curr[0] - prev[0]), float(curr[1] - prev[1])) >= min_spacing_m:
            kept.append(curr)
        elif i == n - 1 and len(kept) >= 2:
            kept[-1] = curr
    return np.asarray(kept, dtype=np.float64)


def _tame_tight_bend_nodes(pts: np.ndarray, half_width_m: float) -> np.ndarray:
    """Thin dense micro-vertices around cul-de-sacs whose radius < ribbon half-width."""
    if pts.shape[0] <= 3 or half_width_m <= 1.5:
        return pts
    min_bend_chord = max(3.0, min(6.0, 0.85 * half_width_m))
    kept: list[np.ndarray] = [pts[0]]
    for i in range(1, pts.shape[0] - 1):
        prev = kept[-1]
        curr = pts[i]
        nxt = pts[i + 1]
        dx1, dy1 = float(curr[0] - prev[0]), float(curr[1] - prev[1])
        dx2, dy2 = float(nxt[0] - curr[0]), float(nxt[1] - curr[1])
        l1 = math.hypot(dx1, dy1)
        l2 = math.hypot(dx2, dy2)
        if l1 < min_bend_chord or l2 < min_bend_chord:
            if l1 > 1e-6 and l2 > 1e-6:
                dot = (dx1 * dx2 + dy1 * dy2) / (l1 * l2)
                if dot < 0.96:
                    continue
        kept.append(curr)
    kept.append(pts[-1])
    return np.asarray(kept, dtype=np.float64)


def _remove_spike_nodes(pts: np.ndarray, reversal_dot: float = SPIKE_REVERSAL_DOT) -> np.ndarray:
    """Drop interior hairpin reversal spikes that break BeamNG's ``improvedSpline``."""
    if pts.shape[0] <= 2:
        return pts
    kept: list[np.ndarray] = [pts[0]]
    for i in range(1, pts.shape[0] - 1):
        prev = kept[-1]
        curr = pts[i]
        nxt = pts[i + 1]
        in_x = float(curr[0] - prev[0])
        in_y = float(curr[1] - prev[1])
        out_x = float(nxt[0] - curr[0])
        out_y = float(nxt[1] - curr[1])
        len_in = math.hypot(in_x, in_y)
        len_out = math.hypot(out_x, out_y)
        if len_in > 1e-9 and len_out > 1e-9:
            dot = (in_x * out_x + in_y * out_y) / (len_in * len_out)
            if dot <= reversal_dot:
                continue
        kept.append(curr)
    kept.append(pts[-1])
    return np.asarray(kept, dtype=np.float64)


def sanitize_road_nodes(
    points: np.ndarray,
    *,
    min_spacing_m: float = MIN_ROAD_NODE_SPACING_M,
    reversal_dot: float = SPIKE_REVERSAL_DOT,
) -> np.ndarray:
    """Filter non-finite coordinates, close duplicates, and hairpin spikes.

    Adapted from MapNG ``sanitizeRoadNodes`` (MIT License, Copyright (c) 2026
    Nikki Luzader). Alternating spacing collapse and spike removal prevents
    ``improvedSpline: true`` from emitting ``inf``/degenerate tangents.
    """
    pts = np.asarray(points, dtype=np.float64)
    cols = pts.shape[1] if pts.ndim == 2 else 0
    if pts.ndim != 2 or pts.shape[0] == 0 or cols < 2:
        return np.empty((0, max(2, cols)), dtype=np.float64)

    out = pts[np.all(np.isfinite(pts), axis=1)]
    if out.shape[0] < 2:
        return np.empty((0, cols), dtype=np.float64)

    for _ in range(5):
        if out.shape[0] < 2:
            break
        collapsed = _collapse_close_nodes(out, min_spacing_m)
        despiked = _remove_spike_nodes(collapsed, reversal_dot)
        stable = despiked.shape[0] == out.shape[0]
        out = despiked
        if stable:
            break

    return out if out.shape[0] >= 2 else np.empty((0, cols), dtype=np.float64)


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
    if pts.shape[0] >= 2 and not keep[-1]:
        kept_indices = np.flatnonzero(keep)
        if kept_indices.size >= 2:
            keep[kept_indices[-1]] = False
            keep[-1] = True

    pts = pts[keep]
    if ext is not None:
        ext = ext[keep]
    return pts, ext


def _sanitize_xyz_points(
    points: np.ndarray,
    *,
    min_dist_m: float = _MIN_NODE_DIST_M,
) -> np.ndarray:
    """Drop non-finite 3D vertices, close duplicates, and hairpin spikes."""
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] == 0:
        return np.empty((0, 3), dtype=np.float64)
    return sanitize_road_nodes(pts[:, :3], min_spacing_m=min_dist_m)


def _bbox_span(pts: np.ndarray) -> float:
    """Return the maximum X/Y/Z bounding-box span of a point array."""
    if pts.shape[0] == 0:
        return 0.0
    mins = np.min(pts, axis=0)
    maxs = np.max(pts, axis=0)
    return float(np.max(maxs - mins))


def _polyline_arc_length_xy(pts: np.ndarray) -> float:
    """Return total 2D XY arc length of a ``(n, >=2)`` point array."""
    if pts.shape[0] < 2:
        return 0.0
    deltas = np.diff(pts[:, :2], axis=0)
    return float(np.hypot(deltas[:, 0], deltas[:, 1]).sum())


def _clamp_fade_for_length(fade: tuple[float, float], arc_len_m: float) -> tuple[float, float]:
    """Ensure ``start_fade + end_fade`` never exceeds 40% of ``arc_len_m``."""
    sf = max(0.0, float(fade[0]))
    ef = max(0.0, float(fade[1]))
    total = sf + ef
    if total <= 1e-6:
        return (0.0, 0.0)
    cap = max(0.0, arc_len_m * 0.4)
    if total > cap:
        scale = cap / total
        return (round(sf * scale, 3), round(ef * scale, 3))
    return (sf, ef)


def _split_points_by_span(
    pts: np.ndarray,
    max_segment_length_m: float,
    max_bbox_span_m: float,
) -> list[np.ndarray]:
    """Split a 3D polyline into contiguous slices bounded by arc length, bbox span, and origin distance."""
    n = pts.shape[0]
    if n < 2:
        return []

    deltas = np.diff(pts[:, :2], axis=0)
    seg_lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    total_len = float(seg_lengths.sum())
    if total_len < 0.4:
        return []

    max_dist_from_start = float(
        np.hypot(pts[:, 0] - pts[0, 0], pts[:, 1] - pts[0, 1]).max()
    )
    if total_len <= max_segment_length_m and _bbox_span(pts) <= max_bbox_span_m and max_dist_from_start <= max_bbox_span_m:
        return [pts]

    slices: list[np.ndarray] = []
    start_idx = 0
    while start_idx < n - 1:
        current_len = 0.0
        min_xyz = pts[start_idx].copy()
        max_xyz = pts[start_idx].copy()
        sx, sy = float(pts[start_idx, 0]), float(pts[start_idx, 1])
        end_idx = start_idx
        while end_idx < n - 1:
            step = float(seg_lengths[end_idx])
            nxt = pts[end_idx + 1]
            cand_min = np.minimum(min_xyz, nxt)
            cand_max = np.maximum(max_xyz, nxt)
            cand_span = float(np.max(cand_max - cand_min))
            cand_dist = math.hypot(float(nxt[0]) - sx, float(nxt[1]) - sy)
            if (
                current_len + step > max_segment_length_m
                or cand_span > max_bbox_span_m
                or cand_dist > max_bbox_span_m
            ) and end_idx > start_idx:
                break
            current_len += step
            min_xyz = cand_min
            max_xyz = cand_max
            end_idx += 1
        sub = pts[start_idx : end_idx + 1]
        if _polyline_arc_length_xy(sub) >= 0.4:
            slices.append(sub)
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


def _resample_xyz_uniform(points: np.ndarray, step_m: float) -> np.ndarray:
    """Resample a 3D polyline at uniform XY arc-length intervals ``<= step_m``."""
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] < 3 or step_m <= 0.0:
        return pts[:, :3].copy() if pts.ndim == 2 and pts.shape[1] >= 3 else np.empty((0, 3), dtype=np.float64)
    deltas = np.diff(pts[:, :2], axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    total_len = float(seg_lens.sum())
    if total_len <= 1e-6:
        return pts[:2, :3].copy()
    cum = np.concatenate(([0.0], np.cumsum(seg_lens)))
    n_samples = max(2, math.ceil(total_len / step_m) + 1)
    targets = np.linspace(0.0, total_len, n_samples)
    xs = np.interp(targets, cum, pts[:, 0])
    ys = np.interp(targets, cum, pts[:, 1])
    zs = np.interp(targets, cum, pts[:, 2])
    return np.column_stack((xs, ys, zs))


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

    eff_spacing = min(max_node_spacing_m, 2.0) if road.bridge else max_node_spacing_m

    if terrain_build is not None:
        if road.bridge:
            from ivanovo2beam.roads.bridges import compute_bridge_deck_profile

            raw_gz = sample_points_z(pts_2d, terrain_build)
            if gz is not None and len(gz) == pts_2d.shape[0]:
                raw_gz = gz
            bridge_road = replace(road, points=pts_2d)
            deck_z = compute_bridge_deck_profile(bridge_road, ground_z=raw_gz)
            pts_3d = np.column_stack((pts_2d, deck_z))
        else:
            from ivanovo2beam.roads.network import densify

            d2 = densify(pts_2d, eff_spacing)
            z_vals = sample_points_z(d2, terrain_build)
            pts_3d = np.column_stack((d2, z_vals))
    elif gz is not None and len(gz) == pts_2d.shape[0]:
        pts_3d = np.column_stack((pts_2d, gz)) if road.bridge else _densify_with_z(pts_2d, gz, eff_spacing)
    else:
        from ivanovo2beam.roads.network import densify

        d2 = densify(pts_2d, eff_spacing)
        # No elevation known: place the decal at zero and let the terrain own it.
        pts_3d = np.column_stack((d2, np.zeros(d2.shape[0])))

    if road.bridge:
        clean = _sanitize_xyz_points(pts_3d)
        if clean.shape[0] >= 3:
            clean = _straighten_shallow_bridge_xy(clean)
        uniform = _resample_xyz_uniform(clean, eff_spacing)
        return _sanitize_xyz_points(uniform, min_dist_m=0.5)

    clean = _sanitize_xyz_points(pts_3d)
    max_seg = min(250.0, max(1.0, max_bbox_span_m * 0.5))
    simplified = _simplify_3d_z_aware(clean, tol_xy=0.10, tol_z=0.02, max_seg_m=max_seg)
    return _sanitize_xyz_points(simplified)


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
    start_end_fade: tuple[float, float] = (0.0, 0.0),
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
        start_end_fade: Endpoint fade distances ``(start_m, end_m)``.

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

    points = sanitize_road_nodes(
        points,
        min_spacing_m=0.5 if road.bridge else MIN_ROAD_NODE_SPACING_M,
        reversal_dot=SPIKE_REVERSAL_DOT,
    )
    if not road.bridge:
        points = _tame_tight_bend_nodes(points, road.half_width_m)
    arc_len = _polyline_arc_length_xy(points)
    if points.shape[0] < 2 or arc_len < 0.4:
        return None

    # Public roads are not gated; only private/no access or driveways/parking aisles are gated.
    gated = (road.access in {"private", "no"}) or (road.service in {"driveway", "parking_aisle"})

    # Drivability: 1.0 for paved, 0.5 for unpaved/track, 0.0 if undrivable.
    if not road.drivable:
        drivability = 0.0
    elif _is_unpaved_road(road):
        drivability = 0.5
    else:
        drivability = 1.0

    # On bridges, lift nodes by drape height (+0.5m) so overObjects projects cleanly DOWN onto the 3D deck.
    z_offset = _BRIDGE_DECAL_Z_OFFSET_M if road.bridge else 0.0
    # BeamNG node format is [x, y, z, width], where width is the full width.
    nodes = [[float(p[0]), float(p[1]), float(p[2]) + z_offset, float(road.width_m)] for p in points]

    object_key = key if key is not None else f"way{road.osm_id}"
    distance_fade, break_angle, detail = _decal_lod_for(road.highway)
    if road.bridge:
        break_angle = min(break_angle, 1.5)
    clamped_fade = _clamp_fade_for_length(start_end_fade, arc_len)
    obj: dict[str, Any] = build_road_object(
        level_id,
        key=object_key,
        nodes=nodes,
        material=road.decal_material,
        drivability=drivability,
        parent=parent,
        over_objects=bool(road.bridge),
        gated_road=gated,
        texture_length=texture_length_for(road),
        render_priority=surface_render_priority_for(road),
        break_angle=break_angle,
        distance_fade=distance_fade,
        start_end_fade=clamped_fade,
    )

    # Populate BeamNG AI navgraph and LOD properties
    obj["name"] = object_key
    obj["detail"] = detail
    obj["startTangent"] = 0
    obj["endTangent"] = 0
    obj["oneWay"] = bool(road.oneway)
    obj["autoLanes"] = False
    obj["lanesLeft"] = 0 if road.oneway else max(1, road.lanes // 2)
    obj["lanesRight"] = road.lanes if road.oneway else max(1, road.lanes - road.lanes // 2)
    obj["autoJunction"] = not road.bridge
    obj["speedLimit"] = round(road.maxspeed_kph / 3.6, 2)
    if road.bridge:
        obj["overObjects"] = True
        obj["decalBias"] = 0.002
    else:
        obj["decalBias"] = 0.0015

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
    start_end_fade: tuple[float, float] = (0.0, 0.0),
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
        start_end_fade: Endpoint fade distances ``(start_m, end_m)`` applied
            only to the outer termini (internal splits always get ``0.0``).

    Returns:
        List of DecalRoad scene objects (empty if suppressed/tunnel or too short).
    """
    if road.tunnel:
        return []

    eff_seg_len = min(max_segment_length_m, MAX_BRIDGE_SEGMENT_LENGTH_M) if road.bridge else max_segment_length_m
    eff_bbox_span = min(max_bbox_span_m, MAX_BRIDGE_SEGMENT_LENGTH_M) if road.bridge else max_bbox_span_m

    pts = _prepare_road_points_3d(
        road,
        ground_z=ground_z,
        terrain_build=terrain_build,
        max_node_spacing_m=max_node_spacing_m,
        max_bbox_span_m=eff_bbox_span,
    )
    if pts.shape[0] < 2:
        return []

    slices = _split_points_by_span(pts, eff_seg_len, eff_bbox_span)
    if not slices:
        return []
    if len(slices) == 1:
        obj = road_to_decal_object(
            road,
            level_id,
            points_3d=slices[0],
            key=f"way{road.osm_id}",
            parent=parent,
            start_end_fade=start_end_fade,
        )
        return [obj] if obj is not None else []

    objects: list[dict[str, object]] = []
    start_fade, end_fade = start_end_fade
    last_idx = len(slices) - 1
    for seg_idx, sub_pts in enumerate(slices):
        seg_fade = (
            start_fade if seg_idx == 0 else 0.0,
            end_fade if seg_idx == last_idx else 0.0,
        )
        obj = road_to_decal_object(
            road,
            level_id,
            points_3d=sub_pts,
            key=f"way{road.osm_id}_{seg_idx}",
            parent=parent,
            start_end_fade=seg_fade,
        )
        if obj is not None:
            objects.append(obj)
    return objects


_UNPAVED_SURFACES = frozenset(
    {
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
        "grass",
    }
)


def _is_unpaved_road(road: Road) -> bool:
    """Return whether a road is unpaved track/dirt/gravel."""
    return (
        road.highway in {"track", "path"}
        or road.surface in _UNPAVED_SURFACES
        or road.material_key in {"Dirt", "Gravel", "Sand"}
        or road.decal_material in {"m_dirt_road", "m_dirt_road_gravels"}
    )


def _should_generate_markings(road: Road) -> bool:
    """Return whether a road receives GOST lane/centerline/edge decal markings."""
    if not road.drivable or road.tunnel or _is_unpaved_road(road):
        return False
    if road.decal_material == "road_invisible":
        return False
    if road.lanes >= 4:
        return True
    if road.highway in _MAJOR_MARKING_HIGHWAYS:
        if road.oneway:
            return road.width_m >= 4.5
        return road.lanes >= 2 and road.width_m >= 6.0
    if road.highway in {"tertiary", "tertiary_link"}:
        return (not road.oneway) and road.lanes >= 2 and road.width_m >= 7.0
    return False


def _should_generate_edge_decals(road: Road) -> bool:
    """Return whether a ground asphalt road receives grass-edge blend decals."""
    if not road.drivable or road.bridge or road.tunnel or _is_unpaved_road(road):
        return False
    if road.highway in {"service", "living_street", "pedestrian", "footway", "cycleway", "steps"}:
        return False
    if not math.isfinite(road.width_m) or road.width_m <= 5.5:
        return False
    min_len = 20.0 if road.highway == "residential" else 12.0
    if road.length_m < min_len:
        return False
    return road.material_key in {"Asphalt", "Asphalt_worn"} and road.decal_material in {
        "road_asphalt_2lane",
        "AsphaltRoad_variation_two_way",
    }


@dataclass(frozen=True, slots=True)
class _IndexedSegment:
    ax: float
    ay: float
    az: float
    bx: float
    by: float
    bz: float
    half_width: float
    owner_id: int
    layer: int
    ux: float
    uy: float
    seg_len: float
    highway: str = ""
    render_priority: int = _DEFAULT_SURFACE_RENDER_PRIORITY


class CarriagewayIndex:
    """Spatial grid index of drivable road carriageways for crossing cuts.

    Adapted from MapNG ``roadMarkingCuts.js`` (MIT License, Copyright (c) 2026
    Nikki Luzader).
    """

    def __init__(self, cell_size_m: float = 16.0) -> None:
        self.cell_size_m = cell_size_m
        self.segments: list[_IndexedSegment] = []
        self.grid: dict[tuple[int, int], list[int]] = {}

    def add_polyline(
        self,
        points: np.ndarray,
        *,
        half_width_m: float,
        owner_id: int,
        layer: int = 0,
        highway: str = "",
        render_priority: int = _DEFAULT_SURFACE_RENDER_PRIORITY,
    ) -> None:
        """Register every segment of a 2D or 3D polyline in the spatial grid."""
        pts = np.asarray(points, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[0] < 2:
            return
        has_z = pts.shape[1] >= 3
        cell = self.cell_size_m
        pad = half_width_m + 4.0
        for i in range(pts.shape[0] - 1):
            ax, ay = float(pts[i, 0]), float(pts[i, 1])
            bx, by = float(pts[i + 1, 0]), float(pts[i + 1, 1])
            az = float(pts[i, 2]) if has_z else 0.0
            bz = float(pts[i + 1, 2]) if has_z else 0.0
            if not (
                math.isfinite(ax)
                and math.isfinite(ay)
                and math.isfinite(bx)
                and math.isfinite(by)
            ):
                continue
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length < 1e-6:
                continue
            seg_idx = len(self.segments)
            self.segments.append(
                _IndexedSegment(
                    ax=ax,
                    ay=ay,
                    az=az,
                    bx=bx,
                    by=by,
                    bz=bz,
                    half_width=half_width_m,
                    owner_id=owner_id,
                    layer=layer,
                    ux=dx / length,
                    uy=dy / length,
                    seg_len=length,
                    highway=highway,
                    render_priority=render_priority,
                )
            )
            gx0 = math.floor((min(ax, bx) - pad) / cell)
            gx1 = math.floor((max(ax, bx) + pad) / cell)
            gy0 = math.floor((min(ay, by) - pad) / cell)
            gy1 = math.floor((max(ay, by) + pad) / cell)
            for gx in range(gx0, gx1 + 1):
                for gy in range(gy0, gy1 + 1):
                    self.grid.setdefault((gx, gy), []).append(seg_idx)

    def is_blocked(
        self,
        x: float,
        y: float,
        *,
        margin_m: float,
        tangent: tuple[float, float],
        owner_id: int,
        layer: int = 0,
        z: float | None = None,
        cut_parallel_inside: bool = False,
        cut_all_angles: bool = False,
        ignored_ids: frozenset[int] | set[int] | None = None,
        parallel_cos: float = 0.85,
        caller_highway: str = "",
        skip_minor_crossings: bool = False,
        end_cap_m: float | None = None,
    ) -> bool:
        """Return whether ``(x, y)`` lies on a crossing (or overlapping) carriageway."""
        cell_key = (math.floor(x / self.cell_size_m), math.floor(y / self.cell_size_m))
        candidates = self.grid.get(cell_key)
        if not candidates:
            return False
        tx, ty = tangent
        cap_m = margin_m if end_cap_m is None else end_cap_m
        caller_is_minor = caller_highway in _MINOR_UNCUT_HIGHWAYS
        for idx in candidates:
            seg = self.segments[idx]
            if seg.owner_id == owner_id or seg.layer != layer:
                continue
            if ignored_ids is not None and seg.owner_id in ignored_ids:
                continue
            if skip_minor_crossings and not caller_is_minor and seg.highway in _MINOR_UNCUT_HIGHWAYS:
                continue
            dx = seg.bx - seg.ax
            dy = seg.by - seg.ay
            len2 = dx * dx + dy * dy
            raw_t = ((x - seg.ax) * dx + (y - seg.ay) * dy) / len2 if len2 > 0.0 else 0.0
            if (raw_t < 0.0 and -raw_t * seg.seg_len > cap_m) or (
                raw_t > 1.0 and (raw_t - 1.0) * seg.seg_len > cap_m
            ):
                continue
            t = max(0.0, min(1.0, raw_t))
            ex = seg.ax + t * dx - x
            ey = seg.ay + t * dy - y
            dist2 = ex * ex + ey * ey
            reach = seg.half_width + margin_m
            if dist2 >= reach * reach:
                continue
            if z is not None and math.isfinite(z):
                seg_z = seg.az + (seg.bz - seg.az) * t
                if abs(seg_z - z) > 2.5:
                    continue
            if cut_all_angles:
                return True
            is_parallel = abs(seg.ux * tx + seg.uy * ty) > parallel_cos
            if not is_parallel:
                return True
            if cut_parallel_inside and dist2 < max(0.0, seg.half_width - 0.1) ** 2:
                return True
        return False


def build_carriageway_index(
    roads: Sequence[Road],
    points_by_osm_id: dict[int, list[np.ndarray]] | None = None,
) -> CarriagewayIndex:
    """Build a spatial index of all drivable non-tunnel road carriageways."""
    index = CarriagewayIndex()
    for road in roads:
        if not road.drivable or road.tunnel or not math.isfinite(road.width_m) or road.width_m <= 0.0:
            continue
        prio = surface_render_priority_for(road)
        polylines = points_by_osm_id.get(road.osm_id) if points_by_osm_id is not None else None
        if polylines:
            for pts in polylines:
                index.add_polyline(
                    pts,
                    half_width_m=road.half_width_m,
                    owner_id=road.osm_id,
                    layer=road.layer,
                    highway=road.highway,
                    render_priority=prio,
                )
        else:
            index.add_polyline(
                road.points,
                half_width_m=road.half_width_m,
                owner_id=road.osm_id,
                layer=road.layer,
                highway=road.highway,
                render_priority=prio,
            )
    return index


def cut_polyline_at_crossings(
    points_3d: np.ndarray,
    blocked_fn: Callable[[float, float, float, float, float], bool],
    *,
    sample_step_m: float = 0.5,
    min_piece_m: float = 1.5,
) -> list[np.ndarray]:
    """Split a 3D polyline into contiguous unblocked runs (dropped under ``min_piece_m``).

    Adapted from MapNG ``cutNodes`` (MIT License, Copyright (c) 2026 Nikki Luzader).
    ``blocked_fn(x, y, z, tx, ty)`` returns ``True`` where a paint/edge sample
    falls onto a crossing carriageway.
    """
    pts = np.asarray(points_3d, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 2:
        return []

    samples: list[tuple[np.ndarray, bool, bool]] = []
    for i in range(pts.shape[0] - 1):
        a = pts[i]
        b = pts[i + 1]
        dx = float(b[0] - a[0])
        dy = float(b[1] - a[1])
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-9:
            continue
        tx, ty = dx / seg_len, dy / seg_len
        steps = max(1, math.ceil(seg_len / sample_step_m))
        start_k = 0 if len(samples) == 0 else 1
        for k in range(start_k, steps + 1):
            t = k / steps
            p = a + (b - a) * t
            is_vertex = k == 0 or k == steps
            free = not blocked_fn(float(p[0]), float(p[1]), float(p[2]), tx, ty)
            samples.append((p, is_vertex, free))

    runs: list[np.ndarray] = []
    i = 0
    n_samples = len(samples)
    while i < n_samples:
        if not samples[i][2]:
            i += 1
            continue
        j = i
        while j + 1 < n_samples and samples[j + 1][2]:
            j += 1
        run_pts: list[np.ndarray] = [samples[i][0]]
        for k in range(i + 1, j):
            if samples[k][1]:
                run_pts.append(samples[k][0])
        if j > i:
            run_pts.append(samples[j][0])
        if len(run_pts) >= 2:
            arr = sanitize_road_nodes(np.asarray(run_pts, dtype=np.float64))
            if arr.shape[0] >= 2:
                deltas = np.diff(arr[:, :2], axis=0)
                length = float(np.hypot(deltas[:, 0], deltas[:, 1]).sum())
                if length >= min_piece_m:
                    runs.append(arr)
        i = j + 1
    return runs


def _compute_polyline_normals(pts: np.ndarray) -> np.ndarray:
    """Compute smoothed 2D unit left-hand normals along a polyline."""
    p_diff = np.diff(pts[:, :2], axis=0)
    p_lens = np.hypot(p_diff[:, 0], p_diff[:, 1])
    safe_p_lens = np.where(p_lens < 1e-6, 1.0, p_lens)
    tangents_seg = p_diff / safe_p_lens[:, None]

    normals = np.zeros((pts.shape[0], 2), dtype=np.float64)
    normals[0] = [-tangents_seg[0, 1], tangents_seg[0, 0]]
    normals[-1] = [-tangents_seg[-1, 1], tangents_seg[-1, 0]]
    if pts.shape[0] > 2:
        mid_tangents = (tangents_seg[:-1] + tangents_seg[1:]) * 0.5
        mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
        degenerate = mid_lens < 1e-6
        if np.any(degenerate):
            mid_tangents[degenerate] = tangents_seg[:-1][degenerate]
            mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
            mid_lens = np.where(mid_lens < 1e-6, 1.0, mid_lens)
        mid_tangents /= mid_lens[:, None]
        normals[1:-1] = np.column_stack((-mid_tangents[:, 1], mid_tangents[:, 0]))
    return normals


def _clip_polyline_endpoints(
    clean_pts: np.ndarray,
    *,
    start_clip_m: float,
    end_clip_from_start_m: float,
) -> np.ndarray:
    """Trim a 3D polyline to ``[start_clip_m, end_clip_from_start_m]`` along arc length."""
    deltas = np.diff(clean_pts[:, :2], axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    cum_dist = np.concatenate(([0.0], np.cumsum(seg_lens)))

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

    resampled_pts: list[tuple[float, float, float]] = [point_at_dist(start_clip_m)]
    for i, d_val in enumerate(cum_dist):
        d = float(d_val)
        if start_clip_m < d < end_clip_from_start_m:
            resampled_pts.append((float(clean_pts[i, 0]), float(clean_pts[i, 1]), float(clean_pts[i, 2])))
    resampled_pts.append(point_at_dist(end_clip_from_start_m))
    return _sanitize_xyz_points(np.asarray(resampled_pts, dtype=np.float64))


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
    carriageway_index: CarriagewayIndex | None = None,
    crossing_margin_m: float = 1.5,
    include_edge_lines: bool | None = None,
    ignored_ids: frozenset[int] | set[int] | None = None,
) -> list[dict[str, object]]:
    """Generate GOST markings for multi-lane roads (lanes >= 4) and wide 2-lane highways.

    White edge lines (``renderPriority=1``) and center/lane dividers
    (``renderPriority=2``) sit above base asphalt (``13..15``) per BeamNG 0.40's
    lower-on-top convention, and are trimmed back from junctions and crossing
    carriageways.

    Args:
        road: Road specification.
        level_id: Level ID for UUID generation.
        points_3d: (n, 3) 3D points along the road centerline.
        is_start_junction: Whether road start connects to another road/junction.
        is_end_junction: Whether road end connects to another road/junction.
        clip_distance_m: Fallback junction clip when ``carriageway_index`` is omitted.
        parent_key: Base key for naming overlay lines.
        parent: Scene parent group.
        max_segment_length_m: Maximum marking segment arc length.
        max_bbox_span_m: Maximum marking axis-aligned bounding-box span.
        carriageway_index: Optional spatial carriageway index for geometric
            ``roadMarkingCuts`` trimming (~2 m clear of crossing carriageways).
        crossing_margin_m: Clearance margin outside crossing carriageways.
        include_edge_lines: Emit ``line_left`` / ``line_right`` edge lines. Defaults
            to ``True`` on ``motorway``, ``trunk``, and ``primary`` roads.

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

    is_closed_loop = (
        math.hypot(float(clean_pts[0, 0] - clean_pts[-1, 0]), float(clean_pts[0, 1] - clean_pts[-1, 1])) < 0.5
    )
    effective_clip = crossing_margin_m if carriageway_index is not None else clip_distance_m
    start_clip = 0.0 if is_closed_loop else (effective_clip if is_start_junction else 0.0)
    end_clip = total_len if is_closed_loop else (total_len - (effective_clip if is_end_junction else 0.0))
    min_len = 6.0 if carriageway_index is not None else 12.0

    if end_clip - start_clip < min_len:
        return []

    clipped_pts = _clip_polyline_endpoints(clean_pts, start_clip_m=start_clip, end_clip_from_start_m=end_clip)
    if clipped_pts.shape[0] < 2:
        return []
    if road.bridge:
        clipped_pts = _sanitize_xyz_points(_resample_xyz_uniform(clipped_pts, 2.0), min_dist_m=0.5)
        if clipped_pts.shape[0] < 2:
            return []

    normals = _compute_polyline_normals(clipped_pts)
    num_lanes = road.lanes
    lane_width = road.width_m / float(num_lanes)
    base_key = parent_key or f"way{road.osm_id}"

    line_specs: list[tuple[str, float, str, int]] = []
    for k in range(1, num_lanes):
        offset = -road.width_m * 0.5 + k * lane_width
        is_center = (num_lanes >= 4) and (not road.oneway) and (k == num_lanes // 2)
        material = "m_line_white" if is_center else "m_line_white_discontinue"
        line_specs.append((f"{base_key}_mark_{k}", offset, material, CENTER_LINE_RENDER_PRIORITY))

    emit_edges = (road.highway in _MAJOR_MARKING_HIGHWAYS) if include_edge_lines is None else include_edge_lines
    if emit_edges and road.width_m >= (4.5 if road.oneway else 6.0):
        edge_inset = max(_EDGE_LINE_INSET_M, 0.45) if road.bridge else _EDGE_LINE_INSET_M
        edge_offset = max(0.5, road.half_width_m - edge_inset)
        line_specs.append((f"{base_key}_line_left", -edge_offset, "m_line_white", EDGE_LINE_RENDER_PRIORITY))
        line_specs.append((f"{base_key}_line_right", edge_offset, "m_line_white", EDGE_LINE_RENDER_PRIORITY))

    # Avoid double-adding bridge drape Z if points_3d was copied directly from a bridge DecalRoad node array
    z_delta = 0.0 if road.bridge else 0.01
    is_link_road = road.highway.endswith("_link")
    skip_minor = road.highway not in _MINOR_UNCUT_HIGHWAYS
    eff_seg_len = min(max_segment_length_m, MAX_BRIDGE_SEGMENT_LENGTH_M) if road.bridge else max_segment_length_m
    eff_bbox_span = min(max_bbox_span_m, MAX_BRIDGE_SEGMENT_LENGTH_M) if road.bridge else max_bbox_span_m
    markings: list[dict[str, object]] = []
    for key_prefix, offset, material, priority in line_specs:
        raw_line = np.column_stack(
            (
                clipped_pts[:, 0] + offset * normals[:, 0],
                clipped_pts[:, 1] + offset * normals[:, 1],
                clipped_pts[:, 2] + z_delta,
            )
        )
        clean_line = sanitize_road_nodes(
            raw_line,
            min_spacing_m=0.5 if road.bridge else MIN_ROAD_NODE_SPACING_M,
            reversal_dot=_OFFSET_STRIP_REVERSAL_DOT,
        )
        if clean_line.shape[0] < 2 or _polyline_arc_length_xy(clean_line) < 0.5:
            continue

        is_edge_line = key_prefix.endswith(("_line_left", "_line_right"))
        if carriageway_index is not None:
            piece_runs = cut_polyline_at_crossings(
                clean_line,
                lambda x, y, z, tx, ty, _edge=is_edge_line: carriageway_index.is_blocked(
                    x,
                    y,
                    margin_m=crossing_margin_m,
                    tangent=(tx, ty),
                    owner_id=road.osm_id,
                    layer=road.layer,
                    z=z,
                    cut_parallel_inside=(_edge or is_link_road),
                    ignored_ids=ignored_ids,
                    caller_highway=road.highway,
                    skip_minor_crossings=skip_minor,
                    end_cap_m=crossing_margin_m,
                ),
                min_piece_m=4.0,
            )
        else:
            piece_runs = [clean_line]

        for piece_idx, piece in enumerate(piece_runs):
            line_slices = _split_points_by_span(piece, eff_seg_len, eff_bbox_span)
            for seg_idx, sub_line in enumerate(line_slices):
                if _polyline_arc_length_xy(sub_line) < 0.5:
                    continue
                line_nodes = [[float(p[0]), float(p[1]), float(p[2]), 0.15] for p in sub_line]
                if len(piece_runs) == 1 and len(line_slices) == 1:
                    line_key = key_prefix
                elif len(piece_runs) == 1:
                    line_key = f"{key_prefix}_{seg_idx}"
                elif len(line_slices) == 1:
                    line_key = f"{key_prefix}_p{piece_idx}"
                else:
                    line_key = f"{key_prefix}_p{piece_idx}_{seg_idx}"
                line_obj: dict[str, Any] = build_road_object(
                    level_id,
                    key=line_key,
                    nodes=line_nodes,
                    material=material,
                    drivability=0.0,
                    parent=parent,
                    over_objects=bool(road.bridge),
                    gated_road=False,
                    texture_length=_MARKING_TEXTURE_LENGTH,
                    render_priority=priority,
                    break_angle=1.0,
                    distance_fade=(150.0, 50.0),
                    start_end_fade=(0.0, 0.0),
                )
                line_obj["name"] = line_key
                line_obj["detail"] = DECAL_ROAD_DETAIL
                line_obj["startTangent"] = 0
                line_obj["endTangent"] = 0
                line_obj["decalBias"] = 0.005
                line_obj["autoJunction"] = False
                line_obj["autoLanes"] = False
                line_obj["oneWay"] = bool(road.oneway)
                markings.append(line_obj)

    return markings


def generate_edge_decals(
    road: Road,
    level_id: str,
    points_3d: np.ndarray,
    *,
    parent_key: str | None = None,
    parent: str = "road_network",
    max_segment_length_m: float = MAX_SEGMENT_LENGTH_M,
    max_bbox_span_m: float = MAX_SEGMENT_BBOX_SPAN_M,
    carriageway_index: CarriagewayIndex | None = None,
    ignored_ids: frozenset[int] | set[int] | None = None,
    margin_m: float = 1.25,
) -> list[dict[str, object]]:
    """Generate feathered asphalt-to-grass shoulder edge decals along ground asphalt roads.

    Omitted on bridges, tunnels, and unpaved roads. ``edge_left`` nodes are
    reversed so the asymmetric grass edge faces outward on both sides, matching
    MapNG's ``EDGE_BLEND`` pattern.
    """
    if not _should_generate_edge_decals(road):
        return []
    if not math.isfinite(road.width_m) or road.width_m <= 0.0:
        return []

    clean_pts = _sanitize_xyz_points(points_3d)
    if clean_pts.shape[0] < 2:
        return []

    from ivanovo2beam.beamng.level import build_road_object

    edge_width = 2.5 if road.highway in _MAJOR_EDGE_HIGHWAYS else 2.0
    overlap = edge_blend_overlap(edge_width)
    lateral = road.half_width_m + edge_width * 0.5 - overlap
    normals = _compute_polyline_normals(clean_pts)
    base_key = parent_key or f"way{road.osm_id}"
    skip_minor = road.highway not in _MINOR_UNCUT_HIGHWAYS

    edges: list[dict[str, object]] = []
    for side_name, sign, reverse_nodes in (("edge_left", -1.0, True), ("edge_right", 1.0, False)):
        raw_strip = np.column_stack(
            (
                clean_pts[:, 0] + sign * lateral * normals[:, 0],
                clean_pts[:, 1] + sign * lateral * normals[:, 1],
                clean_pts[:, 2],
            )
        )
        clean_strip = sanitize_road_nodes(
            raw_strip,
            min_spacing_m=0.25,
            reversal_dot=_OFFSET_STRIP_REVERSAL_DOT,
        )
        if clean_strip.shape[0] < 2 or _polyline_arc_length_xy(clean_strip) < 2.0:
            continue

        if carriageway_index is not None:
            piece_runs = cut_polyline_at_crossings(
                clean_strip,
                lambda x, y, z, tx, ty: carriageway_index.is_blocked(
                    x,
                    y,
                    margin_m=margin_m,
                    tangent=(tx, ty),
                    owner_id=road.osm_id,
                    layer=road.layer,
                    z=z,
                    cut_all_angles=True,
                    ignored_ids=ignored_ids,
                    caller_highway=road.highway,
                    skip_minor_crossings=skip_minor,
                    end_cap_m=margin_m,
                ),
                min_piece_m=6.0,
            )
        else:
            piece_runs = [clean_strip]

        for piece_idx, piece in enumerate(piece_runs):
            oriented = piece[::-1] if reverse_nodes else piece
            strip_slices = _split_points_by_span(oriented, max_segment_length_m, max_bbox_span_m)
            for seg_idx, sub_strip in enumerate(strip_slices):
                slice_len = _polyline_arc_length_xy(sub_strip)
                if slice_len < 2.0:
                    continue
                strip_nodes = [[float(p[0]), float(p[1]), float(p[2]), edge_width] for p in sub_strip]
                if len(piece_runs) == 1 and len(strip_slices) == 1:
                    edge_key = f"{base_key}_{side_name}"
                elif len(piece_runs) == 1:
                    edge_key = f"{base_key}_{side_name}_{seg_idx}"
                elif len(strip_slices) == 1:
                    edge_key = f"{base_key}_{side_name}_p{piece_idx}"
                else:
                    edge_key = f"{base_key}_{side_name}_p{piece_idx}_{seg_idx}"
                edge_obj: dict[str, Any] = build_road_object(
                    level_id,
                    key=edge_key,
                    nodes=strip_nodes,
                    material="m_road_asphalt_edge_grass",
                    drivability=0.0,
                    parent=parent,
                    over_objects=False,
                    gated_road=False,
                    texture_length=_EDGE_GRASS_TEXTURE_LENGTH,
                    render_priority=EDGE_GRASS_RENDER_PRIORITY,
                    break_angle=2.0,
                    distance_fade=(150.0, 45.0),
                    start_end_fade=_clamp_fade_for_length((0.5, 0.5), slice_len),
                )
                edge_obj["name"] = edge_key
                edge_obj["detail"] = DECAL_ROAD_DETAIL
                edge_obj["startTangent"] = 0
                edge_obj["endTangent"] = 0
                edge_obj["decalBias"] = 0.0025
                edge_obj["autoJunction"] = False
                edge_obj["autoLanes"] = False
                edges.append(edge_obj)

    return edges


# ---------------------------------------------------------------------------
# Pure road-profile & junction Z synchronization helpers (for terrain/builder)
# ---------------------------------------------------------------------------


def _resample_xy_uniform(points: np.ndarray, step_m: float) -> np.ndarray:
    """Resample a 2D polyline at uniform arc-length intervals ``<= step_m``."""
    pts = np.asarray(points, dtype=np.float64)
    if pts.shape[0] < 2 or step_m <= 0.0:
        return pts[:, :2].copy()
    deltas = np.diff(pts[:, :2], axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    total_len = float(seg_lens.sum())
    if total_len <= 1e-6:
        return pts[:2, :2].copy()
    cum = np.concatenate(([0.0], np.cumsum(seg_lens)))
    n_samples = max(2, math.ceil(total_len / step_m) + 1)
    targets = np.linspace(0.0, total_len, n_samples)
    xs = np.interp(targets, cum, pts[:, 0])
    ys = np.interp(targets, cum, pts[:, 1])
    return np.column_stack((xs, ys))


def sample_road_profile_points(
    points: np.ndarray,
    elevation_sampler: Callable[[np.ndarray], np.ndarray],
    *,
    sample_step_m: float = 5.0,
    smooth_window_m: float = 20.0,
    smooth_passes: int = 2,
    max_grade: float = 0.08,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample, smooth, and grade-limit a longitudinal road profile.

    Pure helper usable from terrain corridor grading without coupling to DEM I/O.

    Args:
        points: ``(n, 2)`` world-space road centreline in metres.
        elevation_sampler: Callable mapping ``(m, 2)`` XY coordinates to ``(m,)``
            elevations in metres.
        sample_step_m: Resampling interval along the polyline.
        smooth_window_m: Moving-average smoothing window in metres.
        smooth_passes: Number of boxcar smoothing passes.
        max_grade: Maximum longitudinal grade clamp (rise / run).

    Returns:
        ``(resampled_xy, smooth_profile_z)`` arrays.
    """
    resampled = _resample_xy_uniform(points, sample_step_m)
    raw = np.asarray(elevation_sampler(resampled), dtype=np.float64)
    if raw.shape[0] != resampled.shape[0]:
        raise ValueError(f"elevation_sampler returned {raw.shape[0]} heights for {resampled.shape[0]} points")

    if not np.all(np.isfinite(raw)):
        finite = np.isfinite(raw)
        if not finite.any():
            raise ValueError("elevation_sampler returned no finite elevations along road centreline")
        idx = np.arange(raw.size)
        raw = np.interp(idx, idx[finite], raw[finite])

    z = raw.copy()
    deltas = np.diff(resampled, axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1]) if resampled.shape[0] >= 2 else np.empty(0, dtype=np.float64)
    cum = np.concatenate(([0.0], np.cumsum(seg_lens))) if seg_lens.size else np.zeros(z.size, dtype=np.float64)

    half_win = max(0.0, smooth_window_m * 0.5)
    for _ in range(max(0, smooth_passes)):
        if z.size < 3 or half_win <= 0.0:
            break
        nxt = np.empty_like(z)
        for i in range(z.size):
            d_i = float(cum[i])
            mask = np.abs(cum - d_i) <= half_win
            nxt[i] = float(np.mean(z[mask])) if mask.any() else float(z[i])
        z = nxt

    if max_grade > 0.0 and z.size >= 2:
        for i in range(1, z.size):
            limit = max_grade * float(seg_lens[i - 1])
            z[i] = min(max(z[i], z[i - 1] - limit), z[i - 1] + limit)
        for i in range(z.size - 2, -1, -1):
            limit = max_grade * float(seg_lens[i])
            z[i] = min(max(z[i], z[i + 1] - limit), z[i + 1] + limit)

    return resampled, z


def _junction_key_xy(pt: np.ndarray, snap_tol_m: float) -> tuple[int, int]:
    return (round(float(pt[0]) / snap_tol_m), round(float(pt[1]) / snap_tol_m))


def pin_junction_elevations(
    corridors: Sequence[Any],
    *,
    ramp_m: float = 40.0,
    snap_tol_m: float = 0.5,
    through_cos: float = -0.7,
    arm_dir_m: float = 8.0,
) -> dict[str, int]:
    """Synchronize shared intersection elevations and seat side roads into through roads.

    Adapted from MapNG ``roadSmoother.js`` (MIT License, Copyright (c) 2026
    Nikki Luzader). Operates in place on ``corridor.surface_z`` for every
    graded corridor with finite elevations.

    Args:
        corridors: Sequence of road corridors exposing ``.points``,
            ``.surface_z``, ``.priority``, ``.half_width_m``, and ``.grade_terrain``.
        ramp_m: Distance along each arm over which junction offsets taper out.
        snap_tol_m: Spatial tolerance in metres for matching shared junction nodes.
        through_cos: Cosine threshold (default ``-0.7``) identifying two opposite
            arms as one continuous through road at a junction.
        arm_dir_m: Lookahead length in metres for estimating arm direction.

    Returns:
        Summary counts ``{"junctions_pinned", "arms_adjusted", "side_arms_seated"}``.
    """
    active: list[tuple[int, np.ndarray, np.ndarray, np.ndarray, float, float]] = []
    for idx, c in enumerate(corridors):
        if not getattr(c, "grade_terrain", True):
            continue
        sz = getattr(c, "surface_z", None)
        pts = getattr(c, "points", None)
        if sz is None or pts is None:
            continue
        pts_arr = np.asarray(pts, dtype=np.float64)
        sz_arr = np.asarray(sz, dtype=np.float64).copy()
        if pts_arr.ndim != 2 or pts_arr.shape[0] < 2 or sz_arr.shape[0] != pts_arr.shape[0]:
            continue
        if not np.all(np.isfinite(sz_arr)):
            continue
        deltas = np.diff(pts_arr[:, :2], axis=0)
        cum = np.concatenate(([0.0], np.cumsum(np.hypot(deltas[:, 0], deltas[:, 1]))))
        priority = float(getattr(c, "priority", 1.0) or 1.0)
        half_w = float(getattr(c, "half_width_m", 4.0) or 4.0)
        active.append((idx, pts_arr, sz_arr, cum, priority, half_w))

    if not active:
        return {"junctions_pinned": 0, "arms_adjusted": 0, "side_arms_seated": 0}

    # Index endpoint arrivals and interior T-junction anchors
    end_agg: dict[tuple[int, int], list[tuple[int, bool, float, float]]] = {}
    interior_anchors: dict[tuple[int, int], tuple[float, float]] = {}
    for a_i, (_, pts_arr, sz_arr, _, prio, _) in enumerate(active):
        k0 = _junction_key_xy(pts_arr[0], snap_tol_m)
        k1 = _junction_key_xy(pts_arr[-1], snap_tol_m)
        end_agg.setdefault(k0, []).append((a_i, False, float(sz_arr[0]), prio))
        end_agg.setdefault(k1, []).append((a_i, True, float(sz_arr[-1]), prio))
        for vi in range(1, pts_arr.shape[0] - 1):
            kv = _junction_key_xy(pts_arr[vi], snap_tol_m)
            prev = interior_anchors.get(kv)
            if prev is None or prio > prev[1]:
                interior_anchors[kv] = (float(sz_arr[vi]), prio)

    consensus: dict[tuple[int, int], float] = {}
    for key, entries in end_agg.items():
        anchor = interior_anchors.get(key)
        if len(entries) < 2 and anchor is None:
            continue
        if anchor is not None:
            # Through road interior vertex anchors T-junction arms cleanly
            w_sum = anchor[1]
            wz_sum = anchor[0] * anchor[1]
            for _, _, z_val, prio in entries:
                w = max(0.25, prio * 0.25)
                w_sum += w
                wz_sum += z_val * w
            consensus[key] = wz_sum / w_sum
        else:
            w_sum = sum(max(1.0, e[3]) for e in entries)
            wz_sum = sum(e[2] * max(1.0, e[3]) for e in entries)
            consensus[key] = wz_sum / w_sum

    arms_adjusted = 0
    for _, pts_arr, sz_arr, cum, _, _ in active:
        total = float(cum[-1])
        eff_ramp = min(ramp_m, total * 0.5)
        if eff_ramp <= 0.0:
            continue
        for at_end in (False, True):
            key = _junction_key_xy(pts_arr[-1] if at_end else pts_arr[0], snap_tol_m)
            target_z = consensus.get(key)
            if target_z is None:
                continue
            z_end = float(sz_arr[-1] if at_end else sz_arr[0])
            delta = target_z - z_end
            if abs(delta) < 1e-9:
                continue
            arms_adjusted += 1
            if not at_end:
                for i in range(sz_arr.size):
                    f = 1.0 - float(cum[i]) / eff_ramp
                    if f <= 0.0:
                        break
                    s = f * f * (3.0 - 2.0 * f)
                    sz_arr[i] += delta * s
            else:
                for i in range(sz_arr.size - 1, -1, -1):
                    f = 1.0 - float(total - cum[i]) / eff_ramp
                    if f <= 0.0:
                        break
                    s = f * f * (3.0 - 2.0 * f)
                    sz_arr[i] += delta * s

    # Step 2: seat side arms onto through-road decks at 3+ way junctions
    arms_by_key: dict[tuple[int, int], list[tuple[int, bool, float, float]]] = {}
    for a_i, (_, pts_arr, _, cum, _, _) in enumerate(active):
        k0 = _junction_key_xy(pts_arr[0], snap_tol_m)
        k1 = _junction_key_xy(pts_arr[-1], snap_tol_m)
        if k0 == k1:
            continue
        for at_end, key in ((False, k0), (True, k1)):
            n_pts = pts_arr.shape[0]
            i0 = n_pts - 1 if at_end else 0
            step = -1 if at_end else 1
            i = i0
            while 0 <= i + step < n_pts and abs(float(cum[i] - cum[i0])) < arm_dir_m:
                i += step
            dx = float(pts_arr[i, 0] - pts_arr[i0, 0])
            dy = float(pts_arr[i, 1] - pts_arr[i0, 1])
            length = math.hypot(dx, dy)
            if length <= 1e-9:
                continue
            arms_by_key.setdefault(key, []).append((a_i, at_end, dx / length, dy / length))

    side_arms_seated = 0
    for arm_list in arms_by_key.values():
        if len(arm_list) < 3:
            continue
        through_indices: set[int] = set()
        for a in range(len(arm_list)):
            for b in range(a + 1, len(arm_list)):
                if arm_list[a][2] * arm_list[b][2] + arm_list[a][3] * arm_list[b][3] <= through_cos:
                    through_indices.add(a)
                    through_indices.add(b)
        if not through_indices or len(through_indices) == len(arm_list):
            continue
        mains = [arm_list[i] for i in through_indices]

        def through_surface_at(x: float, y: float, mains_list: list[tuple[int, bool, float, float]] = mains) -> tuple[float, float, float] | None:
            best: tuple[float, float, float] | None = None
            for m_idx, m_end, _, _ in mains_list:
                _, m_pts, m_sz, m_cum, _, m_hw = active[m_idx]
                m_n = m_pts.shape[0]
                mi0 = m_n - 1 if m_end else 0
                m_step = -1 if m_end else 1
                mi = mi0
                while 0 <= mi + m_step < m_n and abs(float(m_cum[mi] - m_cum[mi0])) < 80.0:
                    ax, ay = float(m_pts[mi, 0]), float(m_pts[mi, 1])
                    bx, by = float(m_pts[mi + m_step, 0]), float(m_pts[mi + m_step, 1])
                    za, zb = float(m_sz[mi]), float(m_sz[mi + m_step])
                    dx, dy = bx - ax, by - ay
                    len2 = dx * dx + dy * dy
                    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / len2)) if len2 > 0.0 else 0.0
                    dist = math.hypot(ax + t * dx - x, ay + t * dy - y)
                    if best is None or dist < best[0]:
                        best = (dist, za + t * (zb - za), m_hw)
                    mi += m_step
            return best

        for arm_pos, (a_i, at_end, adx, ady) in enumerate(arm_list):
            if arm_pos in through_indices:
                continue
            if any(adx * m[2] + ady * m[3] >= 0.85 for m in mains):
                continue
            _, pts_arr, sz_arr, cum, _, _ = active[a_i]
            n_pts = pts_arr.shape[0]
            i0 = n_pts - 1 if at_end else 0
            step = -1 if at_end else 1
            total = float(cum[-1])
            edge_idx = -1
            z_edge = 0.0
            i = i0 + step
            while 0 <= i < n_pts and abs(float(cum[i] - cum[i0])) <= total * 0.5:
                surf = through_surface_at(float(pts_arr[i, 0]), float(pts_arr[i, 1]))
                if surf is None:
                    break
                if surf[0] >= surf[2]:
                    edge_idx = i
                    z_edge = surf[1]
                    break
                i += step
            if edge_idx < 0:
                continue
            reach = abs(float(cum[edge_idx] - cum[i0]))
            delta = z_edge - float(sz_arr[edge_idx])
            eff_ramp = min(ramp_m, total * 0.5 - reach)
            k = edge_idx
            while 0 <= k < n_pts:
                from_edge = abs(float(cum[k] - cum[i0])) - reach
                f = (1.0 - from_edge / eff_ramp) if eff_ramp > 0.0 else (1.0 if k == edge_idx else 0.0)
                if f <= 0.0:
                    break
                s = f * f * (3.0 - 2.0 * f)
                sz_arr[k] += delta * s
                k += step
            k = i0 + step
            while k != edge_idx:
                surf = through_surface_at(float(pts_arr[k, 0]), float(pts_arr[k, 1]))
                if surf is not None:
                    sz_arr[k] = surf[1]
                k += step
            side_arms_seated += 1

    # Write synchronized elevation arrays back to corridors
    for orig_idx, _, sz_arr, _, _, _ in active:
        corridors[orig_idx].surface_z = sz_arr

    return {
        "junctions_pinned": len(consensus),
        "arms_adjusted": arms_adjusted,
        "side_arms_seated": side_arms_seated,
    }


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
    level_dir: Path | None = None,
    osm_data: object | None = None,
    projection: object | None = None,
    include_props: bool = True,
) -> dict[str, int]:
    """Add every road in a network to a level scene as layered DecalRoads.

    Exports base surfaces (with ``0 m`` fade at junctions/internal splits and
    ``2 m`` fade at true dead-ends), roadside grass-edge transition decals on
    ground asphalt roads, and crossing-cut lane/edge markings.

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
    # deduplicating per road so closed loops do not self-flag as junctions) plus
    # endpoint tangents to identify collinear continuations & bridge portals.
    vertex_counts: dict[tuple[int, int], int] = {}
    endpoint_arms: dict[tuple[int, int], list[tuple[int, bool, float, float, bool]]] = {}
    for r in roads:
        if r.drivable and not r.tunnel and r.points.shape[0] >= 2:
            seen_verts: set[tuple[int, int]] = set()
            for pt in r.points:
                if math.isfinite(float(pt[0])) and math.isfinite(float(pt[1])):
                    seen_verts.add((round(float(pt[0] * 2)), round(float(pt[1] * 2))))
            for v in seen_verts:
                vertex_counts[v] = vertex_counts.get(v, 0) + 1
            for at_end in (False, True):
                p_end = r.points[-1] if at_end else r.points[0]
                p_in = r.points[-2] if at_end else r.points[1]
                if not (
                    math.isfinite(float(p_end[0]))
                    and math.isfinite(float(p_end[1]))
                    and math.isfinite(float(p_in[0]))
                    and math.isfinite(float(p_in[1]))
                ):
                    continue
                dx = float(p_in[0] - p_end[0])
                dy = float(p_in[1] - p_end[1])
                length = math.hypot(dx, dy)
                if length <= 1e-6:
                    continue
                key = (round(float(p_end[0] * 2)), round(float(p_end[1] * 2)))
                endpoint_arms.setdefault(key, []).append(
                    (r.osm_id, at_end, dx / length, dy / length, bool(r.bridge))
                )

    through_partners: dict[int, set[int]] = {}
    edge_partners: dict[int, set[int]] = {}
    continuous_ends: set[tuple[int, bool]] = set()
    road_prio_by_id = {r.osm_id: surface_render_priority_for(r) for r in roads}
    for key, arms in endpoint_arms.items():
        if len(arms) < 2:
            continue
        if len(arms) == 2 and vertex_counts.get(key, 0) == 2:
            (id_a, end_a, dxa, dya, br_a), (id_b, end_b, dxb, dyb, br_b) = arms
            if id_a != id_b and dxa * dxb + dya * dyb <= -0.7:
                through_partners.setdefault(id_a, set()).add(id_b)
                through_partners.setdefault(id_b, set()).add(id_a)
                edge_partners.setdefault(id_a, set()).add(id_b)
                edge_partners.setdefault(id_b, set()).add(id_a)
                continuous_ends.add((id_a, end_a))
                continuous_ends.add((id_b, end_b))
            continue

        # At 3+ way forks/intersections, only link the single best collinear through-pair
        # (preferring matching road tier) so diverging ramps/branches still cut edge decals
        # and side edge lines cleanly.
        best_pair: tuple[float, int, int, bool, bool, bool, bool, float] | None = None
        for i in range(len(arms)):
            id_a, end_a, dxa, dya, br_a = arms[i]
            pa = road_prio_by_id.get(id_a, _DEFAULT_SURFACE_RENDER_PRIORITY)
            for j in range(i + 1, len(arms)):
                id_b, end_b, dxb, dyb, br_b = arms[j]
                if id_a == id_b:
                    continue
                dot = dxa * dxb + dya * dyb
                if dot > -0.7:
                    continue
                pb = road_prio_by_id.get(id_b, _DEFAULT_SURFACE_RENDER_PRIORITY)
                score = abs(pa - pb) * 2.0 + (dot + 1.0)
                if best_pair is None or score < best_pair[0]:
                    best_pair = (score, id_a, id_b, end_a, end_b, br_a, br_b, dot)
        if best_pair is not None:
            _, id_a, id_b, end_a, end_b, br_a, br_b, dot = best_pair
            through_partners.setdefault(id_a, set()).add(id_b)
            through_partners.setdefault(id_b, set()).add(id_a)
            if dot <= -0.92 and abs(road_prio_by_id.get(id_a, 99) - road_prio_by_id.get(id_b, 99)) <= 1:
                edge_partners.setdefault(id_a, set()).add(id_b)
                edge_partners.setdefault(id_b, set()).add(id_a)
            if br_a != br_b:
                continuous_ends.add((id_a, end_a))
                continuous_ends.add((id_b, end_b))

    parent_name = group_path[-1] if group_path else "road_network"
    terrain_arg: object | None = build if hasattr(build, "elevations") else None

    exported_bases: list[tuple[Road, list[dict[str, object]], bool, bool]] = []
    points_by_osm_id: dict[int, list[np.ndarray]] = {}
    raw_index = build_carriageway_index(roads)
    minor_stub_highways = frozenset({"service", "track", "path", "living_street"})

    def _is_junction_endpoint(pt: np.ndarray, road: Road) -> bool:
        if not (math.isfinite(float(pt[0])) and math.isfinite(float(pt[1]))):
            return False
        key = (round(float(pt[0] * 2)), round(float(pt[1] * 2)))
        if vertex_counts.get(key, 0) > 1:
            return True
        return raw_index.is_blocked(
            float(pt[0]),
            float(pt[1]),
            margin_m=1.0,
            tangent=(0.0, 0.0),
            owner_id=road.osm_id,
            layer=road.layer,
            cut_parallel_inside=True,
        )

    for road in roads:
        if road.points.shape[0] >= 2:
            is_start_junc = _is_junction_endpoint(road.points[0], road)
            is_end_junc = _is_junction_endpoint(road.points[-1], road)
        else:
            is_start_junc = False
            is_end_junc = False
        dead_fade = (
            min(4.0, max(1.0, road.length_m * 0.25))
            if road.highway in minor_stub_highways
            else 2.0
        )
        start_fade = 0.0 if (is_start_junc or road.bridge) else dead_fade
        end_fade = 0.0 if (is_end_junc or road.bridge) else dead_fade

        objs = road_to_decal_objects(
            road,
            tree.level_id,
            terrain_build=terrain_arg,
            max_node_spacing_m=max_node_spacing_m,
            parent=parent_name,
            start_end_fade=(start_fade, end_fade),
        )
        if not objs:
            skipped += 1
            continue

        for obj in objs:
            tree.add_object(group_path, obj)
            exported += 1
            nodes += len(obj["nodes"])  # type: ignore[arg-type]
            points_by_osm_id.setdefault(road.osm_id, []).append(
                np.asarray(obj["nodes"], dtype=np.float64)[:, :3]
            )
        exported_bases.append((road, objs, is_start_junc, is_end_junc))

    if not exported_bases:
        return {"exported": exported, "skipped": skipped, "nodes": nodes}

    carriageway_index = build_carriageway_index(roads, points_by_osm_id=points_by_osm_id)

    for road, objs, is_start_junc, is_end_junc in exported_bases:
        emit_edges = _should_generate_edge_decals(road)
        emit_marks = _should_generate_markings(road)
        if not emit_edges and not emit_marks:
            continue

        e_partners = frozenset(edge_partners.get(road.osm_id, ()))
        m_partners = frozenset(through_partners.get(road.osm_id, ()))
        for seg_idx, obj in enumerate(objs):
            obj_nodes = np.asarray(obj["nodes"], dtype=np.float64)[:, :3]
            base_k = f"way{road.osm_id}_{seg_idx}" if len(objs) > 1 else f"way{road.osm_id}"
            if emit_edges:
                edge_objs = generate_edge_decals(
                    road,
                    tree.level_id,
                    obj_nodes,
                    parent_key=base_k,
                    parent=parent_name,
                    carriageway_index=carriageway_index,
                    ignored_ids=e_partners,
                )
                for edge_obj in edge_objs:
                    tree.add_object(group_path, edge_obj)
                    exported += 1
                    nodes += len(edge_obj["nodes"])  # type: ignore[arg-type]

            if emit_marks:
                seg_is_start = (
                    is_start_junc and (road.osm_id, False) not in continuous_ends
                    if seg_idx == 0
                    else False
                )
                seg_is_end = (
                    is_end_junc and (road.osm_id, True) not in continuous_ends
                    if seg_idx == len(objs) - 1
                    else False
                )
                markings = generate_lane_markings(
                    road,
                    tree.level_id,
                    obj_nodes,
                    is_start_junction=seg_is_start,
                    is_end_junction=seg_is_end,
                    parent_key=base_k,
                    parent=parent_name,
                    carriageway_index=carriageway_index,
                    ignored_ids=m_partners,
                )
                for mark_obj in markings:
                    tree.add_object(group_path, mark_obj)
                    exported += 1
                    nodes += len(mark_obj["nodes"])  # type: ignore[arg-type]

    if include_props:
        from ivanovo2beam.roads.props import add_roadside_props_to_scene

        prop_counts = add_roadside_props_to_scene(
            tree,
            roads,
            build if hasattr(build, "elevations") else None,
            level_dir=level_dir,
            osm_data=osm_data,  # type: ignore[arg-type]
            projection=projection,  # type: ignore[arg-type]
            carriageway_index=carriageway_index,
        )
        return {"exported": exported, "skipped": skipped, "nodes": nodes, **prop_counts}

    return {"exported": exported, "skipped": skipped, "nodes": nodes}
