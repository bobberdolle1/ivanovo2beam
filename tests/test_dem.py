"""Tests for the DEM acquisition and sampling module.

The suite is split in two halves:

* everything that can be checked without a network -- cache-key stability, the
  Copernicus / SRTM tile-name arithmetic (including the negative hemispheres
  Ivanovo does not need), bilinear interpolation against hand-computed values,
  nodata handling, out-of-bounds behaviour, and the whole fallback/caching
  orchestration driven through fake providers;
* one ``@pytest.mark.network`` test that really downloads the Copernicus tiles
  around Ivanovo and asserts the elevations are plausible metres above sea
  level, plus a cheap one for the Terrain Tiles fallback.

Run offline::

    pytest tests/test_dem.py -m "not network"
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Geod
from rasterio.transform import Affine

from ivanovo2beam.gis.projection import BBox
from ivanovo2beam.terrain import dem
from ivanovo2beam.terrain.dem import (
    DemFetchError,
    DemReader,
    DemSourceUnavailable,
    available_sources,
    cache_key,
    fetch_dem,
    read_sidecar,
)

#: Ivanovo city centre (OpenStreetMap), WGS84 degrees.
IVANOVO = (40.97393, 56.99987)

#: The bbox the network test downloads: it straddles both the 41 E and the
#: 57 N degree boundaries, so it exercises a four-tile mosaic and its seams.
SMALL_BBOX = BBox(west=40.95, south=56.98, east=41.00, north=57.02)

#: The oblast-scale box used for resolution reasoning only.
OBLAST_BBOX = BBox(west=39.00, south=56.10, east=43.20, north=57.80)

_GEOD = Geod(ellps="WGS84")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def write_synthetic_geotiff(
    path: Path,
    array: np.ndarray,
    *,
    west: float = 40.0,
    north: float = 57.0,
    res: float = 0.005,
    nodata: float | None = None,
    point_registered: bool = False,
    crs: str = "EPSG:4326",
) -> Path:
    """Write a small single-band float32 GeoTIFF with known georeferencing.

    ``(west, north)`` is the top-left *area* corner, or the first pixel *centre*
    when ``point_registered`` is set -- the convention the Copernicus COGs use.
    """
    height, width = array.shape
    transform = Affine.translation(west, north) @ Affine.scale(res, -res)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dataset:
        dataset.write(np.asarray(array, dtype=np.float32), 1)
        if point_registered:
            dataset.update_tags(AREA_OR_POINT="Point")
    return path


def fake_fetch_result(
    work: Path,
    array: np.ndarray,
    *,
    bbox: BBox | None = None,
    payload_bytes: bytes = b"fake-tile-payload",
    url: str = "https://example.invalid/tile.tif",
) -> dem._FetchResult:
    """Build a :class:`dem._FetchResult` the way a real provider would.

    The raster covers ``bbox`` (its pixel-centre extent contains the request),
    mirroring the contract every real provider honours.
    """
    box = SMALL_BBOX if bbox is None else bbox
    height, width = array.shape
    raster = write_synthetic_geotiff(
        work / "provider-output.tif",
        array,
        west=box.west - 0.25 * (box.east - box.west) / width,
        north=box.north + 0.25 * (box.north - box.south) / height,
        res=max((box.east - box.west) / width, (box.north - box.south) / height) / 2.0,
    )
    payload_file = work / "payload.bin"
    payload_file.write_bytes(payload_bytes)
    payload = dem._Payload(
        url=url,
        path=payload_file,
        n_bytes=len(payload_bytes),
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
        etag='"deadbeef"',
    )
    return dem._FetchResult(
        path=raster,
        payloads=(payload,),
        detail={"endpoint": "https://example.invalid"},
        stats=dem._elevation_stats(np.asarray(array, dtype=np.float64)),
    )


def make_fake_provider(
    calls: list[str],
    key: str,
    *,
    fail: bool = False,
    array: np.ndarray | None = None,
):
    """Return a provider stub that records its calls (and can refuse to serve)."""

    def provider(bbox: BBox, resolution_m: float | None, timeout: float, work: Path):
        calls.append(key)
        if fail:
            raise DemSourceUnavailable(f"{key}: simulated outage")
        work.mkdir(parents=True, exist_ok=True)
        data = np.full((8, 8), 123.0) if array is None else array
        return fake_fetch_result(work, data, bbox=bbox)

    return provider


# --------------------------------------------------------------------------- #
# source catalogue
# --------------------------------------------------------------------------- #


class TestSourceCatalogue:
    def test_chain_order_and_uniqueness(self) -> None:
        sources = available_sources()
        assert [info.key for info in sources] == list(dem.SOURCE_CHAIN)
        assert len({info.key for info in sources}) == len(sources)
        assert sources[0].key == "copernicus_glo30"

    def test_every_source_is_documented_and_anonymous(self) -> None:
        for info in available_sources():
            assert info.title
            assert info.resolution_m > 0.0
            assert info.attribution
            assert info.licence
            assert info.url_template.startswith("https://")
            assert info.requires_auth is False, (
                f"{info.key} claims to need auth; every shipped source was verified anonymous"
            )
            assert info.notes, f"{info.key} must document what was verified about it"

    def test_copernicus_notes_record_the_anonymous_verification(self) -> None:
        notes = dem.source_info("copernicus_glo30").notes.lower()
        assert "no aws credentials" in notes
        assert "2026-10-07" in notes

    def test_unknown_source_is_rejected_before_any_network_use(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown DEM source"):
            dem.source_info("nasas-private-dem")
        with pytest.raises(ValueError, match="unknown DEM source"):
            fetch_dem(SMALL_BBOX, tmp_path / "cache", source="nasas-private-dem")

    def test_invalid_resolution_and_timeout_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="resolution_m"):
            fetch_dem(SMALL_BBOX, tmp_path / "cache", resolution_m=0.0)
        with pytest.raises(ValueError, match="timeout"):
            fetch_dem(SMALL_BBOX, tmp_path / "cache", timeout=-1.0)


# --------------------------------------------------------------------------- #
# cache key
# --------------------------------------------------------------------------- #


class TestCacheKey:
    def test_is_a_hex_sha256(self) -> None:
        key = cache_key(SMALL_BBOX, "copernicus_glo30")
        assert len(key) == 64
        assert all(character in "0123456789abcdef" for character in key)

    def test_is_stable_across_calls(self) -> None:
        assert cache_key(SMALL_BBOX, "copernicus_glo30") == cache_key(SMALL_BBOX, "copernicus_glo30")

    def test_absorbs_float_noise_but_not_real_moves(self) -> None:
        reference = cache_key(SMALL_BBOX, "copernicus_glo30")
        noisy = BBox(
            west=SMALL_BBOX.west + 1e-9,
            south=SMALL_BBOX.south - 1e-9,
            east=SMALL_BBOX.east,
            north=SMALL_BBOX.north,
        )
        assert cache_key(noisy, "copernicus_glo30") == reference
        # 1e-4 deg is ~10 m: a real change, and it must change the key.
        moved = BBox(west=40.9501, south=56.98, east=41.00, north=57.02)
        assert cache_key(moved, "copernicus_glo30") != reference

    @pytest.mark.parametrize(
        "other",
        [
            BBox(west=40.94, south=56.98, east=41.00, north=57.02),
            BBox(west=40.95, south=56.97, east=41.00, north=57.02),
            BBox(west=40.95, south=56.98, east=41.01, north=57.02),
            BBox(west=40.95, south=56.98, east=41.00, north=57.03),
        ],
    )
    def test_every_edge_matters(self, other: BBox) -> None:
        assert cache_key(other, "copernicus_glo30") != cache_key(SMALL_BBOX, "copernicus_glo30")

    def test_source_and_resolution_matter(self) -> None:
        reference = cache_key(SMALL_BBOX, "copernicus_glo30")
        assert cache_key(SMALL_BBOX, "aws_terrain_tiles") != reference
        assert cache_key(SMALL_BBOX, "copernicus_glo30", 30.0) != reference
        assert cache_key(SMALL_BBOX, "copernicus_glo30", 90.0) != cache_key(
            SMALL_BBOX, "copernicus_glo30", 30.0
        )
        assert cache_key(SMALL_BBOX, "copernicus_glo30", None) != cache_key(
            SMALL_BBOX, "copernicus_glo30", 30.0
        )

    def test_is_not_the_raw_bbox_string(self) -> None:
        key = cache_key(SMALL_BBOX, "copernicus_glo30")
        naive = hashlib.sha256(str(SMALL_BBOX).encode("utf-8")).hexdigest()
        assert key != naive
        assert "40.95" not in key


# --------------------------------------------------------------------------- #
# tile-name arithmetic
# --------------------------------------------------------------------------- #


class TestCopernicusNaming:
    def test_ivanovo_tile_name_and_url(self) -> None:
        name = dem.copernicus_tile_name(56, 41)
        assert name == "Copernicus_DSM_COG_10_N56_00_E041_00_DEM"
        # This exact URL answered HTTP 200 with 32103651 bytes when verified.
        assert dem.copernicus_tile_url(name) == (
            "https://copernicus-dem-30m.s3.amazonaws.com/"
            "Copernicus_DSM_COG_10_N56_00_E041_00_DEM/"
            "Copernicus_DSM_COG_10_N56_00_E041_00_DEM.tif"
        )

    @pytest.mark.parametrize(
        ("lat_index", "lon_index", "expected"),
        [
            (56, 41, "Copernicus_DSM_COG_10_N56_00_E041_00_DEM"),
            (0, 0, "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"),
            (0, 9, "Copernicus_DSM_COG_10_N00_00_E009_00_DEM"),
            (-1, 9, "Copernicus_DSM_COG_10_S01_00_E009_00_DEM"),
            (-1, -1, "Copernicus_DSM_COG_10_S01_00_W001_00_DEM"),
            (-23, -47, "Copernicus_DSM_COG_10_S23_00_W047_00_DEM"),
            (51, -1, "Copernicus_DSM_COG_10_N51_00_W001_00_DEM"),
            (-56, -123, "Copernicus_DSM_COG_10_S56_00_W123_00_DEM"),
        ],
    )
    def test_hemisphere_and_zero_padding(self, lat_index: int, lon_index: int, expected: str) -> None:
        assert dem.copernicus_tile_name(lat_index, lon_index) == expected

    def test_zero_is_north_and_east(self) -> None:
        # Copernicus names tiles after their lower-left corner, so 0..1 degree
        # is N00/E000 and -1..0 is S01/W001 -- never S00 or W000.
        assert dem.copernicus_tile_name(0, 0).startswith("Copernicus_DSM_COG_10_N00_00_E000")
        assert "S00" not in dem.copernicus_tile_name(-1, -1)
        assert "W000" not in dem.copernicus_tile_name(-1, -1)

    def test_tile_ids_for_ivanovo(self) -> None:
        assert dem.copernicus_tile_ids(SMALL_BBOX) == [(56, 40), (56, 41), (57, 40), (57, 41)]

    def test_tile_ids_for_a_bbox_inside_one_tile(self) -> None:
        assert dem.copernicus_tile_ids(BBox(40.2, 56.2, 40.8, 56.8)) == [(56, 40)]

    def test_tile_ids_for_negative_hemispheres(self) -> None:
        assert dem.copernicus_tile_ids(BBox(west=-3.5, south=-1.2, east=-2.5, north=-0.5)) == [
            (-2, -4),
            (-2, -3),
            (-1, -4),
            (-1, -3),
        ]

    def test_tile_ids_at_exact_degree_edges_include_both_sides(self) -> None:
        # Sampling exactly at 57.0 N needs the N57 tile's first row, so a
        # boundary-touching bbox must pull in the tile on each side.
        assert dem.copernicus_tile_ids(BBox(40.0, 56.0, 41.0, 57.0)) == [
            (56, 40),
            (56, 41),
            (57, 40),
            (57, 41),
        ]
        assert dem.copernicus_tile_ids(BBox(0.0, 0.0, 1.0, 1.0)) == [(0, 0), (0, 1), (1, 0), (1, 1)]

    def test_srtm_tile_name_matches_the_skadi_layout(self) -> None:
        assert dem.srtm_tile_name(57, 41) == "N57E041"
        assert dem.srtm_tile_name(-1, -1) == "S01W001"
        assert dem.srtm_tile_name(0, 0) == "N00E000"


class TestMercatorTiles:
    def test_index_matches_the_live_dataset(self) -> None:
        # Both of these URLs were fetched successfully when verified:
        # .../geotiff/12/2514/1254.tif (59314 bytes) and
        # .../geotiff/13/5028/2509.tif (325490 bytes).
        assert dem.mercator_tile_index(41.0, 57.0, 12) == (2514, 1254)
        assert dem.mercator_tile_index(41.0, 57.0, 13) == (5028, 2509)

    def test_index_is_clamped_to_the_grid(self) -> None:
        for zoom in (0, 1, 12):
            x, y = dem.mercator_tile_index(180.0, 90.0, zoom)
            assert 0 <= x < 2**zoom
            assert 0 <= y < 2**zoom
        assert dem.mercator_tile_index(-180.0, -90.0, 4) == (0, 15)
        with pytest.raises(ValueError, match="zoom"):
            dem.mercator_tile_index(0.0, 0.0, 24)

    def test_pixel_resolution_is_half_the_textbook_value(self) -> None:
        # The dataset ships 512 px over one standard XYZ tile extent, so its
        # z=12 pixel is 19.109 m -- what a real tile's transform reported.
        assert dem.terrain_tile_pixel_res_m(12) == pytest.approx(19.10925707129354, rel=1e-9)
        assert dem.terrain_tile_span_m(12) == pytest.approx(9783.940, rel=1e-5)
        assert dem.mercator_ground_resolution_m(12, 57.0) == pytest.approx(10.4076, rel=1e-3)
        assert dem.mercator_ground_resolution_m(12, 0.0) == pytest.approx(19.1093, rel=1e-3)

    def test_zoom_rule_picks_the_coarsest_zoom_that_meets_the_target(self) -> None:
        assert dem.zoom_for_resolution(30.0, 57.0) == 11  # 20.8 m at Ivanovo
        assert dem.zoom_for_resolution(30.0, 0.0) == 12  # 19.1 m at the equator
        assert dem.zoom_for_resolution(90.0, 57.0) == 9  # 83.3 m, the first zoom <= 90 m
        assert dem.zoom_for_resolution(5.0, 57.0) == dem.MAX_ZOOM  # clamped
        assert dem.zoom_for_resolution(1e9, 57.0) == dem.MIN_ZOOM  # clamped


# --------------------------------------------------------------------------- #
# pure numerics
# --------------------------------------------------------------------------- #


class TestDecimation:
    def test_factor_below_two_is_a_no_op(self) -> None:
        array = np.arange(9, dtype=float).reshape(3, 3)
        assert dem._decimate_block_mean(array, 1) is array

    def test_block_means(self) -> None:
        array = np.arange(16, dtype=float).reshape(4, 4)
        result = dem._decimate_block_mean(array, 2)
        np.testing.assert_allclose(result, [[2.5, 4.5], [10.5, 12.5]])

    def test_ignores_nan_but_keeps_all_nan_blocks_nan(self) -> None:
        array = np.array([[1.0, 3.0, np.nan, np.nan], [5.0, 7.0, np.nan, np.nan]], dtype=float)
        result = dem._decimate_block_mean(array, 2)
        assert result[0, 0] == pytest.approx(4.0)  # mean of 1, 3, 5, 7
        assert np.isnan(result[0, 1])

    def test_partial_edge_blocks_still_cover_the_extent(self) -> None:
        array = np.full((5, 5), 2.0)
        result = dem._decimate_block_mean(array, 2)
        assert result.shape == (3, 3)  # ceil(5 / 2)
        np.testing.assert_allclose(result, 2.0)

    def test_decimation_factor_policy(self) -> None:
        assert dem._decimation_factor(None, 28.0) == 1
        assert dem._decimation_factor(30.0, 28.0) == 1  # finer than 1.5x native
        assert dem._decimation_factor(90.0, 28.0) == 3
        assert dem._decimation_factor(120.0, 30.0) == 4
        assert dem._decimation_factor(1e6, 30.0) == 64  # capped
        assert dem._decimation_factor(90.0, 0.0) == 1


class TestBilinear:
    def test_reads_exact_pixels_and_interpolates_between_them(self) -> None:
        array = np.array([[0.0, 10.0], [20.0, 30.0]])
        col = np.array([0.0, 1.0, 0.5, 0.25])
        row = np.array([0.0, 1.0, 0.5, 0.75])
        result = dem._bilinear(array, col, row)
        # 0.25 / 0.75 into the cell: 0.1875*0 + 0.0625*10 + 0.5625*20 + 0.1875*30.
        expected = [0.0, 30.0, 15.0, 17.5]
        np.testing.assert_allclose(result, expected)

    def test_outside_is_nan(self) -> None:
        array = np.ones((2, 2))
        result = dem._bilinear(array, np.array([-0.01, 1.01, 0.5]), np.array([0.5, 0.5, -0.01]))
        assert np.isnan(result).all()

    def test_nan_neighbour_propagates_only_when_it_contributes(self) -> None:
        array = np.array([[1.0, np.nan], [3.0, 4.0]])
        result = dem._bilinear(array, np.array([0.0, 0.5]), np.array([0.0, 0.5]))
        # Exactly on the valid pixel: `nan * 0` must not poison the result.
        assert result[0] == 1.0
        # Halfway across the cell the nodata corner really does contribute.
        assert np.isnan(result[1])


# --------------------------------------------------------------------------- #
# DemReader on synthetic rasters
# --------------------------------------------------------------------------- #


@pytest.fixture
def synthetic_path(tmp_path: Path) -> Path:
    """A 4x3 area-registered raster: 0.005 deg pixels, values 10..120 by 10."""
    array = np.arange(10.0, 130.0, 10.0).reshape(4, 3)
    return write_synthetic_geotiff(tmp_path / "synthetic.tif", array, west=40.0, north=57.0, res=0.005)


class TestDemReaderProperties:
    def test_metadata_round_trip(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        assert reader.path == synthetic_path
        assert reader.crs_epsg == 4326
        assert reader.shape == (4, 3)
        assert reader.resolution_deg == pytest.approx(0.005)
        assert reader.resolution_xy_deg == pytest.approx((0.005, 0.005))
        assert reader.nodata is None
        assert reader.metadata == {}  # no sidecar next to a synthetic raster
        assert reader.source_key is None

    def test_bounds_are_the_pixel_centre_extent(self, synthetic_path: Path) -> None:
        bounds = DemReader(synthetic_path).bounds
        assert bounds.west == pytest.approx(40.0025)
        assert bounds.east == pytest.approx(40.0125)
        assert bounds.north == pytest.approx(56.9975)
        assert bounds.south == pytest.approx(56.9825)  # four rows southwards

    def test_point_registered_raster_is_shifted_by_half_a_pixel(self, tmp_path: Path) -> None:
        # Copernicus COGs put the transform origin on a pixel centre; the reader
        # must normalise that to the same contract as area-registered rasters.
        array = np.full((2, 2), 5.0)
        path = write_synthetic_geotiff(
            tmp_path / "point.tif", array, west=41.0, north=57.0, res=0.5, point_registered=True
        )
        reader = DemReader(path)
        assert reader.bounds.west == pytest.approx(41.0)
        assert reader.bounds.north == pytest.approx(57.0)
        assert reader.bounds.east == pytest.approx(41.5)
        assert reader.bounds.south == pytest.approx(56.5)
        assert reader.sample(41.0, 57.0) == pytest.approx(5.0)

    def test_missing_file_and_wrong_crs(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            DemReader(tmp_path / "nope.tif")
        array = np.ones((2, 2))
        projected = write_synthetic_geotiff(tmp_path / "merc.tif", array, crs="EPSG:3857")
        with pytest.raises(ValueError, match="geographic"):
            DemReader(projected)

    def test_context_manager_and_reopen(self, synthetic_path: Path) -> None:
        with DemReader(synthetic_path) as reader:
            assert reader.sample(40.0025, 56.9975) == pytest.approx(10.0)
        reader.close()
        # A closed reader must transparently reopen rather than fail.
        assert reader.sample(40.0025, 56.9975) == pytest.approx(10.0)
        reader.close()


class TestSampling:
    def test_sample_returns_metres_at_pixel_centres(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        # Row-major values 10, 20, 30 / 40, 50, 60 / 70, 80, 90 / 100, 110, 120,
        # the first row being the northernmost (lat 56.9975).
        assert reader.sample(40.0025, 56.9975) == pytest.approx(10.0)
        assert reader.sample(40.0125, 56.9975) == pytest.approx(30.0)
        assert reader.sample(40.0025, 56.9825) == pytest.approx(100.0)
        assert reader.sample(40.0075, 56.9875) == pytest.approx(80.0)

    def test_sample_interpolates_bilinearly(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        # Halfway between the 10 and 20 pixels, on the top row.
        assert reader.sample(40.005, 56.9975) == pytest.approx(15.0)
        # Centre of the 10/20/40/50 cell.
        assert reader.sample(40.005, 56.995) == pytest.approx(30.0)
        # A quarter of the way into that cell from its north-west corner.
        expected = 0.75 * 0.75 * 10 + 0.25 * 0.75 * 20 + 0.75 * 0.25 * 40 + 0.25 * 0.25 * 50
        assert expected == pytest.approx(20.0)  # sanity-check the arithmetic below
        assert reader.sample(40.00375, 56.99625) == pytest.approx(expected)

    def test_sample_outside_raises(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        for lon, lat in [(39.9, 57.0), (40.1, 57.0), (40.005, 57.1), (40.005, 56.9)]:
            with pytest.raises(ValueError, match="outside"):
                reader.sample(lon, lat)

    def test_sample_on_the_edge_is_allowed(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        assert reader.sample(40.0025, 56.9825) == pytest.approx(100.0)  # SW corner pixel
        assert reader.sample(40.0125, 56.9975) == pytest.approx(30.0)  # NE corner pixel

    def test_sample_array_preserves_shape_and_masks_outside(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        lons = np.array([[40.0025, 40.0075], [40.0125, 39.0]])
        lats = np.array([[56.9975, 56.9875], [56.9825, 57.0]])
        result = reader.sample_array(lons, lats)
        assert result.shape == (2, 2)
        np.testing.assert_allclose(result[0], [10.0, 80.0])
        np.testing.assert_allclose(result[1, 0], 120.0)
        assert np.isnan(result[1, 1])

    def test_sample_array_one_dimensional_and_flat(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        result = reader.sample_array([40.0025, 40.0125], [56.9975, 56.9825])
        assert result.shape == (2,)
        np.testing.assert_allclose(result, [10.0, 120.0])

    def test_extreme_corner_pixels_are_not_lost_to_float_error(self, synthetic_path: Path) -> None:
        # (last column - first column) / pixel size evaluates to 2.0000000000000004,
        # which a naive `col <= width - 1` test would call "outside".
        reader = DemReader(synthetic_path)
        corners = reader.sample_array(
            np.array([40.0125, 40.0125, 40.0025, 40.0025]),
            np.array([56.9975, 56.9825, 56.9975, 56.9825]),
        )
        np.testing.assert_allclose(corners, [30.0, 120.0, 10.0, 100.0])

    def test_sample_array_rejects_shape_mismatch(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        with pytest.raises(ValueError, match="shape mismatch"):
            reader.sample_array([40.0, 41.0], [56.0])

    def test_nodata_becomes_nan(self, tmp_path: Path) -> None:
        array = np.array([[10.0, -9999.0], [30.0, 40.0]])
        path = write_synthetic_geotiff(tmp_path / "holes.tif", array, nodata=-9999.0)
        reader = DemReader(path)
        assert reader.nodata == -9999.0
        assert np.isnan(reader.sample(40.0075, 56.9975))
        assert reader.sample(40.0025, 56.9975) == pytest.approx(10.0)
        # A sample whose bilinear neighbourhood touches the hole is NaN too.
        assert np.isnan(reader.sample(40.005, 56.9975))


class TestWindowArray:
    @staticmethod
    def ramp(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        """The exact plane the fixture's pixels lie on: ``10 + 10*col + 30*row``."""
        return 10.0 + 10.0 * (lon - 40.0025) / 0.005 + 30.0 * (56.9975 - lat) / 0.005

    def test_shape_cell_size_and_values(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        bbox = BBox(west=40.0025, south=56.9875, east=40.0125, north=56.9975)
        grid, cell_m = reader.window_array(bbox, 12)
        assert grid.shape == (12, 12)
        assert grid.dtype == np.float64
        assert not np.isnan(grid).any()
        _, _, span_x = _GEOD.inv(bbox.west, bbox.center[1], bbox.east, bbox.center[1])
        _, _, span_y = _GEOD.inv(bbox.center[0], bbox.south, bbox.center[0], bbox.north)
        assert cell_m == pytest.approx(0.5 * (span_x + span_y) / 12)
        # Bilinear resampling of a plane is exact, so the whole grid is known.
        step = (bbox.east - bbox.west) / 12
        lons = bbox.west + (np.arange(12) + 0.5) * step
        lats = bbox.north - (np.arange(12) + 0.5) * step
        np.testing.assert_allclose(grid, self.ramp(*np.meshgrid(lons, lats)), atol=1e-9)

    def test_covers_the_bbox_centre(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        bbox = BBox(west=40.0025, south=56.9875, east=40.0125, north=56.9975)
        grid, _ = reader.window_array(bbox, 2)
        # For a plane, the mean of the four quarter-point samples is the value
        # at the centre: 10 + 10*1 + 30*1 = 50 m.
        assert float(np.mean(grid)) == pytest.approx(50.0)
        assert float(np.mean(grid)) == pytest.approx(float(self.ramp(*bbox.center)))

    def test_nan_outside_a_partially_covered_bbox(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        # Overlaps the raster on its eastern side only.
        bbox = BBox(west=40.001, south=56.988, east=40.008, north=56.996)
        grid, _ = reader.window_array(bbox, 8)
        assert grid.shape == (8, 8)
        assert np.isnan(grid[:, 0]).all()  # west of the first valid pixel centre
        assert np.isfinite(grid[:, -1]).all()

    def test_disjoint_bbox_raises(self, synthetic_path: Path) -> None:
        reader = DemReader(synthetic_path)
        with pytest.raises(ValueError, match="does not overlap"):
            reader.window_array(BBox(west=30.0, south=50.0, east=31.0, north=51.0), 4)

    @pytest.mark.parametrize("bad", [0, 1, -3, 2.5, "4", True])
    def test_invalid_width_raises(self, synthetic_path: Path, bad: object) -> None:
        reader = DemReader(synthetic_path)
        with pytest.raises(ValueError, match="width"):
            reader.window_array(BBox(40.0025, 56.9875, 40.0125, 56.9975), bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# fetch orchestration, offline (fake providers)
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_chain(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every provider with a recording stub; return the call log."""
    calls: list[str] = []
    for key in dem.SOURCE_CHAIN:
        monkeypatch.setitem(dem._PROVIDERS, key, make_fake_provider(calls, key))
    return calls


class TestFetchOrchestration:
    def test_writes_raster_and_full_sidecar(self, tmp_path: Path, fake_chain: list[str]) -> None:
        cache = tmp_path / "cache"
        path = fetch_dem(SMALL_BBOX, cache, timeout=5.0)
        assert path == cache / f"{cache_key(SMALL_BBOX, 'copernicus_glo30')}.tif"
        assert path.is_file()
        assert path.parent == cache
        entry = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        for key in (
            "cache_key",
            "source",
            "requested_source",
            "source_urls",
            "fetched_utc",
            "bbox",
            "requested_bbox",
            "bytes",
            "sha256",
            "sha256_payload",
            "payloads",
            "source_attribution",
            "source_licence",
        ):
            assert key in entry, f"sidecar is missing {key!r}"
        assert entry["source"] == "copernicus_glo30"
        assert entry["requested_source"] == "copernicus_glo30"
        assert entry["fallback_used"] is False
        assert entry["source_urls"] == ["https://example.invalid/tile.tif"]
        # The recorded extent is the raster's own, and it covers the request.
        assert set(entry["bbox"]) == {"west", "south", "east", "north"}
        assert entry["bbox"]["west"] <= entry["requested_bbox"]["west"]
        assert entry["bbox"]["north"] >= entry["requested_bbox"]["north"]
        assert entry["requested_bbox"] == {
            "west": SMALL_BBOX.west,
            "south": SMALL_BBOX.south,
            "east": SMALL_BBOX.east,
            "north": SMALL_BBOX.north,
        }
        # The recorded checksums must really describe the artefacts.
        assert entry["bytes"] == path.stat().st_size
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["payloads"][0]["sha256"] == hashlib.sha256(b"fake-tile-payload").hexdigest()
        # The scratch directory must not survive the call.
        assert not [item for item in cache.iterdir() if item.name.startswith(".work-")]

    def test_second_call_is_a_cache_hit_without_touching_a_provider(
        self, tmp_path: Path, fake_chain: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = tmp_path / "cache"
        first = fetch_dem(SMALL_BBOX, cache, timeout=5.0)
        stamp = first.stat().st_mtime_ns
        assert fake_chain == ["copernicus_glo30"]

        def explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("a cache hit must not download anything")

        monkeypatch.setattr(dem, "_download", explode)
        second = fetch_dem(SMALL_BBOX, cache, timeout=5.0)
        assert second == first
        assert second.stat().st_mtime_ns == stamp
        assert fake_chain == ["copernicus_glo30"]  # no provider ran again

    def test_force_bypasses_the_cache(self, tmp_path: Path, fake_chain: list[str]) -> None:
        cache = tmp_path / "cache"
        fetch_dem(SMALL_BBOX, cache, timeout=5.0)
        fetch_dem(SMALL_BBOX, cache, timeout=5.0, force=True)
        assert fake_chain == ["copernicus_glo30", "copernicus_glo30"]

    def test_a_tif_without_sidecar_is_not_a_cache_hit(self, tmp_path: Path, fake_chain: list[str]) -> None:
        cache = tmp_path / "cache"
        path = fetch_dem(SMALL_BBOX, cache, timeout=5.0)
        # Simulate a crashed or concurrent run: raster published, sidecar absent.
        path.with_suffix(".json").unlink()
        fetch_dem(SMALL_BBOX, cache, timeout=5.0)
        assert fake_chain == ["copernicus_glo30", "copernicus_glo30"]

    def test_a_truncated_raster_is_not_a_cache_hit(self, tmp_path: Path, fake_chain: list[str]) -> None:
        cache = tmp_path / "cache"
        path = fetch_dem(SMALL_BBOX, cache, timeout=5.0)
        path.write_bytes(b"not a geotiff")
        fetch_dem(SMALL_BBOX, cache, timeout=5.0)
        assert fake_chain == ["copernicus_glo30", "copernicus_glo30"]

    def test_falls_back_and_records_the_source_that_delivered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setitem(
            dem._PROVIDERS, "copernicus_glo30", make_fake_provider(calls, "copernicus_glo30", fail=True)
        )
        monkeypatch.setitem(
            dem._PROVIDERS, "aws_terrain_tiles", make_fake_provider(calls, "aws_terrain_tiles")
        )
        monkeypatch.setitem(dem._PROVIDERS, "srtm_gl1", make_fake_provider(calls, "srtm_gl1"))
        path = fetch_dem(SMALL_BBOX, tmp_path / "cache", timeout=5.0)
        entry = read_sidecar(path)
        assert calls == ["copernicus_glo30", "aws_terrain_tiles"]
        assert entry["requested_source"] == "copernicus_glo30"
        assert entry["source"] == "aws_terrain_tiles"
        assert entry["fallback_used"] is True
        assert entry["source_title"] == dem.source_info("aws_terrain_tiles").title
        assert entry["source_attribution"] == dem.source_info("aws_terrain_tiles").attribution
        assert "simulated outage" in entry["source_attempts"][0]

    def test_allow_fallback_false_turns_an_outage_into_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        for key in dem.SOURCE_CHAIN:
            monkeypatch.setitem(dem._PROVIDERS, key, make_fake_provider(calls, key, fail=True))
        with pytest.raises(DemFetchError) as excinfo:
            fetch_dem(SMALL_BBOX, tmp_path / "cache", timeout=5.0, allow_fallback=False)
        assert calls == ["copernicus_glo30"]
        assert len(excinfo.value.attempts) == 1
        assert "simulated outage" in str(excinfo.value)

    def test_every_source_failing_reports_every_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        for key in dem.SOURCE_CHAIN:
            monkeypatch.setitem(dem._PROVIDERS, key, make_fake_provider(calls, key, fail=True))
        with pytest.raises(DemFetchError) as excinfo:
            fetch_dem(SMALL_BBOX, tmp_path / "cache", timeout=5.0)
        assert calls == list(dem.SOURCE_CHAIN)
        assert len(excinfo.value.attempts) == len(dem.SOURCE_CHAIN)
        assert not list((tmp_path / "cache").glob("*.tif"))

    def test_read_sidecar_on_a_plain_raster_returns_empty(self, synthetic_path: Path) -> None:
        assert read_sidecar(synthetic_path) == {}
        assert read_sidecar(synthetic_path.with_suffix(".json")) == {}


# --------------------------------------------------------------------------- #
# network
# --------------------------------------------------------------------------- #


@pytest.mark.network
def test_copernicus_fetch_and_cache_hit_ivanovo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Download the four Copernicus tiles around Ivanovo, then prove the cache.

    The bbox deliberately straddles 41 E and 57 N so a single call exercises the
    multi-tile mosaic and both seams. Elevations must be metres above sea level:
    Ivanovo sits at ~110-130 m in a lowland that never leaves 50..250 m.
    """
    cache = tmp_path / "cache"
    path = fetch_dem(SMALL_BBOX, cache, timeout=300.0)

    entry = read_sidecar(path)
    assert entry["source"] == "copernicus_glo30"
    assert entry["fallback_used"] is False
    assert len(entry["source_urls"]) == 4  # the bbox spans four 1x1 degree tiles
    assert sum(payload["bytes"] for payload in entry["payloads"]) > 100_000_000
    assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    reader = DemReader(path)
    assert reader.crs_epsg == 4326
    assert reader.source_key == "copernicus_glo30"
    bounds = reader.bounds
    assert (bounds.west, bounds.south) <= (SMALL_BBOX.west, SMALL_BBOX.south)
    assert (bounds.east, bounds.north) >= (SMALL_BBOX.east, SMALL_BBOX.north)

    grid, cell_m = reader.window_array(SMALL_BBOX, 32)
    assert grid.shape == (32, 32)
    assert not np.isnan(grid).any()
    assert 50.0 < float(np.mean(grid)) < 250.0
    assert 0.0 < float(np.min(grid)) < 250.0
    assert 50.0 < float(np.max(grid)) < 400.0
    assert 10.0 < cell_m < 200.0  # 32 cells over a ~3.0 x 4.4 km box: ~117 m each

    ivanovo = reader.sample(*IVANOVO)
    assert 50.0 < ivanovo < 250.0

    # Second call: same file, same metadata, and provably no network access.
    fetched_at = entry["fetched_utc"]
    stamp = path.stat().st_mtime_ns

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the second fetch_dem call must be served from the cache")

    monkeypatch.setattr(dem, "_download", explode)
    again = fetch_dem(SMALL_BBOX, cache, timeout=300.0)
    assert again == path
    assert again.stat().st_mtime_ns == stamp
    assert read_sidecar(again)["fetched_utc"] == fetched_at


@pytest.mark.network
def test_terrain_tiles_fallback_matches_copernicus(tmp_path: Path) -> None:
    """The documented fallback must really deliver metres, not a stub.

    Same bbox as the Copernicus test, fetched explicitly from the anonymous AWS
    Terrain Tiles bucket and reprojected from EPSG:3857; its elevations must
    agree with the primary source to within a couple of tens of metres.
    """
    cache = tmp_path / "cache"
    path = fetch_dem(SMALL_BBOX, cache, source="aws_terrain_tiles", timeout=300.0)
    entry = read_sidecar(path)
    assert entry["source"] == "aws_terrain_tiles"
    assert entry["provider_detail"]["reprojected_from"] == "EPSG:3857"
    assert entry["provider_detail"]["zoom"] == 11  # ~20.8 m ground at 57 N
    assert all("elevation-tiles-prod" in url for url in entry["source_urls"])

    reader = DemReader(path)
    assert reader.crs_epsg == 4326
    grid, _ = reader.window_array(SMALL_BBOX, 32)
    mean = float(np.mean(grid))
    assert not math.isnan(mean)
    assert 50.0 < mean < 250.0
    assert 50.0 < reader.sample(*IVANOVO) < 250.0
