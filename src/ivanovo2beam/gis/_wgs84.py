"""Pure-NumPy WGS84 Transverse Mercator and geodesic fallback.

Used only when :mod:`pyproj` cannot be imported (for example inside restricted
Windows sandboxes where PROJ's native DLL dependencies cannot initialise).
On standard Linux/macOS/Windows installations :mod:`pyproj` remains the primary
implementation.

Both algorithms match PROJ's WGS84 mathematics directly:

* Transverse Mercator uses Krueger's 6th-order series in third flattening
  ``n = f / (2 - f)`` (Karney 2011), accurate to sub-nanometre round-trip error
  across regional extents.
* Geodesic direct/inverse solves Vincenty's equations on the WGS84 ellipsoid to
  ``1e-12`` radian (~0.006 mm).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

#: WGS84 ellipsoid parameters.
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_B = WGS84_A * (1.0 - WGS84_F)
_N = WGS84_F / (2.0 - WGS84_F)
_E2 = WGS84_F * (2.0 - WGS84_F)
_E = math.sqrt(_E2)
_EP2 = (WGS84_A * WGS84_A - WGS84_B * WGS84_B) / (WGS84_B * WGS84_B)

_RECTIFYING_A = (WGS84_A / (1.0 + _N)) * (
    1.0 + (_N**2) / 4.0 + (_N**4) / 64.0 + (_N**6) / 256.0
)

_ALPHA = (
    0.5 * _N
    - (2.0 / 3.0) * _N**2
    + (5.0 / 16.0) * _N**3
    + (41.0 / 180.0) * _N**4
    - (127.0 / 288.0) * _N**5
    + (7891.0 / 37800.0) * _N**6,
    (13.0 / 48.0) * _N**2
    - (3.0 / 5.0) * _N**3
    + (557.0 / 1440.0) * _N**4
    + (281.0 / 630.0) * _N**5
    - (1983433.0 / 1935360.0) * _N**6,
    (61.0 / 240.0) * _N**3
    - (103.0 / 140.0) * _N**4
    + (15061.0 / 26880.0) * _N**5
    + (167603.0 / 181440.0) * _N**6,
    (49561.0 / 161280.0) * _N**4
    - (179.0 / 168.0) * _N**5
    + (6601661.0 / 7257600.0) * _N**6,
    (34729.0 / 80640.0) * _N**5 - (3418889.0 / 1995840.0) * _N**6,
    (212378941.0 / 319334400.0) * _N**6,
)

_BETA = (
    0.5 * _N
    - (2.0 / 3.0) * _N**2
    + (37.0 / 96.0) * _N**3
    - (1.0 / 360.0) * _N**4
    - (81.0 / 512.0) * _N**5
    + (96199.0 / 604800.0) * _N**6,
    (1.0 / 48.0) * _N**2
    + (1.0 / 15.0) * _N**3
    - (437.0 / 1440.0) * _N**4
    + (46.0 / 105.0) * _N**5
    - (1118711.0 / 3870720.0) * _N**6,
    (17.0 / 480.0) * _N**3
    - (37.0 / 840.0) * _N**4
    - (209.0 / 4480.0) * _N**5
    + (5569.0 / 90720.0) * _N**6,
    (4397.0 / 161280.0) * _N**4
    - (11.0 / 504.0) * _N**5
    - (830251.0 / 7257600.0) * _N**6,
    (4583.0 / 161280.0) * _N**5 - (108847.0 / 3991680.0) * _N**6,
    (20648693.0 / 638668800.0) * _N**6,
)


def _conformal_tau(tau: np.ndarray) -> np.ndarray:
    """Convert geodetic ``tan(phi)`` to conformal ``tan(chi)``."""
    sigma = np.sinh(_E * np.arctanh(_E * tau / np.sqrt(1.0 + tau * tau)))
    return tau * np.sqrt(1.0 + sigma * sigma) - sigma * np.sqrt(1.0 + tau * tau)


def _geodetic_tau(tau_prime: np.ndarray) -> np.ndarray:
    """Invert :func:`_conformal_tau` by Newton iteration."""
    tau = tau_prime.copy()
    one_minus_e2 = 1.0 - _E2
    for _ in range(6):
        tau_i = _conformal_tau(tau)
        dtau = (
            (tau_prime - tau_i)
            / np.sqrt(1.0 + tau_i * tau_i)
            * (1.0 + one_minus_e2 * tau * tau)
            / (one_minus_e2 * np.sqrt(1.0 + tau * tau))
        )
        tau = tau + dtau
    return tau


def _meridian_xi(lat_rad: float) -> float:
    """Return dimensionless rectifying latitude ``xi`` on the central meridian."""
    if lat_rad == 0.0:
        return 0.0
    tau_p = float(_conformal_tau(np.asarray(math.tan(lat_rad), dtype=np.float64)))
    xi_p = math.atan(tau_p)
    return xi_p + sum(_ALPHA[j - 1] * math.sin(2.0 * j * xi_p) for j in range(1, 7))


@dataclass(frozen=True, slots=True)
class FallbackCRS:
    """Minimal PROJ-string representation matching the subset ``projection.py`` uses."""

    proj_name: str
    lat_0: float
    lon_0: float
    k_0: float
    x_0: float
    y_0: float
    units: str
    proj_string: str

    @property
    def is_projected(self) -> bool:
        return self.proj_name not in {"longlat", "latlong", "latlon"}

    @classmethod
    def from_proj4(cls, proj_string: str) -> FallbackCRS:
        tokens: dict[str, str] = {}
        for raw in proj_string.split():
            item = raw.strip()
            if not item:
                continue
            if item.startswith("+"):
                item = item[1:]
            if "=" in item:
                key, value = item.split("=", 1)
                tokens[key.strip()] = value.strip()
            else:
                tokens[item] = "true"
        proj_name = tokens.get("proj", "")
        if not proj_name:
            raise ValueError(f"invalid PROJ string (missing +proj): {proj_string!r}")
        ellps = tokens.get("ellps", tokens.get("datum", "WGS84")).upper()
        if ellps != "WGS84":
            raise ValueError(f"unsupported ellipsoid {ellps!r} without pyproj")
        units = tokens.get("units", "m")
        k_val = tokens.get("k", tokens.get("k_0", "1.0"))
        return cls(
            proj_name=proj_name,
            lat_0=float(tokens.get("lat_0", "0.0")),
            lon_0=float(tokens.get("lon_0", "0.0")),
            k_0=float(k_val),
            x_0=float(tokens.get("x_0", "0.0")),
            y_0=float(tokens.get("y_0", "0.0")),
            units=units,
            proj_string=proj_string,
        )


class FallbackTMerc:
    """Bidirectional WGS84 Krueger Transverse Mercator transformer."""

    __slots__ = ("_crs", "_forward", "_scale", "_xi_0")

    def __init__(self, crs: FallbackCRS, *, forward: bool) -> None:
        if crs.proj_name != "tmerc":
            raise ValueError(f"unsupported projection {crs.proj_name!r} without pyproj")
        self._crs = crs
        self._forward = forward
        self._scale = crs.k_0 * _RECTIFYING_A
        self._xi_0 = _meridian_xi(math.radians(crs.lat_0))

    def transform(self, a: Any, b: Any) -> tuple[Any, Any]:
        scalar = np.ndim(a) == 0 and np.ndim(b) == 0
        arr_a = np.asarray(a, dtype=np.float64)
        arr_b = np.asarray(b, dtype=np.float64)
        if self._forward:
            out_x, out_y = self._fwd(arr_a, arr_b)
        else:
            out_x, out_y = self._inv(arr_a, arr_b)
        if scalar:
            return (float(out_x), float(out_y))
        return (out_x, out_y)

    def _fwd(self, lon_deg: np.ndarray, lat_deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        phi = np.radians(lat_deg)
        dlon = np.radians(lon_deg - self._crs.lon_0)
        dlon = np.arctan2(np.sin(dlon), np.cos(dlon))

        tau = np.tan(phi)
        tau_p = _conformal_tau(tau)
        cos_dlon = np.cos(dlon)
        sin_dlon = np.sin(dlon)

        xi_p = np.arctan2(tau_p, cos_dlon)
        eta_p = np.arcsinh(sin_dlon / np.hypot(tau_p, cos_dlon))

        xi = xi_p.copy()
        eta = eta_p.copy()
        for j, alpha in enumerate(_ALPHA, start=1):
            two_j = 2.0 * j
            xi = xi + alpha * np.sin(two_j * xi_p) * np.cosh(two_j * eta_p)
            eta = eta + alpha * np.cos(two_j * xi_p) * np.sinh(two_j * eta_p)

        easting = self._crs.x_0 + self._scale * eta
        northing = self._crs.y_0 + self._scale * (xi - self._xi_0)
        return (easting, northing)

    def _inv(self, easting: np.ndarray, northing: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        eta = (easting - self._crs.x_0) / self._scale
        xi = self._xi_0 + (northing - self._crs.y_0) / self._scale

        xi_p = xi.copy()
        eta_p = eta.copy()
        for j, beta in enumerate(_BETA, start=1):
            two_j = 2.0 * j
            xi_p = xi_p - beta * np.sin(two_j * xi) * np.cosh(two_j * eta)
            eta_p = eta_p - beta * np.cos(two_j * xi) * np.sinh(two_j * eta)

        sinh_eta_p = np.sinh(eta_p)
        cos_xi_p = np.cos(xi_p)
        tau_p = np.sin(xi_p) / np.hypot(sinh_eta_p, cos_xi_p)
        dlon = np.arctan2(sinh_eta_p, cos_xi_p)

        tau = _geodetic_tau(tau_p)
        lat_deg = np.degrees(np.arctan(tau))
        lon_deg = self._crs.lon_0 + np.degrees(dlon)
        lon_deg = ((lon_deg + 180.0) % 360.0) - 180.0
        return (lon_deg, lat_deg)


class FallbackGeod:
    """WGS84 ellipsoid geodesic solver (Vincenty direct and inverse)."""

    def __init__(self, ellps: str = "WGS84") -> None:
        if ellps.upper() != "WGS84":
            raise ValueError(f"unsupported ellipsoid {ellps!r}")

    def fwd(self, lon: float, lat: float, az_deg: float, dist_m: float) -> tuple[float, float, float]:
        if dist_m == 0.0:
            back = ((az_deg + 180.0 + 180.0) % 360.0) - 180.0
            return (float(lon), float(lat), float(back))

        phi1 = math.radians(lat)
        lam1 = math.radians(lon)
        alpha1 = math.radians(az_deg)
        sin_alpha1 = math.sin(alpha1)
        cos_alpha1 = math.cos(alpha1)

        tan_u1 = (1.0 - WGS84_F) * math.tan(phi1)
        cos_u1 = 1.0 / math.sqrt(1.0 + tan_u1 * tan_u1)
        sin_u1 = tan_u1 * cos_u1
        sigma1 = math.atan2(tan_u1, cos_alpha1)
        sin_alpha = cos_u1 * sin_alpha1
        cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
        u_sq = cos_sq_alpha * _EP2
        big_a = 1.0 + u_sq / 16384.0 * (4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq)))
        big_b = u_sq / 1024.0 * (256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq)))

        sigma = dist_m / (WGS84_B * big_a)
        cos_2sigma_m = 0.0
        sin_sigma = 0.0
        cos_sigma = 1.0
        for _ in range(100):
            cos_2sigma_m = math.cos(2.0 * sigma1 + sigma)
            sin_sigma = math.sin(sigma)
            cos_sigma = math.cos(sigma)
            delta_sigma = (
                big_b
                * sin_sigma
                * (
                    cos_2sigma_m
                    + (big_b / 4.0)
                    * (
                        cos_sigma * (-1.0 + 2.0 * cos_2sigma_m * cos_2sigma_m)
                        - (big_b / 6.0)
                        * cos_2sigma_m
                        * (-3.0 + 4.0 * sin_sigma * sin_sigma)
                        * (-3.0 + 4.0 * cos_2sigma_m * cos_2sigma_m)
                    )
                )
            )
            prev = sigma
            sigma = dist_m / (WGS84_B * big_a) + delta_sigma
            if abs(sigma - prev) < 1e-13:
                break

        tmp = sin_u1 * sin_sigma - cos_u1 * cos_sigma * cos_alpha1
        phi2 = math.atan2(
            sin_u1 * cos_sigma + cos_u1 * sin_sigma * cos_alpha1,
            (1.0 - WGS84_F) * math.hypot(sin_alpha, tmp),
        )
        lam = math.atan2(
            sin_sigma * sin_alpha1,
            cos_u1 * cos_sigma - sin_u1 * sin_sigma * cos_alpha1,
        )
        c_val = WGS84_F / 16.0 * cos_sq_alpha * (4.0 + WGS84_F * (4.0 - 3.0 * cos_sq_alpha))
        diff_lon = lam - (1.0 - c_val) * WGS84_F * sin_alpha * (
            sigma + c_val * sin_sigma * (cos_2sigma_m + c_val * cos_sigma * (-1.0 + 2.0 * cos_2sigma_m**2))
        )
        lon2 = ((math.degrees(lam1 + diff_lon) + 180.0) % 360.0) - 180.0
        lat2 = math.degrees(phi2)
        back_az = math.degrees(math.atan2(sin_alpha, -tmp)) + 180.0
        back_az = ((back_az + 180.0) % 360.0) - 180.0
        return (lon2, lat2, back_az)

    def inv(self, lon1: float, lat1: float, lon2: float, lat2: float) -> tuple[float, float, float]:
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        diff_lon = math.radians(lon2 - lon1)
        if abs(phi2 - phi1) < 1e-15 and abs(diff_lon) < 1e-15:
            return (0.0, 0.0, 0.0)

        u1 = math.atan((1.0 - WGS84_F) * math.tan(phi1))
        u2 = math.atan((1.0 - WGS84_F) * math.tan(phi2))
        sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
        sin_u2, cos_u2 = math.sin(u2), math.cos(u2)

        lam = diff_lon
        sin_sigma = 0.0
        cos_sigma = 1.0
        sigma = 0.0
        cos_sq_alpha = 0.0
        cos_2sigma_m = 0.0
        sin_lam = 0.0
        cos_lam = 1.0
        for _ in range(200):
            sin_lam = math.sin(lam)
            cos_lam = math.cos(lam)
            sin_sigma = math.hypot(
                cos_u2 * sin_lam,
                cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam,
            )
            if sin_sigma == 0.0:
                return (0.0, 0.0, 0.0)
            cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
            sigma = math.atan2(sin_sigma, cos_sigma)
            sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
            cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
            cos_2sigma_m = (
                0.0
                if cos_sq_alpha == 0.0
                else cos_sigma - 2.0 * sin_u1 * sin_u2 / cos_sq_alpha
            )
            c_val = WGS84_F / 16.0 * cos_sq_alpha * (4.0 + WGS84_F * (4.0 - 3.0 * cos_sq_alpha))
            prev = lam
            lam = diff_lon + (1.0 - c_val) * WGS84_F * sin_alpha * (
                sigma
                + c_val * sin_sigma * (cos_2sigma_m + c_val * cos_sigma * (-1.0 + 2.0 * cos_2sigma_m**2))
            )
            if abs(lam - prev) < 1e-13:
                break

        u_sq = cos_sq_alpha * _EP2
        big_a = 1.0 + u_sq / 16384.0 * (4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq)))
        big_b = u_sq / 1024.0 * (256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq)))
        delta_sigma = (
            big_b
            * sin_sigma
            * (
                cos_2sigma_m
                + (big_b / 4.0)
                * (
                    cos_sigma * (-1.0 + 2.0 * cos_2sigma_m * cos_2sigma_m)
                    - (big_b / 6.0)
                    * cos_2sigma_m
                    * (-3.0 + 4.0 * sin_sigma * sin_sigma)
                    * (-3.0 + 4.0 * cos_2sigma_m * cos_2sigma_m)
                )
            )
        )
        dist = WGS84_B * big_a * (sigma - delta_sigma)
        fwd_az = math.degrees(
            math.atan2(cos_u2 * sin_lam, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam)
        )
        back_az = math.degrees(
            math.atan2(cos_u1 * sin_lam, -sin_u1 * cos_u2 + cos_u1 * sin_u2 * cos_lam)
        ) + 180.0
        back_az = ((back_az + 180.0) % 360.0) - 180.0
        return (fwd_az, back_az, dist)
