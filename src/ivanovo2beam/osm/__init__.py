"""OpenStreetMap acquisition and parsing.

This package is the first stage of the pipeline: it turns a geographic bounding
box into an in-memory extract of OSM elements.

* :func:`~ivanovo2beam.osm.fetch.fetch_overpass` -- download (and cache) an
  Overpass extract for a bounding box.
* :func:`~ivanovo2beam.osm.fetch.default_query` -- the Overpass QL for the
  feature classes the map generator needs.
* :func:`~ivanovo2beam.osm.parse.parse_osm` -- stream an OSM XML file into
  plain-data structures without building a DOM.
* :mod:`ivanovo2beam.osm.types` -- ``OsmNode``/``OsmWay``/``OsmRelation`` and
  the :class:`~ivanovo2beam.osm.types.OsmData` container they are parsed into.

Typical use::

    from ivanovo2beam.gis.projection import BBox
    from ivanovo2beam.osm import fetch_overpass, parse_osm

    bbox = BBox(west=40.96, south=56.99, east=40.98, north=57.01)
    data = parse_osm(fetch_overpass(bbox, cache_dir=Path("cache/osm")))

Nothing in this package needs a dependency beyond the standard library; the
projection type it consumes comes from :mod:`ivanovo2beam.gis`.
"""

from __future__ import annotations

from ivanovo2beam.osm.fetch import (
    DEFAULT_ENDPOINTS,
    DEFAULT_FEATURE_ORDER,
    FEATURE_CLASSES,
    cache_key,
    cache_paths,
    default_query,
    fetch_overpass,
    read_cache_meta,
)
from ivanovo2beam.osm.parse import parse_osm, parse_osm_pbf
from ivanovo2beam.osm.types import (
    OsmData,
    OsmError,
    OsmFetchError,
    OsmNode,
    OsmParseError,
    OsmRelation,
    OsmWay,
    OverpassError,
)

__all__ = [
    "DEFAULT_ENDPOINTS",
    "DEFAULT_FEATURE_ORDER",
    "FEATURE_CLASSES",
    "OsmData",
    "OsmError",
    "OsmFetchError",
    "OsmNode",
    "OsmParseError",
    "OsmRelation",
    "OsmWay",
    "OverpassError",
    "cache_key",
    "cache_paths",
    "default_query",
    "fetch_overpass",
    "parse_osm",
    "parse_osm_pbf",
    "read_cache_meta",
]
