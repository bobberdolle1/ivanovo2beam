"""Vegetation and forest area extraction from OpenStreetMap.

Extracts woodland, scrub, and meadow polygons from OSM closed ways and
multipolygon relations (with inner clearing cutouts) for Central Russian
ecosystems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import linemerge, polygonize
from shapely.validation import make_valid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

    from ivanovo2beam.gis.projection import RegionProjection
    from ivanovo2beam.osm.types import OsmData, OsmRelation, OsmWay

#: Tag values for woodland / dense tree canopy
FOREST_TAGS = frozenset({"wood", "forest"})

#: Tag values for bushes / undergrowth
SCRUB_TAGS = frozenset({"scrub", "tree_row"})

#: Tag values for grassy meadows / clearings
MEADOW_TAGS = frozenset({"meadow", "grass", "allotments", "orchard", "village_green"})


@dataclass(slots=True)
class VegetationArea:
    """One contiguous vegetation polygon in world metres.

    Attributes:
        osm_id: Source OSM way or relation id.
        ring: ``(n, 2)`` closed exterior ring of world coordinates (m).
        kind: ``"forest"``, ``"scrub"``, or ``"meadow"``.
        holes: Optional tuple of closed ``(m, 2)`` inner clearing rings.
        area_m2: Net area in square metres (excluding clearings).
        leaf_type: ``"broadleaved"``, ``"needleleaved"``, ``"mixed"``, or None.
        name: Area or forest name if tagged.
    """

    osm_id: int
    ring: NDArray[np.float64]
    kind: str
    holes: tuple[NDArray[np.float64], ...] = ()
    area_m2: float = 0.0
    leaf_type: str | None = None
    name: str = ""

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Return (min_x, min_y, max_x, max_y) bounding box."""
        xs = self.ring[:, 0]
        ys = self.ring[:, 1]
        return (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))


