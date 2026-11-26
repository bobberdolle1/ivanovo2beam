"""Vegetation and forest area extraction from OpenStreetMap.

Extracts woodland, scrub, and meadow polygons from OSM closed ways and
multipolygon relations (with inner clearing cutouts) for Central Russian
ecosystems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import linemerge, polygonize, unary_union
from shapely.validation import make_valid

from ivanovo2beam.vegetation.worldcover import (
    load_worldcover_cache,
    worldcover_to_forest_polygons,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from numpy.typing import NDArray

    from ivanovo2beam.gis.projection import RegionProjection
    from ivanovo2beam.osm.types import OsmData, OsmRelation, OsmWay

#: Tag values for woodland / dense tree canopy
FOREST_TAGS = frozenset({"wood", "forest"})

#: Tag values for bushes / undergrowth
SCRUB_TAGS = frozenset({"scrub", "tree_row", "heath"})

#: Tag values for grassy meadows / clearings
MEADOW_TAGS = frozenset(
    {"meadow", "grass", "allotments", "orchard", "village_green", "greenfield", "recreation_ground"}
)

#: Tag values for arable farmland / pastures
FARMLAND_TAGS = frozenset({"farmland", "farmyard"})


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

    def to_polygon(self) -> Polygon:
        """Return a Shapely ``Polygon`` preserving exterior ring and inner holes."""
        return Polygon(self.ring, self.holes)


@dataclass(slots=True)
class VegetationStats:
    """Diagnostics from vegetation extraction."""

    areas: int = 0
    relations: int = 0
    ways: int = 0
    forest_areas: int = 0
    scrub_areas: int = 0
    meadow_areas: int = 0
    farmland_areas: int = 0
    worldcover_areas: int = 0
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
            "farmland_areas": self.farmland_areas,
            "worldcover_areas": self.worldcover_areas,
            "relations": self.relations,
            "ways": self.ways,
            "total_area_km2": round(self.total_area_m2 / 1e6, 4),
            "by_kind": dict(sorted(self.by_kind.items(), key=lambda kv: -kv[1])),
        }


def _infer_leaf_type(tags: dict[str, str]) -> str | None:
    """Infer 'broadleaved', 'needleleaved', or 'mixed' from OSM vegetation tags."""
    lt = tags.get("leaf_type", "").strip().lower()
    if lt in {"broadleaved", "needleleaved", "mixed"}:
        return lt
    wood = tags.get("wood", "").strip().lower()
    if wood in {"coniferous", "conifers"}:
        return "needleleaved"
    if wood in {"deciduous", "broadleaved"}:
        return "broadleaved"
    if wood == "mixed":
        return "mixed"
    cycle = tags.get("leaf_cycle", "").strip().lower()
    if cycle == "evergreen":
        return "needleleaved"
    if cycle == "deciduous":
        return "broadleaved"
    return None


def _classify_vegetation(tags: dict[str, str]) -> str | None:
    """Classify OSM tags into 'forest', 'scrub', 'meadow', or 'farmland'."""
    nat = tags.get("natural", "").strip().lower()
    lu = tags.get("landuse", "").strip().lower()

    if nat in FOREST_TAGS or lu in FOREST_TAGS:
        return "forest"
    if nat in SCRUB_TAGS or lu in SCRUB_TAGS:
        return "scrub"
    if nat in MEADOW_TAGS or lu in MEADOW_TAGS:
        return "meadow"
    if lu in FARMLAND_TAGS:
        return "farmland"
    return None


def _orient_ring(coords: NDArray[np.float64], *, ccw: bool) -> NDArray[np.float64]:
    """Return ring coordinates oriented counter-clockwise (ccw=True) or clockwise (ccw=False)."""
    if coords.shape[0] < 4:
        return coords
    signed_area = float(
        0.5 * np.sum(coords[:-1, 0] * coords[1:, 1] - coords[1:, 0] * coords[:-1, 1])
    )
    if (ccw and signed_area < 0) or (not ccw and signed_area > 0):
        return coords[::-1].copy()
    return coords


def _extract_polygons(geom: Any) -> list[Polygon]:
    """Extract all non-empty valid Polygon parts from any Shapely geometry."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 0 else []
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty and g.area > 0]
    if hasattr(geom, "geoms"):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_extract_polygons(g))
        return out
    return []


