"""Tests for the terrain material library.

The library is what makes a generated level render at all, and it works by
referencing base-game textures by path. A single wrong directory name produces a
magenta or black terrain in game, so the path construction is pinned here.
"""

from __future__ import annotations

import json
import struct
import uuid

import pytest

from ivanovo2beam.beamng.materials import (
    DEFAULT_BASE_TEX_SIZE,
    DEFAULT_MATERIAL_ORDER,
    MATERIAL_LIBRARY,
    VALID_GROUNDMODELS,
    TerrainMaterialSpec,
    asset_paths_by_material,
    build_materials_json,
    highway_to_material,
    referenced_asset_paths,
    resolve_materials,
    surface_to_material,
    write_terrain_base_textures,
)


class TestSpec:
    def test_rejects_unknown_groundmodel(self) -> None:
        with pytest.raises(ValueError, match="not a known BeamNG ground model"):
            TerrainMaterialSpec(
                key="X",
                title="x",
                groundmodel="NOT_A_GROUNDMODEL",
                texture_set="grass/t_grass_01",
                texture_prefix="t_grass_01",
                macro_set="grass/macro_grass",
                macro_prefix="t_macro_grass",
            )

    def test_channel_paths_follow_the_convention(self) -> None:
        spec = MATERIAL_LIBRARY["Grass"]
        paths = spec.channel_paths()
        assert paths["b"] == "/assets/materials/terrain/grass/t_grass_01/t_grass_01_b.png"
        assert paths["nm"].endswith("_nm.png")
        assert paths["r"].endswith("_r.png")
        assert paths["ao"].endswith("_ao.png")
        assert paths["h"].endswith("_h.png")
        assert len(paths) == 5

    def test_persistent_id_is_deterministic_and_unique(self) -> None:
        ids = {key: spec.persistent_id() for key, spec in MATERIAL_LIBRARY.items()}
        assert len(set(ids.values())) == len(ids), "material UUIDs must be unique"
        # Stable across calls.
        assert MATERIAL_LIBRARY["Grass"].persistent_id() == ids["Grass"]
        # A valid UUID that the game will accept.
        assert uuid.UUID(ids["Grass"])

    def test_every_library_material_declares_the_required_slots(self) -> None:
        for key, spec in MATERIAL_LIBRARY.items():
            assert spec.groundmodel in VALID_GROUNDMODELS, key
            assert spec.texture_set and spec.texture_prefix, key
            assert spec.macro_set and spec.macro_prefix, key
            assert len(spec.all_asset_paths()) == 10, key


class TestLibraryCoverage:
    def test_covers_the_surfaces_the_brief_requires(self) -> None:
        titles = " ".join(s.title.lower() for s in MATERIAL_LIBRARY.values())
        for surface in ("asphalt", "gravel", "dirt", "concrete", "grass", "field", "forest"):
            assert surface in titles, surface

    def test_default_order_is_a_subset_of_the_library(self) -> None:
        for key in DEFAULT_MATERIAL_ORDER:
            assert key in MATERIAL_LIBRARY, key

    def test_default_order_has_no_duplicates(self) -> None:
        assert len(set(DEFAULT_MATERIAL_ORDER)) == len(DEFAULT_MATERIAL_ORDER)

    def test_background_material_is_index_zero(self) -> None:
        """Anything unpainted stays grass; layer index 0 must be the default fill."""
        assert DEFAULT_MATERIAL_ORDER[0] == "Grass"

    def test_groundmodel_is_spelled_as_the_engine_expects(self) -> None:
        """The engine matches ground model names case-sensitively."""
        for key, spec in MATERIAL_LIBRARY.items():
            assert spec.groundmodel == spec.groundmodel.upper(), key


class TestResolve:
    def test_resolve_preserves_order(self) -> None:
        specs = resolve_materials(["Gravel", "Grass", "Asphalt"])
        assert [s.key for s in specs] == ["Gravel", "Grass", "Asphalt"]

    def test_unknown_key_raises(self) -> None:
        with pytest.raises(KeyError):
            resolve_materials(["Nope"])


