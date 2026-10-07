"""Bridge between the extracted road network and BeamNG level data."""

from __future__ import annotations

from ivanovo2beam.roads.network import (
    DECAL_MATERIALS,
    DEFAULT_LANE_WIDTH_M,
    Road,
    decal_material_for,
    densify,
    extract_roads,
    is_truthy,
    parse_lanes,
    parse_maxspeed_kph,
    parse_width,
    road_network_stats,
    road_width_m,
    surface_to_material_key,
    total_network_length_km,
)

__all__ = [
    "DECAL_MATERIALS",
    "DEFAULT_LANE_WIDTH_M",
    "Road",
    "decal_material_for",
    "densify",
    "extract_roads",
    "is_truthy",
    "parse_lanes",
    "parse_maxspeed_kph",
    "parse_width",
    "road_network_stats",
    "road_width_m",
    "surface_to_material_key",
    "total_network_length_km",
]
