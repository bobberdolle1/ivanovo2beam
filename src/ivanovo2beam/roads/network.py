"""Turning OpenStreetMap ways into a drivable road network.

The output of this stage is deliberately not geometry for BeamNG yet. It is a
list of :class:`Road` objects in the project's metric world plane, each carrying
the attributes that later stages need: how wide the carriageway is, whether it
is drivable at all, whether it is a bridge, and how it ranks against other roads
at a junction.

Attribute precedence follows the project rule that **real OSM tags always win**:

1. an explicit ``width`` tag;
2. ``lanes`` multiplied by a standard lane width;
3. the class default from the region config.

Only ``highway=*`` ways are considered. They are clipped to the build extent so
a way that leaves the mapped area does not drag a corridor across the whole
terrain.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from ivanovo2beam.config import RoadClassSpec, RoadSettings
    from ivanovo2beam.gis.projection import RegionProjection
    from ivanovo2beam.osm.types import OsmData, OsmWay

#: Width of a single traffic lane used when a way has ``lanes`` but no ``width``.
DEFAULT_LANE_WIDTH_M = 3.5

#: BeamNG decal-road materials that ship with the base game in
#: ``art/shapes/common/decalroads/main.materials.json``. Referencing these by
#: name is the intended modding workflow: no game asset is copied.
DECAL_MATERIALS: dict[str, str] = {
    "asphalt": "road_asphalt_2lane",
    "concrete": "road1_concrete",
    "dirt": "m_dirt_road",
    "gravel": "m_dirt_road_gravels",
    "invisible": "road_invisible",
}

#: OSM ``highway`` classes that map onto each decal material.
_CLASS_TO_DECAL: dict[str, str] = {
    "motorway": "asphalt",
    "motorway_link": "asphalt",
    "trunk": "asphalt",
    "trunk_link": "asphalt",
    "primary": "asphalt",
    "primary_link": "asphalt",
    "secondary": "asphalt",
    "secondary_link": "asphalt",
    "tertiary": "asphalt",
    "tertiary_link": "asphalt",
    "unclassified": "asphalt",
    "residential": "asphalt",
    "living_street": "asphalt",
    "service": "asphalt",
    "track": "gravel",
    "path": "dirt",
    "pedestrian": "concrete",
    "footway": "invisible",
    "cycleway": "invisible",
    "steps": "invisible",
}

_TRUTHY = {"yes", "true", "1"}
_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

# ---------------------------------------------------------------------------
# Curve filleting (adapted from MapNG roadCurves.js, MIT License,
# Copyright (c) 2026 Nikki Luzader)
# ---------------------------------------------------------------------------

#: Maximum circular fillet radius (metres) by OSM highway class.
RADIUS_BY_HIGHWAY: dict[str, float] = {
    "motorway": 800.0,
    "trunk": 600.0,
    "primary": 400.0,
    "secondary": 300.0,
    "tertiary": 200.0,
    "motorway_link": 120.0,
    "trunk_link": 120.0,
    "primary_link": 100.0,
    "secondary_link": 100.0,
    "tertiary_link": 80.0,
    "unclassified": 120.0,
    "residential": 80.0,
    "road": 80.0,
    "living_street": 30.0,
    "service": 30.0,
    "track": 60.0,
    "busway": 80.0,
    "raceway": 120.0,
}
DEFAULT_FILLET_RADIUS_M = 80.0
#: Maximum distance (metres) a corner fillet may cut inside the mapped vertex.
MAX_CUT_M = 3.0
_MAX_SEGMENT_SHARE = 0.5
_FIXED_SEGMENT_SHARE = 0.9
_MIN_TURN_RAD = math.radians(1.0)
_MAX_TURN_RAD = math.radians(170.0)
_ARC_STEP_RAD = math.radians(5.0)
_ARC_STEP_M = 1.0


@dataclass(frozen=True, slots=True)
class Road:
    """One OSM way interpreted as a road, in world metres.

    Attributes:
        osm_id: The OSM way id.
        highway: The ``highway`` tag value.
        points: ``(n, 2)`` centreline vertices in world metres.
        width_m: Carriageway width in metres.
        lanes: Lane count.
        surface: Surface key, e.g. ``asphalt`` or ``gravel``.
        oneway: Whether traffic is one-way.
        bridge: Whether the way is a bridge.
        tunnel: Whether the way is a tunnel.
        layer: OSM ``layer`` value; higher crosses over lower.
        name: Street name when tagged.
        maxspeed_kph: Speed limit when tagged, otherwise the class default.
        priority: Junction precedence from the class spec.
        drivable: Whether the way should be rendered as a drivable road.
        material_key: Terrain material painted on the carriageway.
        decal_material: BeamNG decal material name.
        access: Access restrictions (e.g. "private", "no").
        service: Service road classification (e.g. "driveway", "parking_aisle").
    """

    osm_id: int
    highway: str
    points: NDArray[np.float64]
    width_m: float
    lanes: int
    surface: str
    oneway: bool
    bridge: bool
    tunnel: bool
    layer: int
    name: str
    maxspeed_kph: float
    priority: int
    drivable: bool
    material_key: str
    decal_material: str
    access: str = ""
    service: str = ""

    @property
    def half_width_m(self) -> float:
        """Half the carriageway width in metres."""
        return self.width_m / 2.0

    @property
    def length_m(self) -> float:
        """Centreline length in metres."""
        if self.points.shape[0] < 2:
            return 0.0
        deltas = np.diff(self.points, axis=0)
        return float(np.hypot(deltas[:, 0], deltas[:, 1]).sum())

    @property
    def elevation_source(self) -> str:
        """Where this road's elevation comes from.

        Returns:
            ``"dem"`` for the default case where the road is graded into the
            terrain, or ``"structure"`` for bridges and tunnels, which keep
            their own elevation and must not flatten the ground.
        """
        return "structure" if (self.bridge or self.tunnel) else "dem"


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------


def parse_lanes(value: str | None) -> int | None:
    """Parse an OSM ``lanes`` tag.

    Handles plain counts (``"2"``), direction-split lists (``"2;1"``), tagged
    counts (``"2|2"``) and the common ``forward``/``backward`` suffixes.

    Args:
        value: The raw tag value, or ``None``.

    Returns:
        A positive lane count, or ``None`` when unparseable.
    """
    if not value:
        return None
    numbers = [int(n) for n in _INT_RE.findall(value)]
    positive = [n for n in numbers if n > 0]
    if not positive:
        return None
    # "2;1" means two lanes one way and one the other; the total is what matters.
    return sum(positive) if len(positive) > 1 else positive[0]


def parse_width(value: str | None) -> float | None:
    """Parse an OSM ``width`` tag into metres.

    Accepts ``"7"``, ``"7.5"``, ``"7,5"`` and ``"7 m"``. Values in feet
    (``"24'"``) are converted.

    Args:
        value: The raw tag value, or ``None``.

    Returns:
        A width in metres, or ``None`` when unparseable or implausible.
    """
    if not value:
        return None
    text = value.strip().lower()
    if text.endswith("'") or text.endswith("ft"):
        match = _FLOAT_RE.search(text)
        if match is None:
            return None
        feet = float(match.group().replace(",", "."))
        metres = feet * 0.3048
        return metres if 0.5 <= metres <= 60.0 else None
    match = _FLOAT_RE.search(text)
    if match is None:
        return None
    metres = float(match.group().replace(",", "."))
    # Reject nonsense: a "road" narrower than a person or wider than a runway.
    return metres if 0.5 <= metres <= 60.0 else None


def parse_maxspeed_kph(value: str | None) -> float | None:
    """Parse an OSM ``maxspeed`` tag into km/h.

    Args:
        value: The raw tag value, or ``None``.

    Returns:
        Speed in km/h, or ``None`` for unparseable or non-numeric values such
        as ``"RU:urban"`` or ``"walk"``.
    """
    if not value:
        return None
    text = value.strip().lower()
    match = _FLOAT_RE.search(text)
    if match is None:
        return None
    speed = float(match.group().replace(",", "."))
    if "mph" in text:
        speed *= 1.609344
    return speed if 0.0 < speed <= 200.0 else None


def is_truthy(value: str | None) -> bool:
    """Return whether an OSM tag value means "yes"."""
    return (value or "").strip().lower() in _TRUTHY


def road_width_m(
    spec: RoadClassSpec,
    lanes: int,
    *,
    lane_width_m: float = DEFAULT_LANE_WIDTH_M,
) -> float:
    """Return the carriageway width implied by a class and lane count.

    Args:
        spec: Class defaults from the region config.
        lanes: Lane count.
        lane_width_m: Width assumed per lane.

    Returns:
        Width in metres: at least the class width, and at least ``lanes`` lanes
        wide.
    """
    return max(spec.width_m, lanes * lane_width_m)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _clip_points(
    points: NDArray[np.float64],
    bounds: tuple[float, float, float, float] | None,
) -> NDArray[np.float64] | None:
    """Clip a polyline to an axis-aligned metric box.

    A simple Liang-Barsky segment clip, so the module does not pull in shapely
    for what is a few lines of arithmetic, and so a single long way that leaves
    the area does not produce a corridor stretching to the horizon.

    Args:
        points: ``(n, 2)`` polyline in world metres.
        bounds: ``(x0, y0, x1, y1)``, or ``None`` to skip clipping.

    Returns:
        The clipped polyline, or ``None`` if it lies entirely outside.
    """
    if bounds is None:
        return points
    x0, y0, x1, y1 = bounds
    pieces: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    def inside(px: float, py: float) -> bool:
        return x0 <= px <= x1 and y0 <= py <= y1

    for i in range(points.shape[0] - 1):
        ax, ay = float(points[i, 0]), float(points[i, 1])
        bx, by = float(points[i + 1, 0]), float(points[i + 1, 1])
        dx, dy = bx - ax, by - ay
        t_enter, t_exit = 0.0, 1.0
        reject = False
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    reject = True
                    break
            else:
                r = q / p
                if p < 0:
                    t_enter = max(t_enter, r)
                else:
                    t_exit = min(t_exit, r)
        if reject or t_enter > t_exit:
            if len(current) >= 2:
                pieces.append(current)
            current = []
            continue
        enter_x, enter_y = ax + t_enter * dx, ay + t_enter * dy
        exit_x, exit_y = ax + t_exit * dx, ay + t_exit * dy
        if not current or current[-1] != (enter_x, enter_y):
            current.append((enter_x, enter_y))
        current.append((exit_x, exit_y))
        if t_exit < 1.0:
            if len(current) >= 2:
                pieces.append(current)
            current = []

    if len(current) >= 2:
        pieces.append(current)
    if not pieces:
        return None
    longest = max(pieces, key=len)
    if len(longest) < 2:
        return None
    return np.asarray(longest, dtype=np.float64)


def _node_key_xy(pt: NDArray[np.float64] | tuple[float, float]) -> tuple[int, int]:
    """Quantize a world XY point to 1 cm for topological junction matching."""
    return (round(float(pt[0]) * 100.0), round(float(pt[1]) * 100.0))


def _turn_angle_xy(a: NDArray[np.float64], b: NDArray[np.float64], c: NDArray[np.float64]) -> float:
    ux = float(b[0] - a[0])
    uy = float(b[1] - a[1])
    vx = float(c[0] - b[0])
    vy = float(c[1] - b[1])
    lu = math.hypot(ux, uy)
    lv = math.hypot(vx, vy)
    if lu < 1e-9 or lv < 1e-9:
        return 0.0
    cos_t = max(-1.0, min(1.0, (ux * vx + uy * vy) / (lu * lv)))
    return math.acos(cos_t)


def _through_curve_xy(
    b: NDArray[np.float64],
    ux: float,
    uy: float,
    vx: float,
    vy: float,
    tangent: float,
    turn: float,
) -> list[tuple[float, float]]:
    """Two cubic Hermite pieces keeping shared vertex ``b`` pinned while curving through it."""
    wx = ux + vx
    wy = uy + vy
    wl = math.hypot(wx, wy) or 1.0
    wx /= wl
    wy /= wl
    steps = max(2, math.ceil(max((turn * 0.5) / _ARC_STEP_RAD, tangent / _ARC_STEP_M)))
    bx, by = float(b[0]), float(b[1])

    def hermite(
        p0x: float,
        p0y: float,
        m0x: float,
        m0y: float,
        p1x: float,
        p1y: float,
        m1x: float,
        m1y: float,
        s: float,
    ) -> tuple[float, float]:
        s2 = s * s
        s3 = s2 * s
        h00 = 2.0 * s3 - 3.0 * s2 + 1.0
        h10 = s3 - 2.0 * s2 + s
        h01 = -2.0 * s3 + 3.0 * s2
        h11 = s3 - s2
        return (
            bx + h00 * p0x + h10 * m0x + h01 * p1x + h11 * m1x,
            by + h00 * p0y + h10 * m0y + h01 * p1y + h11 * m1y,
        )

    pts: list[tuple[float, float]] = []
    for k in range(steps + 1):
        pts.append(
            hermite(
                -ux * tangent,
                -uy * tangent,
                ux * tangent,
                uy * tangent,
                0.0,
                0.0,
                wx * tangent,
                wy * tangent,
                k / steps,
            )
        )
    for k in range(1, steps + 1):
        pts.append(
            hermite(
                0.0,
                0.0,
                wx * tangent,
                wy * tangent,
                vx * tangent,
                vy * tangent,
                vx * tangent,
                vy * tangent,
                k / steps,
            )
        )
    pts[steps] = (bx, by)
    return pts


def _fillet_arc_xy(
    a: NDArray[np.float64],
    b: NDArray[np.float64],
    c: NDArray[np.float64],
    radius: float,
    share_in: float,
    share_out: float,
    *,
    pinned: bool = False,
    max_cut_m: float = MAX_CUT_M,
) -> list[tuple[float, float]] | None:
    """Replace corner ``a -> b -> c`` with a tangent circular arc (or Hermite if pinned)."""
    ax = float(a[0] - b[0])
    ay = float(a[1] - b[1])
    cx = float(c[0] - b[0])
    cy = float(c[1] - b[1])
    len_in = math.hypot(ax, ay)
    len_out = math.hypot(cx, cy)
    if len_in < 1e-6 or len_out < 1e-6:
        return None
    ux = -ax / len_in
    uy = -ay / len_in
    vx = cx / len_out
    vy = cy / len_out
    turn = math.acos(max(-1.0, min(1.0, ux * vx + uy * vy)))
    if turn < _MIN_TURN_RAD or turn > _MAX_TURN_RAD:
        return None

    half = math.tan(turn * 0.5)
    cos_half = math.cos(turn * 0.5)
    if half < 1e-9 or abs(1.0 - cos_half) < 1e-9:
        return None
    cut_limited = max_cut_m / (1.0 / cos_half - 1.0)
    tangent = min(min(radius, cut_limited) * half, share_in * len_in, share_out * len_out)
    if tangent <= 1e-4:
        return None
    if pinned:
        return _through_curve_xy(b, ux, uy, vx, vy, tangent, turn)

    r = tangent / half
    sx = -ux * tangent
    sy = -uy * tangent
    cross = ux * vy - uy * vx
    side = 1.0 if cross > 0.0 else (-1.0 if cross < 0.0 else 1.0)
    ox = sx - uy * r * side
    oy = sy + ux * r * side
    a0 = math.atan2(sy - oy, sx - ox)
    steps = max(2, math.ceil(max(turn / _ARC_STEP_RAD, (r * turn) / _ARC_STEP_M)))
    bx, by = float(b[0]), float(b[1])
    arc: list[tuple[float, float]] = []
    for k in range(steps + 1):
        if k == 0:
            x, y = sx, sy
        elif k == steps:
            x, y = vx * tangent, vy * tangent
        else:
            ang = a0 + side * turn * (k / steps)
            x = ox + r * math.cos(ang)
            y = oy + r * math.sin(ang)
        arc.append((bx + x, by + y))
    return arc


def round_road_corners(
    roads: Sequence[Road],
    *,
    max_cut_m: float = MAX_CUT_M,
) -> list[Road]:
    """Round sharp OSM polyline corners into tangent circular arcs.

    Shared junction vertices (3+ ways or interior connections) stay pinned in XY
    while roads bend smoothly through them; pass-through two-way OSM splits
    round across the shared node. Bridges and tunnels are kept exact.

    Args:
        roads: Input roads in world metres.
        max_cut_m: Maximum inward offset from the mapped corner vertex.

    Returns:
        Updated list of :class:`Road` instances with smoothed corners.
    """
    if not roads:
        return []

    node_use: dict[tuple[int, int], int] = {}
    end_use: dict[tuple[int, int], list[tuple[int, bool]]] = {}
    for fi, road in enumerate(roads):
        if not road.drivable or road.tunnel or road.points.shape[0] < 2:
            continue
        pts = road.points
        seen_keys: set[tuple[int, int]] = set()
        for idx in range(pts.shape[0]):
            k = _node_key_xy(pts[idx])
            if k not in seen_keys:
                node_use[k] = node_use.get(k, 0) + 1
                seen_keys.add(k)
            if idx == 0 or idx == pts.shape[0] - 1:
                end_use.setdefault(k, []).append((fi, idx == 0))

    def is_structural(road: Road) -> bool:
        return bool(road.bridge or road.tunnel)

    join_of: dict[tuple[int, int], tuple[tuple[int, bool], tuple[int, bool]]] = {}
    for k, ends in end_use.items():
        if len(ends) != 2 or node_use.get(k, 0) != 2:
            continue
        (fi0, s0), (fi1, s1) = ends
        if fi0 == fi1:
            continue
        r0, r1 = roads[fi0], roads[fi1]
        if not r0.drivable or not r1.drivable or is_structural(r0) or is_structural(r1):
            continue
        join_of[k] = ((fi0, s0), (fi1, s1))

    def curves_at(pts: NDArray[np.float64], j: int) -> bool:
        if j == 0 or j == pts.shape[0] - 1:
            return _node_key_xy(pts[j]) in join_of
        turn = _turn_angle_xy(pts[j - 1], pts[j], pts[j + 1])
        return _MIN_TURN_RAD <= turn <= _MAX_TURN_RAD

    def share_for(pts: NDArray[np.float64], j: int) -> float:
        return _MAX_SEGMENT_SHARE if curves_at(pts, j) else _FIXED_SEGMENT_SHARE

    join_halves: dict[tuple[int, bool], list[tuple[float, float]]] = {}
    for ((fi0, s0), (fi1, s1)) in join_of.values():
        g0 = roads[fi0].points
        g1 = roads[fi1].points
        j0 = 1 if s0 else g0.shape[0] - 2
        j1 = 1 if s1 else g1.shape[0] - 2
        node = g0[0] if s0 else g0[-1]
        rad = min(
            RADIUS_BY_HIGHWAY.get(roads[fi0].highway, DEFAULT_FILLET_RADIUS_M),
            RADIUS_BY_HIGHWAY.get(roads[fi1].highway, DEFAULT_FILLET_RADIUS_M),
        )
        arc = _fillet_arc_xy(
            g0[j0],
            node,
            g1[j1],
            rad,
            share_for(g0, j0),
            share_for(g1, j1),
            pinned=False,
            max_cut_m=max_cut_m,
        )
        if not arc:
            continue
        mid = len(arc) // 2
        join_halves[(fi0, s0)] = arc[: mid + 1]
        join_halves[(fi1, s1)] = list(reversed(arc[mid:]))

    out: list[Road] = []
    for fi, road in enumerate(roads):
        if not road.drivable or is_structural(road) or road.points.shape[0] < 2:
            out.append(road)
            continue
        pts = road.points
        rad = RADIUS_BY_HIGHWAY.get(road.highway, DEFAULT_FILLET_RADIUS_M)
        new_pts: list[tuple[float, float]] = []
        changed = False

        def push(*coords: tuple[float, float], target: list[tuple[float, float]] = new_pts) -> None:
            for px, py in coords:
                if target and math.hypot(target[-1][0] - px, target[-1][1] - py) < 1e-4:
                    continue
                target.append((px, py))

        start_half = join_halves.get((fi, True))
        if start_half:
            push(*reversed(start_half))
            changed = True
        else:
            push((float(pts[0, 0]), float(pts[0, 1])))

        for i in range(1, pts.shape[0] - 1):
            k = _node_key_xy(pts[i])
            pinned = node_use.get(k, 0) > 1
            arc = _fillet_arc_xy(
                pts[i - 1],
                pts[i],
                pts[i + 1],
                rad,
                share_for(pts, i - 1),
                share_for(pts, i + 1),
                pinned=pinned,
                max_cut_m=max_cut_m,
            )
            if not arc:
                push((float(pts[i, 0]), float(pts[i, 1])))
                continue
            push(*arc)
            changed = True

        end_half = join_halves.get((fi, False))
        if end_half:
            push(*end_half)
            changed = True
        else:
            push((float(pts[-1, 0]), float(pts[-1, 1])))

        if changed and len(new_pts) >= 2:
            out.append(replace(road, points=np.asarray(new_pts, dtype=np.float64)))
        else:
            out.append(road)

    return out


def extract_roads(
    osm: OsmData,
    projection: RegionProjection,
    settings: RoadSettings,
    *,
    clip_bounds: tuple[float, float, float, float] | None = None,
    include_undrivable: bool = False,
    min_length_m: float = 1.0,
    smooth_corners: bool = True,
) -> list[Road]:
    """Extract roads from parsed OSM data.

    Args:
        osm: Parsed OSM extract.
        projection: World plane transform.
        settings: Road settings, supplying per-class defaults.
        clip_bounds: Optional ``(x0, y0, x1, y1)`` world box to clip to.
        include_undrivable: Whether to keep footways, steps and similar. They
            are useful for rendering but must never be graded as roads.
        min_length_m: Discard fragments shorter than this.

    Returns:
        Roads sorted by descending priority, so higher-class roads are graded
        last and therefore win at junctions.
    """
    roads: list[Road] = []
    unknown: dict[str, int] = {}

    for way in osm.ways:
        highway = way.tags.get("highway")
        if not highway:
            continue
        spec = settings.spec_for(highway)
        if spec is None:
            unknown[highway] = unknown.get(highway, 0) + 1
            continue
        if not spec.drivable and not include_undrivable:
            continue

        coords = osm.way_coords(way)
        if len(coords) < 2:
            continue

        lons = np.asarray([c[0] for c in coords], dtype=np.float64)
        lats = np.asarray([c[1] for c in coords], dtype=np.float64)
        xs, ys = projection.geo_to_world_array(lons, lats)
        points = np.column_stack((xs, ys))
        clipped = _clip_points(points, clip_bounds)
        if clipped is None:
            continue

        # A clip can leave a straight run of collinear points; that is harmless
        # for grading but wasteful for decals, so collapse exact duplicates.
        keep = np.ones(clipped.shape[0], dtype=bool)
        if clipped.shape[0] > 2:
            deltas = np.diff(clipped, axis=0)
            keep[1:] = np.hypot(deltas[:, 0], deltas[:, 1]) > 1e-6
        clipped = clipped[keep]
        if clipped.shape[0] < 2:
            continue

        lanes = parse_lanes(way.tags.get("lanes")) or spec.lanes
        width = parse_width(way.tags.get("width")) or road_width_m(spec, lanes)
        surface = (way.tags.get("surface") or spec.surface).strip().lower()
        maxspeed = parse_maxspeed_kph(way.tags.get("maxspeed")) or spec.speed_kph
        oneway_tag = (way.tags.get("oneway") or "").strip().lower()
        if oneway_tag == "-1":
            clipped = np.ascontiguousarray(clipped[::-1])
        access = (way.tags.get("access") or "").strip().lower()
        service = (way.tags.get("service") or "").strip().lower()
        layer_text = way.tags.get("layer") or "0"
        layer_match = _INT_RE.search(layer_text)

        road = Road(
            osm_id=way.id,
            highway=highway,
            points=clipped,
            width_m=width,
            lanes=lanes,
            surface=surface,
            oneway=oneway_tag in _TRUTHY or oneway_tag == "-1",
            bridge=is_truthy(way.tags.get("bridge"))
            or (way.tags.get("bridge") or "").strip().lower() in {"viaduct", "boardwalk"},
            tunnel=is_truthy(way.tags.get("tunnel"))
            or (way.tags.get("tunnel") or "").strip().lower() in {"culvert", "building_passage"},
            layer=int(layer_match.group()) if layer_match else 0,
            name=way.tags.get("name", ""),
            maxspeed_kph=maxspeed,
            priority=spec.priority,
            drivable=spec.drivable,
            material_key=surface_to_material_key(surface, highway),
            decal_material=decal_material_for(highway, surface),
            access=access,
            service=service,
        )
        if road.length_m < min_length_m:
            continue
        roads.append(road)

    roads.sort(key=lambda r: (-r.priority, r.osm_id))
    if smooth_corners and roads:
        roads = round_road_corners(roads)
    return roads


def decal_material_for(highway: str, surface: str | None = None) -> str:
    """Return the BeamNG decal material for an OSM road.

    Args:
        highway: The ``highway`` tag value.
        surface: The ``surface`` tag value, when present.

    Returns:
        A decal material name from the base-game shared library.
    """
    if surface:
        key = surface.strip().lower()
        if key in {"gravel", "fine_gravel", "pebblestone", "compacted"}:
            return DECAL_MATERIALS["gravel"]
        if key in {"dirt", "ground", "earth", "mud", "unpaved", "grass"}:
            return DECAL_MATERIALS["dirt"]
        if key in {"concrete", "paving_stones", "concrete:plates"}:
            return DECAL_MATERIALS["concrete"]
    return DECAL_MATERIALS[_CLASS_TO_DECAL.get(highway, "asphalt")]


def surface_to_material_key(surface: str, highway: str) -> str:
    """Return the terrain material key for a road surface.

    Args:
        surface: Lower-cased OSM ``surface`` value.
        highway: The ``highway`` tag value.

    Returns:
        A key from :data:`ivanovo2beam.beamng.materials.MATERIAL_LIBRARY`.
    """
    if surface in {"gravel", "fine_gravel", "pebblestone", "compacted"}:
        return "Gravel"
    if surface in {"dirt", "ground", "earth", "mud", "unpaved", "grass"}:
        return "Dirt"
    if surface in {"concrete", "paving_stones", "concrete:plates"}:
        return "Concrete"
    if surface == "sand":
        return "Sand"
    if surface == "rock":
        return "Rock"
    if surface == "asphalt":
        return "Asphalt"
    return "Gravel" if highway in {"track", "path"} else "Asphalt"


def road_network_stats(roads: Sequence[Road]) -> dict[str, Any]:
    """Summarise a road network for logging and metadata.

    Args:
        roads: Extracted roads.

    Returns:
        Counts and totals keyed for a build report.
    """
    by_class: dict[str, int] = {}
    length_by_class: dict[str, float] = {}
    total = 0.0
    for road in roads:
        by_class[road.highway] = by_class.get(road.highway, 0) + 1
        length = road.length_m
        length_by_class[road.highway] = length_by_class.get(road.highway, 0.0) + length
        total += length
    return {
        "roads": len(roads),
        "total_length_km": round(total / 1000.0, 3),
        "by_class": dict(sorted(by_class.items(), key=lambda kv: -kv[1])),
        "length_km_by_class": {
            k: round(v / 1000.0, 3) for k, v in sorted(length_by_class.items(), key=lambda kv: -kv[1])
        },
        "bridges": sum(1 for r in roads if r.bridge),
        "tunnels": sum(1 for r in roads if r.tunnel),
        "drivable": sum(1 for r in roads if r.drivable),
    }


def total_network_length_km(roads: Sequence[Road]) -> float:
    """Return the summed centreline length of a network, in kilometres."""
    return sum(r.length_m for r in roads) / 1000.0


def densify(points: NDArray[np.float64], max_spacing_m: float) -> NDArray[np.float64]:
    """Insert vertices so no segment exceeds ``max_spacing_m``.

    OSM nodes can be hundreds of metres apart on straight rural roads. BeamNG
    decal roads follow terrain between nodes, so a long unbroken segment would
    float above or cut through the ground wherever that terrain moves.

    Args:
        points: ``(n, 2)`` polyline.
        max_spacing_m: Maximum permitted segment length in metres.

    Returns:
        A densified polyline. Returns the input unchanged when it is already
        dense enough.
    """
    if max_spacing_m <= 0:
        raise ValueError(f"max_spacing_m must be positive, got {max_spacing_m}")
    if points.shape[0] < 2:
        return points

    deltas = np.diff(points, axis=0)
    lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    if float(lengths.max()) <= max_spacing_m:
        return points

    out: list[tuple[float, float]] = [(float(points[0, 0]), float(points[0, 1]))]
    for i, length in enumerate(lengths):
        if length <= max_spacing_m:
            out.append((float(points[i + 1, 0]), float(points[i + 1, 1])))
            continue
        steps = max(2, math.ceil(length / max_spacing_m))
        ax, ay = float(points[i, 0]), float(points[i, 1])
        bx, by = float(points[i + 1, 0]), float(points[i + 1, 1])
        for step in range(1, steps + 1):
            t = step / steps
            out.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return np.asarray(out, dtype=np.float64)


def _way_is_area(way: OsmWay) -> bool:
    """Return whether a way is a closed area rather than a line."""
    return way.is_closed