def _polygon_to_rings(
    poly: Polygon,
) -> tuple[NDArray[np.float64], tuple[NDArray[np.float64], ...]] | None:
    """Convert a Shapely Polygon into CCW exterior ring and CW hole rings."""
    ext = _orient_ring(np.asarray(poly.exterior.coords, dtype=np.float64), ccw=True)
    if ext.shape[0] < 4:
        return None
    holes: list[NDArray[np.float64]] = []
    for interior in poly.interiors:
        h = _orient_ring(np.asarray(interior.coords, dtype=np.float64), ccw=False)
        if h.shape[0] >= 4:
            holes.append(h)
    return ext, tuple(holes)


def _clean_polygon(points: NDArray[np.float64], tolerance_m: float) -> list[Polygon]:
    """Validate, close, and simplify a ring into one or more valid Polygons."""
    if points.shape[0] < 3:
        return []
    closed = points
    if not np.array_equal(points[0], points[-1]):
        closed = np.vstack((points, points[0]))
    if closed.shape[0] < 4:
        return []

    geom: Any = Polygon(closed)
    if not geom.is_valid:
        geom = make_valid(geom)

    polys = _extract_polygons(geom)
    if not polys:
        return []

    if tolerance_m > 0:
        simplified: list[Polygon] = []
        for poly in polys:
            simp = poly.simplify(tolerance_m, preserve_topology=True)
            if not simp.is_valid:
                simp = make_valid(simp)
            simplified.extend(_extract_polygons(simp))
        polys = simplified

    return polys


def _clean_ring(
    points: NDArray[np.float64], tolerance_m: float
) -> NDArray[np.float64] | None:
    """Validate, close, and simplify a ring (returning largest polygon exterior)."""
    polys = _clean_polygon(points, tolerance_m)
    if not polys:
        return None
    poly = max(polys, key=lambda g: g.area)
    coords = _orient_ring(np.asarray(poly.exterior.coords, dtype=np.float64), ccw=True)
    return coords if coords.shape[0] >= 4 else None


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

    outer_polys: list[Polygon] = []
    try:
        merged_outer = linemerge(outers)
        outer_polys = [p for p in polygonize(merged_outer) if p.area > 0]
    except Exception:
        outer_polys = []

    if not outer_polys:
        for line in outers:
            if line.is_ring and len(line.coords) >= 4:
                geom: Any = Polygon(line)
                if not geom.is_valid:
                    geom = make_valid(geom)
                outer_polys.extend(_extract_polygons(geom))

    if not outer_polys:
        return []

    inner_polys: list[Polygon] = []
    if inners:
        try:
            merged_inner = linemerge(inners)
            inner_polys = [p for p in polygonize(merged_inner) if p.area > 0]
        except Exception:
            inner_polys = []
        if not inner_polys:
            for line in inners:
                if line.is_ring and len(line.coords) >= 4:
                    geom_i: Any = Polygon(line)
                    if not geom_i.is_valid:
                        geom_i = make_valid(geom_i)
                    inner_polys.extend(_extract_polygons(geom_i))

    # Validate outer shells without premature hole-punching from nested outer rings
    valid_outers: list[Polygon] = []
    for p in outer_polys:
        shell = Polygon(p.exterior)
        if not shell.is_valid:
            valid_outers.extend(_extract_polygons(make_valid(shell)))
        elif shell.area > 0:
            valid_outers.append(shell)

    # Assign each inner clearing to its smallest enclosing outer shell whose area > inner.area
    inners_by_outer: dict[int, list[Polygon]] = {i: [] for i in range(len(valid_outers))}
    for inner in inner_polys:
        fixed_inners = _extract_polygons(make_valid(inner)) if not inner.is_valid else [inner]
        for fi in fixed_inners:
            rep = fi.representative_point()
            candidates = [
                (idx, op.area)
                for idx, op in enumerate(valid_outers)
                if op.area > fi.area and op.covers(rep)
            ]
            if candidates:
                best_idx = min(candidates, key=lambda item: item[1])[0]
                inners_by_outer[best_idx].append(fi)

    assembled: list[Polygon] = []
    for idx, outer_poly in enumerate(valid_outers):
        assigned = inners_by_outer[idx]
        if not assigned:
            assembled.append(outer_poly)
            continue
        try:
            cut_geom: Any = outer_poly.difference(unary_union(assigned))
            if not cut_geom.is_valid:
                cut_geom = make_valid(cut_geom)
            assembled.extend(_extract_polygons(cut_geom))
        except Exception:
            # Fallback to direct hole construction before dropping
            holes = [fi.exterior.coords for fi in assigned]
            poly_fb: Any = Polygon(outer_poly.exterior.coords, holes)
            if not poly_fb.is_valid:
                poly_fb = make_valid(poly_fb)
            assembled.extend(_extract_polygons(poly_fb))

    if simplify_tolerance_m > 0:
        simplified: list[Polygon] = []
        for poly in assembled:
            simp = poly.simplify(simplify_tolerance_m, preserve_topology=True)
            if not simp.is_valid:
                simp = make_valid(simp)
            simplified.extend(_extract_polygons(simp))
        assembled = simplified

    return assembled


