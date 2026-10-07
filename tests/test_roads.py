"""Tests for road extraction: tag parsing, width resolution and clipping."""

from __future__ import annotations

import math

import numpy as np
import pytest

from ivanovo2beam.config import DEFAULT_ROAD_CLASSES, RoadSettings
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.osm.types import OsmData, OsmWay
from ivanovo2beam.roads.build import corridor_half_width_for, roads_to_corridors
from ivanovo2beam.roads.network import (
    Road,
    decal_material_for,
    densify,
    extract_roads,
    parse_lanes,
    parse_maxspeed_kph,
    parse_width,
    road_network_stats,
    road_width_m,
    total_network_length_km,
)

BBOX = BBox(west=39.0, south=56.1, east=43.2, north=57.8)


@pytest.fixture(scope="module")
def projection():
    return make_projection(BBOX, origin_lon=40.97393, origin_lat=56.99987)


def make_osm(projection, ways):
    """Build an OsmData whose ways trace the requested world-space shapes."""
    nodes: dict[int, tuple[float, float]] = {}
    osm_ways = []
    next_id = 1
    for coords, tags, osm_id in ways:
        refs = []
        for x, y in coords:
            lon, lat = projection.world_to_geo(x, y)
            nodes[next_id] = (lon, lat)
            refs.append(next_id)
            next_id += 1
        osm_ways.append(OsmWay(id=osm_id, nodes=tuple(refs), tags=dict(tags)))
    return OsmData(nodes=nodes, node_tags={}, ways=osm_ways, relations=[], source="test")


class TestParseLanes:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("2", 2), ("1", 1), ("4", 4), ("2;1", 3), ("2|2", 4), ("3 lanes", 3), ("0", None)],
    )
    def test_values(self, value: str, expected: int | None) -> None:
        assert parse_lanes(value) == expected

    def test_none_and_empty(self) -> None:
        assert parse_lanes(None) is None
        assert parse_lanes("") is None
        assert parse_lanes("many") is None


class TestParseWidth:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("7", 7.0), ("7.5", 7.5), ("7,5", 7.5), ("7 m", 7.0), ("10.0m", 10.0)],
    )
    def test_metres(self, value: str, expected: float) -> None:
        assert parse_width(value) == pytest.approx(expected)

    def test_feet_are_converted(self) -> None:
        assert parse_width("24'") == pytest.approx(24 * 0.3048, rel=1e-6)

    @pytest.mark.parametrize("value", [None, "", "wide", "0.1", "500"])
    def test_implausible_values_are_rejected(self, value: str | None) -> None:
        assert parse_width(value) is None


class TestParseMaxspeed:
    @pytest.mark.parametrize(
        ("value", "expected"), [("60", 60.0), ("90 km/h", 90.0), ("30 mph", 30 * 1.609344)]
    )
    def test_values(self, value: str, expected: float) -> None:
        assert parse_maxspeed_kph(value) == pytest.approx(expected, rel=1e-6)

    @pytest.mark.parametrize("value", [None, "", "walk", "RU:urban", "0", "500"])
    def test_unparseable_values(self, value: str | None) -> None:
        assert parse_maxspeed_kph(value) is None


class TestRoadWidth:
    def test_class_default_wins_when_wider(self) -> None:
        spec = DEFAULT_ROAD_CLASSES["primary"]
        assert road_width_m(spec, 2) == spec.width_m

    def test_lane_count_wins_when_wider(self) -> None:
        spec = DEFAULT_ROAD_CLASSES["residential"]
        # 6 lanes at 3.5 m is 21 m, wider than the 6 m class default.
        assert road_width_m(spec, 6) == pytest.approx(21.0)


class TestDecalMaterial:
    def test_surface_overrides_class(self) -> None:
        assert decal_material_for("residential", "gravel") == "m_dirt_road_gravels"
        assert decal_material_for("residential", "dirt") == "m_dirt_road"
        assert decal_material_for("residential", "concrete") == "road1_concrete"

    def test_class_defaults(self) -> None:
        assert decal_material_for("primary") == "road_asphalt_2lane"
        assert decal_material_for("track") == "m_dirt_road_gravels"
        assert decal_material_for("footway") == "road_invisible"

    def test_unknown_class_falls_back_to_asphalt(self) -> None:
        assert decal_material_for("something_new") == "road_asphalt_2lane"


