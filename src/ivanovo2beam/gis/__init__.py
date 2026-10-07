"""GIS core: projections, geodesy and metric world coordinates."""

from __future__ import annotations

from ivanovo2beam.gis.projection import (
    BBox,
    RegionProjection,
    ScaleErrorReport,
    make_projection,
)

__all__ = ["BBox", "RegionProjection", "ScaleErrorReport", "make_projection"]
