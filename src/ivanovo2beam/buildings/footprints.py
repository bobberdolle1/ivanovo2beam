"""Building footprint extraction from OpenStreetMap.

Footprints are taken from closed ``building=*`` ways, projected to the metric
world plane, classified into a small set of archetypes, and given a height.

Height resolution order, mirroring the project-wide rule that real tags win:

1. an explicit ``height`` tag, in metres (``"12"``, ``"12 m"``, ``"40'"``);
2. ``building:levels`` multiplied by the class's storey height;
3. the class default from :data:`BUILDING_CLASSES`.

Every footprint is simplified before it becomes geometry. An OSM building traced
from aerial imagery can carry dozens of vertices for what is, geometrically, a
rectangle; keeping them would multiply the triangle count of a city for no
visible gain. Simplification happens here rather than at mesh time so that the
retained shape is what gets validated, measured and cached.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely.geometry import Polygon
from shapely.validation import make_valid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from ivanovo2beam.gis.projection import RegionProjection
    from ivanovo2beam.osm.types import OsmData


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
        title="Detached or semi-detached house",
        levels=1.5,
        level_height_m=3.0,
        height_m=6.0,
        roof="gable",
        colour=(0.72, 0.66, 0.58),
    ),
    "apartments": BuildingClass(
        key="apartments",
        title="Apartment block",
        levels=5.0,
        level_height_m=3.0,
        height_m=15.0,
        roof="flat",
        colour=(0.68, 0.64, 0.60),
        min_area_m2=60.0,
    ),
    "industrial": BuildingClass(
        key="industrial",
        title="Industrial building or factory",
        levels=1.0,
        level_height_m=6.0,
        height_m=9.0,
        roof="gable",
        colour=(0.58, 0.58, 0.60),
        min_area_m2=80.0,
    ),
    "commercial": BuildingClass(
        key="commercial",
        title="Shop, office or retail",
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
        title="Church, chapel or mosque",
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
    "commercial": "commercial",
    "retail": "commercial",
    "supermarket": "commercial",
    "office": "commercial",
    "kiosk": "commercial",
    "mall": "commercial",
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

    Args:
        value: The raw tag value, or ``None``.

    Returns:
        A plausible height in metres, or ``None``.
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
    # Reject nonsense: nothing is 1 m tall, and 700 m would be the tallest
    # structure in Russia.
    return metres if 2.0 <= metres <= 700.0 else None


def parse_levels(value: str | None) -> float | None:
    """Parse an OSM ``building:levels`` tag.

    Args:
        value: The raw tag value, or ``None``.

    Returns:
        A storey count in ``[0.5, 60]``, or ``None``.
    """
    if not value:
        return None
    match = _FLOAT_RE.search(value.replace(",", "."))
    if match is None:
        return None
    levels = float(match.group())
    return levels if 0.5 <= levels <= 60.0 else None


@dataclass(slots=True)
class BuildingFootprint:
    """One building's footprint in world metres.

    Attributes:
        osm_id: Source OSM way id.
        ring: ``(n, 2)`` closed ring of world ``(x, y)`` metres.
        klass: Archetype key.
        height_m: Total height above ground in metres.
        levels: Storey count, whether tagged or defaulted.
        name: Building or operator name when tagged.
        area_m2: Footprint area in square metres.
        height_source: ``"height"``, ``"levels"`` or ``"default"``, recorded so
            a build can report how much of the city is real data.
    """

    osm_id: int
    ring: NDArray[np.float64]
    klass: str
    height_m: float
    levels: float
    name: str
    area_m2: float
    height_source: str

    @property
    def centroid(self) -> tuple[float, float]:
        """Return the ring's area centroid in world metres."""
        pts = self.ring[:-1] if len(self.ring) > 1 and np.array_equal(
            self.ring[0], self.ring[-1]
        ) else self.ring
        x = pts[:, 0]
        y = pts[:, 1]
        # Shoelace centroid.
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
    skipped_area: int = 0
    skipped_invalid: int = 0
    skipped_degenerate: int = 0
    skipped_relations: int = 0
    by_class: dict[str, int] | None = None
    by_height_source: dict[str, int] | None = None
    total_area_m2: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary."""
        return {
            "buildings": self.buildings,
            "skipped_area": self.skipped_area,
            "skipped_invalid": self.skipped_invalid,
            "skipped_degenerate": self.skipped_degenerate,
            "skipped_relations": self.skipped_relations,
            "by_class": self.by_class or {},
            "by_height_source": self.by_height_source or {},
            "total_area_km2": round(self.total_area_m2 / 1e6, 4),
        }


def _ring_from_points(points: NDArray[np.float64], tolerance_m: float) -> NDArray[np.float64] | None:
    """Close, validate and simplify a ring.

    Args:
        points: ``(n, 2)`` open or closed ring in world metres.
        tolerance_m: Douglas-Peucker tolerance. ``0`` disables simplification.

    Returns:
        A closed ``(m, 2)`` ring, or ``None`` if the shape is unusable.
    """
    if points.shape[0] < 3:
        return None
    closed = points
    if not np.array_equal(points[0], points[-1]):
        closed = np.vstack((points, points[0]))
    if closed.shape[0] < 4:
        return None

    polygon = Polygon(closed)
    if not polygon.is_valid:
        # Self-intersecting OSM footprints are common; repair rather than drop.
        repaired = make_valid(polygon)
        if repaired.is_empty:
            return None
        if repaired.geom_type == "MultiPolygon":
            repaired = max(repaired.geoms, key=lambda g: g.area)
        if repaired.geom_type != "Polygon" or repaired.is_empty:
            return None
        polygon = repaired  # type: ignore[assignment]
    if polygon.area <= 0:
        return None

    if tolerance_m > 0:
        simplified = polygon.simplify(tolerance_m, preserve_topology=True)
        if simplified.geom_type == "Polygon" and simplified.area > 0:
            polygon = simplified  # type: ignore[assignment]

    coords = np.asarray(polygon.exterior.coords, dtype=np.float64)
    if coords.shape[0] < 4:
        return None
    return ensure_counter_clockwise(coords)


def extract_buildings(
    osm: OsmData,
    projection: RegionProjection,
    *,
    clip_bounds: tuple[float, float, float, float] | None = None,
    simplify_tolerance_m: float = 1.0,
    max_buildings: int | None = None,
) -> tuple[list[BuildingFootprint], FootprintStats]:
    """Extract building footprints from parsed OSM data.

    Args:
        osm: Parsed OSM extract.
        projection: World plane transform.
        clip_bounds: Optional ``(x0, y0, x1, y1)``; buildings whose centroid
            falls outside are dropped, as are those larger than the bounds
            (a building bigger than the mapped area is a data error).
        simplify_tolerance_m: Footprint simplification tolerance in metres.
        max_buildings: Optional cap on the number of footprints returned,
            largest first. Useful for keeping a first build tractable.

    Returns:
        ``(footprints, stats)``.
    """
    stats = FootprintStats(by_class={}, by_height_source={})
    buildings: list[BuildingFootprint] = []

    for way in osm.ways:
        tag = way.tags.get("building")
        if tag is None or not way.is_closed:
            continue
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
            )
        )

    # Multipolygon buildings (large malls, complexes) are not assembled yet.
    # Count them so the gap is visible in every build report rather than silent.
    stats.skipped_relations = sum(
        1
        for relation in osm.relations
        if relation.tags.get("type") == "multipolygon" and "building" in relation.tags
    )

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
    """Return the ring wound counter-clockwise, which the mesher requires.

    Args:
        ring: Closed ``(n, 2)`` ring.

    Returns:
        The same ring, reversed if it was clockwise.
    """
    return ring[::-1].copy() if footprint_ring_area(ring) < 0 else ring


def default_archetypes(_: Sequence[str] | None = None) -> tuple[str, ...]:
    """Return the archetype keys a build will need meshes for."""
    return tuple(BUILDING_CLASSES)


def _slope_aspect(ring: NDArray[np.float64]) -> float:
    """Return the orientation of the footprint's longest edge, in radians.

    Used to align a gable roof with the building rather than with the world.
    """
    deltas = np.diff(ring, axis=0)
    if deltas.size == 0:
        return 0.0
    lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    longest = int(np.argmax(lengths))
    return math.atan2(deltas[longest, 1], deltas[longest, 0])
