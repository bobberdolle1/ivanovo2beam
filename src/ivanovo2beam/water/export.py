"""BeamNG 0.39 scene object export for hydrology.

Emits:
- BeamNG ``River`` spline objects in ``("level_objects", "water")`` for linear
  waterways (rivers, canals, streams) with 8-tuple nodes ``[x, y, z_water, width, depth, 0, 0, 1]``,
  physics flow, wave animation, and shared textures from
  ``/assets/materials/tileable/water/...``.
- BeamNG ``WaterBlock`` objects in ``("level_objects", "water")`` for standing water
  (lakes, ponds, Volga reservoir).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon

from ivanovo2beam.beamng.level import ROOT_GROUP, stable_uuid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ivanovo2beam.beamng.level import SceneTree
    from ivanovo2beam.water.extract import RiverChannel, WaterBody, WaterNetwork

#: Standard shared BeamNG water texture paths
DEFAULT_DEPTH_GRADIENT_TEX = "/assets/materials/tileable/water/depthcolor_ramp/depthcolor_ramp_italy_rivers_b.png"
DEFAULT_FOAM_TEX = "/assets/materials/tileable/water/water_effects/foam2_b.color.dds"
DEFAULT_RIPPLE_TEX = "/assets/materials/tileable/water/water_effects/ripple3_nm.normal.dds"
DEFAULT_WATER_CUBEMAP = "BNG_Sky_02_cubemap"
WATER_GROUP = ("level_objects", "water")


def build_river_object(
    level_id: str,
    channel: RiverChannel,
    *,
    depth_gradient_tex: str = DEFAULT_DEPTH_GRADIENT_TEX,
    foam_tex: str = DEFAULT_FOAM_TEX,
    ripple_tex: str = DEFAULT_RIPPLE_TEX,
    cubemap: str = DEFAULT_WATER_CUBEMAP,
    parent: str = "water",
) -> dict[str, Any]:
    """Construct a BeamNG ``River`` scene object from a :class:`RiverChannel`."""
    nodes = [
        [
            round(float(x), 5),
            round(float(y), 5),
            round(float(zw), 5),
            round(float(channel.width_m), 2),
            round(float(channel.depth_m), 2),
            0.0,
            0.0,
            1.0,
        ]
        for (x, y), zw in zip(channel.points, channel.z_water, strict=True)
    ]
    if len(nodes) < 2:
        raise ValueError(f"River {channel.osm_id} requires at least 2 nodes, got {len(nodes)}")

    return {
        "name": f"river_{channel.osm_id}",
        "class": "River",
        "persistentId": stable_uuid(level_id, "River", str(channel.osm_id)),
        "__parent": parent,
        "position": nodes[0][:3],
        "nodes": nodes,
        "subdivideLength": 4.0,
        "baseColor": [44, 74, 60, 255],
        "cubemap": cubemap,
        "depthGradientMax": 10.0,
        "depthGradientTex": depth_gradient_tex,
        "distortEndDist": 1.0,
        "flowMagnitudePhysics": round(float(channel.flow_speed), 2),
        "foamAmbientLerp": 0.8,
        "foamMaxDepth": 0.4,
        "foamRippleInfluence": 0.05,
        "foamTex": foam_tex,
        "fresnelBias": 0.12,
        "fresnelPower": 6.0,
        "fullReflect": False,
        "lowLODDistance": 80.0,
        "overallFoamOpacity": 0.0,
        "overallRippleMagnitude": 0.15,
        "overallWaveMagnitude": 0.02,
        "reflectivity": 0.55,
        "rippleTex": ripple_tex,
        "specularPower": 200.0,
        "useOcclusionQuery": False,
        "waterFogDensity": 1.0,
        "waterFogDensityOffset": 0.1,
        "wetDarkening": 0.30,
        "wetDepth": 0.45,
        "Foam": [
            {"foamDir": [0, -1], "foamSpeed": 0.075, "foamTexScale": [3, 3]},
            {"foamDir": [0.4, 0], "foamSpeed": 0.075, "foamTexScale": [10, 10]},
        ],
        "Ripples (texture animation)": [
            {"rippleDir": [0, -1], "rippleMagnitude": 0.6, "rippleSpeed": -1.0, "rippleTexScale": [0.5, 0.5]},
            {"rippleDir": [0.5, 1], "rippleMagnitude": 0.4, "rippleSpeed": 0.06, "rippleTexScale": [7, 7]},
            {"rippleDir": [-0.5, 1], "rippleMagnitude": 0.35, "rippleSpeed": 0.015, "rippleTexScale": [30, 50]},
        ],
        "Waves (vertex undulation)": [
            {"waveDir": [0, 1], "waveMagnitude": 0.05, "waveSpeed": 2.5},
            {"waveDir": [0.707, 0.707], "waveMagnitude": 0.05, "waveSpeed": 1.5},
            {"waveDir": [0.5, 0.86], "waveMagnitude": 0.05, "waveSpeed": 1.5},
        ],
    }


def compute_water_block_grid_element_size(size_x: float, size_y: float) -> float:
    """Compute adaptive ``gridElementSize`` capped below BeamNG's default 100m scale limit."""
    sx = float(size_x)
    sy = float(size_y)
    max_dim = max(sx, sy)
    min_dim = min(sx, sy)
    return round(min(max(8.0, max_dim / 64.0), 96.0, max(1.0, min_dim)), 3)