def _append_clipped_polygons(
    polys: list[Polygon],
    *,
    clip_box: Polygon | None,
    min_area_m2: float,
    osm_id_base: int,
    kind: str,
    leaf_type: str | None,
    name: str,
    areas: list[VegetationArea],
    stats: VegetationStats,
    is_relation: bool,
) -> None:
    """Clip polygons to bounds, validate winding, and append VegetationArea records."""
    part_idx = 0
    for poly in polys:
        if poly.is_empty or poly.area <= 0:
            continue

        target_polys: list[Polygon]
        if clip_box is not None:
            if not poly.intersects(clip_box):
                continue
            clipped = poly.intersection(clip_box)
            target_polys = _extract_polygons(clipped)
        else:
            target_polys = [poly]

        for tp in target_polys:
            area_m2 = float(tp.area)
            if area_m2 < min_area_m2:
                stats.skipped_area += 1
                continue

            rings = _polygon_to_rings(tp)
            if rings is None:
                stats.skipped_invalid += 1
                continue
            ring, holes = rings

            if is_relation:
                oid = -(abs(osm_id_base) * 1_000_000 + part_idx + 1)
            else:
                oid = osm_id_base if part_idx == 0 else (osm_id_base * 1_000_000 + part_idx)
            part_idx += 1

            areas.append(
                VegetationArea(
                    osm_id=oid,
                    ring=ring,
                    kind=kind,
                    holes=holes,
                    area_m2=area_m2,
                    leaf_type=leaf_type,
                    name=name,
                )
            )
            if is_relation:
                stats.relations += 1
            else:
                stats.ways += 1


