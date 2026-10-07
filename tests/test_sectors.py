"""Tests for the sector grid."""

from __future__ import annotations

import math

import pytest

from ivanovo2beam.sectors.grid import SectorBounds, SectorGrid, SectorId


class TestSectorId:
    def test_format_is_fixed_width_and_signed(self) -> None:
        assert str(SectorId(0, 0)) == "sector_x0000_y0000"
        assert str(SectorId(42, 108)) == "sector_x0042_y0108"
        assert str(SectorId(-12, 108)) == "sector_x-012_y0108"

    @pytest.mark.parametrize("sector", [SectorId(0, 0), SectorId(42, 108), SectorId(-12, -3)])
    def test_round_trip(self, sector: SectorId) -> None:
        assert SectorId.parse(str(sector)) == sector

    def test_ids_sort_lexicographically_like_numbers(self) -> None:
        """Padding must keep ordering stable, which caches and filenames rely on."""
        ids = [SectorId(n, 0) for n in (0, 1, 9, 10, 99, 100)]
        assert [str(i) for i in sorted(ids)] == [str(i) for i in ids]

    @pytest.mark.parametrize("bad", ["", "sector_0000_0000", "x0000_y0000", "sector_x1_y2"])
    def test_rejects_malformed_ids(self, bad: str) -> None:
        with pytest.raises(ValueError, match="not a sector id"):
            SectorId.parse(bad)


class TestSectorGrid:
    def test_rejects_non_positive_size(self) -> None:
        with pytest.raises(ValueError, match="positive finite"):
            SectorGrid(0)
        with pytest.raises(ValueError, match="positive finite"):
            SectorGrid(-1.0)
        with pytest.raises(ValueError, match="positive finite"):
            SectorGrid(math.nan)

    def test_origin_lands_in_sector_zero(self) -> None:
        grid = SectorGrid(2000.0)
        assert grid.sector_of(0.0, 0.0) == SectorId(0, 0)
        assert grid.sector_of(1999.9, 1999.9) == SectorId(0, 0)
        assert grid.sector_of(2000.0, 2000.0) == SectorId(1, 1)

    def test_negative_coordinates_get_negative_indices(self) -> None:
        grid = SectorGrid(2000.0)
        assert grid.sector_of(-0.1, -0.1) == SectorId(-1, -1)
        assert grid.sector_of(-2000.0, -2000.0) == SectorId(-1, -1)
        assert grid.sector_of(-2000.1, -2000.1) == SectorId(-2, -2)

    def test_bounds_are_inverse_of_sector_of(self) -> None:
        grid = SectorGrid(1500.0)
        for x, y in [(0.0, 0.0), (-1234.5, 9876.5), (54321.0, -4321.0)]:
            sector = grid.sector_of(x, y)
            bounds = grid.bounds_of(sector)
            assert bounds.x0 <= x < bounds.x1
            assert bounds.y0 <= y < bounds.y1

    def test_bounds_size_equals_sector_size(self) -> None:
        grid = SectorGrid(2000.0)
        bounds = grid.bounds_of(SectorId(3, -2))
        assert bounds.width_m == pytest.approx(2000.0)
        assert bounds.height_m == pytest.approx(2000.0)

    def test_center_of(self) -> None:
        grid = SectorGrid(2000.0)
        assert grid.center_of(SectorId(0, 0)) == (1000.0, 1000.0)
        assert grid.center_of(SectorId(-1, 0)) == (-1000.0, 1000.0)

    def test_sectors_in_bounds_covers_the_rectangle(self) -> None:
        grid = SectorGrid(1000.0)
        sectors = list(grid.sectors_in_bounds(0.0, 0.0, 1999.0, 1999.0))
        assert len(sectors) == 4
        assert set(sectors) == {SectorId(0, 0), SectorId(1, 0), SectorId(0, 1), SectorId(1, 1)}

    def test_sectors_in_bounds_is_inclusive_at_the_far_edge(self) -> None:
        """A rectangle ending exactly on a boundary must include that sector."""
        grid = SectorGrid(1000.0)
        sectors = list(grid.sectors_in_bounds(0.0, 0.0, 1000.0, 1000.0))
        assert SectorId(1, 1) in sectors

    def test_sectors_in_bounds_rejects_inverted_rectangle(self) -> None:
        grid = SectorGrid(1000.0)
        with pytest.raises(ValueError, match="inverted rectangle"):
            list(grid.sectors_in_bounds(10.0, 0.0, 0.0, 10.0))

    def test_count_in_bounds(self) -> None:
        grid = SectorGrid(500.0)
        assert grid.count_in_bounds(0.0, 0.0, 1499.0, 1499.0) == 9

    def test_region_scale_count(self) -> None:
        """A 220 x 145 km region at 2 km sectors is 111 x 74 sectors."""
        grid = SectorGrid(2000.0)
        # Region spans roughly -110 km to +110 km east, -72 km to +72 km north.
        count = grid.count_in_bounds(-110_000.0, -72_000.0, 110_000.0, 72_000.0)
        assert count == 111 * 73

    def test_neighbours_radius_zero_is_self(self) -> None:
        grid = SectorGrid(1000.0)
        assert list(grid.neighbours(SectorId(5, 5), 0)) == [SectorId(5, 5)]

    def test_neighbours_counts(self) -> None:
        grid = SectorGrid(1000.0)
        assert len(list(grid.neighbours(SectorId(0, 0), 1))) == 9
        assert len(list(grid.neighbours(SectorId(0, 0), 3))) == 49
        assert SectorId(-3, -3) in set(grid.neighbours(SectorId(0, 0), 3))

    def test_neighbours_rejects_negative_radius(self) -> None:
        grid = SectorGrid(1000.0)
        with pytest.raises(ValueError, match="radius must be"):
            list(grid.neighbours(SectorId(0, 0), -1))

    def test_chunk_index_groups_sectors(self) -> None:
        grid = SectorGrid(1000.0)
        assert grid.chunk_index(SectorId(0, 0), 5) == (0, 0)
        assert grid.chunk_index(SectorId(4, 4), 5) == (0, 0)
        assert grid.chunk_index(SectorId(5, 5), 5) == (1, 1)
        # floor division, so negatives chunk consistently
        assert grid.chunk_index(SectorId(-1, -1), 5) == (-1, -1)
        assert grid.chunk_index(SectorId(-5, -5), 5) == (-1, -1)

    def test_chunk_index_rejects_non_positive_chunk(self) -> None:
        grid = SectorGrid(1000.0)
        with pytest.raises(ValueError, match="chunk must be positive"):
            grid.chunk_index(SectorId(0, 0), 0)

    def test_shifted_origin(self) -> None:
        grid = SectorGrid(1000.0, origin_x=-500.0, origin_y=250.0)
        assert grid.sector_of(-500.0, 250.0) == SectorId(0, 0)
        bounds = grid.bounds_of(SectorId(0, 0))
        assert (bounds.x0, bounds.y0) == (-500.0, 250.0)


class TestSectorBounds:
    def test_center_and_tuple(self) -> None:
        bounds = SectorBounds(0.0, 0.0, 2000.0, 1000.0)
        assert bounds.center == (1000.0, 500.0)
        assert bounds.as_tuple() == (0.0, 0.0, 2000.0, 1000.0)
        assert bounds.width_m == 2000.0
        assert bounds.height_m == 1000.0
