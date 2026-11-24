"""Hydrology, river profiling, and water body extraction for BeamNG levels."""

from ivanovo2beam.water.export import (
    DEFAULT_DEPTH_GRADIENT_TEX,
    DEFAULT_FOAM_TEX,
    DEFAULT_RIPPLE_TEX,
    WATER_GROUP,
    build_river_object,
    build_water_block_object,
    export_water_to_scene,
)
from ivanovo2beam.water.extract import (
    DEFAULT_LAKE_DEPTH_M,
    WATERWAY_SPECS,
    RiverChannel,
    WaterBody,
    WaterNetwork,
    compute_downstream_water_profile,
    compute_lake_water_elevation,
    extract_water,
)

__all__ = [
    "DEFAULT_DEPTH_GRADIENT_TEX",
    "DEFAULT_FOAM_TEX",
    "DEFAULT_LAKE_DEPTH_M",
    "DEFAULT_RIPPLE_TEX",
    "WATERWAY_SPECS",
    "WATER_GROUP",
    "RiverChannel",
    "WaterBody",
    "WaterNetwork",
    "build_river_object",
    "build_water_block_object",
    "compute_downstream_water_profile",
    "compute_lake_water_elevation",
    "export_water_to_scene",
    "extract_water",
]
