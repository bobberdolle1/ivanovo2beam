"""Tests for CLI commands (specifically install cleanup semantics)."""

from __future__ import annotations

from pathlib import Path

import yaml

from ivanovo2beam.cli import main


def _write_min_config(tmp_path: Path, level_id: str = "demo_level") -> Path:
    cfg = {
        "region": {
            "id": "demo",
            "bbox": {"west": 40.9, "south": 56.9, "east": 41.2, "north": 57.1},
        },
        "level": {
            "id": level_id,
            "center": [41.0, 57.0],
            "terrain": {"grid_size": 128, "square_size_m": 1.0},
        },
        "build": {"dir": str(tmp_path / "build")},
        "cache": {"dir": str(tmp_path / "cache")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def test_install_force_clears_stale_files(tmp_path: Path) -> None:
    cfg_path = _write_min_config(tmp_path)
    source = tmp_path / "build" / "levels" / "demo_level"
    (source / "forest").mkdir(parents=True)
    (source / "info.json").write_text('{"title":"new"}\n', encoding="utf-8")
    (source / "forest" / "tree_aspen.forest4.json").write_text("{}\n", encoding="utf-8")

    userdata = tmp_path / "userdata"
    target = userdata / "levels" / "demo_level"
    (target / "forest").mkdir(parents=True)
    (target / "main.materials.json").write_text('{"stale": true}\n', encoding="utf-8")
    (target / "forest" / "scots_pine.forest4.json").write_text("{}\n", encoding="utf-8")

    # Without --force, existing target must return non-zero exit code.
    rc_fail = main(["--config", str(cfg_path), "install", "--beamng", str(userdata)])
    assert rc_fail != 0
    assert (target / "main.materials.json").is_file()

    # With --force, stale files are purged and only fresh source files remain.
    rc = main(["--config", str(cfg_path), "--json", "install", "--beamng", str(userdata), "--force"])
    assert rc == 0
    assert (target / "info.json").is_file()
    assert (target / "forest" / "tree_aspen.forest4.json").is_file()
    assert not (target / "main.materials.json").exists()
    assert not (target / "forest" / "scots_pine.forest4.json").exists()
