"""Tests for road extraction: tag parsing, width resolution and clipping."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from ivanovo2beam.config import DEFAULT_ROAD_CLASSES, RoadSettings
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.osm.types import OsmData, OsmWay
from ivanovo2beam.roads.build import (
    corridor_half_width_for,
    generate_lane_markings,
    road_to_decal_object,
    road_to_decal_objects,
    roads_to_corridors,
)
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
        osm = make_osm(projection, [([(0, 0), (100, 0)], {"highway": "residential", "lanes": "4"}, 1)])
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

    def test_paved_roads_use_grass_shoulders_unpaved_use_gravel_or_dirt(self) -> None:
        paved = self._road("primary", material_key="Asphalt")
        concrete = self._road("residential", material_key="Concrete")
        gravel = self._road("track", material_key="Gravel")
        dirt = self._road("track", material_key="Dirt")
        corridors = roads_to_corridors([paved, concrete, gravel, dirt], RoadSettings())
        assert [c.shoulder_material_key for c in corridors] == ["Grass", "Grass", "Gravel", "Dirt"]


class TestNetworkStats:
    def test_stats_are_computed(self) -> None:
        def road(osm_id: int, highway: str, length: float, **kw) -> Road:
            spec = DEFAULT_ROAD_CLASSES[highway]
            base = {
                "osm_id": osm_id,
                "highway": highway,
                "points": np.array([[0.0, 0.0], [length, 0.0]]),
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
        roads = extract_roads(osm, projection, RoadSettings(), clip_bounds=(-1000.0, -1000.0, 1000.0, 1000.0))
        assert len(roads) == 1
        pts = roads[0].points
        assert pts[:, 0].min() >= -1000.001 and pts[:, 0].max() <= 1000.001
        assert pts[:, 1].min() >= -1000.001 and pts[:, 1].max() <= 1000.001

    def test_road_entering_and_leaving_the_box_twice(self, projection) -> None:
        """An S-shaped way leaves and re-enters; each piece must be clipped."""
        coords = [(-3000, 0), (3000, 0), (3000, 3000), (-3000, 3000)]
        osm = make_osm(projection, [(coords, {"highway": "primary"}, 1)])
        roads = extract_roads(osm, projection, RoadSettings(), clip_bounds=(-1000.0, -1000.0, 1000.0, 1000.0))
        # The longest surviving piece is returned; it must stay inside the box.
        assert len(roads) == 1
        pts = roads[0].points
        assert pts[:, 0].max() <= 1000.001
        assert pts[:, 1].max() <= 1000.001
        assert roads[0].length_m > 0

    def test_axis_aligned_road_along_the_boundary(self, projection) -> None:
        osm = make_osm(projection, [([(-1000, 500), (1000, 500)], {"highway": "primary"}, 1)])
        roads = extract_roads(osm, projection, RoadSettings(), clip_bounds=(-1000.0, -1000.0, 1000.0, 1000.0))
        assert len(roads) == 1
        assert roads[0].length_m == pytest.approx(2000.0, rel=1e-3)

    def test_no_clipping_leaves_geometry_unchanged(self, projection) -> None:
        osm = make_osm(projection, [([(-5000, 0), (5000, 0)], {"highway": "primary"}, 1)])
        road = extract_roads(osm, projection, RoadSettings())[0]
        assert road.length_m == pytest.approx(10_000.0, rel=1e-4)
        assert math.isclose(road.points[0, 0], -5000.0, abs_tol=0.01)


class TestRoadWidthAndDirection:
    def test_decal_nodes_receive_full_width(self) -> None:
        road = Road(
            osm_id=42,
            highway="primary",
            points=np.array([[0.0, 0.0], [50.0, 0.0]]),
            width_m=8.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="Test St",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        obj = road_to_decal_object(road, "test_level")
        assert obj is not None
        nodes = obj["nodes"]
        # In DecalRoad nodes [x, y, z, width], width must be full road.width_m (8.0), not half width (4.0)
        for node in nodes:
            assert node[3] == pytest.approx(8.0)

    def test_oneway_negative_one_reverses_points(self, projection) -> None:
        osm = make_osm(
            projection,
            [([(0, 0), (100, 50), (200, 100)], {"highway": "residential", "oneway": "-1"}, 1)],
        )
        roads = extract_roads(osm, projection, RoadSettings())
        assert len(roads) == 1
        road = roads[0]
        assert road.oneway is True
        # First point should now be at x ~ 200 (reversed from OSM order)
        assert road.points[0, 0] > road.points[-1, 0]
        assert road.points[0, 0] == pytest.approx(200.0, abs=1.0)
        assert road.points[-1, 0] == pytest.approx(0.0, abs=1.0)

    def test_oneway_yes_preserves_point_order(self, projection) -> None:
        osm = make_osm(
            projection,
            [([(0, 0), (100, 50), (200, 100)], {"highway": "residential", "oneway": "yes"}, 1)],
        )
        roads = extract_roads(osm, projection, RoadSettings())
        assert len(roads) == 1
        road = roads[0]
        assert road.oneway is True
        assert road.points[0, 0] < road.points[-1, 0]
        assert road.points[0, 0] == pytest.approx(0.0, abs=1.0)


class TestAITrafficAndNavgraph:
    def test_gated_road_defaults_and_overrides(self) -> None:
        base_kwargs = {
            "osm_id": 100,
            "highway": "residential",
            "points": np.array([[0.0, 0.0], [50.0, 0.0]]),
            "width_m": 6.0,
            "lanes": 2,
            "surface": "asphalt",
            "oneway": False,
            "bridge": False,
            "tunnel": False,
            "layer": 0,
            "name": "",
            "maxspeed_kph": 60.0,
            "priority": 50,
            "drivable": True,
            "material_key": "Asphalt",
            "decal_material": "road_asphalt_2lane",
        }
        # Public road -> not gated
        public_road = Road(**base_kwargs)
        obj_pub = road_to_decal_object(public_road, "test_level")
        assert obj_pub is not None
        assert obj_pub["gatedRoad"] is False

        # Private access -> gated
        priv_road = Road(**{**base_kwargs, "access": "private"})
        obj_priv = road_to_decal_object(priv_road, "test_level")
        assert obj_priv is not None
        assert obj_priv["gatedRoad"] is True

        # Driveway service -> gated
        driveway_road = Road(**{**base_kwargs, "service": "driveway"})
        obj_drive = road_to_decal_object(driveway_road, "test_level")
        assert obj_drive is not None
        assert obj_drive["gatedRoad"] is True

    def test_ai_traffic_properties(self) -> None:
        road = Road(
            osm_id=101,
            highway="primary",
            points=np.array([[0.0, 0.0], [100.0, 0.0]]),
            width_m=14.0,
            lanes=4,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="Main Ave",
            maxspeed_kph=90.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        obj = road_to_decal_object(road, "test_level")
        assert obj is not None
        assert obj["drivability"] == 1.0
        assert obj["oneWay"] is False
        assert obj["autoLanes"] is False
        assert obj["lanesLeft"] == 2
        assert obj["lanesRight"] == 2
        assert obj["autoJunction"] is True
        assert obj["speedLimit"] == pytest.approx(25.0)  # 90 / 3.6

    def test_oneway_lanes_distribution(self) -> None:
        road = Road(
            osm_id=102,
            highway="motorway",
            points=np.array([[0.0, 0.0], [100.0, 0.0]]),
            width_m=12.0,
            lanes=3,
            surface="asphalt",
            oneway=True,
            bridge=False,
            tunnel=False,
            layer=0,
            name="M7",
            maxspeed_kph=110.0,
            priority=100,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        obj = road_to_decal_object(road, "test_level")
        assert obj is not None
        assert obj["oneWay"] is True
        assert obj["lanesLeft"] == 0
        assert obj["lanesRight"] == 3
        assert obj["speedLimit"] == pytest.approx(30.56)  # 110 / 3.6

    def test_track_drivability(self) -> None:
        road = Road(
            osm_id=103,
            highway="track",
            points=np.array([[0.0, 0.0], [50.0, 0.0]]),
            width_m=4.0,
            lanes=1,
            surface="dirt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="",
            maxspeed_kph=30.0,
            priority=20,
            drivable=True,
            material_key="Dirt",
            decal_material="m_dirt_road",
        )
        obj = road_to_decal_object(road, "test_level")
        assert obj is not None
        assert obj["drivability"] == 0.5

    def test_bridge_decal_properties(self) -> None:
        road = Road(
            osm_id=104,
            highway="primary",
            points=np.array([[0.0, 0.0], [50.0, 0.0]]),
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=True,
            tunnel=False,
            layer=1,
            name="Volga Bridge",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        obj = road_to_decal_object(road, "test_level", ground_z=np.array([15.0, 15.0]))
        assert obj is not None
        assert obj["overObjects"] is True
        assert obj["decalBias"] == 0.002
        assert obj["autoJunction"] is False
        # Node elevations must be lifted by +0.002
        for node in obj["nodes"]:
            assert node[2] == pytest.approx(15.002)


class TestRoadSplittingAndMarkings:
    def test_road_longer_than_1500m_is_split(self) -> None:
        # 3000m long highway
        pts = np.array([[0.0, 0.0], [1000.0, 0.0], [2000.0, 0.0], [3000.0, 0.0]])
        road = Road(
            osm_id=200,
            highway="motorway",
            points=pts,
            width_m=14.0,
            lanes=4,
            surface="asphalt",
            oneway=True,
            bridge=False,
            tunnel=False,
            layer=0,
            name="M7 Long",
            maxspeed_kph=110.0,
            priority=100,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        objs = road_to_decal_objects(road, "test_level", max_segment_length_m=1500.0)
        assert len(objs) >= 2
        # Verify naming pattern way<id>_0, way<id>_1
        assert objs[0].get("name") == "way200_0"
        assert objs[1].get("name") == "way200_1"

        # Verify continuity: end node of segment 0 equals start node of segment 1
        seg0_end = objs[0]["nodes"][-1][:2]
        seg1_start = objs[1]["nodes"][0][:2]
        assert seg0_end == seg1_start

    def test_short_road_not_split(self) -> None:
        pts = np.array([[0.0, 0.0], [500.0, 0.0]])
        road = Road(
            osm_id=201,
            highway="primary",
            points=pts,
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="Short",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        objs = road_to_decal_objects(road, "test_level", max_segment_length_m=1500.0)
        assert len(objs) == 1
        assert objs[0].get("name") == "way201"

    def test_multi_lane_gost_markings(self) -> None:
        pts = np.array([[0.0, 0.0], [100.0, 0.0]])
        road = Road(
            osm_id=202,
            highway="primary",
            points=pts,
            width_m=14.0,
            lanes=4,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="Avenue",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        # 100m straight road in 3D
        pts_3d = np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]])
        markings = generate_lane_markings(
            road,
            "test_level",
            pts_3d,
            is_start_junction=True,
            is_end_junction=True,
            clip_distance_m=8.0,
        )
        # 4 lanes on primary -> 3 interior dividers (center + 2 dashed) + 2 edge lines
        assert len(markings) == 5

        materials = [m["material"] for m in markings]
        # Two-way road: solid white center/edge lines and dashed lane dividers
        assert "m_line_white" in materials
        assert "m_line_white_discontinue" in materials
        assert {m["renderPriority"] for m in markings} == {1, 2}
        assert all(m["textureLength"] == pytest.approx(6.4) for m in markings)
        assert all(m["decalBias"] == pytest.approx(0.005) for m in markings)

        # Clipped 8m from both ends: length must be 100 - 16 = 84m
        for m in markings:
            nodes = m["nodes"]
            start_x = nodes[0][0]
            end_x = nodes[-1][0]
            assert start_x == pytest.approx(8.0, abs=0.5)
            assert end_x == pytest.approx(92.0, abs=0.5)

    def test_no_markings_for_narrow_roads(self) -> None:
        pts_3d = np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]])
        road = Road(
            osm_id=203,
            highway="residential",
            points=np.array([[0.0, 0.0], [100.0, 0.0]]),
            width_m=6.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="",
            maxspeed_kph=40.0,
            priority=40,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        markings = generate_lane_markings(road, "test_level", pts_3d)
        assert markings == []


class TestTunnelSuppression:
    def test_tunnel_suppresses_decal_and_grading(self) -> None:
        road = Road(
            osm_id=300,
            highway="primary",
            points=np.array([[0.0, 0.0], [100.0, 0.0]]),
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=True,
            layer=-1,
            name="Metro Tunnel",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        # road_to_decal_object returns None
        assert road_to_decal_object(road, "test_level") is None
        # road_to_decal_objects returns empty list
        assert road_to_decal_objects(road, "test_level") == []

        # Terrain corridor carries grade_terrain=False
        corridors = roads_to_corridors([road], RoadSettings())
        assert len(corridors) == 1
        assert corridors[0].grade_terrain is False


class TestDecalRoadSafetyAndMaterials:
    def _make_road(self, osm_id: int, points: np.ndarray, **overrides) -> Road:
        base = {
            "osm_id": osm_id,
            "highway": "primary",
            "points": points,
            "width_m": 14.0,
            "lanes": 4,
            "surface": "asphalt",
            "oneway": False,
            "bridge": False,
            "tunnel": False,
            "layer": 0,
            "name": "Test Road",
            "maxspeed_kph": 60.0,
            "priority": 80,
            "drivable": True,
            "material_key": "Asphalt",
            "decal_material": "road_asphalt_2lane",
        }
        base.update(overrides)
        return Road(**base)

    def test_road_split_strictly_by_bbox_span(self) -> None:
        # 1200m straight road: arc length <= 1500m, but bbox span > 800m
        road = self._make_road(401, np.array([[0.0, 0.0], [1200.0, 0.0]]))
        objs = road_to_decal_objects(road, "test_level", max_segment_length_m=1500.0, max_bbox_span_m=800.0)
        assert len(objs) >= 2
        for obj in objs:
            nodes = np.asarray(obj["nodes"], dtype=np.float64)[:, :3]
            spans = np.max(nodes, axis=0) - np.min(nodes, axis=0)
            assert np.max(spans) <= 800.0 + 1e-6

    def test_nan_inf_and_duplicate_nodes_filtered(self) -> None:
        pts = np.array(
            [
                [0.0, 0.0],
                [0.0, 0.0],
                [float("nan"), 5.0],
                [50.0, 0.0],
                [50.0, 0.0],
                [100.0, float("inf")],
                [100.0, 0.0],
            ]
        )
        road = self._make_road(402, pts)
        objs = road_to_decal_objects(road, "test_level")
        assert len(objs) == 1
        nodes = objs[0]["nodes"]
        assert len(nodes) >= 2
        for n in nodes:
            assert all(math.isfinite(v) for v in n)
        for a, b in itertools.pairwise(nodes):
            assert math.hypot(b[0] - a[0], b[1] - a[1]) >= 1e-3

        markings = generate_lane_markings(road, "test_level", np.asarray(nodes)[:, :3])
        assert len(markings) == 5
        for m in markings:
            for n in m["nodes"]:
                assert all(math.isfinite(v) for v in n)

    def test_over_objects_and_decal_bias_ground_vs_bridge(self) -> None:
        pts = np.array([[0.0, 0.0], [100.0, 0.0]])
        ground_road = self._make_road(403, pts, bridge=False)
        bridge_road = self._make_road(404, pts, bridge=True)

        g_obj = road_to_decal_object(ground_road, "test_level")
        b_obj = road_to_decal_object(bridge_road, "test_level")
        assert g_obj is not None and b_obj is not None
        assert g_obj["overObjects"] is False
        assert b_obj["overObjects"] is True

        g_marks = generate_lane_markings(ground_road, "test_level", np.asarray(g_obj["nodes"])[:, :3])
        b_marks = generate_lane_markings(bridge_road, "test_level", np.asarray(b_obj["nodes"])[:, :3])
        assert g_marks and b_marks
        assert all(m["overObjects"] is False for m in g_marks)
        assert all(m["overObjects"] is True for m in b_marks)
        assert all(m["decalBias"] >= b_obj["decalBias"] for m in b_marks)

    def test_sample_surface_z_nonsquare_and_per_node_sampling(self) -> None:
        from types import SimpleNamespace

        from ivanovo2beam.beamng.level import SceneTree
        from ivanovo2beam.roads.build import add_network_to_scene, sample_surface_z

        # Non-square 4x8 elevation grid with a ridge at x=50m
        rows, cols = 4, 8
        elev = np.zeros((rows, cols), dtype=np.float64)
        elev[:, 2:4] = 10.0  # ridge around x = 40..70m
        build = SimpleNamespace(elevations=elev, origin_x=0.0, origin_y=0.0, cell_size_m=20.0)

        road = self._make_road(405, np.array([[10.0, 10.0], [110.0, 10.0]]), lanes=2, width_m=7.0)
        z_raw = sample_surface_z(road, build)
        assert z_raw.shape == (2,)
        assert z_raw[0] == pytest.approx(0.0)
        assert z_raw[1] == pytest.approx(0.0)

        tree = SceneTree(level_id="test_level")
        add_network_to_scene(tree, [road], build, max_node_spacing_m=10.0)
        road_objs = tree.objects[("level_objects", "road_network")]
        base_objs = [o for o in road_objs if o["material"] == "road_asphalt_2lane"]
        assert len(base_objs) == 1
        zs = [n[2] for n in base_objs[0]["nodes"]]
        # Densified interior nodes must sample the 10m ridge rather than staying at 0m
        assert max(zs) >= 9.0

    def test_road_materials_aliases_blending_and_single_file(self, tmp_path) -> None:
        from ivanovo2beam.roads.materials import build_road_materials_json, write_road_materials

        mats = build_road_materials_json("test_level")
        # Aliases must be independent deep copies with unique names, mapTo, and persistentId
        pids = [m["persistentId"] for m in mats.values()]
        assert len(set(pids)) == len(pids)
        for key, mat in mats.items():
            assert mat["name"] == key
            assert mat["mapTo"] == key

        # Solid opaque asphalt, concrete, and gravel opacity maps
        asphalt = mats["road_asphalt_2lane"]["Stages"][0]
        assert asphalt["baseColorMap"].endswith("t_road_asphalt1_b.color.dds")
        assert asphalt["opacityMap"].endswith("t_road_asphalt2_o.data.dds")
        assert asphalt["baseColorFactor"][3] == pytest.approx(1.0)
        assert mats["road1_concrete"]["Stages"][0]["opacityMap"].endswith("t_road_asphalt2_o.data.dds")
        assert mats["road1_concrete"]["Stages"][0]["baseColorFactor"][3] == pytest.approx(1.0)
        assert mats["m_dirt_road_gravels"]["Stages"][0]["opacityMap"].endswith("t_dirt_road_flows_o.data.dds")

        # Concrete, dirt, gravel must use .dds, opacityMap, and castShadows=False
        for key in ("road_asphalt_2lane", "road1_concrete", "m_dirt_road", "m_dirt_road_gravels"):
            m = mats[key]
            assert m["castShadows"] is False
            stage0 = m["Stages"][0]
            assert "opacityMap" in stage0
            for val in stage0.values():
                if isinstance(val, str) and val.startswith("/"):
                    assert val.endswith(".dds"), f"{key}: expected .dds, got {val}"

        # Unified alphaRef on line markings
        for key in ("m_line_white", "m_line_white_discontinue", "m_line_yellow", "m_line_yellow_double"):
            assert mats[key]["alphaRef"] == 255
            assert "alphaTest" not in mats[key]
        assert mats["m_line_white_discontinue"]["annotation"] == "DASHED_LINE"

        # write_road_materials writes art/road/main.materials.json without root duplication
        written = write_road_materials(tmp_path, "test_level")
        assert written == [tmp_path / "art" / "road" / "main.materials.json"]
        assert written[0].is_file()
        assert not (tmp_path / "main.materials.json").exists()

    def test_tiered_decal_lod_settings_by_road_class(self) -> None:
        pts = np.array([[0.0, 0.0], [100.0, 0.0]])
        cases = [
            ("trunk", [750.0, 180.0], 1.5, 0.5, 12),
            ("secondary", [550.0, 120.0], 1.5, 0.5, 15),
            ("residential", [400.0, 100.0], 2.0, 0.5, 20),
            ("service", [250.0, 75.0], 2.0, 0.5, 22),
        ]
        for highway, exp_fade, exp_break, exp_detail, exp_prio in cases:
            road = self._make_road(410, pts, highway=highway)
            obj = road_to_decal_object(road, "test_level")
            assert obj is not None
            assert obj["distanceFade"] == exp_fade
            assert obj["breakAngle"] == exp_break
            assert obj["detail"] == exp_detail
            assert obj["renderPriority"] == exp_prio
            assert obj["textureLength"] == pytest.approx(11.0)

    def test_z_aware_simplification_drops_flat_collinear_keeps_crest(self) -> None:
        from types import SimpleNamespace

        from ivanovo2beam.beamng.level import SceneTree
        from ivanovo2beam.roads.build import add_network_to_scene

        # Flat 200m straightaway: densified every 6m (~35 nodes) should simplify to just 2 endpoints
        flat_road = self._make_road(411, np.array([[0.0, 0.0], [200.0, 0.0]]), lanes=2)
        flat_objs = road_to_decal_objects(flat_road, "test_level", max_node_spacing_m=6.0)
        assert len(flat_objs) == 1
        assert len(flat_objs[0]["nodes"]) == 2

        # Straight road with a 2m terrain crest at x=100m: Z-aware DP must retain crest (>0.10m)
        elev = np.zeros((4, 12), dtype=np.float64)
        elev[:, 5] = 2.0  # crest at x = 100m (cell_size_m=20)
        build = SimpleNamespace(elevations=elev, origin_x=0.0, origin_y=0.0, cell_size_m=20.0)
        tree = SceneTree(level_id="test_level")
        add_network_to_scene(tree, [self._make_road(412, np.array([[10.0, 10.0], [190.0, 10.0]]), lanes=2)], build)
        crest_nodes = tree.objects[("level_objects", "road_network")][0]["nodes"]
        assert max(n[2] for n in crest_nodes) >= 1.8

    def test_short_marking_clipped_under_12m_is_skipped(self) -> None:
        # 25m 4-lane road clipped 8m at both ends leaves 9m (< 12m threshold)
        road = self._make_road(413, np.array([[0.0, 0.0], [25.0, 0.0]]), lanes=4)
        pts_3d = np.array([[0.0, 0.0, 0.0], [25.0, 0.0, 0.0]])
        assert generate_lane_markings(road, "test_level", pts_3d, is_start_junction=True, is_end_junction=True) == []

    def test_bridge_lod_clamp_and_shallow_collinear_straightening(self) -> None:
        # 3-point bridge with 0.02 deg lateral kink (<0.05m) and 1m Z hump
        pts = np.array([[0.0, 0.0], [34.0, 0.015], [47.0, 0.0]])
        bridge = self._make_road(414, pts, highway="service", bridge=True, lanes=2, width_m=7.0)
        obj = road_to_decal_object(bridge, "test_level", ground_z=np.array([96.0, 97.0, 95.5]))
        assert obj is not None
        assert obj["overObjects"] is True
        assert obj["breakAngle"] <= 1.5
        assert obj["detail"] == pytest.approx(0.5)
        # Any kept interior node must lie strictly on the XY chord (y == 0.0)
        for n in obj["nodes"]:
            assert n[1] == pytest.approx(0.0, abs=1e-6)

    def test_wide_two_lane_highway_gets_dashed_centerline(self) -> None:
        pts = np.array([[0.0, 0.0], [100.0, 0.0]])
        pts_3d = np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]])
        primary_wide = self._make_road(415, pts, highway="primary", lanes=2, width_m=8.5, oneway=False)
        marks = generate_lane_markings(primary_wide, "test_level", pts_3d, is_start_junction=False, is_end_junction=False)
        # Dashed centerline (renderPriority=2) + 2 white edge lines (renderPriority=1)
        assert len(marks) == 3
        center = [m for m in marks if m["material"] == "m_line_white_discontinue"]
        edges = [m for m in marks if m["material"] == "m_line_white"]
        assert len(center) == 1
        assert center[0]["renderPriority"] == 2
        assert all(n[1] == pytest.approx(0.0, abs=1e-6) and n[3] == pytest.approx(0.15) for n in center[0]["nodes"])
        assert len(edges) == 2
        assert all(e["renderPriority"] == 1 for e in edges)

        # Secondary wide 2-lane roads now get markings; narrow non-highway residential gets none
        secondary_wide = self._make_road(416, pts, highway="secondary", lanes=2, width_m=9.0, oneway=False)
        residential_2lane = self._make_road(417, pts, highway="residential", lanes=2, width_m=7.0, oneway=False)
        sec_marks = generate_lane_markings(secondary_wide, "test_level", pts_3d, is_start_junction=False, is_end_junction=False)
        assert len(sec_marks) == 3
        assert generate_lane_markings(residential_2lane, "test_level", pts_3d) == []


class TestGoldenSectorRoadOverhaul:
    def _road(self, osm_id: int, points: np.ndarray, **overrides) -> Road:
        base = {
            "osm_id": osm_id,
            "highway": "primary",
            "points": points,
            "width_m": 10.0,
            "lanes": 2,
            "surface": "asphalt",
            "oneway": False,
            "bridge": False,
            "tunnel": False,
            "layer": 0,
            "name": "Test Road",
            "maxspeed_kph": 90.0,
            "priority": 80,
            "drivable": True,
            "material_key": "Asphalt",
            "decal_material": "road_asphalt_2lane",
        }
        base.update(overrides)
        return Road(**base)

    def test_corner_filleting_rounds_bend_and_pins_junctions(self) -> None:
        from ivanovo2beam.roads.network import round_road_corners

        bent = self._road(501, np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]]))
        rounded = round_road_corners([bent])[0]
        assert rounded.points.shape[0] > 5
        # Cut distance inside corner (100, 0) must not exceed MAX_CUT_M (3.0m)
        dists = np.hypot(rounded.points[:, 0] - 100.0, rounded.points[:, 1] - 0.0)
        assert dists.min() <= 3.0 + 1e-6

        # Shared 3-way junction vertex must remain pinned exact while main road curves smoothly
        main = self._road(502, np.array([[0.0, 0.0], [50.0, 0.0], [100.0, 50.0]]))
        side = self._road(503, np.array([[50.0, 0.0], [50.0, -50.0]]), highway="residential", width_m=6.0)
        out_main, _ = round_road_corners([main, side])
        assert out_main.points.shape[0] > 3
        pinned_dists = np.hypot(out_main.points[:, 0] - 50.0, out_main.points[:, 1] - 0.0)
        assert pinned_dists.min() < 1e-6

        # Bridge corners are never rounded away from structural centerline
        bridge = self._road(504, np.array([[0.0, 0.0], [20.0, 0.0], [20.0, 20.0]]), bridge=True)
        out_bridge = round_road_corners([bridge])[0]
        np.testing.assert_allclose(out_bridge.points, bridge.points)

    def test_sanitize_road_nodes_drops_spikes_and_short_segments(self) -> None:
        from ivanovo2beam.roads.build import sanitize_road_nodes

        pts = np.array(
            [
                [0.0, 0.0, 1.0],
                [0.05, 0.0, 1.0],       # < 0.15m spacing -> dropped
                [50.0, 0.0, 1.0],
                [52.0, 0.0, 1.0],       # spike out
                [50.1, 0.001, 1.0],     # reversal back -> spike dot <= -0.99 dropped
                [100.0, 0.0, 1.0],
                [100.05, 0.0, 1.0],     # terminus preserved instead of 100.0
            ]
        )
        clean = sanitize_road_nodes(pts)
        assert clean.shape[0] >= 2
        assert clean[-1, 0] == pytest.approx(100.05)
        for a, b in itertools.pairwise(clean):
            assert math.hypot(b[0] - a[0], b[1] - a[1]) >= 0.15 - 1e-6

    def test_inverted_render_priority_stack_grass_edges_and_unpaved_exclusion(self) -> None:
        from types import SimpleNamespace

        from ivanovo2beam.beamng.level import SceneTree
        from ivanovo2beam.roads.build import add_network_to_scene
        from ivanovo2beam.roads.materials import build_road_materials_json

        mats = build_road_materials_json("test_level")
        assert "m_road_asphalt_edge_grass" in mats
        for tex in mats["m_road_asphalt_edge_grass"]["Stages"][0].values():
            if isinstance(tex, str) and tex.startswith("/"):
                assert tex.endswith(".dds")

        elev = np.full((8, 8), 10.0, dtype=np.float64)
        build = SimpleNamespace(elevations=elev, origin_x=-50.0, origin_y=-50.0, cell_size_m=50.0)
        tree = SceneTree(level_id="test_level")

        primary = self._road(601, np.array([[0.0, 0.0], [100.0, 0.0]]), highway="primary", lanes=2, width_m=10.0)
        secondary = self._road(602, np.array([[50.0, 0.0], [50.0, 80.0]]), highway="secondary", lanes=2, width_m=7.0)
        track = self._road(
            603,
            np.array([[0.0, 40.0], [100.0, 40.0]]),
            highway="track",
            lanes=1,
            width_m=4.0,
            surface="gravel",
            material_key="Gravel",
            decal_material="m_dirt_road_gravels",
        )
        bridge = self._road(604, np.array([[120.0, 0.0], [170.0, 0.0]]), highway="primary", lanes=2, width_m=10.0, bridge=True)

        add_network_to_scene(tree, [primary, secondary, track, bridge], build)
        objs = tree.objects[("level_objects", "road_network")]
        by_name = {o["name"]: o for o in objs}

        assert by_name["way601"]["renderPriority"] == 13
        assert by_name["way602"]["renderPriority"] == 15
        assert by_name["way603"]["renderPriority"] == 23
        assert by_name["way603"]["textureLength"] == pytest.approx(88.0)
        assert by_name["way601"]["textureLength"] == pytest.approx(11.0)
        # Junction end on way602 (50,0) gets 0 fade, dead-end (50,80) gets 2m fade
        assert by_name["way602"]["startEndFade"] == [0.0, 2.0]

        # Ground asphalt roads get reversed left (-normal) + forward right (+normal) grass edge decals;
        # way601_edge_right (north side) is split around crossing way602 into two pieces!
        edge_right_pieces = [o for k, o in by_name.items() if k.startswith("way601_edge_right")]
        assert len(edge_right_pieces) == 2
        assert "way601_edge_left" in by_name
        assert by_name["way601_edge_left"]["renderPriority"] == 9
        assert by_name["way601_edge_left"]["nodes"][0][0] > by_name["way601_edge_left"]["nodes"][-1][0]
        assert all(p["renderPriority"] == 9 for p in edge_right_pieces)
        assert not any(k.startswith("way603_edge_") for k in by_name)
        assert not any(k.startswith("way603_mark_") for k in by_name)
        assert not any(k.startswith("way604_edge_") for k in by_name)

        # Edge lines (1) < center line (2) < grass edge (9) < primary (13) < secondary (14) < track (15)
        edge_line_pieces = [o for k, o in by_name.items() if k.startswith("way601_line_left")]
        center_pieces = [o for k, o in by_name.items() if k.startswith("way601_mark_1")]
        assert edge_line_pieces and all(o["renderPriority"] == 1 for o in edge_line_pieces)
        assert center_pieces and all(o["renderPriority"] == 2 for o in center_pieces)

    def test_crossing_cuts_trim_markings_2m_clear_of_cross_street(self) -> None:
        from ivanovo2beam.roads.build import build_carriageway_index

        main = self._road(701, np.array([[0.0, 0.0], [120.0, 0.0]]), highway="primary", lanes=2, width_m=10.0)
        cross = self._road(702, np.array([[60.0, -40.0], [60.0, 40.0]]), highway="secondary", lanes=2, width_m=8.0)
        idx = build_carriageway_index([main, cross])
        pts_3d = np.array([[0.0, 0.0, 0.0], [120.0, 0.0, 0.0]])

        marks = generate_lane_markings(
            main,
            "test_level",
            pts_3d,
            is_start_junction=False,
            is_end_junction=False,
            carriageway_index=idx,
            crossing_margin_m=2.0,
        )
        # Cross road has half_width=4.0 + margin=2.0 -> markings cut in x=[54..66], leaving 2 pieces per line (6 total)
        assert len(marks) == 6
        for m in marks:
            xs = [n[0] for n in m["nodes"]]
            assert not any(54.5 < x < 65.5 for x in xs)

    def test_sample_road_profile_and_pin_junction_elevations(self) -> None:
        from types import SimpleNamespace

        from ivanovo2beam.roads.build import pin_junction_elevations, sample_road_profile_points

        pts = np.array([[0.0, 0.0], [100.0, 0.0]])
        res_xy, prof_z = sample_road_profile_points(
            pts,
            lambda xy: 100.0 + 0.02 * xy[:, 0],
            sample_step_m=5.0,
            smooth_window_m=20.0,
            max_grade=0.06,
        )
        assert res_xy.shape[0] == 21
        assert prof_z.shape[0] == 21
        assert np.all(np.isfinite(prof_z))

        # Two through arms and a side arm with an elevation mismatch at (50, 0)
        arm_w = SimpleNamespace(
            points=np.array([[0.0, 0.0], [50.0, 0.0]]),
            surface_z=np.full(2, 100.0),
            priority=80,
            half_width_m=5.0,
            grade_terrain=True,
        )
        arm_e = SimpleNamespace(
            points=np.array([[50.0, 0.0], [100.0, 0.0]]),
            surface_z=np.full(2, 100.0),
            priority=80,
            half_width_m=5.0,
            grade_terrain=True,
        )
        side = SimpleNamespace(
            points=np.array([[50.0, 0.0], [50.0, 2.5], [50.0, 50.0]]),
            surface_z=np.array([103.0, 103.0, 102.0]),
            priority=40,
            half_width_m=3.5,
            grade_terrain=True,
        )
        res = pin_junction_elevations([arm_w, arm_e, side], ramp_m=30.0)
        assert res["junctions_pinned"] >= 1
        # All three arms now meet at exact consensus elevation at (50, 0)
        assert arm_w.surface_z[-1] == pytest.approx(arm_e.surface_z[0], abs=1e-6)
        assert side.surface_z[0] == pytest.approx(arm_w.surface_z[-1], abs=1e-6)

    def test_acute_y_junction_cuts_edge_grass_and_bridge_portal_keeps_markings(self) -> None:
        from types import SimpleNamespace

        from ivanovo2beam.beamng.level import SceneTree
        from ivanovo2beam.roads.build import add_network_to_scene

        elev = np.full((8, 8), 10.0, dtype=np.float64)
        build = SimpleNamespace(elevations=elev, origin_x=-50.0, origin_y=-50.0, cell_size_m=50.0)
        tree = SceneTree(level_id="test_level")

        # Acute Y-junction at (100, 0): primary through + 18-deg secondary fork
        west_main = self._road(801, np.array([[0.0, 0.0], [100.0, 0.0]]), highway="primary", lanes=2, width_m=10.0)
        east_main = self._road(802, np.array([[100.0, 0.0], [200.0, 0.0]]), highway="primary", lanes=2, width_m=10.0)
        y_branch = self._road(803, np.array([[40.0, 20.0], [100.0, 0.0]]), highway="secondary", lanes=2, width_m=8.0)
        stub = self._road(804, np.array([[10.0, 40.0], [50.0, 40.0]]), highway="service", lanes=1, width_m=5.0)
        bridge = self._road(805, np.array([[200.0, 0.0], [260.0, 0.0]]), highway="primary", lanes=2, width_m=10.0, bridge=True, layer=1)

        add_network_to_scene(tree, [west_main, east_main, y_branch, stub, bridge], build)
        objs = tree.objects[("level_objects", "road_network")]
        by_name = {o["name"]: o for o in objs}

        # Service stub dead-end fades over 4.0m smoothly
        assert by_name["way804"]["startEndFade"] == [4.0, 4.0]
        assert by_name["way805"]["startEndFade"] == [0.0, 0.0]

        # Bridge portal end (200, 0) of east_main is not clipped 2m back: centerline reaches x=200
        east_marks = [o for k, o in by_name.items() if k.startswith("way802_mark_1")]
        assert east_marks
        assert max(n[0] for m in east_marks for n in m["nodes"]) == pytest.approx(200.0, abs=0.1)

        # Acute Y edge_grass on west_main (+normal = north side toward y_branch) stops before overlapping y_branch
        west_edge_right = [o for k, o in by_name.items() if k.startswith("way801_edge_right")]
        assert west_edge_right
        # Near x=85..100 where y_branch comes within ~5m of west_main, edge_right is cleanly trimmed away
        assert max(n[0] for e in west_edge_right for n in e["nodes"]) < 88.0

    def test_bridge_and_markings_keep_uniform_2m_nodes_and_tight_bias(self) -> None:
        # Curved 60m bridge deck matching way274102985 geometry (~4m steps, parabolic crest)
        xs = np.linspace(7974.53, 7913.75, 16)
        ys = np.linspace(-8308.59, -8304.70, 16)
        t = np.linspace(0.0, 1.0, 16)
        deck_z = 106.94 + 0.26 * (1.0 - (2.0 * t - 1.0) ** 2) - 0.11 * t
        bridge = self._road(274102985, np.column_stack((xs, ys)), highway="primary", lanes=2, width_m=10.0, bridge=True, layer=1)

        b_objs = road_to_decal_objects(bridge, "test_level", ground_z=deck_z)
        assert len(b_objs) == 1
        b_nodes = b_objs[0]["nodes"]
        assert len(b_nodes) >= 25
        for a, b in itertools.pairwise(b_nodes):
            step = math.hypot(b[0] - a[0], b[1] - a[1])
            assert 0.5 <= step <= 2.05

        marks = generate_lane_markings(
            bridge,
            "test_level",
            np.asarray(b_nodes, dtype=np.float64)[:, :3],
            is_start_junction=False,
            is_end_junction=False,
        )
        assert len(marks) == 3
        for m in marks:
            assert m["overObjects"] is True
            assert m["decalBias"] == pytest.approx(0.005)
            assert len(m["nodes"]) >= 25
            for a, b in itertools.pairwise(m["nodes"]):
                step = math.hypot(b[0] - a[0], b[1] - a[1])
                assert 0.5 <= step <= 2.05
                assert all(math.isfinite(v) for v in a) and all(math.isfinite(v) for v in b)

    def test_t_junction_preserves_opposite_edge_and_tight_cut_zone(self) -> None:
        from ivanovo2beam.roads.build import build_carriageway_index

        main = self._road(901, np.array([[0.0, 0.0], [160.0, 0.0]]), highway="primary", lanes=2, width_m=10.0)
        # Side street joins from NORTH (y > 0) and terminates right at (80, 0)
        stem = self._road(902, np.array([[80.0, 60.0], [80.0, 0.0]]), highway="secondary", lanes=2, width_m=8.0)
        idx = build_carriageway_index([main, stem])
        pts_3d = np.array([[0.0, 0.0, 0.0], [160.0, 0.0, 0.0]])

        marks = generate_lane_markings(
            main,
            "test_level",
            pts_3d,
            is_start_junction=False,
            is_end_junction=False,
            carriageway_index=idx,
            crossing_margin_m=1.5,
        )
        # North edge (+4.7m, right) is split into 2 pieces around x=[74.5..85.5],
        # while South edge (-4.7m, left) stays completely unbroken (1 piece)!
        left_edges = [m for m in marks if m["name"].startswith("way901_line_left")]
        right_edges = [m for m in marks if m["name"].startswith("way901_line_right")]
        assert len(left_edges) == 1
        assert len(right_edges) == 2
        right_xs = sorted(n[0] for m in right_edges for n in m["nodes"])
        assert any(65.0 <= x <= 76.0 for x in right_xs)
        assert any(84.0 <= x <= 95.0 for x in right_xs)

    def test_angled_ramp_y_fork_keeps_markings_tight_to_gore(self) -> None:
        from types import SimpleNamespace

        from ivanovo2beam.beamng.level import SceneTree
        from ivanovo2beam.roads.build import add_network_to_scene

        elev = np.full((12, 12), 10.0, dtype=np.float64)
        build = SimpleNamespace(elevations=elev, origin_x=-50.0, origin_y=-50.0, cell_size_m=50.0)
        tree = SceneTree(level_id="test_level")

        # Main 2-lane primary + 14-deg one-way ramp merging on the North (+Y / right normal) side
        main = self._road(911, np.array([[0.0, 0.0], [200.0, 0.0]]), highway="primary", lanes=2, width_m=10.0)
        ramp = self._road(
            912,
            np.array([[100.0, 0.0], [200.0, 25.0]]),
            highway="primary_link",
            lanes=1,
            width_m=5.5,
            oneway=True,
        )
        add_network_to_scene(tree, [main, ramp], build)
        by_name = {o["name"]: o for o in tree.objects[("level_objects", "road_network")]}

        # One-way ramp gets edge markings and resumes well before x=200 (not dropped for 100m!)
        ramp_marks = [o for k, o in by_name.items() if k.startswith("way912_line_")]
        assert ramp_marks
        assert max(n[0] for m in ramp_marks for n in m["nodes"]) > 170.0
        # Main road keeps its centerline unbroken past x=100 gore
        main_center = [o for k, o in by_name.items() if k.startswith("way911_mark_1")]
        assert len(main_center) == 1
        # Ground road gets tight decalBias and edge_grass has zero alphaRef/translucentZWrite
        assert by_name["way911"]["decalBias"] == pytest.approx(0.0015)

    def test_four_way_crossing_minor_track_never_cuts_highway_centerline(self) -> None:
        from ivanovo2beam.roads.build import build_carriageway_index
        from ivanovo2beam.roads.materials import build_road_materials_json

        mats = build_road_materials_json("test_level")
        assert "alphaRef" not in mats["m_road_asphalt_edge_grass"]
        assert mats["m_road_asphalt_edge_grass"]["translucentZWrite"] is False
        assert "m_road_asphalt_edge_dirt" in mats

        main = self._road(921, np.array([[0.0, 0.0], [200.0, 0.0]]), highway="primary", lanes=2, width_m=10.0)
        cross_sec = self._road(922, np.array([[60.0, -40.0], [60.0, 40.0]]), highway="secondary", lanes=2, width_m=8.0)
        farm_track = self._road(
            923,
            np.array([[140.0, -40.0], [140.0, 40.0]]),
            highway="track",
            lanes=1,
            width_m=4.0,
            surface="gravel",
            material_key="Gravel",
            decal_material="m_dirt_road_gravels",
        )
        idx = build_carriageway_index([main, cross_sec, farm_track])
        pts_3d = np.array([[0.0, 0.0, 0.0], [200.0, 0.0, 0.0]])
        marks = generate_lane_markings(
            main,
            "test_level",
            pts_3d,
            is_start_junction=False,
            is_end_junction=False,
            carriageway_index=idx,
            crossing_margin_m=1.5,
        )
        # Cut ONLY once around secondary road (x=60), NOT at farm track (x=140)
        center = [m for m in marks if m["name"].startswith("way921_mark_1")]
        assert len(center) == 2
        piece2_xs = [n[0] for n in center[1]["nodes"]]
        assert min(piece2_xs) < 67.0 and max(piece2_xs) > 199.0


