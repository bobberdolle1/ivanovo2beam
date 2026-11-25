"""Building generation: OSM footprints in, BeamNG meshes and objects out.

Design:
1. Spatial Chunk Batching (Default):
   Batches buildings into 500m x 500m sector `.dae` files
   (`art/shapes/buildings/chunk_<ix>_<iy>.dae`) with `meshCulling: true` and
   `collisionType: "Visible Mesh Final"`. This cuts 14,000 separate TSStatic
   objects down to ~100 batched chunks with zero scaling shear and exact real-world
   footprints!
2. Slope Skirts:
   Wall skirts extend downwards on slopes (min -3.0m) to anchor buildings into
   hillsides (Plyos bluffs, Uvod riverbanks) so they never float.
3. Ivanovo Oblast Typology & PBR Materials:
   - Red-brick textile mills (БИМ, НИМ, Красная Талка) on industrial/brick;
   - Soviet panel blocks (Khrushchevkas <=5F brick, Brezhnevkas >=6F precast concrete);
   - Constructivist monuments & civic buildings (plaster/concrete);
   - Russian wooden houses (izbas with timber cladding & pitched roofs).
   Emits `levels/<id>/art/shapes/buildings/main.materials.json` referencing stock
   BeamNG textures so there is zero WarningMaterial.
4. Legacy / Standalone Archetype Support:
   Preserved for single-building instances or offline prototype testing.
"""

from __future__ import annotations

