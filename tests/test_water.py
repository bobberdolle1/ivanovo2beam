"""Tests for hydrology extraction, river profiling, and BeamNG water export."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from ivanovo2beam.beamng.level import ROOT_GROUP, SceneTree, build_info_json
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.osm.types import OsmData, OsmNode, OsmRelation, OsmWay
from ivanovo2beam.water.export import (
    DEFAULT_DEPTH_GRADIENT_TEX,
    DEFAULT_FOAM_TEX,
    DEFAULT_RIPPLE_TEX,
    WATER_GROUP,
    build_river_object,
    build_water_block_object,
    compute_water_block_grid_element_size,
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

    def test_running_median_and_cross_channel_offset(self, projection):
        """Channel probe + runningMedian(5) ignores spike and sits +0.25m above channel min."""

        class BankSpikeDem:
            def sample_world_array(self, xs, ys):
                xs_arr = np.asarray(xs, dtype=np.float64)
                ys_arr = np.asarray(ys, dtype=np.float64)
                # Gentle valley sloping southwards, with a high bank to the east and a spike at y=400
                base = 100.0 + ys_arr * 0.002
                bank = np.where(np.abs(xs_arr) >= 2.5, 0.0, 4.0)
                spike = np.where((np.abs(ys_arr - 400.0) < 10.0) & (np.abs(xs_arr) < 1.0), 15.0, 0.0)
                return base + bank + spike

        pts = np.array([[0.0, float(y)] for y in range(800, -1, -100)], dtype=np.float64)
        z_water = compute_downstream_water_profile(pts, projection, BankSpikeDem(), width_m=10.0)
        assert z_water.shape == (len(pts),)
        # Never spikes at y=400 and stays strictly non-increasing downstream
        assert z_water.max() < 103.0
        assert z_water[-1] == pytest.approx(100.25, abs=0.05)
        for i in range(1, len(z_water)):
            assert z_water[i] <= z_water[i - 1] + 1e-9


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
        assert np.allclose(river.z_water, 125.25)

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

    def test_clip_bounds_drops_offmap_and_clips_straddling_rivers_and_lakes(self, projection):
        # Build synthetic world-coordinate water features converted back to geo for OsmData
        world_pts = [
            # River 1 (IDs 1-2): completely outside clip_bounds
            (-5000.0, 500.0),
            (-3000.0, 500.0),
            # River 2 (IDs 3-4): enters clip_bounds from west to east
            (-1000.0, 500.0),
            (1000.0, 500.0),
            # River 3 (IDs 5-8): crosses clip_bounds twice (exits and re-enters -> 2 parts)
            (100.0, 200.0),
            (100.0, 1500.0),
            (400.0, 1500.0),
            (400.0, 200.0),
            # Off-map lake (IDs 9-12)
            (-4000.0, -4000.0),
            (-3000.0, -4000.0),
            (-3000.0, -3000.0),
            (-4000.0, -3000.0),
            # Huge straddling reservoir (IDs 13-16): extends 10 km outside
            (500.0, 200.0),
            (10000.0, 200.0),
            (10000.0, 10000.0),
            (500.0, 10000.0),
            # Multipolygon outer (IDs 17-20)
            (-500.0, -500.0),
            (1500.0, -500.0),
            (1500.0, 1500.0),
            (-500.0, 1500.0),
            # Island 1 inside clip_bounds (IDs 21-24)
            (200.0, 200.0),
            (300.0, 200.0),
            (300.0, 300.0),
            (200.0, 300.0),
            # Island 2 outside clip_bounds (IDs 25-28)
            (-400.0, -400.0),
            (-200.0, -400.0),
            (-200.0, -200.0),
            (-400.0, -200.0),
        ]
        xs = np.array([p[0] for p in world_pts], dtype=np.float64)
        ys = np.array([p[1] for p in world_pts], dtype=np.float64)
        lons, lats = projection.world_to_geo_array(xs, ys)
        nodes = {
            idx + 1: OsmNode(idx + 1, float(lons[idx]), float(lats[idx]))
            for idx in range(len(world_pts))
        }
        ways = {
            10: OsmWay(10, (1, 2), tags={"waterway": "river", "name": "OffmapRiver"}),
            11: OsmWay(11, (3, 4), tags={"waterway": "river", "name": "CrossingRiver"}),
            12: OsmWay(12, (5, 6, 7, 8), tags={"waterway": "stream", "name": "LoopStream"}),
            20: OsmWay(20, (9, 10, 11, 12, 9), tags={"natural": "water", "name": "OffmapLake"}),
            21: OsmWay(21, (13, 14, 15, 16, 13), tags={"natural": "water", "name": "StraddlingLake"}),
            30: OsmWay(30, (17, 18, 19, 20, 17), tags={}),
            31: OsmWay(31, (21, 22, 23, 24, 21), tags={}),
            32: OsmWay(32, (25, 26, 27, 28, 25), tags={}),
        }
        relations = {
            40: OsmRelation(
                40,
                members=(("way", 30, "outer"), ("way", 31, "inner"), ("way", 32, "inner")),
                tags={"type": "multipolygon", "natural": "water", "name": "ClippedReservoir"},
            )
        }
        osm_data = OsmData(nodes=nodes, ways=ways, relations=relations)
        dem = MockDem(100.0)
        clip_bounds = (0.0, 0.0, 1000.0, 1000.0)

        # Without clip_bounds, everything is returned unclipped
        unclipped = extract_water(osm_data, projection, dem)
        assert len(unclipped.rivers) == 3
        assert len(unclipped.water_bodies) == 3

        clipped = extract_water(osm_data, projection, dem, clip_bounds=clip_bounds)

        # Off-map river dropped; CrossingRiver clipped to [0, 1000]; LoopStream split into 2 parts
        river_names = [r.name for r in clipped.rivers]
        assert "OffmapRiver" not in river_names
        assert river_names.count("CrossingRiver") == 1
        assert river_names.count("LoopStream") == 2
        assert len({r.osm_id for r in clipped.rivers}) == len(clipped.rivers)

        crossing = next(r for r in clipped.rivers if r.name == "CrossingRiver")
        assert crossing.points[0, 0] == pytest.approx(0.0, abs=1e-4)
        assert crossing.points[-1, 0] == pytest.approx(1000.0, abs=1e-4)

        # Off-map lake dropped; straddling lake & multipolygon clipped strictly inside clip_bounds
        wb_names = {b.name for b in clipped.water_bodies}
        assert wb_names == {"StraddlingLake", "ClippedReservoir"}
        for wb in clipped.water_bodies:
            min_x, min_y, max_x, max_y = wb.bounds
            assert min_x >= -1e-4 and min_y >= -1e-4
            assert max_x <= 1000.0 + 1e-4 and max_y <= 1000.0 + 1e-4

        reservoir = next(b for b in clipped.water_bodies if b.name == "ClippedReservoir")
        assert len(reservoir.inner_rings) == 1  # Only on-map island preserved!
        assert reservoir.area_m2 == pytest.approx(1_000_000.0 - 10_000.0, rel=1e-4)


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
        assert obj["cubemap"] == "BNG_Sky_02_cubemap"
        assert obj["useOcclusionQuery"] is False
        assert obj["fresnelBias"] == 0.12
        assert obj["fresnelPower"] == 6.0
        assert obj["overallFoamOpacity"] == 0.0
        assert obj["reflectivity"] == 0.55
        assert obj["baseColor"] == [44, 74, 60, 255]

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
        assert obj["position"] == [55.0, 45.0, 112.0]
        assert obj["scale"] == [90.0, 70.0, 3.5]
        assert obj["rotationMatrix"] == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        assert obj["gridElementSize"] == 8.0
        assert obj["cubemap"] == "BNG_Sky_02_cubemap"
        assert obj["useOcclusionQuery"] is False
        assert obj["fresnelBias"] == 0.12
        assert obj["fresnelPower"] == 6.0
        assert obj["overallFoamOpacity"] == 0.0
        assert obj["reflectivity"] == 0.55
        assert obj["baseColor"] == [44, 74, 60, 255]
        assert 0.0 <= obj["foamAmbientLerp"] <= 1.0
        assert "Waves (vertex undulation)" in obj
        assert "Ripples (texture animation)" in obj
        for layer in obj["Ripples (texture animation)"]:
            assert isinstance(layer["rippleMagnitude"], float)

    def test_water_block_adaptive_grid_element_size_for_large_lake(self):
        # Regression test for multi-km reservoir (e.g. waterblock_87983155: 5852.34 x 2561.53 m)
        body = WaterBody(
            osm_id=87983155,
            name="Горьковское водохранилище",
            water_type="reservoir",
            outer_ring=np.array(
                [[0.0, 0.0], [5852.34, 0.0], [5852.34, 2561.53], [0.0, 2561.53], [0.0, 0.0]],
                dtype=np.float64,
            ),
            depth_m=3.5,
            z_water=84.0,
        )
        obj = build_water_block_object("ivanovo_shuya", body)
        grid = obj["gridElementSize"]
        assert grid == pytest.approx(compute_water_block_grid_element_size(5852.34, 2561.53))
        assert 80.0 < grid <= 96.0
        approx_vertices = (obj["scale"][0] / grid + 1.0) * (obj["scale"][1] / grid + 1.0)
        assert 1000 <= approx_vertices <= 4500

    def test_water_block_grid_element_size_clamped_below_100_and_min_scale(self):
        # Mega reservoir (like waterblock_3235909: 5490.95 x 31406.45 m) must clamp <= 96.0
        assert compute_water_block_grid_element_size(5490.95, 31406.45) == 96.0
        mega = WaterBody(
            osm_id=3235909,
            name="Volga",
            water_type="reservoir",
            outer_ring=np.array(
                [[0.0, 0.0], [5490.95, 0.0], [5490.95, 31406.45], [0.0, 31406.45], [0.0, 0.0]],
                dtype=np.float64,
            ),
            depth_m=4.0,
            z_water=84.0,
        )
        mega_obj = build_water_block_object("ivanovo_shuya", mega)
        assert mega_obj["gridElementSize"] == 96.0
        assert mega_obj["gridElementSize"] < 100.0

        # Small pond (5 x 6 m) must not exceed min(size_x, size_y)
        assert compute_water_block_grid_element_size(5.0, 6.0) == 5.0
        # Tiny degenerate pond (< 1 m) clamped at 1.0
        assert compute_water_block_grid_element_size(0.5, 0.5) == 1.0

    def test_build_river_object_rejects_empty_or_single_point(self):
        empty_channel = SimpleNamespace(
            osm_id=999,
            name="Empty",
            waterway_type="stream",
            points=np.empty((0, 2), dtype=np.float64),
            width_m=4.0,
            depth_m=0.8,
            z_water=np.empty((0,), dtype=np.float64),
            flow_speed=0.8,
        )
        single_channel = SimpleNamespace(
            osm_id=1000,
            name="Single",
            waterway_type="stream",
            points=np.array([[1.0, 2.0]], dtype=np.float64),
            width_m=4.0,
            depth_m=0.8,
            z_water=np.array([100.0], dtype=np.float64),
            flow_speed=0.8,
        )
        with pytest.raises(ValueError, match="at least 2 nodes"):
            build_river_object("ivanovo_test", empty_channel)
        with pytest.raises(ValueError, match="at least 2 nodes"):
            build_river_object("ivanovo_test", single_channel)

    def test_export_water_to_scene_skips_degenerate_river_and_allows_empty_group(self):
        tree = SceneTree("ivanovo_test")
        empty_river = SimpleNamespace(
            osm_id=99,
            name="Empty",
            waterway_type="stream",
            points=np.empty((0, 2), dtype=np.float64),
            width_m=4.0,
            depth_m=0.8,
            z_water=np.empty((0,), dtype=np.float64),
            flow_speed=0.8,
        )
        body = WaterBody(
            osm_id=2,
            name="Lake",
            water_type="lake",
            outer_ring=np.array([[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]], dtype=np.float64),
            depth_m=3.5,
            z_water=98.0,
        )
        network = WaterNetwork(rivers=[empty_river], water_bodies=[body])

        count = export_water_to_scene(tree, network, group_path=())
        assert count == 1
        assert () in tree.objects
        assert tree.objects[()][0]["__parent"] == ROOT_GROUP

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
