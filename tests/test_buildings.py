"""Tests for building generation.

The two things worth pinning here are the shape of the generated geometry and
the validity of the COLLADA. A building mesh that is inside-out, open at the
bottom, or has no collision volume produces a level that looks subtly wrong in
ways that are very hard to diagnose from inside the game, so the mesh is checked
directly: closed, correctly wound, above ground, and with a plausible triangle
count for its footprint.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from ivanovo2beam.buildings.export import (
    UNIQUE_AREA_M2,
    build_archetypes,
    choose_prototypes,
    export_buildings,
)
from ivanovo2beam.buildings.footprints import (
    BUILDING_CLASSES,
    BuildingFootprint,
    building_class_for,
    ensure_counter_clockwise,
    extract_buildings,
    footprint_ring_area,
    parse_height_m,
    parse_levels,
)
from ivanovo2beam.buildings.mesh import (
    Mesh,
    extrude_footprint,
    mesh_to_dae_xml,
    roof_height_for,
    write_dae,
)
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.osm.types import OsmData, OsmWay

BBOX = BBox(west=39.0, south=56.1, east=43.2, north=57.8)
COLLADA_NS = "{http://www.collada.org/2005/11/COLLADASchema}"


@pytest.fixture(scope="module")
def projection():
    return make_projection(BBOX, origin_lon=40.97393, origin_lat=56.99987)


def rectangle(width: float, height: float, x: float = 0.0, y: float = 0.0) -> np.ndarray:
    """Return a closed, counter-clockwise rectangular ring."""
    return np.array(
        [
            [x, y],
            [x + width, y],
            [x + width, y + height],
            [x, y + height],
            [x, y],
        ],
        dtype=np.float64,
    )


class TestHeightAndLevelsParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("12", 12.0), ("12 m", 12.0), ("12.5", 12.5), ("12,5", 12.5), ("40'", 12.192)],
    )
    def test_heights(self, value: str, expected: float) -> None:
        assert parse_height_m(value) == pytest.approx(expected, rel=1e-3)

    @pytest.mark.parametrize("value", [None, "", "tall", "0.5", "900"])
    def test_implausible_heights_rejected(self, value: str | None) -> None:
        assert parse_height_m(value) is None

    @pytest.mark.parametrize(("value", "expected"), [("5", 5.0), ("5.0", 5.0), ("12", 12.0)])
    def test_levels(self, value: str, expected: float) -> None:
        assert parse_levels(value) == expected

    @pytest.mark.parametrize("value", [None, "", "many", "0", "100"])
    def test_implausible_levels_rejected(self, value: str | None) -> None:
        assert parse_levels(value) is None


class TestClassification:
    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("house", "house"),
            ("detached", "house"),
            ("apartments", "apartments"),
            ("residential", "apartments"),
            ("industrial", "industrial"),
            ("warehouse", "industrial"),
            ("commercial", "commercial"),
            ("retail", "commercial"),
            ("garage", "garage"),
            ("garages", "garage"),
            ("shed", "shed"),
            ("barn", "shed"),
            ("school", "school"),
            ("hospital", "hospital"),
            ("church", "church"),
            ("  HOUSE  ", "house"),
        ],
    )
    def test_known_values(self, tag: str, expected: str) -> None:
        assert building_class_for(tag).key == expected

    def test_unknown_and_missing_values_fall_back(self) -> None:
        assert building_class_for("something_new").key == "yes"
        assert building_class_for(None).key == "yes"
        assert building_class_for("yes").key == "yes"

    def test_every_class_has_sane_defaults(self) -> None:
        for key, spec in BUILDING_CLASSES.items():
            assert spec.levels > 0, key
            assert spec.level_height_m > 0, key
            assert spec.height_m > 0, key
            assert spec.roof in {"flat", "gable", "hip"}, key
            assert len(spec.colour) == 3
            assert all(0.0 <= c <= 1.0 for c in spec.colour), key


class TestRingGeometry:
    def test_signed_area_sign_follows_winding(self) -> None:
        ccw = rectangle(10.0, 10.0)
        assert footprint_ring_area(ccw) > 0
        assert footprint_ring_area(ccw[::-1]) < 0

    def test_ensure_counter_clockwise_flips_clockwise_rings(self) -> None:
        flipped = ensure_counter_clockwise(rectangle(10.0, 10.0)[::-1])
        assert footprint_ring_area(flipped) > 0

    def test_ensure_counter_clockwise_leaves_ccw_alone(self) -> None:
        original = rectangle(10.0, 10.0)
        assert np.array_equal(ensure_counter_clockwise(original), original)


def _make_osm(projection, shapes):
    """Build OsmData, sharing node ids between coincident coordinates."""
    nodes: dict[int, tuple[float, float]] = {}
    node_ids: dict[tuple[float, float], int] = {}
    ways: list[OsmWay] = []
    next_id = 1
    for osm_id, (coords, tags) in enumerate(shapes, start=1):
        refs = []
        for x, y in coords:
            key = (round(float(x), 6), round(float(y), 6))
            if key not in node_ids:
                lon, lat = projection.world_to_geo(x, y)
                nodes[next_id] = (lon, lat)
                node_ids[key] = next_id
                next_id += 1
            refs.append(node_ids[key])
        ways.append(OsmWay(id=osm_id, nodes=tuple(refs), tags=dict(tags)))
    return OsmData(nodes=nodes, node_tags={}, ways=ways, relations=[], source="test")


class TestExtraction:
    _osm = staticmethod(_make_osm)

    def test_extracts_a_simple_building(self, projection) -> None:
        osm = self._osm(
            projection, [(rectangle(12.0, 8.0).tolist(), {"building": "house", "height": "7"})]
        )
        buildings, stats = extract_buildings(osm, projection)
        assert len(buildings) == 1
        assert stats.buildings == 1
        assert buildings[0].klass == "house"
        assert buildings[0].height_m == pytest.approx(7.0)
        assert buildings[0].height_source == "height"
        assert buildings[0].area_m2 == pytest.approx(96.0, rel=1e-2)

    def test_levels_drive_height_when_height_is_absent(self, projection) -> None:
        osm = self._osm(
            projection,
            [(rectangle(20.0, 15.0).tolist(), {"building": "apartments", "building:levels": "9"})],
        )
        building = extract_buildings(osm, projection)[0][0]
        spec = BUILDING_CLASSES["apartments"]
        assert building.height_m == pytest.approx(9 * spec.level_height_m)
        assert building.height_source == "levels"

    def test_class_default_is_the_last_resort(self, projection) -> None:
        osm = self._osm(projection, [(rectangle(12.0, 8.0).tolist(), {"building": "yes"})])
        building = extract_buildings(osm, projection)[0][0]
        assert building.height_source == "default"
        assert building.height_m == pytest.approx(BUILDING_CLASSES["yes"].height_m)

    def test_open_ways_are_ignored(self, projection) -> None:
        open_way = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
        osm = self._osm(projection, [(open_way, {"building": "house"})])
        buildings, _ = extract_buildings(osm, projection)
        assert buildings == []

    def test_non_building_ways_are_ignored(self, projection) -> None:
        osm = self._osm(projection, [(rectangle(12.0, 8.0).tolist(), {"landuse": "grass"})])
        assert extract_buildings(osm, projection)[0] == []

    def test_tiny_footprints_are_dropped(self, projection) -> None:
        osm = self._osm(projection, [(rectangle(1.0, 1.0).tolist(), {"building": "yes"})])
        buildings, stats = extract_buildings(osm, projection)
        assert buildings == []
        assert stats.skipped_area == 1

    def test_footprints_outside_the_clip_are_dropped(self, projection) -> None:
        osm = self._osm(
            projection,
            [
                (rectangle(12.0, 8.0, 0.0, 0.0).tolist(), {"building": "house"}),
                (rectangle(12.0, 8.0, 5000.0, 5000.0).tolist(), {"building": "house"}),
            ],
        )
        buildings, _ = extract_buildings(
            osm, projection, clip_bounds=(-100.0, -100.0, 100.0, 100.0)
        )
        assert len(buildings) == 1

    def test_simplification_reduces_vertex_count(self, projection) -> None:
        # A near-circle traced with many vertices: geometrically a blob, but the
        # OSM data may carry fifty points for it.
        angles = np.linspace(0.0, 2 * math.pi, 49)
        circle = [(20 + 10 * math.cos(a), 20 + 10 * math.sin(a)) for a in angles]
        osm = self._osm(projection, [(circle, {"building": "yes"})])
        coarse = extract_buildings(osm, projection, simplify_tolerance_m=2.0)[0][0]
        assert coarse.ring.shape[0] < len(circle)

    def test_max_buildings_keeps_the_largest(self, projection) -> None:
        shapes = [
            (rectangle(10.0 + i * 10, 10.0 + i * 10, i * 100.0, 0.0).tolist(), {"building": "yes"})
            for i in range(5)
        ]
        osm = self._osm(projection, shapes)
        buildings, _ = extract_buildings(osm, projection, max_buildings=2)
        assert len(buildings) == 2
        assert buildings[0].area_m2 > buildings[1].area_m2

    def test_multipolygon_buildings_are_extracted(self, projection) -> None:
        from ivanovo2beam.osm.types import OsmRelation

        osm = self._osm(projection, [(rectangle(12.0, 8.0).tolist(), {"building": "house"})])
        osm.relations.append(
            OsmRelation(
                id=99, members=(("way", 1, "outer"),), tags={"type": "multipolygon", "building": "yes"}
            )
        )
        buildings, stats = extract_buildings(osm, projection)
        assert stats.relations == 1
        assert len(buildings) == 1
        assert buildings[0].osm_id == -99

    def test_degenerate_multipolygons_are_counted_as_skipped(self, projection) -> None:
        from ivanovo2beam.osm.types import OsmRelation

        osm = OsmData(nodes={}, node_tags={}, ways=[], relations=[], source="test")
        osm.relations.append(
            OsmRelation(
                id=99, members=(("way", 9999, "outer"),), tags={"type": "multipolygon", "building": "yes"}
            )
        )
        _, stats = extract_buildings(osm, projection)
        assert stats.skipped_relations == 1

    def test_stats_break_down_by_class_and_source(self, projection) -> None:
        osm = self._osm(
            projection,
            [
                (rectangle(12.0, 8.0, 0.0, 0.0).tolist(), {"building": "house", "height": "6"}),
                (rectangle(12.0, 8.0, 100.0, 0.0).tolist(), {"building": "house"}),
                (rectangle(30.0, 20.0, 200.0, 0.0).tolist(), {"building": "apartments"}),
            ],
        )
        _, stats = extract_buildings(osm, projection)
        data = stats.as_dict()
        assert data["by_class"]["house"] == 2
        assert data["by_class"]["apartments"] == 1
        assert data["by_height_source"]["height"] == 1
        assert sum(data["by_height_source"].values()) == 3
        assert data["total_area_km2"] > 0


class TestMeshGeneration:
    def test_flat_roof_mesh_is_closed_and_above_ground(self) -> None:
        mesh = extrude_footprint(rectangle(10.0, 8.0), height_m=5.0, roof="flat")
        low, high = mesh.bounds_z()
        assert low == pytest.approx(0.0)
        assert high == pytest.approx(5.0)
        assert mesh.triangle_count > 0

    def test_walls_produce_two_triangles_per_edge(self) -> None:
        # 4 edges -> 8 wall triangles, plus a roof and a ground cap.
        mesh = extrude_footprint(rectangle(10.0, 8.0), height_m=5.0, roof="flat")
        assert mesh.triangle_count >= 8

    def test_gable_roof_rises_above_the_wall_height(self) -> None:
        mesh = extrude_footprint(rectangle(10.0, 8.0), height_m=5.0, roof="gable")
        low, high = mesh.bounds_z()
        assert low == pytest.approx(0.0)
        assert high > 5.0

    def test_flat_roof_does_not_rise(self) -> None:
        mesh = extrude_footprint(rectangle(10.0, 8.0), height_m=5.0, roof="flat")
        assert mesh.bounds_z()[1] == pytest.approx(5.0)

    def test_hip_roof_is_at_most_as_tall_as_a_gable(self) -> None:
        gable = extrude_footprint(rectangle(20.0, 10.0), height_m=5.0, roof="gable")
        hip = extrude_footprint(rectangle(20.0, 10.0), height_m=5.0, roof="hip")
        assert hip.bounds_z()[1] > 5.0
        assert hip.bounds_z()[1] <= gable.bounds_z()[1] + 1e-6

    def test_clockwise_ring_still_produces_outward_wall_normals(self) -> None:
        """OSM ways are often traced clockwise; walls must still face outwards."""
        cw_ring = rectangle(12.0, 8.0)[::-1].copy()
        mesh = extrude_footprint(cw_ring, height_m=5.0, roof="gable")
        wall_tris = [t for t in mesh.triangles if abs(mesh.normals[t[0][1]][2]) < 0.9]
        assert wall_tris
        for tri in wall_tris:
            pts = [mesh.positions[vi] for vi, _ in tri]
            n = mesh.normals[tri[0][1]]
            cx = sum(p[0] for p in pts) / 3.0
            cy = sum(p[1] for p in pts) / 3.0
            assert n[0] * cx + n[1] * cy > 0

    def test_rotated_footprint_normals_match_triangle_geometry(self) -> None:
        """Rotating back from the ridge-aligned frame must rotate normals too."""
        angle = math.radians(37.0)
        c, s = math.cos(angle), math.sin(angle)
        rot = np.array([[c, -s], [s, c]], dtype=np.float64)
        rotated_ring = rectangle(20.0, 8.0) @ rot.T
        mesh = extrude_footprint(rotated_ring, height_m=6.0, roof="gable")
        for tri in mesh.triangles:
            a, b, d = (np.array(mesh.positions[vi]) for vi, _ in tri)
            geom_n = np.cross(b - a, d - a)
            norm = float(np.linalg.norm(geom_n))
            if norm < 1e-9:
                continue
            geom_n = geom_n / norm
            stored_n = np.array(mesh.normals[tri[0][1]])
            assert float(np.dot(geom_n, stored_n)) > 0.999

    def test_concave_l_shaped_footprint_does_not_bridge_concavity(self) -> None:
        """Constrained triangulation must not span triangles across an empty courtyard."""
        l_ring = np.array(
            [[0.0, 0.0], [10.0, 0.0], [10.0, 4.0], [4.0, 4.0], [4.0, 10.0], [0.0, 10.0], [0.0, 0.0]],
            dtype=np.float64,
        )
        mesh = extrude_footprint(l_ring, height_m=5.0, roof="flat")
        top_area = 0.0
        for tri in mesh.triangles:
            if mesh.normals[tri[0][1]][2] > 0.9:
                ax, ay, _ = mesh.positions[tri[0][0]]
                bx, by, _ = mesh.positions[tri[1][0]]
                cx, cy, _ = mesh.positions[tri[2][0]]
                top_area += 0.5 * abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))
        # True L-shape area is 10*4 + 4*6 = 64 m^2 (convex hull would be 82 m^2).
        assert top_area == pytest.approx(64.0, abs=0.05)

    def test_geometry_is_centred_on_the_footprint(self) -> None:
        """A TSStatic places the mesh by position alone, so it must be centred."""
        ring = rectangle(20.0, 10.0, 500.0, -300.0)
        mesh = extrude_footprint(ring, height_m=6.0, roof="flat")
        xs = [p[0] for p in mesh.positions]
        ys = [p[1] for p in mesh.positions]
        assert abs((min(xs) + max(xs)) / 2) < 0.01
        assert abs((min(ys) + max(ys)) / 2) < 0.01

    def test_footprint_extent_is_preserved(self) -> None:
        mesh = extrude_footprint(rectangle(20.0, 10.0), height_m=6.0, roof="flat")
        xs = [p[0] for p in mesh.positions]
        assert (max(xs) - min(xs)) == pytest.approx(20.0, abs=0.01)

    def test_mesh_has_outward_normals(self) -> None:
        """A building whose walls face inwards is invisible from outside."""
        mesh = extrude_footprint(rectangle(10.0, 10.0), height_m=5.0, roof="flat")
        outward = 0
        for triangle in mesh.triangles:
            points = [mesh.positions[vi] for vi, _ in triangle]
            normals = [mesh.normals[ni] for _, ni in triangle]
            # Wall triangles are vertical; their normal should point away from
            # the centre, i.e. have a positive dot with the face-centre offset.
            n = normals[0]
            if abs(n[2]) > 0.9:
                continue
            cx = sum(p[0] for p in points) / 3
            cy = sum(p[1] for p in points) / 3
            if n[0] * cx + n[1] * cy > 0:
                outward += 1
        assert outward > 0, "no wall triangles found"
        wall_triangles = [
            t
            for t in mesh.triangles
            if abs(mesh.normals[t[0][1]][2]) < 0.9
        ]
        assert outward == len(wall_triangles), "some wall normals point inwards"

    def test_rejects_bad_input(self) -> None:
        with pytest.raises(ValueError, match="height_m must be positive"):
            extrude_footprint(rectangle(10.0, 10.0), height_m=0.0)
        with pytest.raises(ValueError, match="closed polygon"):
            extrude_footprint(np.array([[0.0, 0.0], [1.0, 0.0]]), height_m=5.0)

    def test_deterministic(self) -> None:
        first = extrude_footprint(rectangle(10.0, 8.0), height_m=5.0, roof="gable")
        second = extrude_footprint(rectangle(10.0, 8.0), height_m=5.0, roof="gable")
        assert first.positions == second.positions
        assert first.triangles == second.triangles

    def test_roof_height_scales_with_depth_and_is_bounded(self) -> None:
        small = roof_height_for(rectangle(4.0, 4.0), "gable")
        large = roof_height_for(rectangle(60.0, 60.0), "gable")
        assert small >= 0.8
        assert large <= 4.5
        assert large > small

    def test_flat_roof_has_no_roof_height(self) -> None:
        assert roof_height_for(rectangle(10.0, 10.0), "flat") == 0.0

    def test_sliver_triangles_are_not_emitted(self) -> None:
        """A degenerate footprint must not produce zero-area triangles."""
        ring = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 1e-9], [0.0, 1e-9], [0.0, 0.0]])
        try:
            mesh = extrude_footprint(ring, height_m=3.0, roof="flat")
        except ValueError:
            return  # refusing degenerate input outright is also correct
        for triangle in mesh.triangles:
            a, b, c = (mesh.positions[vi] for vi, _ in triangle)
            u = np.array(b) - np.array(a)
            v = np.array(c) - np.array(a)
            assert np.linalg.norm(np.cross(u, v)) > 1e-12


class TestCollada:
    def _mesh(self) -> Mesh:
        return extrude_footprint(rectangle(10.0, 8.0), height_m=5.0, roof="gable")

    def test_output_is_well_formed_xml(self) -> None:
        xml = mesh_to_dae_xml(self._mesh(), name="t", material_name="m", colour=(0.5, 0.5, 0.5))
        root = ET.fromstring(xml)
        assert root.tag == f"{COLLADA_NS}COLLADA"
        assert root.get("version") == "1.4.1"

    def test_declares_metres_and_z_up(self) -> None:
        """BeamNG 0.39 is Z-up; a Y-up export would lay every building on its side."""
        xml = mesh_to_dae_xml(self._mesh(), name="t", material_name="m", colour=(0.5, 0.5, 0.5))
        root = ET.fromstring(xml)
        unit = root.find(f"{COLLADA_NS}asset/{COLLADA_NS}unit")
        up = root.find(f"{COLLADA_NS}asset/{COLLADA_NS}up_axis")
        assert unit is not None and unit.get("meter") == "1"
        assert up is not None and up.text == "Z_UP"

    def test_contains_the_elements_the_engine_reads(self) -> None:
        xml = mesh_to_dae_xml(self._mesh(), name="t", material_name="m", colour=(0.5, 0.5, 0.5))
        root = ET.fromstring(xml)
        assert root.find(f"{COLLADA_NS}library_effects") is not None
        assert root.find(f"{COLLADA_NS}library_materials") is not None
        geometry = root.find(f"{COLLADA_NS}library_geometries/{COLLADA_NS}geometry")
        assert geometry is not None
        assert root.find(f"{COLLADA_NS}library_visual_scenes") is not None
        assert root.find(f"{COLLADA_NS}scene") is not None
        triangles = geometry.find(f"{COLLADA_NS}mesh/{COLLADA_NS}triangles")
        assert triangles is not None and int(triangles.get("count", "0")) > 0

    def test_index_counts_are_consistent(self) -> None:
        """Every index in <p> must address a real vertex and normal."""
        mesh = self._mesh()
        xml = mesh_to_dae_xml(mesh, name="t", material_name="m", colour=(0.5, 0.5, 0.5))
        root = ET.fromstring(xml)
        mesh_el = root.find(f"{COLLADA_NS}library_geometries/{COLLADA_NS}geometry/{COLLADA_NS}mesh")
        assert mesh_el is not None
        positions = mesh_el.find(f"{COLLADA_NS}source[@id='t-mesh-positions']/{COLLADA_NS}float_array")
        normals = mesh_el.find(f"{COLLADA_NS}source[@id='t-mesh-normals']/{COLLADA_NS}float_array")
        assert positions is not None and normals is not None
        n_vertices = int(positions.get("count", "0")) // 3
        n_normals = int(normals.get("count", "0")) // 3
        assert n_vertices == len(mesh.positions)
        assert n_normals == len(mesh.normals)
        p = mesh_el.find(f"{COLLADA_NS}triangles/{COLLADA_NS}p")
        assert p is not None and p.text
        values = [int(v) for v in p.text.split()]
        # 3 indices per corner (vertex, normal, uv) * 3 corners = 9 values per triangle
        assert len(values) == mesh.triangle_count * 9
        assert max(values[0::3]) < n_vertices
        assert max(values[1::3]) < n_normals

    def test_deterministic_bytes(self, tmp_path: Path) -> None:
        mesh = self._mesh()
        first = tmp_path / "a.dae"
        second = tmp_path / "b.dae"
        write_dae(first, mesh, name="t", material_name="m", colour=(0.4, 0.4, 0.4))
        write_dae(second, mesh, name="t", material_name="m", colour=(0.4, 0.4, 0.4))
        assert first.read_bytes() == second.read_bytes()

    def test_refuses_empty_mesh(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="refusing to write an empty mesh"):
            write_dae(tmp_path / "e.dae", Mesh(), name="t", material_name="m", colour=(0, 0, 0))


class TestArchetypes:
    def _buildings(self, count: int = 40) -> list[BuildingFootprint]:
        out = []
        for i in range(count):
            size = 8.0 + (i % 7) * 2.0
            ring = rectangle(size, size * 0.8, i * 30.0, 0.0)
            out.append(
                BuildingFootprint(
                    osm_id=i + 1,
                    ring=ring,
                    klass="house" if i % 3 else "apartments",
                    height_m=6.0 + (i % 4),
                    levels=2.0,
                    name="",
                    area_m2=float(size * size * 0.8),
                    height_source="default",
                )
            )
        return out

    def test_prototypes_cover_each_class(self) -> None:
        prototypes = choose_prototypes(self._buildings(), variants_per_class=3)
        keys = set(prototypes)
        assert any(k.startswith("house_") for k in keys)
        assert any(k.startswith("apartments_") for k in keys)

    def test_prototype_count_is_bounded_by_variants(self) -> None:
        prototypes = choose_prototypes(self._buildings(), variants_per_class=2)
        per_class: dict[str, int] = {}
        for key in prototypes:
            klass = key.rsplit("_", 1)[0]
            per_class[klass] = per_class.get(klass, 0) + 1
        assert all(count <= 2 for count in per_class.values())

    def test_single_building_class_still_yields_a_prototype(self) -> None:
        prototypes = choose_prototypes(self._buildings(1), variants_per_class=4)
        assert len(prototypes) == 1

    def test_no_buildings_yields_no_prototypes(self) -> None:
        assert choose_prototypes([]) == {}

    def test_archetypes_are_written_and_named(self, tmp_path: Path) -> None:
        prototypes = choose_prototypes(self._buildings(), variants_per_class=2)
        archetypes = build_archetypes(
            prototypes,
            level_id="lvl",
            shapes_dir=tmp_path / "shapes",
            asset_prefix="/levels/lvl/art/shapes/buildings",
        )
        assert archetypes
        for key, archetype in archetypes.items():
            assert archetype.dae_path == f"/levels/lvl/art/shapes/buildings/{key}.dae"
            assert archetype.triangle_count > 0
            assert (tmp_path / "shapes" / f"{key}.dae").is_file()
            ET.fromstring((tmp_path / "shapes" / f"{key}.dae").read_text(encoding="utf-8"))


class TestExport:
    def _buildings(self, small: int = 20, large: int = 2) -> list[BuildingFootprint]:
        out = []
        for i in range(small):
            out.append(
                BuildingFootprint(
                    osm_id=100 + i,
                    ring=rectangle(10.0, 8.0, i * 25.0, 0.0),
                    klass="house",
                    height_m=6.0,
                    levels=2.0,
                    name="",
                    area_m2=80.0,
                    height_source="default",
                )
            )
        for i in range(large):
            out.append(
                BuildingFootprint(
                    osm_id=900 + i,
                    ring=rectangle(60.0, 40.0, i * 200.0, 300.0),
                    klass="industrial",
                    height_m=9.0,
                    levels=1.0,
                    name="",
                    area_m2=2400.0,
                    height_source="height",
                )
            )
        return out

    class _FlatBuild:
        """Minimal stand-in for TerrainBuild, enough to seat buildings."""

        def __init__(self) -> None:
            self.elevations = np.full((64, 64), 120.0)
            self.origin_x = -320.0
            self.origin_y = -320.0
            self.cell_size_m = 10.0

    _osm = staticmethod(_make_osm)

    def test_large_buildings_get_their_own_mesh(self, tmp_path: Path) -> None:
        objects, stats = export_buildings(
            self._buildings(), self._FlatBuild(), level_id="lvl", level_dir=tmp_path, batch_chunks=False
        )
        assert stats.unique_meshes == 2
        assert stats.instances == 20
        # Exact meshes are named after the source way and are not scaled.
        exact = [o for o in objects if "/b9" in o["shapeName"]]
        assert len(exact) == 2
        assert all(o["scale"] == [1.0, 1.0, 1.0] for o in exact)

    def test_instances_share_a_small_mesh_library(self, tmp_path: Path) -> None:
        """The whole point: 20 houses must not become 20 distinct meshes."""
        objects, stats = export_buildings(
            self._buildings(), self._FlatBuild(), level_id="lvl", level_dir=tmp_path, batch_chunks=False
        )
        shared = {o["shapeName"] for o in objects if "/b9" not in o["shapeName"]}
        assert 0 < len(shared) <= 4
        assert stats.archetypes == len(shared)
        assert stats.instances == 20
        assert len(list((tmp_path / "art" / "shapes" / "buildings").glob("*.dae"))) == (
            stats.archetypes + stats.unique_meshes
        )

    def test_objects_carry_the_fields_beamng_needs(self, tmp_path: Path) -> None:
        objects, _ = export_buildings(
            self._buildings(), self._FlatBuild(), level_id="lvl", level_dir=tmp_path, batch_chunks=False
        )
        for obj in objects:
            assert obj["class"] == "TSStatic"
            assert obj["shapeName"].startswith("/levels/lvl/")
            assert obj["collisionType"] == "Visible Mesh Final"
            assert obj["useInstanceRenderData"] is True
            assert len(obj["position"]) == 3
            assert len(obj["scale"]) == 3
            assert all(s > 0 for s in obj["scale"]), obj["scale"]
            assert obj["persistentId"]

    def test_buildings_are_seated_on_the_ground(self, tmp_path: Path) -> None:
        objects, _ = export_buildings(
            self._buildings(), self._FlatBuild(), level_id="lvl", level_dir=tmp_path, batch_chunks=False
        )
        assert all(o["position"][2] == pytest.approx(120.0) for o in objects)

    def test_persistent_ids_are_unique_and_stable(self, tmp_path: Path) -> None:
        first, _ = export_buildings(
            self._buildings(), self._FlatBuild(), level_id="lvl", level_dir=tmp_path / "a"
        )
        second, _ = export_buildings(
            self._buildings(), self._FlatBuild(), level_id="lvl", level_dir=tmp_path / "b"
        )
        ids = [o["persistentId"] for o in first]
        assert len(set(ids)) == len(ids)
        assert ids == [o["persistentId"] for o in second]

    def test_max_instances_keeps_the_largest(self, tmp_path: Path) -> None:
        objects, stats = export_buildings(
            self._buildings(),
            self._FlatBuild(),
            level_id="lvl",
            level_dir=tmp_path,
            batch_chunks=False,
            max_instances=5,
        )
        assert stats.buildings == 5
        assert len(objects) == 5

    def test_spatial_chunk_batching(self, tmp_path: Path) -> None:
        """Batch buildings into 500m x 500m sector .dae files."""
        objects, stats = export_buildings(
            self._buildings(), self._FlatBuild(), level_id="lvl", level_dir=tmp_path, batch_chunks=True
        )
        assert stats.chunks > 0
        assert stats.buildings == 22
        assert len(objects) == stats.chunks
        for obj in objects:
            assert obj["class"] == "TSStatic"
            assert obj["collisionType"] == "Visible Mesh Final"
            assert obj["meshCulling"] is True
            assert obj["useInstanceRenderData"] is True
            assert obj["shapeName"].startswith("/levels/lvl/art/shapes/buildings/chunk_")
            assert obj["scale"] == [1.0, 1.0, 1.0]

        # Check .dae files on disk
        for obj in objects:
            rel = obj["shapeName"].replace("/levels/lvl/", "")
            dae_path = tmp_path / rel
            assert dae_path.is_file()
            root = ET.fromstring(dae_path.read_text(encoding="utf-8"))
            assert root.tag.endswith("COLLADA")

        # Check main.materials.json
        mat_path = tmp_path / "art" / "shapes" / "buildings" / "main.materials.json"
        assert mat_path.is_file()
        materials = json.loads(mat_path.read_text(encoding="utf-8"))
        assert "bld_brick_industrial" in materials
        assert "bld_wood_wall" in materials
        assert materials["bld_brick_industrial"]["class"] == "Material"

    def test_slope_skirts_extend_walls_downwards(self) -> None:
        ring = rectangle(10.0, 10.0)
        mesh = extrude_footprint(ring, height_m=5.0, skirt_depth_m=3.0)
        low, high = mesh.bounds_z()
        assert low == pytest.approx(-3.0)
        assert high >= 5.0

    def test_multipolygon_with_courtyard_extracted_and_meshed(self, projection) -> None:
        from ivanovo2beam.osm.types import OsmRelation

        outer_rect = rectangle(30.0, 30.0).tolist()
        inner_rect = rectangle(10.0, 10.0, 10.0, 10.0).tolist()

        osm = self._osm(
            projection,
            [
                (outer_rect, {"building": "apartments"}),
                (inner_rect, {}),
            ],
        )
        osm.relations.append(
            OsmRelation(
                id=101,
                members=(("way", 1, "outer"), ("way", 2, "inner")),
                tags={"type": "multipolygon", "building": "apartments", "building:levels": "9"},
            )
        )
        buildings, stats = extract_buildings(osm, projection)
        assert stats.relations == 1
        b = next(bld for bld in buildings if bld.osm_id == -101)
        assert len(b.holes) == 1
        assert b.area_m2 == pytest.approx(800.0, rel=1e-2)

        mesh = extrude_footprint(b.ring, height_m=b.height_m, holes=b.holes, roof="flat")
        assert mesh.triangle_count > 0
        low, high = mesh.bounds_z()
        assert low == pytest.approx(0.0)
        assert high == pytest.approx(b.height_m)

    def test_building_part_extracted_and_meshed(self, projection) -> None:
        osm = self._osm(
            projection,
            [
                (
                    rectangle(12.0, 8.0).tolist(),
                    {"building:part": "yes", "height": "15", "min_height": "6", "roof:shape": "flat"},
                )
            ],
        )
        buildings, stats = extract_buildings(osm, projection)
        assert stats.parts == 1
        b = buildings[0]
        assert b.part is True
        assert b.min_height_m == pytest.approx(6.0)
        assert b.height_m == pytest.approx(15.0)

        mesh = extrude_footprint(b.ring, height_m=b.height_m, min_height_m=b.min_height_m, roof="flat")
        low, high = mesh.bounds_z()
        assert low == pytest.approx(6.0)
        assert high == pytest.approx(21.0)

    def test_architectural_typology_material_mapping(self) -> None:
        from ivanovo2beam.buildings.export import resolve_building_materials

        ring = rectangle(10.0, 10.0)
        b_ind = BuildingFootprint(1, ring, "industrial", 9.0, 1.0, "BIM Textile Mill", 500.0, "height")
        w_mat, r_mat = resolve_building_materials(b_ind)
        assert w_mat == "bld_brick_industrial"

        b_panel = BuildingFootprint(2, ring, "apartments", 27.0, 9.0, "Brezhnevka", 600.0, "levels")
        w_mat, r_mat = resolve_building_materials(b_panel)
        assert w_mat == "bld_concrete_panel"
        assert r_mat == "bld_roof_tar"

        b_khrush = BuildingFootprint(3, ring, "apartments", 15.0, 5.0, "Khrushchevka", 400.0, "levels")
        w_mat, r_mat = resolve_building_materials(b_khrush)
        assert w_mat == "bld_brick_khrushchevka"

        b_wood = BuildingFootprint(4, ring, "house", 6.0, 1.5, "Izba", 80.0, "default")
        w_mat, r_mat = resolve_building_materials(b_wood)
        assert w_mat == "bld_wood_wall"

    def test_empty_input_is_handled(self, tmp_path: Path) -> None:
        objects, stats = export_buildings(
            [], self._FlatBuild(), level_id="lvl", level_dir=tmp_path
        )
        assert objects == []
        assert stats.buildings == 0

    def test_no_files_written_for_an_empty_build(self, tmp_path: Path) -> None:
        export_buildings([], self._FlatBuild(), level_id="lvl", level_dir=tmp_path)
        assert not (tmp_path / "art").exists()

    def test_unique_threshold_is_documented_and_sane(self) -> None:
        assert 100.0 < UNIQUE_AREA_M2 < 100_000.0
