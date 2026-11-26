"""Unit and offline end-to-end tests for P2 multi-TerrainBlock scaling (multiblock)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from ivanovo2beam.beamng.ter import read_ter
from ivanovo2beam.config import ConfigError, ProjectConfig
from ivanovo2beam.osm.types import OsmData, OsmWay
from ivanovo2beam.sectors.multiblock import (
    MultiBlockSpec,
    build_multiblock_level,
    plan_tile_layout,
    slice_terrain_tiles,
    split_polyline_at_seams,
    validate_multiblock_level,
    verify_tile_seams,
)
from ivanovo2beam.terrain import _geotiff
from ivanovo2beam.terrain.builder import TerrainBuild

try:
    import rasterio
    from rasterio.transform import from_bounds

    _open_raster = rasterio.open
except ImportError:  # pragma: no cover - optional rasterio / sandbox fallback
    rasterio = None  # type: ignore[assignment]
    from_bounds = _geotiff.fallback_from_bounds
    _open_raster = _geotiff.open_geotiff


def _write_synthetic_dem(path: Path, bbox: tuple[float, float, float, float]) -> Path:
    west, south, east, north = bbox
    width = height = 64
    lons = np.linspace(west, east, width, dtype=np.float64).reshape(1, width)
    lats = np.linspace(south, north, height, dtype=np.float64).reshape(height, 1)
    data = (120.0 + (lats - south) * 400.0 + (lons - west) * 200.0).astype(np.float32)
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


def _synthetic_seam_osm(projection, origin_x: float, origin_y: float, extent_w: float, extent_h: float) -> OsmData:
    """Create road, river, forest, and building features that deliberately cross x_seam."""
    x_seam = origin_x + extent_w * 0.5
    y_mid = origin_y + extent_h * 0.5
    nodes: dict[int, tuple[float, float]] = {}
    ways: list[OsmWay] = []
    next_id = 1

    def add_way(coords: list[tuple[float, float]], tags: dict[str, str], osm_id: int, *, closed: bool = False) -> None:
        nonlocal next_id
        refs = []
        for x, y in coords:
            lon, lat = projection.world_to_geo(x, y)
            nodes[next_id] = (lon, lat)
            refs.append(next_id)
            next_id += 1
        if closed and refs:
            refs.append(refs[0])
        ways.append(OsmWay(id=osm_id, nodes=tuple(refs), tags=tags))

    # Primary road crossing x_seam
    add_way(
        [(x_seam - 90.0, y_mid), (x_seam + 90.0, y_mid)],
        {"highway": "primary", "lanes": "2", "surface": "asphalt"},
        101,
    )
    # River crossing x_seam
    add_way(
        [(x_seam - 80.0, y_mid - 40.0), (x_seam + 80.0, y_mid - 40.0)],
        {"waterway": "river", "width": "12"},
        102,
    )
    # Closed forest polygon crossing x_seam
    forest_ring = [
        (x_seam - 45.0, y_mid + 20.0),
        (x_seam + 45.0, y_mid + 20.0),
        (x_seam + 45.0, y_mid + 75.0),
        (x_seam - 45.0, y_mid + 75.0),
    ]
    add_way(forest_ring, {"natural": "wood"}, 103, closed=True)
    # Closed building polygon crossing x_seam
    bld_ring = [
        (x_seam - 8.0, y_mid - 20.0),
        (x_seam + 8.0, y_mid - 20.0),
        (x_seam + 8.0, y_mid - 10.0),
        (x_seam - 8.0, y_mid - 10.0),
    ]
    add_way(bld_ring, {"building": "house"}, 104, closed=True)

    return OsmData(nodes=nodes, node_tags={}, ways=ways, relations=[], source="synthetic")


class TestMultiBlockSpecAndLayout:
    def test_spec_dimensions_for_2x1_2048(self) -> None:
        spec = MultiBlockSpec(tile_grid_size=2048, square_size_m=1.0, cols=2, rows=1)
        assert spec.tile_extent_m == 2047.0
        assert spec.total_width_m == 4094.0
        assert spec.total_height_m == 2047.0
        assert spec.combined_cols == 4095
        assert spec.combined_rows == 2048
        assert spec.parent_grid_size == 4096

    def test_invalid_spec_rejected(self) -> None:
        with pytest.raises(ConfigError):
            MultiBlockSpec(tile_grid_size=300, square_size_m=1.0, cols=2, rows=1)
        with pytest.raises(ConfigError):
            MultiBlockSpec(tile_grid_size=2048, square_size_m=1.0, cols=0, rows=1)
        with pytest.raises(ConfigError):
            MultiBlockSpec(tile_grid_size=4096, square_size_m=1.0, cols=3, rows=1)
        with pytest.raises(ConfigError):
            MultiBlockSpec(tile_grid_size=128, square_size_m=1.0, road_seam_mode="bogus")

    def test_load_experiment_yaml_config(self) -> None:
        cfg = ProjectConfig.load("configs/experiment_dual_terrain.yaml")
        spec = MultiBlockSpec.from_yaml("configs/experiment_dual_terrain.yaml")
        assert cfg.level is not None
        assert cfg.level.id == "ivanovo_dual_terrain_exp"
        assert spec.cols == 2
        assert spec.rows == 1
        assert spec.tile_grid_size == 2048
        assert spec.origin_world == (6303.0, -9800.0)

    def test_plan_tile_layout_border_alignment(self) -> None:
        spec = MultiBlockSpec(tile_grid_size=128, square_size_m=2.0, cols=2, rows=2)
        layout = plan_tile_layout(spec, 100.0, -50.0)
        assert len(layout) == 4
        step = 127 * 2.0
        assert layout[0] == (0, 0, "theTerrain_0_0", 100.0, -50.0)
        assert layout[1] == (1, 0, "theTerrain_1_0", 100.0 + step, -50.0)
        assert layout[2] == (0, 1, "theTerrain_0_1", 100.0, -50.0 + step)
        assert layout[3] == (1, 1, "theTerrain_1_1", 100.0 + step, -50.0 + step)


class TestSlicingAndSeamVerification:
    def test_slice_2x2_shares_exact_border_heights_and_layers(self) -> None:
        spec = MultiBlockSpec(tile_grid_size=128, square_size_m=1.0, cols=2, rows=2)
        rng = np.random.default_rng(42)
        elev = 100.0 + rng.uniform(0.0, 45.0, size=(256, 256))
        layers = rng.integers(0, 5, size=(256, 256), dtype=np.uint8)
        mats = ("Grass", "Field", "ForestFloor", "Dirt", "Asphalt")
        parent = TerrainBuild(
            elevations=elev,
            layers=layers,
            materials=mats,
            base_z=92.0,
            max_height=60.0,
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=1.0,
        )
        tiles, _, _ = slice_terrain_tiles(parent, spec, level_id="test_mb")
        assert len(tiles) == 4
        seam = verify_tile_seams(tiles)
        assert seam.seams_checked == 4
        assert seam.seam_vertices_checked == 4 * 128
        assert seam.all_seams_exact
        assert seam.max_elevation_diff_m == 0.0
        assert seam.max_quantized_diff == 0
        assert seam.layer_mismatches == 0

    def test_split_polyline_at_seams_preserves_continuity(self) -> None:
        pts = np.array([[0.0, 10.0, 120.0], [100.0, 30.0, 124.0], [200.0, 50.0, 128.0]], dtype=np.float64)
        parts = split_polyline_at_seams(pts, seam_xs=[127.0], seam_ys=[])
        assert len(parts) == 2
        np.testing.assert_allclose(parts[0][-1], parts[1][0], atol=1e-9)
        assert parts[0][-1, 0] == pytest.approx(127.0)
        assert parts[0][-1, 1] == pytest.approx(35.4)
        assert parts[0][-1, 2] == pytest.approx(125.08)


class TestOfflineDualTerrainEndToEnd:
    @pytest.mark.parametrize("road_seam_mode", ["continuous", "split"])
    def test_offline_dual_terrain_build_and_validate(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        road_seam_mode: str,
    ) -> None:
        import ivanovo2beam.sectors.multiblock as mb_mod

        cfg = ProjectConfig.load("configs/test_sector.yaml")
        assert cfg.level is not None
        object.__setattr__(cfg, "cache_dir", tmp_path / "cache")
        object.__setattr__(cfg, "build_dir", tmp_path / "build")
        object.__setattr__(cfg, "vegetation_enabled", True)
        object.__setattr__(cfg, "water_enabled", True)
        object.__setattr__(cfg, "buildings_enabled", True)
        object.__setattr__(
            cfg,
            "level",
            type(cfg.level)(
                id=f"test_dual_{road_seam_mode}",
                title="Test Dual Terrain",
                center_lon=cfg.level.center_lon,
                center_lat=cfg.level.center_lat,
                grid=cfg.level.grid,
                ground_margin_m=5.0,
            ),
        )

        spec = MultiBlockSpec(
            tile_grid_size=128,
            square_size_m=2.0,
            cols=2,
            rows=1,
            origin_world=(-254.0, -127.0),
            road_seam_mode=road_seam_mode,
            groundcover_enabled=True,
            max_buildings=20,
        )

        def fake_resolve_dem(bbox, *args, **kwargs) -> Path:
            return _write_synthetic_dem(tmp_path / "cache" / "dem" / "synth.tif", bbox.as_tuple())

        def fake_fetch_osm(c, box, **kwargs):
            return _synthetic_seam_osm(c.projection, -254.0, -127.0, spec.total_width_m, spec.total_height_m), {
                "tiles_requested": 1,
                "tiles_succeeded": 1,
                "tiles_failed": 0,
                "tiles_skipped": 0,
                "budget_exhausted": False,
            }

        monkeypatch.setattr(mb_mod, "resolve_dem_for_bbox", fake_resolve_dem)
        monkeypatch.setattr(mb_mod, "fetch_level_osm", fake_fetch_osm)

        result = build_multiblock_level(cfg, spec, make_zip=False)
        assert result.seam_report.all_seams_exact
        assert result.seam_report.roads_crossing_seam >= 1
        assert result.seam_report.rivers_crossing_seam >= 1
        assert result.seam_report.forests_crossing_seam >= 1
        assert result.seam_report.buildings_crossing_seam >= 1

        report = validate_multiblock_level(result.level_dir, cfg)
        assert report.ok, report.describe()

        tf_w = read_ter(result.level_dir / "theTerrain_0_0.ter")
        tf_e = read_ter(result.level_dir / "theTerrain_1_0.ter")
        np.testing.assert_array_equal(tf_w.heights[:, -1], tf_e.heights[:, 0])
        np.testing.assert_array_equal(tf_w.layers[:, -1], tf_e.layers[:, 0])

        manifest = json.loads((result.level_dir / "multiblock_experiment.json").read_text(encoding="utf-8"))
        assert manifest["road_seam_mode"] == road_seam_mode
        assert len(manifest["tiles"]) == 2
