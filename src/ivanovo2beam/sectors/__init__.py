"""Sector-based spatial indexing, multi-TerrainBlock, and hybrid terrain support."""

from __future__ import annotations

from ivanovo2beam.sectors.grid import SectorBounds, SectorGrid, SectorId
from ivanovo2beam.sectors.hybrid import (
    CorridorTileMesh,
    HybridBuildResult,
    HybridSpec,
    HybridSurfaceIndex,
    apply_hybrid_decal_projection,
    build_corridor_rtin_tile,
    build_hybrid_level,
    depress_base_terrain,
    rtin_errors,
    rtin_triangles,
    validate_hybrid_level,
    write_corridor_materials,
    write_corridor_tile_dae,
)
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
    "CorridorTileMesh",
    "HybridBuildResult",
    "HybridSpec",
    "HybridSurfaceIndex",
    "MultiBlockBuildResult",
    "MultiBlockSpec",
    "SeamReport",
    "SectorBounds",
    "SectorGrid",
    "SectorId",
    "TerrainTileSlice",
    "apply_hybrid_decal_projection",
    "build_corridor_rtin_tile",
    "build_hybrid_level",
    "build_multiblock_level",
    "depress_base_terrain",
    "plan_tile_layout",
    "rtin_errors",
    "rtin_triangles",
    "slice_terrain_tiles",
    "split_polyline_at_seams",
    "validate_hybrid_level",
    "validate_multiblock_level",
    "verify_tile_seams",
    "write_corridor_materials",
    "write_corridor_tile_dae",
]
