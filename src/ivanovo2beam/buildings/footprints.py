"""Building footprint extraction from OpenStreetMap.

Footprints are taken from closed ``building=*`` ways and ``type=multipolygon``
building relations, projected to the metric world plane, classified into
architectural typologies, and given height, base offsets, and courtyard holes.

Height resolution order:
1. explicit ``height`` tag, in metres ("12", "12 m", "40'");
2. ``building:levels`` multiplied by the class's storey height;
3. the class default from :data:`BUILDING_CLASSES`.

For multi-height ``building:part`` volumes:
- ``min_height`` or ``building:min_level`` determines base height above ground;
- ``roof:shape`` and ``roof:height`` define roof form;
- ``building:material`` provides PBR material hints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import linemerge, polygonize
from shapely.validation import make_valid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from ivanovo2beam.gis.projection import RegionProjection
    from ivanovo2beam.osm.types import OsmData, OsmRelation, OsmWay


@dataclass(frozen=True, slots=True)
class BuildingClass:
    """Default massing for one kind of building.

    Attributes:
        key: Stable identifier, also the archetype family name.
        title: Human-readable description.
        levels: Storey count used when the tags are silent.
        level_height_m: Storey height, used to turn levels into metres.
        height_m: Height used when neither ``height`` nor ``building:levels``
            is tagged.
        roof: Roof form, ``"flat"``, ``"gable"`` or ``"hip"``.
        colour: Diffuse RGB in ``[0, 1]``, used for the generated material.
        min_area_m2: Footprints smaller than this are dropped as noise.
    """

    key: str
    title: str
    levels: float
    level_height_m: float
    height_m: float
    roof: str
    colour: tuple[float, float, float]
    min_area_m2: float = 20.0

    def height_for_levels(self, levels: float) -> float:
        """Return the height implied by a storey count."""
        return max(2.5, levels * self.level_height_m)


#: OSM ``building`` values mapped onto archetypes. Anything not listed falls
#: back to ``"yes"``.
BUILDING_CLASSES: dict[str, BuildingClass] = {
    "house": BuildingClass(
        key="house",
        title="Detached or semi-detached wooden house / izba",
        levels=1.5,
        level_height_m=3.0,
        height_m=6.0,
        roof="gable",
        colour=(0.72, 0.66, 0.58),
    ),
    "apartments": BuildingClass(
        key="apartments",
        title="Apartment block (Khrushchevka or Brezhnevka)",
        levels=5.0,
        level_height_m=3.0,
        height_m=15.0,
        roof="flat",
        colour=(0.68, 0.64, 0.60),
        min_area_m2=60.0,
    ),
    "industrial": BuildingClass(
        key="industrial",
        title="Industrial textile mill or factory",
        levels=1.0,
        level_height_m=6.0,
        height_m=9.0,
        roof="gable",
        colour=(0.58, 0.58, 0.60),
        min_area_m2=80.0,
    ),
    "commercial": BuildingClass(
        key="commercial",
        title="Shop, office, civic or retail",
        levels=2.0,
        level_height_m=3.6,
        height_m=7.5,
        roof="flat",
        colour=(0.66, 0.63, 0.61),
    ),
    "garage": BuildingClass(
        key="garage",
        title="Garage or carport",
        levels=1.0,
        level_height_m=2.6,
        height_m=2.8,
        roof="flat",
        colour=(0.62, 0.60, 0.57),
        min_area_m2=10.0,
    ),
    "shed": BuildingClass(
        key="shed",
        title="Shed, barn or outbuilding",
        levels=1.0,
        level_height_m=3.0,
        height_m=3.5,
        roof="gable",
        colour=(0.55, 0.50, 0.44),
        min_area_m2=8.0,
    ),
    "school": BuildingClass(
        key="school",
        title="School, college or kindergarten",
        levels=3.0,
        level_height_m=3.6,
        height_m=12.0,
        roof="flat",
        colour=(0.74, 0.70, 0.62),
        min_area_m2=100.0,
    ),
    "hospital": BuildingClass(
        key="hospital",
        title="Hospital or clinic",
        levels=4.0,
        level_height_m=3.6,
        height_m=16.0,
        roof="flat",
        colour=(0.78, 0.78, 0.76),
        min_area_m2=100.0,
    ),
    "church": BuildingClass(
        key="church",
        title="Church, chapel or monastery",
        levels=1.0,
        level_height_m=6.0,
        height_m=12.0,
        roof="gable",
        colour=(0.80, 0.76, 0.68),
    ),
    "yes": BuildingClass(
        key="yes",
        title="Building, type untagged",
        levels=2.0,
        level_height_m=3.2,
        height_m=7.0,
        roof="gable",
        colour=(0.70, 0.66, 0.60),
    ),
}

#: OSM tag values mapped onto archetype keys.
_TAG_TO_CLASS: dict[str, str] = {
    "house": "house",
    "detached": "house",
    "semidetached_house": "house",
    "terrace": "house",
    "bungalow": "house",
    "static_caravan": "house",
    "cabin": "house",
    "shack": "house",
    "allotment_house": "house",
    "farm": "house",
    "apartments": "apartments",
    "residential": "apartments",
    "dormitory": "apartments",
    "barracks": "apartments",
    "hotel": "apartments",
    "industrial": "industrial",
    "factory": "industrial",
    "warehouse": "industrial",
    "hangar": "industrial",
    "manufacture": "industrial",
    "mill": "industrial",
    "textile": "industrial",
    "works": "industrial",
    "commercial": "commercial",
    "retail": "commercial",
    "supermarket": "commercial",
    "office": "commercial",
    "kiosk": "commercial",
    "mall": "commercial",
    "civic": "commercial",
    "public": "commercial",
    "government": "commercial",
    "townhall": "commercial",
    "theatre": "commercial",
    "museum": "commercial",
    "garage": "garage",
    "garages": "garage",
    "carport": "garage",
    "service": "garage",
    "shed": "shed",
    "barn": "shed",
    "farm_auxiliary": "shed",
    "greenhouse": "shed",
    "stable": "shed",
    "cowshed": "shed",
    "school": "school",
    "college": "school",
    "university": "school",
    "kindergarten": "school",
    "hospital": "hospital",
    "clinic": "hospital",
    "church": "church",
    "cathedral": "church",
    "chapel": "church",
    "mosque": "church",
    "temple": "church",
    "synagogue": "church",
    "monastery": "church",
    "yes": "yes",
}

_FLOAT_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_FEET_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*'\s*$")


def building_class_for(tag_value: str | None) -> BuildingClass:
    """Return the archetype for an OSM ``building`` tag value.

    Args:
        tag_value: The tag value, e.g. ``"apartments"``.

    Returns:
        The matching :class:`BuildingClass`, falling back to the untagged
        archetype for anything unrecognised.
    """
    key = (tag_value or "yes").strip().lower()
    return BUILDING_CLASSES[_TAG_TO_CLASS.get(key, "yes")]


def parse_height_m(value: str | None) -> float | None:
    """Parse an OSM ``height`` tag into metres.

    Accepts ``"12"``, ``"12 m"``, ``"12.5"``, ``"12,5"`` and ``"40'"``.
    """
    if not value:
        return None
    text = value.strip()
    feet = _FEET_RE.match(text)
    if feet:
        metres = float(feet.group(1).replace(",", ".")) * 0.3048
        return metres if 2.0 <= metres <= 700.0 else None
    match = _FLOAT_RE.search(text.replace(",", "."))
    if match is None:
        return None
    metres = float(match.group())
    return metres if 2.0 <= metres <= 700.0 else None


def parse_min_height_m(value: str | None) -> float | None:
    """Parse an OSM ``min_height`` tag into metres.

    Accepts ``"3"``, ``"3.5 m"``, ``"0"``.
    """
    if not value:
        return None
    text = value.strip()
    feet = _FEET_RE.match(text)
    if feet:
        metres = float(feet.group(1).replace(",", ".")) * 0.3048
        return metres if 0.0 <= metres <= 700.0 else None
    match = _FLOAT_RE.search(text.replace(",", "."))
    if match is None:
        return None
    metres = float(match.group())
    return metres if 0.0 <= metres <= 700.0 else None


def parse_levels(value: str | None) -> float | None:
    """Parse an OSM ``building:levels`` tag."""
    if not value:
        return None
    match = _FLOAT_RE.search(value.replace(",", "."))
    if match is None:
        return None
    levels = float(match.group())
    return levels if 0.5 <= levels <= 60.0 else None


def parse_min_level(value: str | None) -> float | None:
    """Parse an OSM ``building:min_level`` tag."""
    if not value:
        return None
    match = _FLOAT_RE.search(value.replace(",", "."))
    if match is None:
        return None
    lvl = float(match.group())
    return lvl if 0.0 <= lvl <= 60.0 else None


@dataclass(slots=True)
class BuildingFootprint:
    """One building's footprint in world metres.

    Attributes:
        osm_id: Source OSM way or relation id.
        ring: ``(n, 2)`` closed exterior ring of world ``(x, y)`` metres.
        klass: Archetype key.
        height_m: Total height above ground in metres.
        levels: Storey count, whether tagged or defaulted.
        name: Building or operator name when tagged.
        area_m2: Footprint area in square metres (net area).
        height_source: ``"height"``, ``"levels"`` or ``"default"``.
        min_height_m: Base elevation above ground in metres (for building:part).
        holes: Optional tuple of closed ``(m, 2)`` inner courtyard rings.
        roof_shape: Optional roof form (``"flat"``, ``"gable"``, ``"hip"``).
        roof_height_m: Optional explicit roof height.
        material: Optional material hint (``"brick"``, ``"concrete"``, ``"plaster"``, ``"wood"``).
        part: Whether this is a ``building:part`` volume.
    """

    osm_id: int
    ring: NDArray[np.float64]
    klass: str
    height_m: float
    levels: float
    name: str
    area_m2: float
    height_source: str
    min_height_m: float = 0.0
    holes: tuple[NDArray[np.float64], ...] = ()
    roof_shape: str | None = None
    roof_height_m: float | None = None
    material: str | None = None
    part: bool = False

    @property
    def centroid(self) -> tuple[float, float]:
        """Return the footprint centroid in world metres."""
        try:
            poly = Polygon(self.ring, self.holes)
            if poly.is_valid and not poly.is_empty:
                c = poly.centroid
                return (float(c.x), float(c.y))
        except Exception:
            pass
        pts = self.ring[:-1] if len(self.ring) > 1 and np.array_equal(
            self.ring[0], self.ring[-1]
        ) else self.ring
        x = pts[:, 0]
        y = pts[:, 1]
        cross = x * np.roll(y, -1) - np.roll(x, -1) * y
        area = cross.sum() / 2.0
        if abs(area) < 1e-9:
            return (float(x.mean()), float(y.mean()))
        cx = ((x + np.roll(x, -1)) * cross).sum() / (6.0 * area)
        cy = ((y + np.roll(y, -1)) * cross).sum() / (6.0 * area)
        return (float(cx), float(cy))


@dataclass(slots=True)
class FootprintStats:
    """Diagnostics from footprint extraction."""

    buildings: int = 0
    parts: int = 0
    relations: int = 0
    skipped_area: int = 0
    skipped_invalid: int = 0
    skipped_degenerate: int = 0
    skipped_relations: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    by_height_source: dict[str, int] = field(default_factory=dict)
    total_area_m2: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary."""
        return {
            "buildings": self.buildings,
            "parts": self.parts,
            "relations": self.relations,
            "skipped_area": self.skipped_area,
            "skipped_invalid": self.skipped_invalid,
            "skipped_degenerate": self.skipped_degenerate,
            "skipped_relations": self.skipped_relations,
            "by_class": self.by_class or {},
            "by_height_source": self.by_height_source or {},
            "total_area_km2": round(self.total_area_m2 / 1e6, 4),
        }


