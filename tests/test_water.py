"""Tests for hydrology extraction, river profiling, and BeamNG water export."""

from __future__ import annotations

import numpy as np
import pytest

from ivanovo2beam.beamng.level import SceneTree, build_info_json
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.osm.types import OsmData, OsmNode, OsmRelation, OsmWay
from ivanovo2beam.water.export import (
    DEFAULT_DEPTH_GRADIENT_TEX,
    DEFAULT_FOAM_TEX,
    DEFAULT_RIPPLE_TEX,
    WATER_GROUP,
    build_river_object,
    build_water_block_object,
    export_water_to_scene,
)
from ivanovo2beam.water.extract import (
    RiverChannel,
    WaterBody,
    WaterNetwork,
    compute_downstream_water_profile,
    compute_lake_water_elevation,
    extract_water,
    parse_width_m,
)

BBOX = BBox(west=40.5, south=56.8, east=41.5, north=57.2)


@pytest.fixture
def projection():
    return make_projection(BBOX, origin_lon=40.97, origin_lat=57.0)


class MockDem:
    """A synthetic DEM returning elevations based on latitude or coordinates."""

    def __init__(self, elevation: float = 120.0) -> None:
        self.elevation = elevation

    def sample_array(self, lons, lats):
        return np.full(np.shape(lons), self.elevation, dtype=np.float64)


class UphillBumpyDem:
    """A DEM with local washboard ripples and an artificial uphill bump."""

    def sample_array(self, lons, lats):
        lats_arr = np.asarray(lats, dtype=np.float64)
        # Latitudes decrease downstream, base elevation drops from 150 to 100
        t = (lats_arr - 56.8) / 0.4
        base = 100.0 + 50.0 * t
        # Add high-frequency ripples + an uphill bump in the middle
        ripples = 3.0 * np.sin(lats_arr * 2000.0)
        return base + ripples


class TestParseWidth:
    def test_parses_plain_number(self):
        assert parse_width_m("30", 15.0) == 30.0

    def test_parses_metres_suffix(self):
        assert parse_width_m("25m", 10.0) == 25.0
        assert parse_width_m("40.5 m", 10.0) == 40.5
        assert parse_width_m("12 meters", 10.0) == 12.0

    def test_falls_back_on_invalid_or_none(self):
        assert parse_width_m(None, 25.0) == 25.0
        assert parse_width_m("", 25.0) == 25.0
        assert parse_width_m("unknown", 25.0) == 25.0
        assert parse_width_m("-5", 25.0) == 25.0


class TestRiverProfile:
    def test_downstream_monotonicity_enforced(self, projection):
        """Downstream profile must never flow uphill (z_water[i] <= z_water[i-1])."""
        # Create a river flowing north to south (downward in Y / lat)
        points = np.array([
            [0.0, 5000.0],
            [0.0, 4000.0],
            [0.0, 3000.0],
            [0.0, 2000.0],
            [0.0, 1000.0],
            [0.0, 0.0],
        ], dtype=np.float64)

        dem = UphillBumpyDem()
        z_water = compute_downstream_water_profile(points, projection, dem, smooth_window_m=150.0)

        assert len(z_water) == len(points)
        # Verify strict non-increasing monotonicity
        for i in range(1, len(z_water)):
            assert z_water[i] <= z_water[i - 1] + 1e-6, (
                f"River flowed uphill at node {i}: {z_water[i]} > {z_water[i-1]}"
            )

    def test_fallback_on_empty_dem(self, projection):
        points = np.array([[0.0, 0.0], [100.0, 100.0]], dtype=np.float64)
        z_water = compute_downstream_water_profile(points, projection, None, default_elevation_m=95.0)
        assert np.allclose(z_water, 95.0)


class TestWaterBodyElevation:
    def test_lake_elevation_is_level(self, projection):
        ring = np.array([
            [100.0, 100.0],
            [200.0, 100.0],
            [200.0, 200.0],
            [100.0, 200.0],
            [100.0, 100.0],
        ], dtype=np.float64)
        dem = MockDem(118.5)
        z_water = compute_lake_water_elevation(ring, projection, dem)
        assert z_water == pytest.approx(118.5)