def extract_vegetation(
    osm: OsmData,
    projection: RegionProjection,
    *,
    clip_bounds: tuple[float, float, float, float] | None = None,
    min_area_m2: float = 100.0,
    simplify_tolerance_m: float = 2.0,
    worldcover_dir: str | Path | bool | None = None,
) -> tuple[list[VegetationArea], VegetationStats]:
    """Extract vegetation areas (forests, scrub, meadows, farmland) from OSM data.

    Supports closed ways, linear ``natural=tree_row`` ways (buffered to narrow
    belts), multipolygon relations with inner clearings, and optional cached
    ESA WorldCover 10m canopy patches (when ``clip_bounds`` is given and
    ``worldcover_dir`` is not ``False``).
    """
    stats = VegetationStats()
    areas: list[VegetationArea] = []
    ways_by_id: dict[int, OsmWay] = {w.id: w for w in osm.ways}
    relation_outer_way_ids: set[int] = set()
    clip_box = box(*clip_bounds) if clip_bounds is not None else None

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

        for mtype, ref, role in relation.members:
            if mtype == "way" and role in {"outer", ""}:
                relation_outer_way_ids.add(ref)

        _append_clipped_polygons(
            polys,
            clip_box=clip_box,
            min_area_m2=min_area_m2,
            osm_id_base=relation.id,
            kind=kind,
            leaf_type=_infer_leaf_type(dict(relation.tags)),
            name=relation.tags.get("name", ""),
            areas=areas,
            stats=stats,
            is_relation=True,
        )

    # 2. Process closed ways and linear tree_row ways
    for way in osm.ways:
        if way.id in relation_outer_way_ids:
            continue
        kind = _classify_vegetation(dict(way.tags))
        if kind is None:
            continue

        is_tree_row = (
            not way.is_closed
            and way.tags.get("natural", "").strip().lower() == "tree_row"
        )
        if not way.is_closed and not is_tree_row:
            continue

        coords = osm.way_coords(way)
        min_pts = 2 if is_tree_row else 3
        if len(coords) < min_pts:
            continue
        lons = np.asarray([c[0] for c in coords], dtype=np.float64)
        lats = np.asarray([c[1] for c in coords], dtype=np.float64)
        xs, ys = projection.geo_to_world_array(lons, lats)
        pts = np.column_stack((xs, ys))

        if is_tree_row:
            line = LineString(pts)
            if line.is_empty or line.length <= 0:
                stats.skipped_invalid += 1
                continue
            polys = _extract_polygons(line.buffer(3.0, cap_style="flat"))
        else:
            polys = _clean_polygon(pts, simplify_tolerance_m)

        if not polys:
            stats.skipped_invalid += 1
            continue

        _append_clipped_polygons(
            polys,
            clip_box=clip_box,
            min_area_m2=min_area_m2,
            osm_id_base=way.id,
            kind=kind,
            leaf_type=_infer_leaf_type(dict(way.tags)),
            name=way.tags.get("name", ""),
            areas=areas,
            stats=stats,
            is_relation=False,
        )

    # 3. Supplement unmapped tree canopy from cached ESA WorldCover 10m raster (when available)
    if clip_bounds is not None and worldcover_dir is not False:
        corners = [
            projection.world_to_geo(x, y)
            for x in (clip_bounds[0], clip_bounds[2])
            for y in (clip_bounds[1], clip_bounds[3])
        ]
        geo_bbox = (
            min(c[0] for c in corners),
            min(c[1] for c in corners),
            max(c[0] for c in corners),
            max(c[1] for c in corners),
        )
        wc_cache = Path(worldcover_dir) if isinstance(worldcover_dir, (str, Path)) else None
        wc_grid = load_worldcover_cache(geo_bbox, cache_dir=wc_cache)
        if wc_grid is not None:
            exclude_geoms = [
                a.to_polygon()
                for a in areas
                if a.kind in {"forest", "farmland", "meadow", "scrub"}
            ]
            wc_polys = worldcover_to_forest_polygons(
                wc_grid,
                projection,
                clip_bounds=clip_bounds,
                exclude_geoms=exclude_geoms,
                min_area_m2=max(400.0, min_area_m2),
            )
            for idx, wp in enumerate(wc_polys, start=1):
                rings = _polygon_to_rings(wp)
                if rings is None:
                    continue
                ring, holes = rings
                areas.append(
                    VegetationArea(
                        osm_id=-9_000_000_000 - idx,
                        ring=ring,
                        kind="forest",
                        holes=holes,
                        area_m2=float(wp.area),
                        leaf_type="mixed",
                        name="WorldCover Canopy",
                    )
                )
                stats.worldcover_areas += 1

    for a in areas:
        stats.by_kind[a.kind] = stats.by_kind.get(a.kind, 0) + 1
        stats.total_area_m2 += a.area_m2
        if a.kind == "forest":
            stats.forest_areas += 1
        elif a.kind == "scrub":
            stats.scrub_areas += 1
        elif a.kind == "meadow":
            stats.meadow_areas += 1
        elif a.kind == "farmland":
            stats.farmland_areas += 1

    stats.areas = len(areas)
    return areas, stats
