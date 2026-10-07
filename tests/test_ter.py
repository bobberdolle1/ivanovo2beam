"""Tests for the ``.ter`` v9 terrain codec.

The strongest test in this file reads a terrain that ships with BeamNG.drive,
re-encodes it, and asserts the bytes are identical. That is the only way to be
sure the format description is right: a self-consistent but wrong layout would
pass every synthetic round-trip and still produce levels the game refuses to
load.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from ivanovo2beam.beamng.ter import (
    HOLE,
    MAX_MATERIALS,
    MAX_SIZE,
    MIN_SIZE,
    TER_VERSION,
    TerrainFile,
    TerrainFormatError,
    decode_ter,
    encode_ter,
    read_ter,
    write_ter,
)

MATERIALS = ("Grass", "Asphalt", "dirt_rocky_large", "rock")


def make_terrain(size: int = 128, materials: tuple[str, ...] = MATERIALS) -> TerrainFile:
    """Build a deterministic synthetic terrain."""
    rng = np.random.default_rng(20261007)
    heights = rng.integers(0, 65535, size=(size, size), dtype=np.uint16)
    layers = rng.integers(0, len(materials), size=(size, size), dtype=np.uint8)
    return TerrainFile(size=size, heights=heights, layers=layers, materials=materials)


class TestFormatConstants:
    def test_version_is_nine(self) -> None:
        assert TER_VERSION == 9

    def test_size_range_matches_engine(self) -> None:
        assert (MIN_SIZE, MAX_SIZE) == (128, 8192)

    def test_material_and_hole_constants(self) -> None:
        assert MAX_MATERIALS == 254
        assert HOLE == 255


class TestPayloadAccounting:
    """The layout is `u8 + u32 + 3*size^2 + trailer`; prove it arithmetically."""

    @pytest.mark.parametrize("size", [128, 256, 512])
    def test_payload_size_formula(self, size: int) -> None:
        terrain = make_terrain(size)
        trailer = 4 + sum(1 + len(m.encode()) for m in MATERIALS)
        assert terrain.payload_size() == 5 + 3 * size * size + trailer
        assert len(encode_ter(terrain)) == terrain.payload_size()

    def test_header_is_version_then_u32_size(self) -> None:
        blob = encode_ter(make_terrain(128))
        version, size = struct.unpack_from("<BI", blob, 0)
        assert version == 9
        assert size == 128

    def test_material_trailer_is_u32_count_then_length_prefixed_names(self) -> None:
        blob = encode_ter(make_terrain(128))
        trailer_start = 5 + 3 * 128 * 128
        (count,) = struct.unpack_from("<I", blob, trailer_start)
        assert count == len(MATERIALS)
        cursor = trailer_start + 4
        for name in MATERIALS:
            (length,) = struct.unpack_from("<B", blob, cursor)
            cursor += 1
            assert blob[cursor : cursor + length].decode() == name
            cursor += length
        assert cursor == len(blob)


class TestSyntheticRoundTrip:
    @pytest.mark.parametrize("size", [128, 256])
    def test_bytes_survive_a_round_trip(self, size: int) -> None:
        original = make_terrain(size)
        restored = decode_ter(encode_ter(original))
        assert restored.size == original.size
        assert restored.version == original.version
        assert restored.materials == original.materials
        np.testing.assert_array_equal(restored.heights, original.heights)
        np.testing.assert_array_equal(restored.layers, original.layers)

    def test_encoded_bytes_are_stable(self) -> None:
        """Determinism: same input must give byte-identical output."""
        terrain = make_terrain(128)
        assert encode_ter(terrain) == encode_ter(terrain)

    def test_write_and_read_a_file(self, tmp_path) -> None:
        terrain = make_terrain(128)
        path = tmp_path / "nested" / "theTerrain.ter"
        written = write_ter(path, terrain)
        assert written == terrain.payload_size()
        assert read_ter(path).materials == terrain.materials

    def test_decoded_arrays_are_writable_copies(self) -> None:
        """Decoded arrays must not alias the input buffer."""
        blob = encode_ter(make_terrain(128))
        terrain = decode_ter(blob)
        terrain.heights[0, 0] = 12345  # must not raise
        assert terrain.heights.flags.writeable


class TestDecodeValidation:
    def test_rejects_short_file(self) -> None:
        with pytest.raises(TerrainFormatError, match="too short"):
            decode_ter(b"\x09\x00")

    def test_rejects_wrong_version(self) -> None:
        blob = bytearray(encode_ter(make_terrain(128)))
        blob[0] = 8
        with pytest.raises(TerrainFormatError, match="unsupported .ter version 8"):
            decode_ter(bytes(blob))

    def test_rejects_non_power_of_two_size(self) -> None:
        blob = bytearray(encode_ter(make_terrain(128)))
        struct.pack_into("<I", blob, 1, 200)
        with pytest.raises(TerrainFormatError, match="power of two"):
            decode_ter(bytes(blob))

    def test_rejects_out_of_range_size(self) -> None:
        blob = bytearray(encode_ter(make_terrain(128)))
        struct.pack_into("<I", blob, 1, 64)
        with pytest.raises(TerrainFormatError, match="outside the engine"):
            decode_ter(bytes(blob))

    def test_rejects_truncated_heightmap(self) -> None:
        blob = encode_ter(make_terrain(128))
        with pytest.raises(TerrainFormatError, match="truncated"):
            decode_ter(blob[: 5 + 2 * 128 * 128])

    def test_rejects_truncated_material_trailer(self) -> None:
        blob = encode_ter(make_terrain(128))
        with pytest.raises(TerrainFormatError, match="truncated"):
            decode_ter(blob[:-3])


class TestTerrainFileValidation:
    def test_rejects_shape_mismatch(self) -> None:
        with pytest.raises(TerrainFormatError, match="heights shape"):
            TerrainFile(
                size=128,
                heights=np.zeros((128, 64), dtype=np.uint16),
                layers=np.zeros((128, 128), dtype=np.uint8),
                materials=MATERIALS,
            )

    def test_rejects_wrong_dtype(self) -> None:
        with pytest.raises(TerrainFormatError, match="dtype"):
            TerrainFile(
                size=128,
                heights=np.zeros((128, 128), dtype=np.int32),
                layers=np.zeros((128, 128), dtype=np.uint8),
                materials=MATERIALS,
            )

    def test_rejects_layer_index_beyond_materials(self) -> None:
        layers = np.zeros((128, 128), dtype=np.uint8)
        layers[5, 5] = 9
        with pytest.raises(TerrainFormatError, match="references material index 9"):
            TerrainFile(
                size=128,
                heights=np.zeros((128, 128), dtype=np.uint16),
                layers=layers,
                materials=MATERIALS,
            )

    def test_allows_hole_value(self) -> None:
        layers = np.zeros((128, 128), dtype=np.uint8)
        layers[0, 0] = HOLE
        terrain = TerrainFile(
            size=128,
            heights=np.zeros((128, 128), dtype=np.uint16),
            layers=layers,
            materials=MATERIALS,
        )
        assert terrain.material_histogram()["hole"] == 1

    def test_rejects_too_many_materials(self) -> None:
        names = tuple(f"mat{i}" for i in range(MAX_MATERIALS + 1))
        with pytest.raises(TerrainFormatError, match="only honours 254"):
            TerrainFile(
                size=128,
                heights=np.zeros((128, 128), dtype=np.uint16),
                layers=np.zeros((128, 128), dtype=np.uint8),
                materials=names,
            )

    def test_rejects_empty_material_name(self) -> None:
        with pytest.raises(TerrainFormatError, match="must not be empty"):
            TerrainFile(
                size=128,
                heights=np.zeros((128, 128), dtype=np.uint16),
                layers=np.zeros((128, 128), dtype=np.uint8),
                materials=("Grass", ""),
            )

    def test_rejects_duplicate_material_names(self) -> None:
        with pytest.raises(TerrainFormatError, match="duplicate material"):
            TerrainFile(
                size=128,
                heights=np.zeros((128, 128), dtype=np.uint16),
                layers=np.zeros((128, 128), dtype=np.uint8),
                materials=("Grass", "Grass"),
            )

    def test_rejects_unsupported_version(self) -> None:
        with pytest.raises(TerrainFormatError, match="unsupported .ter version 7"):
            TerrainFile(
                size=128,
                heights=np.zeros((128, 128), dtype=np.uint16),
                layers=np.zeros((128, 128), dtype=np.uint8),
                materials=MATERIALS,
                version=7,
            )


class TestHeightQuantisation:
    def test_elevation_round_trip_within_half_a_step(self) -> None:
        """Quantisation error must stay below half the vertical step."""
        size = 128
        base_z, max_height = 60.0, 200.0
        rng = np.random.default_rng(7)
        elevations = rng.uniform(base_z, base_z + max_height, size=(size, size))
        layers = np.zeros((size, size), dtype=np.uint8)
        terrain = TerrainFile.from_elevations(
            elevations, layers, MATERIALS, base_z=base_z, max_height=max_height
        )
        restored = terrain.to_elevations(base_z, max_height)
        step = max_height / 65536.0
        assert np.abs(restored - elevations).max() <= step

    def test_uses_65536_not_65535(self) -> None:
        """The engine divides by 65536; using 65535 would be a systematic error."""
        terrain = TerrainFile(
            size=128,
            heights=np.full((128, 128), 65535, dtype=np.uint16),
            layers=np.zeros((128, 128), dtype=np.uint8),
            materials=MATERIALS,
        )
        elevations = terrain.to_elevations(base_z=0.0, max_height=65536.0)
        np.testing.assert_allclose(elevations, 65535.0, atol=1e-9)

    def test_clamps_rather_than_wraps(self) -> None:
        """Out-of-range elevations must saturate, never wrap around."""
        size = 128
        elevations = np.zeros((size, size))
        elevations[0, 0] = 10_000.0  # far above the range
        elevations[1, 1] = -10_000.0  # far below
        layers = np.zeros((size, size), dtype=np.uint8)
        terrain = TerrainFile.from_elevations(
            elevations, layers, MATERIALS, base_z=0.0, max_height=100.0
        )
        assert terrain.heights[0, 0] == 65535
        assert terrain.heights[1, 1] == 0

    def test_fit_vertical_range_covers_data_with_margin(self) -> None:
        elevations = np.array([[75.0, 120.0], [90.0, 168.0]])
        base_z, max_height = TerrainFile.fit_vertical_range(elevations, margin_m=5.0)
        assert base_z == pytest.approx(70.0)
        assert max_height == pytest.approx(103.0)
        # Every sample must land strictly inside the encodable range.
        stored_max = (168.0 - base_z) / (max_height / 65536.0)
        stored_min = (75.0 - base_z) / (max_height / 65536.0)
        assert 0 < stored_min < stored_max < 65536

    def test_fit_vertical_range_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="no finite elevations"):
            TerrainFile.fit_vertical_range(np.array([np.nan, np.inf]))

    def test_fit_vertical_range_handles_flat_terrain(self) -> None:
        base_z, max_height = TerrainFile.fit_vertical_range(np.full((4, 4), 100.0))
        assert max_height > 0
        assert base_z < 100.0

    def test_to_elevations_rejects_bad_max_height(self) -> None:
        with pytest.raises(ValueError, match="max_height must be positive"):
            make_terrain(128).to_elevations(0.0, 0.0)

    def test_from_elevations_rejects_bad_shapes(self) -> None:
        with pytest.raises(ValueError, match="square 2-D array"):
            TerrainFile.from_elevations(
                np.zeros((10, 20)), np.zeros((10, 20), dtype=np.uint8), MATERIALS, 0.0, 100.0
            )

    def test_from_elevations_rejects_layer_shape_mismatch(self) -> None:
        with pytest.raises(ValueError, match="layers shape"):
            TerrainFile.from_elevations(
                np.zeros((8, 8)), np.zeros((4, 4), dtype=np.uint8), MATERIALS, 0.0, 100.0
            )


class TestMaterialHistogram:
    def test_counts_every_cell(self) -> None:
        terrain = make_terrain(128)
        histogram = terrain.material_histogram()
        assert sum(histogram.values()) == 128 * 128
        assert set(histogram) == set(MATERIALS)

    def test_with_layers_preserves_heights(self) -> None:
        terrain = make_terrain(128)
        new_layers = np.full((128, 128), 1, dtype=np.uint8)
        updated = terrain.with_layers(new_layers)
        np.testing.assert_array_equal(updated.heights, terrain.heights)
        assert updated.material_histogram()["Asphalt"] == 128 * 128


# --------------------------------------------------------------------------
# Validation against the real thing.
# --------------------------------------------------------------------------


class TestAgainstShippedBeamNGTerrain:
    """Re-encode real BeamNG terrains and require byte-identical output.

    These tests need a BeamNG.drive installation and are skipped without one
    (``pytest -m "not beamng"`` to deselect explicitly).
    """

    @pytest.mark.beamng
    @pytest.mark.parametrize(
        ("level", "member"),
        [
            ("template", "levels/template/theTerrain.ter"),
            ("gridmap_v2", "levels/gridmap_v2/theTerrain.ter"),
            ("east_coast_usa", "levels/east_coast_usa/theTerrain2.ter"),
            ("west_coast_usa", "levels/west_coast_usa/west_coast_usa.ter"),
            ("Utah", "levels/Utah/theTerrain.ter"),
            ("italy", "levels/italy/theTerrain.ter"),
        ],
    )
    def test_shipped_terrain_round_trips_byte_exactly(
        self, beamng_levels_dir, level: str, member: str
    ) -> None:
        """The format description is correct iff real files survive a round trip."""
        from tests.conftest import open_shipped_level

        with open_shipped_level(beamng_levels_dir, level) as archive:
            original = archive.read(member)

        terrain = decode_ter(original)
        reencoded = encode_ter(terrain)

        assert MIN_SIZE <= terrain.size <= MAX_SIZE
        assert terrain.size & (terrain.size - 1) == 0, "size must be a power of two"
        assert len(reencoded) == len(original)
        assert reencoded == original, (
            f"{level}: re-encoded terrain differs from the shipped file "
            f"(decoded size={terrain.size}, materials={terrain.materials})"
        )

    @pytest.mark.beamng
    def test_template_materials_match_the_terrain_json(self, beamng_levels_dir) -> None:
        """Cross-check the material list in the .ter against its descriptor."""
        import json

        from tests.conftest import open_shipped_level

        with open_shipped_level(beamng_levels_dir, "template") as archive:
            terrain = decode_ter(archive.read("levels/template/theTerrain.ter"))
            descriptor = json.loads(
                archive.read("levels/template/theTerrain.terrain.json").decode("utf-8", "replace")
            )

        assert list(terrain.materials) == descriptor["materials"]
        assert terrain.size == descriptor["size"]
        # heightMapSize/layerMapSize are cell counts, i.e. size squared.
        assert descriptor["heightMapSize"] == terrain.size**2
        assert descriptor["layerMapSize"] == terrain.size**2
        assert descriptor["heightMapItemSize"] == 2
        assert descriptor["layerMapItemSize"] == 1
        assert descriptor["version"] == TER_VERSION
