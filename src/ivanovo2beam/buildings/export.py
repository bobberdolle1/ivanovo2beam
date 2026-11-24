"""Building generation: OSM footprints in, BeamNG meshes and objects out.

Design note — why archetypes rather than one mesh per building
------------------------------------------------------------
A dense 5 x 5 km city sector contains on the order of ten thousand buildings.
Generating a unique mesh for each would put ten thousand COLLADA files in the
level archive and, worse, ten thousand distinct meshes in VRAM: BeamNG would
have no opportunity to share geometry between similar houses, and load times
would suffer badly.

So the generator emits a small library of **archetypes** — a handful of
prototype meshes per building class, derived from real footprints in the data —
and places each building as an instance of the archetype closest to it in area,
scaled to its own footprint. Instances carry ``useInstanceRenderData`` so the
engine can batch them.

Buildings above :data:`UNIQUE_AREA_M2` are the exception. Those are the
landmarks, the places where the plan shape is what makes the map recognisable,
so they get an exact extrusion of their own footprint. That hybrid is the answer
to "prefab, extrusion, or both": extrusion where the shape carries meaning,
instancing where it does not.
"""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from ivanovo2beam.beamng.level import stable_uuid
from ivanovo2beam.buildings.footprints import (
    BUILDING_CLASSES,
    BuildingFootprint,
    building_class_for,
)
from ivanovo2beam.buildings.mesh import extrude_footprint, roof_height_for, write_dae

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from pathlib import Path

    from numpy.typing import NDArray

#: Footprint area above which a building gets its own exact mesh rather than an
#: archetype instance, in square metres. Roughly the size of a small apartment
#: block: below this the plan shape is not what makes a place recognisable.
UNIQUE_AREA_M2 = 1500.0

#: Number of archetype variants generated per building class.
VARIANTS_PER_CLASS = 4


@dataclass(frozen=True, slots=True)
class Archetype:
    """A shared building mesh that instances are placed from.

    Attributes:
        key: Identifier, also the mesh name.
        klass: Building class this archetype belongs to.
        dae_path: In-game asset path of the generated COLLADA file.
        material_name: COLLADA material id.
        colour: Diffuse RGB.
        width_m: Prototype footprint bounding-box width, for instance scaling.
        depth_m: Prototype footprint bounding-box depth.
        height_m: Prototype total height including the roof.
        area_m2: Prototype footprint area, used to match instances.
        triangle_count: Triangles in the mesh.
    """

    key: str
    klass: str
    dae_path: str
    material_name: str
    colour: tuple[float, float, float]
    width_m: float
    depth_m: float
    height_m: float
    area_m2: float
    triangle_count: int


