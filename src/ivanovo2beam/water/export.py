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
from shapely.geometry import Polygon

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


def _tile_water_body_bounds(body: WaterBody) -> list[tuple[float, float, float, float]]:
    """Decompose concave, holed, or curved water bodies into tight non-overlapping rectangles.

    Axis-aligned single ``WaterBlock`` boxes cover dry land (and islands) whenever
    a pond/oxbow is diagonal or curved. Rectangular/high-fill bodies stay a single block.
    """
    min_x, min_y, max_x, max_y = body.bounds
    sx = max_x - min_x
    sy = max_y - min_y
    bbox_area = sx * sy
    if bbox_area <= 0.0:
        return [body.bounds]
    fill_ratio = body.area_m2 / bbox_area
    if not body.inner_rings and (fill_ratio >= 0.72 or max(sx, sy) <= 16.0):
        return [body.bounds]

    poly = Polygon(body.outer_ring, holes=[h for h in body.inner_rings if len(h) >= 4])
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return [body.bounds]

    cell = max(2.5, min(10.0, max(sx, sy) / 32.0))
    nx = max(1, math.ceil(sx / cell))
    ny = max(1, math.ceil(sy / cell))
    xs = min_x + (np.arange(nx, dtype=np.float64) + 0.5) * cell
    ys = min_y + (np.arange(ny, dtype=np.float64) + 0.5) * cell
    gx, gy = np.meshgrid(xs, ys)
    # Slightly buffer curved ponds/oxbows so boundary cells fill up to the sloped
    # shoreline cleanly without leaving square staircase corners on bare sand.
    wet_poly = poly.buffer(0.6)
    wet = shapely.contains_xy(wet_poly, gx.ravel(), gy.ravel()).reshape(ny, nx)
    if not wet.any():
        return [body.bounds]

    # Dilate by 1 cell only where polygon boundary intersects? No — keep strictly
    # interior cells (+0.25m overlap between adjacent tiles) so water never floods
    # onto dry land outside concave ponds or islands.
    used = np.zeros((ny, nx), dtype=bool)
    rects: list[tuple[float, float, float, float]] = []
    pad = 0.25
    for r in range(ny):
        for c in range(nx):
            if not wet[r, c] or used[r, c]:
                continue
            c_end = c + 1
            while c_end < nx and wet[r, c_end] and not used[r, c_end]:
                c_end += 1
            r_end = r + 1
            while r_end < ny and bool(np.all(wet[r_end, c:c_end] & ~used[r_end, c:c_end])):
                r_end += 1
            used[r:r_end, c:c_end] = True
            rx0 = min_x + c * cell - (pad if c > 0 else 0.0)
            rx1 = min(max_x, min_x + c_end * cell + (pad if c_end < nx else 0.0))
            ry0 = min_y + r * cell - (pad if r > 0 else 0.0)
            ry1 = min(max_y, min_y + r_end * cell + (pad if r_end < ny else 0.0))
            if rx1 > rx0 and ry1 > ry0:
                rects.append((rx0, ry0, rx1, ry1))
    return rects or [body.bounds]


def export_water_to_scene(
    tree: SceneTree,
    water_network: WaterNetwork,
    *,
    group_path: tuple[str, ...] = WATER_GROUP,
    depth_gradient_tex: str = DEFAULT_DEPTH_GRADIENT_TEX,
    foam_tex: str = DEFAULT_FOAM_TEX,
    ripple_tex: str = DEFAULT_RIPPLE_TEX,
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

    Returns:
        Number of water scene objects added.
    """
    count = 0
    tree.add_group(group_path)
    parent = group_path[-1] if group_path else ROOT_GROUP

    for river in water_network.rivers:
        if len(river.points) < 2 or len(river.z_water) < 2:
            continue
        obj = build_river_object(
            tree.level_id,
            river,
            depth_gradient_tex=depth_gradient_tex,
            foam_tex=foam_tex,
            ripple_tex=ripple_tex,
            parent=parent,
        )
        tree.add_object(group_path, obj)
        count += 1

    for body in water_network.water_bodies:
        rects = _tile_water_body_bounds(body)
        for idx, rect in enumerate(rects):
            suffix = f"_t{idx}" if len(rects) > 1 else ""
            obj = build_water_block_object(
                tree.level_id,
                body,
                depth_gradient_tex=depth_gradient_tex,
                foam_tex=foam_tex,
                ripple_tex=ripple_tex,
                parent=parent,
                bounds_override=rect if len(rects) > 1 else None,
                name_suffix=suffix,
            )
            tree.add_object(group_path, obj)
            count += 1

    return count
