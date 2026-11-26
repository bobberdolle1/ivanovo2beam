"""Tests for the P0 Single-Terrain 8192x8192 @ 2.0m configuration, offline DEM slicing, and script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from ivanovo2beam.config import ProjectConfig
from ivanovo2beam.gis.projection import BBox
from ivanovo2beam.terrain import _geotiff, dem
from ivanovo2beam.terrain.dem import DemReader, DemSourceUnavailable, cache_key, fetch_dem, read_sidecar

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import rasterio
    from rasterio.transform import Affine

    _open_raster = rasterio.open
except ImportError:  # pragma: no cover - optional rasterio / sandbox fallback
    rasterio = None  # type: ignore[assignment]
    Affine = _geotiff.FallbackAffine  # type: ignore[assignment,misc]
    _open_raster = _geotiff.open_geotiff


def _write_cached_dem_entry(
    cache_dir: Path,
    bbox: BBox,
    *,
    source: str = "copernicus_glo30",
    elevation_m: float = 125.0,
) -> Path:
    """Create a valid cached GeoTIFF + JSON sidecar entry covering ``bbox``."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(bbox, source, 30.0)
    tif_path = cache_dir / f"{key}.tif"
    json_path = cache_dir / f"{key}.json"

    width, height = 32, 32
    xres = (bbox.east - bbox.west) / (width - 2)
    yres = (bbox.north - bbox.south) / (height - 2)
    left = bbox.west - xres
    top = bbox.north + yres
    transform = Affine.translation(left, top) @ Affine.scale(xres, -yres)

    row_ramp = np.linspace(0.0, 10.0, height, dtype=np.float32)[:, None]
    col_ramp = np.linspace(0.0, 5.0, width, dtype=np.float32)[None, :]
    data = np.broadcast_to(elevation_m + row_ramp + col_ramp, (height, width)).astype(np.float32)

    with _open_raster(
        tif_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=float("nan"),
    ) as dst:
        dst.write(data, 1)

    sidecar = {
        "schema": "ivanovo2beam.dem/1",
        "cache_key": key,
        "requested_source": source,
        "source": source,
        "crs_epsg": 4326,
        "bbox": {
            "west": left,
            "south": top - height * yres,
            "east": left + width * xres,
            "north": top,
        },
        "requested_bbox": {
            "west": bbox.west,
            "south": bbox.south,
            "east": bbox.east,
            "north": bbox.north,
        },
        "source_urls": ["https://example.invalid/superset.tif"],
        "bytes": tif_path.stat().st_size,
        "sha256": dem._sha256_file(tif_path),
    }
    json_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return tif_path


def test_p0_config_geometry_and_features() -> None:
    cfg = ProjectConfig.load("configs/experiment_single_8192_2m.yaml", root=REPO_ROOT)
    assert cfg.level is not None
    assert cfg.level.id == "ivanovo_single_8192_2m"
    assert cfg.level.grid.grid_size == 8192
    assert cfg.level.grid.square_size_m == pytest.approx(2.0)
    assert cfg.level.grid.extent_m == pytest.approx(16382.0)

    # All Golden Sector V2 features enabled
    assert cfg.buildings_enabled is True
    assert cfg.vegetation_enabled is True
    assert cfg.water_enabled is True
    assert cfg.railways_enabled is True
    assert cfg.bridges_enabled is True

    # Spawn lies comfortably inside the 16.382 x 16.382 km terrain bounds
    ox, oy = cfg.level_terrain_origin()
    sx, sy = cfg.level.spawn_anchor(cfg.projection)
    assert ox + 500.0 <= sx <= ox + cfg.level.grid.extent_m - 500.0
    assert oy + 500.0 <= sy <= oy + cfg.level.grid.extent_m - 500.0


def test_p0_bbox_inside_cached_copernicus_extent() -> None:
    from ivanovo2beam.build import _level_geographic_bbox

    cfg = ProjectConfig.load("configs/experiment_single_8192_2m.yaml", root=REPO_ROOT)
    bbox, _, _ = _level_geographic_bbox(cfg, margin_deg=0.004)
    # Cached Copernicus superset bbox: {west: 40.895, south: 56.772222, east: 41.445, north: 57.075833}
    assert bbox.west >= 40.896
    assert bbox.south >= 56.773
    assert bbox.east <= 41.444
    assert bbox.north <= 57.075


def test_fetch_dem_slices_from_cached_superset_when_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "dem"
    superset = BBox(west=40.895, south=56.772, east=41.445, north=57.075)
    _write_cached_dem_entry(cache_dir, superset, source="copernicus_glo30", elevation_m=120.0)

    for key in dem.SOURCE_CHAIN:
        def _offline(*_a: object, _k: str = key, **_kw: object):
            raise DemSourceUnavailable(f"{_k}: offline")

        monkeypatch.setitem(dem._PROVIDERS, key, _offline)

    sub_bbox = BBox(west=41.03, south=56.85, east=41.31, north=57.00)
    out_tif = fetch_dem(sub_bbox, cache_dir, source="copernicus_glo30", resolution_m=30.0)
    assert out_tif.is_file()
    meta = read_sidecar(out_tif)
    assert meta["source"] == "copernicus_glo30"
    assert meta["fallback_used"] is False
    assert "cached_slice_from" in meta["provider_detail"]

    with DemReader(out_tif) as reader:
        assert reader.bounds.west <= sub_bbox.west
        assert reader.bounds.south <= sub_bbox.south
        assert reader.bounds.east >= sub_bbox.east
        assert reader.bounds.north >= sub_bbox.north
        grid, _ = reader.window_array(sub_bbox, 16)
        assert not np.isnan(grid).any()
        assert 115.0 < float(np.mean(grid)) < 140.0


