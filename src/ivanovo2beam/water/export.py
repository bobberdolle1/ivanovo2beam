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

from typing import TYPE_CHECKING, Any

from ivanovo2beam.beamng.level import stable_uuid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ivanovo2beam.beamng.level import SceneTree
    from ivanovo2beam.water.extract import RiverChannel, WaterBody, WaterNetwork

#: Standard shared BeamNG water texture paths
DEFAULT_DEPTH_GRADIENT_TEX = "/assets/materials/tileable/water/depthcolor_ramp/depthcolor_ramp_italy_rivers_b.png"
DEFAULT_FOAM_TEX = "/assets/materials/tileable/water/water_effects/foam2_b.color.dds"
DEFAULT_RIPPLE_TEX = "/assets/materials/tileable/water/water_effects/ripple3_nm.normal.dds"
WATER_GROUP = ("level_objects", "water")


def build_river_object(
    level_id: str,
    channel: RiverChannel,
    *,
    depth_gradient_tex: str = DEFAULT_DEPTH_GRADIENT_TEX,
    foam_tex: str = DEFAULT_FOAM_TEX,
    ripple_tex: str = DEFAULT_RIPPLE_TEX,
    parent: str = "water",
) -> dict[str, Any]:
    """Construct a BeamNG ``River`` scene object from a :class:`RiverChannel`."""
    nodes = []
    for (x, y), zw in zip(channel.points, channel.z_water, strict=True):
        nodes.append([
            round(float(x), 5),
            round(float(y), 5),
            round(float(zw), 5),
            round(float(channel.width_m), 2),
            round(float(channel.depth_m), 2),
            0.0,
            0.0,
            1.0,
        ])

    return {
        "name": f"river_{channel.osm_id}",
        "class": "River",
        "persistentId": stable_uuid(level_id, "River", str(channel.osm_id)),
        "__parent": parent,
        "position": nodes[0][:3],
        "nodes": nodes,
        "baseColor": [220, 235, 240, 255],
        "depthGradientMax": 5.0,
        "depthGradientTex": depth_gradient_tex,
        "distortEndDist": 1.0,
        "flowMagnitudePhysics": round(float(channel.flow_speed), 2),
        "foamAmbientLerp": 1.0,
        "foamMaxDepth": 0.75,
        "foamRippleInfluence": 0.05,
        "foamTex": foam_tex,
        "fresnelBias": 0.0,
        "fresnelPower": 3.0,
        "fullReflect": False,
        "lowLODDistance": 80.0,
        "rippleTex": ripple_tex,
        "Foam": [
            {"foamDir": [0, -1], "foamSpeed": 0.075, "foamTexScale": [3, 3]},
            {"foamDir": [0.4, 0], "foamSpeed": 0.075, "foamTexScale": [10, 10]},
        ],
        "Ripples (texture animation)": [
            {"rippleDir": [0, -1], "rippleMagnitude": 0.7, "rippleSpeed": -1.2, "rippleTexScale": [0.5, 0.5]},
            {"rippleDir": [0.5, 1], "rippleMagnitude": 0.5, "rippleSpeed": 0.07, "rippleTexScale": [7, 7]},
            {"rippleDir": [-0.5, 1], "rippleMagnitude": 0.4, "rippleSpeed": 0.017, "rippleTexScale": [30, 50]},
        ],
        "Waves (vertex undulation)": [
            {"waveDir": [0, 1], "waveMagnitude": 0.2, "waveSpeed": 3.0},
            {"waveDir": [0.707, 0.707], "waveMagnitude": 0.2, "waveSpeed": 1.5},
            {"waveDir": [0.5, 0.86], "waveMagnitude": 0.2, "waveSpeed": 1.5},
        ],
    }


def build_water_block_object(
    level_id: str,
    body: WaterBody,
    *,
    depth_gradient_tex: str = DEFAULT_DEPTH_GRADIENT_TEX,
    foam_tex: str = DEFAULT_FOAM_TEX,
    ripple_tex: str = DEFAULT_RIPPLE_TEX,
    parent: str = "water",
) -> dict[str, Any]:
    """Construct a BeamNG ``WaterBlock`` scene object from a :class:`WaterBody`."""
    min_x, min_y, max_x, max_y = body.bounds
    size_x = max(2.0, max_x - min_x)
    size_y = max(2.0, max_y - min_y)
    depth_z = max(1.0, float(body.depth_m))

    return {
        "name": f"waterblock_{body.osm_id}",
        "class": "WaterBlock",
        "persistentId": stable_uuid(level_id, "WaterBlock", str(body.osm_id)),
        "__parent": parent,
        "position": [round(min_x, 5), round(min_y, 5), round(body.z_water, 5)],
        "scale": [round(size_x, 5), round(size_y, 5), round(depth_z, 5)],
        "rotationMatrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        "baseColor": [189, 253, 255, 255],
        "depthGradientMax": 30.0,
        "depthGradientTex": depth_gradient_tex,
        "foamAmbientLerp": 1.3,
        "foamMaxDepth": 0.15,
        "foamRippleInfluence": 0.015,
        "foamTex": foam_tex,
        "fresnelBias": 0.2,
        "fresnelPower": 20.0,
        "fullReflect": False,
        "gridElementSize": 1.0,
        "gridSize": 1,
        "overallRippleMagnitude": 0.2,
        "overallWaveMagnitude": 0.1,
        "reflectivity": 0.8,
        "rippleTex": ripple_tex,
        "specularPower": 200.0,
        "waterFogDensity": 1.0,
        "waterFogDensityOffset": 0.1,
        "wetDarkening": 0.5,
        "wetDepth": 0.2,
        "Foam": [{}, {}],
        "Ripples (texture animation)": [
            {"rippleDir": [0, 1], "rippleMagnitude": 0.8, "rippleSpeed": 0.001, "rippleTexScale": [12, 12]},
            {"rippleDir": [0, 1], "rippleMagnitude": None, "rippleSpeed": 0.02, "rippleTexScale": [6, 6]},
            {"rippleDir": [0.7, -0.7], "rippleMagnitude": 1.0, "rippleSpeed": 0.02, "rippleTexScale": [3, 3]},
        ],
        "Waves (vertex undulation)": [
            {"waveDir": [0, 1], "waveMagnitude": 0.2, "waveSpeed": 1.0},
            {"waveDir": [0.707, 0.707], "waveMagnitude": 0.2, "waveSpeed": 1.0},
            {"waveDir": [0.5, 0.86], "waveMagnitude": 0.2, "waveSpeed": 1.0},
        ],
    }


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

    for river in water_network.rivers:
        obj = build_river_object(
            tree.level_id,
            river,
            depth_gradient_tex=depth_gradient_tex,
            foam_tex=foam_tex,
            ripple_tex=ripple_tex,
            parent=group_path[-1],
        )
        tree.add_object(group_path, obj)
        count += 1

    for body in water_network.water_bodies:
        obj = build_water_block_object(
            tree.level_id,
            body,
            depth_gradient_tex=depth_gradient_tex,
            foam_tex=foam_tex,
            ripple_tex=ripple_tex,
            parent=group_path[-1],
        )
        tree.add_object(group_path, obj)
        count += 1

    return count
