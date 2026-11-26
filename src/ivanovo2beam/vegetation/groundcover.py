"""Native BeamNG GroundCover generation for summer grass, wildflowers, ferns, and low scrub.

Generates:
1. ``levels/<id>/art/shapes/groundcover/main.materials.json`` referencing stock
   BeamNG ``/assets/materials/foliage/...`` DDS textures directly (zero copied
   asset bytes);
2. Optional zero-copy ``.link`` pointers to ``east_coast_usa`` 3D grass, clover,
   daisy, and fern shapes under ``levels/<id>/art/shapes/linked/east_coast_usa/``;
3. Native ``GroundCover`` scene objects (with fixed 8-slot ``Types`` arrays,
   verified ``billboardUVs`` / ``shapeFilename`` entries, and strict ``layer``
   terrain material masks so vegetation never spawns on asphalt, concrete,
   gravel, sand, river channels, or bridges).
"""

from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ivanovo2beam.beamng.level import stable_uuid
from ivanovo2beam.beamng.materials import DEFAULT_MATERIAL_ORDER

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

#: Deterministic UUID namespace for GroundCover materials and objects.
_GROUNDCOVER_NS = uuid.UUID("4f88c2a1-7b23-4d91-9e14-8a2b1c5d6e7f")

#: BeamNG Torque3D GroundCover has a fixed 8-slot Types array.
MAX_COVERTYPES = 8

#: Terrain materials that may NEVER receive GroundCover elements.
EXCLUDED_TERRAIN_LAYERS = frozenset(
    {
        "Asphalt",
        "Asphalt_worn",
        "Concrete",
        "Gravel",
        "Rock",
        "Sand",
        "Pebbles",
    }
)

#: Stock GroundCover PBR materials (using verified /assets/materials/foliage/*.dds).
GROUNDCOVER_MATERIALS: dict[str, dict[str, Any]] = {
    "m_grass_green_short_01": {
        "baseColorMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_b.color.dds",
        "normalMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_nm.normal.dds",
        "opacityMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_o.data.dds",
        "roughnessMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_ao.data.dds",
        "baseColorFactor": [0.84, 0.86, 0.80, 1.0],
        "alphaRef": 35,
        "invertBackFaceNormals": True,
    },
    "m_grass_green_long_01": {
        "baseColorMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_b.color.dds",
        "normalMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_nm.normal.dds",
        "opacityMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_o.data.dds",
        "roughnessMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_ao.data.dds",
        "baseColorFactor": [0.84, 0.86, 0.78, 1.0],
        "alphaRef": 25,
        "invertBackFaceNormals": True,
    },
    "m_seeded_grass_01": {
        "baseColorMap": "/assets/materials/foliage/grass/t_seeded_grass_01/t_seeded_grass_01_b.color.dds",
        "normalMap": "/assets/materials/foliage/grass/t_seeded_grass_01/t_seeded_grass_01_nm.normal.dds",
        "opacityMap": "/assets/materials/foliage/grass/t_seeded_grass_01/t_seeded_grass_01_o.data.dds",
        "roughnessMap": "/assets/materials/foliage/grass/t_seeded_grass_01/t_seeded_grass_01_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/foliage/grass/t_seeded_grass_01/t_seeded_grass_01_ao.data.dds",
        "baseColorFactor": [0.88, 0.88, 0.82, 1.0],
        "alphaRef": 50,
        "invertBackFaceNormals": True,
    },
    "m_grass_dry_01": {
        "baseColorMap": "/assets/materials/foliage/grass/t_grass_dry_long_01/t_grass_dry_long_01_b.color.dds",
        "normalMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_nm.normal.dds",
        "opacityMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_o.data.dds",
        "roughnessMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_ao.data.dds",
        "baseColorFactor": [0.92, 0.90, 0.84, 1.0],
        "alphaRef": 20,
        "invertBackFaceNormals": True,
    },
    "m_flowers_01": {
        "baseColorMap": "/assets/materials/foliage/groundcover/t_flowers_01/t_flowers_01_b.color.dds",
        "normalMap": "/assets/materials/foliage/groundcover/t_flowers_01/t_flowers_01_nm.normal.dds",
        "opacityMap": "/assets/materials/foliage/groundcover/t_flowers_01/t_flowers_01_o.data.dds",
        "roughnessMap": "/assets/materials/foliage/groundcover/t_flowers_01/t_flowers_01_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/foliage/groundcover/t_flowers_01/t_flowers_01_ao.data.dds",
        "baseColorFactor": [0.92, 0.92, 0.90, 1.0],
        "alphaRef": 10,
        "invertBackFaceNormals": True,
    },
    "Groundcover_spring": {
        "baseColorMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_b.color.dds",
        "normalMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_nm.normal.dds",
        "opacityMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_o.data.dds",
        "roughnessMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_ao.data.dds",
        "baseColorFactor": [0.80, 0.82, 0.78, 1.0],
        "alphaRef": 40,
        "invertBackFaceNormals": True,
    },
    "italy_bushes_bb": {
        "baseColorMap": "/assets/materials/foliage/bush/t_italy_bushes_bb/t_italy_bushes_bb_b.color.dds",
        "normalMap": "/assets/materials/foliage/bush/t_italy_bushes_bb/t_italy_bushes_bb_nm.normal.dds",
        "opacityMap": "/assets/materials/foliage/bush/t_italy_bushes_bb/t_italy_bushes_bb_o.data.dds",
        "roughnessMap": "/assets/materials/foliage/bush/t_italy_bushes_bb/t_italy_bushes_bb_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/foliage/bush/t_italy_bushes_bb/t_italy_bushes_bb_ao.data.dds",
        "baseColorFactor": [0.88, 0.88, 0.84, 1.0],
        "alphaRef": 88,
        "invertBackFaceNormals": True,
    },
}

