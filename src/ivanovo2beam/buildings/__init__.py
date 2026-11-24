"""Building generation from OpenStreetMap footprints.

Pipeline::

    osm -> footprints.extract_buildings   classify, simplify, height
        -> export.choose_prototypes       a few archetypes per class
        -> mesh.extrude_footprint         walls + roof
        -> mesh.write_dae                 COLLADA 1.4.1
        -> export.export_buildings        TSStatic instances in the scene

BeamNG 0.39 loads plain ``.dae`` directly, so no compiled mesh format is
needed. Only the subset of COLLADA the engine reads is emitted.
"""

from __future__ import annotations

from ivanovo2beam.buildings.export import (
    ROOF_BY_CLASS,
    UNIQUE_AREA_M2,
    VARIANTS_PER_CLASS,
    Archetype,
    BuildingExportStats,
    build_archetypes,
    choose_prototypes,
    export_buildings,
)
from ivanovo2beam.buildings.footprints import (
    BUILDING_CLASSES,
    BuildingClass,
    BuildingFootprint,
    FootprintStats,
    building_class_for,
    ensure_counter_clockwise,
    extract_buildings,
    parse_height_m,
    parse_levels,
)
from ivanovo2beam.buildings.mesh import (
    Mesh,
    extrude_footprint,
    mesh_to_dae_xml,
    roof_height_for,
    write_dae,
)

__all__ = [
    "BUILDING_CLASSES",
    "ROOF_BY_CLASS",
    "UNIQUE_AREA_M2",
    "VARIANTS_PER_CLASS",
    "Archetype",
    "BuildingClass",
    "BuildingExportStats",
    "BuildingFootprint",
    "FootprintStats",
    "Mesh",
    "build_archetypes",
    "building_class_for",
    "choose_prototypes",
    "ensure_counter_clockwise",
    "export_buildings",
    "extract_buildings",
    "extrude_footprint",
    "mesh_to_dae_xml",
    "parse_height_m",
    "parse_levels",
    "roof_height_for",
    "write_dae",
]
