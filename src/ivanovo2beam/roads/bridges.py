"""3D procedural bridges subsystem for BeamNG.drive.

For OSM ways tagged with ``bridge=yes`` (or viaducts / boardwalks), this module
generates:
1. Procedural Collada 1.4.1 meshes (``.dae``) containing:
   - Closed manifold deck slab (width = road width + 1.2m, thickness = 0.65m)
     with UV mapping for ``ivanovo_bridge_deck``;
   - Continuous guardrails / parapets (0.85m high x 0.35m wide safety barriers
     along both deck edges);
   - Substructure abutments and concrete piers spaced every 25m down to bed.
2. BeamNG ``TSStatic`` scene object with:
   - ``collisionType: "Visible Mesh Final"``
   - ``decalType: "Visible Mesh"``
3. Materials definition in ``levels/<id>/art/shapes/bridges/main.materials.json``
   referencing native BeamNG concrete textures.
4. Bridgehead approach ramp corridors pinned to deck endpoints with 0cm step lip.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import numpy as np

from ivanovo2beam.beamng.level import stable_uuid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ivanovo2beam.config import RoadSettings
    from ivanovo2beam.roads.network import Road
    from ivanovo2beam.terrain.builder import RoadCorridor

#: Fixed timestamp for byte-identical deterministic COLLADA outputs.
_FIXED_TIMESTAMP = "2026-01-01T00:00:00Z"

#: Material name bound in Collada and defined in main.materials.json
BRIDGE_DECAL_MATERIAL = "ivanovo_bridge_deck"

#: Base-game precast concrete textures referenced by path
CONCRETE_PRECAST_TEXTURES: dict[str, str] = {
    "ao": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_ao.data.dds",
    "b": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_b.color.dds",
    "nm": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_nm.normal.dds",
    "r": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_r.data.dds",
}

#: Bridge slab parameters (in metres)
#: Coupled vertical curve, skirt, and end-lip constants adapted from MapNG
#: (https://github.com/nikkiluzader/mapng), MIT License.
SLAB_EXTRA_WIDTH_M = 1.2
SLAB_THICKNESS_M = 0.65
TOP_SKIRT_M = 0.15
GIRDER_DEPTH_M = 1.0
GIRDER_WIDTH_M = 0.5
GUARDRAIL_HEIGHT_M = 0.85
GUARDRAIL_WIDTH_M = 0.35
PIER_SPACING_M = 25.0
PIER_THICKNESS_M = 1.6
PIER_FOOTING_M = 1.5
ABUTMENT_LENGTH_M = 3.5
END_LIP_M = 1.6
END_LIP_UNDER_M = 0.05
DECK_STEP_M = 4.0
DECK_MAX_GRADE = 0.06
VERTICAL_CURVE_M = 40.0
RAMP_GRADE = 0.05


def _quad_face_normal(
    v0: tuple[float, float, float],
    v1: tuple[float, float, float],
    v2: tuple[float, float, float],
    fallback: tuple[float, float, float] = (0.0, 0.0, 1.0),
) -> tuple[float, float, float]:
    """Compute the unit outward normal of a CCW quad ``(v0, v1, v2, v3)``."""
    ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
    bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
    nx = ay * bz - az * by
    ny = az * bx - ax * bz
    nz = ax * by - ay * bx
    length = float(np.sqrt(nx * nx + ny * ny + nz * nz))
    if length < 1e-9:
        return fallback
    return (nx / length, ny / length, nz / length)


@dataclass(slots=True)
class BridgeGeometry:
    """Procedural 3D mesh representation for a bridge."""

    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    # Each triangle is a tuple of 3 indices: ((v0, n0, uv0), (v1, n1, uv1), (v2, n2, uv2))
    triangles: list[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=list
    )
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def center(self) -> tuple[float, float, float]:
        """Return world-space bounding center ``(cx, cy, min_z)`` for localization."""
        if not self.positions:
            return self.origin
        xs = [p[0] for p in self.positions]
        ys = [p[1] for p in self.positions]
        zs = [p[2] for p in self.positions]
        return (
            round(self.origin[0] + 0.5 * (min(xs) + max(xs)), 4),
            round(self.origin[1] + 0.5 * (min(ys) + max(ys)), 4),
            round(self.origin[2] + min(zs), 4),
        )

    def add_triangle(
        self,
        v0: tuple[float, float, float],
        v1: tuple[float, float, float],
        v2: tuple[float, float, float],
        n: tuple[float, float, float],
        uv0: tuple[float, float],
        uv1: tuple[float, float],
        uv2: tuple[float, float],
    ) -> None:
        """Add a single triangle with specified normal and UVs."""
        idx_base = len(self.positions)
        self.positions.extend([v0, v1, v2])
        self.normals.extend([n, n, n])
        self.uvs.extend([uv0, uv1, uv2])
        self.triangles.append(
            ((idx_base, idx_base, idx_base), (idx_base + 1, idx_base + 1, idx_base + 1), (idx_base + 2, idx_base + 2, idx_base + 2))
        )

    def add_quad(
        self,
        v0: tuple[float, float, float],
        v1: tuple[float, float, float],
        v2: tuple[float, float, float],
        v3: tuple[float, float, float],
        n: tuple[float, float, float],
        uv0: tuple[float, float] = (0.0, 0.0),
        uv1: tuple[float, float] = (1.0, 0.0),
        uv2: tuple[float, float] = (1.0, 1.0),
        uv3: tuple[float, float] = (0.0, 1.0),
    ) -> None:
        """Add a quad (split into 2 triangles) with CCW winding: (v0, v1, v2) and (v0, v2, v3)."""
        self.add_triangle(v0, v1, v2, n, uv0, uv1, uv2)
        self.add_triangle(v0, v2, v3, n, uv0, uv2, uv3)

    def add_box(
        self,
        c000: tuple[float, float, float],
        c100: tuple[float, float, float],
        c110: tuple[float, float, float],
        c010: tuple[float, float, float],
        c001: tuple[float, float, float],
        c101: tuple[float, float, float],
        c111: tuple[float, float, float],
        c011: tuple[float, float, float],
    ) -> None:
        """Add a closed 6-sided box defined by 8 corner vertices (right-handed)."""
        faces = (
            (c001, c101, c111, c011, (0.0, 0.0, 1.0)),   # Top (+Z)
            (c000, c010, c110, c100, (0.0, 0.0, -1.0)),  # Bottom (-Z)
            (c000, c100, c101, c001, (0.0, -1.0, 0.0)),  # Front (-N)
            (c110, c010, c011, c111, (0.0, 1.0, 0.0)),   # Back (+N)
            (c010, c000, c001, c011, (-1.0, 0.0, 0.0)),  # Left (-T)
            (c100, c110, c111, c101, (1.0, 0.0, 0.0)),   # Right (+T)
        )
        for v0, v1, v2, v3, fallback in faces:
            self.add_quad(v0, v1, v2, v3, _quad_face_normal(v0, v1, v2, fallback))


def water_clear_m(run_m: float) -> float:
    """Required deck clearance above water for a wet crossing run length.

    Adapted from MapNG (https://github.com/nikkiluzader/mapng), MIT License.
    """
    if run_m > 1500.0:
        return 5.0
    return float(min(20.0, max(2.5, 0.15 * run_m)))


def _eased_grade(d: float, slope: float = DECK_MAX_GRADE, curve_m: float = VERTICAL_CURVE_M) -> float:
    """Parabolic vertical curve easing into linear ``slope`` over ``curve_m``.

    Adapted from MapNG (https://github.com/nikkiluzader/mapng), MIT License.
    """
    if d <= 0.0:
        return 0.0
    if d < curve_m:
        return (slope * d * d) / (2.0 * curve_m)
    return slope * (d - 0.5 * curve_m)


def _no_sag(s: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Upper concave hull preventing mid-bridge sag between higher points.

    Adapted from MapNG (https://github.com/nikkiluzader/mapng), MIT License.
    """
    hull: list[tuple[float, float]] = []
    for si, zi in zip(s, z, strict=True):
        p = (float(si), float(zi))
        while len(hull) >= 2:
            x1, y1 = hull[-2]
            x2, y2 = hull[-1]
            if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) >= 0.0:
                hull.pop()
            else:
                break
        hull.append(p)

    out = np.empty_like(z)
    h = 0
    for i, si in enumerate(s):
        sv = float(si)
        while h < len(hull) - 2 and hull[h + 1][0] < sv:
            h += 1
        x1, y1 = hull[h]
        x2, y2 = hull[min(h + 1, len(hull) - 1)]
        out[i] = y1 + ((y2 - y1) * (sv - x1)) / (x2 - x1) if x2 > x1 else y1
    return out


def _smooth_pinned(z: np.ndarray, step: float, sigma_m: float) -> np.ndarray:
    """Gaussian smoothing with odd-reflected ends so endpoints stay pinned.

    Adapted from MapNG (https://github.com/nikkiluzader/mapng), MIT License.
    """
    n = z.size
    if n < 3 or step <= 0.0 or sigma_m <= 0.0:
        return z.copy()
    line = np.linspace(float(z[0]), float(z[-1]), n)
    d = z - line
    r = max(1, round((3.0 * sigma_m) / step))
    sig = sigma_m / step

    def at(idx: int) -> float:
        sign = 1.0
        while True:
            if idx < 0:
                idx = -idx
                sign = -sign
            elif idx > n - 1:
                idx = 2 * (n - 1) - idx
                sign = -sign
            else:
                return sign * float(d[idx])

    out = np.empty_like(z)
    weights = [math.exp(-(k * k) / (2.0 * sig * sig)) for k in range(-r, r + 1)]
    den = sum(weights)
    for i in range(n):
        num = sum(w * at(i + k) for w, k in zip(weights, range(-r, r + 1), strict=True))
        out[i] = num / den + float(line[i])
    out[0] = z[0]
    out[-1] = z[-1]
    return out


def fit_theil_sen_approach(
    pt: tuple[float, float],
    tangent_away: tuple[float, float],
    ground_sampler: Callable[[float, float], float],
    *,
    min_dist_m: float = 4.0,
    max_dist_m: float = 40.0,
    step_m: float = 4.0,
) -> float:
    """Fit a robust Theil-Sen median slope over ``4..40 m`` behind an abutment.

    Prevents riverbank drop-offs right at the bridge portal from dragging the
    abutment elevation downward.
    """
    tx, ty = tangent_away
    tlen = math.hypot(tx, ty)
    if tlen < 1e-6:
        return float(ground_sampler(pt[0], pt[1]))
    ux, uy = tx / tlen, ty / tlen

    samples: list[tuple[float, float]] = []
    d = min_dist_m
    while d <= max_dist_m + 1e-6:
        z = float(ground_sampler(pt[0] + ux * d, pt[1] + uy * d))
        if math.isfinite(z):
            samples.append((d, z))
        d += step_m

    if len(samples) < 3:
        return float(ground_sampler(pt[0], pt[1]))

    slopes: list[float] = []
    for i in range(len(samples)):
        for j in range(i + 1, len(samples)):
            dd = samples[j][0] - samples[i][0]
            if dd > 1e-6:
                slopes.append((samples[j][1] - samples[i][1]) / dd)
    if not slopes:
        return float(np.median([s[1] for s in samples]))

    slopes.sort()
    slope = max(-0.12, min(0.12, slopes[len(slopes) >> 1]))
    intercepts = sorted(z - slope * dist for dist, z in samples)
    return float(intercepts[len(intercepts) >> 1])


def _water_requirements_along(
    pts: np.ndarray,
    water_network: Any | None,
) -> np.ndarray:
    """Return required minimum deck elevation at each point over ``water_network``."""
    n = pts.shape[0]
    req = np.full(n, -np.inf, dtype=np.float64)
    if water_network is None or n == 0:
        return req

    rivers = getattr(water_network, "rivers", ()) or ()
    bodies = getattr(water_network, "water_bodies", ()) or ()
    if not rivers and not bodies:
        return req

    wet_z = np.full(n, -np.inf, dtype=np.float64)
    for k in range(n):
        px, py = float(pts[k, 0]), float(pts[k, 1])
        for river in rivers:
            rpts = river.points
            if rpts.shape[0] < 2:
                continue
            pad = float(river.width_m) * 0.5 + 2.0
            rx0, ry0 = float(rpts[:, 0].min()) - pad, float(rpts[:, 1].min()) - pad
            rx1, ry1 = float(rpts[:, 0].max()) + pad, float(rpts[:, 1].max()) + pad
            if px < rx0 or px > rx1 or py < ry0 or py > ry1:
                continue
            for j in range(rpts.shape[0] - 1):
                ax, ay = float(rpts[j, 0]), float(rpts[j, 1])
                bx, by = float(rpts[j + 1, 0]), float(rpts[j + 1, 1])
                dx, dy = bx - ax, by - ay
                l2 = dx * dx + dy * dy
                t = 0.0 if l2 <= 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                if math.hypot(ax + dx * t - px, ay + dy * t - py) <= pad:
                    zw = float(river.z_water[j] + t * (river.z_water[j + 1] - river.z_water[j]))
                    if zw > wet_z[k]:
                        wet_z[k] = zw
        for body in bodies:
            bx0, by0, bx1, by1 = body.bounds
            if px < bx0 or px > bx1 or py < by0 or py > by1:
                continue
            from shapely.geometry import Point, Polygon

            poly = Polygon(body.outer_ring, holes=body.inner_rings)
            if poly.contains(Point(px, py)):
                zw = float(body.z_water)
                if zw > wet_z[k]:
                    wet_z[k] = zw

    wet = np.isfinite(wet_z)
    if not wet.any():
        return req

    deltas = np.diff(pts, axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    cum_lens = np.concatenate(([0.0], np.cumsum(seg_lens)))

    i = 0
    while i < n:
        if not wet[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and wet[j + 1]:
            j += 1
        avg_step = float(seg_lens[0]) if seg_lens.size else 4.0
        run_m = float(cum_lens[j] - cum_lens[i]) + avg_step
        clear = water_clear_m(run_m)
        for k in range(i, j + 1):
            req[k] = wet_z[k] + clear
        i = j + 1
    return req


def compute_bridge_deck_profile(
    road: Road,
    ground_z: np.ndarray | None = None,
    ground_sampler: Callable[[float, float], float] | None = None,
    clearance_m: float = 4.0,
    *,
    water_network: Any | None = None,
) -> np.ndarray:
    """Compute the longitudinal 3D deck elevation profile for a bridge.

    Pins endpoints to terrain/ground elevation, ensures carriageway spans
    at least ``clearance_m`` above the valley below without step kinks near
    bridgeheads, and enforces ``water_network`` clearance when supplied.
    """
    n = road.points.shape[0]
    if ground_z is not None and len(ground_z) == n:
        raw_z = np.asarray(ground_z, dtype=np.float64).copy()
    elif ground_sampler is not None:
        raw_z = np.array([ground_sampler(float(p[0]), float(p[1])) for p in road.points], dtype=np.float64)
    else:
        raw_z = np.zeros(n, dtype=np.float64)

    if n < 2:
        return raw_z

    deltas = np.diff(road.points, axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    cum_lens = np.concatenate(([0.0], np.cumsum(seg_lens)))
    total_len = float(cum_lens[-1])
    if total_len <= 1e-3:
        return raw_z

    z0 = float(raw_z[0])
    z1 = float(raw_z[-1])

    # Check crossed water along a 4m-resampled span so 2-node bridges also clear rivers
    if water_network is not None:
        n_steps = max(2, math.ceil(total_len / DECK_STEP_M) + 1)
        s_probe = np.linspace(0.0, total_len, n_steps)
        px_probe = np.interp(s_probe, cum_lens, road.points[:, 0])
        py_probe = np.interp(s_probe, cum_lens, road.points[:, 1])
        pts_probe = np.column_stack((px_probe, py_probe))
        w_req = _water_requirements_along(pts_probe, water_network)
        for k in range(n_steps):
            if np.isfinite(w_req[k]):
                s_k = float(s_probe[k])
                z0 = max(z0, float(w_req[k]) - _eased_grade(s_k))
                z1 = max(z1, float(w_req[k]) - _eased_grade(total_len - s_k))

    # A smooth linear chord between bridgehead endpoints, with tapered clearance
    # over interior spans so bank vertices near the ends do not cause a step kink.
    taper_len = max(total_len * 0.25, 1.0)
    deck_z = np.zeros(n, dtype=np.float64)
    for i in range(n):
        t = float(cum_lens[i] / total_len)
        chord_z = z0 + t * (z1 - z0)
        if i == 0:
            deck_z[i] = z0
        elif i == n - 1:
            deck_z[i] = z1
        else:
            d_nearest = float(min(cum_lens[i], total_len - cum_lens[i]))
            s = min(1.0, d_nearest / taper_len)
            smooth_s = s * s * (3.0 - 2.0 * s)
            target_z = max(chord_z, float(raw_z[i]) + clearance_m * smooth_s)
            deck_z[i] = min(target_z, chord_z + 0.08 * d_nearest)

    return deck_z


def solve_coupled_bridge_profile(
    road: Road,
    ground_sampler: Callable[[float, float], float] | None = None,
    *,
    water_network: Any | None = None,
    approach_end_z: tuple[float | None, float | None] | None = None,
    step_m: float = DECK_STEP_M,
    deck_clear_m: float = 0.05,
) -> tuple[np.ndarray, np.ndarray]:
    """Solve a coupled, vertical-curved 3D profile for a bridge span.

    Resamples the bridge polyline every ``step_m`` (4 m) so 2-node spans get
    smooth parabolic vertical curves (``VERTICAL_CURVE_M=40``), fits abutment
    elevations via Theil-Sen slope ``4..40 m`` behind portals (or uses coupled
    approach road elevations), lifts spans to clear ``water_network`` by at
    least ``water_clear_m(run)``, prevents mid-span sag via ``noSag``, and
    smooths with pinned endpoints.

    Adapted from MapNG (https://github.com/nikkiluzader/mapng), MIT License.
    """
    pts = np.asarray(road.points, dtype=np.float64)
    if pts.shape[0] < 2:
        z = np.zeros(pts.shape[0], dtype=np.float64)
        return pts, z

    deltas = np.diff(pts, axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    cum = np.concatenate(([0.0], np.cumsum(seg_lens)))
    total_len = float(cum[-1])
    if total_len <= 1e-3:
        z0 = float(ground_sampler(float(pts[0, 0]), float(pts[0, 1]))) if ground_sampler is not None else 0.0
        return pts.copy(), np.full(pts.shape[0], z0, dtype=np.float64)

    n_steps = max(2, round(total_len / step_m) + 1)
    s = np.linspace(0.0, total_len, n_steps)
    pts_r = np.column_stack((np.interp(s, cum, pts[:, 0]), np.interp(s, cum, pts[:, 1])))

    if ground_sampler is not None:
        ground = np.array([float(ground_sampler(float(p[0]), float(p[1]))) for p in pts_r], dtype=np.float64)
    else:
        ground = np.zeros(n_steps, dtype=np.float64)

    # Abutment heights: coupled approach Z if given, else Theil-Sen 4..40m behind portals
    if approach_end_z is not None and approach_end_z[0] is not None:
        z0 = float(approach_end_z[0])
    elif ground_sampler is not None:
        t0 = (float(pts_r[0, 0] - pts_r[1, 0]), float(pts_r[0, 1] - pts_r[1, 1]))
        z0 = fit_theil_sen_approach((float(pts_r[0, 0]), float(pts_r[0, 1])), t0, ground_sampler)
    else:
        z0 = float(ground[0])

    if approach_end_z is not None and approach_end_z[1] is not None:
        z1 = float(approach_end_z[1])
    elif ground_sampler is not None:
        t1 = (float(pts_r[-1, 0] - pts_r[-2, 0]), float(pts_r[-1, 1] - pts_r[-2, 1]))
        z1 = fit_theil_sen_approach((float(pts_r[-1, 0]), float(pts_r[-1, 1])), t1, ground_sampler)
    else:
        z1 = float(ground[-1])

    water_req = _water_requirements_along(pts_r, water_network)
    # Lift abutments if raw portal heights sit too low to clear water within DECK_MAX_GRADE
    for k in range(n_steps):
        if np.isfinite(water_req[k]):
            sk = float(s[k])
            z0 = max(z0, float(water_req[k]) - _eased_grade(sk))
            z1 = max(z1, float(water_req[k]) - _eased_grade(total_len - sk))

    base = z0 + (s / total_len) * (z1 - z0)
    cap = np.array(
        [
            max(
                float(base[k]),
                min(z0 + _eased_grade(float(s[k])), z1 + _eased_grade(total_len - float(s[k]))),
            )
            for k in range(n_steps)
        ],
        dtype=np.float64,
    )

    if n_steps >= 3:
        actual_step = total_len / (n_steps - 1)
        sigma = min(40.0, max(12.0, total_len / 25.0))
        rounded_cap = _smooth_pinned(cap, actual_step, sigma)
        cap = np.minimum(cap, rounded_cap)

    req_vals = np.maximum(base, water_req)
    z = np.maximum(np.minimum(req_vals, cap), ground + deck_clear_m)
    z[0] = z0
    z[-1] = z1

    if n_steps > 3:
        filled = _no_sag(s, z)
        z = np.maximum(np.minimum(filled, np.maximum(cap, z)), z)
        actual_step = total_len / (n_steps - 1)
        sigma = min(40.0, max(12.0, total_len / 25.0))
        floor = np.minimum(np.maximum(water_req, ground + deck_clear_m), cap)
        sm = _smooth_pinned(z, actual_step, sigma)
        for _ in range(12):
            short = np.maximum(0.0, floor - sm)
            over = np.maximum(0.0, sm - cap)
            if float(max(short.max(), over.max())) < 0.01:
                break
            lift = _smooth_pinned(short, actual_step, sigma * 0.5)
            drop = _smooth_pinned(over, actual_step, sigma * 0.5)
            gl = float(short.max() / lift.max()) if float(lift.max()) > 1e-9 else 0.0
            gd = float(over.max() / drop.max()) if float(drop.max()) > 1e-9 else 0.0
            sm = sm + lift * gl - drop * gd
        z = np.maximum(np.minimum(sm, cap), np.maximum(floor, ground + deck_clear_m))
        z[0] = z0
        z[-1] = z1

    return pts_r, z


def _populate_bridge_geometry(
    road: Road,
    deck_z: np.ndarray,
    ground_sampler: Callable[[float, float], float] | None,
    *,
    deck_geom: BridgeGeometry | None,
    struct_geom: BridgeGeometry,
) -> None:
    """Populate combined or split deck/structure bridge geometry."""
    pts = np.asarray(road.points, dtype=np.float64)
    z_arr = np.asarray(deck_z, dtype=np.float64)
    if pts.shape[0] < 2:
        return

    # Deduplicate consecutive zero-length points while preserving endpoints
    keep = np.ones(pts.shape[0], dtype=bool)
    last_x, last_y = float(pts[0, 0]), float(pts[0, 1])
    for i in range(1, pts.shape[0]):
        if math.hypot(float(pts[i, 0]) - last_x, float(pts[i, 1]) - last_y) < 1e-6:
            keep[i] = False
        else:
            last_x, last_y = float(pts[i, 0]), float(pts[i, 1])
    pts = pts[keep]
    z_arr = z_arr[keep]
    n = pts.shape[0]
    if n < 2:
        return
    deck_z = z_arr

    diffs = np.diff(pts, axis=0)
    raw_seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])
    safe_seg_lens = np.where(raw_seg_lens < 1e-6, 1.0, raw_seg_lens)
    seg_tangents = diffs / safe_seg_lens[:, None]

    normals = np.zeros((n, 2), dtype=np.float64)
    normals[0] = [-seg_tangents[0, 1], seg_tangents[0, 0]]
    normals[-1] = [-seg_tangents[-1, 1], seg_tangents[-1, 0]]
    if n > 2:
        mid_tangents = (seg_tangents[:-1] + seg_tangents[1:]) * 0.5
        mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
        degenerate = mid_lens < 1e-6
        if np.any(degenerate):
            mid_tangents[degenerate] = seg_tangents[:-1][degenerate]
            mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
            mid_lens = np.where(mid_lens < 1e-6, 1.0, mid_lens)
        mid_tangents /= mid_lens[:, None]
        normals[1:-1] = np.column_stack((-mid_tangents[:, 1], mid_tangents[:, 0]))

    deck_width = float(road.width_m + SLAB_EXTRA_WIDTH_M)
    deck_half = deck_width * 0.5
    thickness = SLAB_THICKNESS_M
    top_target = deck_geom if deck_geom is not None else struct_geom

    # 1. Deck slab cross-sections
    tl: list[tuple[float, float, float]] = []
    tr: list[tuple[float, float, float]] = []
    br: list[tuple[float, float, float]] = []
    bl: list[tuple[float, float, float]] = []
    sl: list[tuple[float, float, float]] = []
    sr: list[tuple[float, float, float]] = []

    for i in range(n):
        px, py = float(pts[i, 0]), float(pts[i, 1])
        nx, ny = float(normals[i, 0]), float(normals[i, 1])
        top_z = float(deck_z[i])
        bot_z = top_z - thickness
        skirt_z = top_z - TOP_SKIRT_M
        tl.append((px - deck_half * nx, py - deck_half * ny, top_z))
        tr.append((px + deck_half * nx, py + deck_half * ny, top_z))
        br.append((px + deck_half * nx, py + deck_half * ny, bot_z))
        bl.append((px - deck_half * nx, py - deck_half * ny, bot_z))
        sl.append((px - deck_half * nx, py - deck_half * ny, skirt_z))
        sr.append((px + deck_half * nx, py + deck_half * ny, skirt_z))

    cum_s = 0.0
    for i in range(n - 1):
        ds = float(raw_seg_lens[i])
        s0, s1 = cum_s * 0.25, (cum_s + ds) * 0.25
        cum_s += ds

        # Top face (+Z) on decal-receiving deck mesh
        top_target.add_quad(
            tl[i], tl[i + 1], tr[i + 1], tr[i],
            _quad_face_normal(tl[i], tl[i + 1], tr[i + 1], (0.0, 0.0, 1.0)),
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )
        if deck_geom is not None:
            # 0.15m top skirt + underside so _deck is a closed non-zero-height box for BeamNG DecalRoad
            deck_geom.add_quad(
                sl[i], sl[i + 1], tl[i + 1], tl[i],
                _quad_face_normal(sl[i], sl[i + 1], tl[i + 1], (-float(normals[i, 0]), -float(normals[i, 1]), 0.0)),
                (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
            )
            deck_geom.add_quad(
                tr[i], tr[i + 1], sr[i + 1], sr[i],
                _quad_face_normal(tr[i], tr[i + 1], sr[i + 1], (float(normals[i, 0]), float(normals[i, 1]), 0.0)),
                (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
            )
            deck_geom.add_quad(
                sl[i], sr[i], sr[i + 1], sl[i + 1],
                _quad_face_normal(sl[i], sr[i], sr[i + 1], (0.0, 0.0, -1.0)),
                (0.0, s0), (1.0, s0), (1.0, s1), (0.0, s1),
            )

        # Bottom face (-Z) & slab sides on structural mesh
        struct_geom.add_quad(
            bl[i], br[i], br[i + 1], bl[i + 1],
            _quad_face_normal(bl[i], br[i], br[i + 1], (0.0, 0.0, -1.0)),
            (0.0, s0), (1.0, s0), (1.0, s1), (0.0, s1),
        )
        struct_geom.add_quad(
            bl[i], bl[i + 1], tl[i + 1], tl[i],
            _quad_face_normal(bl[i], bl[i + 1], tl[i + 1], (-float(normals[i, 0]), -float(normals[i, 1]), 0.0)),
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )
        struct_geom.add_quad(
            tr[i], tr[i + 1], br[i + 1], br[i],
            _quad_face_normal(tr[i], tr[i + 1], br[i + 1], (float(normals[i, 0]), float(normals[i, 1]), 0.0)),
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )

    # Deck end caps
    t0 = seg_tangents[0]
    t_end = seg_tangents[-1]
    if deck_geom is not None:
        deck_geom.add_quad(tl[0], tr[0], sr[0], sl[0], (-float(t0[0]), -float(t0[1]), 0.0))
        deck_geom.add_quad(tl[-1], sl[-1], sr[-1], tr[-1], (float(t_end[0]), float(t_end[1]), 0.0))
    struct_geom.add_quad(tl[0], tr[0], br[0], bl[0], (-float(t0[0]), -float(t0[1]), 0.0))
    struct_geom.add_quad(tl[-1], bl[-1], br[-1], tr[-1], (float(t_end[0]), float(t_end[1]), 0.0))

    # Longitudinal box girders beneath slab for multi-span / long bridges (> 12m)
    cum_lens = np.concatenate(([0.0], np.cumsum(raw_seg_lens)))
    total_len = float(cum_lens[-1])
    if total_len > 12.0:
        g_half = GIRDER_WIDTH_M * 0.5
        for g_off in (-max(1.0, deck_half - 1.2), max(1.0, deck_half - 1.2)):
            for i in range(n - 1):
                nx0, ny0 = float(normals[i, 0]), float(normals[i, 1])
                nx1, ny1 = float(normals[i + 1, 0]), float(normals[i + 1, 1])
                px0, py0, zb0 = float(pts[i, 0]), float(pts[i, 1]), float(deck_z[i]) - thickness
                px1, py1, zb1 = float(pts[i + 1, 0]), float(pts[i + 1, 1]), float(deck_z[i + 1]) - thickness
                zd0, zd1 = zb0 - GIRDER_DEPTH_M, zb1 - GIRDER_DEPTH_M

                gl0 = (px0 + (g_off - g_half) * nx0, py0 + (g_off - g_half) * ny0, zd0)
                gr0 = (px0 + (g_off + g_half) * nx0, py0 + (g_off + g_half) * ny0, zd0)
                glt0 = (px0 + (g_off - g_half) * nx0, py0 + (g_off - g_half) * ny0, zb0)
                grt0 = (px0 + (g_off + g_half) * nx0, py0 + (g_off + g_half) * ny0, zb0)

                gl1 = (px1 + (g_off - g_half) * nx1, py1 + (g_off - g_half) * ny1, zd1)
                gr1 = (px1 + (g_off + g_half) * nx1, py1 + (g_off + g_half) * ny1, zd1)
                glt1 = (px1 + (g_off - g_half) * nx1, py1 + (g_off - g_half) * ny1, zb1)
                grt1 = (px1 + (g_off + g_half) * nx1, py1 + (g_off + g_half) * ny1, zb1)

                struct_geom.add_quad(gl0, gl1, glt1, glt0, _quad_face_normal(gl0, gl1, glt1, (-nx0, -ny0, 0.0)))
                struct_geom.add_quad(grt0, grt1, gr1, gr0, _quad_face_normal(grt0, grt1, gr1, (nx0, ny0, 0.0)))
                struct_geom.add_quad(gl0, gr0, gr1, gl1, _quad_face_normal(gl0, gr0, gr1, (0.0, 0.0, -1.0)))

    # 2. Guardrails / Parapets (0.85m high x 0.35m wide safety barriers on struct_geom)
    gh = GUARDRAIL_HEIGHT_M
    gw = GUARDRAIL_WIDTH_M

    for i in range(n - 1):
        nx0, ny0 = float(normals[i, 0]), float(normals[i, 1])
        nx1, ny1 = float(normals[i + 1, 0]), float(normals[i + 1, 1])
        px0, py0, z0 = float(pts[i, 0]), float(pts[i, 1]), float(deck_z[i])
        px1, py1, z1 = float(pts[i + 1, 0]), float(pts[i + 1, 1]), float(deck_z[i + 1])

        l_out_bot0 = (px0 - deck_half * nx0, py0 - deck_half * ny0, z0)
        l_out_top0 = (px0 - deck_half * nx0, py0 - deck_half * ny0, z0 + gh)
        l_in_top0 = (px0 - (deck_half - gw) * nx0, py0 - (deck_half - gw) * ny0, z0 + gh)
        l_in_bot0 = (px0 - (deck_half - gw) * nx0, py0 - (deck_half - gw) * ny0, z0)

        l_out_bot1 = (px1 - deck_half * nx1, py1 - deck_half * ny1, z1)
        l_out_top1 = (px1 - deck_half * nx1, py1 - deck_half * ny1, z1 + gh)
        l_in_top1 = (px1 - (deck_half - gw) * nx1, py1 - (deck_half - gw) * ny1, z1 + gh)
        l_in_bot1 = (px1 - (deck_half - gw) * nx1, py1 - (deck_half - gw) * ny1, z1)

        struct_geom.add_quad(
            l_out_top0, l_out_top1, l_in_top1, l_in_top0,
            _quad_face_normal(l_out_top0, l_out_top1, l_in_top1, (0.0, 0.0, 1.0)),
        )
        struct_geom.add_quad(
            l_in_top0, l_in_top1, l_in_bot1, l_in_bot0,
            _quad_face_normal(l_in_top0, l_in_top1, l_in_bot1, (nx0, ny0, 0.0)),
        )
        struct_geom.add_quad(
            l_out_bot0, l_out_bot1, l_out_top1, l_out_top0,
            _quad_face_normal(l_out_bot0, l_out_bot1, l_out_top1, (-nx0, -ny0, 0.0)),
        )

        r_in_bot0 = (px0 + (deck_half - gw) * nx0, py0 + (deck_half - gw) * ny0, z0)
        r_in_top0 = (px0 + (deck_half - gw) * nx0, py0 + (deck_half - gw) * ny0, z0 + gh)
        r_out_top0 = (px0 + deck_half * nx0, py0 + deck_half * ny0, z0 + gh)
        r_out_bot0 = (px0 + deck_half * nx0, py0 + deck_half * ny0, z0)

        r_in_bot1 = (px1 + (deck_half - gw) * nx1, py1 + (deck_half - gw) * ny1, z1)
        r_in_top1 = (px1 + (deck_half - gw) * nx1, py1 + (deck_half - gw) * ny1, z1 + gh)
        r_out_top1 = (px1 + deck_half * nx1, py1 + deck_half * ny1, z1 + gh)
        r_out_bot1 = (px1 + deck_half * nx1, py1 + deck_half * ny1, z1)

        struct_geom.add_quad(
            r_in_top0, r_in_top1, r_out_top1, r_out_top0,
            _quad_face_normal(r_in_top0, r_in_top1, r_out_top1, (0.0, 0.0, 1.0)),
        )
        struct_geom.add_quad(
            r_in_bot0, r_in_bot1, r_in_top1, r_in_top0,
            _quad_face_normal(r_in_bot0, r_in_bot1, r_in_top1, (-nx0, -ny0, 0.0)),
        )
        struct_geom.add_quad(
            r_out_top0, r_out_top1, r_out_bot1, r_out_bot0,
            _quad_face_normal(r_out_top0, r_out_top1, r_out_bot1, (nx0, ny0, 0.0)),
        )

    # Guardrail end caps
    nx0, ny0 = float(normals[0, 0]), float(normals[0, 1])
    px0, py0, z0 = float(pts[0, 0]), float(pts[0, 1]), float(deck_z[0])
    struct_geom.add_quad(
        (px0 - deck_half * nx0, py0 - deck_half * ny0, z0 + gh),
        (px0 - (deck_half - gw) * nx0, py0 - (deck_half - gw) * ny0, z0 + gh),
        (px0 - (deck_half - gw) * nx0, py0 - (deck_half - gw) * ny0, z0),
        (px0 - deck_half * nx0, py0 - deck_half * ny0, z0),
        (-float(t0[0]), -float(t0[1]), 0.0),
    )
    struct_geom.add_quad(
        (px0 + (deck_half - gw) * nx0, py0 + (deck_half - gw) * ny0, z0 + gh),
        (px0 + deck_half * nx0, py0 + deck_half * ny0, z0 + gh),
        (px0 + deck_half * nx0, py0 + deck_half * ny0, z0),
        (px0 + (deck_half - gw) * nx0, py0 + (deck_half - gw) * ny0, z0),
        (-float(t0[0]), -float(t0[1]), 0.0),
    )

    nx1, ny1 = float(normals[-1, 0]), float(normals[-1, 1])
    px1, py1, z1 = float(pts[-1, 0]), float(pts[-1, 1]), float(deck_z[-1])
    struct_geom.add_quad(
        (px1 - deck_half * nx1, py1 - deck_half * ny1, z1 + gh),
        (px1 - deck_half * nx1, py1 - deck_half * ny1, z1),
        (px1 - (deck_half - gw) * nx1, py1 - (deck_half - gw) * ny1, z1),
        (px1 - (deck_half - gw) * nx1, py1 - (deck_half - gw) * ny1, z1 + gh),
        (float(t_end[0]), float(t_end[1]), 0.0),
    )
    struct_geom.add_quad(
        (px1 + (deck_half - gw) * nx1, py1 + (deck_half - gw) * ny1, z1 + gh),
        (px1 + (deck_half - gw) * nx1, py1 + (deck_half - gw) * ny1, z1),
        (px1 + deck_half * nx1, py1 + deck_half * ny1, z1),
        (px1 + deck_half * nx1, py1 + deck_half * ny1, z1 + gh),
        (float(t_end[0]), float(t_end[1]), 0.0),
    )

    # 3. Abutments + 1.6m end lips (0.03m sunk to bridge grid-cell portal notches)
    def add_abutment(
        pt: tuple[float, float],
        z_deck: float,
        adj_pt: tuple[float, float],
        z_adj: float,
        tangent: np.ndarray,
        normal: np.ndarray,
    ) -> None:
        px, py = pt
        tx, ty = float(tangent[0]), float(tangent[1])
        nx, ny = float(normal[0]), float(normal[1])
        top_z = z_deck - thickness
        ground = ground_sampler(px, py) if ground_sampler is not None else (top_z - 4.0)
        bot_z = min(top_z - 1.0, ground - PIER_FOOTING_M)

        w_half = deck_half
        l_len = ABUTMENT_LENGTH_M
        c000 = (px - l_len * tx - w_half * nx, py - l_len * ty - w_half * ny, bot_z)
        c100 = (px - w_half * nx, py - w_half * ny, bot_z)
        c110 = (px + w_half * nx, py + w_half * ny, bot_z)
        c010 = (px - l_len * tx + w_half * nx, py - l_len * ty + w_half * ny, bot_z)
        c001 = (px - l_len * tx - w_half * nx, py - l_len * ty - w_half * ny, top_z)
        c101 = (px - w_half * nx, py - w_half * ny, top_z)
        c111 = (px + w_half * nx, py + w_half * ny, top_z)
        c011 = (px - l_len * tx + w_half * nx, py - l_len * ty + w_half * ny, top_z)
        struct_geom.add_box(c000, c100, c110, c010, c001, c101, c111, c011)

        # End lip extending END_LIP_M past portal along deck tangent, sunk END_LIP_UNDER_M
        seg_d = max(1e-3, math.hypot(px - adj_pt[0], py - adj_pt[1]))
        lip_rise = ((z_deck - z_adj) / seg_d) * END_LIP_M
        lip_top0 = z_deck - END_LIP_UNDER_M
        lip_top1 = lip_top0 + lip_rise - 0.03
        lip_bot0 = lip_top0 - thickness
        lip_bot1 = lip_top1 - thickness
        lx_out = px - END_LIP_M * tx
        ly_out = py - END_LIP_M * ty

        l000 = (lx_out - w_half * nx, ly_out - w_half * ny, lip_bot1)
        l100 = (px - w_half * nx, py - w_half * ny, lip_bot0)
        l110 = (px + w_half * nx, py + w_half * ny, lip_bot0)
        l010 = (lx_out + w_half * nx, ly_out + w_half * ny, lip_bot1)
        l001 = (lx_out - w_half * nx, ly_out - w_half * ny, lip_top1)
        l101 = (px - w_half * nx, py - w_half * ny, lip_top0)
        l111 = (px + w_half * nx, py + w_half * ny, lip_top0)
        l011 = (lx_out + w_half * nx, ly_out + w_half * ny, lip_top1)
        struct_geom.add_box(l000, l100, l110, l010, l001, l101, l111, l011)

    add_abutment(
        (float(pts[0, 0]), float(pts[0, 1])),
        float(deck_z[0]),
        (float(pts[1, 0]), float(pts[1, 1])),
        float(deck_z[1]),
        seg_tangents[0],
        normals[0],
    )
    add_abutment(
        (float(pts[-1, 0]), float(pts[-1, 1])),
        float(deck_z[-1]),
        (float(pts[-2, 0]), float(pts[-2, 1])),
        float(deck_z[-2]),
        -seg_tangents[-1],
        -normals[-1],
    )

    # 4. Concrete piers every 25m grounded 1.5m below carved riverbed
    pier_dist = PIER_SPACING_M
    while pier_dist < total_len - 12.0:
        idx = int(np.searchsorted(cum_lens, pier_dist, side="right") - 1)
        idx = max(0, min(idx, len(raw_seg_lens) - 1))
        d0 = float(cum_lens[idx])
        slen = float(raw_seg_lens[idx])
        t = 0.0 if slen <= 1e-6 else (pier_dist - d0) / slen

        px = float(pts[idx, 0] + t * (pts[idx + 1, 0] - pts[idx, 0]))
        py = float(pts[idx, 1] + t * (pts[idx + 1, 1] - pts[idx, 1]))
        pz = float(deck_z[idx] + t * (deck_z[idx + 1] - deck_z[idx]))

        tx, ty = float(seg_tangents[idx, 0]), float(seg_tangents[idx, 1])
        nx, ny = float(normals[idx, 0]), float(normals[idx, 1])

        top_z = pz - thickness
        bed_z = ground_sampler(px, py) if ground_sampler is not None else (top_z - 6.0)
        bot_z = min(top_z - 1.0, bed_z - PIER_FOOTING_M)

        p_width = max(2.0, deck_width - 1.6)
        pw_half = p_width * 0.5
        pl_half = PIER_THICKNESS_M * 0.5

        c000 = (px - pl_half * tx - pw_half * nx, py - pl_half * ty - pw_half * ny, bot_z)
        c100 = (px + pl_half * tx - pw_half * nx, py + pl_half * ty - pw_half * ny, bot_z)
        c110 = (px + pl_half * tx + pw_half * nx, py + pl_half * ty + pw_half * ny, bot_z)
        c010 = (px - pl_half * tx + pw_half * nx, py - pl_half * ty + pw_half * ny, bot_z)
        c001 = (px - pl_half * tx - pw_half * nx, py - pl_half * ty - pw_half * ny, top_z)
        c101 = (px + pl_half * tx - pw_half * nx, py + pl_half * ty - pw_half * ny, top_z)
        c111 = (px + pl_half * tx + pw_half * nx, py + pl_half * ty + pw_half * ny, top_z)
        c011 = (px - pl_half * tx + pw_half * nx, py - pl_half * ty + pw_half * ny, top_z)
        struct_geom.add_box(c000, c100, c110, c010, c001, c101, c111, c011)

        pier_dist += PIER_SPACING_M


def build_bridge_geometry(
    road: Road,
    deck_z: np.ndarray,
    ground_sampler: Callable[[float, float], float] | None = None,
    *,
    localize: bool = False,
) -> BridgeGeometry:
    """Generate complete 3D procedural bridge geometry.

    Includes deck slab manifold, guardrails, abutments, end lips, and piers.
    """
    geom = BridgeGeometry()
    _populate_bridge_geometry(road, deck_z, ground_sampler, deck_geom=None, struct_geom=geom)
    if localize and geom.positions:
        ox, oy, oz = geom.center
        geom.positions = [(px - ox, py - oy, pz - oz) for px, py, pz in geom.positions]
        geom.origin = (ox, oy, oz)
    return geom


def build_bridge_split_geometry(
    road: Road,
    deck_z: np.ndarray,
    ground_sampler: Callable[[float, float], float] | None = None,
    *,
    localize: bool = True,
) -> tuple[BridgeGeometry, BridgeGeometry]:
    """Build split ``(deck_geom, structure_geom)`` sharing the same centroid origin.

    ``deck_geom`` carries only the road deck top + 0.15m edge skirt for
    ``decalType="Visible Mesh"``, while ``structure_geom`` carries parapets,
    girders, end lips, abutments, and grounded piers for ``decalType="None"``.
    """
    deck_geom = BridgeGeometry()
    struct_geom = BridgeGeometry()
    _populate_bridge_geometry(road, deck_z, ground_sampler, deck_geom=deck_geom, struct_geom=struct_geom)
    if localize and struct_geom.positions:
        ox, oy, oz = struct_geom.center
        deck_geom.positions = [(px - ox, py - oy, pz - oz) for px, py, pz in deck_geom.positions]
        deck_geom.origin = (ox, oy, oz)
        struct_geom.positions = [(px - ox, py - oy, pz - oz) for px, py, pz in struct_geom.positions]
        struct_geom.origin = (ox, oy, oz)
    return deck_geom, struct_geom


def localize_bridge_geometry(
    geom: BridgeGeometry,
    origin: tuple[float, float, float] | None = None,
) -> BridgeGeometry:
    """Return a copy of ``geom`` with positions localized around ``origin``.

    If ``origin`` is omitted, ``geom.center`` is used. Safe to call on an
    already-localized ``BridgeGeometry`` (computes delta relative to ``geom.origin``).
    """
    target = geom.center if origin is None else (float(origin[0]), float(origin[1]), float(origin[2]))
    dx = target[0] - geom.origin[0]
    dy = target[1] - geom.origin[1]
    dz = target[2] - geom.origin[2]
    return BridgeGeometry(
        positions=[(px - dx, py - dy, pz - dz) for px, py, pz in geom.positions],
        normals=list(geom.normals),
        uvs=list(geom.uvs),
        triangles=list(geom.triangles),
        origin=target,
    )


def _format_floats(values: Sequence[float]) -> str:
    """Format a sequence of floats compactly."""
    return " ".join(f"{v:.4f}".rstrip("0").rstrip(".") or "0" for v in values)


def export_bridge_dae(
    path: Path,
    geom: BridgeGeometry,
    bridge_name: str,
    *,
    origin: tuple[float, float, float] | None = None,
) -> int:
    """Write bridge geometry to a BeamNG-compatible COLLADA 1.4.1 file.

    Args:
        path: Output file path.
        geom: Geometry container.
        bridge_name: Shape name (e.g. ``bridge_way12345``).
        origin: Optional world-space origin ``(ox, oy, oz)`` to subtract from
            ``geom.positions`` so the exported mesh stays near local ``(0, 0, 0)``.
            Accounts for ``geom.origin`` so localized meshes are never shifted twice.

    Returns:
        Number of bytes written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if origin is None:
        dx = dy = dz = 0.0
    else:
        dx = float(origin[0]) - geom.origin[0]
        dy = float(origin[1]) - geom.origin[1]
        dz = float(origin[2]) - geom.origin[2]

    if dx == 0.0 and dy == 0.0 and dz == 0.0:
        pos_flat = [coord for p in geom.positions for coord in p]
    else:
        pos_flat = [coord for p in geom.positions for coord in (p[0] - dx, p[1] - dy, p[2] - dz)]
    norm_flat = [coord for n in geom.normals for coord in n]
    uv_flat = [coord for uv in geom.uvs for coord in uv]

    pos_str = _format_floats(pos_flat)
    norm_str = _format_floats(norm_flat)
    uv_str = _format_floats(uv_flat)

    p_entries: list[str] = []
    for tri in geom.triangles:
        for vi, ni, uvi in tri:
            p_entries.append(f"{vi} {ni} {uvi}")
    p_str = " ".join(p_entries)

    mat_name = BRIDGE_DECAL_MATERIAL
    safe_name = escape(bridge_name)

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor>
      <author>ivanovo2beam</author>
      <authoring_tool>ivanovo2beam 3D bridge generator</authoring_tool>
    </contributor>
    <created>{_FIXED_TIMESTAMP}</created>
    <modified>{_FIXED_TIMESTAMP}</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="{safe_name}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse>
              <color>0.7 0.7 0.7 1</color>
            </diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{mat_name}" name="{mat_name}">
      <instance_effect url="#{safe_name}-effect"/>
    </material>
  </library_materials>
  <library_geometries>
    <geometry id="{safe_name}-mesh" name="{safe_name}">
      <mesh>
        <source id="{safe_name}-mesh-positions">
          <float_array id="{safe_name}-mesh-positions-array" count="{len(pos_flat)}">{pos_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-positions-array" count="{len(geom.positions)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{safe_name}-mesh-normals">
          <float_array id="{safe_name}-mesh-normals-array" count="{len(norm_flat)}">{norm_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-normals-array" count="{len(geom.normals)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{safe_name}-mesh-map-0">
          <float_array id="{safe_name}-mesh-map-0-array" count="{len(uv_flat)}">{uv_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-map-0-array" count="{len(geom.uvs)}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{safe_name}-mesh-vertices">
          <input semantic="POSITION" source="#{safe_name}-mesh-positions"/>
        </vertices>
        <triangles material="{mat_name}" count="{len(geom.triangles)}">
          <input semantic="VERTEX" source="#{safe_name}-mesh-vertices" offset="0"/>
          <input semantic="NORMAL" source="#{safe_name}-mesh-normals" offset="1"/>
          <input semantic="TEXCOORD" source="#{safe_name}-mesh-map-0" offset="2" set="0"/>
          <p>{p_str}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="{safe_name}" name="{safe_name}" type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <instance_geometry url="#{safe_name}-mesh" name="{safe_name}">
          <bind_material>
            <technique_common>
              <instance_material symbol="{mat_name}" target="#{mat_name}"/>
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#Scene"/>
  </scene>
</COLLADA>
"""
    data = xml.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def emit_bridges_materials(level_dir: Path, level_id: str) -> Path:
    """Emit ``levels/<id>/art/shapes/bridges/main.materials.json``.

    Maps ``ivanovo_bridge_deck`` to BeamNG native precast concrete PBR textures.
    """
    materials_path = level_dir / "art" / "shapes" / "bridges" / "main.materials.json"
    materials_path.parent.mkdir(parents=True, exist_ok=True)

    materials_doc = {
        BRIDGE_DECAL_MATERIAL: {
            "name": BRIDGE_DECAL_MATERIAL,
            "mapTo": BRIDGE_DECAL_MATERIAL,
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", BRIDGE_DECAL_MATERIAL),
            "Stages": [
                {
                    "ambientOcclusionMap": CONCRETE_PRECAST_TEXTURES["ao"],
                    "baseColorMap": CONCRETE_PRECAST_TEXTURES["b"],
                    "normalMap": CONCRETE_PRECAST_TEXTURES["nm"],
                    "roughnessMap": CONCRETE_PRECAST_TEXTURES["r"],
                },
                {},
                {},
                {},
            ],
            "version": 1.5,
        }
    }

    materials_path.write_text(json.dumps(materials_doc, indent=2), encoding="utf-8")
    return materials_path


def build_bridge_scene_object(
    road: Road,
    level_id: str,
    *,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    shape_path: str | None = None,
    parent: str = "bridges",
    part: str | None = None,
) -> dict[str, Any]:
    """Place a bridge mesh as a BeamNG ``TSStatic`` scene object.

    Args:
        road: Bridge road descriptor.
        level_id: BeamNG level identifier.
        position: World-space origin matching localized DAE vertices.
        shape_path: Optional override for ``shapeName``.
        parent: Scene group parent name.
        part: ``"deck"`` (``decalType="Visible Mesh"``), ``"structure"``
            (``decalType="None"``), or ``None`` for legacy combined mesh.
    """
    base_key = f"bridge_way{road.osm_id}"
    key = f"{base_key}_deck" if part == "deck" else base_key
    if shape_path is None:
        shape_path = f"/levels/{level_id}/art/shapes/bridges/{key}.dae"
    decal_type = "None" if part == "structure" else "Visible Mesh"

    return {
        "class": "TSStatic",
        "persistentId": stable_uuid(level_id, "TSStatic", key),
        "__parent": parent,
        "position": [float(position[0]), float(position[1]), float(position[2])],
        "shapeName": shape_path,
        "scale": [1.0, 1.0, 1.0],
        "collisionType": "Visible Mesh Final",
        "decalType": decal_type,
    }


def export_bridge_bundle(
    road: Road,
    deck_z: np.ndarray,
    level_id: str,
    bridges_dir: Path,
    *,
    ground_sampler: Callable[[float, float], float] | None = None,
    parent: str = "bridges",
) -> list[dict[str, Any]]:
    """Export split ``_deck`` and structural bridge DAEs + TSStatic scene objects."""
    deck_geom, struct_geom = build_bridge_split_geometry(
        road,
        deck_z,
        ground_sampler=ground_sampler,
        localize=True,
    )
    base_name = f"bridge_way{road.osm_id}"
    export_bridge_dae(bridges_dir / f"{base_name}_deck.dae", deck_geom, f"{base_name}_deck")
    export_bridge_dae(bridges_dir / f"{base_name}.dae", struct_geom, base_name)
    return [
        build_bridge_scene_object(road, level_id, position=deck_geom.origin, parent=parent, part="deck"),
        build_bridge_scene_object(road, level_id, position=struct_geom.origin, parent=parent, part="structure"),
    ]


def create_bridgehead_ramps(
    road: Road,
    settings: RoadSettings,
    deck_z: np.ndarray,
    ground_sampler: Callable[[float, float], float] | None = None,
    ramp_length_m: float | None = None,
    *,
    max_grade: float = RAMP_GRADE,
    min_ramp_length_m: float = 70.0,
    far_z_override: tuple[float | None, float | None] | None = None,
) -> list[RoadCorridor]:
    """Generate approach ramp corridors pinned to deck endpoints with 0cm step lip.

    Grading ramps smoothly connect existing terrain to the bridge deck level,
    eliminating any vertical barrier or collision step at bridge entrances.
    """
    from ivanovo2beam.roads.build import corridor_half_width_for
    from ivanovo2beam.terrain.builder import RoadCorridor

    pts = road.points
    if pts.shape[0] < 2:
        return []

    corridors: list[RoadCorridor] = []
    half_w = road.half_width_m
    corridor_half = corridor_half_width_for(road, settings)
    shoulder_material_key = (
        "Grass"
        if road.material_key in {"Asphalt", "Asphalt_worn", "Concrete"}
        else ("Dirt" if road.material_key == "Dirt" else "Gravel")
    )

    def _ramp_length(pt: np.ndarray, u_dir: np.ndarray, z_pin: float) -> float:
        if ramp_length_m is not None:
            return float(ramp_length_m)
        probe = pt + u_dir * min_ramp_length_m
        z_probe = ground_sampler(float(probe[0]), float(probe[1])) if ground_sampler is not None else z_pin
        dz = abs(z_pin - z_probe)
        return float(max(min_ramp_length_m, min(160.0, dz / max(0.01, max_grade))))

    def _cosine_ramp(p0: np.ndarray, p1: np.ndarray, z0: float, z1: float, step_m: float = 4.0) -> tuple[np.ndarray, np.ndarray]:
        dist = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        n_pts = max(2, round(dist / step_m) + 1)
        ts = np.linspace(0.0, 1.0, n_pts)
        r_pts = p0[None, :] + ts[:, None] * (p1 - p0)[None, :]
        # Smooth cosine transition pinned exactly to z0 and z1 at endpoints
        s = 0.5 * (1.0 - np.cos(math.pi * ts))
        r_z = z0 + s * (z1 - z0)
        return r_pts, r_z

    # 1. Start bridgehead ramp (p_far -> pts[0])
    v_start = pts[0] - pts[1]
    v_start_len = float(np.hypot(v_start[0], v_start[1]))
    if v_start_len > 1e-4:
        u_start = v_start / v_start_len
        r_len = _ramp_length(pts[0], u_start, float(deck_z[0]))
        p_far = pts[0] + u_start * r_len
        z_pin = float(deck_z[0])
        if far_z_override is not None and far_z_override[0] is not None:
            z_far = float(far_z_override[0])
        elif ground_sampler is not None:
            z_far = ground_sampler(float(p_far[0]), float(p_far[1]))
        else:
            z_far = z_pin
        ramp_pts, ramp_z = _cosine_ramp(p_far, pts[0], z_far, z_pin)
        corridors.append(
            RoadCorridor(
                points=ramp_pts,
                half_width_m=half_w,
                corridor_half_width_m=corridor_half,
                priority=road.priority + 20,
                grade_terrain=True,
                material_key=road.material_key,
                shoulder_material_key=shoulder_material_key,
                key=f"ramp_start_way{road.osm_id}",
                surface_z=ramp_z,
            )
        )

    # 2. End bridgehead ramp (pts[-1] -> p_far)
    v_end = pts[-1] - pts[-2]
    v_end_len = float(np.hypot(v_end[0], v_end[1]))
    if v_end_len > 1e-4:
        u_end = v_end / v_end_len
        r_len = _ramp_length(pts[-1], u_end, float(deck_z[-1]))
        p_far = pts[-1] + u_end * r_len
        z_pin = float(deck_z[-1])
        if far_z_override is not None and far_z_override[1] is not None:
            z_far = float(far_z_override[1])
        elif ground_sampler is not None:
            z_far = ground_sampler(float(p_far[0]), float(p_far[1]))
        else:
            z_far = z_pin
        ramp_pts, ramp_z = _cosine_ramp(pts[-1], p_far, z_pin, z_far)
        corridors.append(
            RoadCorridor(
                points=ramp_pts,
                half_width_m=half_w,
                corridor_half_width_m=corridor_half,
                priority=road.priority + 20,
                grade_terrain=True,
                material_key=road.material_key,
                shoulder_material_key=shoulder_material_key,
                key=f"ramp_end_way{road.osm_id}",
                surface_z=ramp_z,
            )
        )

    return corridors