class TestExtractRoads:
    def test_skips_non_highway_ways(self, projection) -> None:
        osm = make_osm(
            projection,
            [
                ([(0, 0), (100, 0)], {"building": "yes"}, 1),
                ([(0, 0), (100, 0)], {"highway": "residential"}, 2),
            ],
        )
        roads = extract_roads(osm, projection, RoadSettings())
        assert [r.osm_id for r in roads] == [2]

    def test_skips_unknown_highway_classes(self, projection) -> None:
        osm = make_osm(projection, [([(0, 0), (100, 0)], {"highway": "monorail"}, 1)])
        assert extract_roads(osm, projection, RoadSettings()) == []

    def test_undrivable_classes_are_excluded_by_default(self, projection) -> None:
        osm = make_osm(projection, [([(0, 0), (100, 0)], {"highway": "footway"}, 1)])
        assert extract_roads(osm, projection, RoadSettings()) == []
        included = extract_roads(osm, projection, RoadSettings(), include_undrivable=True)
        assert len(included) == 1
        assert not included[0].drivable

    def test_geometry_is_projected_to_metres(self, projection) -> None:
        osm = make_osm(projection, [([(0, 0), (1000, 0)], {"highway": "residential"}, 1)])
        road = extract_roads(osm, projection, RoadSettings())[0]
        assert road.length_m == pytest.approx(1000.0, rel=1e-3)

    def test_real_tags_override_class_defaults(self, projection) -> None:
        osm = make_osm(
            projection,
            [
                (
                    [(0, 0), (500, 0)],
                    {
                        "highway": "primary",
                        "lanes": "6",
                        "width": "22",
                        "surface": "gravel",
                        "maxspeed": "40",
                        "oneway": "yes",
                        "bridge": "yes",
                        "layer": "2",
                        "name": "Test Street",
                    },
                    7,
                )
            ],
        )
        road = extract_roads(osm, projection, RoadSettings())[0]
        assert road.width_m == pytest.approx(22.0)
        assert road.lanes == 6
        assert road.surface == "gravel"
        assert road.maxspeed_kph == pytest.approx(40.0)
        assert road.oneway is True
        assert road.bridge is True
        assert road.layer == 2
        assert road.name == "Test Street"
        assert road.decal_material == "m_dirt_road_gravels"
        assert road.material_key == "Gravel"

    def test_width_from_lanes_when_no_width_tag(self, projection) -> None:
        osm = make_osm(
            projection, [([(0, 0), (100, 0)], {"highway": "residential", "lanes": "4"}, 1)]
        )
        road = extract_roads(osm, projection, RoadSettings())[0]
        assert road.width_m == pytest.approx(14.0)

    def test_roads_are_sorted_by_descending_priority(self, projection) -> None:
        osm = make_osm(
            projection,
            [
                ([(0, 0), (100, 0)], {"highway": "residential"}, 1),
                ([(0, 50), (100, 50)], {"highway": "primary"}, 2),
                ([(0, 100), (100, 100)], {"highway": "track"}, 3),
            ],
        )
        roads = extract_roads(osm, projection, RoadSettings())
        assert [r.highway for r in roads] == ["primary", "residential", "track"]

    def test_clipping_removes_geometry_outside_the_box(self, projection) -> None:
        osm = make_osm(projection, [([(-5000, 0), (5000, 0)], {"highway": "primary"}, 1)])
        clipped = extract_roads(
            osm, projection, RoadSettings(), clip_bounds=(-1000.0, -1000.0, 1000.0, 1000.0)
        )[0]
        assert clipped.points[:, 0].min() >= -1000.001
        assert clipped.points[:, 0].max() <= 1000.001
        assert clipped.length_m == pytest.approx(2000.0, rel=1e-3)

    def test_fully_outside_roads_are_dropped(self, projection) -> None:
        osm = make_osm(projection, [([(20000, 20000), (21000, 20000)], {"highway": "primary"}, 1)])
        assert extract_roads(osm, projection, RoadSettings(), clip_bounds=(-1000, -1000, 1000, 1000)) == []

    def test_short_fragments_are_dropped(self, projection) -> None:
        osm = make_osm(projection, [([(0, 0), (0.2, 0)], {"highway": "residential"}, 1)])
        assert extract_roads(osm, projection, RoadSettings(), min_length_m=1.0) == []

    def test_bridge_and_tunnel_flags(self, projection) -> None:
        osm = make_osm(
            projection,
            [
                ([(0, 0), (100, 0)], {"highway": "primary", "bridge": "viaduct"}, 1),
                ([(0, 100), (100, 100)], {"highway": "primary", "tunnel": "culvert"}, 2),
            ],
        )
        roads = {r.osm_id: r for r in extract_roads(osm, projection, RoadSettings())}
        assert roads[1].bridge and roads[1].elevation_source == "structure"
        assert roads[2].tunnel and roads[2].elevation_source == "structure"