class TestWaterExtraction:
    def test_extract_linear_rivers_and_canals(self, projection):
        nodes = {
            1: OsmNode(1, 40.95, 57.00),
            2: OsmNode(2, 40.96, 57.01),
            3: OsmNode(3, 40.97, 57.02),
            4: OsmNode(4, 40.98, 57.03),
            5: OsmNode(5, 40.99, 57.04),
        }
        ways = {
            101: OsmWay(101, (1, 2, 3), tags={"waterway": "river", "name": "Уводь", "width": "35m"}),
            102: OsmWay(102, (3, 4, 5), tags={"waterway": "canal", "name": "Волга-Уводь канал"}),
        }
        osm_data = OsmData(nodes=nodes, ways=ways)
        dem = MockDem(125.0)

        network = extract_water(osm_data, projection, dem)
        assert len(network.rivers) == 2

        river = next(r for r in network.rivers if r.osm_id == 101)
        assert river.waterway_type == "river"
        assert river.name == "Уводь"
        assert river.width_m == 35.0
        assert river.depth_m == 2.5
        assert len(river.points) == 3
        assert np.allclose(river.z_water, 125.0)

        canal = next(r for r in network.rivers if r.osm_id == 102)
        assert canal.waterway_type == "canal"
        assert canal.width_m == 15.0  # default
        assert canal.depth_m == 2.0   # default canal depth

    def test_extract_lake_closed_way(self, projection):
        nodes = {
            10: OsmNode(10, 40.95, 57.00),
            11: OsmNode(11, 40.96, 57.00),
            12: OsmNode(12, 40.96, 57.01),
            13: OsmNode(13, 40.95, 57.01),
        }
        # Closed way (10 repeats at end)
        ways = {
            201: OsmWay(201, (10, 11, 12, 13, 10), tags={"natural": "water", "water": "lake", "name": "Рубское озеро"}),
        }
        osm_data = OsmData(nodes=nodes, ways=ways)
        dem = MockDem(115.0)

        network = extract_water(osm_data, projection, dem)
        assert len(network.water_bodies) == 1
        lake = network.water_bodies[0]
        assert lake.osm_id == 201
        assert lake.name == "Рубское озеро"
        assert lake.depth_m == 3.5
        assert lake.z_water == pytest.approx(115.0)
        assert len(lake.outer_ring) >= 4
        assert len(lake.inner_rings) == 0

    def test_extract_multipolygon_with_islands(self, projection):
        # Outer ring
        nodes = {
            1: OsmNode(1, 40.90, 57.00),
            2: OsmNode(2, 40.94, 57.00),
            3: OsmNode(3, 40.94, 57.04),
            4: OsmNode(4, 40.90, 57.04),
            # Island inner ring
            5: OsmNode(5, 40.91, 57.01),
            6: OsmNode(6, 40.93, 57.01),
            7: OsmNode(7, 40.93, 57.03),
            8: OsmNode(8, 40.91, 57.03),
        }
        ways = {
            301: OsmWay(301, (1, 2, 3, 4, 1), tags={}),
            302: OsmWay(302, (5, 6, 7, 8, 5), tags={}),
        }
        relations = {
            401: OsmRelation(
                401,
                members=(("way", 301, "outer"), ("way", 302, "inner")),
                tags={"type": "multipolygon", "natural": "water", "name": "Горьковское водохранилище (Волга)"},
            )
        }
        osm_data = OsmData(nodes=nodes, ways=ways, relations=relations)
        dem = MockDem(84.0)

        network = extract_water(osm_data, projection, dem)
        assert len(network.water_bodies) == 1
        volga = network.water_bodies[0]
        assert volga.osm_id == 401
        assert "Волга" in volga.name
        assert volga.depth_m >= 3.5
        assert len(volga.inner_rings) == 1  # Island preserved!


