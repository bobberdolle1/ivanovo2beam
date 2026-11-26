"""Offline end-to-end build test.

The tests elsewhere check parts. This one runs the whole pipeline -- DEM
sampling, road projection, corridor grading, scene assembly, file writing and
mod packaging -- against a synthetic DEM and a hand-built OSM extract, so CI can
prove the pipeline produces a structurally valid level without downloading
anything or depending on the availability of the Overpass API.

The synthetic data is deliberately geographic: it uses real Ivanovo coordinates
and the real projection, so a georeferencing mistake still fails the test.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from ivanovo2beam.beamng.ter import read_ter
from ivanovo2beam.build import build_level
from ivanovo2beam.config import ProjectConfig
from ivanovo2beam.osm.types import OsmData, OsmWay
from ivanovo2beam.terrain import _geotiff
from ivanovo2beam.validation.level import validate_level

try:
    import rasterio
    from rasterio.transform import from_bounds

    _open_raster = rasterio.open
except ImportError:  # pragma: no cover - optional rasterio / sandbox fallback
    rasterio = None  # type: ignore[assignment]
    from_bounds = _geotiff.fallback_from_bounds
    _open_raster = _geotiff.open_geotiff

#: Elevation of Ivanovo, in metres. The city sits around 120 m ASL.
IVANOVO_ELEVATION_M = 120.0


def write_synthetic_dem(path: Path, bbox: tuple[float, float, float, float]) -> Path:
    """Write a small GeoTIFF whose elevation varies gently with latitude.

    Args:
        path: Destination for the GeoTIFF.
        bbox: ``(west, south, east, north)`` in degrees.

    Returns:
        The path written.
    """
    west, south, east, north = bbox
    width = height = 64
    # ~40 m of relief across the tile: enough that grading has something to do.
    lats = np.linspace(south, north, height, dtype=np.float64).reshape(height, 1)
    data = IVANOVO_ELEVATION_M + (lats - south) * 500.0
    data = np.broadcast_to(data, (height, width)).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _open_raster(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_bounds(west, south, east, north, width, height),
        nodata=-9999.0,
    ) as dst:
        dst.write(data, 1)
    return path


def synthetic_osm(projection, extent: float) -> OsmData:
    """Build a small but realistic street grid in world metres.

    Args:
        projection: World plane transform.
        extent: Terrain extent in metres; roads are laid inside it.

    Returns:
        OSM data with a cross of two arterials and a few side streets.
    """
    half = extent / 2.0
    step = extent / 8.0
    shapes: list[tuple[list[tuple[float, float]], dict[str, str]]] = [
        # A primary running east-west through the middle.
        ([(-half + step * i, 0.0) for i in range(9)], {"highway": "primary", "lanes": "2"}),
        # A secondary running north-south.
        ([(0.0, -half + step * i) for i in range(9)], {"highway": "secondary", "lanes": "2"}),
    ]
    for i in (2, 6):
        offset = -half + step * i
        shapes.append(
            (
                [(offset, -half + step * j) for j in range(9)],
                {"highway": "residential", "lanes": "2"},
            )
        )
        shapes.append(
            (
                [(-half + step * j, offset) for j in range(9)],
                {"highway": "residential", "lanes": "2"},
            )
        )
    # A bridge and a track, so those code paths are exercised too.
    shapes.append(([(half - step, -half + step) for _ in range(2)], {"highway": "primary", "bridge": "yes"}))
    shapes.append(
        (
            [(-half + step, half - step / 2), (half - step, half - step / 2)],
            {"highway": "track", "surface": "gravel"},
        )
    )

    nodes: dict[int, tuple[float, float]] = {}
    ways: list[OsmWay] = []
    next_id = 1
    for osm_id, (coords, tags) in enumerate(shapes, start=1):
        refs = []
        for x, y in coords:
            lon, lat = projection.world_to_geo(x, y)
            nodes[next_id] = (lon, lat)
            refs.append(next_id)
            next_id += 1
        ways.append(OsmWay(id=osm_id, nodes=tuple(refs), tags=dict(tags)))
    return OsmData(nodes=nodes, node_tags={}, ways=ways, relations=[], source="synthetic")


@pytest.fixture
def offline_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run a full build with network stages replaced by synthetic data."""
    import ivanovo2beam.build as build_module

    config = ProjectConfig.load("configs/test_sector.yaml")
    assert config.level is not None
    # Redirect caches and output into the test's own directory.
    object.__setattr__(config, "cache_dir", tmp_path / "cache")
    object.__setattr__(config, "build_dir", tmp_path / "build")

    _bbox, _, extent = build_module._level_geographic_bbox(config, margin_deg=0.0)

    def fake_fetch_dem(requested_bbox, *args, **kwargs) -> Path:
        """Stand in for the real fetcher, covering exactly the requested area."""
        return write_synthetic_dem(tmp_path / "cache" / "dem" / "synthetic.tif", requested_bbox.as_tuple())

    def fake_fetch_osm(cfg, box, **kwargs):
        return synthetic_osm(cfg.projection, extent), {
            "tiles_requested": 1,
            "tiles_succeeded": 1,
            "tiles_failed": 0,
            "tiles_skipped": 0,
            "budget_exhausted": False,
            "merge_summary": "synthetic",
        }

    monkeypatch.setattr(build_module, "fetch_level_osm", fake_fetch_osm)
    monkeypatch.setattr("ivanovo2beam.terrain.dem.fetch_dem", fake_fetch_dem)

    result = build_level(config, progress=None, make_zip=True)
    return config, result


