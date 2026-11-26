"""Unit tests for 3D bridges, railways, and tram subsystems."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from ivanovo2beam.config import RoadSettings
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.osm.types import OsmData, OsmWay
from ivanovo2beam.roads.bridges import (
    BRIDGE_DECAL_MATERIAL,
    BridgeGeometry,
    build_bridge_geometry,
    build_bridge_scene_object,
    build_bridge_split_geometry,
    compute_bridge_deck_profile,
    create_bridgehead_ramps,
    emit_bridges_materials,
    export_bridge_bundle,
    export_bridge_dae,
    localize_bridge_geometry,
    solve_coupled_bridge_profile,
    water_clear_m,
)
from ivanovo2beam.roads.network import Road
from ivanovo2beam.roads.railways import (
    MATERIAL_RAIL_BALLAST,
    MATERIAL_RAIL_STEEL,
    RUSSIAN_GAUGE_M,
    build_railway_geometry,
    build_railway_scene_object,
    emit_railways_materials,
    export_railway_dae,
    extract_railways,
    parse_gauge,
    railways_to_corridors,
    smooth_rail_grades,
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


# ===========================================================================
# 3D Bridges Subsystem Tests
# ===========================================================================


class TestBridgeDeckAndGeometry:
    @pytest.fixture
    def bridge_road(self) -> Road:
        # 100m straight bridge across a 10m deep river valley
        pts = np.array([[0.0, 0.0], [50.0, 0.0], [100.0, 0.0]])
        return Road(
            osm_id=500,
            highway="primary",
            points=pts,
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=True,
            tunnel=False,
            layer=1,
            name="Ivanovo Bridge",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )

    def test_deck_profile_computation(self, bridge_road) -> None:
        # Ground has a dip at midpoint (0m, -10m, 0m)
        ground_z = np.array([20.0, 5.0, 20.0])
        deck_z = compute_bridge_deck_profile(bridge_road, ground_z=ground_z, clearance_m=4.0)

        # Endpoints must match ground elevation exactly (pinning)
        assert deck_z[0] == pytest.approx(20.0)
        assert deck_z[-1] == pytest.approx(20.0)
        # Midpoint deck elevation must be above ground + clearance (5 + 4 = 9m)
        # Linear chord between 20m and 20m is 20m >= 9m
        assert deck_z[1] == pytest.approx(20.0)

    def test_bridge_mesh_deck_dimensions_and_manifold(self, bridge_road) -> None:
        deck_z = np.array([20.0, 20.0, 20.0])
        geom = build_bridge_geometry(bridge_road, deck_z)

        assert len(geom.positions) > 0
        assert len(geom.triangles) > 0
        assert len(geom.normals) == len(geom.positions)
        assert len(geom.uvs) == len(geom.positions)

        # Check deck slab width = road.width_m + 1.2m = 11.2m
        # y coordinates of slab vertices must reach +/- 5.6m
        y_coords = [p[1] for p in geom.positions]
        assert max(y_coords) >= 5.59
        assert min(y_coords) <= -5.59

        # Check guardrail height = deck_z + 0.85m = 20.85m
        z_coords = [p[2] for p in geom.positions]
        assert max(z_coords) == pytest.approx(20.85, abs=0.01)

        # Check piers every 25m: for a 100m bridge, piers at 25m, 50m, 75m
        # Pier columns extend down below deck bottom (20 - 0.65 = 19.35)
        assert min(z_coords) < 19.0

    def test_export_bridge_dae(self, bridge_road, tmp_path: Path) -> None:
        deck_z = np.array([20.0, 20.0, 20.0])
        geom = build_bridge_geometry(bridge_road, deck_z)
        dae_file = tmp_path / "art" / "shapes" / "bridges" / "bridge_way500.dae"

        bytes_written = export_bridge_dae(dae_file, geom, "bridge_way500")
        assert bytes_written > 0
        assert dae_file.is_file()

        # Parse XML and verify COLLADA structure
        tree = ET.parse(dae_file)
        root = tree.getroot()
        assert "COLLADA" in root.tag
        # Verify material binding
        mat_elem = root.find(".//{http://www.collada.org/2005/11/COLLADASchema}material")
        assert mat_elem is not None
        assert mat_elem.attrib.get("id") == BRIDGE_DECAL_MATERIAL

        # Verify UV texcoords input
        texcoord_input = root.find(".//{http://www.collada.org/2005/11/COLLADASchema}input[@semantic='TEXCOORD']")
        assert texcoord_input is not None

    def test_emit_bridges_materials(self, tmp_path: Path) -> None:
        mat_path = emit_bridges_materials(tmp_path, "ivanovo_mvp")
        assert mat_path.is_file()

        doc = json.loads(mat_path.read_text(encoding="utf-8"))
        assert BRIDGE_DECAL_MATERIAL in doc
        spec = doc[BRIDGE_DECAL_MATERIAL]
        assert spec["class"] == "Material"
        stages = spec["Stages"][0]
        assert "concrete_precast1" in stages["baseColorMap"]
        assert "concrete_precast1" in stages["normalMap"]

    def test_build_bridge_scene_object(self, bridge_road) -> None:
        obj = build_bridge_scene_object(bridge_road, "ivanovo_mvp")
        assert obj["class"] == "TSStatic"
        assert obj["collisionType"] == "Visible Mesh Final"
        assert obj["decalType"] == "Visible Mesh"
        assert "bridge_way500.dae" in obj["shapeName"]

    def test_create_bridgehead_ramps_zero_step_lip(self, bridge_road) -> None:
        deck_z = np.array([25.0, 25.0, 25.0])
        ramps = create_bridgehead_ramps(
            bridge_road,
            RoadSettings(),
            deck_z,
            ground_sampler=lambda x, y: 20.0,
            ramp_length_m=25.0,
        )
        # Must produce 2 approach ramp corridors (start and end)
        assert len(ramps) == 2

        # Start ramp: ends at bridge start point with exact deck elevation (0cm step lip)
        ramp_start = ramps[0]
        assert ramp_start.grade_terrain is True
        assert ramp_start.surface_z is not None
        assert ramp_start.surface_z[-1] == pytest.approx(25.0)  # Pinned to deck_z[0]
        assert ramp_start.surface_z[0] == pytest.approx(20.0)   # Ground elevation 25m out

        # End ramp: starts at bridge end point with exact deck elevation (0cm step lip)
        ramp_end = ramps[1]
        assert ramp_end.grade_terrain is True
        assert ramp_end.surface_z is not None
        assert ramp_end.surface_z[0] == pytest.approx(25.0)   # Pinned to deck_z[-1]
        assert ramp_end.surface_z[-1] == pytest.approx(20.0)  # Ground elevation 25m out

    def test_bridge_mesh_localization_and_scene_position(self, tmp_path: Path) -> None:
        # Distant bridge ~20 km from map origin (matching way183556845 pattern)
        pts = np.array([[19929.0, 28.0], [19952.5, 29.7], [19976.0, 31.4]])
        distant_road = Road(
            osm_id=183556845,
            highway="primary",
            points=pts,
            width_m=8.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=True,
            tunnel=False,
            layer=1,
            name="Distant Bridge",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        deck_z = np.array([95.4, 96.0, 96.6])

        world_geom = build_bridge_geometry(distant_road, deck_z)
        assert world_geom.origin == (0.0, 0.0, 0.0)
        origin = world_geom.center
        assert origin[0] == pytest.approx(19952.5, abs=0.5)
        assert origin[1] == pytest.approx(29.7, abs=0.5)
        assert origin[2] == pytest.approx(min(p[2] for p in world_geom.positions), abs=1e-3)

        local_geom = build_bridge_geometry(distant_road, deck_z, localize=True)
        assert local_geom.origin == origin
        assert local_geom.center == origin
        for wp, lp in zip(world_geom.positions, local_geom.positions, strict=True):
            assert lp[0] + origin[0] == pytest.approx(wp[0], abs=1e-4)
            assert lp[1] + origin[1] == pytest.approx(wp[1], abs=1e-4)
            assert lp[2] + origin[2] == pytest.approx(wp[2], abs=1e-4)
        assert max(abs(lp[0]) for lp in local_geom.positions) < 40.0
        assert max(abs(lp[1]) for lp in local_geom.positions) < 20.0
        assert min(lp[2] for lp in local_geom.positions) == pytest.approx(0.0, abs=1e-4)

        # localize_bridge_geometry is idempotent
        relocalized = localize_bridge_geometry(local_geom)
        assert relocalized.origin == origin
        assert relocalized.positions == local_geom.positions

        # Backward compatibility: default BridgeGeometry has zero origin
        manual_geom = BridgeGeometry()
        assert manual_geom.origin == (0.0, 0.0, 0.0)
        assert manual_geom.center == (0.0, 0.0, 0.0)

        # Export both ways (world+origin vs pre-localized) and verify COLLADA vertices are local
        dae_from_world = tmp_path / "world_origin.dae"
        dae_from_local = tmp_path / "pre_local.dae"
        dae_no_double = tmp_path / "no_double.dae"
        export_bridge_dae(dae_from_world, world_geom, "bridge_way183556845", origin=origin)
        export_bridge_dae(dae_from_local, local_geom, "bridge_way183556845")
        export_bridge_dae(dae_no_double, local_geom, "bridge_way183556845", origin=origin)
        assert dae_from_world.read_bytes() == dae_from_local.read_bytes() == dae_no_double.read_bytes()

        ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
        root = ET.parse(dae_from_world).getroot()
        pos_elem = root.find(".//c:float_array[@id='bridge_way183556845-mesh-positions-array']", ns)
        assert pos_elem is not None and pos_elem.text is not None
        vals = [float(v) for v in pos_elem.text.split()]
        xs = vals[0::3]
        assert max(abs(x) for x in xs) < 40.0

        # Scene object carries world-space TSStatic.position matching origin
        obj_default = build_bridge_scene_object(distant_road, "ivanovo_shuya")
        assert obj_default["position"] == [0.0, 0.0, 0.0]
        obj_loc = build_bridge_scene_object(distant_road, "ivanovo_shuya", position=origin)
        assert obj_loc["position"] == list(origin)

    @pytest.mark.parametrize(
        ("material_key", "expected_shoulder"),
        [
            ("Asphalt", "Grass"),
            ("Asphalt_worn", "Grass"),
            ("Concrete", "Grass"),
            ("Dirt", "Dirt"),
            ("Gravel", "Gravel"),
        ],
    )
    def test_create_bridgehead_ramps_shoulder_materials(
        self, bridge_road, material_key: str, expected_shoulder: str
    ) -> None:
        from dataclasses import replace

        road = replace(bridge_road, material_key=material_key)
        ramps = create_bridgehead_ramps(
            road,
            RoadSettings(),
            np.array([22.0, 22.0, 22.0]),
            ground_sampler=lambda _x, _y: 20.0,
        )
        assert len(ramps) == 2
        assert all(r.shoulder_material_key == expected_shoulder for r in ramps)

    def test_solve_coupled_bridge_profile_2_node_clears_river_and_theil_sen(self) -> None:
        from ivanovo2beam.water.extract import RiverChannel, WaterNetwork

        # 2-node 60m bridge spanning a 25m-wide river at z_water=100m, with riverbank drop near portals
        bridge = Road(
            osm_id=274102985,
            highway="primary",
            points=np.array([[0.0, 0.0], [60.0, 0.0]]),
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=True,
            tunnel=False,
            layer=1,
            name="Uvod Bridge",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        river = RiverChannel(
            osm_id=136383439,
            name="Uvod",
            waterway_type="river",
            points=np.array([[30.0, -200.0], [30.0, 200.0]]),
            width_m=25.0,
            depth_m=2.5,
            z_water=np.array([100.0, 100.0]),
        )
        water_net = WaterNetwork(rivers=[river])

        # Approach terrain is 101m behind portals, dipping to 99m right at portal edges
        def ground(x: float, _y: float) -> float:
            if x < -4.0 or x > 64.0:
                return 101.0
            return 99.0

        pts_r, deck_z = solve_coupled_bridge_profile(bridge, ground_sampler=ground, water_network=water_net)
        assert pts_r.shape[0] >= 10
        assert deck_z.shape[0] == pts_r.shape[0]
        # Must clear water by at least water_clear_m(25m) = 3.75m -> >= 103.75m over mid-span
        mid_idx = pts_r.shape[0] // 2
        assert deck_z[mid_idx] >= 100.0 + water_clear_m(25.0) - 0.05
        # No sag: deck never dips below chord between abutments
        chord = np.linspace(deck_z[0], deck_z[-1], deck_z.size)
        assert np.all(deck_z >= chord - 1e-4)

    def test_export_bridge_bundle_split_deck_and_structure(self, tmp_path: Path) -> None:
        from dataclasses import replace

        pts = np.array([[7913.75, -8304.70], [7944.14, -8306.65], [7974.53, -8308.59]])
        road = Road(
            osm_id=274102985,
            highway="primary",
            points=pts,
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=True,
            tunnel=False,
            layer=1,
            name="Uvod",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        deck_z = np.array([104.0, 104.5, 104.0])
        # Bed at 96.0m -> grounded piers should reach 96.0 - 1.5 = 94.5m
        bed_sampler = lambda _x, _y: 96.0  # noqa: E731

        deck_geom, struct_geom = build_bridge_split_geometry(road, deck_z, ground_sampler=bed_sampler, localize=True)
        assert deck_geom.origin == struct_geom.origin
        assert max(p[2] for p in deck_geom.positions) + deck_geom.origin[2] == pytest.approx(104.5, abs=0.01)
        # 0.15m top skirt on _deck
        assert min(p[2] for p in deck_geom.positions) + deck_geom.origin[2] == pytest.approx(104.0 - 0.15, abs=0.01)
        # Grounded piers/abutments reach 1.5m below 96.0m riverbed -> 94.5m
        assert min(p[2] for p in struct_geom.positions) + struct_geom.origin[2] == pytest.approx(94.5, abs=0.05)

        objs = export_bridge_bundle(
            replace(road),
            deck_z,
            "ivanovo_golden_sector",
            tmp_path,
            ground_sampler=bed_sampler,
        )
        assert len(objs) == 2
        deck_obj, struct_obj = objs
        assert deck_obj["decalType"] == "Visible Mesh"
        assert struct_obj["decalType"] == "None"
        assert deck_obj["position"] == struct_obj["position"]
        assert deck_obj["shapeName"].endswith("_deck.dae")
        assert (tmp_path / "bridge_way274102985_deck.dae").is_file()
        assert (tmp_path / "bridge_way274102985.dae").is_file()

        # Default adaptive bridgehead ramps span >= 70m and end lips sit >= 0.05m below deck
        ramps = create_bridgehead_ramps(road, RoadSettings(), deck_z, ground_sampler=bed_sampler)
        assert len(ramps) == 2
        assert ramps[0].length_m >= 70.0
        assert ramps[1].length_m >= 70.0
        end_lip_pts = [
            p[2] + struct_geom.origin[2]
            for p in struct_geom.positions
            if (p[0] + struct_geom.origin[0] < 7912.5 or p[0] + struct_geom.origin[0] > 7975.8)
        ]
        assert end_lip_pts and max(end_lip_pts) <= 104.0 - 0.049

    def test_c1_hermite_bridgehead_ramps_match_portal_slope_and_multi_material_dae(self, tmp_path: Path) -> None:
        from ivanovo2beam.roads.bridges import (
            BRIDGE_PARAPET_MATERIAL,
            BRIDGE_STEEL_MATERIAL,
        )

        pts = np.array([[0.0, 0.0], [30.0, 0.0], [60.0, 0.0]])
        road = Road(
            osm_id=274109999,
            highway="primary",
            points=pts,
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=True,
            tunnel=False,
            layer=1,
            name="Smooth Bridge",
            maxspeed_kph=90.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        # Rising portal at 3% grade into deck crest
        deck_z = np.array([100.0, 100.9, 100.2])
        ramps = create_bridgehead_ramps(
            road,
            RoadSettings(),
            deck_z,
            ground_sampler=lambda _x, _y: 98.5,
            ramp_length_m=60.0,
        )
        assert len(ramps) == 2
        start_ramp, end_ramp = ramps
        assert start_ramp.surface_z is not None and end_ramp.surface_z is not None
        # Check slope right into start portal matches +3% deck slope (no flat cosine kink)
        ds_in = float(np.hypot(*(start_ramp.points[-1] - start_ramp.points[-2])))
        ramp_portal_slope = (float(start_ramp.surface_z[-1]) - float(start_ramp.surface_z[-2])) / ds_in
        assert ramp_portal_slope == pytest.approx(0.03, abs=0.004)
        # No sudden jump > 0.01 grade change between adjacent ramp segments
        for r in (start_ramp, end_ramp):
            dxy = np.hypot(np.diff(r.points[:, 0]), np.diff(r.points[:, 1]))
            slopes = np.diff(r.surface_z) / dxy  # type: ignore[arg-type]
            assert float(np.max(np.abs(np.diff(slopes)))) < 0.01

        # Multi-material COLLADA & main.materials.json include deck, concrete parapet, and galvanized steel rails
        mat_path = emit_bridges_materials(tmp_path, "test_level")
        mats = json.loads(mat_path.read_text(encoding="utf-8"))
        assert {BRIDGE_DECAL_MATERIAL, BRIDGE_PARAPET_MATERIAL, BRIDGE_STEEL_MATERIAL} <= set(mats)

        _, struct_geom = build_bridge_split_geometry(road, deck_z, ground_sampler=lambda _x, _y: 95.0, localize=True)
        dae = tmp_path / "bridge_way274109999.dae"
        export_bridge_dae(dae, struct_geom, "bridge_way274109999")
        root = ET.parse(dae).getroot()
        ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
        mat_ids = [m.attrib.get("id") for m in root.findall(".//c:material", ns)]
        assert mat_ids[0] == BRIDGE_DECAL_MATERIAL
        assert BRIDGE_PARAPET_MATERIAL in mat_ids
        assert BRIDGE_STEEL_MATERIAL in mat_ids


# ===========================================================================
# Railways & Trams Subsystem Tests
# ===========================================================================


class TestRailwaysSubsystem:
    def test_parse_gauge(self) -> None:
        assert parse_gauge(None) == RUSSIAN_GAUGE_M
        assert parse_gauge("1520") == 1.520
        assert parse_gauge("1524") == 1.524
        assert parse_gauge("1435") == 1.435
        assert parse_gauge("1000") == 1.000
        assert parse_gauge("1.52 m") == 1.52

    def test_extract_railways(self, projection) -> None:
        osm = make_osm(
            projection,
            [
                ([(0, 0), (500, 0)], {"railway": "rail", "gauge": "1520", "electrified": "contact_line"}, 601),
                ([(0, 100), (500, 100)], {"railway": "tram"}, 602),
                ([(0, 200), (500, 200)], {"railway": "light_rail"}, 603),
                ([(0, 300), (500, 300)], {"highway": "primary"}, 604),  # Non-railway way skipped
            ],
        )
        railways = extract_railways(osm, projection)
        assert len(railways) == 3
        classes = {rw.railway for rw in railways}
        assert classes == {"rail", "tram", "light_rail"}

        rail = next(rw for rw in railways if rw.railway == "rail")
        assert rail.gauge_m == RUSSIAN_GAUGE_M
        assert rail.electrified is True

    def test_smooth_rail_grades(self) -> None:
        # 1000m track with a steep 100m cliff in the middle (grade = 0.50, far above 0.035)
        pts = np.array([[float(x), 0.0] for x in range(0, 1001, 50)])
        raw_z = np.zeros(len(pts))
        raw_z[10:] = 100.0  # steep 100m step

        smoothed_z = smooth_rail_grades(pts, raw_z, max_grade=0.035)
        diff_z = np.diff(smoothed_z)
        diff_s = np.diff(pts[:, 0])
        grades = np.abs(diff_z / diff_s)

        # Longitudinal grade must never exceed 0.035 anywhere along track
        assert np.all(grades <= 0.035001)

    def test_railways_to_corridors(self, projection) -> None:
        rw = extract_railways(
            make_osm(
                projection,
                [([(0, 0), (200, 0)], {"railway": "rail"}, 701)],
            ),
            projection,
        )[0]
        corridors = railways_to_corridors([rw])
        assert len(corridors) == 1
        corr = corridors[0]
        assert corr.material_key == "Gravel"
        assert corr.shoulder_material_key == "Dirt"
        assert corr.grade_terrain is True

    def test_railway_3d_geometry_and_flush_road_crossing(self, projection) -> None:
        # Railway running along X axis from (0, 0) to (100, 0)
        rw_pts = np.array([[0.0, 0.0], [50.0, 0.0], [100.0, 0.0]])
        rw = extract_railways(
            make_osm(projection, [(rw_pts, {"railway": "rail"}, 801)]),
            projection,
        )[0]

        # Crossing road running along Y axis across X=50
        road_pts = np.array([[50.0, -20.0], [50.0, 20.0]])
        road = Road(
            osm_id=901,
            highway="primary",
            points=road_pts,
            width_m=8.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="Crossing Road",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )

        mesh = build_railway_geometry(rw, roads=[road])

        # Must have both ballast and steel rail triangles
        assert len(mesh.ballast_triangles) > 0
        assert len(mesh.steel_triangles) > 0

        # Verify Russian 1520mm gauge: rails must be offset +/- 0.76m from centerline
        y_coords = [p[1] for p in mesh.positions]
        assert pytest.approx(0.76, abs=0.05) in [round(y, 2) for y in y_coords]
        assert pytest.approx(-0.76, abs=0.05) in [round(y, 2) for y in y_coords]

        # Verify flush road crossing at x=50m:
        # Rail height at road crossing is flush (<= 0.02m) rather than standard 0.18m
        crossing_pts = [p for p in mesh.positions if abs(p[0] - 50.0) < 1.0]
        assert len(crossing_pts) > 0
        crossing_z = [p[2] for p in crossing_pts]
        assert max(crossing_z) <= 0.05  # Flush at grade crossing

    def test_export_railway_dae(self, projection, tmp_path: Path) -> None:
        rw_pts = np.array([[0.0, 0.0], [100.0, 0.0]])
        rw = extract_railways(make_osm(projection, [(rw_pts, {"railway": "rail"}, 802)]), projection)[0]
        mesh = build_railway_geometry(rw)

        dae_file = tmp_path / "art" / "shapes" / "railways" / "railway_way802.dae"
        bytes_written = export_railway_dae(dae_file, mesh, "railway_way802")
        assert bytes_written > 0
        assert dae_file.is_file()

        tree = ET.parse(dae_file)
        root = tree.getroot()
        assert "COLLADA" in root.tag

        # Verify multi-material elements
        materials = [elem.attrib.get("id") for elem in root.findall(".//{http://www.collada.org/2005/11/COLLADASchema}material")]
        assert MATERIAL_RAIL_BALLAST in materials
        assert MATERIAL_RAIL_STEEL in materials

    def test_emit_railways_materials(self, tmp_path: Path) -> None:
        mat_path = emit_railways_materials(tmp_path, "ivanovo_mvp")
        assert mat_path.is_file()

        doc = json.loads(mat_path.read_text(encoding="utf-8"))
        assert MATERIAL_RAIL_BALLAST in doc
        assert MATERIAL_RAIL_STEEL in doc
        assert "dirt_road_gravels" in doc[MATERIAL_RAIL_BALLAST]["Stages"][0]["baseColorMap"]
        assert "aircraft_corrugated_metal" in doc[MATERIAL_RAIL_STEEL]["Stages"][0]["baseColorMap"]

    def test_build_railway_scene_object(self, projection) -> None:
        rw_pts = np.array([[0.0, 0.0], [100.0, 0.0]])
        rw = extract_railways(make_osm(projection, [(rw_pts, {"railway": "rail"}, 803)]), projection)[0]
        obj = build_railway_scene_object(rw, "ivanovo_mvp")
        assert obj["class"] == "TSStatic"
        assert obj["collisionType"] == "Visible Mesh Final"
        assert "railway_way803.dae" in obj["shapeName"]


class TestWindingAndEdgeCases:
    @staticmethod
    def _assert_outward_winding(positions, normals, triangles) -> None:
        for (vi0, ni0, _), (vi1, _, _), (vi2, _, _) in triangles:
            p0 = np.asarray(positions[vi0], dtype=np.float64)
            p1 = np.asarray(positions[vi1], dtype=np.float64)
            p2 = np.asarray(positions[vi2], dtype=np.float64)
            n = np.asarray(normals[ni0], dtype=np.float64)
            cross = np.cross(p1 - p0, p2 - p0)
            assert float(np.dot(cross, n)) > 0.0

    def test_bridge_mesh_winding_no_end_kink_and_zero_seg_safety(self) -> None:
        # Include a duplicate point (zero-length segment) and bank points near ends
        pts = np.array([[0.0, 0.0], [5.0, 0.0], [50.0, 0.0], [50.0, 0.0], [95.0, 0.0], [100.0, 0.0]])
        road = Road(
            osm_id=550,
            highway="primary",
            points=pts,
            width_m=10.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=True,
            tunnel=False,
            layer=1,
            name="Winding Bridge",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )
        # Bank vertices near endpoints are at ground level (20m) while middle dips to 5m
        raw_z = np.array([20.0, 20.0, 5.0, 5.0, 20.0, 20.0])
        deck_z = compute_bridge_deck_profile(road, ground_z=raw_z, clearance_m=4.0)
        # No 4m vertical jump between i=0 (20.0) and i=1 (5m from bridgehead)
        assert deck_z[1] - deck_z[0] <= 0.5
        assert deck_z[-2] - deck_z[-1] <= 0.5

        geom = build_bridge_geometry(road, deck_z)
        self._assert_outward_winding(geom.positions, geom.normals, geom.triangles)

    def test_railway_mesh_winding_segment_crossing_and_tram_corridor(self, projection) -> None:
        # 2-vertex railway crossing a road at x=50 without any intermediate vertex
        rw_rail = extract_railways(
            make_osm(projection, [(np.array([[0.0, 0.0], [100.0, 0.0]]), {"railway": "rail"}, 850)]),
            projection,
        )[0]
        rw_tram = extract_railways(
            make_osm(projection, [(np.array([[0.0, 20.0], [100.0, 20.0]]), {"railway": "tram"}, 851)]),
            projection,
        )[0]

        road = Road(
            osm_id=950,
            highway="primary",
            points=np.array([[50.0, -20.0], [50.0, 20.0]]),
            width_m=8.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="Crossing Road",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )

        mesh = build_railway_geometry(rw_rail, roads=[road])
        self._assert_outward_winding(mesh.positions, mesh.normals, mesh.ballast_triangles)
        self._assert_outward_winding(mesh.positions, mesh.normals, mesh.steel_triangles)

        # Even without an intermediate vertex, the road crossing at x=50 must be flush (z == 0.0)
        crossing_pts = [p for p in mesh.positions if abs(p[0] - 50.0) < 1.0]
        assert crossing_pts
        assert max(p[2] for p in crossing_pts) == pytest.approx(0.0, abs=1e-6)

        # Tram must NOT produce gravel terrain corridors that overwrite city streets
        corridors = railways_to_corridors([rw_rail, rw_tram])
        assert len(corridors) == 1
        assert corridors[0].key == f"railway_way{rw_rail.osm_id}"

        # smooth_rail_grades must not propagate a single NaN across the whole profile
        nan_z = np.array([10.0, float("nan"), 10.5])
        smoothed = smooth_rail_grades(np.array([[0.0, 0.0], [50.0, 0.0], [100.0, 0.0]]), nan_z)
        assert np.all(np.isfinite(smoothed))

    def test_export_railways_sector_chunking_and_crossing_index(self, projection, tmp_path: Path) -> None:
        from ivanovo2beam.roads.railways import RoadCrossingIndex, export_railways, is_crossing_road

        # Two railways in sector (0, 0) and one railway in sector (1, 0) (chunk_size=4000m)
        rws = extract_railways(
            make_osm(
                projection,
                [
                    (np.array([[100.0, 100.0], [300.0, 100.0]]), {"railway": "rail"}, 860),
                    (np.array([[500.0, 200.0], [700.0, 200.0]]), {"railway": "rail"}, 861),
                    (np.array([[4500.0, 100.0], [4700.0, 100.0]]), {"railway": "rail"}, 862),
                ],
            ),
            projection,
        )
        road = Road(
            osm_id=960,
            highway="primary",
            points=np.array([[200.0, 0.0], [200.0, 300.0]]),
            width_m=8.0,
            lanes=2,
            surface="asphalt",
            oneway=False,
            bridge=False,
            tunnel=False,
            layer=0,
            name="Cross",
            maxspeed_kph=60.0,
            priority=80,
            drivable=True,
            material_key="Asphalt",
            decal_material="road_asphalt_2lane",
        )

        # RoadCrossingIndex matches unindexed crossing detection
        idx = RoadCrossingIndex.from_roads([road])
        assert is_crossing_road((200.0, 100.0), None, road_index=idx) is True
        assert is_crossing_road((400.0, 100.0), None, road_index=idx) is False

        objs, stats = export_railways(rws, "test_lvl", tmp_path, roads=[road], chunk_size_m=4000.0)
        assert stats["railways"] == 3
        assert stats["chunks"] == 2
        assert len(objs) == 2
        assert all(o["meshCulling"] is True for o in objs)
        assert all("rail_chunk_" in o["name"] for o in objs)
        for o in objs:
            rel = o["shapeName"].removeprefix("/levels/test_lvl/")
            assert (tmp_path / rel).is_file()
        assert (tmp_path / "art" / "shapes" / "railways" / "main.materials.json").is_file()

