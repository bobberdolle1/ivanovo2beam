"""Tests for configuration loading and validation.

The most important behaviour here is the terrain extent arithmetic.
``extent_m = (grid_size - 1) * square_size_m`` -- an off-by-one that was
confirmed against a shipped level (automation_test_track uses 4096 vertices at
0.5 m and sits at ``position.x = -1023.75``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ivanovo2beam.config import (
    REPO_ROOT,
    ConfigError,
    LevelSpec,
    ProjectConfig,
    RoadSettings,
    TerrainGridSpec,
)

CONFIGS = REPO_ROOT / "configs"


class TestTerrainGridSpec:
    def test_extent_uses_n_minus_one(self) -> None:
        grid = TerrainGridSpec(grid_size=4096, square_size_m=1.25)
        assert grid.extent_m == pytest.approx(4095 * 1.25)
        assert grid.extent_m == pytest.approx(5118.75)

    def test_matches_shipped_automation_test_track(self) -> None:
        """4096 vertices at 0.5 m spans 2047.5 m, matching the shipped level."""
        grid = TerrainGridSpec(grid_size=4096, square_size_m=0.5)
        assert grid.extent_m == pytest.approx(2047.5)

    def test_matches_shipped_hirochi_raceway(self) -> None:
        """2048 vertices at 0.75 m spans 1535.25 m, half of which is 767.625,
        exactly the shipped level's terrain position."""
        grid = TerrainGridSpec(grid_size=2048, square_size_m=0.75)
        assert grid.extent_m / 2 == pytest.approx(767.625)

    @pytest.mark.parametrize("size", [64, 127, 8193, 10000])
    def test_rejects_out_of_range_size(self, size: int) -> None:
        with pytest.raises(ConfigError, match="outside BeamNG's supported range"):
            TerrainGridSpec(grid_size=size, square_size_m=1.0)

    @pytest.mark.parametrize("size", [200, 300, 1000, 3000])
    def test_rejects_non_power_of_two(self, size: int) -> None:
        with pytest.raises(ConfigError, match="power of two"):
            TerrainGridSpec(grid_size=size, square_size_m=1.0)

    @pytest.mark.parametrize("square", [0, -1, float("nan"), float("inf")])
    def test_rejects_bad_square_size(self, square: float) -> None:
        with pytest.raises(ConfigError, match="square_size_m must be positive"):
            TerrainGridSpec(grid_size=2048, square_size_m=square)

    def test_accepts_the_extremes(self) -> None:
        assert TerrainGridSpec(grid_size=128, square_size_m=0.25).extent_m > 0
        assert TerrainGridSpec(grid_size=8192, square_size_m=30.0).extent_m == pytest.approx(245_730.0)

    def test_from_config_derives_square_size_from_extent(self) -> None:
        grid = TerrainGridSpec.from_config({"grid_size": 2048, "extent_m": 2047.0})
        assert grid.square_size_m == pytest.approx(1.0)

    def test_from_config_requires_a_size(self) -> None:
        with pytest.raises(ConfigError, match="square_size_m or extent_m"):
            TerrainGridSpec.from_config({"grid_size": 2048})

    def test_from_config_requires_grid_size(self) -> None:
        with pytest.raises(ConfigError, match="grid_size is required"):
            TerrainGridSpec.from_config({"square_size_m": 1.0})

    def test_vertical_step(self) -> None:
        grid = TerrainGridSpec(grid_size=4096, square_size_m=1.25)
        assert grid.vertical_step_for(58.367) == pytest.approx(58.367 / 65536, rel=1e-12)


class TestLevelSpec:
    def test_rejects_unsafe_level_id(self) -> None:
        with pytest.raises(ConfigError, match="folder name"):
            LevelSpec(
                id="my level!",
                title="x",
                center_lon=41.0,
                center_lat=57.0,
                grid=TerrainGridSpec(2048, 1.0),
            )

    def test_accepts_underscores_and_digits(self) -> None:
        spec = LevelSpec(
            id="ivanovo_mvp_2",
            title="x",
            center_lon=41.0,
            center_lat=57.0,
            grid=TerrainGridSpec(2048, 1.0),
        )
        assert spec.id == "ivanovo_mvp_2"

    @pytest.mark.parametrize(("lon", "lat"), [(200.0, 57.0), (41.0, 95.0)])
    def test_rejects_out_of_range_centre(self, lon: float, lat: float) -> None:
        with pytest.raises(ConfigError, match="out of range"):
            LevelSpec(
                id="x",
                title="x",
                center_lon=lon,
                center_lat=lat,
                grid=TerrainGridSpec(2048, 1.0),
            )

    def test_from_config_requires_centre(self) -> None:
        with pytest.raises(ConfigError, match=r"center \[lon, lat\] is required"):
            LevelSpec.from_config({"id": "x", "terrain": {"grid_size": 2048, "square_size_m": 1}})


