"""Tests for the metric world coordinate system.

The central guarantee of this project is *1 game metre == 1 real metre*. These
tests check that guarantee against independent WGS84 geodesic measurements
rather than against the projection's own arithmetic, so a broken PROJ string
cannot pass by being self-consistent.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from ivanovo2beam.gis.projection import (
    BBox,
    RegionProjection,
    geodesic_distance_m,
    make_projection,
    optimal_k0,
)

# Reference places in / near Ivanovo Oblast, WGS84 degrees.
# Coordinates are town-centre points taken from OpenStreetMap.
IVANOVO = (40.97393, 56.99987)
SHUYA = (41.36444, 56.84861)
KINESHMA = (42.12889, 57.43917)
TEYKOVO = (40.54000, 56.85778)
YUZHA = (42.01667, 56.58333)
PLYOS = (41.51000, 57.46000)

#: The oblast, with a small margin so neighbouring road links are included.
OBLAST_BBOX = BBox(west=39.00, south=56.10, east=43.20, north=57.80)


@pytest.fixture(scope="module")
def proj() -> RegionProjection:
    return make_projection(OBLAST_BBOX, origin_lon=IVANOVO[0], origin_lat=IVANOVO[1])


class TestBBox:
    def test_rejects_inverted_longitude(self) -> None:
        with pytest.raises(ValueError, match="west"):
            BBox(west=43.0, south=56.0, east=39.0, north=57.0)

    def test_rejects_inverted_latitude(self) -> None:
        with pytest.raises(ValueError, match="south"):
            BBox(west=39.0, south=57.0, east=43.0, north=56.0)

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            BBox(west=-200.0, south=56.0, east=39.0, north=57.0)

    def test_center_and_helpers(self) -> None:
        box = BBox(west=0.0, south=0.0, east=2.0, north=4.0)
        assert box.center == (1.0, 2.0)
        assert box.as_overpass() == "0.0,0.0,4.0,2.0"
        assert box.as_tuple() == (0.0, 0.0, 2.0, 4.0)
        assert box.contains(1.0, 2.0)
        assert not box.contains(3.0, 2.0)

    def test_from_config_accepts_list_and_mapping(self) -> None:
        from_list = BBox.from_config([39.0, 56.1, 43.2, 57.8])
        from_map = BBox.from_config({"west": 39.0, "south": 56.1, "east": 43.2, "north": 57.8})
        assert from_list == from_map


class TestOptimalK0:
    def test_is_just_below_unity(self) -> None:
        k0 = optimal_k0(OBLAST_BBOX)
        assert 0.999 < k0 < 1.0

    def test_balances_centre_and_edge_error(self) -> None:
        """With the optimal k0 the centre and edge errors should be near-equal."""
        proj = make_projection(OBLAST_BBOX)
        centre_lon, centre_lat = OBLAST_BBOX.center
        centre_err = proj.measure_scale_error(centre_lon, centre_lat)
        edge_err = proj.measure_scale_error(OBLAST_BBOX.east, OBLAST_BBOX.south)
        assert centre_err < 0, "centre must be slightly compressed"
        assert edge_err > 0, "east/south edge must be slightly stretched"
        # Balanced to within a factor of two of each other.
        assert 0.4 < abs(centre_err) / abs(edge_err) < 2.5

    def test_naive_k0_of_one_would_be_worse(self) -> None:
        optimal = make_projection(OBLAST_BBOX).measure_scale_error_over(OBLAST_BBOX, samples=5)
        naive = make_projection(OBLAST_BBOX, scale_factor=1.0).measure_scale_error_over(
            OBLAST_BBOX, samples=5
        )
        assert optimal.max_abs_relative_error < naive.max_abs_relative_error


class TestRoundTrip:
    @pytest.mark.parametrize(
        ("lon", "lat"),
        [IVANOVO, SHUYA, KINESHMA, TEYKOVO, YUZHA, PLYOS],
    )
    def test_geo_world_geo_is_identity(self, proj: RegionProjection, lon: float, lat: float) -> None:
        x, y = proj.geo_to_world(lon, lat)
        lon2, lat2 = proj.world_to_geo(x, y)
        # 1e-9 degrees is ~0.1 mm; far below the data's own precision.
        assert lon2 == pytest.approx(lon, abs=1e-9)
        assert lat2 == pytest.approx(lat, abs=1e-9)

    def test_world_geo_world_is_identity(self, proj: RegionProjection) -> None:
        for x, y in [(0.0, 0.0), (1000.0, -2000.0), (-55_000.0, 31_000.0)]:
            lon, lat = proj.world_to_geo(x, y)
            x2, y2 = proj.geo_to_world(lon, lat)
            assert x2 == pytest.approx(x, abs=1e-6)
            assert y2 == pytest.approx(y, abs=1e-6)

    def test_origin_maps_to_zero(self, proj: RegionProjection) -> None:
        x, y = proj.geo_to_world(*IVANOVO)
        assert x == pytest.approx(0.0, abs=1e-6)
        assert y == pytest.approx(0.0, abs=1e-6)

    def test_origin_is_configured_value(self, proj: RegionProjection) -> None:
        assert proj.origin_lon == pytest.approx(IVANOVO[0])
        assert proj.origin_lat == pytest.approx(IVANOVO[1])


class TestScaleAccuracy:
    """The load-bearing tests: does the world plane agree with reality?"""

    @pytest.mark.parametrize(
        ("name_a", "a", "name_b", "b"),
        [
            ("Ivanovo", IVANOVO, "Shuya", SHUYA),
            ("Ivanovo", IVANOVO, "Kineshma", KINESHMA),
            ("Ivanovo", IVANOVO, "Teykovo", TEYKOVO),
            ("Ivanovo", IVANOVO, "Yuzha", YUZHA),
            ("Shuya", SHUYA, "Kineshma", KINESHMA),
            ("Plyos", PLYOS, "Kineshma", KINESHMA),
        ],
    )
    def test_projected_distance_matches_geodesic(
        self,
        proj: RegionProjection,
        name_a: str,
        a: tuple[float, float],
        name_b: str,
        b: tuple[float, float],
    ) -> None:
        """A route of N real metres must be N game metres, within 0.05%."""
        xa, ya = proj.geo_to_world(*a)
        xb, yb = proj.geo_to_world(*b)
        projected = math.hypot(xb - xa, yb - ya)
        true = geodesic_distance_m(a[0], a[1], b[0], b[1])
        rel = abs(projected / true - 1.0)
        assert rel < 5e-4, (
            f"{name_a}->{name_b}: projected {projected:.1f} m vs geodesic {true:.1f} m "
            f"(relative error {rel:.2e})"
        )

    def test_ivanovo_to_shuya_is_about_29_km_straight_line(self, proj: RegionProjection) -> None:
        """Sanity-band the headline route so a wrong CRS cannot silently pass.

        Ivanovo -> Shuya is ~29 km straight line and ~33 km by road, so a
        projected distance far outside this band means the CRS is wrong.
        """
        xa, ya = proj.geo_to_world(*IVANOVO)
        xb, yb = proj.geo_to_world(*SHUYA)
        km = math.hypot(xb - xa, yb - ya) / 1000.0
        assert 26.0 < km < 34.0, f"Ivanovo->Shuya = {km:.2f} km"

    def test_ivanovo_to_kineshma_is_about_100_km(self, proj: RegionProjection) -> None:
        xa, ya = proj.geo_to_world(*IVANOVO)
        xb, yb = proj.geo_to_world(*KINESHMA)
        km = math.hypot(xb - xa, yb - ya) / 1000.0
        assert 85.0 < km < 125.0

    def test_worst_case_error_over_oblast_is_below_15m_per_100km(self, proj: RegionProjection) -> None:
        report = proj.measure_scale_error_over(OBLAST_BBOX, samples=11)
        assert report.error_metres_per_100km < 15.0, str(report)
        assert report.samples == 121

    def test_scale_error_is_within_one_metre_over_a_sector(self, proj: RegionProjection) -> None:
        """Over a 2 km sector the map must be metric to sub-centimetre level."""
        sector = BBox(west=40.95, south=56.98, east=40.99, north=57.02)
        report = proj.measure_scale_error_over(sector, samples=3)
        # 2 km * 1.1e-4 is about 0.2 m; assert well under a metre.
        assert report.max_abs_relative_error * 2000.0 < 1.0

    def test_conformal_scale_is_azimuth_independent(self, proj: RegionProjection) -> None:
        """A conformal projection gives the same scale in every direction."""
        errors = [proj.measure_scale_error(*SHUYA, azimuth_deg=az) for az in (0, 45, 90, 135, 180)]
        spread = max(errors) - min(errors)
        assert spread < 1e-6, f"non-conformal behaviour, spread={spread:.2e}"


class TestVectorisedTransforms:
    def test_arrays_match_scalars(self, proj: RegionProjection) -> None:
        points = [IVANOVO, SHUYA, KINESHMA, TEYKOVO]
        lons = np.array([p[0] for p in points])
        lats = np.array([p[1] for p in points])
        xs, ys = proj.geo_to_world_array(lons, lats)
        for i, (lon, lat) in enumerate(points):
            sx, sy = proj.geo_to_world(lon, lat)
            assert xs[i] == pytest.approx(sx, abs=1e-6)
            assert ys[i] == pytest.approx(sy, abs=1e-6)

    def test_round_trip_arrays(self, proj: RegionProjection) -> None:
        lons = np.linspace(39.5, 42.5, 17)
        lats = np.linspace(56.3, 57.5, 17)
        xs, ys = proj.geo_to_world_array(lons, lats)
        lons2, lats2 = proj.world_to_geo_array(xs, ys)
        np.testing.assert_allclose(lons2, lons, atol=1e-9)
        np.testing.assert_allclose(lats2, lats, atol=1e-9)

    def test_shape_mismatch_raises(self, proj: RegionProjection) -> None:
        with pytest.raises(ValueError, match="shape mismatch"):
            proj.geo_to_world_array([1.0, 2.0], [1.0])


class TestProjectionValidation:
    def test_rejects_geographic_crs(self) -> None:
        with pytest.raises(ValueError, match="projected"):
            RegionProjection("+proj=longlat +ellps=WGS84 +no_defs", 41.0, 57.0)

    def test_rejects_non_metric_units(self) -> None:
        with pytest.raises(ValueError, match="metric"):
            RegionProjection(
                "+proj=tmerc +lat_0=57 +lon_0=41 +k=1 +ellps=WGS84 +units=ft +no_defs +type=crs",
                41.0,
                57.0,
            )

    def test_from_config_derives_proj_string(self) -> None:
        cfg = {
            "bbox": {"west": 39.0, "south": 56.1, "east": 43.2, "north": 57.8},
            "projection": {
                "central_meridian": 41.1,
                "latitude_of_origin": 57.0,
                "origin": [40.97393, 56.99987],
            },
        }
        proj = RegionProjection.from_config(cfg)
        assert "+proj=tmerc" in proj.proj_string
        assert proj.origin_lon == pytest.approx(40.97393)
        assert proj.measure_scale_error_over(OBLAST_BBOX, samples=5).error_metres_per_100km < 15.0

    def test_from_config_honours_explicit_proj_string(self) -> None:
        cfg = {
            "bbox": {"west": 39.0, "south": 56.1, "east": 43.2, "north": 57.8},
            "projection": {
                "proj_string": (
                    "+proj=tmerc +lat_0=0 +lon_0=39 +k=0.9996 +x_0=500000 +y_0=0 "
                    "+ellps=WGS84 +units=m +no_defs +type=crs"
                )
            },
        }
        proj = RegionProjection.from_config(cfg)
        assert proj.name == "custom"
        assert "+proj=tmerc" in proj.proj_string
        # A point on the 39E central meridian must land on the 500 km false
        # easting and the equator on the 0 m false northing.
        origin_x, origin_y = proj.origin_projected
        x, y = proj.geo_to_world(39.0, 0.0)
        assert (x + origin_x) == pytest.approx(500_000.0, abs=1.0)
        assert (y + origin_y) == pytest.approx(0.0, abs=1.0)
        # The region lies well east of that meridian.
        assert origin_x > 600_000.0


class TestGeodesicHelper:
    def test_known_distance_paris_london(self) -> None:
        """Independent check of the geodesic authority itself."""
        d = geodesic_distance_m(2.3522, 48.8566, -0.1276, 51.5072)
        assert d / 1000.0 == pytest.approx(343.5, abs=2.0)

    def test_zero_distance(self) -> None:
        assert geodesic_distance_m(41.0, 57.0, 41.0, 57.0) == pytest.approx(0.0)
