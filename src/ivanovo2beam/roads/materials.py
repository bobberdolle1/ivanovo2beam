"""BeamNG decal road and marking material library.

Defines PBR materials for road surfaces (asphalt, concrete, dirt, gravel)
and lane markings (white/yellow solid, dashed, double) referencing BeamNG's
shipped DDS/PNG assets by path without copying asset bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ivanovo2beam.beamng.level import stable_uuid


def build_road_materials_json(level_id: str) -> dict[str, Any]:
    """Build the material definitions for roads and markings.

    Emitted to ``levels/<id>/art/road/main.materials.json`` and
    ``levels/<id>/main.materials.json``.

    Args:
        level_id: Level identifier for deterministic UUIDs.

    Returns:
        JSON-serialisable dict of material objects.
    """
    materials: dict[str, Any] = {
        "road_asphalt_2lane": {
            "name": "road_asphalt_2lane",
            "mapTo": "road_asphalt_2lane",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "road_asphalt_2lane"),
            "Stages": [
                {
                    "ambientOcclusionMap": "/assets/materials/decalroad/asphalt/AsphaltRoad_variation_02/t_asphalt_variation_02_ao.data.dds",
                    "baseColorFactor": [0.85, 0.85, 0.85, 1.0],
                    "baseColorMap": "/assets/materials/decalroad/asphalt/AsphaltRoad_variation_02/t_asphalt_variation_02_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/asphalt/AsphaltRoad_variation_02/t_asphalt_variation_02_nm.normal.dds",
                    "opacityMap": "/assets/materials/decalroad/asphalt/AsphaltRoad_variation_02/t_asphalt_variation_02_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/asphalt/AsphaltRoad_variation_02/t_asphalt_variation_02_r.data.dds",
                    "detailScale": [3, 30],
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "ASPHALT",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
        "road1_concrete": {
            "name": "road1_concrete",
            "mapTo": "road1_concrete",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "road1_concrete"),
            "Stages": [
                {
                    "baseColorFactor": [0.85, 0.85, 0.85, 1.0],
                    "baseColorMap": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_b.color.dds",
                    "normalMap": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_nm.normal.dds",
                    "detailScale": [4, 4],
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "CONCRETE",
            "groundType": "CONCRETE",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
        "m_dirt_road": {
            "name": "m_dirt_road",
            "mapTo": "m_dirt_road",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "m_dirt_road"),
            "Stages": [
                {
                    "baseColorFactor": [0.65, 0.55, 0.45, 1.0],
                    "baseColorMap": "/assets/materials/terrain/soil/dirt_crumbly/t_dirt_crumbly_b.color.dds",
                    "normalMap": "/assets/materials/terrain/soil/dirt_crumbly/t_dirt_crumbly_nm.normal.dds",
                    "detailScale": [4, 4],
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "DIRT",
            "groundType": "DIRT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
        "m_dirt_road_gravels": {
            "name": "m_dirt_road_gravels",
            "mapTo": "m_dirt_road_gravels",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "m_dirt_road_gravels"),
            "Stages": [
                {
                    "baseColorFactor": [0.75, 0.75, 0.7, 1.0],
                    "baseColorMap": "/assets/materials/terrain/soil/t_gravel/t_gravel_b.png",
                    "normalMap": "/assets/materials/terrain/soil/t_gravel/t_gravel_nm.png",
                    "detailScale": [4, 4],
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "GRAVEL",
            "groundType": "GRAVEL",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
        "road_invisible": {
            "name": "road_invisible",
            "mapTo": "road_invisible",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "road_invisible"),
            "Stages": [
                {
                    "baseColorFactor": [0.0, 0.0, 0.0, 0.0],
                    "opacityFactor": 0.0,
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "castShadows": False,
            "translucent": True,
            "translucentZWrite": False,
            "version": 1.5,
        },
        "m_line_white": {
            "name": "m_line_white",
            "mapTo": "m_line_white",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "m_line_white"),
            "Stages": [
                {
                    "ambientOcclusionMap": "/assets/materials/decalroad/lines/chalk_marking/t_line_white_ao.data.dds",
                    "baseColorFactor": [0.99, 0.99, 0.99, 0.9],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white/t_line_white_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/lines/line_white/t_line_white_nm.normal.dds",
                    "opacityFactor": 0.85,
                    "opacityMap": "/assets/materials/decalroad/lines/line_white/t_line_white_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/lines/chalk_marking/t_line_white_r.data.dds",
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "SOLID_LINE",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
        "m_line_white_discontinue": {
            "name": "m_line_white_discontinue",
            "mapTo": "m_line_white_discontinue",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "m_line_white_discontinue"),
            "Stages": [
                {
                    "ambientOcclusionMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_ao.data.dds",
                    "baseColorFactor": [0.99, 0.99, 0.99, 0.9],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_nm.normal.dds",
                    "opacityFactor": 0.85,
                    "opacityMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_r.data.dds",
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "SOLID_LINE",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
        "m_line_yellow": {
            "name": "m_line_yellow",
            "mapTo": "m_line_yellow",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "m_line_yellow"),
            "Stages": [
                {
                    "ambientOcclusionMap": "/assets/materials/decalroad/lines/chalk_marking/t_line_white_ao.data.dds",
                    "baseColorFactor": [0.95, 0.80, 0.25, 0.9],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white/t_line_white_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/lines/line_white/t_line_white_nm.normal.dds",
                    "opacityFactor": 0.85,
                    "opacityMap": "/assets/materials/decalroad/lines/line_white/t_line_white_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/lines/chalk_marking/t_line_white_r.data.dds",
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "SOLID_LINE",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
        "m_line_yellow_double": {
            "name": "m_line_yellow_double",
            "mapTo": "m_line_yellow_double",
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", "m_line_yellow_double"),
            "Stages": [
                {
                    "ambientOcclusionMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_ao.data.dds",
                    "baseColorFactor": [0.95, 0.80, 0.25, 0.9],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_nm.normal.dds",
                    "opacityFactor": 0.85,
                    "opacityMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_r.data.dds",
                },
                {},
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "SOLID_LINE",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
    }

    # Add aliases matching common game material names
    materials["AsphaltRoad_variation_two_way"] = materials["road_asphalt_2lane"]
    materials["line_white"] = materials["m_line_white"]
    materials["line_white_discontinue"] = materials["m_line_white_discontinue"]
    materials["line_yellow"] = materials["m_line_yellow"]
    materials["line_yellow_double"] = materials["m_line_yellow_double"]

    return materials


def write_road_materials(level_dir: Path, level_id: str) -> list[Path]:
    """Write road materials to art/road/main.materials.json and main.materials.json.

    Args:
        level_dir: Root directory of the level.
        level_id: Level identifier.

    Returns:
        List of written paths.
    """
    materials = build_road_materials_json(level_id)
    text = json.dumps(materials, indent=2, ensure_ascii=False) + "\n"

    paths = [
        level_dir / "art" / "road" / "main.materials.json",
        level_dir / "main.materials.json",
    ]
    written = []
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        written.append(p)
    return written