class TestBeamNGExport:
    def test_build_river_object_schema(self):
        channel = RiverChannel(
            osm_id=12345,
            name="Уводь",
            waterway_type="river",
            points=np.array([[0.0, 0.0], [50.0, 100.0]], dtype=np.float64),
            width_m=28.0,
            depth_m=2.5,
            z_water=np.array([120.0, 119.5], dtype=np.float64),
            flow_speed=1.5,
        )
        obj = build_river_object("ivanovo_mvp", channel)

        assert obj["class"] == "River"
        assert obj["name"] == "river_12345"
        assert "__parent" in obj
        assert obj["flowMagnitudePhysics"] == 1.5
        assert obj["depthGradientTex"] == DEFAULT_DEPTH_GRADIENT_TEX
        assert obj["foamTex"] == DEFAULT_FOAM_TEX
        assert obj["rippleTex"] == DEFAULT_RIPPLE_TEX

        # Verify nodes 8-tuples: [x, y, z, width, depth, 0, 0, 1]
        nodes = obj["nodes"]
        assert len(nodes) == 2
        for node in nodes:
            assert len(node) == 8
            assert node[3] == 28.0  # width
            assert node[4] == 2.5   # depth
            assert node[5:] == [0.0, 0.0, 1.0]

    def test_build_water_block_object_schema(self):
        body = WaterBody(
            osm_id=98765,
            name="Пруд",
            water_type="pond",
            outer_ring=np.array([[10, 10], [100, 10], [100, 80], [10, 80], [10, 10]], dtype=np.float64),
            inner_rings=[],
            depth_m=3.5,
            z_water=112.0,
        )
        obj = build_water_block_object("ivanovo_mvp", body)

        assert obj["class"] == "WaterBlock"
        assert obj["name"] == "waterblock_98765"
        assert obj["position"] == [10.0, 10.0, 112.0]
        assert obj["scale"] == [90.0, 70.0, 3.5]
        assert obj["rotationMatrix"] == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        assert "Waves (vertex undulation)" in obj
        assert "Ripples (texture animation)" in obj

    def test_export_water_to_scene(self):
        tree = SceneTree("ivanovo_test")
        channel = RiverChannel(
            osm_id=1,
            name="Stream",
            waterway_type="stream",
            points=np.array([[0, 0], [10, 10]], dtype=np.float64),
            width_m=4.0,
            depth_m=0.8,
            z_water=np.array([100, 99], dtype=np.float64),
        )
        body = WaterBody(
            osm_id=2,
            name="Lake",
            water_type="lake",
            outer_ring=np.array([[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]], dtype=np.float64),
            depth_m=3.5,
            z_water=98.0,
        )
        network = WaterNetwork(rivers=[channel], water_bodies=[body])

        count = export_water_to_scene(tree, network)
        assert count == 2
        assert WATER_GROUP in tree.objects
        water_objects = tree.objects[WATER_GROUP]
        assert len(water_objects) == 2
        classes = {obj["class"] for obj in water_objects}
        assert classes == {"River", "WaterBlock"}


class TestInfoJson:
    def test_info_json_road_rules_and_region(self):
        info = build_info_json(
            "ivanovo_mvp",
            "Ivanovo 1:1",
            "Test description",
            extent_m=4096.0,
        )
        assert info["region"] == "europe"
        assert info["roadRules"] == {"rightHandDrive": False, "turnOnRed": False}

    def test_info_json_named_spawn_points(self):
        sp = [
            {"objectname": "spawn_square", "translationId": "levels.ivanovo.spawn.square"},
            {"objectname": "spawn_station", "translationId": "levels.ivanovo.spawn.station"},
        ]
        info = build_info_json(
            "ivanovo_mvp",
            "Ivanovo 1:1",
            "Test description",
            extent_m=4096.0,
            spawn_points=sp,
            default_spawn_point_name="spawn_square",
        )
        assert info["defaultSpawnPointName"] == "spawn_square"
        assert info["spawnPoints"] == sp