class TestRoadSettings:
    def test_defaults_cover_the_required_classes(self) -> None:
        settings = RoadSettings()
        for klass in (
            "motorway",
            "trunk",
            "primary",
            "secondary",
            "tertiary",
            "residential",
            "service",
            "unclassified",
            "living_street",
            "track",
        ):
            assert settings.spec_for(klass) is not None, klass

    def test_unknown_class_returns_none(self) -> None:
        assert RoadSettings().spec_for("monorail") is None
        assert RoadSettings().spec_for(None) is None

    def test_lookup_is_case_insensitive_and_stripped(self) -> None:
        assert RoadSettings().spec_for("  PRIMARY ") is not None

    def test_config_overrides_defaults_but_keeps_the_rest(self) -> None:
        settings = RoadSettings.from_config({"classes": {"primary": {"width_m": 12.0}}})
        spec = settings.spec_for("primary")
        assert spec is not None
        assert spec.width_m == 12.0
        # Untouched fields survive the override.
        assert spec.priority == 80
        assert spec.surface == "asphalt"

    def test_config_can_add_a_new_class(self) -> None:
        settings = RoadSettings.from_config(
            {"classes": {"raceway": {"width_m": 20.0, "lanes": 2, "surface": "asphalt"}}}
        )
        spec = settings.spec_for("raceway")
        assert spec is not None
        assert spec.width_m == 20.0


class TestProjectConfig:
    def test_loads_the_region_config(self) -> None:
        config = ProjectConfig.load(CONFIGS / "ivanovo_oblast.yaml")
        assert config.region_id == "ivanovo_oblast"
        assert config.bbox.west == 39.0
        assert config.level is None

    @pytest.mark.parametrize("name", ["ivanovo_oblast.yaml", "ivanovo_mvp.yaml", "test_sector.yaml"])
    def test_every_shipped_config_loads(self, name: str) -> None:
        config = ProjectConfig.load(CONFIGS / name)
        assert config.region_id
        assert config.projection is not None
        assert config.sector_size_m > 0

    def test_level_config_has_a_level(self) -> None:
        config = ProjectConfig.load(CONFIGS / "ivanovo_mvp.yaml")
        assert config.level is not None
        assert config.level.id == "ivanovo_mvp"
        assert config.level.grid.grid_size == 4096

    def test_terrain_origin_is_centred_on_the_level(self) -> None:
        config = ProjectConfig.load(CONFIGS / "ivanovo_mvp.yaml")
        level = config.level
        assert level is not None
        cx, cy = config.level_grid_center()
        ox, oy = config.level_terrain_origin()
        half = level.grid.extent_m / 2
        assert ox == pytest.approx(cx - half)
        assert oy == pytest.approx(cy - half)
        # The world origin is Ivanovo city centre, which is the level centre.
        assert cx == pytest.approx(0.0, abs=1e-6)
        assert cy == pytest.approx(0.0, abs=1e-6)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="config file not found"):
            ProjectConfig.load(tmp_path / "nope.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("region: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid YAML"):
            ProjectConfig.load(bad)

    def test_missing_region_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.safe_dump({"terrain": {"source": "x"}}), encoding="utf-8")
        with pytest.raises(ConfigError, match="missing 'region' mapping"):
            ProjectConfig.load(bad)

    def test_missing_bbox_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.safe_dump({"region": {"id": "x"}}), encoding="utf-8")
        with pytest.raises(ConfigError, match=r"region\.bbox is required"):
            ProjectConfig.load(bad)

    def test_sector_size_must_be_positive(self) -> None:
        raw = {
            "region": {
                "id": "x",
                "bbox": {"west": 39.0, "south": 56.1, "east": 43.2, "north": 57.8},
            },
            "sectors": {"size_m": 0},
        }
        with pytest.raises(ConfigError, match=r"sectors\.size_m must be positive"):
            ProjectConfig.from_mapping(raw)

    def test_relative_cache_and_build_dirs_resolve_against_root(self) -> None:
        config = ProjectConfig.load(CONFIGS / "ivanovo_mvp.yaml")
        assert config.cache_dir == REPO_ROOT / ".cache"
        assert config.build_dir == REPO_ROOT / "build"

    def test_level_helpers_raise_without_a_level(self) -> None:
        config = ProjectConfig.load(CONFIGS / "ivanovo_oblast.yaml")
        with pytest.raises(ConfigError, match="no level configured"):
            config.level_grid_center()
        with pytest.raises(ConfigError, match="no level configured"):
            config.level_terrain_origin()

    def test_projection_of_shipped_config_is_accurate(self) -> None:
        config = ProjectConfig.load(CONFIGS / "ivanovo_oblast.yaml")
        report = config.projection.measure_scale_error_over(config.bbox, samples=5)
        assert report.error_metres_per_100km < 15.0
