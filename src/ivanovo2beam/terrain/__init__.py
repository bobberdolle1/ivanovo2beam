"""Terrain acquisition and generation.

Two responsibilities live here:

``dem``
    Acquiring real elevation data (Copernicus DEM GLO-30, with fallbacks) and
    sampling it at arbitrary WGS84 points.

``builder``
    Turning those elevations into a BeamNG-ready heightmap grid, including
    grading smooth, drivable road corridors into the terrain.
"""

from __future__ import annotations

from ivanovo2beam.terrain.builder import (
    DemSampler,
    RoadCorridor,
    TerrainBuild,
    build_terrain,
    derive_road_profile,
    enforce_max_grade,
    resample_polyline,
    sample_dem_onto_grid,
    smooth_profile,
)

__all__ = [
    "DemSampler",
    "RoadCorridor",
    "TerrainBuild",
    "build_terrain",
    "derive_road_profile",
    "enforce_max_grade",
    "resample_polyline",
    "sample_dem_onto_grid",
    "smooth_profile",
]
