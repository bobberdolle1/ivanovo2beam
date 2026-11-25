"""BeamNG decal road and marking material library.

Defines PBR materials for road surfaces (asphalt, concrete, dirt, gravel)
and lane markings (white/yellow solid, dashed, double) referencing BeamNG's
shipped DDS/PNG assets by path without copying asset bytes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ivanovo2beam.beamng.level import stable_uuid


def _alias_material(source: dict[str, Any], alias_name: str, level_id: str) -> dict[str, Any]:
    """Deep-copy a material definition under a distinct alias name and UUID."""
    copied = copy.deepcopy(source)
    copied["name"] = alias_name
    copied["mapTo"] = alias_name
    copied["persistentId"] = stable_uuid(level_id, "Material", alias_name)
    return copied


def build_road_materials_json(level_id: str) -> dict[str, Any]:
    """Build the material definitions for roads and markings.

    Emitted to ``levels/<id>/art/road/main.materials.json``.

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
                    # Solid opaque WCA highway asphalt with feathered edges
                    "ambientOcclusionMap": "/assets/materials/tileable/road/road_asphalt_light/t_road_asphalt1_ao.data.dds",
                    "baseColorFactor": [0.837110996, 0.837103009, 0.837103009, 1.0],
                    "baseColorMap": "/assets/materials/decalroad/asphalt/road_rubber_sticky/t_road_asphalt1_b.color.dds",
                    "detailNormalMap": "/assets/materials/tileable/road/road_asphalt/t_asphalt_track_nm.normal.dds",
                    "detailScale": [5, 5],
                    "normalMap": "/assets/materials/tileable/road/road_asphalt_light/t_road_asphalt1_nm.normal.dds",
                    "opacityMap": "/assets/materials/tileable/road/road_asphalt_light/t_road_asphalt2_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/asphalt/m_asphalt_02/t_asphalt1_r.data.dds",
                },
                {
                    "baseColorFactor": None,
                    "detailScale": None,
                },
                {},
                {},
            ],
            "alphaRef": 0,
            "annotation": "ASPHALT",
            "castShadows": False,
            "groundType": "ASPHALT_OLD",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "specularStrength0": "0",
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
                    "ambientOcclusionMap": "/assets/materials/decalroad/concrete/decalroad_concrete/t_highway_concrete_ao.data.dds",
                    "baseColorFactor": [0.85, 0.85, 0.85, 1.0],
                    "baseColorMap": "/assets/materials/decalroad/concrete/decalroad_concrete/t_highway_concrete_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/concrete/decalroad_concrete/t_highway_concrete_nm.normal.dds",
                    "opacityMap": "/assets/materials/tileable/road/road_asphalt_light/t_road_asphalt2_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/concrete/decalroad_concrete/t_highway_concrete_r.data.dds",
                    "detailScale": [4, 4],
                },
            ],
            "alphaRef": 0,
            "annotation": "CONCRETE",
            "castShadows": False,
            "groundType": "CONCRETE",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "specularStrength0": "0",
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
                    "ambientOcclusionMap": "/assets/materials/decalroad/dirt/dirt_road_flows/t_dirt_road_flows_ao.data.dds",
                    "baseColorFactor": [0.83, 0.80, 0.71, 1.0],
                    "baseColorMap": "/assets/materials/decalroad/dirt/dirt_road_flows/t_dirt_road_flows_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/dirt/dirt_road_flows/t_dirt_road_flows_nm.normal.dds",
                    "opacityMap": "/assets/materials/decalroad/dirt/dirt_road_flows/t_dirt_road_flows_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/dirt/dirt_road_flows/t_dirt_road_flows_r.data.dds",
                    "detailScale": [4, 4],
                },
            ],
            "alphaRef": 0,
            "annotation": "DIRT",
            "castShadows": False,
            "groundType": "DIRT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "specularStrength0": "0",
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
                    "ambientOcclusionMap": "/assets/materials/decalroad/dirt/dirt_road_gravels/t_dirt_road_gravels_ao.data.dds",
                    "baseColorFactor": [0.87, 0.84, 0.78, 1.0],
                    "baseColorMap": "/assets/materials/decalroad/dirt/dirt_road_gravels/t_dirt_road_gravels_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/dirt/dirt_road_gravels/t_dirt_road_gravels_nm.normal.dds",
                    "opacityMap": "/assets/materials/decalroad/dirt/dirt_road_flows/t_dirt_road_flows_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/dirt/dirt_road_gravels/t_dirt_road_gravels_r.data.dds",
                    "detailScale": [4, 4],
                },
            ],
            "alphaRef": 0,
            "annotation": "GRAVEL",
            "castShadows": False,
            "groundType": "GRAVEL",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "specularStrength0": "0",
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
                    # Stage 0: solid white line texture (matches ECA line_white exactly)
                    "ambientOcclusionMap": "/assets/materials/decalroad/lines/chalk_marking/t_line_white_ao.data.dds",
                    "baseColorFactor": [0.992156923, 0.992156923, 0.992156923, 0.8],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white/t_line_white_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/lines/line_white/t_line_white_nm.normal.dds",
                    "opacityFactor": 0.699999988,
                    "opacityMap": "/assets/materials/decalroad/lines/line_white/t_line_white_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/lines/chalk_marking/t_line_white_r.data.dds",
                },
                {
                    # Stage 1: detail stripe used by ECA - required for proper mask (alphaRef 255)
                    "baseColorFactor": [0.992156923, 0.992156923, 0.992156923, 0.699999988],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white_discontinue/AsphaltRoad_variation_01_d.dds",
                },
                {},
                {},
            ],
            "alphaRef": 255,
            "annotation": "SOLID_LINE",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "specularStrength0": "0",
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
                    # Stage 0: dashed white line (matches ECA line_white_discontinue exactly)
                    "ambientOcclusionMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_ao.data.dds",
                    "baseColorFactor": [0.992156923, 0.992156923, 0.992156923, 0.8],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_nm.normal.dds",
                    "opacityFactor": 0.699999988,
                    "opacityMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/lines/line_white_discontinue/t_line_white_discontinue_r.data.dds",
                },
                {
                    # Stage 1: detail stripe for dash pattern mask (alphaRef 255)
                    "baseColorFactor": [0.992156923, 0.992156923, 0.992156923, 0.699999988],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white_discontinue/AsphaltRoad_variation_01_d.dds",
                },
                {},
                {},
            ],
            "alphaRef": 255,
            "annotation": "DASHED_LINE",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "specularStrength0": "0",
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
                    "baseColorFactor": [0.941176534, 0.792156935, 0.274509817, 0.9],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white/t_line_white_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/lines/line_white/t_line_white_nm.normal.dds",
                    "opacityFactor": 0.699999988,
                    "opacityMap": "/assets/materials/decalroad/lines/line_white/t_line_white_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/lines/chalk_marking/t_line_white_r.data.dds",
                },
                {
                    "baseColorFactor": [0.992156923, 0.992156923, 0.992156923, 0.699999988],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white_discontinue/AsphaltRoad_variation_01_d.dds",
                },
                {},
                {},
            ],
            "alphaRef": 255,
            "annotation": "SOLID_LINE",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "specularStrength0": "0",
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
                    "baseColorFactor": [0.941176534, 0.792156935, 0.274509817, 0.9],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_b.color.dds",
                    "normalMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_nm.normal.dds",
                    "opacityFactor": 0.699999988,
                    "opacityMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_o.data.dds",
                    "roughnessMap": "/assets/materials/decalroad/lines/line_yellow_double/t_line_yellow_double_r.data.dds",
                },
                {
                    "baseColorFactor": [0.992156923, 0.992156923, 0.992156923, 0.699999988],
                    "baseColorMap": "/assets/materials/decalroad/lines/line_white_discontinue/AsphaltRoad_variation_01_d.dds",
                },
                {},
                {},
            ],
            "alphaRef": 255,
            "annotation": "SOLID_LINE",
            "castShadows": False,
            "groundType": "ASPHALT",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "specularStrength0": "0",
            "translucent": True,
            "translucentZWrite": True,
            "version": 1.5,
        },
    }

    # Add deep-copied aliases matching common game material names
    for alias_name, source_name in (
        ("AsphaltRoad_variation_two_way", "road_asphalt_2lane"),
        ("line_white", "m_line_white"),
        ("line_white_discontinue", "m_line_white_discontinue"),
        ("line_yellow", "m_line_yellow"),
        ("line_yellow_double", "m_line_yellow_double"),
    ):
        materials[alias_name] = _alias_material(materials[source_name], alias_name, level_id)

    return materials


def write_road_materials(level_dir: Path, level_id: str) -> list[Path]:
    """Write road materials to ``art/road/main.materials.json``.

    Args:
        level_dir: Root directory of the level.
        level_id: Level identifier.

    Returns:
        List of written paths.
    """
    materials = build_road_materials_json(level_id)
    text = json.dumps(materials, indent=2, ensure_ascii=False) + "\n"

    road_mat_path = level_dir / "art" / "road" / "main.materials.json"
    road_mat_path.parent.mkdir(parents=True, exist_ok=True)
    road_mat_path.write_text(text, encoding="utf-8")
    return [road_mat_path]
