"""Sector grid: the spatial indexing used by every stage of the pipeline.

The region is far too large to treat as one object graph, so everything the
pipeline produces is bucketed into square sectors. A sector is the unit of
caching, of validation, and eventually of runtime streaming.

Sector identifiers encode signed grid indices::

    sector_x0000_y0000     the sector containing the world origin
    sector_x-012_y0108     twelve sectors west, 108 north

The sign is part of a fixed-width field so that identifiers stay sortable,
parseable and unambiguous even though the world origin sits inside the region
(which necessarily produces negative indices in every direction).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

#: Matches identifiers produced by :meth:`SectorId.__str__`. The digit run is
#: at least three because a four-character zero-padded field loses one position
#: to the minus sign for negative indices (``-012``).
SECTOR_ID_RE = re.compile(r"^sector_x(-?\d{3,})_y(-?\d{3,})$")


@dataclass(frozen=True, slots=True, order=True)
class SectorId:
    """Integer coordinates of one sector in the region grid.

    Attributes:
        ix: Sector index along the world X (east) axis. May be negative.
        iy: Sector index along the world Y (north) axis. May be negative.
    """

    ix: int
    iy: int

    def __str__(self) -> str:
        return f"sector_x{self.ix:04d}_y{self.iy:04d}"

    @classmethod
    def parse(cls, value: str) -> SectorId:
        """Parse an identifier produced by :meth:`__str__`.

        Args:
            value: Identifier such as ``"sector_x-012_y0108"``.

        Returns:
            The parsed identifier.

        Raises:
            ValueError: If ``value`` is not a valid sector identifier.
        """
        match = SECTOR_ID_RE.match(value)
        if match is None:
            raise ValueError(f"not a sector id: {value!r}")
        return cls(ix=int(match.group(1)), iy=int(match.group(2)))


@dataclass(frozen=True, slots=True)
class SectorBounds:
    """Axis-aligned metric footprint of one sector.

    Attributes:
        x0: Western edge, metres east of the world origin.
        y0: Southern edge, metres north of the world origin.
        x1: Eastern edge.
        y1: Northern edge.
    """

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width_m(self) -> float:
        """East-west extent in metres."""
        return self.x1 - self.x0

    @property
    def height_m(self) -> float:
        """North-south extent in metres."""
        return self.y1 - self.y0

    @property
    def center(self) -> tuple[float, float]:
        """Centre point ``(x, y)`` in metres."""
        return (0.5 * (self.x0 + self.x1), 0.5 * (self.y0 + self.y1))

    def as_tuple(self) -> tuple[float, float, float, float]:
        """Return ``(x0, y0, x1, y1)``."""
        return (self.x0, self.y0, self.x1, self.y1)


class SectorGrid:
    """Maps between metric world coordinates and sector identifiers.

    Args:
        size_m: Sector edge length in metres. Must be positive.
        origin_x: World X of the grid's origin corner.
        origin_y: World Y of the grid's origin corner.

    Raises:
        ValueError: If ``size_m`` is not positive.
    """

    __slots__ = ("_origin_x", "_origin_y", "_size_m")

    def __init__(self, size_m: float, origin_x: float = 0.0, origin_y: float = 0.0) -> None:
        if not math.isfinite(size_m) or size_m <= 0:
            raise ValueError(f"sector size must be a positive finite number, got {size_m!r}")
        self._size_m = float(size_m)
        self._origin_x = float(origin_x)
        self._origin_y = float(origin_y)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"SectorGrid(size_m={self._size_m}, origin=({self._origin_x}, {self._origin_y}))"

    @property
    def size_m(self) -> float:
        """Sector edge length in metres."""
        return self._size_m

    @property
    def origin(self) -> tuple[float, float]:
        """The grid's origin corner in metres."""
        return (self._origin_x, self._origin_y)

    def sector_of(self, x: float, y: float) -> SectorId:
        """Return the sector containing a world point.

        Args:
            x: Metres east of the world origin.
            y: Metres north of the world origin.

        Returns:
            The containing sector.
        """
        return SectorId(
            ix=math.floor((x - self._origin_x) / self._size_m),
            iy=math.floor((y - self._origin_y) / self._size_m),
        )

    def bounds_of(self, sector: SectorId) -> SectorBounds:
        """Return the metric footprint of a sector.

        Args:
            sector: Sector to describe.

        Returns:
            Its axis-aligned bounds in metres.
        """
        x0 = self._origin_x + sector.ix * self._size_m
        y0 = self._origin_y + sector.iy * self._size_m
        return SectorBounds(x0=x0, y0=y0, x1=x0 + self._size_m, y1=y0 + self._size_m)

    def center_of(self, sector: SectorId) -> tuple[float, float]:
        """Return the centre point of a sector in metres."""
        return self.bounds_of(sector).center

    def sectors_in_bounds(self, x0: float, y0: float, x1: float, y1: float) -> Iterator[SectorId]:
        """Iterate every sector overlapping an axis-aligned metric rectangle.

        Args:
            x0: Western edge in metres.
            y0: Southern edge in metres.
            x1: Eastern edge in metres.
            y1: Northern edge in metres.

        Yields:
            Sectors in row-major order (south to north, west to east).

        Raises:
            ValueError: If the rectangle is inverted.
        """
        if x1 < x0 or y1 < y0:
            raise ValueError(f"inverted rectangle: ({x0}, {y0}, {x1}, {y1})")
        lo = self.sector_of(x0, y0)
        hi = self.sector_of(x1, y1)
        for iy in range(lo.iy, hi.iy + 1):
            for ix in range(lo.ix, hi.ix + 1):
                yield SectorId(ix, iy)

    def count_in_bounds(self, x0: float, y0: float, x1: float, y1: float) -> int:
        """Count sectors overlapping a metric rectangle."""
        return sum(1 for _ in self.sectors_in_bounds(x0, y0, x1, y1))

    def neighbours(self, sector: SectorId, radius: int) -> Iterator[SectorId]:
        """Iterate sectors within a Chebyshev ``radius`` of ``sector``.

        Args:
            sector: Centre sector.
            radius: Number of sectors outwards. ``0`` yields only ``sector``.

        Yields:
            Neighbouring sectors, nearest row first.

        Raises:
            ValueError: If ``radius`` is negative.
        """
        if radius < 0:
            raise ValueError(f"radius must be >= 0, got {radius}")
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                yield SectorId(sector.ix + dx, sector.iy + dy)

    def chunk_index(self, sector: SectorId, chunk: int) -> tuple[int, int]:
        """Return the ``(cx, cy)`` index of the chunk holding ``sector``.

        Chunks group sectors into larger units for build scheduling and for
        keeping file counts manageable.

        Args:
            sector: Sector to locate.
            chunk: Sectors per chunk edge. Must be positive.

        Returns:
            The chunk index.

        Raises:
            ValueError: If ``chunk`` is not positive.
        """
        if chunk <= 0:
            raise ValueError(f"chunk must be positive, got {chunk}")
        return (math.floor(sector.ix / chunk), math.floor(sector.iy / chunk))