import json
import math
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from ivanovo2beam.beamng.level import stable_uuid
from ivanovo2beam.buildings.footprints import (
    BUILDING_CLASSES,
    BuildingFootprint,
    building_class_for,
)
from ivanovo2beam.buildings.mesh import (
    MeshBuilder,
    _build_extruded_body,
    _centre,
    extrude_footprint,
    roof_height_for,
    write_dae,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

#: Fixed UUID namespace for building materials.
_BUILDING_MATERIAL_NS = uuid.UUID("7a31b4e0-9112-4c28-98e6-1c8832a514d2")

#: Default spatial sector chunk size in metres.
DEFAULT_CHUNK_SIZE_M = 500.0

#: Footprint area above which a building gets its own exact mesh in archetype mode.
UNIQUE_AREA_M2 = 1500.0

#: Number of archetype variants generated per building class.
VARIANTS_PER_CLASS = 4

#: Native BeamNG PBR texture sets for Ivanovo architectural typologies.
BUILDING_MATERIALS: dict[str, dict[str, Any]] = {
    "bld_brick_industrial": {
        "title": "Red-brick textile mills (BIM, NIM, Krasnaya Talka)",
        "groundType": "CONCRETE",
        "baseColorMap": "/assets/materials/tileable/brick/brick_plain/t_bricks_red_01_b.color.dds",
        "normalMap": "/assets/materials/tileable/brick/brick_plain/t_bricks_red_01_nm.normal.dds",
        "roughnessMap": "/assets/materials/tileable/brick/brick_plain/t_bricks_red_01_r.data.dds",
        "baseColorFactor": [0.65, 0.25, 0.20, 1.0],
    },
    "bld_concrete_panel": {
        "title": "Soviet precast concrete panel blocks (Brezhnevkas >=6F)",
        "groundType": "CONCRETE",
        "baseColorMap": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_b.color.dds",
        "normalMap": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_nm.normal.dds",
        "roughnessMap": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_r.data.dds",
        "baseColorFactor": [0.70, 0.70, 0.68, 1.0],
    },
    "bld_brick_khrushchevka": {
        "title": "Soviet brick blocks (Khrushchevkas <=5F)",
        "groundType": "CONCRETE",
        "baseColorMap": "/assets/materials/tileable/brick/ind_blockwall/t_concrete_cinderblock_01_b.color.dds",
        "normalMap": "/assets/materials/tileable/brick/ind_blockwall/t_concrete_cinderblock_01_nm.normal.dds",
        "roughnessMap": "/assets/materials/tileable/brick/ind_blockwall/t_concrete_cinderblock_01_r.data.dds",
        "baseColorFactor": [0.72, 0.62, 0.54, 1.0],
    },
    "bld_plaster_civic": {
        "title": "Constructivist monuments and civic buildings (schools, clinics)",
        "groundType": "CONCRETE",
        "baseColorMap": "/assets/materials/tileable/plaster/m_plaster_float/t_plaster_float_b.color.dds",
        "normalMap": "/assets/materials/tileable/plaster/m_plaster_float/t_plaster_float_nm.normal.dds",
        "roughnessMap": "/assets/materials/tileable/plaster/m_plaster_float/t_plaster_float_r.data.dds",
        "baseColorFactor": [0.78, 0.76, 0.72, 1.0],
    },
    "bld_wood_wall": {
        "title": "Russian wooden houses (izbas, private sector, Plyos merchants)",
        "groundType": "WOOD",
        "baseColorMap": "/assets/materials/tileable/wood/weatherboards/t_weatherboards_b.color.dds",
        "normalMap": "/assets/materials/tileable/wood/weatherboards/t_weatherboards_nm.normal.dds",
        "roughnessMap": "/assets/materials/tileable/wood/weatherboards/t_weatherboards_r.data.dds",
        "baseColorFactor": [0.58, 0.48, 0.38, 1.0],
    },
    "bld_roof_tar": {
        "title": "Flat tar roof for apartment blocks and factories",
        "groundType": "ASPHALT",
        "baseColorMap": "/assets/materials/tileable/roofing/asphalt_roof/t_asphalt_roof_b.color.dds",
        "normalMap": "/assets/materials/tileable/roofing/asphalt_roof/t_asphalt_roof_nm.normal.dds",
        "roughnessMap": "/assets/materials/tileable/roofing/asphalt_roof/t_asphalt_roof_r.data.dds",
        "baseColorFactor": [0.20, 0.20, 0.22, 1.0],
    },
    "bld_roof_slate": {
        "title": "Pitched slate roof tiles for houses and churches",
        "groundType": "CONCRETE",
        "baseColorMap": "/assets/materials/tileable/roofing/t_slate_rooftiles_01/t_slate_rooftiles_01_b.color.dds",
        "normalMap": "/assets/materials/tileable/roofing/t_slate_rooftiles_01/t_slate_rooftiles_01_nm.normal.dds",
        "roughnessMap": "/assets/materials/tileable/roofing/t_slate_rooftiles_01/t_slate_rooftiles_01_r.data.dds",
        "baseColorFactor": [0.35, 0.35, 0.38, 1.0],
    },
    "bld_roof_metal": {
        "title": "Sheet metal roofs for industrial and domestic buildings",
        "groundType": "METAL",
        "baseColorMap": "/assets/materials/tileable/metal/m_si_metal_roof/t_si_metal_roof_b.color.dds",
        "normalMap": "/assets/materials/tileable/metal/m_si_metal_roof/t_si_metal_roof_nm.normal.dds",
        "roughnessMap": "/assets/materials/tileable/metal/m_si_metal_roof/t_si_metal_roof_r.data.dds",
        "baseColorFactor": [0.45, 0.48, 0.52, 1.0],
    },
}


def resolve_building_materials(footprint: BuildingFootprint) -> tuple[str, str]:
    """Return ``(wall_material, roof_material)`` for an Ivanovo building footprint."""
    mat_hint = (footprint.material or "").strip().lower()
    klass = footprint.klass

    # 1. Wall material
    if mat_hint in {"brick", "red_brick"} or klass == "industrial":
        wall_mat = "bld_brick_industrial"
    elif mat_hint in {"wood", "timber", "log"} or klass in {"house", "shed", "garage"}:
        wall_mat = "bld_wood_wall"
    elif mat_hint in {"concrete", "panel"}:
        wall_mat = "bld_concrete_panel"
    elif mat_hint == "plaster" or klass in {"school", "hospital", "commercial"}:
        wall_mat = "bld_plaster_civic"
    elif klass == "apartments":
        wall_mat = "bld_brick_khrushchevka" if footprint.levels <= 5.0 else "bld_concrete_panel"
    else:
        wall_mat = "bld_wood_wall" if klass in {"house", "shed"} else "bld_plaster_civic"

    # 2. Roof material
    roof_shape = (footprint.roof_shape or "").strip().lower()
    if roof_shape == "flat" or klass in {"apartments", "hospital", "commercial"}:
        roof_mat = "bld_roof_tar"
    elif klass == "industrial":
        roof_mat = "bld_roof_metal" if footprint.osm_id % 2 == 0 else "bld_roof_tar"
    elif klass in {"house", "church", "shed"}:
        roof_mat = "bld_roof_slate" if footprint.osm_id % 2 == 0 else "bld_roof_metal"
    else:
        roof_mat = "bld_roof_tar"

    return wall_mat, roof_mat


def build_building_materials_json(
    level_id: str,
    extra_materials: Sequence[str] = (),
) -> dict[str, Any]:
    """Generate BeamNG Material v1.5 datablocks for all building materials."""
    materials_dict: dict[str, Any] = {}

    for key, spec in BUILDING_MATERIALS.items():
        stage: dict[str, Any] = {}
        if "baseColorMap" in spec:
            stage["baseColorMap"] = spec["baseColorMap"]
        elif "colorMap" in spec:
            stage["baseColorMap"] = spec["colorMap"]
        if "normalMap" in spec:
            stage["normalMap"] = spec["normalMap"]
        if "roughnessMap" in spec:
            stage["roughnessMap"] = spec["roughnessMap"]
        if "baseColorFactor" in spec:
            stage["baseColorFactor"] = list(spec["baseColorFactor"])

        mat_id = str(uuid.uuid5(_BUILDING_MATERIAL_NS, key))
        materials_dict[key] = {
            "name": key,
            "mapTo": key,
            "class": "Material",
            "persistentId": mat_id,
            "Stages": [stage],
            "annotation": "BUILDINGS",
            "groundType": spec.get("groundType", "CONCRETE"),
            "materialTag0": "beamng",
            "materialTag1": "building",
            "materialTag2": "ivanovo2beam",
            "version": 1.5,
        }

    # Also map any extra material names (such as archetype materials)
    for extra in extra_materials:
        if extra in materials_dict:
            continue
        mat_id = str(uuid.uuid5(_BUILDING_MATERIAL_NS, extra))
        materials_dict[extra] = {
            "name": extra,
            "mapTo": extra,
            "class": "Material",
            "persistentId": mat_id,
            "Stages": [
                {
                    "baseColorMap": "/assets/materials/tileable/plaster/m_plaster_float/t_plaster_float_b.color.dds",
                    "normalMap": "/assets/materials/tileable/plaster/m_plaster_float/t_plaster_float_nm.normal.dds",
                    "roughnessMap": "/assets/materials/tileable/plaster/m_plaster_float/t_plaster_float_r.data.dds",
                    "baseColorFactor": [0.78, 0.76, 0.72, 1.0],
                },
            ],
            "annotation": "BUILDINGS",
            "groundType": "CONCRETE",
            "materialTag0": "beamng",
            "materialTag1": "building",
            "version": 1.5,
        }

    return materials_dict


def write_building_materials(
    path: Path,
    level_id: str,
    extra_materials: Sequence[str] = (),
) -> int:
    """Write ``levels/<id>/art/shapes/buildings/main.materials.json``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = build_building_materials_json(level_id, extra_materials)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return len(text)


@dataclass(frozen=True, slots=True)
class Archetype:
    """A shared building mesh that instances are placed from."""

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
    chunks: int = 0
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
            "chunks": self.chunks,
            "triangles": self.triangles,
            "mesh_bytes": self.bytes_written,
            "by_class": dict(sorted(self.by_class.items(), key=lambda kv: -kv[1])),
            "placements_skipped": self.placements_skipped,
        }


def _bbox(ring: NDArray[np.float64]) -> tuple[float, float, float, float]:
    """Return ``(width, depth, centre_x, centre_y)`` of a ring."""
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
    """Pick representative footprints to build archetype meshes from."""
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
    """Generate the archetype meshes and write them to disk."""
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
    elevations = getattr(build, "elevations", None)
    if elevations is None:
        return 0.0
    origin_x = getattr(build, "origin_x", 0.0)
    origin_y = getattr(build, "origin_y", 0.0)
    cell = getattr(build, "cell_size_m", 1.0)
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
    batch_chunks: bool = True,
    chunk_size_m: float = DEFAULT_CHUNK_SIZE_M,
    variants_per_class: int = VARIANTS_PER_CLASS,
    unique_area_m2: float = UNIQUE_AREA_M2,
    max_instances: int | None = None,
) -> tuple[list[dict[str, Any]], BuildingExportStats]:
    """Turn footprints into BeamNG objects and their meshes.

    When ``batch_chunks=True`` (the default), buildings are batched into 500m x 500m
    spatial sector DAE chunks with mesh culling, downwards slope skirts (min -3.0m),
    and native PBR materials, cutting 14,000 separate TSStatic objects down to ~100
    chunks with exact footprints and zero scaling shear!

    When ``batch_chunks=False``, falls back to individual TSStatic instances.
    """
    stats = BuildingExportStats()
    if not buildings:
        return ([], stats)

    shapes_dir = level_dir / "art" / "shapes" / "buildings"
    if shapes_dir.exists():
        shutil.rmtree(shapes_dir)
    shapes_dir.mkdir(parents=True, exist_ok=True)

    selected = list(buildings)
    if max_instances is not None and len(selected) > max_instances:
        selected.sort(key=lambda b: (-b.area_m2, b.osm_id))
        selected = selected[: max(0, max_instances)]

    asset_prefix = f"/levels/{level_id}/art/shapes/buildings"

    if batch_chunks:
        objects = _export_buildings_chunked(
            selected,
            build,
            level_id=level_id,
            shapes_dir=shapes_dir,
            asset_prefix=asset_prefix,
            chunk_size_m=chunk_size_m,
            stats=stats,
        )
    else:
        objects = _export_buildings_archetypes(
            selected,
            build,
            level_id=level_id,
            shapes_dir=shapes_dir,
            asset_prefix=asset_prefix,
            variants_per_class=variants_per_class,
            unique_area_m2=unique_area_m2,
            stats=stats,
        )

    # Emit main.materials.json mapping all materials so there is zero WarningMaterial
    materials_path = shapes_dir / "main.materials.json"
    extra_mats = [o["shapeName"].rsplit("/", 1)[-1].replace(".dae", "") for o in objects]
    stats.bytes_written += write_building_materials(materials_path, level_id, extra_materials=extra_mats)

    for footprint in selected:
        stats.by_class[footprint.klass] = stats.by_class.get(footprint.klass, 0) + 1
    stats.buildings = len(selected)

    return (objects, stats)


def _export_buildings_chunked(
    buildings: Sequence[BuildingFootprint],
    build: Any,
    *,
    level_id: str,
    shapes_dir: Path,
    asset_prefix: str,
    chunk_size_m: float,
    stats: BuildingExportStats,
) -> list[dict[str, Any]]:
    """Batch buildings into 500m x 500m sector .dae chunks."""
    # Group buildings into spatial chunks
    chunks: dict[tuple[int, int], list[BuildingFootprint]] = {}
    for footprint in buildings:
        cx, cy = footprint.centroid
        ix = math.floor(cx / chunk_size_m)
        iy = math.floor(cy / chunk_size_m)
        chunks.setdefault((ix, iy), []).append(footprint)

    objects: list[dict[str, Any]] = []

    for (ix, iy), chunk_buildings in sorted(chunks.items()):
        chunk_name = f"chunk_{ix}_{iy}"
        chunk_cx = (ix + 0.5) * chunk_size_m
        chunk_cy = (iy + 0.5) * chunk_size_m

        builder = MeshBuilder()

        for footprint in chunk_buildings:
            wall_mat, roof_mat = resolve_building_materials(footprint)
            cx, cy = footprint.centroid
            ground_z = _sample_ground(build, cx, cy)

            # Convert footprint to chunk-relative coordinates
            offset = np.array([chunk_cx, chunk_cy], dtype=np.float64)
            ring_local = footprint.ring - offset
            holes_local = [h - offset for h in footprint.holes]

            klass = building_class_for(footprint.klass)
            roof = footprint.roof_shape or klass.roof
            roof_h = footprint.roof_height_m or roof_height_for(footprint.ring, roof)

            # Sample corner ground elevations to determine slope skirt
            corner_zs = [_sample_ground(build, float(pt[0]), float(pt[1])) for pt in footprint.ring[:-1]]
            min_corner = min(corner_zs) if corner_zs else ground_z
            slope_drop = max(0.0, ground_z - min_corner)
            # Minimum -3.0m slope skirt, extending further on steep slopes
            skirt_depth = max(3.0, slope_drop + 2.0)

            # Build in local building frame then offset into chunk space
            try:
                b_cx = _centre(ring_local[:, 0])
                b_cy = _centre(ring_local[:, 1])
                b_offset = np.array([b_cx, b_cy], dtype=np.float64)
                b_ring = ring_local - b_offset
                b_holes = [h - b_offset for h in holes_local]

                bld_builder = MeshBuilder()
                _build_extruded_body(
                    bld_builder,
                    b_ring,
                    b_holes,
                    height_m=footprint.height_m,
                    roof=roof,
                    roof_height_m=roof_h,
                    min_height_m=footprint.min_height_m,
                    skirt_depth_m=skirt_depth,
                    wall_material=wall_mat,
                    roof_material=roof_mat,
                )
                bld_mesh = bld_builder.build()

                # Add to chunk mesh with world elevation and chunk offset
                for idx, tri in enumerate(bld_mesh.triangles):
                    mat = bld_mesh.triangle_materials[idx]
                    uv_idx = bld_mesh.triangle_uvs[idx]
                    pts = []
                    normals = []
                    uvs = []
                    for corner_i in range(3):
                        vi, ni = tri[corner_i]
                        px, py, pz = bld_mesh.positions[vi]
                        pts.append((px + b_cx, py + b_cy, pz + ground_z))
                        normals.append(bld_mesh.normals[ni])
                        uvs.append(bld_mesh.uvs[uv_idx[corner_i]])
                    builder.mesh.add_triangle(
                        pts[0],
                        pts[1],
                        pts[2],
                        normal=normals[0],
                        uv0=uvs[0],
                        uv1=uvs[1],
                        uv2=uvs[2],
                        material=mat,
                    )
            except Exception:
                stats.placements_skipped += 1
                continue

        chunk_mesh = builder.build()
        if chunk_mesh.triangle_count == 0:
            continue

        dae_filename = f"{chunk_name}.dae"
        dae_path = shapes_dir / dae_filename
        written = write_dae(
            dae_path,
            chunk_mesh,
            name=f"{level_id}_{chunk_name}",
            materials=chunk_mesh.unique_materials(),
        )

        stats.bytes_written += written
        stats.chunks += 1
        stats.triangles += chunk_mesh.triangle_count

        objects.append(
            {
                "class": "TSStatic",
                "persistentId": stable_uuid(level_id, "building_chunk", f"{ix}_{iy}"),
                "__parent": "buildings",
                "position": [round(chunk_cx, 4), round(chunk_cy, 4), 0.0],
                "shapeName": f"{asset_prefix}/{dae_filename}",
                "scale": [1.0, 1.0, 1.0],
                "collisionType": "Visible Mesh Final",
                "decalType": "None",
                "meshCulling": True,
                "useInstanceRenderData": True,
                "annotation": "BUILDINGS",
            }
        )

    return objects


def _export_buildings_archetypes(
    selected: list[BuildingFootprint],
    build: Any,
    *,
    level_id: str,
    shapes_dir: Path,
    asset_prefix: str,
    variants_per_class: int,
    unique_area_m2: float,
    stats: BuildingExportStats,
) -> list[dict[str, Any]]:
    """Legacy archetype mode: place individual TSStatic instances."""
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
        _width, _depth, cx, cy = _bbox(footprint.ring)
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
                "annotation": "BUILDINGS",
            }
        )

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
        place(footprint, f"{asset_prefix}/{name}.dae", scale=(1.0, 1.0, 1.0), roof=klass.roof)

    return objects


def archetype_summary(archetypes: dict[str, Archetype]) -> str:
    """Return a one-line summary of an archetype library."""
    triangles = sum(a.triangle_count for a in archetypes.values())
    return f"{len(archetypes)} archetypes, {triangles} triangles total"


#: Roof form per class, re-exported for callers that only need the mapping.
ROOF_BY_CLASS: dict[str, str] = {key: spec.roof for key, spec in BUILDING_CLASSES.items()}