def _ring_from_points(
    points: NDArray[np.float64], tolerance_m: float
) -> NDArray[np.float64] | None:
    """Close, validate and simplify a ring."""
    if points.shape[0] < 3:
        return None
    closed = points
    if not np.array_equal(points[0], points[-1]):
        closed = np.vstack((points, points[0]))
    if closed.shape[0] < 4:
        return None

    polygon = Polygon(closed)
    if not polygon.is_valid:
        repaired = make_valid(polygon)
        if repaired.is_empty:
            return None
        if repaired.geom_type == "MultiPolygon":
            repaired = max(repaired.geoms, key=lambda g: g.area)
        if repaired.geom_type != "Polygon" or repaired.is_empty:
            return None
        polygon = repaired
    if polygon.area <= 0:
        return None

    if tolerance_m > 0:
        simplified = polygon.simplify(tolerance_m, preserve_topology=True)
        if simplified.geom_type == "Polygon" and simplified.area > 0:
            polygon = simplified

    coords = np.asarray(polygon.exterior.coords, dtype=np.float64)
    if coords.shape[0] < 4:
        return None
    return ensure_counter_clockwise(coords)


def _assemble_multipolygon(
    relation: OsmRelation,
    osm: OsmData,
    projection: RegionProjection,
    simplify_tolerance_m: float,
    ways_by_id: dict[int, OsmWay],
) -> list[Polygon]:
    """Assemble outer and inner ways of a multipolygon relation into Polygons."""
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
        # Fallback: try closed ways directly
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
        # Find which inner polygons belong to this outer polygon
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


