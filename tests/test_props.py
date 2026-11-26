"""Tests for Russian roadside props (signs, W-beam guardrails, poles, bus stops)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import numpy as np
import pytest

from ivanovo2beam.beamng.level import SceneTree
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.osm.types import OsmData
from ivanovo2beam.roads.network import Road
from ivanovo2beam.roads.props import (
    BUS_STOP_SHELTER,
    BUS_STOP_SIGN,
    GUARDRAIL_MATERIAL,
    POLE_LIGHT_SINGLE,
    POLE_WOOD_OLD,
    ROAD_PROPS_GROUP,
    SIGN_SHAPES,
    add_roadside_props_to_scene,
    build_wbeam_guardrail_mesh,
    export_guardrail_dae,
)


def _road(osm_id: int, points: np.ndarray, **overrides) -> Road:
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
        "name": "Prop Road",
        "maxspeed_kph": 60.0,
        "priority": 80,
        "drivable": True,
        "material_key": "Asphalt",
        "decal_material": "road_asphalt_2lane",
    }
    base.update(overrides)
    return Road(**base)


class TestRoadsideProps:
    def test_wbeam_guardrail_mesh_and_dae_export(self, tmp_path: Path) -> None:
        pts_3d = np.array([[float(x), 5.0, 100.0] for x in range(0, 33, 4)], dtype=np.float64)
        mesh = build_wbeam_guardrail_mesh(pts_3d, inward_side=-1.0, post_spacing_m=2.0, localize=True)
        assert len(mesh.positions) > 20
        assert len(mesh.triangles) > 20
        assert len(mesh.normals) == len(mesh.positions)
        assert len(mesh.uvs) == len(mesh.positions)
        # Posts sink 0.25m below ground; top beam sits ~0.77m above ground
        zs = [p[2] + mesh.origin[2] for p in mesh.positions]
        assert min(zs) == pytest.approx(99.75, abs=0.05)
        assert max(zs) == pytest.approx(100.77, abs=0.05)

        dae = tmp_path / "guardrail_test.dae"
        assert export_guardrail_dae(dae, mesh, "guardrail_test") > 0
        root = ET.parse(dae).getroot()
        ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
        mat = root.find(".//c:material", ns)
        assert mat is not None and mat.attrib.get("id") == GUARDRAIL_MATERIAL

    def test_signs_poles_bus_stops_and_bridge_guardrails(self, tmp_path: Path) -> None:
        elev = np.full((16, 16), 100.0, dtype=np.float64)
        # Steep drop south of y=-6 for embankment guardrail testing
        elev[:4, :] = 95.0
        build = SimpleNamespace(elevations=elev, origin_x=-50.0, origin_y=-50.0, cell_size_m=50.0)
        tree = SceneTree(level_id="test_level")

        bbox = BBox(west=40.9, south=56.9, east=41.1, north=57.1)
        proj = make_projection(bbox)
        lon_bus, lat_bus = proj.world_to_geo(120.0, -7.0)
        lon_cross, lat_cross = proj.world_to_geo(210.0, -5.0)
        osm = OsmData(
            nodes={1001: (lon_bus, lat_bus), 1002: (lon_cross, lat_cross)},
            node_tags={1001: {"highway": "bus_stop"}, 1002: {"highway": "crossing"}},
        )

        # East-west primary highway leading onto bridge + minor secondary T-junction
        west_app = _road(1, np.array([[0.0, 0.0], [260.0, 0.0]]), highway="primary", maxspeed_kph=60.0, priority=80)
        bridge = _road(2, np.array([[260.0, 0.0], [320.0, 0.0]]), highway="primary", bridge=True, layer=1, priority=80)
        east_app = _road(3, np.array([[320.0, 0.0], [500.0, 0.0]]), highway="primary", maxspeed_kph=60.0, priority=80)
        side = _road(4, np.array([[100.0, 120.0], [100.0, 0.0]]), highway="residential", maxspeed_kph=40.0, priority=30, width_m=7.0)
        rural = _road(5, np.array([[0.0, 200.0], [300.0, 200.0]]), highway="tertiary", maxspeed_kph=60.0, priority=50, width_m=7.0)

        counts = add_roadside_props_to_scene(
            tree,
            [west_app, bridge, east_app, side, rural],
            build,
            level_dir=tmp_path,
            osm_data=osm,
            projection=proj,
        )
        assert counts["signs"] >= 4
        assert counts["poles"] >= 4
        assert counts["bus_stops"] == 1
        assert counts["guardrails"] >= 4

        objs = tree.objects[ROAD_PROPS_GROUP]
        shapes = [o["shapeName"] for o in objs]
        assert SIGN_SHAPES["speed_60"] in shapes
        assert SIGN_SHAPES["bridge"] in shapes
        assert SIGN_SHAPES["zebra"] in shapes
        assert SIGN_SHAPES["give_way"] in shapes or SIGN_SHAPES["priority_road"] in shapes
        assert POLE_LIGHT_SINGLE in shapes
        assert POLE_WOOD_OLD in shapes
        assert BUS_STOP_SHELTER in shapes
        assert BUS_STOP_SIGN in shapes

        # Bus stop rotation opens toward road (+Y inward = (-cos, -sin) -> cos=0, sin=-1)
        bus_obj = next(o for o in objs if o["shapeName"] == BUS_STOP_SHELTER)
        rm = bus_obj["rotationMatrix"]
        # Local -X = (-rm[0], -rm[3]) must point north (+Y) toward road centerline y=0
        assert -rm[3] > 0.9

        # W-beam guardrail materials & DAE files exist under level_dir
        mat_file = tmp_path / "art" / "shapes" / "road_props" / "main.materials.json"
        assert mat_file.is_file()
        mat_doc = json.loads(mat_file.read_text(encoding="utf-8"))
        assert GUARDRAIL_MATERIAL in mat_doc
        for tex in mat_doc[GUARDRAIL_MATERIAL]["Stages"][0].values():
            if isinstance(tex, str) and tex.startswith("/"):
                assert tex.endswith(".dds")
        for gr_obj in (o for o in objs if "/road_props/guardrail_" in o["shapeName"]):
            rel = gr_obj["shapeName"].split("/test_level/", 1)[1]
            assert (tmp_path / rel).is_file()