class TestOfflineEndToEnd:
    def test_build_succeeds_and_reports_stats(self, offline_build) -> None:
        _, result = offline_build
        assert result.level_id == "ivanovo_test_sector"
        assert result.mod_zip is not None and result.mod_zip.is_file()
        assert result.stats["roads"]["roads"] > 0
        assert result.stats["export"]["exported"] > 0

    def test_level_directory_contains_the_required_files(self, offline_build) -> None:
        config, result = offline_build
        level_dir = result.stats.get("level_dir") or (config.build_dir / "levels" / config.level.id)
        for relative in (
            "info.json",
            "main/items.level.json",
            "main/MissionGroup/items.level.json",
            "art/terrains/main.materials.json",
            "art/terrains/t_terrain_base_grass_b.png",
            "art/terrains/t_terrain_base_nm.png",
            "theTerrain.ter",
            "theTerrain.terrain.json",
            "map.json",
        ):
            assert (level_dir / relative).is_file(), relative

    def test_validation_passes(self, offline_build) -> None:
        config, _result = offline_build
        level_dir = config.build_dir / "levels" / config.level.id
        report = validate_level(level_dir, config)
        assert report.ok, report.describe()

    def test_terrain_is_georeferenced_to_the_level(self, offline_build) -> None:
        config, _ = offline_build
        level_dir = config.build_dir / "levels" / config.level.id
        terrain = read_ter(level_dir / "theTerrain.ter")
        assert terrain.size == config.level.grid.grid_size

        scene = level_dir / "main" / "MissionGroup" / "level_objects" / "terrain" / "items.level.json"
        block = None
        for line in scene.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            if obj.get("class") == "TerrainBlock":
                block = obj
        assert block is not None
        expect_x, expect_y = config.level_terrain_origin()
        assert block["position"][0] == pytest.approx(expect_x, abs=1e-3)
        assert block["position"][1] == pytest.approx(expect_y, abs=1e-3)
        assert block["squareSize"] == pytest.approx(config.level.grid.square_size_m)

    def test_scene_tree_parents_all_resolve(self, offline_build) -> None:
        config, _ = offline_build
        level_dir = config.build_dir / "levels" / config.level.id
        names: set[str] = set()
        objects = []
        for path in (level_dir / "main").rglob("items.level.json"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                if obj.get("class") == "SimGroup":
                    names.add(obj["name"])
                objects.append(obj)
        assert objects
        for obj in objects:
            parent = obj.get("__parent")
            if parent:
                assert parent in names, f"{obj.get('class')} has dangling parent {parent!r}"

    def test_spawn_point_exists_and_sits_on_the_terrain(self, offline_build) -> None:
        config, _ = offline_build
        level_dir = config.build_dir / "levels" / config.level.id
        read_ter(level_dir / "theTerrain.ter")
        spawn_dir = level_dir / "main" / "MissionGroup" / "PlayerDropPoints" / "items.level.json"
        spawns = [json.loads(line) for line in spawn_dir.read_text().splitlines() if line.strip()]
        assert spawns, "a level with no spawn point cannot be entered"
        spawn = spawns[0]
        assert spawn["class"] == "SpawnSphere"

    def test_roads_were_graded_into_the_terrain(self, offline_build) -> None:
        _config, result = offline_build
        stats = result.stats["terrain"]
        assert stats["graded_cells"] > 0
        assert stats["cells_touched"] > 0

    def test_materials_json_is_written_and_self_contained(self, offline_build) -> None:
        config, _ = offline_build
        level_dir = config.build_dir / "levels" / config.level.id
        doc = json.loads((level_dir / "art" / "terrains" / "main.materials.json").read_text())
        materials = {k: v for k, v in doc.items() if v.get("class") == "TerrainMaterial"}
        assert materials
        # No BeamNG asset is copied into the archive; everything is referenced.
        assert not any(p.suffix in {".dds", ".cdae"} for p in level_dir.rglob("*"))

    def test_mod_zip_layout_is_correct(self, offline_build) -> None:
        config, result = offline_build
        assert result.mod_zip is not None
        with zipfile.ZipFile(result.mod_zip) as archive:
            names = archive.namelist()
        assert names
        assert all(n.startswith(f"levels/{config.level.id}/") for n in names), names[:5]
        assert f"levels/{config.level.id}/info.json" in names
        assert f"levels/{config.level.id}/theTerrain.ter" in names

    def test_zip_contains_no_game_assets(self, offline_build) -> None:
        """The archive must reference base-game assets, never embed them."""
        _, result = offline_build
        assert result.mod_zip is not None
        with zipfile.ZipFile(result.mod_zip) as archive:
            suffixes = {Path(n).suffix.lower() for n in archive.namelist()}
        assert not any(s in {".dds", ".cdae"} for s in suffixes)
        assert suffixes <= {".json", ".ter", ".txt", ".md", ".png", ".dae", ".link"}, suffixes

    def test_build_is_deterministic(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same inputs must give byte-identical terrain and scene files."""
        import ivanovo2beam.build as build_module

        def run(tag: str):
            config = ProjectConfig.load("configs/test_sector.yaml")
            object.__setattr__(config, "cache_dir", tmp_path / tag / "cache")
            object.__setattr__(config, "build_dir", tmp_path / tag / "build")
            _, _, extent = build_module._level_geographic_bbox(config, margin_deg=0.0)

            def fake_fetch_dem(requested_bbox, *a, **k):
                return write_synthetic_dem(
                    tmp_path / tag / "cache" / "dem" / "s.tif", requested_bbox.as_tuple()
                )

            monkeypatch.setattr(
                build_module,
                "fetch_level_osm",
                lambda cfg, box, **kw: (
                    synthetic_osm(cfg.projection, extent),
                    {
                        "tiles_requested": 1,
                        "tiles_succeeded": 1,
                        "tiles_failed": 0,
                        "tiles_skipped": 0,
                        "budget_exhausted": False,
                        "merge_summary": "",
                    },
                ),
            )
            monkeypatch.setattr("ivanovo2beam.terrain.dem.fetch_dem", fake_fetch_dem)
            build_level(config, progress=None, make_zip=False)
            return config.build_dir / "levels" / config.level.id

        first = run("a")
        second = run("b")
        for relative in ("theTerrain.ter", "info.json", "main/items.level.json"):
            assert (first / relative).read_bytes() == (second / relative).read_bytes(), relative


class TestBuildRegressions:
    def test_residential_landuse_stays_grass_not_asphalt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import ivanovo2beam.build as build_module

        config = ProjectConfig.load("configs/test_sector.yaml")
        assert config.level is not None
        object.__setattr__(config, "cache_dir", tmp_path / "cache")
        object.__setattr__(config, "build_dir", tmp_path / "build")
        _, _, extent = build_module._level_geographic_bbox(config, margin_deg=0.0)

        osm = synthetic_osm(config.projection, extent)
        # Add a residential polygon covering the north-east quadrant
        res_coords = [(100.0, 100.0), (400.0, 100.0), (400.0, 400.0), (100.0, 400.0), (100.0, 100.0)]
        next_node = max(osm.nodes) + 1
        refs = []
        for x, y in res_coords[:-1]:
            osm.nodes[next_node] = config.projection.world_to_geo(x, y)
            refs.append(next_node)
            next_node += 1
        refs.append(refs[0])
        osm.ways.append(OsmWay(id=9999, nodes=tuple(refs), tags={"landuse": "residential"}))

        monkeypatch.setattr(
            build_module,
            "fetch_level_osm",
            lambda cfg, box, **kw: (
                osm,
                {
                    "tiles_requested": 1,
                    "tiles_succeeded": 1,
                    "tiles_failed": 0,
                    "tiles_skipped": 0,
                    "budget_exhausted": False,
                    "merge_summary": "",
                },
            ),
        )
        monkeypatch.setattr(
            "ivanovo2beam.terrain.dem.fetch_dem",
            lambda requested_bbox, *a, **k: write_synthetic_dem(
                tmp_path / "cache" / "dem" / "s.tif", requested_bbox.as_tuple()
            ),
        )
        result = build_level(config, progress=None, make_zip=False)
        hist = result.stats["terrain"]["layer_histogram"]
        assert hist.get("Asphalt_worn", 0) == 0

    def test_choose_spawn_prefers_in_bounds_road_vertices(self) -> None:
        from types import SimpleNamespace

        from ivanovo2beam.build import _choose_spawn

        build = SimpleNamespace(
            elevations=np.full((16, 16), 120.0),
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=10.0,
        )
        out_road = SimpleNamespace(
            drivable=True,
            bridge=False,
            tunnel=False,
            points=np.array([[-50.0, 50.0], [-50.0, 60.0]]),
        )
        in_road = SimpleNamespace(
            drivable=True,
            bridge=False,
            tunnel=False,
            points=np.array([[20.0, 50.0], [30.0, 50.0]]),
        )
        chosen = _choose_spawn([out_road, in_road], build, anchor=(-10.0, 50.0))
        assert len(chosen) == 5
        assert 0.0 <= chosen[0] <= 150.0
        assert 0.0 <= chosen[1] <= 150.0

    def test_sky_and_sun_caps_visible_distance_and_sets_fog_density(self, offline_build) -> None:
        config, _ = offline_build
        level_dir = config.build_dir / "levels" / config.level.id
        sky_file = level_dir / "main" / "MissionGroup" / "level_objects" / "sky_and_sun" / "items.level.json"
        level_infos = [
            json.loads(line)
            for line in sky_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("class") == "LevelInfo"
        ]
        assert len(level_infos) == 1
        assert level_infos[0]["visibleDistance"] <= 12000.0
        assert level_infos[0]["fogDensity"] == pytest.approx(0.00018)

    def test_cached_overlapping_osm_reads_request_bbox_sidecars(self, tmp_path: Path) -> None:
        from ivanovo2beam.build import _cached_overlapping_osm_files
        from ivanovo2beam.gis.projection import BBox
        from ivanovo2beam.osm import read_cache_meta

        cache_dir = tmp_path / "osm"
        cache_dir.mkdir(parents=True)
        (cache_dir / "tile_in.osm").write_text("<osm/>", encoding="utf-8")
        (cache_dir / "tile_in.json").write_text(
            json.dumps({"request": {"bbox": {"west": 40.9, "south": 56.8, "east": 41.4, "north": 57.05}}}),
            encoding="utf-8",
        )
        (cache_dir / "tile_other.osm").write_text("<osm/>", encoding="utf-8")
        (cache_dir / "tile_other.json").write_text(
            json.dumps({"request": {"bbox": {"west": 10.0, "south": 10.0, "east": 11.0, "north": 11.0}}}),
            encoding="utf-8",
        )
        files, lon_cov, lat_cov = _cached_overlapping_osm_files(
            cache_dir, BBox(west=40.9, south=56.8, east=41.4, north=57.05), read_cache_meta
        )
        assert [p.name for p in files] == ["tile_in.osm"]
        assert lon_cov == pytest.approx(1.0)
        assert lat_cov == pytest.approx(1.0)

    def test_fetch_level_osm_prefers_local_pbf(self, tmp_path: Path) -> None:
        from ivanovo2beam.build import fetch_level_osm
        from ivanovo2beam.osm.pbf import write_osm_pbf

        config = ProjectConfig.load("configs/test_sector.yaml")
        object.__setattr__(config, "cache_dir", tmp_path / "cache")
        osm_dir = config.cache_path("osm")
        osm_dir.mkdir(parents=True)
        cx, cy = config.level_grid_center()
        lon, lat = config.projection.world_to_geo(cx, cy)
        lon2, lat2 = config.projection.world_to_geo(cx + 20.0, cy)
        pbf_data = OsmData(
            nodes={1: (lon, lat), 2: (lon2, lat2)},
            node_tags={},
            ways=[OsmWay(id=10, nodes=(1, 2), tags={"highway": "primary"})],
            relations=[],
            source="pbf-test",
        )
        pbf_path = osm_dir / "region.osm.pbf"
        write_osm_pbf(pbf_path, pbf_data)

        from ivanovo2beam.build import _level_geographic_bbox

        bbox, _, _ = _level_geographic_bbox(config)
        loaded, stats = fetch_level_osm(config, bbox)
        assert stats["source"] == str(pbf_path)
        assert "load_seconds" in stats
        assert 10 in {w.id for w in loaded.ways}

    def test_choose_spawn_prefers_major_highway_interior_node(self) -> None:
        from types import SimpleNamespace

        from ivanovo2beam.build import _choose_spawn

        build = SimpleNamespace(
            elevations=np.full((32, 32), 120.0),
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=10.0,
        )
        side_street = SimpleNamespace(
            drivable=True,
            bridge=False,
            tunnel=False,
            highway="service",
            points=np.array([[100.0, 100.0], [110.0, 100.0]]),
        )
        highway = SimpleNamespace(
            drivable=True,
            bridge=False,
            tunnel=False,
            highway="primary",
            points=np.array([[140.0, 100.0], [180.0, 100.0], [220.0, 100.0]]),
        )
        chosen = _choose_spawn([side_street, highway], build, anchor=(105.0, 100.0))
        # Must pick interior vertex (180, 100) on primary highway, not dead-end (140, 100) or service street.
        assert chosen[0] == pytest.approx(180.0)
        assert chosen[1] == pytest.approx(100.0)
        assert chosen[4] == pytest.approx(0.0)

    def test_build_cleans_stale_artifacts_and_uses_512_base_tex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ivanovo2beam.build as build_module

        config = ProjectConfig.load("configs/test_sector.yaml")
        assert config.level is not None
        object.__setattr__(config, "cache_dir", tmp_path / "cache")
        object.__setattr__(config, "build_dir", tmp_path / "build")
        _, _, extent = build_module._level_geographic_bbox(config, margin_deg=0.0)
        level_dir = config.build_path("levels", config.level.id)
        (level_dir / "forest").mkdir(parents=True, exist_ok=True)
        (level_dir / "main.materials.json").write_text("stale", encoding="utf-8")
        (level_dir / "forest" / "scots_pine.forest4.json").write_text("stale", encoding="utf-8")

        monkeypatch.setattr(
            build_module,
            "fetch_level_osm",
            lambda cfg, box, **kw: (
                synthetic_osm(cfg.projection, extent),
                {
                    "tiles_requested": 1,
                    "tiles_succeeded": 1,
                    "tiles_failed": 0,
                    "tiles_skipped": 0,
                    "budget_exhausted": False,
                    "merge_summary": "",
                },
            ),
        )
        monkeypatch.setattr(
            "ivanovo2beam.terrain.dem.fetch_dem",
            lambda requested_bbox, *a, **k: write_synthetic_dem(
                tmp_path / "cache" / "dem" / "s.tif", requested_bbox.as_tuple()
            ),
        )
        build_level(config, progress=None, make_zip=False)
        assert not (level_dir / "main.materials.json").exists()
        assert not (level_dir / "forest" / "scots_pine.forest4.json").exists()
        mat_doc = json.loads((level_dir / "art" / "terrains" / "main.materials.json").read_text(encoding="utf-8"))
        texset = mat_doc[f"{config.level.id}TerrainMaterialTextureSet"]
        assert texset["baseTexSize"] == [512, 512]

    def test_exported_bridge_scene_object_uses_localized_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ivanovo2beam.build as build_module

        config = ProjectConfig.load("configs/test_sector.yaml")
        assert config.level is not None
        object.__setattr__(config, "cache_dir", tmp_path / "cache")
        object.__setattr__(config, "build_dir", tmp_path / "build")
        _, _, extent = build_module._level_geographic_bbox(config, margin_deg=0.0)
        osm = synthetic_osm(config.projection, extent)
        # Add a non-degenerate bridge away from (0, 0)
        n0 = max(osm.nodes) + 1
        osm.nodes[n0] = config.projection.world_to_geo(250.0, 300.0)
        osm.nodes[n0 + 1] = config.projection.world_to_geo(310.0, 300.0)
        osm.ways.append(OsmWay(id=8888, nodes=(n0, n0 + 1), tags={"highway": "primary", "bridge": "yes"}))

        monkeypatch.setattr(
            build_module,
            "fetch_level_osm",
            lambda cfg, box, **kw: (
                osm,
                {
                    "tiles_requested": 1,
                    "tiles_succeeded": 1,
                    "tiles_failed": 0,
                    "tiles_skipped": 0,
                    "budget_exhausted": False,
                    "merge_summary": "",
                },
            ),
        )
        monkeypatch.setattr(
            "ivanovo2beam.terrain.dem.fetch_dem",
            lambda requested_bbox, *a, **k: write_synthetic_dem(
                tmp_path / "cache" / "dem" / "s.tif", requested_bbox.as_tuple()
            ),
        )
        build_level(config, progress=None, make_zip=False)
        bridges_file = (
            config.build_dir / "levels" / config.level.id / "main" / "MissionGroup" / "level_objects" / "bridges" / "items.level.json"
        )
        bridges = [
            json.loads(line)
            for line in bridges_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("class") == "TSStatic"
        ]
        target = [b for b in bridges if "bridge_way8888" in b.get("shapeName", "")]
        assert len(target) == 2
        by_decal = {b.get("decalType"): b for b in target}
        assert "Visible Mesh" in by_decal and "None" in by_decal
        for obj in target:
            assert obj["position"][0] == pytest.approx(280.0, abs=1.0)
            assert obj["position"][1] == pytest.approx(300.0, abs=1.0)
            assert obj["position"][2] > 100.0

    def test_custom_level_honors_level_spawn_instead_of_city_center(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import dataclasses

        import ivanovo2beam.build as build_module

        base = ProjectConfig.load("configs/test_sector.yaml")
        assert base.level is not None
        # Shift center and spawn ~10 km SE of Ivanovo city center.
        spawn_lon, spawn_lat = base.projection.world_to_geo(10_000.0, -8_000.0)
        custom_level = dataclasses.replace(
            base.level,
            id="custom_golden_test",
            center_lon=spawn_lon,
            center_lat=spawn_lat,
            spawn_lon=spawn_lon,
            spawn_lat=spawn_lat,
        )
        config = dataclasses.replace(
            base,
            level=custom_level,
            cache_dir=tmp_path / "cache",
            build_dir=tmp_path / "build",
        )
        _, _, extent = build_module._level_geographic_bbox(config, margin_deg=0.0)
        osm = synthetic_osm(config.projection, extent)
        # Re-center synthetic road nodes around (10000, -8000) so _choose_spawn stays local.
        for nid, (lon, lat) in list(osm.nodes.items()):
            wx, wy = config.projection.geo_to_world(lon, lat)
            osm.nodes[nid] = config.projection.world_to_geo(wx + 10_000.0, wy - 8_000.0)

        monkeypatch.setattr(
            build_module,
            "fetch_level_osm",
            lambda cfg, box, **kw: (
                osm,
                {
                    "tiles_requested": 1,
                    "tiles_succeeded": 1,
                    "tiles_failed": 0,
                    "tiles_skipped": 0,
                    "budget_exhausted": False,
                    "merge_summary": "",
                },
            ),
        )
        monkeypatch.setattr(
            "ivanovo2beam.terrain.dem.fetch_dem",
            lambda requested_bbox, *a, **k: write_synthetic_dem(
                tmp_path / "cache" / "dem" / "s.tif", requested_bbox.as_tuple()
            ),
        )
        result = build_level(config, progress=None, make_zip=False)
        assert result.stats["spawn"]["x"] == pytest.approx(10_000.0, abs=500.0)
        assert result.stats["spawn"]["y"] == pytest.approx(-8_000.0, abs=500.0)
        spawn_file = (
            config.build_dir / "levels" / config.level.id / "main" / "MissionGroup" / "PlayerDropPoints" / "items.level.json"
        )
        spawns = [json.loads(line) for line in spawn_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(spawns) == 1
        assert spawns[0]["name"] == "spawn_default"
