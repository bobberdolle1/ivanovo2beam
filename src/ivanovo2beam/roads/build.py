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
must not flatten the ground beneath them. They are still exported as decals so
they read as roads until real bridge meshes are generated (a later milestone).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

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
) -> dict[str, object] | None:
    """Build a DecalRoad scene object for one road.

    Args:
        road: The road to export.
        level_id: Level identifier for the deterministic UUID.
        ground_z: Optional elevation for each input vertex, in metres. When
            given, nodes follow the graded terrain surface, which is what makes
            the decal sit exactly on the road instead of floating.
        max_node_spacing_m: Maximum spacing between exported nodes.

    Returns:
        The scene object, or ``None`` when the road is too short to draw.
    """
    from ivanovo2beam.beamng.level import build_road_object

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

    # Half-width per node: BeamNG's node format is [x, y, z, width].
    half = road.half_width_m
    nodes = [[float(p[0]), float(p[1]), float(p[2]), half] for p in points]
    return build_road_object(
        level_id,
        key=f"way{road.osm_id}",
        nodes=nodes,
        material=road.decal_material,
        drivability=1.0 if road.drivable else 0.0,
        parent="road_network",
    )


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
    out: list[tuple[float, float, float]] = [
        (float(points[0, 0]), float(points[0, 1]), float(z[0]))
    ]
    for i, length in enumerate(lengths):
        ax, ay = float(points[i, 0]), float(points[i, 1])
        bx, by = float(points[i + 1, 0]), float(points[i + 1, 1])
        az, bz = float(z[i]), float(z[i + 1])
        if length <= max_spacing_m or length <= 0:
            out.append((bx, by, bz))
            continue
        steps = max(2, int(math.ceil(length / max_spacing_m)))
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
    for road in roads:
        try:
            ground_z = sample_surface_z(road, build)
        except Exception:  # pragma: no cover - defensive; shape mismatches only
            ground_z = None
        obj = road_to_decal_object(
            road, tree.level_id, ground_z=ground_z, max_node_spacing_m=max_node_spacing_m
        )
        if obj is None:
            skipped += 1
            continue
        tree.add_object(group_path, obj)
        exported += 1
        nodes += len(obj["nodes"])  # type: ignore[arg-type]
    return {"exported": exported, "skipped": skipped, "nodes": nodes}
