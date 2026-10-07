"""Merging several OSM extracts into one dataset.

Why this exists
---------------
Overpass is a shared, rate-limited public service. Requesting every feature
class for a whole region in one query reliably times out: a 5 x 5 km full-extract
query against ``overpass-api.de`` returns HTTP 504, while the same query for a
1.1 x 1.1 km tile returns in under a second.

The pipeline therefore fetches the region as a grid of small tiles and merges
the results here. Overpass returns each way whole (its ``>`` recursion pulls in
every referenced node) and a way is returned by *every* tile it touches, so
merging by element id is both correct and idempotent: the same way appears once,
with full geometry, regardless of how many tiles reported it.

Merge semantics
---------------
* Nodes are keyed by id; the first value wins. Coordinates for a given OSM node
  are immutable, so a disagreement means corrupt input, and that is reported.
* Node tags are merged per node id.
* Ways and relations are keyed by id. Duplicates must be identical; a conflict
  is reported rather than silently resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ivanovo2beam.osm.types import OsmData, OsmWay

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence


@dataclass(slots=True)
class MergeReport:
    """Diagnostics from merging OSM extracts.

    Attributes:
        sources: Number of extracts merged.
        nodes: Total distinct nodes in the result.
        node_conflicts: Node ids whose coordinates disagreed between sources.
        ways: Total distinct ways in the result.
        duplicate_ways: Ways seen in more than one extract.
        way_conflicts: Way ids whose content disagreed between sources.
        relations: Total distinct relations.
        duplicate_relations: Relations seen in more than one extract.
    """

    sources: int = 0
    nodes: int = 0
    node_conflicts: list[int] = field(default_factory=list)
    ways: int = 0
    duplicate_ways: int = 0
    way_conflicts: list[int] = field(default_factory=list)
    relations: int = 0
    duplicate_relations: int = 0

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable summary."""
        return {
            "sources": self.sources,
            "nodes": self.nodes,
            "node_conflicts": len(self.node_conflicts),
            "ways": self.ways,
            "duplicate_ways": self.duplicate_ways,
            "way_conflicts": len(self.way_conflicts),
            "relations": self.relations,
            "duplicate_relations": self.duplicate_relations,
        }

    def describe(self) -> str:
        """Return a one-line human summary."""
        return (
            f"merged {self.sources} extract(s): {self.ways} ways "
            f"({self.duplicate_ways} duplicated across tiles), {self.nodes} nodes, "
            f"{self.relations} relations, {len(self.node_conflicts)} node conflicts, "
            f"{len(self.way_conflicts)} way conflicts"
        )


def merge_osm_data(
    extracts: Sequence[OsmData],
    *,
    strict: bool = False,
) -> tuple[OsmData, MergeReport]:
    """Merge multiple OSM extracts into one dataset.

    Args:
        extracts: Extracts to merge. Order does not affect the result except
            that earlier extracts win on conflict.
        strict: When ``True``, raise :class:`ValueError` on any conflicting
            element instead of recording it in the report.

    Returns:
        ``(merged, report)``.

    Raises:
        ValueError: If ``extracts`` is empty, or if ``strict`` and a conflict
            is found.
    """
    if not extracts:
        raise ValueError("nothing to merge: extracts is empty")

    report = MergeReport(sources=len(extracts))
    nodes: dict[int, tuple[float, float]] = {}
    node_tags: dict[int, dict[str, str]] = {}
    ways: dict[int, OsmWay] = {}
    relations: dict[int, object] = {}
    node_source: dict[int, str] = {}
    way_source: dict[int, str] = {}

    for extract in extracts:
        for node_id, coord in extract.nodes.items():
            if node_id in nodes:
                existing = nodes[node_id]
                if existing != coord:
                    report.node_conflicts.append(node_id)
                    if strict:
                        raise ValueError(f"node {node_id} has conflicting coordinates: {existing} vs {coord}")
                continue
            nodes[node_id] = coord
            node_source[node_id] = extract.source
        for node_id, tags in extract.node_tags.items():
            node_tags.setdefault(node_id, {}).update(tags)
        for way in extract.ways:
            if way.id in ways:
                report.duplicate_ways += 1
                existing = ways[way.id]
                if existing.nodes != way.nodes or dict(existing.tags) != dict(way.tags):
                    report.way_conflicts.append(way.id)
                    if strict:
                        raise ValueError(
                            f"way {way.id} differs between extracts "
                            f"({way_source[way.id]} and {extract.source})"
                        )
                continue
            ways[way.id] = way
            way_source[way.id] = extract.source
        for relation in extract.relations:
            if relation.id in relations:
                report.duplicate_relations += 1
                continue
            relations[relation.id] = relation

    report.nodes = len(nodes)
    report.ways = len(ways)
    report.relations = len(relations)

    merged = OsmData(
        nodes=nodes,
        node_tags=node_tags,
        ways=sorted(ways.values(), key=lambda w: w.id),
        relations=sorted(relations.values(), key=lambda r: r.id),
        source=f"merged({report.sources})",
    )
    return merged, report


def osm_bbox_tiles(
    west: float,
    south: float,
    east: float,
    north: float,
    *,
    tile_km: float = 1.5,
) -> list[tuple[float, float, float, float]]:
    """Split a geographic box into tiles small enough for Overpass.

    A 1.5 km tile is the size that was measured to return in well under a second
    from ``overpass-api.de`` for a dense city centre; larger requests queue and
    eventually time out.

    Args:
        west: Western edge in degrees.
        south: Southern edge in degrees.
        east: Eastern edge in degrees.
        north: Northern edge in degrees.
        tile_km: Approximate tile size in kilometres.

    Returns:
        A list of ``(west, south, east, north)`` tiles covering the box. Tiles
        overlap slightly so that ways crossing a boundary are returned intact by
        at least one of them.

    Raises:
        ValueError: If the box is inverted or ``tile_km`` is not positive.
    """
    import math

    if east < west or north < south:
        raise ValueError(f"inverted box: west={west} south={south} east={east} north={north}")
    if tile_km <= 0:
        raise ValueError(f"tile_km must be positive, got {tile_km}")

    mid_lat = 0.5 * (south + north)
    km_per_deg_lat = 111.132
    km_per_deg_lon = 111.320 * max(1e-6, math.cos(math.radians(mid_lat)))
    step_lat = tile_km / km_per_deg_lat
    step_lon = tile_km / km_per_deg_lon

    def _axis(start: float, end: float, step: float) -> list[tuple[float, float]]:
        spans: list[tuple[float, float]] = []
        cursor = start
        # A little overlap keeps boundary-crossing ways whole in one tile.
        overlap = step * 0.05
        while cursor < end:
            nxt = min(cursor + step, end)
            spans.append((cursor, nxt))
            if nxt >= end:
                break
            cursor = nxt - overlap
        return spans

    tiles: list[tuple[float, float, float, float]] = []
    for lat0, lat1 in _axis(south, north, step_lat):
        for lon0, lon1 in _axis(west, east, step_lon):
            tiles.append((lon0, lat0, lon1, lat1))
    return tiles


def summarise_extracts(extracts: Iterable[OsmData]) -> dict[str, int]:
    """Return total element counts across extracts, before merging."""
    ways = 0
    nodes = 0
    relations = 0
    for extract in extracts:
        ways += len(extract.ways)
        nodes += len(extract.nodes)
        relations += len(extract.relations)
    return {"extracts": len(list(extracts)), "ways": ways, "nodes": nodes, "relations": relations}