@dataclass(slots=True)
class VegetationStats:
    """Diagnostics from vegetation extraction."""

    areas: int = 0
    relations: int = 0
    ways: int = 0
    forest_areas: int = 0
    scrub_areas: int = 0
    meadow_areas: int = 0
    skipped_area: int = 0
    skipped_invalid: int = 0
    total_area_m2: float = 0.0
    by_kind: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary."""
        return {
            "areas": self.areas,
            "forest_areas": self.forest_areas,
            "scrub_areas": self.scrub_areas,
            "meadow_areas": self.meadow_areas,
            "relations": self.relations,
            "ways": self.ways,
            "total_area_km2": round(self.total_area_m2 / 1e6, 4),
            "by_kind": dict(sorted(self.by_kind.items(), key=lambda kv: -kv[1])),
        }


def _classify_vegetation(tags: dict[str, str]) -> str | None:
    """Classify OSM tags into 'forest', 'scrub', or 'meadow'."""
    nat = tags.get("natural", "").strip().lower()
    lu = tags.get("landuse", "").strip().lower()

    if nat in FOREST_TAGS or lu in FOREST_TAGS:
        return "forest"
    if nat in SCRUB_TAGS or lu in SCRUB_TAGS:
        return "scrub"
    if nat in MEADOW_TAGS or lu in MEADOW_TAGS:
        return "meadow"
    return None


def _clean_ring(
    points: NDArray[np.float64], tolerance_m: float
) -> NDArray[np.float64] | None:
    """Validate, close, and simplify a ring."""
    if points.shape[0] < 3:
        return None
    closed = points
    if not np.array_equal(points[0], points[-1]):
        closed = np.vstack((points, points[0]))
    if closed.shape[0] < 4:
        return None

    poly = Polygon(closed)
    if not poly.is_valid:
        poly = make_valid(poly)
        if isinstance(poly, MultiPolygon):
            poly = max(poly.geoms, key=lambda g: g.area)
        if not isinstance(poly, Polygon) or poly.is_empty:
            return None

    if poly.area <= 0:
        return None

    if tolerance_m > 0:
        poly = poly.simplify(tolerance_m, preserve_topology=True)
        if not isinstance(poly, Polygon) or poly.area <= 0:
            return None

    coords = np.asarray(poly.exterior.coords, dtype=np.float64)
    if coords.shape[0] < 4:
        return None

    # Ensure counter-clockwise exterior
    signed_area = float(0.5 * np.sum(coords[:-1, 0] * coords[1:, 1] - coords[1:, 0] * coords[:-1, 1]))
    if signed_area < 0:
        coords = coords[::-1].copy()
    return coords


def _assemble_vegetation_multipolygon(
    relation: OsmRelation,
    osm: OsmData,
    projection: RegionProjection,
    simplify_tolerance_m: float,
    ways_by_id: dict[int, OsmWay],
) -> list[Polygon]:
    """Assemble multipolygon relation into outer shells with inner clearing holes."""
    outers: list[LineString] = []
    inners: list[LineString] = []

    for mtype, ref, role in relation.members:
        if mtype != "way":
            continue
        way = ways_by_id.get(ref)
        if way is None:
            continue
        coords = osm.way_coords(way)
        if len(coords) < 2:
            continue
        lons = np.asarray([c[0] for c in coords], dtype=np.float64)
        lats = np.asarray([c[1] for c in coords], dtype=np.float64)
        xs, ys = projection.geo_to_world_array(lons, lats)
        line = LineString(np.column_stack((xs, ys)))
        if not line.is_empty and line.length > 0:
            if role in {"outer", ""}:
                outers.append(line)
            elif role == "inner":
                inners.append(line)

    if not outers:
        return []

    try:
        merged_outer = linemerge(outers)
        outer_polys = list(polygonize(merged_outer))
    except Exception:
        outer_polys = []

    if not outer_polys:
        for line in outers:
            if line.is_ring and len(line.coords) >= 4:
                p = Polygon(line)
                if p.is_valid and p.area > 0:
                    outer_polys.append(p)

    if not outer_polys:
        return []

    inner_polys: list[Polygon] = []
    if inners:
        try:
            merged_inner = linemerge(inners)
            inner_polys = list(polygonize(merged_inner))
        except Exception:
            pass
        if not inner_polys:
            for line in inners:
                if line.is_ring and len(line.coords) >= 4:
                    p = Polygon(line)
                    if p.is_valid and p.area > 0:
                        inner_polys.append(p)

    assembled: list[Polygon] = []
    for outer_poly in outer_polys:
        if not outer_poly.is_valid or outer_poly.area <= 0:
            continue
        holes = []
        for inner in inner_polys:
            if outer_poly.contains(inner.representative_point()):
                holes.append(inner.exterior.coords)
        try:
            poly = Polygon(outer_poly.exterior.coords, holes)
            if not poly.is_valid:
                poly = make_valid(poly)
            if isinstance(poly, MultiPolygon):
                assembled.extend([p for p in poly.geoms if p.area > 0])
            elif isinstance(poly, Polygon) and poly.area > 0:
                assembled.append(poly)
        except Exception:
            assembled.append(outer_poly)

    if simplify_tolerance_m > 0:
        simplified: list[Polygon] = []
        for poly in assembled:
            simp = poly.simplify(simplify_tolerance_m, preserve_topology=True)
            if isinstance(simp, Polygon) and simp.area > 0:
                simplified.append(simp)
            elif isinstance(simp, MultiPolygon):
                simplified.extend([p for p in simp.geoms if p.area > 0])
        assembled = simplified

    return assembled


def extract_vegetation(
    osm: OsmData,
    projection: RegionProjection,
    *,
    clip_bounds: tuple[float, float, float, float] | None = None,
    min_area_m2: float = 100.0,
    simplify_tolerance_m: float = 2.0,
) -> tuple[list[VegetationArea], VegetationStats]:
    """Extract vegetation areas (forests, scrub, meadows) from OSM data.

    Supports both closed ways and multipolygon relations with inner clearings.
    """
    stats = VegetationStats()
    areas: list[VegetationArea] = []
    ways_by_id: dict[int, OsmWay] = {w.id: w for w in osm.ways}
    relation_way_ids: set[int] = set()

    # 1. Process multipolygon relations
    for relation in osm.relations:
        if relation.tags.get("type") != "multipolygon":
            continue
        kind = _classify_vegetation(dict(relation.tags))
        if kind is None:
            continue

        polys = _assemble_vegetation_multipolygon(
            relation, osm, projection, simplify_tolerance_m, ways_by_id
        )
        if not polys:
            continue

        for _, ref, _ in relation.members:
            relation_way_ids.add(ref)

        leaf_type = relation.tags.get("leaf_type")
        name = relation.tags.get("name", "")

        for p_idx, poly in enumerate(polys):
            area_m2 = float(poly.area)
            if area_m2 < min_area_m2:
                stats.skipped_area += 1
                continue

            ring = np.asarray(poly.exterior.coords, dtype=np.float64)
            # Ensure counter-clockwise exterior
            signed_a = float(0.5 * np.sum(ring[:-1, 0] * ring[1:, 1] - ring[1:, 0] * ring[:-1, 1]))
            if signed_a < 0:
                ring = ring[::-1].copy()

            holes = tuple(np.asarray(h.coords, dtype=np.float64) for h in poly.interiors)

            if clip_bounds is not None:
                x0, y0, x1, y1 = clip_bounds
                cx = float(ring[:, 0].mean())
                cy = float(ring[:, 1].mean())
                if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                    stats.skipped_invalid += 1
                    continue

            v_area = VegetationArea(
                osm_id=-relation.id if p_idx == 0 else -abs(relation.id * 1000 + p_idx),
                ring=ring,
                kind=kind,
                holes=holes,
                area_m2=area_m2,
                leaf_type=leaf_type,
                name=name,
            )
            areas.append(v_area)
            stats.relations += 1

    # 2. Process closed ways
    for way in osm.ways:
        if not way.is_closed or way.id in relation_way_ids:
            continue
        kind = _classify_vegetation(dict(way.tags))
        if kind is None:
            continue

        coords = osm.way_coords(way)
        if len(coords) < 3:
            continue
        lons = np.asarray([c[0] for c in coords], dtype=np.float64)
        lats = np.asarray([c[1] for c in coords], dtype=np.float64)
        xs, ys = projection.geo_to_world_array(lons, lats)
        ring = _clean_ring(np.column_stack((xs, ys)), simplify_tolerance_m)
        if ring is None:
            stats.skipped_invalid += 1
            continue

        area_m2 = float(Polygon(ring).area)
        if area_m2 < min_area_m2:
            stats.skipped_area += 1
            continue

        if clip_bounds is not None:
            x0, y0, x1, y1 = clip_bounds
            cx = float(ring[:, 0].mean())
            cy = float(ring[:, 1].mean())
            if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                stats.skipped_invalid += 1
                continue

        v_area = VegetationArea(
            osm_id=way.id,
            ring=ring,
            kind=kind,
            holes=(),
            area_m2=area_m2,
            leaf_type=way.tags.get("leaf_type"),
            name=way.tags.get("name", ""),
        )
        areas.append(v_area)
        stats.ways += 1

    for a in areas:
        stats.by_kind[a.kind] = stats.by_kind.get(a.kind, 0) + 1
        stats.total_area_m2 += a.area_m2
        if a.kind == "forest":
            stats.forest_areas += 1
        elif a.kind == "scrub":
            stats.scrub_areas += 1
        elif a.kind == "meadow":
            stats.meadow_areas += 1

    stats.areas = len(areas)
    return areas, stats