#: Optional 3D groundcover shapes linked from east_coast_usa (zero-copy .link).
LINKED_GROUNDCOVER_SHAPES: dict[str, str] = {
    "s_grass_green_short_01": (
        "levels/east_coast_usa/art/shapes/groundcover/3d_grass_test/s_grass_green_short_01.dae"
    ),
    "s_grass_green_long_01": (
        "levels/east_coast_usa/art/shapes/groundcover/3d_grass_test/s_grass_green_long_01.dae"
    ),
    "clover_closetop": "levels/east_coast_usa/art/shapes/groundcover/Clover_closetop.dae",
    "daisies_closetop": "levels/east_coast_usa/art/shapes/groundcover/daisies_closetop.dae",
    "lush_ferns_01_a": "levels/east_coast_usa/art/shapes/groundcover/lush_ferns_01_a.dae",
}


@dataclass(slots=True)
class GroundCoverStats:
    """Diagnostics from native GroundCover generation."""

    layers: int = 0
    total_max_elements: int = 0
    linked_shapes: int = 0
    files_written: int = 0
    bytes_written: int = 0
    by_name: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary."""
        return {
            "layers": self.layers,
            "total_max_elements": self.total_max_elements,
            "linked_shapes": self.linked_shapes,
            "files_written": self.files_written,
            "bytes_written": self.bytes_written,
            "by_name": dict(self.by_name),
        }


def build_groundcover_materials_json() -> dict[str, Any]:
    """Generate ``art/shapes/groundcover/main.materials.json`` for GroundCover."""
    out: dict[str, Any] = {}
    for key, spec in sorted(GROUNDCOVER_MATERIALS.items()):
        mat_id = str(uuid.uuid5(_GROUNDCOVER_NS, f"mat_{key}"))
        stage0: dict[str, Any] = {
            "baseColorMap": spec["baseColorMap"],
            "normalMap": spec["normalMap"],
            "opacityMap": spec["opacityMap"],
            "roughnessMap": spec["roughnessMap"],
            "ambientOcclusionMap": spec["ambientOcclusionMap"],
            "baseColorFactor": list(spec.get("baseColorFactor", [0.9, 0.9, 0.9, 1.0])),
            "pixelSpecular": True,
        }
        entry: dict[str, Any] = {
            "name": key,
            "mapTo": key,
            "class": "Material",
            "persistentId": mat_id,
            "Stages": [stage0, {}, {}, {}],
            "alphaRef": int(spec.get("alphaRef", 40)),
            "alphaTest": True,
            "annotation": "NATURE",
            "doubleSided": True,
            "groundType": "GRASS",
            "materialTag0": "beamng",
            "materialTag1": "vegetation",
            "materialTag2": "Natural",
            "subSurface": True,
            "subSurfaceIntensity": 1.0,
            "version": 1.5,
        }
        if spec.get("invertBackFaceNormals"):
            entry["invertBackFaceNormals"] = True
        out[key] = entry
    return out


def prepare_linked_groundcover_shapes(
    level_id: str,
    level_dir: Path,
    beamng_dir: Path | None,
) -> tuple[dict[str, str], int, int]:
    """Emit zero-copy ``.link`` pointers for stock 3D groundcover shapes."""
    if beamng_dir is None:
        return {}, 0, 0

    zip_path = beamng_dir / "content" / "levels" / "east_coast_usa.zip"
    entries: set[str] = set()
    if zip_path.is_file():
        try:
            with zipfile.ZipFile(zip_path) as zf:
                entries = {name.replace("\\", "/").lstrip("/") for name in zf.namelist()}
        except (zipfile.BadZipFile, OSError):
            entries = set()
    else:
        dir_path = beamng_dir / "content" / "levels" / "east_coast_usa"
        if dir_path.is_dir():
            content_root = beamng_dir / "content"
            entries = {
                p.relative_to(content_root).as_posix() for p in dir_path.rglob("*") if p.is_file()
            }

    if not entries:
        return {}, 0, 0

    active: dict[str, str] = {}
    files_written = 0
    bytes_written = 0
    linked_root = level_dir / "art" / "shapes" / "linked"

    for key, src_rel in LINKED_GROUNDCOVER_SHAPES.items():
        if src_rel not in entries:
            continue
        parts = src_rel.split("/")
        if len(parts) < 5 or parts[0] != "levels" or parts[2:4] != ["art", "shapes"]:
            continue
        src_level = parts[1]
        rel_after_shapes = "/".join(parts[4:])
        targets = [(src_rel, rel_after_shapes)]
        if src_rel.lower().endswith(".dae"):
            cdae_src = src_rel[:-4] + ".cdae"
            cdae_rel = rel_after_shapes[:-4] + ".cdae"
            if cdae_src in entries:
                targets.append((cdae_src, cdae_rel))
        for s_rel, l_rel in targets:
            link_path = linked_root / src_level / f"{l_rel}.link"
            link_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"path": f"/{s_rel}", "type": "normal"}, indent=2) + "\n"
            link_path.write_text(payload, encoding="utf-8")
            files_written += 1
            bytes_written += len(payload)
        active[key] = f"/levels/{level_id}/art/shapes/linked/{src_level}/{rel_after_shapes}"

    return active, files_written, bytes_written


def _empty_type_slot() -> dict[str, Any]:
    """Return an inactive slot matching BeamNG's 8-slot GroundCover Types schema."""
    return {
        "billboardUVs": None,
        "clumpRadius": None,
        "maxClumpCount": None,
        "minClumpCount": None,
        "probability": None,
        "sizeMax": None,
        "sizeMin": None,
        "windScale": None,
    }