class TestDensify:
    def test_short_segments_are_untouched(self) -> None:
        points = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
        assert densify(points, 10.0) is points

    def test_long_segments_are_split(self) -> None:
        points = np.array([[0.0, 0.0], [100.0, 0.0]])
        dense = densify(points, 10.0)
        assert dense.shape[0] == 11
        deltas = np.diff(dense, axis=0)
        assert np.hypot(deltas[:, 0], deltas[:, 1]).max() <= 10.0 + 1e-9

    def test_endpoints_are_preserved(self) -> None:
        points = np.array([[0.0, 0.0], [37.0, 11.0]])
        dense = densify(points, 5.0)
        np.testing.assert_allclose(dense[0], points[0])
        np.testing.assert_allclose(dense[-1], points[-1])

    def test_rejects_non_positive_spacing(self) -> None:
        with pytest.raises(ValueError, match="max_spacing_m must be positive"):
            densify(np.array([[0.0, 0.0], [1.0, 1.0]]), 0.0)


class TestCorridorConversion:
    def _road(self, highway="primary", **overrides) -> Road:
        spec = DEFAULT_ROAD_CLASSES[highway]
        defaults = {
            "osm_id": 1,
            "highway": highway,
            "points": np.array([[0.0, 0.0], [100.0, 0.0]]),
            "width_m": spec.width_m,
            "lanes": spec.lanes,
            "surface": spec.surface,
            "oneway": False,
            "bridge": False,
            "tunnel": False,
            "layer": 0,
            "name": "",
            "maxspeed_kph": spec.speed_kph,
            "priority": spec.priority,
            "drivable": spec.drivable,
            "material_key": "Asphalt",
            "decal_material": "road_asphalt_2lane",
        }
        defaults.update(overrides)
        return Road(**defaults)

    def test_corridor_is_wider_than_the_carriageway(self) -> None:
        settings = RoadSettings()
        corridor = roads_to_corridors([self._road()], settings)[0]
        assert corridor.corridor_half_width_m > corridor.half_width_m

    def test_motorway_gets_a_wider_corridor_than_a_track(self) -> None:
        settings = RoadSettings()
        motorway = corridor_half_width_for(self._road("motorway"), settings)
        track = corridor_half_width_for(self._road("track"), settings)
        assert motorway > track

    def test_config_corridor_width_acts_as_a_floor(self) -> None:
        settings = RoadSettings(corridor_half_width_m=50.0)
        assert corridor_half_width_for(self._road("track"), settings) >= 50.0

    def test_bridges_are_not_graded(self) -> None:
        corridors = roads_to_corridors([self._road(bridge=True)], RoadSettings())
        assert corridors[0].grade_terrain is False

    def test_tunnels_are_not_graded(self) -> None:
        corridors = roads_to_corridors([self._road(tunnel=True)], RoadSettings())
        assert corridors[0].grade_terrain is False

    def test_undrivable_roads_produce_no_corridors(self) -> None:
        road = self._road("footway", drivable=False, width_m=2.0)
        assert roads_to_corridors([road], RoadSettings()) == []

    def test_priority_is_carried_through(self) -> None:
        corridors = roads_to_corridors([self._road("primary")], RoadSettings())
        assert corridors[0].priority == DEFAULT_ROAD_CLASSES["primary"].priority