class TestSurfaceMapping:
    @pytest.mark.parametrize(
        ("surface", "expected"),
        [
            ("asphalt", "Asphalt"),
            ("paved", "Asphalt"),
            ("gravel", "Gravel"),
            ("compacted", "Gravel"),
            ("dirt", "Dirt"),
            ("unpaved", "Dirt"),
            ("concrete", "Concrete"),
            ("sand", "Sand"),
            ("rock", "Rock"),
        ],
    )
    def test_known_surfaces(self, surface: str, expected: str) -> None:
        assert surface_to_material(surface) == expected

    def test_untagged_falls_back_to_asphalt(self) -> None:
        assert surface_to_material(None) == "Asphalt"
        assert surface_to_material("") == "Asphalt"

    def test_unknown_surface_falls_back_to_asphalt(self) -> None:
        assert surface_to_material("moon_dust") == "Asphalt"

    def test_lookup_is_case_insensitive(self) -> None:
        assert surface_to_material("  GRAVEL  ") == "Gravel"

    def test_every_mapped_material_exists_in_the_library(self) -> None:
        for spec in MATERIAL_LIBRARY.values():
            for surface in spec.osm_surface:
                assert surface_to_material(surface) in MATERIAL_LIBRARY

    @pytest.mark.parametrize(
        ("highway", "surface", "expected"),
        [
            ("primary", None, "Asphalt"),
            ("track", None, "Gravel"),
            ("path", None, "Gravel"),
            ("track", "asphalt", "Asphalt"),
            ("residential", "dirt", "Dirt"),
        ],
    )
    def test_highway_to_material(self, highway: str, surface: str | None, expected: str) -> None:
        assert highway_to_material(highway, surface) == expected