def _make_billboard_type(
    layer: str,
    billboard_uvs: tuple[float, float, float, float],
    *,
    size_min: float,
    size_max: float,
    min_clump: int,
    max_clump: int,
    clump_radius: float,
    probability: float,
    wind_scale: float,
    max_slope: float = 42.0,
    clump_exponent: float | None = None,
) -> dict[str, Any]:
    """Build a single GroundCover billboard Type entry."""
    entry: dict[str, Any] = {
        "billboardUVs": [round(v, 6) for v in billboard_uvs],
        "clumpRadius": clump_radius,
        "layer": layer,
        "maxClumpCount": max_clump,
        "maxSlope": max_slope,
        "minClumpCount": min_clump,
        "probability": probability,
        "sizeMax": size_max,
        "sizeMin": size_min,
        "windScale": wind_scale,
    }
    if clump_exponent is not None:
        entry["clumpExponent"] = clump_exponent
    return entry


def _make_shape_type(
    layer: str,
    shape_filename: str,
    *,
    size_min: float,
    size_max: float,
    min_clump: int,
    max_clump: int,
    clump_radius: float,
    probability: float,
    wind_scale: float,
    max_slope: float = 40.0,
) -> dict[str, Any]:
    """Build a 3D mesh GroundCover Type entry."""
    return {
        "billboardUVs": None,
        "clumpRadius": clump_radius,
        "layer": layer,
        "maxClumpCount": max_clump,
        "maxSlope": max_slope,
        "minClumpCount": min_clump,
        "probability": probability,
        "shapeFilename": shape_filename,
        "sizeMax": size_max,
        "sizeMin": size_min,
        "windScale": wind_scale,
    }


