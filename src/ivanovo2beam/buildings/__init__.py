"""Building generation from OpenStreetMap footprints.

Pipeline:
    osm -> footprints.extract_buildings   classify, simplify, height, parts, multipolygons
        -> export.export_buildings        spatial chunk batching or archetype instances
        -> mesh.extrude_footprint         MeshBuilder, slope skirts, multi-materials
        -> mesh.write_dae                 COLLADA 1.4.1
        -> export.write_building_materials main.materials.json for native BeamNG PBR
"""

from __future__ import annotations

from ivanovo2beam.buildings.export import (
    BUILDING_MATERIALS,
    DEFAULT_CHUNK_SIZE_M,
    ROOF_BY_CLASS,
    UNIQUE_AREA_M2,
    VARIANTS_PER_CLASS,
    Archetype,
    BuildingExportStats,
    build_archetypes,
    build_building_materials_json,
    choose_prototypes,
    export_buildings,
    resolve_building_materials,
    write_building_materials,
)
from ivanovo2beam.buildings.footprints import (
    BUILDING_CLASSES,
    BuildingClass,
    BuildingFootprint,
    FootprintStats,
    building_class_for,
    default_archetypes,
    ensure_counter_clockwise,
    extract_buildings,
    footprint_ring_area,
    parse_height_m,
    parse_levels,
    parse_min_height_m,
    parse_min_level,
)
from ivanovo2beam.buildings.mesh import (
    Mesh,
    MeshBuilder,
    extrude_footprint,
    mesh_to_dae_xml,
    roof_height_for,
    write_dae,
)

__all__ = [
    "BUILDING_CLASSES",
    "BUILDING_MATERIALS",
    "DEFAULT_CHUNK_SIZE_M",
    "ROOF_BY_CLASS",
    "UNIQUE_AREA_M2",
    "VARIANTS_PER_CLASS",
    "Archetype",
    "BuildingClass",
    "BuildingExportStats",
    "BuildingFootprint",
    "FootprintStats",
    "Mesh",
    "MeshBuilder",
    "build_archetypes",
    "build_building_materials_json",
    "building_class_for",
    "choose_prototypes",
    "default_archetypes",
    "ensure_counter_clockwise",
    "export_buildings",
    "extract_buildings",
    "extrude_footprint",
    "footprint_ring_area",
    "mesh_to_dae_xml",
    "parse_height_m",
    "parse_levels",
    "parse_min_height_m",
    "parse_min_level",
    "resolve_building_materials",
    "roof_height_for",
    "write_building_materials",
    "write_dae",
]