def build_water_block_object(
    level_id: str,
    body: WaterBody,
    *,
    depth_gradient_tex: str = DEFAULT_DEPTH_GRADIENT_TEX,
    foam_tex: str = DEFAULT_FOAM_TEX,
    ripple_tex: str = DEFAULT_RIPPLE_TEX,
    cubemap: str = DEFAULT_WATER_CUBEMAP,
    parent: str = "water",
    bounds_override: tuple[float, float, float, float] | None = None,
    name_suffix: str = "",
) -> dict[str, Any]:
    """Construct a BeamNG ``WaterBlock`` scene object from a :class:`WaterBody`."""
    min_x, min_y, max_x, max_y = bounds_override if bounds_override is not None else body.bounds
    size_x = max(2.0, max_x - min_x)
    size_y = max(2.0, max_y - min_y)
    center_x = 0.5 * (min_x + max_x)
    center_y = 0.5 * (min_y + max_y)
    depth_z = max(1.0, float(body.depth_m))
    grid_element_size = compute_water_block_grid_element_size(size_x, size_y)
    obj_id = f"{body.osm_id}{name_suffix}"

    return {
        "name": f"waterblock_{obj_id}",
        "class": "WaterBlock",
        "persistentId": stable_uuid(level_id, "WaterBlock", obj_id),
        "__parent": parent,
        "position": [round(center_x, 5), round(center_y, 5), round(body.z_water, 5)],
        "scale": [round(size_x, 5), round(size_y, 5), round(depth_z, 5)],
        "rotationMatrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "baseColor": [44, 74, 60, 255],
        "cubemap": cubemap,
        "depthGradientMax": 30.0,
        "depthGradientTex": depth_gradient_tex,
        "foamAmbientLerp": 0.8,
        "foamMaxDepth": 0.15,
        "foamRippleInfluence": 0.015,
        "foamTex": foam_tex,
        "fresnelBias": 0.12,
        "fresnelPower": 6.0,
        "fullReflect": False,
        "gridElementSize": grid_element_size,
        "gridSize": 1,
        "overallFoamOpacity": 0.0,
        "overallRippleMagnitude": 0.15,
        "overallWaveMagnitude": 0.015,
        "reflectivity": 0.55,
        "rippleTex": ripple_tex,
        "specularPower": 200.0,
        "useOcclusionQuery": False,
        "waterFogDensity": 1.0,
        "waterFogDensityOffset": 0.1,
        "wetDarkening": 0.35,
        "wetDepth": 0.35,
        "Foam": [{}, {}],
        "Ripples (texture animation)": [
            {"rippleDir": [0, 1], "rippleMagnitude": 0.6, "rippleSpeed": 0.001, "rippleTexScale": [12, 12]},
            {"rippleDir": [0, 1], "rippleMagnitude": 0.4, "rippleSpeed": 0.02, "rippleTexScale": [6, 6]},
            {"rippleDir": [0.7, -0.7], "rippleMagnitude": 0.8, "rippleSpeed": 0.02, "rippleTexScale": [3, 3]},
        ],
        "Waves (vertex undulation)": [
            {"waveDir": [0, 1], "waveMagnitude": 0.03, "waveSpeed": 1.0},
            {"waveDir": [0.707, 0.707], "waveMagnitude": 0.03, "waveSpeed": 1.0},
            {"waveDir": [0.5, 0.86], "waveMagnitude": 0.03, "waveSpeed": 1.0},
        ],
    }


