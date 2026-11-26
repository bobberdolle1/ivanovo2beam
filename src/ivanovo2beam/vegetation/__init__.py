"""Vegetation and forest generation for BeamNG.drive.

Pipeline:
    osm -> extract.extract_vegetation      extract forests, scrub, meadows from ways & multipolygons
        -> forest.build_keepout_mask       rasterize roads, rail, water, and building clearance
        -> forest.export_vegetation        deterministic PCG64(osm_id) placement
        -> forest.build_managed_item_data_json  emit art/forest/managedItemData.json
        -> forest.build_forest_materials_json   emit art/forest/main.materials.json
        -> forest.<item>.forest4.json      native BeamNG 0.39 high-FPS instancing
"""

from __future__ import annotations

from ivanovo2beam.vegetation.extract import (
    FARMLAND_TAGS,
    FOREST_TAGS,
    MEADOW_TAGS,
    SCRUB_TAGS,
    VegetationArea,
    VegetationStats,
    extract_vegetation,
)
from ivanovo2beam.vegetation.forest import (
    FOREST_SAPLING_DISTRIBUTION,
    FOREST_SPECIES_DISTRIBUTION,
    LINKED_SPECIES_DATA,
    SCRUB_SPECIES_DISTRIBUTION,
    SPECIES_DATA,
    STOCK_TREE_MATERIALS,
    VegetationExportStats,
    build_forest_materials_json,
    build_keepout_mask,
    build_managed_item_data_json,
    discover_beamng_dir,
    export_vegetation,
)
from ivanovo2beam.vegetation.groundcover import (
    EXCLUDED_TERRAIN_LAYERS,
    GROUNDCOVER_MATERIALS,
    LINKED_GROUNDCOVER_SHAPES,
    GroundCoverStats,
    build_groundcover_materials_json,
    build_groundcover_objects,
    export_groundcover,
)
from ivanovo2beam.vegetation.worldcover import (
    WC_TREES,
    WorldCoverGrid,
    fetch_and_cache_worldcover,
    load_worldcover_cache,
    worldcover_tile_name,
    worldcover_to_forest_polygons,
)

__all__ = [
    "EXCLUDED_TERRAIN_LAYERS",
    "FARMLAND_TAGS",
    "FOREST_SAPLING_DISTRIBUTION",
    "FOREST_SPECIES_DISTRIBUTION",
    "FOREST_TAGS",
    "GROUNDCOVER_MATERIALS",
    "GroundCoverStats",
    "LINKED_GROUNDCOVER_SHAPES",
    "LINKED_SPECIES_DATA",
    "MEADOW_TAGS",
    "SCRUB_SPECIES_DISTRIBUTION",
    "SCRUB_TAGS",
    "SPECIES_DATA",
    "STOCK_TREE_MATERIALS",
    "WC_TREES",
    "VegetationArea",
    "VegetationExportStats",
    "VegetationStats",
    "WorldCoverGrid",
    "build_forest_materials_json",
    "build_groundcover_materials_json",
    "build_groundcover_objects",
    "build_keepout_mask",
    "build_managed_item_data_json",
    "discover_beamng_dir",
    "export_groundcover",
    "export_vegetation",
    "extract_vegetation",
    "fetch_and_cache_worldcover",
    "load_worldcover_cache",
    "worldcover_tile_name",
    "worldcover_to_forest_polygons",
]
