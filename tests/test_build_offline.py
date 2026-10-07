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
import rasterio
from rasterio.transform import from_bounds

from ivanovo2beam.beamng.ter import read_ter
from ivanovo2beam.build import build_level
from ivanovo2beam.config import ProjectConfig
from ivanovo2beam.osm.types import OsmData, OsmWay
from ivanovo2beam.validation.level import validate_level

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
    with rasterio.open(
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
    shapes.append(
        ([(half - step, -half + step) for _ in range(2)], {"highway": "primary", "bridge": "yes"})
    )
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

    bbox, _, extent = build_module._level_geographic_bbox(config, margin_deg=0.0)

    def fake_fetch_dem(requested_bbox, *args, **kwargs) -> Path:  # noqa: ANN001, ANN002, ANN003
        """Stand in for the real fetcher, covering exactly the requested area."""
        return write_synthetic_dem(
            tmp_path / "cache" / "dem" / "synthetic.tif", requested_bbox.as_tuple()
        )

    def fake_fetch_osm(cfg, box, **kwargs):  # noqa: ANN001, ANN003
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
            "theTerrain.ter",
            "theTerrain.terrain.json",
            "map.json",
        ):
            assert (level_dir / relative).is_file(), relative

    def test_validation_passes(self, offline_build) -> None:
        config, result = offline_build
        level_dir = config.build_dir / "levels" / config.level.id
        report = validate_level(level_dir, config)
        assert report.ok, report.describe()

    def test_terrain_is_georeferenced_to_the_level(self, offline_build) -> None:
        config, _ = offline_build
        level_dir = config.build_dir / "levels" / config.level.id
        terrain = read_ter(level_dir / "theTerrain.ter")
        assert terrain.size == config.level.grid.grid_size

        scene = (level_dir / "main" / "MissionGroup" / "level_objects" / "terrain" / "items.level.json")
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
        terrain = read_ter(level_dir / "theTerrain.ter")
        spawn_dir = level_dir / "main" / "MissionGroup" / "PlayerDropPoints" / "items.level.json"
        spawns = [json.loads(line) for line in spawn_dir.read_text().splitlines() if line.strip()]
        assert spawns, "a level with no spawn point cannot be entered"
        spawn = spawns[0]
        assert spawn["class"] == "SpawnSphere"

    def test_roads_were_graded_into_the_terrain(self, offline_build) -> None:
        config, result = offline_build
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
        assert not any(p.suffix in {".dds", ".dae", ".cdae"} for p in level_dir.rglob("*"))

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
        assert suffixes <= {".json", ".ter", ".txt", ".md"}, suffixes

    def test_build_is_deterministic(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same inputs must give byte-identical terrain and scene files."""
        import ivanovo2beam.build as build_module

        def run(tag: str):
            config = ProjectConfig.load("configs/test_sector.yaml")
            object.__setattr__(config, "cache_dir", tmp_path / tag / "cache")
            object.__setattr__(config, "build_dir", tmp_path / tag / "build")
            _, _, extent = build_module._level_geographic_bbox(config, margin_deg=0.0)

            def fake_fetch_dem(requested_bbox, *a, **k):  # noqa: ANN001, ANN002, ANN003
                return write_synthetic_dem(
                    tmp_path / tag / "cache" / "dem" / "s.tif", requested_bbox.as_tuple()
                )

            monkeypatch.setattr(build_module, "fetch_level_osm", lambda cfg, box, **kw: (
                synthetic_osm(cfg.projection, extent),
                {"tiles_requested": 1, "tiles_succeeded": 1, "tiles_failed": 0,
                 "tiles_skipped": 0, "budget_exhausted": False, "merge_summary": ""},
            ))
            monkeypatch.setattr("ivanovo2beam.terrain.dem.fetch_dem", fake_fetch_dem)
            build_level(config, progress=None, make_zip=False)
            return config.build_dir / "levels" / config.level.id

        first = run("a")
        second = run("b")
        for relative in ("theTerrain.ter", "info.json", "main/items.level.json"):
            assert (first / relative).read_bytes() == (second / relative).read_bytes(), relative