def _rle_merge_wet_mask(
    wet: np.ndarray,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    cell: float,
    pad: float = 0.25,
) -> list[tuple[float, float, float, float]]:
    """Single-pass O(ny * nx) RLE horizontal scanline + vertical equal-span coalescing."""
    ny, nx = wet.shape
    active: dict[tuple[int, int], int] = {}
    rects: list[tuple[float, float, float, float]] = []
    padded = np.empty(nx + 2, dtype=bool)
    padded[0] = False
    padded[-1] = False

    for r in range(ny):
        padded[1:-1] = wet[r]
        diffs = np.diff(padded.view(np.int8))
        starts = np.flatnonzero(diffs == 1)
        ends = np.flatnonzero(diffs == -1)
        cur_runs = set(zip(starts.tolist(), ends.tolist(), strict=True))

        for span in list(active.keys()):
            if span not in cur_runs:
                r0 = active.pop(span)
                c0, c1 = span
                rx0 = min_x + c0 * cell - (pad if c0 > 0 else 0.0)
                rx1 = min(max_x, min_x + c1 * cell + (pad if c1 < nx else 0.0))
                ry0 = min_y + r0 * cell - (pad if r0 > 0 else 0.0)
                ry1 = min(max_y, min_y + r * cell + (pad if r < ny else 0.0))
                if rx1 > rx0 and ry1 > ry0:
                    rects.append((rx0, ry0, rx1, ry1))

        for span in cur_runs:
            if span not in active:
                active[span] = r

    for (c0, c1), r0 in active.items():
        rx0 = min_x + c0 * cell - (pad if c0 > 0 else 0.0)
        rx1 = min(max_x, min_x + c1 * cell + (pad if c1 < nx else 0.0))
        ry0 = min_y + r0 * cell - (pad if r0 > 0 else 0.0)
        ry1 = min(max_y, min_y + ny * cell)
        if rx1 > rx0 and ry1 > ry0:
            rects.append((rx0, ry0, rx1, ry1))
    return rects


