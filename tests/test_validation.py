"""Tests for structural level validation (terrain HOLE separation & extra materials)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np

from ivanovo2beam.beamng.ter import HOLE, TerrainFile, write_ter
from ivanovo2beam.validation.level import validate_level


def _build_minimal_level(
    level_dir: Path,
    *,
    layers: np.ndarray | None = None,
    materials: tuple[str, ...] = ("Grass", "Asphalt"),
) -> None:
    size = 128
    heights = np.full((size, size), 100.0, dtype=np.float32)
    if layers is None:
        layers = np.zeros((size, size), dtype=np.uint8)
    ter = TerrainFile.from_elevations(heights, layers, materials, base_z=90.0, max_height=50.0)
    level_dir.mkdir(parents=True, exist_ok=True)
    write_ter(level_dir / "theTerrain.ter", ter)
    (level_dir / "theTerrain.terrain.json").write_text(
        json.dumps(
            {
                "version": 9,
                "size": size,
                "heightMapSize": size * size,
                "layerMapSize": size * size,
                "materials": list(materials),
            }
        ),
        encoding="utf-8",
    )
    (level_dir / "info.json").write_text(json.dumps({"title": "test"}), encoding="utf-8")
    (level_dir / "main" / "MissionGroup").mkdir(parents=True, exist_ok=True)
    (level_dir / "main" / "items.level.json").write_text(
        json.dumps({"name": "MissionGroup", "class": "SimGroup"}) + "\n",
        encoding="utf-8",
    )
    (level_dir / "main" / "MissionGroup" / "items.level.json").write_text(
        json.dumps({"name": "level_objects", "class": "SimGroup", "__parent": "MissionGroup"}) + "\n",
        encoding="utf-8",
    )
    (level_dir / "art" / "terrains").mkdir(parents=True, exist_ok=True)
    (level_dir / "art" / "terrains" / "main.materials.json").write_text(
        json.dumps(
            {
                "Grass": {
                    "class": "TerrainMaterial",
                    "baseColorBaseTex": "/assets/materials/terrain/grass/t_grass_01_b.png",
                },
                "texSet": {"class": "TerrainMaterialTextureSet"},
            }
        ),
        encoding="utf-8",
    )


def test_terrain_hole_does_not_mask_out_of_range_layer_index(tmp_path: Path) -> None:
    # Valid layers + HOLE(255) must pass.
    ok_layers = np.zeros((128, 128), dtype=np.uint8)
    ok_layers[0, 0] = 1
    ok_layers[1, 1] = HOLE
    ok_dir = tmp_path / "ok_level"
    _build_minimal_level(ok_dir, layers=ok_layers)
    ok_report = validate_level(ok_dir)
    layer_check = next(c for c in ok_report.checks if c.name == "terrain_layers")
    assert layer_check.ok
    assert "HOLE=255" in layer_check.detail

    # Out-of-range index 15 alongside HOLE(255) must fail.
    bad_layers = np.zeros((128, 128), dtype=np.uint8)
    bad_layers[0, 0] = 15
    bad_layers[1, 1] = HOLE
    bad_dir = tmp_path / "bad_level"
    _build_minimal_level(bad_dir, layers=bad_layers)
    bad_report = validate_level(bad_dir)
    bad_check = next(c for c in bad_report.checks if c.name == "terrain_layers")
    assert not bad_check.ok
    assert "15" in bad_check.detail


def test_validates_road_forest_and_shape_materials_and_assets(tmp_path: Path) -> None:
    lvl = tmp_path / "lvl"
    _build_minimal_level(lvl)

    # Create a fake BeamNG assets zip containing terrain + road + forest + bridge textures.
    install = tmp_path / "beamng"
    assets_dir = install / "content" / "assets"
    assets_dir.mkdir(parents=True)
    with zipfile.ZipFile(assets_dir / "pack.zip", "w") as zf:
        for tex in (
            "assets/materials/terrain/grass/t_grass_01_b.png",
            "assets/materials/decalroad/asphalt/road.color.dds",
            "assets/materials/tree/birch/leaves.color.dds",
            "assets/materials/tileable/concrete/bridge.color.dds",
        ):
            zf.writestr(tex, b"dds")

    (lvl / "art" / "road").mkdir(parents=True)
    (lvl / "art" / "road" / "main.materials.json").write_text(
        json.dumps(
            {
                "road_asphalt_2lane": {
                    "class": "Material",
                    "persistentId": "11111111-1111-1111-1111-111111111111",
                    "Stages": [{"baseColorMap": "/assets/materials/decalroad/asphalt/road.color.dds"}],
                }
            }
        ),
        encoding="utf-8",
    )
    (lvl / "art" / "forest").mkdir(parents=True)
    (lvl / "art" / "forest" / "main.materials.json").write_text(
        json.dumps(
            {
                "birch_leaves": {
                    "class": "Material",
                    "persistentId": "22222222-2222-2222-2222-222222222222",
                    "Stages": [{"baseColorMap": "/assets/materials/tree/birch/leaves.color.dds"}],
                }
            }
        ),
        encoding="utf-8",
    )
    (lvl / "art" / "shapes" / "bridges").mkdir(parents=True)
    (lvl / "art" / "shapes" / "bridges" / "main.materials.json").write_text(
        json.dumps(
            {
                "bridge_deck": {
                    "class": "Material",
                    "persistentId": "33333333-3333-3333-3333-333333333333",
                    "Stages": [{"baseColorMap": "/assets/materials/tileable/concrete/bridge.color.dds"}],
                }
            }
        ),
        encoding="utf-8",
    )

    ok_report = validate_level(lvl, beamng_install=install)
    assert ok_report.ok
    extra = next(c for c in ok_report.checks if c.name == "extra_materials_json")
    assert extra.ok
    assets = next(c for c in ok_report.checks if c.name == "asset_paths")
    assert assets.ok
    assert "4 referenced textures, 0 missing" in assets.detail

    # Introduce duplicate persistentId and missing texture in road materials -> must fail.
    (lvl / "art" / "road" / "main.materials.json").write_text(
        json.dumps(
            {
                "road_a": {
                    "class": "Material",
                    "persistentId": "dup-id",
                    "Stages": [{"baseColorMap": "/assets/materials/decalroad/asphalt/missing.color.dds"}],
                },
                "road_b": {
                    "class": "Material",
                    "persistentId": "dup-id",
                    "Stages": [{"baseColorMap": "/assets/materials/decalroad/asphalt/road.color.dds"}],
                },
            }
        ),
        encoding="utf-8",
    )
    bad_report = validate_level(lvl, beamng_install=install)
    assert not bad_report.ok
    assert not next(c for c in bad_report.checks if c.name == "extra_materials_json").ok
    assert not next(c for c in bad_report.checks if c.name == "asset_paths").ok
