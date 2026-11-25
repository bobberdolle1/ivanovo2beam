"""Tests for vegetation extraction and BeamNG .forest4 export.

Verifies:
1. OpenStreetMap extraction of wood, scrub, and meadow (both closed ways and
   multipolygon relations with inner clearings);
2. Fast 2D keep-out mask rasterization against roads, rail, water, and buildings;
3. Deterministic tree placement with PCG64(osm_id);
4. Central Russian species mix referencing stock BeamNG meshes by path;
5. Valid managedItemData.json and art/forest/main.materials.json;
6. Scene integration (theForest and ForestWindEmitter objects).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

from ivanovo2beam.buildings.footprints import BuildingFootprint
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.osm.types import OsmData, OsmRelation, OsmWay
from ivanovo2beam.vegetation.extract import (
    VegetationArea,
    extract_vegetation,
)
from ivanovo2beam.vegetation.forest import (
    SPECIES_DATA,
    build_forest_materials_json,
    build_keepout_mask,
    build_managed_item_data_json,
    export_vegetation,
)

BBOX = BBox(west=39.0, south=56.1, east=43.2, north=57.8)


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


class _MockBuild:
    """Mock terrain build for ground elevation sampling."""

    def __init__(self, size: int = 512, elevation: float = 125.0) -> None:
        self.elevations = np.full((size, size), elevation, dtype=np.float32)
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.cell_size_m = 2.0


class TestVegetationExtraction:
    def test_extract_wood_and_forest(self, projection) -> None:
        shapes = [
            (rectangle(200.0, 150.0, 0.0, 0.0).tolist(), {"natural": "wood", "name": "Beryozovaya Roshcha"}),
            (rectangle(300.0, 200.0, 500.0, 0.0).tolist(), {"landuse": "forest"}),
        ]
        osm = _make_osm(projection, shapes)
        areas, stats = extract_vegetation(osm, projection)

        assert len(areas) == 2
        assert stats.forest_areas == 2
        assert all(a.kind == "forest" for a in areas)
        assert areas[0].name == "Beryozovaya Roshcha"
        assert areas[0].area_m2 == pytest.approx(30000.0, rel=1e-2)

    def test_extract_scrub_and_meadow(self, projection) -> None:
        shapes = [
            (rectangle(80.0, 60.0, 0.0, 0.0).tolist(), {"natural": "scrub"}),
            (rectangle(120.0, 100.0, 200.0, 0.0).tolist(), {"landuse": "meadow"}),
            (rectangle(100.0, 80.0, 400.0, 0.0).tolist(), {"landuse": "grass"}),
        ]
        osm = _make_osm(projection, shapes)
        areas, stats = extract_vegetation(osm, projection)

        assert len(areas) == 3
        assert stats.scrub_areas == 1
        assert stats.meadow_areas == 2

    def test_multipolygon_with_inner_clearing(self, projection) -> None:
        outer = rectangle(400.0, 300.0, 0.0, 0.0).tolist()
        inner = rectangle(100.0, 100.0, 150.0, 100.0).tolist()

        osm = _make_osm(projection, [(outer, {}), (inner, {})])
        osm.relations.append(
            OsmRelation(
                id=55,
                members=(("way", 1, "outer"), ("way", 2, "inner")),
                tags={"type": "multipolygon", "natural": "wood"},
            )
        )
        areas, stats = extract_vegetation(osm, projection)

        assert stats.relations == 1
        assert len(areas) == 1
        a = areas[0]
        assert a.kind == "forest"
        assert len(a.holes) == 1
        # Net area: 400*300 - 100*100 = 120,000 - 10,000 = 110,000 m2
        assert a.area_m2 == pytest.approx(110000.0, rel=1e-2)

    def test_tiny_areas_are_skipped(self, projection) -> None:
        shapes = [(rectangle(5.0, 5.0).tolist(), {"natural": "wood"})]  # 25 m2 < 100 m2
        osm = _make_osm(projection, shapes)
        areas, stats = extract_vegetation(osm, projection, min_area_m2=100.0)

        assert len(areas) == 0
        assert stats.skipped_area == 1

    def test_clip_bounds_filter(self, projection) -> None:
        shapes = [
            (rectangle(100.0, 100.0, 50.0, 50.0).tolist(), {"natural": "wood"}),
            (rectangle(100.0, 100.0, 5000.0, 5000.0).tolist(), {"natural": "wood"}),
        ]
        osm = _make_osm(projection, shapes)
        areas, stats = extract_vegetation(
            osm, projection, clip_bounds=(0.0, 0.0, 500.0, 500.0)
        )

        assert len(areas) == 1
        assert stats.skipped_invalid == 0

    def test_clip_bounds_clips_straddling_polygon(self, projection) -> None:
        # Large forest from x=400..1200 (centroid x=800 is outside clip_bounds [0..500])
        shapes = [
            (rectangle(800.0, 200.0, 400.0, 50.0).tolist(), {"natural": "wood"}),
        ]
        osm = _make_osm(projection, shapes)
        areas, stats = extract_vegetation(
            osm, projection, clip_bounds=(0.0, 0.0, 500.0, 500.0)
        )

        assert len(areas) == 1
        assert stats.skipped_invalid == 0
        # Clipped overlap is [400..500] x [50..250] = 20,000 m2
        assert areas[0].area_m2 == pytest.approx(20000.0, rel=1e-2)
        assert areas[0].bounds[2] <= 500.0 + 1e-6

    def test_multipolygon_inner_clearing_tagged_meadow_extracted(self, projection) -> None:
        outer = rectangle(400.0, 300.0, 0.0, 0.0).tolist()
        inner = rectangle(100.0, 100.0, 150.0, 100.0).tolist()

        osm = _make_osm(projection, [(outer, {}), (inner, {"landuse": "meadow"})])
        osm.relations.append(
            OsmRelation(
                id=56,
                members=(("way", 1, "outer"), ("way", 2, "inner")),
                tags={"type": "multipolygon", "natural": "wood"},
            )
        )
        areas, stats = extract_vegetation(osm, projection)

        assert stats.forest_areas == 1
        assert stats.meadow_areas == 1
        forest = next(a for a in areas if a.kind == "forest")
        assert len(forest.holes) == 1
        # Hole ring must be clockwise (negative signed area)
        h = forest.holes[0]
        signed_area = float(0.5 * np.sum(h[:-1, 0] * h[1:, 1] - h[1:, 0] * h[:-1, 1]))
        assert signed_area < 0

    def test_multipolygon_nested_island_does_not_steal_clearing(self, projection) -> None:
        outer_big = rectangle(500.0, 500.0, 0.0, 0.0).tolist()
        inner_clearing = rectangle(300.0, 300.0, 100.0, 100.0).tolist()
        outer_island = rectangle(100.0, 100.0, 200.0, 200.0).tolist()

        osm = _make_osm(
            projection, [(outer_big, {}), (inner_clearing, {}), (outer_island, {})]
        )
        osm.relations.append(
            OsmRelation(
                id=57,
                members=(
                    ("way", 1, "outer"),
                    ("way", 2, "inner"),
                    ("way", 3, "outer"),
                ),
                tags={"type": "multipolygon", "natural": "wood"},
            )
        )
        areas, _ = extract_vegetation(osm, projection)

        assert len(areas) == 2
        total = sum(a.area_m2 for a in areas)
        # 500*500 - 300*300 + 100*100 = 170,000 m2
        assert total == pytest.approx(170000.0, rel=1e-2)

    def test_linear_tree_row_extracted(self, projection) -> None:
        # Unclosed linear way with natural=tree_row
        pts = [(0.0, 50.0), (100.0, 50.0)]
        osm = _make_osm(projection, [(pts, {"natural": "tree_row"})])
        areas, stats = extract_vegetation(osm, projection)

        assert len(areas) == 1
        assert stats.scrub_areas == 1
        assert areas[0].kind == "scrub"
        assert areas[0].area_m2 >= 500.0


class TestKeepoutMask:
    @dataclass
    class _MockRoad:
        points: list[tuple[float, float]] = field(default_factory=list)
        width_m: float = 8.0

    def test_keepout_mask_burns_roads_and_buildings(self) -> None:
        build = _MockBuild(size=256, elevation=100.0)

        # Mock road across y = 100m
        road = self._MockRoad(points=[(0.0, 100.0), (500.0, 100.0)], width_m=8.0)

        # Mock building at (200, 200) to (250, 250)
        ring = rectangle(50.0, 50.0, 200.0, 200.0)
        bld = BuildingFootprint(
            osm_id=1,
            ring=ring,
            klass="house",
            height_m=6.0,
            levels=2.0,
            name="",
            area_m2=2500.0,
            height_source="default",
        )

        mask, ox, oy, cell = build_keepout_mask(
            build, roads=[road], buildings=[bld]
        )

        # Point on the road should be masked (True)
        road_col = round((100.0 - ox) / cell)
        road_row = round((100.0 - oy) / cell)
        assert bool(mask[road_row, road_col]) is True

        # Point inside building and 1.5m outside wall (2m buffer) should be masked (True)
        bld_col = round((225.0 - ox) / cell)
        bld_row = round((225.0 - oy) / cell)
        assert bool(mask[bld_row, bld_col]) is True
        eave_col = round((199.0 - ox) / cell)
        eave_row = round((225.0 - oy) / cell)
        assert bool(mask[eave_row, eave_col]) is True

        # Point in open meadow should NOT be masked (False)
        open_col = round((350.0 - ox) / cell)
        open_row = round((350.0 - oy) / cell)
        assert bool(mask[open_row, open_col]) is False

    def test_keepout_mask_handles_none_build_attrs(self) -> None:
        class _NullAttrBuild:
            elevations = np.zeros((128, 128), dtype=np.float32)
            origin_x = None
            origin_y = None
            cell_size_m = None

        mask, ox, oy, cell = build_keepout_mask(_NullAttrBuild())
        assert mask.shape == (128, 128)
        assert ox == 0.0
        assert oy == 0.0
        assert cell == 1.0

    @pytest.mark.parametrize("use_rasterio", [True, False])
    def test_keepout_mask_pure_numpy_fallback(
        self, monkeypatch: pytest.MonkeyPatch, use_rasterio: bool
    ) -> None:
        import ivanovo2beam.vegetation.forest as forest_mod

        monkeypatch.setattr(forest_mod, "_HAS_PIL", False)
        if not use_rasterio:
            monkeypatch.setattr(forest_mod, "_HAS_RASTERIO", False)
        build = _MockBuild(size=256, elevation=100.0)

        road = self._MockRoad(points=[(0.0, 100.0), (500.0, 100.0)], width_m=8.0)
        ring = rectangle(50.0, 50.0, 200.0, 200.0)
        bld = BuildingFootprint(
            osm_id=1,
            ring=ring,
            klass="house",
            height_m=6.0,
            levels=2.0,
            name="",
            area_m2=2500.0,
            height_source="default",
        )

        mask, ox, oy, cell = build_keepout_mask(
            build, roads=[road], buildings=[bld]
        )

        road_col = round((100.0 - ox) / cell)
        road_row = round((100.0 - oy) / cell)
        assert bool(mask[road_row, road_col]) is True

        bld_col = round((225.0 - ox) / cell)
        bld_row = round((225.0 - oy) / cell)
        assert bool(mask[bld_row, bld_col]) is True

        open_col = round((350.0 - ox) / cell)
        open_row = round((350.0 - oy) / cell)
        assert bool(mask[open_row, open_col]) is False


class TestForestExport:
    @dataclass
    class _MockRoad:
        points: list[tuple[float, float]] = field(default_factory=list)
        width_m: float = 7.0
        highway: str = ""

    def _vegetation_areas(self) -> list[VegetationArea]:
        return [
            VegetationArea(
                osm_id=1001,
                ring=rectangle(250.0, 200.0, 50.0, 50.0),
                kind="forest",
                area_m2=50000.0,
                name="Sosnovy Bor",
            ),
            VegetationArea(
                osm_id=1002,
                ring=rectangle(150.0, 100.0, 350.0, 50.0),
                kind="scrub",
                area_m2=15000.0,
            ),
        ]

    def test_deterministic_placement(self, tmp_path: Path) -> None:
        build = _MockBuild(size=512)
        areas = self._vegetation_areas()

        _, stats1 = export_vegetation(
            areas, build, level_id="lvl", level_dir=tmp_path / "run1"
        )
        _, stats2 = export_vegetation(
            areas, build, level_id="lvl", level_dir=tmp_path / "run2"
        )

        assert stats1.total_trees == stats2.total_trees
        assert stats1.by_species == stats2.by_species

        # Check identical content in generated .forest4.json files
        for sp_key in stats1.by_species:
            f1 = tmp_path / "run1" / "forest" / f"{sp_key}.forest4.json"
            f2 = tmp_path / "run2" / "forest" / f"{sp_key}.forest4.json"
            assert f1.read_bytes() == f2.read_bytes()

    def test_keepout_prevents_trees_on_roads(self, tmp_path: Path) -> None:
        build = _MockBuild(size=512)
        areas = [
            VegetationArea(
                osm_id=2001,
                ring=rectangle(200.0, 200.0, 50.0, 50.0),
                kind="forest",
                area_m2=40000.0,
            )
        ]
        # Road passing directly through the middle of the forest along y = 150
        road = self._MockRoad(points=[(0.0, 150.0), (300.0, 150.0)], width_m=8.0)

        export_vegetation(
            areas, build, level_id="lvl", level_dir=tmp_path, roads=[road]
        )

        forest_dir = tmp_path / "forest"
        for json_file in forest_dir.glob("*.forest4.json"):
            for line in json_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                pos = item["pos"]
                # Road corridor is along y = 150; trees must maintain clearance
                # Clearance: width (8) + shoulders (6) = 14m -> +/- 7m minimum
                dist_to_road = abs(pos[1] - 150.0)
                assert dist_to_road >= 4.0

    def test_species_mix_includes_central_russian_flora(self, tmp_path: Path) -> None:
        build = _MockBuild(size=512)
        areas = [
            VegetationArea(
                osm_id=3001,
                ring=rectangle(400.0, 400.0, 50.0, 50.0),
                kind="forest",
                area_m2=160000.0,
            )
        ]
        _, stats = export_vegetation(
            areas, build, level_id="lvl", level_dir=tmp_path, target_density_trees_per_ha=150.0
        )

        assert stats.total_trees > 50
        species = stats.by_species

        # Check Birch (aspen / birch meshes)
        birch_count = sum(species.get(k, 0) for k in species if "aspen" in k)
        assert birch_count > 0

        # Check Beech
        beech_count = sum(species.get(k, 0) for k in species if "beech" in k)
        assert beech_count > 0

    def test_corridor_proximity_prioritizes_roadside_forest(self, tmp_path: Path) -> None:
        build = _MockBuild(size=1024)  # 2048m extent at cell_size_m=2.0
        road = self._MockRoad(points=[(0.0, 100.0), (500.0, 100.0)], width_m=8.0)
        areas = [
            # Huge far-off forest (> 1km away from road)
            VegetationArea(
                osm_id=3101,
                ring=rectangle(600.0, 600.0, 1200.0, 1200.0),
                kind="forest",
                area_m2=360000.0,
            ),
            # Smaller roadside forest (borders y=100 road corridor)
            VegetationArea(
                osm_id=3102,
                ring=rectangle(200.0, 200.0, 100.0, 100.0),
                kind="forest",
                area_m2=40000.0,
            ),
        ]
        _, stats = export_vegetation(
            areas, build, level_id="lvl", level_dir=tmp_path, roads=[road], max_trees=300
        )
        assert stats.total_trees == 300
        for json_file in (tmp_path / "forest").glob("*.forest4.json"):
            for line in json_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                pos = json.loads(line)["pos"]
                assert 100.0 <= pos[0] <= 300.0
                assert 100.0 <= pos[1] <= 300.0

    def test_stale_forest4_files_removed(self, tmp_path: Path) -> None:
        forest_dir = tmp_path / "forest"
        forest_dir.mkdir(parents=True, exist_ok=True)
        stale_file = forest_dir / "scots_pine.forest4.json"
        stale_file.write_text('{"type":"scots_pine"}\n', encoding="utf-8")

        build = _MockBuild(size=512)
        areas = self._vegetation_areas()
        export_vegetation(areas, build, level_id="lvl", level_dir=tmp_path)

        assert not stale_file.exists()

    def test_priority_highways_ranked_ahead_of_tracks(self, tmp_path: Path) -> None:
        build = _MockBuild(size=1024)  # 2048m extent
        track = self._MockRoad(
            points=[(1100.0, 1200.0), (1900.0, 1200.0)], width_m=4.0, highway="track"
        )
        trunk = self._MockRoad(
            points=[(0.0, 100.0), (500.0, 100.0)], width_m=10.0, highway="trunk"
        )
        areas = [
            VegetationArea(
                osm_id=3201,
                ring=rectangle(600.0, 600.0, 1200.0, 1200.0),
                kind="forest",
                area_m2=360000.0,
            ),
            VegetationArea(
                osm_id=3202,
                ring=rectangle(200.0, 200.0, 100.0, 100.0),
                kind="forest",
                area_m2=40000.0,
            ),
        ]
        _, stats = export_vegetation(
            areas,
            build,
            level_id="lvl",
            level_dir=tmp_path,
            roads=[track, trunk],
            max_trees=250,
        )
        assert stats.total_trees == 250
        for json_file in (tmp_path / "forest").glob("*.forest4.json"):
            for line in json_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                pos = json.loads(line)["pos"]
                assert 100.0 <= pos[0] <= 300.0
                assert 100.0 <= pos[1] <= 300.0

    def test_two_pass_allocation_avoids_mega_forest_starvation(self, tmp_path: Path) -> None:
        build = _MockBuild(size=1024)
        highway = self._MockRoad(
            points=[(0.0, 100.0), (1800.0, 100.0)], width_m=10.0, highway="primary"
        )
        areas = [
            # Mega forest along highway (> 4000 target trees)
            VegetationArea(
                osm_id=3301,
                ring=rectangle(600.0, 600.0, 100.0, 100.0),
                kind="forest",
                area_m2=360000.0,
            ),
            # Smaller roadside forest further down the same highway
            VegetationArea(
                osm_id=3302,
                ring=rectangle(200.0, 200.0, 1000.0, 100.0),
                kind="forest",
                area_m2=40000.0,
            ),
        ]
        _, stats = export_vegetation(
            areas,
            build,
            level_id="lvl",
            level_dir=tmp_path,
            roads=[highway],
            max_trees=500,
        )
        assert stats.total_trees == 500
        mega_count = 0
        small_count = 0
        for json_file in (tmp_path / "forest").glob("*.forest4.json"):
            for line in json_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                pos = json.loads(line)["pos"]
                if pos[0] < 800.0:
                    mega_count += 1
                else:
                    small_count += 1
        assert mega_count == 300
        assert small_count == 200

    def test_forest_prioritized_over_meadow_in_budget(self, tmp_path: Path) -> None:
        build = _MockBuild(size=512)
        areas = [
            # Huge meadow listed first
            VegetationArea(
                osm_id=4001,
                ring=rectangle(500.0, 500.0, 50.0, 50.0),
                kind="meadow",
                area_m2=250000.0,
            ),
            # Smaller forest listed second
            VegetationArea(
                osm_id=4002,
                ring=rectangle(200.0, 200.0, 600.0, 50.0),
                kind="forest",
                area_m2=40000.0,
            ),
        ]
        _, stats = export_vegetation(
            areas, build, level_id="lvl", level_dir=tmp_path, max_trees=400
        )
        assert stats.total_trees == 400
        # All placed instances must be inside the forest [600..800] x [50..250]
        forest_dir = tmp_path / "forest"
        for json_file in forest_dir.glob("*.forest4.json"):
            for line in json_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                pos = json.loads(line)["pos"]
                assert 600.0 <= pos[0] <= 800.0
                assert 50.0 <= pos[1] <= 250.0

    def test_out_of_bounds_candidates_not_clamped(self, tmp_path: Path) -> None:
        # Terrain covers [0..256) with cell_size_m=2.0 -> 512m; polygon extends to x=800
        build = _MockBuild(size=256)
        areas = [
            VegetationArea(
                osm_id=5001,
                ring=rectangle(600.0, 200.0, 200.0, 50.0),
                kind="forest",
                area_m2=120000.0,
            )
        ]
        export_vegetation(areas, build, level_id="lvl", level_dir=tmp_path, max_trees=200)
        forest_dir = tmp_path / "forest"
        for json_file in forest_dir.glob("*.forest4.json"):
            for line in json_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                pos = json.loads(line)["pos"]
                assert 0.0 <= pos[0] < 512.0
                assert 0.0 <= pos[1] < 512.0

    def test_minimum_tree_spacing_enforced(self, tmp_path: Path) -> None:
        build = _MockBuild(size=512)
        areas = [
            VegetationArea(
                osm_id=6001,
                ring=rectangle(100.0, 100.0, 50.0, 50.0),
                kind="forest",
                area_m2=10000.0,
            )
        ]
        export_vegetation(
            areas, build, level_id="lvl", level_dir=tmp_path, target_density_trees_per_ha=200.0
        )
        pts: list[tuple[float, float]] = []
        for json_file in (tmp_path / "forest").glob("*.forest4.json"):
            for line in json_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                p = json.loads(line)["pos"]
                pts.append((p[0], p[1]))
        assert len(pts) > 50
        for i, (x1, y1) in enumerate(pts):
            for x2, y2 in pts[i + 1 :]:
                assert (x1 - x2) ** 2 + (y1 - y2) ** 2 >= 2.5**2 - 1e-6

    def test_managed_item_data_json_structure_and_no_collisions(self, tmp_path: Path) -> None:
        build = _MockBuild(size=512)
        areas = self._vegetation_areas()
        export_vegetation(areas, build, level_id="lvl", level_dir=tmp_path)

        mid_path = tmp_path / "art" / "forest" / "managedItemData.json"
        assert mid_path.is_file()
        mid_data = json.loads(mid_path.read_text(encoding="utf-8"))

        mat_data = build_forest_materials_json()
        assert set(mid_data).isdisjoint(set(mat_data))
        assert len(SPECIES_DATA) == 7
        assert set(build_managed_item_data_json()) == set(SPECIES_DATA)

        for key in SPECIES_DATA:
            assert key in mid_data
            item = mid_data[key]
            assert item["class"] in {"TSForestItemData", "ForestItemData"}
            assert item["shapeFile"].startswith("/assets/")
            assert not item["shapeFile"].startswith("/levels/")
            assert item["lodReflectScalar"] == 0.0
            assert item["persistentId"]
            if item["class"] == "TSForestItemData":
                assert item["radius"] > 0
                assert item["mass"] > 0

    def test_forest_materials_json_structure(self, tmp_path: Path) -> None:
        build = _MockBuild(size=512)
        areas = self._vegetation_areas()
        export_vegetation(areas, build, level_id="lvl", level_dir=tmp_path)

        mat_path = tmp_path / "art" / "forest" / "main.materials.json"
        assert mat_path.is_file()
        mat_data = json.loads(mat_path.read_text(encoding="utf-8"))

        # Verify all 10 materials (.dae and compiled .cdae names) for the 7 lean /assets/ meshes
        expected_mats = [
            "Birch_Bark_01",
            "Birch_Branch_01",
            "m_ind_birch_leaves_01",
            "beech_trunk",
            "beech_branch_01",
            "beech_leaves",
            "beech_blocker",
            "m_ind_beech_leaves",
            "m_ind_beech_blocker",
            "leaves_strong",
        ]
        assert len(mat_data) == len(expected_mats)
        for mat_key in expected_mats:
            assert mat_key in mat_data
            m = mat_data[mat_key]
            assert m["class"] == "Material"
            assert m["mapTo"] == mat_key
            assert m["persistentId"]
            assert len(m["Stages"]) == 4

        assert mat_data["m_ind_beech_leaves"]["Stages"] == mat_data["beech_leaves"]["Stages"]
        assert mat_data["m_ind_beech_blocker"]["Stages"] == mat_data["beech_blocker"]["Stages"]

        # Every alphaTest material must define opacityMap and no cross-level /levels/ paths
        for m in mat_data.values():
            stage0 = m["Stages"][0]
            if m.get("alphaTest"):
                assert stage0.get("opacityMap"), f"{m['name']} missing opacityMap"
            for tex_val in stage0.values():
                if isinstance(tex_val, str):
                    assert tex_val.startswith("/assets/"), f"Unexpected path {tex_val}"

        branch = mat_data["Birch_Branch_01"]
        assert branch.get("alphaTest") is True
        assert branch.get("doubleSided") is True

    def test_scene_objects_emitted(self, tmp_path: Path) -> None:
        build = _MockBuild(size=512)
        areas = self._vegetation_areas()
        scene_objs, _ = export_vegetation(areas, build, level_id="lvl", level_dir=tmp_path)

        assert len(scene_objs) == 2
        classes = {o["class"] for o in scene_objs}
        assert classes == {"Forest", "ForestWindEmitter"}

        forest_obj = next(o for o in scene_objs if o["class"] == "Forest")
        assert forest_obj["name"] == "theForest"
        assert forest_obj["__parent"] == "vegetation"
        assert forest_obj["lodReflectScalar"] == 0.0

        wind_obj = next(o for o in scene_objs if o["class"] == "ForestWindEmitter")
        assert wind_obj["name"] == "windEmitter"
        assert wind_obj["__parent"] == "vegetation"
        assert wind_obj["strength"] > 0
