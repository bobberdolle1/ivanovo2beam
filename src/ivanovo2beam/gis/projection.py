"""Metric world coordinate system for ivanovo2beam.

Why this module exists
----------------------
OpenStreetMap and Copernicus DEM deliver coordinates in WGS84 geographic
degrees (``EPSG:4326``). Degrees are not metres, and a degree of longitude is
only ~61 km at Ivanovo's latitude (56.9 N) whereas a degree of latitude is
~111 km. Feeding lat/lon straight into a game engine would therefore produce a
map that is sheared by a factor of ~1.8 in the east-west direction.

This module converts geographic coordinates into a **local metric plane** in
which one unit is exactly one metre, which is the plane the game world is
built from. The contract is deliberately tiny::

    x, y = projection.geo_to_world(lon, lat)   # metres east / north of origin
    lon, lat = projection.world_to_geo(x, y)   # exact inverse

Projection choice for Ivanovo Oblast
------------------------------------
The oblast spans roughly 39.0-43.2 E and 56.1-57.8 N: about 220 km across and
145 km tall. It straddles the official UTM zone boundary (zone 37 covers
36-42 E, zone 38 covers 42-48 E), so a standard UTM zone would place a third
of the region up to 4 degrees from its central meridian, where the point scale
factor departs from unity by ~+0.032% (about 32 m of error per 100 km driven).

Instead we use a **custom Transverse Mercator whose central meridian is the
region's own centre**. Transverse Mercator point scale along the central
meridian equals ``k0``, and at a distance from the meridian it grows
approximately as ``k0 * (1 + W^2 / 2)`` where ``W`` is the angular half-width
of the region measured at its lowest latitude (the worst case, since
``cos(lat)`` is largest there). Choosing

    k0 = 1 - W^2 / 4

balances the two ends: distances are compressed by ``W^2/4`` at the centre and
stretched by ``W^2/4`` at the region edge. For Ivanovo this gives
``k0 = 0.9998955`` and a worst-case error of about **+/-0.0104%, i.e. +/-10 m
per 100 km**, which is far below anything a driver can perceive (a car is
4.5 m long) and far below the intrinsic accuracy of a 30 m DEM.

Transverse Mercator is also conformal (it preserves local shape and angles)
and keeps north up and meridians straight, which makes the sector grid and the
game build deterministic and easy to reason about.

The projection is fully specified by a PROJ string stored in the region
config, so nothing here is magic: change the config to map another region.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from pyproj import CRS, Geod, Transformer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

#: WGS84 ellipsoid, used for authoritatively measuring real-world distances.
WGS84_GEOD = Geod(ellps="WGS84")

#: Distance used when probing the local scale factor, in metres. 1 km is long
#: enough that geodesic curvature is negligible and short enough that the
#: point scale factor is effectively constant along it.
_SCALE_PROBE_LENGTH_M = 1000.0


@dataclass(frozen=True, slots=True)
class BBox:
    """A geographic bounding box in degrees.

    Attributes:
        west: Minimum longitude (degrees east).
        south: Minimum latitude (degrees north).
        east: Maximum longitude (degrees east).
        north: Maximum latitude (degrees north).
    """

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        if not (self.west < self.east):
            raise ValueError(f"bbox west ({self.west}) must be < east ({self.east})")
        if not (self.south < self.north):
            raise ValueError(f"bbox south ({self.south}) must be < north ({self.north})")
        if not (-180.0 <= self.west <= 180.0 and -180.0 <= self.east <= 180.0):
            raise ValueError("longitudes must be within [-180, 180]")
        if not (-90.0 <= self.south <= 90.0 and -90.0 <= self.north <= 90.0):
            raise ValueError("latitudes must be within [-90, 90]")

    @property
    def center(self) -> tuple[float, float]:
        """Return ``(lon, lat)`` of the box centre."""
        return (0.5 * (self.west + self.east), 0.5 * (self.south + self.north))

    def as_overpass(self) -> str:
        """Return the ``south,west,north,east`` string used by the Overpass API."""
        return f"{self.south},{self.west},{self.north},{self.east}"

    def as_tuple(self) -> tuple[float, float, float, float]:
        """Return ``(west, south, east, north)``."""
        return (self.west, self.south, self.east, self.north)

    def expanded(self, margin_deg: float) -> BBox:
        """Return a copy grown by ``margin_deg`` on every side."""
        return BBox(
            west=self.west - margin_deg,
            south=self.south - margin_deg,
            east=self.east + margin_deg,
            north=self.north + margin_deg,
        )

    def contains(self, lon: float, lat: float) -> bool:
        """Return whether ``(lon, lat)`` lies inside the box (inclusive)."""
        return self.west <= lon <= self.east and self.south <= lat <= self.north

    @classmethod
    def from_config(cls, data: Any) -> BBox:
        """Build a bbox from a config mapping with ``west/south/east/north`` keys."""
        if isinstance(data, (list, tuple)) and len(data) == 4:
            west, south, east, north = (float(v) for v in data)
            return cls(west=west, south=south, east=east, north=north)
        return cls(
            west=float(data["west"]),
            south=float(data["south"]),
            east=float(data["east"]),
            north=float(data["north"]),
        )


@dataclass(frozen=True, slots=True)
class ScaleErrorReport:
    """Result of probing the projection's local scale error over a region.

    Attributes:
        max_abs_relative_error: Largest ``|projected / geodesic - 1|`` found.
        worst_lon: Longitude where the worst error was measured.
        worst_lat: Latitude where the worst error was measured.
        samples: Number of probe locations evaluated.
    """

    max_abs_relative_error: float
    worst_lon: float
    worst_lat: float
    samples: int

    @property
    def error_metres_per_100km(self) -> float:
        """Worst-case distance error expressed in metres per 100 km driven."""
        return self.max_abs_relative_error * 100_000.0

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"max scale error {self.max_abs_relative_error:.3e} "
            f"({self.error_metres_per_100km:.2f} m per 100 km) "
            f"at lon={self.worst_lon:.4f}, lat={self.worst_lat:.4f} "
            f"over {self.samples} samples"
        )


def optimal_k0(bbox: BBox) -> float:
    """Return the central-meridian scale factor minimising regional distortion.

    Distances are compressed by ``W^2/4`` at the central meridian and stretched
    by ``W^2/4`` at the region edge, where ``W`` is the angular half-width of
    the region evaluated at its lowest latitude. See the module docstring.

    Args:
        bbox: Geographic extent the projection must serve.

    Returns:
        The scale factor ``k0`` to place in the PROJ string.
    """
    center_lon, _ = bbox.center
    half_width_deg = max(abs(bbox.east - center_lon), abs(center_lon - bbox.west))
    # Worst case is the southern edge: cos(lat) is largest there.
    cos_lat = math.cos(math.radians(bbox.south))
    w = math.radians(half_width_deg) * cos_lat
    return 1.0 - (w * w) / 4.0


class RegionProjection:
    """Bidirectional WGS84 <-> metric world-plane transform with a local origin.

    The world plane is a right-handed metric ENU plane:

    * ``x`` — metres east of the world origin
    * ``y`` — metres north of the world origin

    Elevation (``z``) is handled by the terrain module and is not part of this
    transform.

    Two independent things happen here and it is important not to conflate them:

    1. **Projection** — ``EPSG:4326`` degrees to metres on the plane. This is a
       fixed, lossless-in-practice bijection over the region.
    2. **Origin shift** — subtracting the projected coordinate of a chosen
       anchor point so game coordinates stay small and numerically precise.
       BeamNG stores world positions as 32-bit floats, so keeping Ivanovo's
       city centre near ``(0, 0)`` matters (see docs/research/beamng_engine_limits.md).

    Args:
        proj_string: PROJ string describing the metric CRS.
        origin_lon: Longitude of the world origin.
        origin_lat: Latitude of the world origin.
        name: Human-readable CRS name, used in metadata and docs.
    """

    __slots__ = (
        "_name",
        "_origin_lat",
        "_origin_lon",
        "_origin_x",
        "_origin_y",
        "_proj_string",
        "_to_geo",
        "_to_world",
    )

    def __init__(
        self,
        proj_string: str,
        origin_lon: float,
        origin_lat: float,
        name: str = "custom",
    ) -> None:
        self._proj_string = proj_string
        self._name = name
        self._origin_lon = float(origin_lon)
        self._origin_lat = float(origin_lat)

        crs = CRS.from_proj4(proj_string)
        if not crs.is_projected:
            raise ValueError("proj_string must describe a projected (metric) CRS")
        units = crs.axis_info[0].unit_name if crs.axis_info else ""
        if units not in {"metre", "meter", "m"}:
            raise ValueError(f"proj_string must be metric, got units={units!r}")

        self._to_world = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        self._to_geo = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

        # Cache the projected origin so the common path is a subtraction.
        self._origin_x, self._origin_y = self._to_world.transform(origin_lon, origin_lat)

    # ------------------------------------------------------------------ dunder

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"RegionProjection(name={self._name!r}, origin=({self._origin_lon}, {self._origin_lat}))"

    # --------------------------------------------------------------- properties

    @property
    def name(self) -> str:
        """Human-readable CRS name."""
        return self._name

    @property
    def proj_string(self) -> str:
        """The PROJ string backing this projection."""
        return self._proj_string

    @property
    def origin_lon(self) -> float:
        """Longitude of the world origin in degrees."""
        return self._origin_lon

    @property
    def origin_lat(self) -> float:
        """Latitude of the world origin in degrees."""
        return self._origin_lat

    @property
    def origin_projected(self) -> tuple[float, float]:
        """The world origin expressed in unshifted projected metres."""
        return (self._origin_x, self._origin_y)

    @property
    def crs(self) -> CRS:
        """The underlying :class:`pyproj.CRS`."""
        return CRS.from_proj4(self._proj_string)

    # ---------------------------------------------------------------- transform

    def geo_to_world(self, lon: float, lat: float) -> tuple[float, float]:
        """Convert a WGS84 coordinate to metric world coordinates.

        Args:
            lon: Longitude in degrees east.
            lat: Latitude in degrees north.

        Returns:
            ``(x, y)`` in metres east/north of the world origin.
        """
        px, py = self._to_world.transform(lon, lat)
        return (px - self._origin_x, py - self._origin_y)

    def world_to_geo(self, x: float, y: float) -> tuple[float, float]:
        """Convert metric world coordinates back to WGS84.

        Args:
            x: Metres east of the world origin.
            y: Metres north of the world origin.

        Returns:
            ``(lon, lat)`` in degrees.
        """
        return self._to_geo.transform(x + self._origin_x, y + self._origin_y)

    def geo_to_world_array(
        self,
        lon: NDArray[np.floating] | Sequence[float],
        lat: NDArray[np.floating] | Sequence[float],
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Vectorised :meth:`geo_to_world`.

        Args:
            lon: Longitudes in degrees.
            lat: Latitudes in degrees.

        Returns:
            Arrays ``(x, y)`` of metres relative to the world origin.
        """
        lon_arr = np.asarray(lon, dtype=np.float64)
        lat_arr = np.asarray(lat, dtype=np.float64)
        if lon_arr.shape != lat_arr.shape:
            raise ValueError(f"shape mismatch: lon {lon_arr.shape} vs lat {lat_arr.shape}")
        px, py = self._to_world.transform(lon_arr, lat_arr)
        return (np.asarray(px) - self._origin_x, np.asarray(py) - self._origin_y)

    def world_to_geo_array(
        self,
        x: NDArray[np.floating] | Sequence[float],
        y: NDArray[np.floating] | Sequence[float],
    ) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        """Vectorised :meth:`world_to_geo`."""
        x_arr = np.asarray(x, dtype=np.float64) + self._origin_x
        y_arr = np.asarray(y, dtype=np.float64) + self._origin_y
        if x_arr.shape != y_arr.shape:
            raise ValueError(f"shape mismatch: x {x_arr.shape} vs y {y_arr.shape}")
        lon, lat = self._to_geo.transform(x_arr, y_arr)
        return (np.asarray(lon), np.asarray(lat))

    # ------------------------------------------------------------ quality probe

    def measure_scale_error(
        self,
        lon: float,
        lat: float,
        azimuth_deg: float = 90.0,
        length_m: float = _SCALE_PROBE_LENGTH_M,
    ) -> float:
        """Measure the relative distance error at one location.

        A geodesic of ``length_m`` is laid out from ``(lon, lat)`` along
        ``azimuth_deg`` and its projected length is compared with its true
        length. Because the projection is conformal the result is independent
        of azimuth to first order, so the default due-east probe suffices.

        Args:
            lon: Longitude of the probe start.
            lat: Latitude of the probe start.
            azimuth_deg: Geodesic azimuth in degrees clockwise from north.
            length_m: Probe length in metres.

        Returns:
            ``projected_length / true_length - 1``. Positive means the game
            world is stretched relative to reality.
        """
        lon2, lat2, _ = WGS84_GEOD.fwd(lon, lat, azimuth_deg, length_m)
        x1, y1 = self._to_world.transform(lon, lat)
        x2, y2 = self._to_world.transform(lon2, lat2)
        projected = math.hypot(x2 - x1, y2 - y1)
        return projected / length_m - 1.0

    def measure_scale_error_over(
        self,
        bbox: BBox,
        samples: int = 9,
    ) -> ScaleErrorReport:
        """Probe the worst-case scale error across a regular grid over ``bbox``.

        Args:
            bbox: Region to probe.
            samples: Number of steps per axis (``samples >= 2``).

        Returns:
            A :class:`ScaleErrorReport` describing the worst location found.

        Raises:
            ValueError: If ``samples < 2``.
        """
        if samples < 2:
            raise ValueError("samples must be >= 2")
        lons = np.linspace(bbox.west, bbox.east, samples)
        lats = np.linspace(bbox.south, bbox.north, samples)
        worst = 0.0
        worst_lon = bbox.center[0]
        worst_lat = bbox.center[1]
        count = 0
        for lat in lats:
            for lon in lons:
                err = abs(self.measure_scale_error(float(lon), float(lat)))
                count += 1
                if err > worst:
                    worst, worst_lon, worst_lat = err, float(lon), float(lat)
        return ScaleErrorReport(
            max_abs_relative_error=worst,
            worst_lon=worst_lon,
            worst_lat=worst_lat,
            samples=count,
        )

    # -------------------------------------------------------------- constructor

    @classmethod
    def from_config(cls, config: Any) -> RegionProjection:
        """Build a projection from a region config mapping.

        The config may either give a fully explicit ``proj_string``, or a
        ``central_meridian``/``latitude_of_origin`` pair from which the string
        (including an optimal ``k0``) is derived.

        Args:
            config: Mapping with the region's projection settings.

        Returns:
            A ready-to-use projection.
        """
        bbox = BBox.from_config(config["bbox"])
        proj_cfg = config.get("projection", {}) if hasattr(config, "get") else {}

        origin = proj_cfg.get("origin")
        if origin is None:
            origin_lon, origin_lat = bbox.center
        else:
            origin_lon, origin_lat = float(origin[0]), float(origin[1])

        proj_string = proj_cfg.get("proj_string")
        if proj_string:
            name = proj_cfg.get("name", "custom")
        else:
            lon_0 = float(proj_cfg.get("central_meridian", bbox.center[0]))
            lat_0 = float(proj_cfg.get("latitude_of_origin", bbox.center[1]))
            k0 = proj_cfg.get("scale_factor")
            k0 = optimal_k0(bbox) if k0 is None else float(k0)
            proj_string = (
                f"+proj=tmerc +lat_0={lat_0} +lon_0={lon_0} +k={k0:.9f} "
                "+x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs +type=crs"
            )
            name = proj_cfg.get("name", f"ivanovo2beam local TM (CM {lon_0}E)")

        return cls(
            proj_string=proj_string,
            origin_lon=origin_lon,
            origin_lat=origin_lat,
            name=name,
        )


