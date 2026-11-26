"""Tests for level metadata, bundled real previews, and BeamNG level cleanup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ivanovo2beam.beamng.level import (
    BUNDLED_PREVIEWS_DIR,
    build_info_json,
    write_level_files,
)
from ivanovo2beam.beamng.preview import generate_level_preview_png
from ivanovo2beam.config import REPO_ROOT, ProjectConfig
from scripts.cleanup_beamng_levels import cleanup_beamng_levels, is_obsolete_level_id


class TestLevelMetadataAndPreviews:
    def test_ivanovo_shuya_v2_config_has_human_friendly_title(self) -> None:
        cfg = ProjectConfig.load(REPO_ROOT / "configs" / "ivanovo_shuya_v2.yaml")
        assert cfg.level is not None
        assert cfg.level.id == "ivanovo_shuya_v2"
        assert "Ivanovo" in cfg.level.title
        assert "Shuya" in cfg.level.title
        assert "Р152" in cfg.level.title

    def test_curated_info_json_enriches_generic_build_description(self) -> None:
        info = build_info_json(
            "ivanovo_shuya_v2",
            "Ivanovo - Kokhma - Shuya Corridor 1:1 (Р152)",
            "1:1 scale map of Ivanovo Oblast generated from OpenStreetMap and Copernicus DEM. Build extent 32.76 km.",
            extent_m=32764.0,
        )
        assert info["title"] == "Ivanovo - Kokhma - Shuya Corridor 1:1 (Р152)"
        assert "32.8 x 32.8 km" in info["description"]
        assert "82 bridges" in info["description"]
        assert info["country"] == "Russia"
        assert info["previews"] == ["ivanovo_shuya_v2_preview.png"]
        assert info["spawnPoints"][0]["preview"] == "ivanovo_shuya_v2_preview.png"

    def test_custom_test_description_is_preserved(self) -> None:
        info = build_info_json(
            "ivanovo_shuya_v2",
            "Custom Title",
            "Custom explicit description",
            extent_m=32764.0,
        )
        assert info["title"] == "Custom Title"
        assert info["description"] == "Custom explicit description"

    def test_write_level_files_installs_bundled_real_previews(self, tmp_path: Path) -> None:
        for level_id in ("ivanovo_shuya_v2", "ivanovo_golden_sector"):
            src_png = BUNDLED_PREVIEWS_DIR / f"{level_id}_preview.png"
            assert src_png.is_file()
            assert src_png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

            lvl_dir = tmp_path / level_id
            info = build_info_json(level_id, level_id, "", extent_m=4095.0)
            written = write_level_files(lvl_dir, {"info.json": json.dumps(info)})

            named_preview = lvl_dir / f"{level_id}_preview.png"
            alias_preview = lvl_dir / "preview.png"
            assert named_preview.is_file()
            assert alias_preview.is_file()
            assert named_preview in written
            assert alias_preview in written
            assert named_preview.read_bytes() == src_png.read_bytes()

            # generate_level_preview_png must preserve the real screenshot instead of overwriting it.
            out = generate_level_preview_png(lvl_dir, level_id)
            assert out == named_preview
            assert named_preview.read_bytes() == src_png.read_bytes()


class TestCleanupBeamngLevels:
    @pytest.mark.parametrize(
        ("level_id", "expected"),
        [
            ("ivanovo_mvp", True),
            ("ivanovo_mvp_old", True),
            ("ivanovo_shuya", True),
            ("ivanovo_shuya_v1", True),
            ("mapng_golden_sector", True),
            ("mapng_golden_sector_old", True),
            ("ivanovo_dual_terrain_exp", True),
            ("ivanovo_hybrid_corridor", True),
            ("ivanovo_single_8192_2m", True),
            ("ivanovo_shuya_v2", False),
            ("ivanovo_golden_sector", False),
            ("VirtualCity", False),
            ("east_coast_usa", False),
        ],
    )
    def test_obsolete_level_matching(self, level_id: str, expected: bool) -> None:
        assert is_obsolete_level_id(level_id) is expected

    def test_dry_run_and_apply_preserve_canonical_and_third_party(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_dir = tmp_path / "beamng_user"
        for name in (
            "ivanovo_shuya_v2",
            "ivanovo_golden_sector",
            "VirtualCity",
            "ivanovo_mvp",
            "ivanovo_shuya",
            "mapng_golden_sector",
            "ivanovo_dual_terrain_exp",
            "ivanovo_hybrid_corridor",
            "ivanovo_single_8192_2m",
        ):
            d = user_dir / "levels" / name
            d.mkdir(parents=True)
            (d / "info.json").write_text("{}", encoding="utf-8")

        mods_dir = user_dir / "mods"
        mods_dir.mkdir(parents=True)
        for z in ("ivanovo_mvp.zip", "ivanovo_shuya.zip", "mapng_golden_sector.zip", "etk_w_rev.zip"):
            (mods_dir / z).write_bytes(b"zipbytes")

        temp_dir = user_dir / "temp" / "levels" / "ivanovo_mvp"
        temp_dir.mkdir(parents=True)
        (temp_dir / "cache.bin").write_bytes(b"123")

        monkeypatch.setattr("scripts.cleanup_beamng_levels.find_beamng_pids", lambda: [])

        dry = cleanup_beamng_levels(user_dir=user_dir, dry_run=True)
        assert dry["ok"] is True
        assert dry["targets_found"] == 10
        assert (user_dir / "levels" / "ivanovo_mvp").is_dir()

        applied = cleanup_beamng_levels(user_dir=user_dir, dry_run=False)
        assert applied["ok"] is True
        assert len(applied["removed"]) == 10
        assert (user_dir / "levels" / "ivanovo_shuya_v2").is_dir()
        assert (user_dir / "levels" / "ivanovo_golden_sector").is_dir()
        assert (user_dir / "levels" / "VirtualCity").is_dir()
        assert (mods_dir / "etk_w_rev.zip").is_file()
        for obsolete in (
            "ivanovo_mvp",
            "ivanovo_shuya",
            "mapng_golden_sector",
            "ivanovo_dual_terrain_exp",
            "ivanovo_hybrid_corridor",
            "ivanovo_single_8192_2m",
        ):
            assert not (user_dir / "levels" / obsolete).exists()
        for obsolete_zip in ("ivanovo_mvp.zip", "ivanovo_shuya.zip", "mapng_golden_sector.zip"):
            assert not (mods_dir / obsolete_zip).exists()
        assert not temp_dir.exists()