class TestNetworkStats:
    def test_stats_are_computed(self) -> None:
        def road(osm_id: int, highway: str, length: float, **kw) -> Road:
            spec = DEFAULT_ROAD_CLASSES[highway]
            base = dict(
                osm_id=osm_id,
                highway=highway,
                points=np.array([[0.0, 0.0], [length, 0.0]]),
                width_m=spec.width_m,
                lanes=spec.lanes,
                surface=spec.surface,
                oneway=False,
                bridge=False,
                tunnel=False,
                layer=0,
                name="",
                maxspeed_kph=spec.speed_kph,
                priority=spec.priority,
                drivable=spec.drivable,
                material_key="Asphalt",
                decal_material="road_asphalt_2lane",
            )
            base.update(kw)
            return Road(**base)

        roads = [
            road(1, "primary", 1000.0),
            road(2, "residential", 500.0, bridge=True),
            road(3, "footway", 200.0, drivable=False),
        ]
        stats = road_network_stats(roads)
        assert stats["roads"] == 3
        assert stats["bridges"] == 1
        assert stats["drivable"] == 2
        assert stats["total_length_km"] == pytest.approx(1.7)
        assert total_network_length_km(roads) == pytest.approx(1.7)
        assert stats["by_class"]["primary"] == 1

    def test_empty_network(self) -> None:
        stats = road_network_stats([])
        assert stats["roads"] == 0
        assert stats["total_length_km"] == 0.0


class TestRoadGeometry:
    def test_length_of_a_multi_segment_road(self) -> None:
        road = Road(
            osm_id=1,
            highway="primary",
            points=np.array([[0.0, 0.0], [300.0, 400.0], [300.0, 400.0 + 100.0]]),
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="",
            maxspeed_kph=90.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        assert road.length_m == pytest.approx(600.0)
        assert road.half_width_m == pytest.approx(5.0)

    def test_single_point_road_has_zero_length(self) -> None:
        road = Road(
            osm_id=1,
            highway="primary",
            points=np.array([[0.0, 0.0]]),
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="",
            maxspeed_kph=90.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        assert road.length_m == 0.0


class TestClipGeometry:
    """The Liang-Barsky clipper is load-bearing; check the edge cases."""

    def test_diagonal_crossing_is_clipped_at_both_ends(self, projection) -> None:
        osm = make_osm(projection, [([(-8000, -8000), (8000, 8000)], {"highway": "primary"}, 1)])
        roads = extract_roads(
            osm, projection, RoadSettings(), clip_bounds=(-1000.0, -1000.0, 1000.0, 1000.0)
        )
        assert len(roads) == 1
        pts = roads[0].points
        assert pts[:, 0].min() >= -1000.001 and pts[:, 0].max() <= 1000.001
        assert pts[:, 1].min() >= -1000.001 and pts[:, 1].max() <= 1000.001

    def test_road_entering_and_leaving_the_box_twice(self, projection) -> None:
        """An S-shaped way leaves and re-enters; each piece must be clipped."""
        coords = [(-3000, 0), (3000, 0), (3000, 3000), (-3000, 3000)]
        osm = make_osm(projection, [(coords, {"highway": "primary"}, 1)])
        roads = extract_roads(
            osm, projection, RoadSettings(), clip_bounds=(-1000.0, -1000.0, 1000.0, 1000.0)
        )
        # The longest surviving piece is returned; it must stay inside the box.
        assert len(roads) == 1
        pts = roads[0].points
        assert pts[:, 0].max() <= 1000.001
        assert pts[:, 1].max() <= 1000.001
        assert roads[0].length_m > 0

    def test_axis_aligned_road_along_the_boundary(self, projection) -> None:
        osm = make_osm(projection, [([(-1000, 500), (1000, 500)], {"highway": "primary"}, 1)])
        roads = extract_roads(
            osm, projection, RoadSettings(), clip_bounds=(-1000.0, -1000.0, 1000.0, 1000.0)
        )
        assert len(roads) == 1
        assert roads[0].length_m == pytest.approx(2000.0, rel=1e-3)

    def test_no_clipping_leaves_geometry_unchanged(self, projection) -> None:
        osm = make_osm(projection, [([(-5000, 0), (5000, 0)], {"highway": "primary"}, 1)])
        road = extract_roads(osm, projection, RoadSettings())[0]
        assert road.length_m == pytest.approx(10_000.0, rel=1e-4)
        assert math.isclose(road.points[0, 0], -5000.0, abs_tol=0.01)