def make_projection(
    bbox: BBox,
    origin_lon: float | None = None,
    origin_lat: float | None = None,
    scale_factor: float | None = None,
) -> RegionProjection:
    """Convenience factory for an optimal regional Transverse Mercator.

    Args:
        bbox: Region the projection must serve; decides the central meridian
            and, unless overridden, the optimal scale factor.
        origin_lon: Longitude of the world origin. Defaults to the bbox centre.
        origin_lat: Latitude of the world origin. Defaults to the bbox centre.
        scale_factor: Override ``k0``. Defaults to :func:`optimal_k0`.

    Returns:
        A :class:`RegionProjection` covering ``bbox``.
    """
    centre_lon, centre_lat = bbox.center
    lon_0 = centre_lon if origin_lon is None else origin_lon
    lat_0 = centre_lat if origin_lat is None else origin_lat
    k0 = optimal_k0(bbox) if scale_factor is None else float(scale_factor)
    proj_string = (
        f"+proj=tmerc +lat_0={lat_0} +lon_0={centre_lon} +k={k0:.9f} "
        "+x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs +type=crs"
    )
    return RegionProjection(
        proj_string=proj_string,
        origin_lon=lon_0,
        origin_lat=lat_0,
        name=f"ivanovo2beam local TM (CM {centre_lon:.4f}E, k0={k0:.7f})",
    )


def geodesic_distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Return the true WGS84 geodesic distance between two points, in metres.

    This is the authority the tests compare the world plane against; it is
    independent of any projection.

    Args:
        lon1: Longitude of the first point.
        lat1: Latitude of the first point.
        lon2: Longitude of the second point.
        lat2: Latitude of the second point.

    Returns:
        Distance in metres.
    """
    _, _, dist = WGS84_GEOD.inv(lon1, lat1, lon2, lat2)
    return float(dist)
