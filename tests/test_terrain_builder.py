"""Tests for the terrain builder.

The road-grading tests matter most. BeamNG draws roads as decals over the
terrain and the terrain is the collision surface, so if grading is wrong the
player sees asphalt and drives on a ploughed field. The invariants checked here
are the ones that guarantee the opposite: the carriageway ends up flat and at
the road's own profile, the grade is respected, and the surrounding ground is
left alone.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from ivanovo2beam.beamng.ter import HOLE, TerrainFile
from ivanovo2beam.gis.projection import BBox, make_projection
from ivanovo2beam.terrain.builder import (
    RoadCorridor,
    build_terrain,
    derive_road_profile,
    enforce_max_grade,
    grade_corridors,
    resample_polyline,
    sample_dem_onto_grid,
    smooth_profile,
)

BBOX = BBox(west=39.0, south=56.1, east=43.2, north=57.8)


@pytest.fixture(scope="module")
def projection():
    return make_projection(BBOX, origin_lon=40.97393, origin_lat=56.99987)


class FlatDem:
    """A DEM that returns a constant elevation."""

    def __init__(self, elevation: float = 120.0) -> None:
        self.elevation = elevation
        self.calls = 0

    def sample_array(self, lons, lats):
        self.calls += 1
        return np.full(np.shape(lons), self.elevation, dtype=np.float64)


class SlopedDem:
    """A DEM sloping north at a fixed gradient, for profile tests."""

    def __init__(self, base: float = 100.0, per_degree_lat: float = 1000.0) -> None:
        self.base = base
        self.per_degree_lat = per_degree_lat

    def sample_array(self, lons, lats):
        return self.base + (np.asarray(lats, dtype=np.float64) - 57.0) * self.per_degree_lat


class TestSampleDemOntoGrid:
    def test_row_zero_is_the_southern_edge(self, projection) -> None:
        """Row 0 must be minimum Y, matching the BeamNG .ter convention."""

        class LatRecorder:
            def __init__(self) -> None:
                self.lats: list[float] = []

            def sample_array(self, lons, lats):
                self.lats.append(float(np.ravel(lats)[0]))
                return np.zeros(np.shape(lons))

        dem = LatRecorder()
        sample_dem_onto_grid(dem, projection, 0.0, 0.0, 4, 1000.0)
        assert dem.lats == sorted(dem.lats), "latitude must increase with row index"

    def test_grid_coordinates_round_trip_through_the_projection(self, projection) -> None:
        """Cell (row, col) must sit at the world point the caller expects.

        ``sample_array`` receives degrees, so each captured point is converted
        back to the world plane before comparing.
        """
        captured: list[tuple[float, float]] = []

        class Capture:
            def sample_array(self, lons, lats):
                captured.append((float(np.ravel(lons)[0]), float(np.ravel(lats)[0])))
                return np.zeros(np.shape(lons))

        sample_dem_onto_grid(Capture(), projection, -1000.0, -2000.0, 3, 500.0)
        assert len(captured) == 3  # one call per row
        expected = [(-1000.0, -2000.0), (-1000.0, -1500.0), (-1000.0, -1000.0)]
        for (lon, lat), (want_x, want_y) in zip(captured, expected, strict=True):
            got_x, got_y = projection.geo_to_world(lon, lat)
            assert got_x == pytest.approx(want_x, abs=0.01)
            assert got_y == pytest.approx(want_y, abs=0.01)

    def test_shape_and_dtype(self, projection) -> None:
        result = sample_dem_onto_grid(FlatDem(150.0), projection, 0.0, 0.0, 8, 100.0)
        assert result.shape == (8, 8)
        assert result.dtype == np.float64
        assert np.all(result == 150.0)


class TestResamplePolyline:
    def test_straight_line_spacing(self) -> None:
        points = np.array([[0.0, 0.0], [100.0, 0.0]])
        resampled, arc = resample_polyline(points, 10.0)
        assert resampled.shape[0] == 11
        assert arc[0] == 0.0
        assert arc[-1] == pytest.approx(100.0)

    def test_follows_a_corner(self) -> None:
        points = np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]])
        resampled, arc = resample_polyline(points, 25.0)
        assert resampled[0].tolist() == [0.0, 0.0]
        assert resampled[-1].tolist() == [100.0, 100.0]
        assert arc[-1] == pytest.approx(200.0)

    def test_rejects_non_positive_step(self) -> None:
        with pytest.raises(ValueError, match="step_m must be positive"):
            resample_polyline(np.array([[0.0, 0.0], [1.0, 1.0]]), 0.0)

    def test_degenerate_polyline_does_not_crash(self) -> None:
        points = np.array([[5.0, 5.0], [5.0, 5.0]])
        resampled, arc = resample_polyline(points, 10.0)
        assert resampled.shape[0] >= 1
        assert arc[0] == 0.0


class TestSmoothProfile:
    def test_reduces_noise(self) -> None:
        z = np.array([0.0, 10.0, 0.0, 10.0, 0.0, 10.0, 0.0])
        smoothed = smooth_profile(z, 5)
        assert smoothed.std() < z.std()

    def test_preserves_a_constant_profile(self) -> None:
        z = np.full(20, 42.0)
        np.testing.assert_allclose(smooth_profile(z, 7), z)

    def test_preserves_length_and_endpoints_approximately(self) -> None:
        z = np.arange(20, dtype=np.float64)
        smoothed = smooth_profile(z, 5)
        assert smoothed.shape == z.shape
        # Edge handling shrinks the window, so endpoints stay close to the ramp.
        assert smoothed[0] == pytest.approx(z[0], abs=2.0)
        assert smoothed[-1] == pytest.approx(z[-1], abs=2.0)

    def test_short_input_is_returned_unchanged(self) -> None:
        z = np.array([1.0, 2.0])
        np.testing.assert_array_equal(smooth_profile(z, 9), z)

    def test_window_below_three_is_a_no_op(self) -> None:
        z = np.array([1.0, 5.0, 1.0])
        np.testing.assert_array_equal(smooth_profile(z, 1), z)


class TestEnforceMaxGrade:
    def test_clamps_a_steep_ramp(self) -> None:
        z = np.array([0.0, 100.0, 200.0, 300.0])
        limited = enforce_max_grade(z, spacing_m=10.0, max_grade=0.1)
        deltas = np.abs(np.diff(limited))
        assert deltas.max() <= 0.1 * 10.0 + 1e-9

    def test_preserves_a_gentle_profile(self) -> None:
        z = np.array([0.0, 1.0, 2.0, 3.0])
        limited = enforce_max_grade(z, spacing_m=10.0, max_grade=1.0)
        np.testing.assert_allclose(limited, z)

    def test_result_never_exceeds_the_grade_in_either_direction(self) -> None:
        rng = np.random.default_rng(11)
        z = np.cumsum(rng.normal(0.0, 5.0, size=200))
        limited = enforce_max_grade(z, spacing_m=5.0, max_grade=0.08)
        assert np.abs(np.diff(limited)).max() <= 0.08 * 5.0 + 1e-9

    def test_preserves_overall_trend(self) -> None:
        """Clamping must shape the profile, not flatten it to a constant."""
        z = np.linspace(0.0, 50.0, 21)
        limited = enforce_max_grade(z, spacing_m=10.0, max_grade=0.5)
        assert limited[-1] > limited[0]

    @pytest.mark.parametrize(("spacing", "grade"), [(0.0, 0.1), (-1.0, 0.1), (10.0, 0.0), (10.0, -1.0)])
    def test_rejects_bad_parameters(self, spacing: float, grade: float) -> None:
        with pytest.raises(ValueError):
            enforce_max_grade(np.array([0.0, 1.0]), spacing, grade)

    def test_single_sample(self) -> None:
        limited = enforce_max_grade(np.array([7.0]), 10.0, 0.1)
        np.testing.assert_allclose(limited, [7.0])


class TestDeriveRoadProfile:
    def test_profile_matches_a_flat_dem(self, projection) -> None:
        points = np.array([[0.0, 0.0], [500.0, 0.0]])
        resampled, profile = derive_road_profile(FlatDem(123.0), projection, points)
        assert resampled.shape[0] == profile.shape[0]
        np.testing.assert_allclose(profile, 123.0, atol=1e-6)

    def test_profile_is_grade_limited_over_a_cliff(self, projection) -> None:
        class CliffDem:
            def sample_array(self, lons, lats):
                lats = np.asarray(lats, dtype=np.float64)
                return np.where(lats > 57.0002, 400.0, 100.0)

        points = np.array([[0.0, -3000.0], [0.0, 3000.0]])
        _, profile = derive_road_profile(
            CliffDem(), projection, points, sample_step_m=5.0, smooth_window_m=50.0, max_grade=0.1
        )
        assert np.abs(np.diff(profile)).max() <= 0.1 * 5.0 + 1e-6

    def test_nan_gaps_are_interpolated(self, projection) -> None:
        class GapDem:
            def sample_array(self, lons, lats):
                out = np.full(np.shape(lons), 100.0)
                out[..., ::3] = np.nan
                return out

        points = np.array([[0.0, 0.0], [500.0, 0.0]])
        _, profile = derive_road_profile(GapDem(), projection, points)
        assert np.all(np.isfinite(profile))

    def test_all_nan_raises(self, projection) -> None:
        class NoDataDem:
            def sample_array(self, lons, lats):
                return np.full(np.shape(lons), np.nan)

        with pytest.raises(ValueError, match="no finite elevations"):
            derive_road_profile(NoDataDem(), projection, np.array([[0.0, 0.0], [10.0, 0.0]]))


class TestRoadCorridorValidation:
    def test_rejects_bad_shape(self) -> None:
        with pytest.raises(ValueError, match=r"must be \(n, 2\)"):
            RoadCorridor(points=np.zeros((3, 3)), half_width_m=3.0, corridor_half_width_m=10.0)

    def test_rejects_single_point(self) -> None:
        with pytest.raises(ValueError, match="at least 2 points"):
            RoadCorridor(points=np.zeros((1, 2)), half_width_m=3.0, corridor_half_width_m=10.0)

    def test_rejects_non_positive_half_width(self) -> None:
        with pytest.raises(ValueError, match="half_width_m must be positive"):
            RoadCorridor(points=np.zeros((2, 2)), half_width_m=0.0, corridor_half_width_m=10.0)

    def test_rejects_corridor_narrower_than_the_road(self) -> None:
        with pytest.raises(ValueError, match="must be at least"):
            RoadCorridor(points=np.zeros((2, 2)), half_width_m=10.0, corridor_half_width_m=5.0)

    def test_accepts_a_corridor_equal_to_the_road(self) -> None:
        corridor = RoadCorridor(points=np.zeros((2, 2)), half_width_m=5.0, corridor_half_width_m=5.0)
        assert corridor.half_width_m == 5.0

    def test_rejects_mismatched_profile_length(self) -> None:
        with pytest.raises(ValueError, match="surface_z must have"):
            RoadCorridor(
                points=np.zeros((3, 2)),
                half_width_m=3.0,
                corridor_half_width_m=10.0,
                surface_z=np.zeros(2),
            )

    def test_length(self) -> None:
        corridor = RoadCorridor(
            points=np.array([[0.0, 0.0], [300.0, 400.0]]),
            half_width_m=3.0,
            corridor_half_width_m=10.0,
        )
        assert corridor.length_m == pytest.approx(500.0)


class TestGradeCorridors:
    """The heart of the pipeline: does a road actually become drivable?"""

    def _grid(self, size: int = 64, elevation: float = 100.0):
        return np.full((size, size), elevation, dtype=np.float64)

    def _layers(self, size: int = 64):
        return np.zeros((size, size), dtype=np.uint8)

    def test_flattens_a_carriageway_across_a_slope(self) -> None:
        size, cell = 64, 10.0
        # Slope the terrain steeply across the road's path.
        rows = np.arange(size, dtype=np.float64).reshape(size, 1)
        elevations = np.broadcast_to(100.0 + rows * 5.0, (size, size)).copy()
        layers = self._layers(size)
        corridor = RoadCorridor(
            points=np.array([[0.0, 320.0], [630.0, 320.0]]),
            half_width_m=5.0,
            corridor_half_width_m=20.0,
            surface_z=np.array([150.0, 150.0]),
        )
        graded, stats = grade_corridors(
            elevations,
            layers,
            {"Asphalt": 5, "Gravel": 8},
            [corridor],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=cell,
        )
        assert graded > 0
        assert stats["cells_touched"] > 0
        # Cells in the middle of the carriageway must be flat at the road height.
        row = 32
        centre = elevations[row, 30:35]
        np.testing.assert_allclose(centre, 150.0, atol=0.5)

    def test_leaves_terrain_far_from_the_road_untouched(self) -> None:
        size, cell = 64, 10.0
        rows = np.arange(size, dtype=np.float64).reshape(size, 1)
        elevations = np.broadcast_to(100.0 + rows * 5.0, (size, size)).copy()
        before = elevations.copy()
        corridor = RoadCorridor(
            points=np.array([[0.0, 100.0], [100.0, 100.0]]),
            half_width_m=4.0,
            corridor_half_width_m=15.0,
            surface_z=np.array([100.0, 100.0]),
        )
        grade_corridors(
            elevations,
            self._layers(size),
            {"Asphalt": 1, "Gravel": 2},
            [corridor],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=cell,
        )
        # Row 60 is 500 m north of the road; far outside a 15 m corridor.
        np.testing.assert_allclose(elevations[60], before[60])

    def test_paints_carriageway_and_shoulder_materials(self) -> None:
        size, cell = 64, 10.0
        elevations = self._grid(size)
        layers = self._layers(size)
        corridor = RoadCorridor(
            points=np.array([[0.0, 320.0], [630.0, 320.0]]),
            half_width_m=5.0,
            corridor_half_width_m=25.0,
            surface_z=np.array([100.0, 100.0]),
        )
        grade_corridors(
            elevations,
            layers,
            {"Asphalt": 5, "Gravel": 8},
            [corridor],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=cell,
        )
        assert (layers == 5).any(), "carriageway was not painted"
        assert (layers == 8).any(), "shoulder was not painted"
        assert (layers == 0).any(), "untouched terrain must keep the background material"

    def test_grade_terrain_false_makes_no_change(self) -> None:
        size = 32
        elevations = self._grid(size)
        before = elevations.copy()
        corridor = RoadCorridor(
            points=np.array([[0.0, 100.0], [200.0, 100.0]]),
            half_width_m=4.0,
            corridor_half_width_m=20.0,
            grade_terrain=False,
            surface_z=np.array([500.0, 500.0]),
        )
        graded, _ = grade_corridors(
            elevations,
            self._layers(size),
            {"Asphalt": 1},
            [corridor],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=10.0,
        )
        assert graded == 0
        np.testing.assert_array_equal(elevations, before)

    def test_corridor_without_a_profile_is_skipped(self) -> None:
        size = 32
        elevations = self._grid(size)
        corridor = RoadCorridor(
            points=np.array([[0.0, 100.0], [200.0, 100.0]]),
            half_width_m=4.0,
            corridor_half_width_m=20.0,
            surface_z=None,
        )
        graded, _ = grade_corridors(
            elevations,
            self._layers(size),
            {"Asphalt": 1},
            [corridor],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=10.0,
        )
        assert graded == 0

    def test_higher_priority_road_dominates_a_junction(self) -> None:
        """At a crossing the surface must favour the more important road."""
        size, cell = 80, 5.0
        elevations = self._grid(size, 100.0)
        layers = self._layers(size)
        primary = RoadCorridor(
            points=np.array([[0.0, 200.0], [395.0, 200.0]]),
            half_width_m=5.0,
            corridor_half_width_m=20.0,
            priority=80,
            surface_z=np.array([120.0, 120.0]),
            key="primary",
        )
        residential = RoadCorridor(
            points=np.array([[200.0, 0.0], [200.0, 395.0]]),
            half_width_m=3.0,
            corridor_half_width_m=15.0,
            priority=45,
            surface_z=np.array([90.0, 90.0]),
            key="residential",
        )
        grade_corridors(
            elevations,
            layers,
            {"Asphalt": 1},
            [residential, primary],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=cell,
        )
        junction = elevations[40, 40]
        # Weighted mean of 120 and 90 is 105; the primary must pull it above that.
        assert junction > 105.0

    def test_two_parallel_roads_do_not_disturb_each_other(self) -> None:
        size, cell = 64, 10.0
        elevations = self._grid(size, 100.0)
        layers = self._layers(size)
        a = RoadCorridor(
            points=np.array([[0.0, 100.0], [630.0, 100.0]]),
            half_width_m=4.0,
            corridor_half_width_m=15.0,
            surface_z=np.array([100.0, 100.0]),
        )
        b = RoadCorridor(
            points=np.array([[0.0, 500.0], [630.0, 500.0]]),
            half_width_m=4.0,
            corridor_half_width_m=15.0,
            surface_z=np.array([200.0, 200.0]),
        )
        grade_corridors(
            elevations,
            layers,
            {"Asphalt": 1},
            [a, b],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=cell,
        )
        np.testing.assert_allclose(elevations[10, 30], 100.0, atol=0.5)
        np.testing.assert_allclose(elevations[50, 30], 200.0, atol=0.5)

    def test_road_near_the_grid_edge_is_handled(self) -> None:
        """Window clipping must not raise when a corridor leaves the grid.

        The centreline runs along ``y = 0``, so row 0 is the carriageway itself
        and the corridor extends below the grid. Row 0 must land exactly on the
        road profile; rows further in are shoulder and taper towards the
        surrounding 100 m terrain.
        """
        size, cell = 32, 10.0
        elevations = self._grid(size)
        corridor = RoadCorridor(
            points=np.array([[-500.0, 0.0], [1000.0, 0.0]]),
            half_width_m=4.0,
            corridor_half_width_m=20.0,
            surface_z=np.array([50.0, 50.0]),
        )
        graded, _ = grade_corridors(
            elevations,
            self._layers(size),
            {"Asphalt": 1},
            [corridor],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=cell,
        )
        assert graded > 0
        np.testing.assert_allclose(elevations[0], 50.0, atol=0.01)
        # The shoulder rises smoothly back towards the untouched terrain.
        assert elevations[0].min() < elevations[1].min() < elevations[3].min()
        np.testing.assert_allclose(elevations[5], 100.0)


class TestBuildTerrain:
    def test_flat_dem_produces_a_valid_terrain_file(self, projection) -> None:
        build = build_terrain(
            FlatDem(120.0),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=10.0,
            ground_margin_m=5.0,
        )
        assert build.grid_size == 128
        assert build.extent_m == pytest.approx(1270.0)
        assert build.materials[0] == "Grass"
        assert build.stats["dem_min_m"] == pytest.approx(120.0)
        assert build.stats["dem_max_m"] == pytest.approx(120.0)

        terrain = build.to_terrain_file()
        assert terrain.size == 128
        assert terrain.materials == build.materials
        # A flat DEM must quantise to a single stored height.
        assert len(np.unique(terrain.heights)) == 1

    def test_elevations_survive_the_round_trip(self, projection) -> None:
        build = build_terrain(
            SlopedDem(100.0, per_degree_lat=500.0),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=60.0,
            ground_margin_m=5.0,
        )
        terrain = build.to_terrain_file()
        restored = terrain.to_elevations(build.base_z, build.max_height)
        step = build.max_height / 65536.0
        assert np.abs(restored - build.elevations).max() <= step

    def test_base_z_sits_below_the_lowest_elevation(self, projection) -> None:
        build = build_terrain(
            SlopedDem(100.0),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=60.0,
            ground_margin_m=5.0,
        )
        assert build.base_z <= build.elevations.min()
        assert build.base_z + build.max_height >= build.elevations.max()

    def test_explicit_max_height_is_honoured(self, projection) -> None:
        build = build_terrain(
            FlatDem(100.0),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=10.0,
            max_height_m=250.0,
        )
        assert build.max_height == pytest.approx(250.0)

    def test_vertical_step_is_millimetre_scale(self, projection) -> None:
        build = build_terrain(
            FlatDem(100.0),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=10.0,
        )
        assert build.max_height / 65536.0 < 0.01

    @pytest.mark.parametrize("grid", [64, 100, 8192 * 2])
    def test_rejects_invalid_grid_size(self, projection, grid: int) -> None:
        with pytest.raises(ValueError, match="power of two"):
            build_terrain(
                FlatDem(),
                projection,
                origin_x=0.0,
                origin_y=0.0,
                grid_size=grid,
                cell_size_m=10.0,
            )

    def test_rejects_non_positive_cell_size(self, projection) -> None:
        with pytest.raises(ValueError, match="cell_size_m must be positive"):
            build_terrain(FlatDem(), projection, origin_x=0.0, origin_y=0.0, grid_size=128, cell_size_m=0.0)

    def test_all_nan_dem_is_rejected(self, projection) -> None:
        class NoData:
            def sample_array(self, lons, lats):
                return np.full(np.shape(lons), np.nan)

        with pytest.raises(ValueError, match="no finite elevations"):
            build_terrain(NoData(), projection, origin_x=0.0, origin_y=0.0, grid_size=128, cell_size_m=10.0)

    def test_gaps_are_filled(self, projection) -> None:
        class GapDem:
            def sample_array(self, lons, lats):
                out = np.full(np.shape(lons), 100.0)
                out[..., 0] = np.nan
                return out

        build = build_terrain(
            GapDem(), projection, origin_x=0.0, origin_y=0.0, grid_size=128, cell_size_m=10.0
        )
        assert np.all(np.isfinite(build.elevations))
        assert build.stats["dem_gap_cells"] > 0

    def test_corridors_are_graded_and_counted(self, projection) -> None:
        build = build_terrain(
            SlopedDem(100.0, per_degree_lat=2000.0),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=10.0,
            corridors=[
                RoadCorridor(
                    points=np.array([[-100.0, 600.0], [1400.0, 600.0]]),
                    half_width_m=5.0,
                    corridor_half_width_m=20.0,
                    key="test",
                )
            ],
        )
        assert build.graded_cells > 0
        assert build.stats["corridors"] == 1
        assert build.stats["road_corridors"] == 1
        assert (build.layers == build.materials.index("Asphalt")).any()

    def test_deterministic(self, projection) -> None:
        kwargs = {"origin_x": 0.0, "origin_y": 0.0, "grid_size": 128, "cell_size_m": 10.0}
        a = build_terrain(FlatDem(110.0), projection, **kwargs)
        b = build_terrain(FlatDem(110.0), projection, **kwargs)
        np.testing.assert_array_equal(a.elevations, b.elevations)
        np.testing.assert_array_equal(a.layers, b.layers)
        assert a.base_z == b.base_z
        assert a.max_height == b.max_height

    def test_progress_callback_is_invoked(self, projection) -> None:
        calls: list[tuple[float, str]] = []
        build_terrain(
            FlatDem(),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=10.0,
            progress=lambda f, m: calls.append((f, m)),
        )
        assert calls
        assert calls[-1][0] == pytest.approx(0.95)
        assert all(0.0 <= f <= 1.0 for f, _ in calls)

    def test_layer_histogram_covers_every_cell(self, projection) -> None:
        build = build_terrain(
            FlatDem(), projection, origin_x=0.0, origin_y=0.0, grid_size=128, cell_size_m=10.0
        )
        histogram = build.stats["layer_histogram"]
        assert sum(histogram.values()) == 128 * 128

    def test_hole_value_is_never_used_by_the_builder(self, projection) -> None:
        build = build_terrain(
            FlatDem(), projection, origin_x=0.0, origin_y=0.0, grid_size=128, cell_size_m=10.0
        )
        assert int(build.layers.max()) < len(build.materials)
        assert int(build.layers.max()) != HOLE


class TestGeoreferencing:
    """A terrain must sit where the caller said it sits."""

    def test_origin_is_recorded_exactly(self, projection) -> None:
        build = build_terrain(
            FlatDem(),
            projection,
            origin_x=-2559.375,
            origin_y=1234.5,
            grid_size=128,
            cell_size_m=1.25,
        )
        assert build.origin_x == pytest.approx(-2559.375)
        assert build.origin_y == pytest.approx(1234.5)

    def test_extent_matches_grid_and_cell_size(self, projection) -> None:
        build = build_terrain(
            FlatDem(), projection, origin_x=0.0, origin_y=0.0, grid_size=256, cell_size_m=2.5
        )
        assert build.extent_m == pytest.approx(255 * 2.5)
        assert math.isclose(build.extent_m, 637.5, rel_tol=1e-12)

    def test_terrain_file_payload_matches_its_grid(self, projection) -> None:
        build = build_terrain(
            FlatDem(), projection, origin_x=0.0, origin_y=0.0, grid_size=256, cell_size_m=2.5
        )
        terrain: TerrainFile = build.to_terrain_file()
        trailer = 4 + sum(1 + len(m.encode()) for m in terrain.materials)
        assert terrain.payload_size() == 5 + 3 * 256 * 256 + trailer


class TestWaterCarvingAndBankPainting:
    def test_carve_river_channel(self, projection) -> None:
        """River channel must depress terrain to z_water - depth and paint bed materials."""
        from ivanovo2beam.beamng.materials import DEFAULT_MATERIAL_ORDER
        from ivanovo2beam.water.extract import RiverChannel, WaterNetwork

        mat_idx = {key: i for i, key in enumerate(DEFAULT_MATERIAL_ORDER)}
        elevations = np.full((128, 128), 120.0, dtype=np.float64)
        layers = np.zeros((128, 128), dtype=np.uint8)

        # River running along y = 160 m across the grid from x = 0 to 320 m
        river = RiverChannel(
            osm_id=1,
            name="Test River",
            waterway_type="river",
            points=np.array([[0.0, 160.0], [320.0, 160.0]], dtype=np.float64),
            width_m=20.0,  # hw = 10m
            depth_m=2.5,
            z_water=np.array([120.0, 120.0], dtype=np.float64),
        )
        network = WaterNetwork(rivers=[river])

        from ivanovo2beam.terrain.builder import carve_water
        touched, stats = carve_water(
            elevations,
            layers,
            mat_idx,
            network,
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=2.5,
            bank_transition_m=10.0,
        )

        assert touched > 0
        assert stats["rivers_carved"] == 1

        # Check centreline cell at row 64 (y=160m with cell_size=2.5m)
        center_elev = elevations[64, :]
        assert np.allclose(center_elev, 120.0 - 2.5), "River bed should be depressed to z_water - depth"

        # Check materials: center must be Pebbles (submerged bed)
        pebbles_idx = mat_idx["Pebbles"]
        assert (layers[64, :] == pebbles_idx).all(), "Channel bed must be painted as Pebbles"

        # Check banks: Sand (beach) and RiverbankSoil (wet bank) must be present
        sand_idx = mat_idx["Sand"]
        soil_idx = mat_idx["RiverbankSoil"]
        assert (layers == sand_idx).any(), "Shallow banks must include Sand"
        assert (layers == soil_idx).any(), "Wet banks must include RiverbankSoil"

    def test_carve_lake_basin_with_island(self, projection) -> None:
        """Lake basin must depress terrain while strictly preserving island rings."""
        from ivanovo2beam.beamng.materials import DEFAULT_MATERIAL_ORDER
        from ivanovo2beam.water.extract import WaterBody, WaterNetwork

        mat_idx = {k: i for i, k in enumerate(DEFAULT_MATERIAL_ORDER)}
        elevations = np.full((128, 128), 100.0, dtype=np.float64)
        layers = np.zeros((128, 128), dtype=np.uint8)

        # Outer lake ring from 200..800m
        outer = np.array([
            [200.0, 200.0],
            [800.0, 200.0],
            [800.0, 800.0],
            [200.0, 800.0],
            [200.0, 200.0],
        ], dtype=np.float64)

        # Island ring in the center from 450..550m
        island = np.array([
            [450.0, 450.0],
            [550.0, 450.0],
            [550.0, 550.0],
            [450.0, 550.0],
            [450.0, 450.0],
        ], dtype=np.float64)

        lake = WaterBody(
            osm_id=99,
            name="Test Lake with Island",
            water_type="lake",
            outer_ring=outer,
            inner_rings=[island],
            depth_m=3.5,
            z_water=100.0,
        )
        network = WaterNetwork(water_bodies=[lake])

        from ivanovo2beam.terrain.builder import carve_water
        touched, stats = carve_water(
            elevations,
            layers,
            mat_idx,
            network,
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=10.0,
            bank_transition_m=10.0,
        )

        assert touched > 0
        assert stats["water_bodies_carved"] == 1

        # Lake basin at row 30, col 30 (y=300, x=300) should be depressed to 100 - 3.5 = 96.5
        assert elevations[30, 30] == pytest.approx(96.5)
        assert layers[30, 30] == mat_idx["Pebbles"]

        # Island center at row 50, col 50 (y=500, x=500) must NOT be submerged!
        assert elevations[50, 50] == pytest.approx(100.0), "Island elevation must be preserved"
        assert layers[50, 50] != mat_idx["Pebbles"], "Island center must not be painted as submerged pebbles"

    def test_build_terrain_with_water_network(self, projection) -> None:
        from ivanovo2beam.water.extract import RiverChannel, WaterNetwork

        river = RiverChannel(
            osm_id=42,
            name="Uvod",
            waterway_type="river",
            points=np.array([[100.0, 500.0], [900.0, 500.0]], dtype=np.float64),
            width_m=25.0,
            depth_m=2.5,
            z_water=np.array([120.0, 120.0], dtype=np.float64),
        )
        network = WaterNetwork(rivers=[river])

        build = build_terrain(
            FlatDem(120.0),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=10.0,
            water_network=network,
        )
        assert build.stats.get("water_cells", 0) > 0


class TestForestCanopyStripping:
    def test_strips_canopy_jump_inside_woods(self, projection) -> None:
        """Forest canopy stripping must measure edge jump and subtract canopy height."""
        from ivanovo2beam.beamng.materials import DEFAULT_MATERIAL_ORDER
        from ivanovo2beam.terrain.builder import strip_forest_canopy

        mat_idx = {key: i for i, key in enumerate(DEFAULT_MATERIAL_ORDER)}

        # Create a synthetic DEM where woods sit 14m above open ground
        elevations = np.full((128, 128), 100.0, dtype=np.float64)
        # Woods from row 40..80, col 40..80 (x=400..800, y=400..800)
        elevations[40:81, 40:81] = 114.0
        layers = np.zeros((128, 128), dtype=np.uint8)

        wood_poly = np.array([
            [395.0, 395.0],
            [805.0, 395.0],
            [805.0, 805.0],
            [395.0, 805.0],
            [395.0, 395.0],
        ], dtype=np.float64)

        cells, stats = strip_forest_canopy(
            elevations,
            layers,
            mat_idx,
            [wood_poly],
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=10.0,
        )

        assert cells > 0
        assert stats["canopy_height_m"] == pytest.approx(14.0, abs=0.5)

        # Elevation inside woods should now be stripped back to ~100m ground level!
        assert elevations[60, 60] == pytest.approx(100.0, abs=0.5)
        assert layers[60, 60] == mat_idx["ForestFloor"]

    def test_build_terrain_with_forest_canopy_and_road_grading(self, projection) -> None:
        """Road cut through woods must not sit in a 20m trench when canopy is stripped."""
        wood_poly = np.array([
            [200.0, 200.0],
            [600.0, 200.0],
            [600.0, 600.0],
            [200.0, 600.0],
            [200.0, 200.0],
        ], dtype=np.float64)

        # Sloped DEM with forest
        dem = FlatDem(115.0)

        build = build_terrain(
            dem,
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=10.0,
            forest_polygons=[wood_poly],
            canopy_height_m=15.0,
        )
        assert build.stats.get("forest_cells", 0) > 0


class TestLandcoverAndSatelliteSupport:
    def test_paint_landcover(self, projection) -> None:
        from ivanovo2beam.beamng.materials import DEFAULT_MATERIAL_ORDER
        from ivanovo2beam.terrain.builder import paint_landcover

        mat_idx = {key: i for i, key in enumerate(DEFAULT_MATERIAL_ORDER)}
        layers = np.zeros((128, 128), dtype=np.uint8)

        field_poly = np.array([
            [100.0, 100.0],
            [300.0, 100.0],
            [300.0, 300.0],
            [100.0, 300.0],
            [100.0, 100.0],
        ], dtype=np.float64)

        painted = paint_landcover(
            layers,
            mat_idx,
            {"Field": [field_poly]},
            origin_x=0.0,
            origin_y=0.0,
            cell_size_m=10.0,
        )
        assert painted > 0
        assert layers[20, 20] == mat_idx["Field"]

    def test_satellite_base_texture_support(self, projection) -> None:
        sat_path = "/levels/ivanovo_mvp/art/textures/satellite_ortho.dds"
        build = build_terrain(
            FlatDem(110.0),
            projection,
            origin_x=0.0,
            origin_y=0.0,
            grid_size=128,
            cell_size_m=10.0,
            satellite_tex=sat_path,
        )
        assert build.satellite_image == sat_path
        assert build.stats.get("satellite_tex") == sat_path

    def test_terrain_placement_with_base_color_base_tex(self) -> None:
        from ivanovo2beam.beamng.level import TerrainPlacement

        sat_path = "/levels/ivanovo_mvp/art/textures/satellite_ortho.dds"
        placement = TerrainPlacement(
            name="theTerrain",
            origin_x=-1000.0,
            origin_y=-1000.0,
            base_z=100.0,
            max_height=50.0,
            square_size=1.0,
            grid_size=1024,
            terrain_file="levels/ivanovo_mvp/theTerrain.ter",
            base_color_base_tex=sat_path,
        )
        obj = placement.to_object("ivanovo_mvp")
        assert obj["baseColorBaseTex"] == sat_path