def _extract_polygonal_bounds(geom: Any) -> list[tuple[float, float, float, float]]:
    """Return bounding boxes of polygonal components in `geom`."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom.bounds] if geom.area >= 16.0 else []
    if hasattr(geom, "geoms"):
        return [g.bounds for g in geom.geoms if g.geom_type == "Polygon" and not g.is_empty and g.area >= 16.0]
    return []


def _tile_water_body_bounds(
    body: WaterBody,
    *,
    poly_override: Polygon | None = None,
) -> list[tuple[float, float, float, float]]:
    """Decompose concave, holed, or curved water bodies into tight coalesced rectangles.

    Uses single-pass O(ny * nx) RLE + vertical span merging (choosing horizontal or
    transposed orientation with fewer tiles) on an adaptive grid, then trims tiles
    tightly against the shoreline polygon so WaterBlocks never spill onto dry land.
    """
    poly = poly_override
    if poly is None:
        min_x, min_y, max_x, max_y = body.bounds
        area_m2 = body.area_m2
        has_holes = bool(body.inner_rings)
    else:
        min_x, min_y, max_x, max_y = poly.bounds
        area_m2 = float(poly.area)
        has_holes = len(poly.interiors) > 0

    bounds = (min_x, min_y, max_x, max_y)
    sx = max_x - min_x
    sy = max_y - min_y
    bbox_area = sx * sy
    if bbox_area <= 0.0:
        return [bounds]
    fill_ratio = area_m2 / bbox_area
    if not has_holes and (fill_ratio >= 0.72 or max(sx, sy) <= 24.0):
        return [bounds]

    if poly is None:
        poly = Polygon(body.outer_ring, holes=[h for h in body.inner_rings if len(h) >= 4])
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return [bounds]

    cell = max(8.0, min(80.0, max(sx, sy) / 8.0))
    nx = max(1, math.ceil(sx / cell))
    ny = max(1, math.ceil(sy / cell))
    xs = min_x + (np.arange(nx, dtype=np.float64) + 0.5) * cell
    ys = min_y + (np.arange(ny, dtype=np.float64) + 0.5) * cell
    gx, gy = np.meshgrid(xs, ys)

    # Slightly generous hit buffer keeps narrow river reaches on coarse cells intact,
    # while per-tile clipping against `wet_poly` prevents spilling onto dry banks.
    hit_poly = poly.buffer(max(0.6, 0.25 * cell))
    wet = shapely.contains_xy(hit_poly, gx.ravel(), gy.ravel()).reshape(ny, nx)
    if not wet.any():
        return [bounds]

    rects_h = _rle_merge_wet_mask(wet, min_x, min_y, max_x, max_y, cell)
    rects_v_raw = _rle_merge_wet_mask(wet.T, min_y, min_x, max_y, max_x, cell)
    rects_v = [(x0, y0, x1, y1) for (y0, x0, y1, x1) in rects_v_raw]
    raw_rects = rects_h if len(rects_h) <= len(rects_v) else rects_v

    wet_poly = poly.buffer(0.5)
    trimmed: list[tuple[float, float, float, float]] = []
    for rx0, ry0, rx1, ry1 in raw_rects:
        clipped = shapely.clip_by_rect(wet_poly, rx0, ry0, rx1, ry1)
        parts = _extract_polygonal_bounds(clipped)
        if not parts:
            continue
        if len(parts) <= 3:
            trimmed.extend(parts)
        else:
            tx0 = min(p[0] for p in parts)
            ty0 = min(p[1] for p in parts)
            tx1 = max(p[2] for p in parts)
            ty1 = max(p[3] for p in parts)
            trimmed.append((tx0, ty0, tx1, ty1))
    return trimmed or [bounds]


def _clip_river_to_bounds(
    river: RiverChannel,
    clip_bounds: tuple[float, float, float, float],
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Clip a RiverChannel polyline to `clip_bounds`, preserving interpolated z_water."""
    bx0, by0, bx1, by1 = clip_bounds
    pts = river.points
    zw = river.z_water
    if (
        pts[:, 0].max() < bx0
        or pts[:, 0].min() > bx1
        or pts[:, 1].max() < by0
        or pts[:, 1].min() > by1
    ):
        return []
    if (
        pts[:, 0].min() >= bx0
        and pts[:, 0].max() <= bx1
        and pts[:, 1].min() >= by0
        and pts[:, 1].max() <= by1
    ):
        return [(pts, zw)]

    coords_3d = [(float(x), float(y), float(z)) for (x, y), z in zip(pts, zw, strict=True)]
    clipped = shapely.clip_by_rect(LineString(coords_3d), bx0, by0, bx1, by1)
    if clipped.is_empty:
        return []
    lines = [clipped] if clipped.geom_type == "LineString" else [
        g for g in getattr(clipped, "geoms", ()) if g.geom_type == "LineString"
    ]
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for line in lines:
        arr = np.asarray(line.coords, dtype=np.float64)
        if arr.shape[0] >= 2:
            out.append((arr[:, :2], arr[:, 2]))
    return out