class TestBuildMaterialsJson:
    def test_structure(self) -> None:
        doc = build_materials_json("test_level")
        assert "test_levelTerrainMaterialTextureSet" in doc
        texture_set = doc["test_levelTerrainMaterialTextureSet"]
        assert texture_set["class"] == "TerrainMaterialTextureSet"
        assert texture_set["name"] == "test_levelTerrainMaterialTextureSet"
        assert isinstance(texture_set["baseTexSize"], list)

        materials = {k: v for k, v in doc.items() if v.get("class") == "TerrainMaterial"}
        assert len(materials) == len(DEFAULT_MATERIAL_ORDER)

    def test_is_json_serialisable(self) -> None:
        json.dumps(build_materials_json("test_level"))

    def test_every_material_has_the_fields_the_engine_reads(self) -> None:
        doc = build_materials_json("test_level")
        required = {
            "internalName",
            "class",
            "persistentId",
            "baseColorBaseTex",
            "baseColorBaseTexSize",
            "normalBaseTex",
            "normalBaseTexSize",
            "roughnessBaseTex",
            "roughnessBaseTexSize",
            "aoBaseTex",
            "aoBaseTexSize",
            "heightBaseTex",
            "heightBaseTexSize",
            "baseColorDetailTex",
            "baseColorDetailTexSize",
            "baseColorDetailStrength",
            "normalDetailTex",
            "normalDetailTexSize",
            "normalDetailStrength",
            "roughnessDetailTex",
            "roughnessDetailTexSize",
            "roughnessDetailStrength",
            "aoDetailTex",
            "aoDetailTexSize",
            "heightDetailTex",
            "heightDetailTexSize",
            "detailDistances",
            "baseColorMacroTex",
            "baseColorMacroTexSize",
            "baseColorMacroStrength",
            "normalMacroTex",
            "normalMacroTexSize",
            "normalMacroStrength",
            "roughnessMacroTex",
            "roughnessMacroTexSize",
            "roughnessMacroStrength",
            "aoMacroTex",
            "aoMacroTexSize",
            "heightMacroTex",
            "heightMacroTexSize",
            "macroDistances",
            "groundmodelName",
        }
        for key, material in doc.items():
            if material.get("class") != "TerrainMaterial":
                continue
            missing = required - set(material)
            assert not missing, f"{key} is missing {sorted(missing)}"
            assert material["baseColorDetailTexSize"] < material["baseColorMacroTexSize"] < material["baseColorBaseTexSize"]

    def test_all_texture_paths_are_absolute_asset_paths(self) -> None:
        doc = build_materials_json("test_level")
        for key, material in doc.items():
            if material.get("class") != "TerrainMaterial":
                continue
            for field, value in material.items():
                if field.endswith("Tex"):
                    assert value.startswith("/"), f"{key}.{field} = {value!r}"

    def test_no_reference_to_another_level(self) -> None:
        """Referencing base-game *shared* assets or the current level's own
        base maps is valid; referencing another level's local art must not happen."""
        doc = build_materials_json("test_level")
        for key, material in doc.items():
            if material.get("class") != "TerrainMaterial":
                continue
            for field, value in material.items():
                if field.endswith("Tex") and "/levels/" in value:
                    assert value.startswith("/levels/test_level/"), f"{key}.{field} = {value!r}"
                if field.endswith("DetailTex") or field.endswith("MacroTex"):
                    assert value.startswith("/assets/materials/terrain/"), f"{key}.{field} = {value!r}"

    def test_write_terrain_base_textures_creates_summer_meadow_maps(self, tmp_path) -> None:
        from ivanovo2beam.beamng.materials import _tileable_color_patch

        written = write_terrain_base_textures(tmp_path, size=64)
        assert written
        assert all(p.is_file() for p in written)
        grass_png = (tmp_path / "art" / "terrains" / "t_terrain_base_grass_b.png").read_bytes()
        assert grass_png.startswith(b"\x89PNG\r\n\x1a\n")
        grass_img = _tileable_color_patch(MATERIAL_LIBRARY["Grass"].base_color, "Grass", size=64)
        mean_rgb = grass_img.mean(axis=(0, 1))
        assert mean_rgb[1] > mean_rgb[0] and mean_rgb[1] > mean_rgb[2], f"Grass must be green, got {mean_rgb}"
        # Every generated /levels/ path in main.materials.json must exist on disk.
        doc = build_materials_json("test_level")
        for material in doc.values():
            if not isinstance(material, dict) or material.get("class") != "TerrainMaterial":
                continue
            for field, value in material.items():
                if field.endswith("Tex") and isinstance(value, str) and value.startswith("/levels/test_level/"):
                    rel = value.removeprefix("/levels/test_level/")
                    assert (tmp_path / rel).is_file(), value

    def test_write_terrain_base_textures_pixel_size_equals_basetexsize(self, tmp_path) -> None:
        """Every generated t_terrain_base_* PNG must have identical (size, size) dimensions
        matching TerrainMaterialTextureSet.baseTexSize to avoid BeamNG TerrainCellMaterial errors."""
        doc = build_materials_json("test_level")
        texture_set = doc["test_levelTerrainMaterialTextureSet"]
        assert texture_set["baseTexSize"] == [DEFAULT_BASE_TEX_SIZE, DEFAULT_BASE_TEX_SIZE]

        written = write_terrain_base_textures(tmp_path)
        assert written
        for p in written:
            assert p.is_file()
            data = p.read_bytes()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            w, h = struct.unpack(">II", data[16:24])
            assert (w, h) == (DEFAULT_BASE_TEX_SIZE, DEFAULT_BASE_TEX_SIZE), (
                f"{p.name} has dimensions ({w}, {h}), expected ({DEFAULT_BASE_TEX_SIZE}, {DEFAULT_BASE_TEX_SIZE})"
            )

    def test_rejects_more_than_254_materials(self) -> None:
        with pytest.raises(ValueError, match="at most 254"):
            build_materials_json("x", ["Grass"] * 255)

    def test_deterministic(self) -> None:
        assert build_materials_json("lvl") == build_materials_json("lvl")
        assert json.dumps(build_materials_json("lvl")) == json.dumps(build_materials_json("lvl"))

    def test_groundmodel_is_carried_through(self) -> None:
        doc = build_materials_json("lvl", ["Asphalt"])
        assert doc["Asphalt"]["groundmodelName"] == "ASPHALT"


