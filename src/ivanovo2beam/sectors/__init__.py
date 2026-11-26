"""Sector-based spatial indexing and multi-TerrainBlock experiment support."""

from __future__ import annotations

from ivanovo2beam.sectors.grid import SectorBounds, SectorGrid, SectorId
from ivanovo2beam.sectors.multiblock import (
    MultiBlockBuildResult,
    MultiBlockSpec,
    SeamReport,
    TerrainTileSlice,
    build_multiblock_level,
    plan_tile_layout,
    slice_terrain_tiles,
    split_polyline_at_seams,
    validate_multiblock_level,
    verify_tile_seams,
)

__all__ = [
    "MultiBlockBuildResult",
    "MultiBlockSpec",
    "SeamReport",
    "SectorBounds",
    "SectorGrid",
    "SectorId",
    "TerrainTileSlice",
    "build_multiblock_level",
    "plan_tile_layout",
    "slice_terrain_tiles",
    "split_polyline_at_seams",
    "validate_multiblock_level",
    "verify_tile_seams",
]
