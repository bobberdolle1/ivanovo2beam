"""Data model for OpenStreetMap extracts, shared by the fetch and parse stages.

Why a bespoke model instead of ``osmnx`` / ``pyosmium``
------------------------------------------------------
The map generator only ever needs four things from OpenStreetMap:

1. node coordinates, so geometry can be projected into the game world;
2. way node lists, so roads and water courses become polylines;
3. closed ways and relations, so buildings and water bodies become polygons;
4. tags, so each element can be classified into a render/road profile.

That is a *tiny* subset of the OSM data model, and it is deliberately kept
tiny here. The heavy machinery (``osmnx``, ``pyosmium``, ``osm2pgsql``) either
drags in a graph library, a C++ toolchain or a database -- none of which is
acceptable for a pipeline that must run from a bare checkout on Windows. This
module therefore defines plain frozen dataclasses plus one mutable container,
:class:`OsmData`, which holds the whole extract in a form that is cheap to
iterate and cheap to query by tag.

Coordinate convention
---------------------
Every coordinate in this module is ``(lon, lat)`` in WGS84 degrees -- the same
order :mod:`pyproj` uses with ``always_xy=True`` and the same order
:class:`~ivanovo2beam.gis.projection.RegionProjection` consumes. Lat/lon order
is the single most common bug in OSM tooling, so it is stated once here and
never varied.

Memory
------
:class:`OsmData` is intentionally *plain data*: node geometry lives in one
``dict[int, tuple[float, float]]`` rather than in one object per node, because
a regional extract has millions of nodes and per-node objects would cost
hundreds of megabytes of interpreter overhead. See
:mod:`ivanovo2beam.osm.parse` for how that dictionary is filled without ever
materialising an XML DOM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Mapping

__all__ = [
    "AREA_KEYS",
    "LINEAR_NATURAL_VALUES",
    "OsmData",
    "OsmError",
    "OsmFetchError",
    "OsmNode",
    "OsmParseError",
    "OsmRelation",
    "OsmWay",
    "OverpassError",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class OsmError(Exception):
    """Base class for every error raised by :mod:`ivanovo2beam.osm`."""


class OsmFetchError(OsmError):
    """An OSM extract could not be downloaded or read from the cache."""


class OverpassError(OsmFetchError):
    """The Overpass API answered, but not with usable data.

    Raised for HTTP failures on every endpoint, for rate limiting that could
    not be satisfied, and for a ``<remark>`` element, which Overpass uses to
    report a timeout or a query error *inside* an otherwise valid HTTP 200
    response.
    """


class OsmParseError(OsmError):
    """An OSM XML file could not be parsed."""


# --------------------------------------------------------------------------- #
# Area classification
# --------------------------------------------------------------------------- #

#: Tag keys that make a *closed* way an area (a polygon) rather than a ring.
#:
#: This mirrors the "polygon features" list used by osm2pgsql and osm2streets:
#: anything with a land use, a building, an administrative boundary or a
#: natural feature attached to a closed ring describes a surface.
AREA_KEYS = frozenset(
    {
        "aeroway",
        "amenity",
        "area:highway",
        "boundary",
        "building",
        "building:part",
        "craft",
        "geological",
        "healthcare",
        "historic",
        "indoor",
        "landuse",
        "leisure",
        "man_made",
        "military",
        "natural",
        "office",
        "place",
        "power",
        "public_transport",
        "room",
        "shop",
        "tourism",
        "water",
        "waterway",
        "wetland",
    }
)

#: ``natural=*`` values that stay *linear* even when the ring is closed.
#:
#: ``natural=coastline`` in particular is a closed way around an island, but it
#: is a line in every renderer's data model; the same is true for cliffs and
#: tree rows.
LINEAR_NATURAL_VALUES = frozenset(
    {"arete", "cliff", "coastline", "earth_bank", "gorge", "ridge", "tree_row", "valley"}
)

_TRUTHY = frozenset({"1", "true", "yes"})
_FALSY = frozenset({"0", "false", "no"})


# --------------------------------------------------------------------------- #
# Elements
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OsmNode:
    """A single OSM node.

    Standalone nodes (POIs, traffic signals, speed cameras) are rarely needed
    as geometry, but they carry the tags that matter for placement -- hence the
    separate :class:`OsmNode` type alongside the coordinate-only dictionary in
    :class:`OsmData`.

    Attributes:
        id: OSM element id (positive for real data).
        lon: Longitude in degrees east (WGS84).
        lat: Latitude in degrees north (WGS84).
        tags: Element tags; empty for untagged geometry nodes.
    """

    id: int
    lon: float
    lat: float
    tags: Mapping[str, str] = field(default_factory=dict)

    @property
    def coord(self) -> tuple[float, float]:
        """Return the node position as ``(lon, lat)``."""
        return (self.lon, self.lat)


@dataclass(frozen=True, slots=True)
class OsmWay:
    """An ordered list of node references.

    Attributes:
        id: OSM element id.
        nodes: Node ids in order. A closed way repeats its first id last.
        tags: Element tags; empty for untagged geometry ways (for example the
            member ways of a multipolygon relation).
    """

    id: int
    nodes: tuple[int, ...]
    tags: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_closed(self) -> bool:
        """Return whether the way's first and last node ids are equal.

        A way needs at least two node references before "closed" means
        anything, so a degenerate one-node way is never reported as closed.
        """
        return len(self.nodes) >= 2 and self.nodes[0] == self.nodes[-1]

    @property
    def is_area(self) -> bool:
        """Return whether the way describes a surface rather than a line.

        The test is the one every OSM renderer uses:

        1. the way must be closed *and* have at least four node references (a
           ring needs three distinct corners plus the repeated first id);
        2. an explicit ``area=yes`` / ``area=no`` tag wins over everything;
        3. ``type=multipolygon`` on a closed way is an area;
        4. otherwise a tag key from :data:`AREA_KEYS` makes it an area, except
           when the only such key is a linear ``natural=*`` value such as
           ``natural=coastline``.

        Ways that fail these tests keep their tags -- ``highway=pedestrian``
        on a closed ring is still a road, it is simply not a surface.
        """
        if not self.is_closed or len(self.nodes) < 4:
            return False

        area = self.tags.get("area")
        if area is not None:
            normalised = area.strip().lower()
            if normalised in _TRUTHY:
                return True
            if normalised in _FALSY:
                return False

        if self.tags.get("type") == "multipolygon":
            return True

        present = AREA_KEYS.intersection(self.tags)
        if not present:
            return False
        if present == {"natural"} and self.tags["natural"] in LINEAR_NATURAL_VALUES:
            return False
        return True


@dataclass(frozen=True, slots=True)
class OsmRelation:
    """A group of elements, stored as ``(type, ref, role)`` member triples.

    Attributes:
        id: OSM element id.
        members: Members in document order; ``type`` is ``"node"``, ``"way"``
            or ``"relation"``, ``ref`` is the member's id and ``role`` is its
            role string (``"outer"``, ``"inner"``, ``""``, ...).
        tags: Element tags.
    """

    id: int
    members: tuple[tuple[str, int, str], ...]
    tags: Mapping[str, str] = field(default_factory=dict)

    def members_of(self, member_type: str, role: str | None = None) -> tuple[int, ...]:
        """Return member ids filtered by element type and optionally by role.

        Args:
            member_type: ``"node"``, ``"way"`` or ``"relation"``.
            role: If given, only members with exactly this role are returned.

        Returns:
            Member ids in document order.
        """
        return tuple(
            ref
            for mtype, ref, mrole in self.members
            if mtype == member_type and (role is None or mrole == role)
        )

    @property
    def type(self) -> str:
        """Return the relation's ``type`` tag, or ``""`` when untagged."""
        return self.tags.get("type", "")


