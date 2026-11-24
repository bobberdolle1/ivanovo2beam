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
    build_bridge_geometry,
    build_bridge_scene_object,
    compute_bridge_deck_profile,
    create_bridgehead_ramps,
    emit_bridges_materials,
    export_bridge_dae,
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