@dataclass(slots=True)
class BuildingExportStats:
    """Diagnostics from building export."""

    buildings: int = 0
    instances: int = 0
    unique_meshes: int = 0
    archetypes: int = 0
    triangles: int = 0
    bytes_written: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    placements_skipped: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary."""
        return {
            "buildings": self.buildings,
            "instances": self.instances,
            "unique_meshes": self.unique_meshes,
            "archetypes": self.archetypes,
            "triangles": self.triangles,
            "mesh_bytes": self.bytes_written,
            "by_class": dict(sorted(self.by_class.items(), key=lambda kv: -kv[1])),
            "placements_skipped": self.placements_skipped,
        }


def _bbox(ring: NDArray[np.float64]) -> tuple[float, float, float, float]:
    """Return ``(width, depth, centre_x, centre_y)`` of a ring.

    The centre is the bounding-box midpoint rather than the vertex mean: a closed
    ring repeats its first vertex, which biases a mean towards that corner and
    would place every building slightly off its own footprint.
    """
    xs = ring[:, 0]
    ys = ring[:, 1]
    return (
        float(xs.max() - xs.min()),
        float(ys.max() - ys.min()),
        float((xs.min() + xs.max()) / 2.0),
        float((ys.min() + ys.max()) / 2.0),
    )


def choose_prototypes(
    buildings: Sequence[BuildingFootprint],
    *,
    variants_per_class: int = VARIANTS_PER_CLASS,
) -> dict[str, BuildingFootprint]:
    """Pick representative footprints to build archetype meshes from.

    Buildings of each class are sorted by area and split into
    ``variants_per_class`` buckets; the median building of each bucket becomes a
    prototype. Bucketing by area means the archetypes span the real size range
    of that class instead of all being the average.

    Args:
        buildings: All extracted footprints.
        variants_per_class: Archetypes to derive per building class.

    Returns:
        Mapping of archetype key (``"house_0"``, ``"house_1"``, ...) to the
        footprint it is generated from.
    """
    by_class: dict[str, list[BuildingFootprint]] = {}
    for building in buildings:
        by_class.setdefault(building.klass, []).append(building)

    prototypes: dict[str, BuildingFootprint] = {}
    for klass, members in sorted(by_class.items()):
        members = sorted(members, key=lambda b: (b.area_m2, b.osm_id))
        count = max(1, min(variants_per_class, len(members)))
        seen_areas: set[float] = set()
        variant_idx = 0
        for index in range(count):
            lo = round(index * len(members) / count)
            hi = max(lo + 1, round((index + 1) * len(members) / count))
            bucket = members[lo:hi]
            candidate = bucket[len(bucket) // 2]
            area_key = round(candidate.area_m2, 3)
            if area_key in seen_areas:
                continue
            seen_areas.add(area_key)
            prototypes[f"{klass}_{variant_idx}"] = candidate
            variant_idx += 1
    return prototypes


def build_archetypes(
    prototypes: dict[str, BuildingFootprint],
    *,
    level_id: str,
    shapes_dir: Path,
    asset_prefix: str,
) -> dict[str, Archetype]:
    """Generate the archetype meshes and write them to disk.

    Args:
        prototypes: Output of :func:`choose_prototypes`.
        level_id: Level identifier, used for deterministic object ids.
        shapes_dir: Directory to write ``.dae`` files into.
        asset_prefix: In-game path prefix, e.g. ``/levels/<id>/art/shapes``.

    Returns:
        Mapping of archetype key to :class:`Archetype`.
    """
    archetypes: dict[str, Archetype] = {}
    for key, prototype in sorted(prototypes.items()):
        klass = building_class_for(prototype.klass)
        roof_height = roof_height_for(prototype.ring, klass.roof)
        mesh = extrude_footprint(
            prototype.ring,
            height_m=prototype.height_m,
            roof=klass.roof,
            roof_height_m=roof_height,
        )
        material_name = f"{level_id}_building_{key}"
        path = shapes_dir / f"{key}.dae"
        # The byte count is totalled from the written files after the loop, so
        # the return value is not needed here.
        write_dae(
            path,
            mesh,
            name=f"{level_id}_bld_{key}",
            material_name=material_name,
            colour=klass.colour,
        )
        width, depth, _, _ = _bbox(prototype.ring)
        archetypes[key] = Archetype(
            key=key,
            klass=prototype.klass,
            dae_path=f"{asset_prefix}/{key}.dae",
            material_name=material_name,
            colour=klass.colour,
            width_m=max(width, 0.5),
            depth_m=max(depth, 0.5),
            height_m=prototype.height_m + roof_height,
            area_m2=prototype.area_m2,
            triangle_count=mesh.triangle_count,
        )
    return archetypes


def _sample_ground(build: Any, x: float, y: float) -> float:
    """Return the built terrain elevation at a world point."""
    elevations = build.elevations
    origin_x, origin_y = build.origin_x, build.origin_y
    cell = build.cell_size_m
    n = elevations.shape[0]
    col = int(np.clip(round((x - origin_x) / cell), 0, n - 1))
    row = int(np.clip(round((y - origin_y) / cell), 0, n - 1))
    return float(elevations[row, col])


def export_buildings(
    buildings: Sequence[BuildingFootprint],
    build: Any,
    *,
    level_id: str,
    level_dir: Path,
    variants_per_class: int = VARIANTS_PER_CLASS,
    unique_area_m2: float = UNIQUE_AREA_M2,
    max_instances: int | None = None,
) -> tuple[list[dict[str, Any]], BuildingExportStats]:
    """Turn footprints into BeamNG ``TSStatic`` objects and their meshes.

    Args:
        buildings: Footprints to export.
        build: The built terrain, used to seat each building on the ground.
        level_id: Level identifier.
        level_dir: Built level directory; meshes go under ``art/shapes/buildings``.
        variants_per_class: Archetypes to generate per class.
        unique_area_m2: Footprints at least this large get their own mesh.
        max_instances: Optional cap on placed buildings, largest first.

    Returns:
        ``(objects, stats)`` where each object is ready for
        :meth:`ivanovo2beam.beamng.level.SceneTree.add_object`.
    """
    stats = BuildingExportStats()
    shapes_dir = level_dir / "art" / "shapes" / "buildings"
    if shapes_dir.exists():
        shutil.rmtree(shapes_dir)
    if not buildings:
        return ([], stats)

    selected = list(buildings)
    if max_instances is not None and len(selected) > max_instances:
        selected.sort(key=lambda b: (-b.area_m2, b.osm_id))
        selected = selected[: max(0, max_instances)]

    asset_prefix = f"/levels/{level_id}/art/shapes/buildings"

    large = [b for b in selected if b.area_m2 >= unique_area_m2]
    small = [b for b in selected if b.area_m2 < unique_area_m2]

    raw_prototypes = choose_prototypes(small, variants_per_class=variants_per_class)
    proto_by_class: dict[str, list[tuple[str, BuildingFootprint]]] = {}
    for key, proto in sorted(raw_prototypes.items()):
        proto_by_class.setdefault(proto.klass, []).append((key, proto))

    chosen_keys: dict[int, str] = {}
    used_prototypes: dict[str, BuildingFootprint] = {}
    for idx, footprint in enumerate(small):
        proto_candidates = proto_by_class.get(footprint.klass)
        if not proto_candidates:
            continue
        best_key, best_proto = min(
            proto_candidates,
            key=lambda item: abs(math.log(max(item[1].area_m2, 1.0) / max(footprint.area_m2, 1.0))),
        )
        chosen_keys[idx] = best_key
        used_prototypes[best_key] = best_proto

    archetypes = build_archetypes(
        used_prototypes,
        level_id=level_id,
        shapes_dir=shapes_dir,
        asset_prefix=asset_prefix,
    )
    stats.archetypes = len(archetypes)
    stats.bytes_written += sum(
        (shapes_dir / f"{key}.dae").stat().st_size
        for key in archetypes
        if (shapes_dir / f"{key}.dae").is_file()
    )
    stats.triangles += sum(a.triangle_count for a in archetypes.values())

    objects: list[dict[str, Any]] = []

    def place(
        footprint: BuildingFootprint,
        shape_name: str,
        *,
        scale: tuple[float, float, float],
        roof: str,
    ) -> None:
        width, depth, cx, cy = _bbox(footprint.ring)
        ground = _sample_ground(build, cx, cy)
        objects.append(
            {
                "class": "TSStatic",
                "persistentId": stable_uuid(level_id, "building", f"way{footprint.osm_id}"),
                "__parent": "buildings",
                "position": [round(cx, 4), round(cy, 4), round(ground, 4)],
                "shapeName": shape_name,
                "scale": [round(scale[0], 5), round(scale[1], 5), round(scale[2], 5)],
                "collisionType": "Visible Mesh Final",
                "decalType": "None",
                "useInstanceRenderData": True,
                "annotation": f"{footprint.klass}:{roof}:{footprint.height_source}:{width:.1f}x{depth:.1f}",
            }
        )

    # -- shared archetype instances ----------------------------------------
    for idx, footprint in enumerate(small):
        chosen_key = chosen_keys.get(idx)
        archetype = archetypes.get(chosen_key) if chosen_key is not None else None
        if archetype is None:
            stats.placements_skipped += 1
            continue
        width, depth, _, _ = _bbox(footprint.ring)
        klass = building_class_for(footprint.klass)
        roof_height = roof_height_for(footprint.ring, klass.roof)
        place(
            footprint,
            archetype.dae_path,
            scale=(
                width / archetype.width_m,
                depth / archetype.depth_m,
                (footprint.height_m + roof_height) / archetype.height_m,
            ),
            roof=klass.roof,
        )
        stats.instances += 1

    # -- exact meshes for notable buildings --------------------------------
    for footprint in large:
        klass = building_class_for(footprint.klass)
        roof_height = roof_height_for(footprint.ring, klass.roof)
        try:
            mesh = extrude_footprint(
                footprint.ring,
                height_m=footprint.height_m,
                roof=klass.roof,
                roof_height_m=roof_height,
            )
        except ValueError:
            stats.placements_skipped += 1
            continue
        name = f"b{footprint.osm_id}"
        written = write_dae(
            shapes_dir / f"{name}.dae",
            mesh,
            name=f"{level_id}_{name}",
            material_name=f"{level_id}_building_{name}",
            colour=klass.colour,
        )
        stats.bytes_written += written
        stats.unique_meshes += 1
        stats.triangles += mesh.triangle_count
        # The mesh already matches the footprint, so no scaling is applied.
        place(footprint, f"{asset_prefix}/{name}.dae", scale=(1.0, 1.0, 1.0), roof=klass.roof)

    for footprint in selected:
        stats.by_class[footprint.klass] = stats.by_class.get(footprint.klass, 0) + 1
    stats.buildings = len(selected)
    return (objects, stats)


def archetype_summary(archetypes: dict[str, Archetype]) -> str:
    """Return a one-line summary of an archetype library."""
    triangles = sum(a.triangle_count for a in archetypes.values())
    return f"{len(archetypes)} archetypes, {triangles} triangles total"


#: Roof form per class, re-exported for callers that only need the mapping.
ROOF_BY_CLASS: dict[str, str] = {key: spec.roof for key, spec in BUILDING_CLASSES.items()}