def export_water_to_scene(
    tree: SceneTree,
    water_network: WaterNetwork,
    *,
    group_path: tuple[str, ...] = WATER_GROUP,
    depth_gradient_tex: str = DEFAULT_DEPTH_GRADIENT_TEX,
    foam_tex: str = DEFAULT_FOAM_TEX,
    ripple_tex: str = DEFAULT_RIPPLE_TEX,
    clip_bounds: tuple[float, float, float, float] | None = None,
) -> int:
    """Add all rivers and water bodies in `water_network` to the BeamNG `SceneTree`.

    Objects are added under `group_path` (default ``("level_objects", "water")``).

    Args:
        tree: BeamNG SceneTree to populate.
        water_network: Extracted hydrology network.
        group_path: SimGroup path below MissionGroup.
        depth_gradient_tex: Path to depth color ramp texture.
        foam_tex: Path to water foam texture.
        ripple_tex: Path to water ripple normal map.
        clip_bounds: Optional ``(min_x, min_y, max_x, max_y)`` terrain bounds to
            clip/cull out-of-bounds rivers and water bodies before WaterBlock tiling.

    Returns:
        Number of water scene objects added.
    """
    from dataclasses import replace

    count = 0
    tree.add_group(group_path)
    parent = group_path[-1] if group_path else ROOT_GROUP

    for river in water_network.rivers:
        if len(river.points) < 2 or len(river.z_water) < 2:
            continue
        if clip_bounds is None:
            river_parts = [river]
        else:
            clipped_segs = _clip_river_to_bounds(river, clip_bounds)
            river_parts = [
                replace(
                    river,
                    osm_id=(river.osm_id if i == 0 else river.osm_id * 1_000_000 + i),
                    points=pts_i,
                    z_water=zw_i,
                )
                for i, (pts_i, zw_i) in enumerate(clipped_segs)
            ]
        for r_part in river_parts:
            obj = build_river_object(
                tree.level_id,
                r_part,
                depth_gradient_tex=depth_gradient_tex,
                foam_tex=foam_tex,
                ripple_tex=ripple_tex,
                parent=parent,
            )
            tree.add_object(group_path, obj)
            count += 1

    for body in water_network.water_bodies:
        poly_overrides: list[Polygon | None] = [None]
        if clip_bounds is not None:
            bx0, by0, bx1, by1 = clip_bounds
            min_x, min_y, max_x, max_y = body.bounds
            if max_x < bx0 or min_x > bx1 or max_y < by0 or min_y > by1:
                continue
            if min_x < bx0 or max_x > bx1 or min_y < by0 or max_y > by1:
                poly = Polygon(body.outer_ring, holes=[h for h in body.inner_rings if len(h) >= 4])
                if not poly.is_valid:
                    poly = poly.buffer(0)
                clipped = shapely.clip_by_rect(poly, bx0, by0, bx1, by1)
                if clipped.is_empty:
                    continue
                if clipped.geom_type == "Polygon":
                    parts = [clipped] if clipped.area >= 25.0 else []
                else:
                    parts = [
                        g
                        for g in getattr(clipped, "geoms", ())
                        if g.geom_type == "Polygon" and not g.is_empty and g.area >= 25.0
                    ]
                if not parts:
                    continue
                poly_overrides = parts

        for part_idx, poly_ov in enumerate(poly_overrides):
            rects = _tile_water_body_bounds(body, poly_override=poly_ov)
            multi = len(rects) > 1 or len(poly_overrides) > 1
            for idx, rect in enumerate(rects):
                if len(poly_overrides) > 1:
                    suffix = f"_p{part_idx}_t{idx}" if len(rects) > 1 else f"_p{part_idx}"
                else:
                    suffix = f"_t{idx}" if len(rects) > 1 else ""
                obj = build_water_block_object(
                    tree.level_id,
                    body,
                    depth_gradient_tex=depth_gradient_tex,
                    foam_tex=foam_tex,
                    ripple_tex=ripple_tex,
                    parent=parent,
                    bounds_override=rect if multi else None,
                    name_suffix=suffix,
                )
                tree.add_object(group_path, obj)
                count += 1

    return count
