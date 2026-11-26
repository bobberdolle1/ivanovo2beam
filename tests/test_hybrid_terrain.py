"""Unit and offline end-to-end tests for P1 Hybrid Terrain (base TerrainBlock + RTIN corridor meshes)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pytest

from ivanovo2beam.beamng.level import SceneTree
from ivanovo2beam.beamng.materials import DEFAULT_MATERIAL_ORDER
from ivanovo2beam.beamng.ter import read_ter
from ivanovo2beam.config import ConfigError, ProjectConfig
from ivanovo2beam.osm.types import OsmData, OsmWay
from ivanovo2beam.sectors.hybrid import (
    HybridSpec,
    HybridSurfaceIndex,
    apply_hybrid_decal_projection,
    build_corridor_rtin_tile,
    build_hybrid_level,
    depress_base_terrain,
    rtin_errors,
    rtin_triangles,
    validate_hybrid_level,
    write_corridor_materials,
    write_corridor_tile_dae,
)
from ivanovo2beam.terrain import _geotiff
from ivanovo2beam.terrain.builder import RoadCorridor, TerrainBuild

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


def _synthetic_hybrid_osm(
    projection,
    origin_x: float,
    origin_y: float,
    extent_m: float,
) -> OsmData:
    """Create a P152-like primary highway + side road + bridge + river + forest + building."""
    cx = origin_x + extent_m * 0.5
    cy = origin_y + extent_m * 0.5
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

    # Main primary highway crossing tile boundary at x = origin_x + 256 (or mid-extent)
    add_way(
        [(origin_x + 20.0, cy), (cx - 25.0, cy)],
        {"highway": "primary", "lanes": "2", "surface": "asphalt"},
        201,
    )
    add_way(
        [(cx - 25.0, cy), (cx + 25.0, cy)],
        {"highway": "primary", "lanes": "2", "surface": "asphalt", "bridge": "yes", "layer": "1"},
        202,
    )
    add_way(
        [(cx + 25.0, cy), (origin_x + extent_m - 20.0, cy)],
        {"highway": "primary", "lanes": "2", "surface": "asphalt"},
        203,
    )
    # Intersecting residential street joining primary road
    add_way(
        [(cx - 90.0, cy), (cx - 90.0, cy + 160.0)],
        {"highway": "residential", "lanes": "2", "surface": "asphalt"},
        204,
    )
    # River under the bridge
    add_way(
        [(cx, origin_y + 20.0), (cx, origin_y + extent_m - 20.0)],
        {"waterway": "river", "width": "14"},
        205,
    )
    # Forest polygon & building outside carriageway
    add_way(
        [
            (cx - 180.0, cy + 50.0),
            (cx - 110.0, cy + 50.0),
            (cx - 110.0, cy + 130.0),
            (cx - 180.0, cy + 130.0),
        ],
        {"natural": "wood"},
        206,
        closed=True,
    )
    add_way(
        [
            (cx - 75.0, cy + 45.0),
            (cx - 60.0, cy + 45.0),
            (cx - 60.0, cy + 60.0),
            (cx - 75.0, cy + 60.0),
        ],
        {"building": "house"},
        207,
        closed=True,
    )

    return OsmData(nodes=nodes, node_tags={}, ways=ways, relations=[], source="synthetic")


class TestHybridSpec:
    def test_load_experiment_hybrid_yaml_matches_p0_territory(self) -> None:
        cfg = ProjectConfig.load("configs/experiment_hybrid_corridor.yaml")
        spec = HybridSpec.from_yaml("configs/experiment_hybrid_corridor.yaml")
        assert cfg.level is not None
        assert cfg.level.id == "ivanovo_hybrid_corridor"
        assert (cfg.level.center_lon, cfg.level.center_lat) == pytest.approx((41.16958, 56.92439))
        assert (cfg.level.spawn_lon, cfg.level.spawn_lat) == pytest.approx((41.10000, 56.92700))
        assert cfg.level.grid.grid_size == 4096
        assert cfg.level.grid.square_size_m == 4.0
        assert cfg.level.grid.extent_m == pytest.approx(16380.0)
        assert spec.tile_squares == 512
        assert spec.corridor_cell_size_m == 1.0
        assert 0.05 <= spec.rtin_max_error_m <= 0.15
        assert 0.35 <= spec.depress_depth_m <= 0.60

    def test_invalid_hybrid_spec_rejected(self) -> None:
        with pytest.raises(ConfigError):
            HybridSpec(tile_squares=300)
        with pytest.raises(ConfigError):
            HybridSpec(corridor_cell_size_m=0.0)
        with pytest.raises(ConfigError):
            HybridSpec(depress_depth_m=0.05)
        with pytest.raises(ConfigError):
            HybridSpec(depress_inner_extra_m=10.0, depress_outer_extra_m=8.0)


class TestRtinMesher:
    def test_rtin_respects_error_bound_and_forces_borders(self) -> None:
        t = 64
        xs = np.arange(t + 1, dtype=np.float64)[None, :]
        ys = np.arange(t + 1, dtype=np.float64)[:, None]
        # Smooth hill + sharp ridge inside tile
        heights = 2.0 * np.sin(xs / 9.0) * np.cos(ys / 11.0) + 0.8 * np.exp(-((xs - 30) ** 2 + (ys - 32) ** 2) / 18.0)
        force = np.zeros((t + 1, t + 1), dtype=bool)
        force[0, :] = True
        force[-1, :] = True
        force[:, 0] = True
        force[:, -1] = True

        max_err = 0.05
        errors = rtin_errors(heights, force, t)
        tris = rtin_triangles(errors, t, max_err)
        assert len(tris) < 2 * t * t  # Actually simplified on flat/smooth zones
        assert len(tris) > 100

        # Border edges must be forced at unit step so neighbouring tiles never crack
        border_pts = set()
        for ax, ay, cx, cy, bx, by in tris:
            for px, py in ((ax, ay), (cx, cy), (bx, by)):
                if px == 0 or px == t or py == 0 or py == t:
                    border_pts.add((px, py))
        assert len(border_pts) == 4 * t


class TestBaseDepressionAndCorridorMeshes:
    def _make_base_build_and_corridors(self):
        n = 128
        cell = 4.0
        ox, oy = 0.0, 0.0
        xs = ox + np.arange(n, dtype=np.float64) * cell
        ys = oy + np.arange(n, dtype=np.float64) * cell
        gx, gy = np.meshgrid(xs, ys)
        elev = 100.0 + 0.02 * gx + 0.01 * gy
        layers = np.zeros((n, n), dtype=np.uint8)  # Grass (idx 0)
        corridor = RoadCorridor(
            points=np.array([[20.0, 256.0], [490.0, 256.0]], dtype=np.float64),
            half_width_m=5.0,
            corridor_half_width_m=20.0,
            priority=80,
            surface_z=np.array([105.0, 114.0], dtype=np.float64),
            grade_terrain=True,
            material_key="Asphalt",
            shoulder_material_key="Grass",
            key="way1",
        )
        build = TerrainBuild(
            elevations=elev,
            layers=layers,
            materials=DEFAULT_MATERIAL_ORDER,
            base_z=90.0,
            max_height=40.0,
            origin_x=ox,
            origin_y=oy,
            cell_size_m=cell,
        )
        return build, [corridor]

    def test_depress_base_terrain_prevents_double_collision_and_groundcover_poke(self) -> None:
        build, corridors = self._make_base_build_and_corridors()
        spec = HybridSpec(tile_squares=64, corridor_cell_size_m=1.0, rtin_max_error_m=0.08)
        depressed, clearance, count = depress_base_terrain(build, corridors, spec)
        assert count > 0

        # At y=256 (centreline): base terrain must be depressed by exactly depress_depth_m
        row_mid = 64  # y = 64 * 4 = 256
        cols_in = np.arange(10, 115)
        dz_in = depressed.elevations[row_mid, cols_in] - build.elevations[row_mid, cols_in]
        assert np.allclose(dz_in, -spec.depress_depth_m, atol=1e-6)

        # Far from road (y=40): untouched
        row_far = 10
        assert np.allclose(depressed.elevations[row_far, :], build.elevations[row_far, :])

        # Grass layer inside corridor depression must be suppressed to Asphalt/Gravel
        grass_idx = DEFAULT_MATERIAL_ORDER.index("Grass")
        assert not (depressed.layers[row_mid, cols_in] == grass_idx).any()

    def test_adjacent_corridor_tiles_meet_seamlessly_and_feather_underground(self, tmp_path: Path) -> None:
        build, corridors = self._make_base_build_and_corridors()
        spec = HybridSpec(tile_squares=256, corridor_cell_size_m=1.0, rtin_max_error_m=0.08)

        # Tile (0, 0) covers x=[0, 256], Tile (1, 0) covers x=[256, 512]
        t0 = build_corridor_rtin_tile(
            0,
            0,
            un_depressed_build=build,
            all_solved_corridors=corridors,
            corridor_roads_only=corridors,
            spec=spec,
        )
        t1 = build_corridor_rtin_tile(
            1,
            0,
            un_depressed_build=build,
            all_solved_corridors=corridors,
            corridor_roads_only=corridors,
            spec=spec,
        )
        assert t0 is not None and t1 is not None
        assert t0.triangle_count < t0.full_triangle_count

        # Shared seam at x = 256.0 must have identical world heights and normals
        seam_0 = {
            (round(float(t0.origin_x + t0.positions[i, 0]), 3), round(float(t0.origin_y + t0.positions[i, 1]), 3)): (
                float(t0.base_z + t0.positions[i, 2]),
                tuple(np.round(t0.normals[i], 5)),
            )
            for i in range(t0.positions.shape[0])
            if math.isclose(t0.origin_x + t0.positions[i, 0], 256.0, abs_tol=1e-6)
        }
        seam_1 = {
            (round(float(t1.origin_x + t1.positions[i, 0]), 3), round(float(t1.origin_y + t1.positions[i, 1]), 3)): (
                float(t1.base_z + t1.positions[i, 2]),
                tuple(np.round(t1.normals[i], 5)),
            )
            for i in range(t1.positions.shape[0])
            if math.isclose(t1.origin_x + t1.positions[i, 0], 256.0, abs_tol=1e-6)
        }
        assert len(seam_0) >= 10
        assert set(seam_0) == set(seam_1)
        for pt in seam_0:
            assert seam_0[pt][0] == pytest.approx(seam_1[pt][0], abs=1e-6)
            assert seam_0[pt][1] == pytest.approx(seam_1[pt][1], abs=1e-5)

        # Outer feathered edge must dip below un-depressed base terrain (zero floating gap)
        # Road is at y=256, hw=5, depress_outer=12 -> 17m, feather_end=16 -> 21m (y=235 or y=277)
        edge_zs = [
            float(t0.base_z + t0.positions[i, 2])
            for i in range(t0.positions.shape[0])
            if math.isclose(t0.origin_y + t0.positions[i, 1], 235.0, abs_tol=0.6)
            and 50.0 <= t0.origin_x + t0.positions[i, 0] <= 200.0
        ]
        assert edge_zs
        # Base terrain at y=235, x~100 is ~104.35m; feathered lip should be ~0.4-0.6m below it
        assert max(edge_zs) < 107.0

        # DAE export & material JSON check
        dae_path = tmp_path / "corridor_0_0.dae"
        nbytes = write_corridor_tile_dae(dae_path, t0)
        assert nbytes > 500
        root = ET.fromstring(dae_path.read_text(encoding="utf-8"))
        ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
        assert root.find(".//c:up_axis", ns).text == "Z_UP"  # type: ignore[union-attr]
        nodes = [el.attrib.get("name") for el in root.findall(".//c:node", ns)]
        assert "ground_a" in nodes
        assert not any(n and n[-1].isdigit() and n not in {"base00", "start01"} for n in nodes)

        mat_path = write_corridor_materials(tmp_path, "test_hybrid", DEFAULT_MATERIAL_ORDER, base_tex_size=64)
        mats = json.loads(mat_path.read_text(encoding="utf-8"))
        for mat_key in t0.triangles_by_material:
            assert mat_key in mats
            assert mats[mat_key]["Stages"][0]["baseColorMap"].endswith(".color.png")

    def test_decal_projection_sets_over_objects_only_on_corridor(self) -> None:
        build, corridors = self._make_base_build_and_corridors()
        spec = HybridSpec(tile_squares=256, corridor_cell_size_m=1.0, rtin_max_error_m=0.08)
        t0 = build_corridor_rtin_tile(
            0,
            0,
            un_depressed_build=build,
            all_solved_corridors=corridors,
            corridor_roads_only=corridors,
            spec=spec,
        )
        assert t0 is not None
        idx = HybridSurfaceIndex(
            tile_extent_m=256.0,
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=1.0,
            tiles_by_coord={(0, 0): t0},
        )
        tree = SceneTree(level_id="test_hybrid")
        tree.add_object(
            ("level_objects", "road_network"),
            {
                "class": "DecalRoad",
                "name": "on_corr",
                "overObjects": False,
                "decalBias": 0.0015,
                "nodes": [[50.0, 256.0, 99.0, 10.0], [150.0, 256.0, 99.0, 10.0]],
            },
        )
        tree.add_object(
            ("level_objects", "road_network"),
            {
                "class": "DecalRoad",
                "name": "off_corr",
                "overObjects": False,
                "decalBias": 0.0015,
                "nodes": [[50.0, 40.0, 101.4, 6.0], [150.0, 40.0, 103.4, 6.0]],
            },
        )
        stats = apply_hybrid_decal_projection(tree, idx)
        assert stats["projected_decals"] == 1
        objs = {o["name"]: o for o in tree.objects[("level_objects", "road_network")]}
        assert objs["on_corr"]["overObjects"] is True
        assert objs["on_corr"]["decalBias"] >= 0.002
        assert objs["on_corr"]["nodes"][0][2] > 104.0
        assert objs["off_corr"]["overObjects"] is False
        assert objs["off_corr"]["nodes"][0][2] == pytest.approx(101.4)


class TestOfflineHybridEndToEnd:
    def test_offline_hybrid_build_and_validate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg_data = {
            "region": {
                "id": "ivanovo_oblast",
                "title": "Ivanovo Oblast",
                "bbox": {"west": 40.90, "south": 56.88, "east": 41.30, "north": 57.02},
                "projection": {
                    "central_meridian": 41.10,
                    "latitude_of_origin": 57.00,
                    "origin": [40.97393, 56.99987],
                },
            },
            "terrain": {"source": "copernicus_glo30", "resolution_m": 30},
            "sectors": {"size_m": 500},
            "roads": {"simplify_tolerance_m": 0.5, "junction_snap_m": 0.5, "corridor_half_width_m": 12.0, "max_grade": 0.08},
            "level": {
                "id": "test_hybrid_lvl",
                "title": "Test Hybrid Level",
                "center": [41.100, 56.925],
                "spawn": [41.098, 56.925],
                "terrain": {"grid_size": 128, "square_size_m": 4.0},
                "ground_margin_m": 8.0,
            },
            "buildings": {"enabled": True},
            "vegetation": {"enabled": True},
            "water": {"enabled": True},
            "railways": {"enabled": False},
            "bridges": {"enabled": True},
            "cache": {"dir": str(tmp_path / "cache")},
            "build": {"dir": str(tmp_path / "build")},
        }
        cfg = ProjectConfig.from_mapping(cfg_data, root=tmp_path)
        dem_path = _write_synthetic_dem(tmp_path / "dem.tif", (40.95, 56.89, 41.25, 57.00))
        origin_x, origin_y = cfg.level_terrain_origin(cfg.level)
        osm_data = _synthetic_hybrid_osm(cfg.projection, origin_x, origin_y, cfg.level.grid.extent_m)  # type: ignore[union-attr]

        monkeypatch.setattr("ivanovo2beam.sectors.hybrid.resolve_dem_for_bbox", lambda *a, **kw: dem_path)
        monkeypatch.setattr(
            "ivanovo2beam.sectors.hybrid.fetch_level_osm",
            lambda *a, **kw: (osm_data, {"source": "synthetic", "tiles_requested": 1, "tiles_succeeded": 1, "tiles_failed": 0}),
        )

        spec = HybridSpec(
            corridor_cell_size_m=1.0,
            tile_squares=256,
            rtin_max_error_m=0.08,
            base_tex_size=64,
            groundcover_enabled=True,
            max_buildings=50,
            max_trees=500,
        )
        result = build_hybrid_level(cfg, spec, make_zip=False)
        report = validate_hybrid_level(result.level_dir, cfg)
        assert report.ok, report.describe()

        assert result.stats["corridor_chunks"] >= 1
        assert result.stats["corridor_triangles"] > 0
        assert result.stats["depressed_cells"] > 0
        assert result.stats["export"]["projected_decals"] >= 1
        assert result.stats["bridges"]["bridges"] == 1

        tf = read_ter(result.level_dir / "theTerrain.ter")
        assert tf.size == 128

        corr_daes = list((result.level_dir / "art" / "shapes" / "corridor").glob("corridor_*.dae"))
        assert len(corr_daes) == result.stats["corridor_chunks"]