def extract_buildings(
    osm: OsmData,
    projection: RegionProjection,
    *,
    clip_bounds: tuple[float, float, float, float] | None = None,
    simplify_tolerance_m: float = 1.0,
    max_buildings: int | None = None,
) -> tuple[list[BuildingFootprint], FootprintStats]:
    """Extract building footprints and building:parts from parsed OSM data.

    Supports closed ways, multi-height building:part volumes, and type=multipolygon
    relations with inner courtyard holes.
    """
    stats = FootprintStats(by_class={}, by_height_source={})
    buildings: list[BuildingFootprint] = []
    ways_by_id: dict[int, OsmWay] = {w.id: w for w in osm.ways}

    # Track member ways of building relations so they aren't duplicated
    relation_way_ids: set[int] = set()

    # Process multipolygon building relations
    for relation in osm.relations:
        is_mp = relation.tags.get("type") == "multipolygon"
        is_bld = "building" in relation.tags or "building:part" in relation.tags
        if not (is_mp and is_bld):
            continue

        tag = relation.tags.get("building") or relation.tags.get("building:part")
        klass = building_class_for(tag)
        polys = _assemble_multipolygon(
            relation, osm, projection, simplify_tolerance_m, ways_by_id
        )
        if not polys:
            stats.skipped_relations += 1
            continue

        # Mark member ways as absorbed
        for _, ref, _ in relation.members:
            relation_way_ids.add(ref)

        rel_height = parse_height_m(relation.tags.get("height"))
        source = "height"
        if rel_height is None:
            levels = parse_levels(relation.tags.get("building:levels"))
            if levels is not None:
                rel_height = klass.height_for_levels(levels)
                source = "levels"
            else:
                levels = klass.levels
                rel_height = klass.height_m
                source = "default"
        else:
            levels = parse_levels(relation.tags.get("building:levels")) or round(
                rel_height / klass.level_height_m, 1
            )

        min_h = parse_min_height_m(relation.tags.get("min_height"))
        if min_h is None:
            min_lvl = parse_min_level(relation.tags.get("building:min_level"))
            min_h = min_lvl * klass.level_height_m if min_lvl is not None else 0.0

        roof_shape = relation.tags.get("roof:shape")
        roof_h = parse_height_m(relation.tags.get("roof:height"))
        mat = relation.tags.get("building:material") or relation.tags.get("material")

        for p_idx, poly in enumerate(polys):
            area = float(poly.area)
            if area < klass.min_area_m2:
                stats.skipped_area += 1
                continue

            exterior_ring = ensure_counter_clockwise(
                np.asarray(poly.exterior.coords, dtype=np.float64)
            )
            holes = tuple(
                np.asarray(inner.coords, dtype=np.float64) for inner in poly.interiors
            )

            if clip_bounds is not None:
                x0, y0, x1, y1 = clip_bounds
                xs_r = exterior_ring[:, 0]
                ys_r = exterior_ring[:, 1]
                width = float(xs_r.max() - xs_r.min())
                depth = float(ys_r.max() - ys_r.min())
                cx = float((xs_r.min() + xs_r.max()) / 2.0)
                cy = float((ys_r.min() + ys_r.max()) / 2.0)
                span_x = max(0.0, x1 - x0)
                span_y = max(0.0, y1 - y0)
                if not (x0 <= cx <= x1 and y0 <= cy <= y1) or (
                    span_x > 0 and span_y > 0 and (width > span_x or depth > span_y)
                ):
                    stats.skipped_invalid += 1
                    continue

            footprint = BuildingFootprint(
                osm_id=-relation.id if p_idx == 0 else -abs(relation.id * 1000 + p_idx),
                ring=exterior_ring,
                klass=klass.key,
                height_m=float(rel_height),
                levels=float(levels),
                name=relation.tags.get("name", ""),
                area_m2=area,
                height_source=source,
                min_height_m=float(min_h),
                holes=holes,
                roof_shape=roof_shape,
                roof_height_m=roof_h,
                material=mat,
                part="building:part" in relation.tags,
            )
            buildings.append(footprint)
            stats.relations += 1

    # Process closed ways
    for way in osm.ways:
        is_part = "building:part" in way.tags and way.tags["building:part"] not in {"no", "none"}
        bld_tag = way.tags.get("building")
        if bld_tag is None and not is_part:
            continue
        if not way.is_closed:
            continue
        if way.id in relation_way_ids and not is_part:
            continue

        tag = bld_tag or way.tags.get("building:part")
        klass = building_class_for(tag)

        coords = osm.way_coords(way)
        if len(coords) < 3:
            stats.skipped_degenerate += 1
            continue
        lons = np.asarray([c[0] for c in coords], dtype=np.float64)
        lats = np.asarray([c[1] for c in coords], dtype=np.float64)
        xs, ys = projection.geo_to_world_array(lons, lats)
        ring = _ring_from_points(np.column_stack((xs, ys)), simplify_tolerance_m)
        if ring is None:
            stats.skipped_invalid += 1
            continue

        area = float(Polygon(ring).area)
        if area < klass.min_area_m2:
            stats.skipped_area += 1
            continue
        if clip_bounds is not None:
            x0, y0, x1, y1 = clip_bounds
            xs_r = ring[:, 0]
            ys_r = ring[:, 1]
            width = float(xs_r.max() - xs_r.min())
            depth = float(ys_r.max() - ys_r.min())
            cx = float((xs_r.min() + xs_r.max()) / 2.0)
            cy = float((ys_r.min() + ys_r.max()) / 2.0)
            span_x = max(0.0, x1 - x0)
            span_y = max(0.0, y1 - y0)
            if not (x0 <= cx <= x1 and y0 <= cy <= y1) or (
                span_x > 0 and span_y > 0 and (width > span_x or depth > span_y)
            ):
                stats.skipped_invalid += 1
                continue

        height = parse_height_m(way.tags.get("height"))
        source = "height"
        if height is None:
            levels = parse_levels(way.tags.get("building:levels"))
            if levels is not None:
                height = klass.height_for_levels(levels)
                source = "levels"
            else:
                levels = klass.levels
                height = klass.height_m
                source = "default"
        else:
            levels = parse_levels(way.tags.get("building:levels")) or round(
                height / klass.level_height_m, 1
            )

        min_h = parse_min_height_m(way.tags.get("min_height"))
        if min_h is None:
            min_lvl = parse_min_level(way.tags.get("building:min_level"))
            min_h = min_lvl * klass.level_height_m if min_lvl is not None else 0.0

        roof_shape = way.tags.get("roof:shape")
        roof_h = parse_height_m(way.tags.get("roof:height"))
        mat = way.tags.get("building:material") or way.tags.get("material")

        buildings.append(
            BuildingFootprint(
                osm_id=way.id,
                ring=ring,
                klass=klass.key,
                height_m=float(height),
                levels=float(levels),
                name=way.tags.get("name", ""),
                area_m2=area,
                height_source=source,
                min_height_m=float(min_h),
                holes=(),
                roof_shape=roof_shape,
                roof_height_m=roof_h,
                material=mat,
                part=is_part,
            )
        )
        if is_part:
            stats.parts += 1

    if max_buildings is not None and len(buildings) > max_buildings:
        buildings.sort(key=lambda b: (-b.area_m2, b.osm_id))
        buildings = buildings[: max(0, max_buildings)]

    for building in buildings:
        stats.by_class[building.klass] = stats.by_class.get(building.klass, 0) + 1
        stats.by_height_source[building.height_source] = (
            stats.by_height_source.get(building.height_source, 0) + 1
        )
        stats.total_area_m2 += building.area_m2

    stats.buildings = len(buildings)
    return buildings, stats


def footprint_ring_area(ring: NDArray[np.float64]) -> float:
    """Return the signed area of a closed ring."""
    x = ring[:, 0]
    y = ring[:, 1]
    return float(0.5 * np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def ensure_counter_clockwise(ring: NDArray[np.float64]) -> NDArray[np.float64]:
    """Return the ring wound counter-clockwise."""
    return ring[::-1].copy() if footprint_ring_area(ring) < 0 else ring


def default_archetypes(_: Sequence[str] | None = None) -> tuple[str, ...]:
    """Return the archetype keys a build will need meshes for."""
    return tuple(BUILDING_CLASSES)