class TestAssetReferences:
    def test_paths_are_unique_and_sorted(self) -> None:
        paths = referenced_asset_paths()
        assert list(paths) == sorted(paths)
        assert len(set(paths)) == len(paths)

    def test_covers_every_referenced_texture(self) -> None:
        """Each of the 12 materials references 10 textures (5 channels x 2 layers).

        Sets are shared between materials, so the union is smaller than the sum;
        the point of this test is that no material is silently contributing
        fewer than its two full channel sets.
        """
        grouped = asset_paths_by_material()
        assert len(grouped) == len(DEFAULT_MATERIAL_ORDER)
        for key, paths in grouped.items():
            assert len(paths) == 10, f"{key} references {len(paths)} textures, expected 10"
        union = referenced_asset_paths()
        assert len(union) < sum(len(v) for v in grouped.values()), "sets should be shared"
        assert len(union) >= 10

    def test_grouped_by_material(self) -> None:
        grouped = asset_paths_by_material(["Grass", "Gravel"])
        assert set(grouped) == {"Grass", "Gravel"}
        assert len(grouped["Grass"]) == 10
        assert grouped["Grass"] != grouped["Gravel"]

    def test_all_paths_reference_the_shared_terrain_library(self) -> None:
        for path in referenced_asset_paths():
            assert path.startswith("/assets/materials/terrain/"), path
            assert not path.endswith(".link"), path

    def test_summer_ground_layers_are_visually_distinct(self) -> None:
        grass = MATERIAL_LIBRARY["Grass"].base_color
        field = MATERIAL_LIBRARY["Field"].base_color
        forest = MATERIAL_LIBRARY["ForestFloor"].base_color
        bank = MATERIAL_LIBRARY["RiverbankSoil"].base_color
        sand = MATERIAL_LIBRARY["Sand"].base_color

        # Grass: vibrant summer green
        assert grass[1] > grass[0] and grass[1] > grass[2]
        # Field: warm dry-grass / ochre (distinctly warmer than green Grass)
        assert field[0] > field[1] > field[2]
        # ForestFloor: darker earthy litter / humus (distinct from Grass & Field)
        assert forest[0] > forest[1] > forest[2]
        assert sum((a - b) ** 2 for a, b in zip(grass, forest, strict=True)) ** 0.5 > 45.0
        assert sum((a - b) ** 2 for a, b in zip(grass, field, strict=True)) ** 0.5 > 65.0
        # RiverbankSoil & Sand stay clearly separated
        assert sum((a - b) ** 2 for a, b in zip(sand, bank, strict=True)) ** 0.5 > 120.0

    def test_meadow_and_dry_grass_present_with_valid_channels(self) -> None:
        for required in (
            "Grass",
            "Meadow",
            "DryGrass",
            "Field",
            "ForestFloor",
            "RiverbankSoil",
            "Sand",
            "Pebbles",
            "Dirt",
            "Concrete",
            "Gravel",
            "Rock",
            "Asphalt",
            "Asphalt_worn",
        ):
            assert required in DEFAULT_MATERIAL_ORDER
            assert required in MATERIAL_LIBRARY
        grouped = asset_paths_by_material(["Meadow", "DryGrass"])
        assert len(grouped["Meadow"]) == 10
        assert len(grouped["DryGrass"]) == 10

    def test_base_textures_have_isotropic_variation_and_no_flat_stripes(self, tmp_path) -> None:
        import zlib
        import numpy as np
        from ivanovo2beam.beamng.materials import DEFAULT_BASE_TEX_SIZE, write_terrain_base_textures

        paths = write_terrain_base_textures(tmp_path, ["Grass", "Meadow", "Field", "DryGrass"])
        color_paths = [p for p in paths if p.name.endswith("_b.png")]
        assert len(color_paths) >= 4
        for p in color_paths:
            raw = p.read_bytes()
            pos = 8
            idat = bytearray()
            while pos < len(raw):
                ln = struct.unpack(">I", raw[pos : pos + 4])[0]
                ctype = raw[pos + 4 : pos + 8]
                if ctype == b"IDAT":
                    idat.extend(raw[pos + 8 : pos + 8 + ln])
                pos += 12 + ln
            dec = zlib.decompress(bytes(idat))
            arr = np.frombuffer(dec, dtype=np.uint8).reshape(
                DEFAULT_BASE_TEX_SIZE, 1 + DEFAULT_BASE_TEX_SIZE * 3
            )[:, 1:]
            assert float(arr.std()) > 4.0