def _pad_types(types: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim or pad ``Types`` to BeamNG's fixed 8-slot length."""
    trimmed = list(types[:MAX_COVERTYPES])
    while len(trimmed) < MAX_COVERTYPES:
        trimmed.append(_empty_type_slot())
    return trimmed


def _make_groundcover_obj(
    level_id: str,
    name: str,
    *,
    position: tuple[float, float, float],
    material: str | None,
    types: list[dict[str, Any]],
    radius: float,
    dissolve_radius: float,
    shape_cull_radius: float,
    grid_size: int,
    max_elements: int,
    seed: int,
    parent: str = "vegetation",
    max_billboard_tilt_angle: float = 40.0,
    wind_gust_frequency: float = 8.0,
    wind_gust_length: float = 8.0,
    wind_gust_strength: float = 0.15,
    wind_turbulence_frequency: float = 0.35,
    wind_turbulence_strength: float = 0.35,
) -> dict[str, Any]:
    """Build one BeamNG GroundCover scene object."""
    obj: dict[str, Any] = {
        "name": name,
        "class": "GroundCover",
        "persistentId": stable_uuid(level_id, "GroundCover", name),
        "__parent": parent,
        "position": [round(position[0], 4), round(position[1], 4), round(position[2], 4)],
        "Types": _pad_types(types),
        "dissolveRadius": dissolve_radius,
        "gridSize": grid_size,
        "maxBillboardTiltAngle": max_billboard_tilt_angle,
        "maxElements": max_elements,
        "radius": radius,
        "seed": seed,
        "shapeCullRadius": shape_cull_radius,
        "shapesCastShadows": False,
        "windGustFrequency": wind_gust_frequency,
        "windGustLength": wind_gust_length,
        "windGustStrength": wind_gust_strength,
        "windTurbulenceFrequency": wind_turbulence_frequency,
        "windTurbulenceStrength": wind_turbulence_strength,
    }
    if material is not None:
        obj["material"] = material
    return obj


def _filter_allowed_types(
    types: Sequence[dict[str, Any]],
    allowed_layers: set[str],
) -> list[dict[str, Any]]:
    """Keep only types bound to valid, non-road/non-water terrain layers."""
    return [
        t
        for t in types
        if t.get("layer") in allowed_layers and t.get("layer") not in EXCLUDED_TERRAIN_LAYERS
    ]


def build_groundcover_objects(
    level_id: str,
    *,
    center_pos: tuple[float, float, float] = (0.0, 0.0, 100.0),
    terrain_materials: Sequence[str] | None = None,
    linked_shapes: dict[str, str] | None = None,
    parent: str = "vegetation",
) -> list[dict[str, Any]]:
    """Build deterministic GroundCover scene objects for Russian summer biomes.

    Tuned for RTX 3080 high FPS (``shapesCastShadows=False``, radius 38-105 m,
    moderate ``maxElements`` per layer) and strictly masked by terrain materials
    (never spawning on ``Asphalt``, ``Asphalt_worn``, ``Concrete``, ``Gravel``,
    ``Rock``, ``Sand``, or ``Pebbles``).
    """
    active_mats = set(terrain_materials or DEFAULT_MATERIAL_ORDER)
    allowed_layers = active_mats - EXCLUDED_TERRAIN_LAYERS
    grass_layers = [lyr for lyr in ("Grass", "Meadow", "Grass2", "Grass3") if lyr in allowed_layers]
    if not grass_layers:
        grass_layers = ["Grass"] if "Grass" in allowed_layers else []
    dry_field_layers = [lyr for lyr in ("DryGrass", "Field", "GrassDry") if lyr in allowed_layers]

    objects: list[dict[str, Any]] = []

    # 1. Close meadow & riverbank grass (dense short blades, 0-36 m, tight clumpRadius so blades never bleed onto roads)
    close_grass_types: list[dict[str, Any]] = []
    for lyr in grass_layers[:2]:
        close_grass_types.extend(
            [
                _make_billboard_type(
                    lyr,
                    (0.0, 0.0078125, 1.0, 0.464844),
                    size_min=0.14,
                    size_max=0.28,
                    min_clump=3,
                    max_clump=6,
                    clump_radius=0.75,
                    probability=0.9,
                    wind_scale=0.12,
                    max_slope=42.0,
                ),
                _make_billboard_type(
                    lyr,
                    (0.0, 0.505125, 1.0, 0.472656),
                    size_min=0.16,
                    size_max=0.30,
                    min_clump=2,
                    max_clump=5,
                    clump_radius=0.65,
                    probability=0.65,
                    wind_scale=0.15,
                    max_slope=42.0,
                ),
            ]
        )
    if "RiverbankSoil" in allowed_layers:
        close_grass_types.append(
            _make_billboard_type(
                "RiverbankSoil",
                (0.0, 0.515625, 0.5, 0.476563),
                size_min=0.18,
                size_max=0.36,
                min_clump=2,
                max_clump=4,
                clump_radius=0.75,
                probability=0.45,
                wind_scale=0.15,
                max_slope=35.0,
            )
        )
    close_grass_types = _filter_allowed_types(close_grass_types, allowed_layers)
    if close_grass_types:
        objects.append(
            _make_groundcover_obj(
                level_id,
                "gc_meadow_grass_close",
                position=center_pos,
                material="m_grass_green_short_01",
                types=close_grass_types,
                radius=36.0,
                dissolve_radius=22.0,
                shape_cull_radius=36.0,
                grid_size=6,
                max_elements=34000,
                seed=11,
                parent=parent,
            )
        )

    # 2. Medium/distant meadow grass (seamless ground cover out to 78 m)
    distant_grass_types: list[dict[str, Any]] = []
    for lyr in grass_layers[:2]:
        distant_grass_types.extend(
            [
                _make_billboard_type(
                    lyr,
                    (0.0, 0.0, 1.0, 0.491806),
                    size_min=0.28,
                    size_max=0.52,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.95,
                    probability=0.9,
                    wind_scale=0.18,
                    max_slope=42.0,
                ),
                _make_billboard_type(
                    lyr,
                    (0.0, 0.511718, 1.0, 0.488282),
                    size_min=0.32,
                    size_max=0.58,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.95,
                    probability=0.6,
                    wind_scale=0.20,
                    max_slope=42.0,
                ),
            ]
        )
    distant_grass_types = _filter_allowed_types(distant_grass_types, allowed_layers)
    if distant_grass_types:
        objects.append(
            _make_groundcover_obj(
                level_id,
                "gc_meadow_grass_distant",
                position=center_pos,
                material="m_grass_green_long_01",
                types=distant_grass_types,
                radius=78.0,
                dissolve_radius=50.0,
                shape_cull_radius=65.0,
                grid_size=8,
                max_elements=46000,
                seed=17,
                parent=parent,
            )
        )

    # 3. Tall seeded roadside & field grass (clumped wild meadow plumes)
    tall_grass_types: list[dict[str, Any]] = []
    if grass_layers:
        tall_grass_types.extend(
            [
                _make_billboard_type(
                    grass_layers[0],
                    (0.0, 0.0, 1.0, 0.491806),
                    size_min=0.45,
                    size_max=0.78,
                    min_clump=2,
                    max_clump=5,
                    clump_radius=0.95,
                    probability=0.45,
                    wind_scale=0.25,
                    max_slope=40.0,
                ),
                _make_billboard_type(
                    grass_layers[0],
                    (0.0, 0.511718, 1.0, 0.488282),
                    size_min=0.42,
                    size_max=0.72,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.85,
                    probability=0.35,
                    wind_scale=0.25,
                    max_slope=40.0,
                ),
            ]
        )
    for field_lyr in dry_field_layers:
        tall_grass_types.append(
            _make_billboard_type(
                field_lyr,
                (0.0, 0.511718, 1.0, 0.488282),
                size_min=0.45,
                size_max=0.82,
                min_clump=2,
                max_clump=5,
                clump_radius=1.0,
                probability=0.55,
                wind_scale=0.28,
                max_slope=40.0,
            )
        )
    tall_grass_types = _filter_allowed_types(tall_grass_types, allowed_layers)
    if tall_grass_types:
        objects.append(
            _make_groundcover_obj(
                level_id,
                "gc_tall_roadside_grass",
                position=center_pos,
                material="m_seeded_grass_01",
                types=tall_grass_types,
                radius=72.0,
                dissolve_radius=46.0,
                shape_cull_radius=62.0,
                grid_size=8,
                max_elements=32000,
                seed=23,
                parent=parent,
            )
        )

    # 4. Warm dry field grass / stubble on Field, GrassDry & Dirt margins
    field_dry_types: list[dict[str, Any]] = []
    for field_lyr in dry_field_layers:
        field_dry_types.extend(
            [
                _make_billboard_type(
                    field_lyr,
                    (0.0, 0.0, 1.0, 0.493654),
                    size_min=0.24,
                    size_max=0.48,
                    min_clump=2,
                    max_clump=5,
                    clump_radius=0.95,
                    probability=0.85,
                    wind_scale=0.20,
                    max_slope=40.0,
                ),
                _make_billboard_type(
                    field_lyr,
                    (0.0, 0.523437, 1.0, 0.448785),
                    size_min=0.28,
                    size_max=0.52,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.85,
                    probability=0.60,
                    wind_scale=0.22,
                    max_slope=40.0,
                ),
            ]
        )
    if "Dirt" in allowed_layers:
        field_dry_types.append(
            _make_billboard_type(
                "Dirt",
                (0.0, 0.523437, 1.0, 0.448785),
                size_min=0.22,
                size_max=0.42,
                min_clump=1,
                max_clump=3,
                clump_radius=0.80,
                probability=0.25,
                wind_scale=0.18,
                max_slope=38.0,
            )
        )
    field_dry_types = _filter_allowed_types(field_dry_types, allowed_layers)
    if field_dry_types:
        objects.append(
            _make_groundcover_obj(
                level_id,
                "gc_field_grass",
                position=center_pos,
                material="m_grass_dry_01",
                types=field_dry_types,
                radius=75.0,
                dissolve_radius=48.0,
                shape_cull_radius=65.0,
                grid_size=7,
                max_elements=30000,
                seed=29,
                parent=parent,
            )
        )

    # 5. Summer wildflowers in meadows & fields
    flower_types: list[dict[str, Any]] = []
    flower_uvs = (
        (0.391768, 0.386836, 0.150037, 0.226648),
        (0.542877, 0.323836, 0.092376, 0.288212),
        (0.906522, 0.262987, 0.093478, 0.351979),
        (0.635661, 0.330551, 0.272889, 0.279477),
    )
    flower_source_layers = [lyr for lyr in ("Meadow", "Grass", "DryGrass", "Field") if lyr in allowed_layers]
    if flower_source_layers:
        primary_flower_lyr = flower_source_layers[0]
        for idx, uvs in enumerate(flower_uvs):
            flower_types.append(
                _make_billboard_type(
                    primary_flower_lyr,
                    uvs,
                    size_min=0.16,
                    size_max=0.28,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.35,
                    probability=0.60 if idx < 2 else 0.40,
                    wind_scale=0.15,
                    max_slope=38.0,
                )
            )
        for sec_lyr in flower_source_layers[1:3]:
            flower_types.append(
                _make_billboard_type(
                    sec_lyr,
                    flower_uvs[0],
                    size_min=0.16,
                    size_max=0.28,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.45,
                    probability=0.35,
                    wind_scale=0.15,
                    max_slope=38.0,
                )
            )
    flower_types = _filter_allowed_types(flower_types, allowed_layers)
    if flower_types:
        objects.append(
            _make_groundcover_obj(
                level_id,
                "gc_wildflowers",
                position=center_pos,
                material="m_flowers_01",
                types=flower_types,
                radius=50.0,
                dissolve_radius=32.0,
                shape_cull_radius=45.0,
                grid_size=5,
                max_elements=16000,
                seed=31,
                parent=parent,
            )
        )

    # 6. Forest ferns & woodland undergrowth (ForestFloor + meadow daisies/buttercups)
    forest_gc_types: list[dict[str, Any]] = []
    if "ForestFloor" in allowed_layers:
        forest_gc_types.extend(
            [
                _make_billboard_type(
                    "ForestFloor",
                    (0.0, 0.71094, 0.164062, 0.28906),
                    size_min=0.45,
                    size_max=0.80,
                    min_clump=2,
                    max_clump=5,
                    clump_radius=1.1,
                    probability=0.85,
                    wind_scale=0.18,
                    max_slope=45.0,
                ),
                _make_billboard_type(
                    "ForestFloor",
                    (0.160157, 0.710938, 0.250001, 0.289062),
                    size_min=0.48,
                    size_max=0.85,
                    min_clump=2,
                    max_clump=5,
                    clump_radius=1.2,
                    probability=0.85,
                    wind_scale=0.18,
                    max_slope=45.0,
                ),
                _make_billboard_type(
                    "ForestFloor",
                    (0.0, 0.570313, 0.156249, 0.14453),
                    size_min=0.26,
                    size_max=0.46,
                    min_clump=2,
                    max_clump=5,
                    clump_radius=1.2,
                    probability=0.90,
                    wind_scale=0.08,
                    max_slope=45.0,
                ),
                _make_billboard_type(
                    "ForestFloor",
                    (0.394531, 0.695313, 0.15625, 0.144532),
                    size_min=0.25,
                    size_max=0.42,
                    min_clump=3,
                    max_clump=6,
                    clump_radius=1.0,
                    probability=0.80,
                    wind_scale=0.08,
                    max_slope=45.0,
                ),
            ]
        )
    if grass_layers:
        forest_gc_types.extend(
            [
                _make_billboard_type(
                    grass_layers[0],
                    (0.0, 0.359375, 0.199218, 0.222655),
                    size_min=0.28,
                    size_max=0.48,
                    min_clump=2,
                    max_clump=5,
                    clump_radius=0.55,
                    probability=0.35,
                    wind_scale=0.08,
                    max_slope=38.0,
                ),
                _make_billboard_type(
                    grass_layers[0],
                    (0.808594, 0.398437, 0.191406, 0.226561),
                    size_min=0.28,
                    size_max=0.45,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.50,
                    probability=0.30,
                    wind_scale=0.10,
                    max_slope=38.0,
                ),
            ]
        )
    forest_gc_types = _filter_allowed_types(forest_gc_types, allowed_layers)
    if forest_gc_types:
        objects.append(
            _make_groundcover_obj(
                level_id,
                "gc_forest_undergrowth",
                position=center_pos,
                material="Groundcover_spring",
                types=forest_gc_types,
                radius=72.0,
                dissolve_radius=46.0,
                shape_cull_radius=62.0,
                grid_size=7,
                max_elements=32000,
                seed=37,
                parent=parent,
            )
        )

    # 7. Low woody scrub & undergrowth bushes (ForestFloor, Field, Dirt)
    scrub_types: list[dict[str, Any]] = []
    if "ForestFloor" in allowed_layers:
        scrub_types.extend(
            [
                _make_billboard_type(
                    "ForestFloor",
                    (0.0, 0.316407, 0.265624, 0.332031),
                    size_min=0.45,
                    size_max=0.78,
                    min_clump=2,
                    max_clump=3,
                    clump_radius=0.7,
                    probability=0.65,
                    wind_scale=0.18,
                    max_slope=48.0,
                ),
                _make_billboard_type(
                    "ForestFloor",
                    (0.496094, 0.300781, 0.230469, 0.296876),
                    size_min=0.50,
                    size_max=0.92,
                    min_clump=2,
                    max_clump=3,
                    clump_radius=0.8,
                    probability=0.75,
                    wind_scale=0.18,
                    max_slope=48.0,
                ),
            ]
        )
    for field_lyr in dry_field_layers[:2]:
        scrub_types.append(
            _make_billboard_type(
                field_lyr,
                (0.0, 0.65625, 0.339843, 0.34375),
                size_min=0.45,
                size_max=0.75,
                min_clump=1,
                max_clump=2,
                clump_radius=0.85,
                probability=0.20 if field_lyr == "DryGrass" else 0.12,
                wind_scale=0.18,
                max_slope=45.0,
            )
        )
    scrub_types = _filter_allowed_types(scrub_types, allowed_layers)
    if scrub_types:
        objects.append(
            _make_groundcover_obj(
                level_id,
                "gc_low_scrub",
                position=center_pos,
                material="italy_bushes_bb",
                types=scrub_types,
                radius=80.0,
                dissolve_radius=52.0,
                shape_cull_radius=68.0,
                grid_size=8,
                max_elements=10000,
                seed=41,
                parent=parent,
                max_billboard_tilt_angle=55.0,
            )
        )

    # 8. Optional close-range 3D grass/clover/fern meshes when linked from east_coast_usa
    if linked_shapes:
        shape_types: list[dict[str, Any]] = []
        if grass_layers and "s_grass_green_short_01" in linked_shapes:
            shape_types.append(
                _make_shape_type(
                    grass_layers[0],
                    linked_shapes["s_grass_green_short_01"],
                    size_min=0.35,
                    size_max=0.60,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.75,
                    probability=0.45,
                    wind_scale=0.20,
                )
            )
        if grass_layers and "s_grass_green_long_01" in linked_shapes:
            shape_types.append(
                _make_shape_type(
                    grass_layers[0],
                    linked_shapes["s_grass_green_long_01"],
                    size_min=0.45,
                    size_max=0.75,
                    min_clump=2,
                    max_clump=3,
                    clump_radius=0.85,
                    probability=0.30,
                    wind_scale=0.25,
                )
            )
        if grass_layers and "clover_closetop" in linked_shapes:
            shape_types.append(
                _make_shape_type(
                    grass_layers[0],
                    linked_shapes["clover_closetop"],
                    size_min=0.35,
                    size_max=0.55,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=0.65,
                    probability=0.25,
                    wind_scale=0.10,
                )
            )
        if grass_layers and "daisies_closetop" in linked_shapes:
            shape_types.append(
                _make_shape_type(
                    grass_layers[0],
                    linked_shapes["daisies_closetop"],
                    size_min=0.30,
                    size_max=0.50,
                    min_clump=2,
                    max_clump=3,
                    clump_radius=0.60,
                    probability=0.15,
                    wind_scale=0.12,
                )
            )
        if "ForestFloor" in allowed_layers and "lush_ferns_01_a" in linked_shapes:
            shape_types.append(
                _make_shape_type(
                    "ForestFloor",
                    linked_shapes["lush_ferns_01_a"],
                    size_min=0.45,
                    size_max=0.80,
                    min_clump=2,
                    max_clump=4,
                    clump_radius=1.0,
                    probability=0.45,
                    wind_scale=0.22,
                    max_slope=44.0,
                )
            )
        shape_types = _filter_allowed_types(shape_types, allowed_layers)
        if shape_types:
            objects.append(
                _make_groundcover_obj(
                    level_id,
                    "gc_3d_foliage_close",
                    position=center_pos,
                    material=None,
                    types=shape_types,
                    radius=28.0,
                    dissolve_radius=18.0,
                    shape_cull_radius=28.0,
                    grid_size=6,
                    max_elements=10000,
                    seed=43,
                    parent=parent,
                )
            )

    return objects


def export_groundcover(
    level_id: str,
    level_dir: Path,
    *,
    center_pos: tuple[float, float, float] = (0.0, 0.0, 100.0),
    terrain_materials: Sequence[str] | None = None,
    beamng_dir: Path | None = None,
    parent: str = "vegetation",
) -> tuple[list[dict[str, Any]], GroundCoverStats]:
    """Write GroundCover materials/links and return GroundCover scene objects."""
    stats = GroundCoverStats()

    mat_path = level_dir / "art" / "shapes" / "groundcover" / "main.materials.json"
    mat_path.parent.mkdir(parents=True, exist_ok=True)
    mat_text = json.dumps(build_groundcover_materials_json(), indent=2, ensure_ascii=False) + "\n"
    mat_path.write_text(mat_text, encoding="utf-8")
    stats.files_written += 1
    stats.bytes_written += len(mat_text)

    linked_shapes, link_files, link_bytes = prepare_linked_groundcover_shapes(
        level_id, level_dir, beamng_dir
    )
    stats.linked_shapes = len(linked_shapes)
    stats.files_written += link_files
    stats.bytes_written += link_bytes

    objects = build_groundcover_objects(
        level_id,
        center_pos=center_pos,
        terrain_materials=terrain_materials,
        linked_shapes=linked_shapes,
        parent=parent,
    )
    stats.layers = len(objects)
    for obj in objects:
        max_el = int(obj.get("maxElements", 0))
        stats.total_max_elements += max_el
        stats.by_name[str(obj["name"])] = max_el

    return objects, stats