def test_fetch_dem_ignores_invalid_sidecar_during_offline_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = tmp_path / "dem"
    superset = BBox(west=40.895, south=56.772, east=41.445, north=57.075)
    tif_path = _write_cached_dem_entry(cache_dir, superset)
    # Corrupt recorded byte count in sidecar so _valid_cache_entry rejects it
    json_path = tif_path.with_suffix(".json")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    data["bytes"] = 123
    json_path.write_text(json.dumps(data), encoding="utf-8")

    for key in dem.SOURCE_CHAIN:
        monkeypatch.setitem(
            dem._PROVIDERS,
            key,
            lambda *_a, _k=key, **_kw: (_ for _ in ()).throw(DemSourceUnavailable(f"{_k}: offline")),
        )

    with pytest.raises(dem.DemFetchError):
        fetch_dem(BBox(west=41.03, south=56.85, east=41.31, north=57.00), cache_dir)


def test_experiment_script_offline_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import experiment_single_8192 as exp_mod
    import ivanovo2beam.build as build_module
    from ivanovo2beam.config import TerrainGridSpec
    from ivanovo2beam.osm.types import OsmData, OsmWay

    base_cfg = ProjectConfig.load("configs/experiment_single_8192_2m.yaml", root=REPO_ROOT)
    assert base_cfg.level is not None
    small_level = type(base_cfg.level)(
        id=base_cfg.level.id,
        title=base_cfg.level.title,
        center_lon=base_cfg.level.center_lon,
        center_lat=base_cfg.level.center_lat,
        grid=TerrainGridSpec(grid_size=128, square_size_m=2.0),
        ground_margin_m=base_cfg.level.ground_margin_m,
        spawn_lon=base_cfg.level.center_lon,
        spawn_lat=base_cfg.level.center_lat,
    )
    small_cfg = type(base_cfg)(
        root=base_cfg.root,
        region_id=base_cfg.region_id,
        region_title=base_cfg.region_title,
        bbox=base_cfg.bbox,
        projection=base_cfg.projection,
        terrain_source=base_cfg.terrain_source,
        terrain_resolution_m=base_cfg.terrain_resolution_m,
        sectors=base_cfg.sectors,
        roads=base_cfg.roads,
        buildings_enabled=False,
        vegetation_enabled=False,
        water_enabled=False,
        railways_enabled=False,
        bridges_enabled=False,
        level=small_level,
        cache_dir=tmp_path / "cache",
        build_dir=tmp_path / "build",
    )

    monkeypatch.setattr(exp_mod.ProjectConfig, "load", lambda *_a, **_kw: small_cfg)

    cx, cy = small_cfg.level_grid_center()
    lon0, lat0 = small_cfg.projection.world_to_geo(cx - 80.0, cy)
    lon1, lat1 = small_cfg.projection.world_to_geo(cx, cy)
    lon2, lat2 = small_cfg.projection.world_to_geo(cx + 80.0, cy)
    osm = OsmData(
        nodes={1: (lon0, lat0), 2: (lon1, lat1), 3: (lon2, lat2)},
        node_tags={},
        ways=[OsmWay(id=101, nodes=(1, 2, 3), tags={"highway": "primary", "lanes": "2"})],
        relations=[],
        source="synthetic",
    )

    bbox, _, _ = build_module._level_geographic_bbox(small_cfg, margin_deg=0.004)
    _write_cached_dem_entry(small_cfg.cache_path("dem"), bbox.expanded(0.01))
    for key in dem.SOURCE_CHAIN:
        monkeypatch.setitem(
            dem._PROVIDERS,
            key,
            lambda *_a, _k=key, **_kw: (_ for _ in ()).throw(DemSourceUnavailable(f"{_k}: offline")),
        )
    monkeypatch.setattr(
        build_module,
        "fetch_level_osm",
        lambda *_a, **_kw: (
            osm,
            {
                "tiles_requested": 1,
                "tiles_succeeded": 1,
                "tiles_failed": 0,
                "tiles_skipped": 0,
                "budget_exhausted": False,
                "merge_summary": "synthetic",
            },
        ),
    )

    rc = exp_mod.main(
        [
            "--cache-dir",
            str(small_cfg.cache_dir),
            "--build-dir",
            str(small_cfg.build_dir),
            "--no-beamng-check",
        ]
    )
    assert rc == 0
    level_dir = small_cfg.build_path("levels", small_cfg.level.id)
    assert (level_dir / "theTerrain.ter").is_file()