# --------------------------------------------------------------------------- #
# Container
# --------------------------------------------------------------------------- #


@dataclass(slots=True, repr=False)
class OsmData:
    """A parsed OSM extract.

    Attributes:
        nodes: ``id -> (lon, lat)``. Coordinates only, because a regional
            extract holds millions of geometry nodes and one dictionary of
            tuples is an order of magnitude smaller than one object per node.
        node_tags: ``id -> tags`` for the *tagged* nodes only, which is a small
            minority (POIs, barriers, traffic signals).
        ways: All ways in document order.
        relations: All relations in document order.
        source: Path or URL the extract came from, for provenance in build
            metadata.
    """

    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)
    node_tags: dict[int, dict[str, str]] = field(default_factory=dict)
    ways: list[OsmWay] = field(default_factory=list)
    relations: list[OsmRelation] = field(default_factory=list)
    source: str = ""

    # ------------------------------------------------------------------ dunder

    def __len__(self) -> int:
        """Return the total number of elements (nodes + ways + relations)."""
        return len(self.nodes) + len(self.ways) + len(self.relations)

    def __repr__(self) -> str:
        """Return a short summary; the raw dataclass repr would be unusable."""
        return (
            f"OsmData(nodes={len(self.nodes)}, ways={len(self.ways)}, "
            f"relations={len(self.relations)}, source={self.source!r})"
        )

    # ----------------------------------------------------------------- lookups

    def way_coords(self, way: OsmWay) -> list[tuple[float, float]]:
        """Return ``[(lon, lat), ...]`` for the way's nodes.

        Node ids that are not present in the extract are skipped rather than
        raising: Overpass legitimately returns ways whose nodes fall outside
        the requested bounding box, and a road that leaves the play area must
        still render up to the boundary. The order of the way's node list is
        preserved, so a closed way stays closed when all of its nodes exist.

        Args:
            way: The way to resolve.

        Returns:
            Coordinates in degrees, in the way's own node order.
        """
        nodes = self.nodes
        return [nodes[nid] for nid in way.nodes if nid in nodes]

    def ways_with_tag(
        self,
        key: str,
        values: Iterable[str] | None = None,
    ) -> list[OsmWay]:
        """Return ways carrying ``key``, optionally restricted to given values.

        Args:
            key: Tag key, for example ``"highway"``.
            values: If given, only ways whose value for ``key`` is in this
                collection are returned. ``None`` means "any value".

        Returns:
            Matching ways in document order.
        """
        if values is None:
            return [way for way in self.ways if key in way.tags]
        wanted = {str(value) for value in values}
        return [way for way in self.ways if way.tags.get(key) in wanted]

    def relations_with_tag(
        self,
        key: str,
        values: Iterable[str] | None = None,
    ) -> list[OsmRelation]:
        """Return relations carrying ``key``, optionally restricted to values.

        Args:
            key: Tag key, for example ``"type"``.
            values: If given, only relations whose value for ``key`` is in this
                collection are returned. ``None`` means "any value".

        Returns:
            Matching relations in document order.
        """
        if values is None:
            return [rel for rel in self.relations if key in rel.tags]
        wanted = {str(value) for value in values}
        return [rel for rel in self.relations if rel.tags.get(key) in wanted]

    def count_ways_with_tag(self, key: str, values: Iterable[str] | None = None) -> int:
        """Return the number of ways matching :meth:`ways_with_tag`.

        Counting without building the list matters for regional extracts, where
        a per-feature-class report must not allocate millions of list slots.

        Args:
            key: Tag key.
            values: Optional value filter, as in :meth:`ways_with_tag`.

        Returns:
            The number of matching ways.
        """
        if values is None:
            return sum(1 for way in self.ways if key in way.tags)
        wanted = {str(value) for value in values}
        return sum(1 for way in self.ways if way.tags.get(key) in wanted)

    def node_coord(self, node_id: int) -> tuple[float, float] | None:
        """Return ``(lon, lat)`` for ``node_id``, or ``None`` when absent."""
        return self.nodes.get(node_id)

    # ------------------------------------------------------------- constructors

    @classmethod
    def empty(cls, source: str = "") -> OsmData:
        """Return an empty extract attributed to ``source``."""
        return cls(source=source)

    def stats(self) -> dict[str, Any]:
        """Return a small summary dictionary, for logs and build metadata."""
        return {
            "source": self.source,
            "nodes": len(self.nodes),
            "tagged_nodes": len(self.node_tags),
            "ways": len(self.ways),
            "closed_ways": sum(1 for way in self.ways if way.is_closed),
            "areas": sum(1 for way in self.ways if way.is_area),
            "relations": len(self.relations),
        }
