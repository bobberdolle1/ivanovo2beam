"""Terrain building: DEM resampling plus road corridor grading.

This is where the map's sense of place comes from. Two things have to be true
simultaneously and they pull in opposite directions:

* the ground must look like the real Ivanovo Oblast, so it is driven by
  Copernicus DEM elevations; and
* roads must be *drivable*, which a 30 m DEM cannot deliver on its own, because
  a 30 m sample spacing can put a metre-high step inside a 7 m wide carriageway.

The resolution of that tension is the architecture the project uses throughout:
**a coarse world terrain with a graded, high-resolution road corridor carved
into it**. Real DEM elevations are never destroyed; where a road runs, the
terrain is re-profiled to a smooth longitudinal gradient that respects a
maximum grade, blended back into the surrounding ground over a shoulder.

Because BeamNG draws roads as decals over the terrain and the terrain *is* the
collision surface, carving the corridor is not a cosmetic step -- it is what
makes the road exist physically.

Grid convention
---------------
The terrain grid is indexed ``[row, col]`` and maps onto world metres as::

    x = origin_x + col * cell_size_m     (east)
    y = origin_y + row * cell_size_m     (north)

so **row 0 is the southern edge**, which is the convention BeamNG's ``.ter``
format uses (verified against the shipped template level: the spawn sphere at
y = 70.16 sits on the terrain height sampled at row 583, not row 441).

Each cell is sampled by transforming its own world coordinate to WGS84 and
asking the DEM for that exact point, rather than resampling a raster window.
That removes any possibility of a silent orientation or half-cell error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import shapely
from numpy.lib.stride_tricks import sliding_window_view
from shapely.geometry import Polygon
from shapely.validation import make_valid

from ivanovo2beam.beamng.materials import DEFAULT_MATERIAL_ORDER
from ivanovo2beam.beamng.ter import TerrainFile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from ivanovo2beam.gis.projection import RegionProjection


class DemSampler(Protocol):
    """The DEM interface the terrain builder depends on.

    :class:`ivanovo2beam.terrain.dem.DemReader` satisfies this. Depending on the
    protocol rather than the concrete class keeps the builder testable with a
    synthetic elevation model and independent of which dataset is configured.
    """

    def sample_array(self, lons: Any, lats: Any) -> NDArray[np.float64]:
        """Return bilinear elevations in metres for arrays of WGS84 points."""
        ...


@dataclass(slots=True)
class RoadCorridor:
    """A road centreline plus the shape of the ground it needs.

    Attributes:
        points: ``(n, 2)`` array of world ``(x, y)`` centreline vertices.
        half_width_m: Half the carriageway width. The surface is flat across it.
        corridor_half_width_m: Half-width of the graded corridor, including
            shoulders. Beyond this the terrain is untouched.
        priority: Junction precedence. Where corridors overlap, the higher
            priority road dominates the resulting surface elevation.
        surface_z: Optional pre-computed longitudinal profile in metres. When
            ``None`` the builder derives one from the DEM.
        grade_terrain: Set ``False`` for bridges and tunnels, which must not
            flatten the ground beneath them.
        material_key: Terrain material painted on the carriageway.
        shoulder_material_key: Terrain material painted on the shoulders.
        key: Stable identifier, used in diagnostics.
    """

    points: NDArray[np.float64]
    half_width_m: float
    corridor_half_width_m: float
    priority: int = 0
    surface_z: NDArray[np.float64] | None = None
    grade_terrain: bool = True
    material_key: str = "Asphalt"
    shoulder_material_key: str = "Gravel"
    key: str = ""

    def __post_init__(self) -> None:
        pts = np.asarray(self.points, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[1] != 2:
            raise ValueError(f"corridor points must be (n, 2), got {pts.shape}")
        if pts.shape[0] < 2:
            raise ValueError(f"a corridor needs at least 2 points, got {pts.shape[0]}")
        if self.half_width_m <= 0:
            raise ValueError(f"half_width_m must be positive, got {self.half_width_m}")
        if self.corridor_half_width_m < self.half_width_m:
            raise ValueError(
                f"corridor_half_width_m ({self.corridor_half_width_m}) must be at least "
                f"half_width_m ({self.half_width_m})"
            )
        self.points = pts
        if self.surface_z is not None:
            z = np.asarray(self.surface_z, dtype=np.float64)
            if z.shape != (pts.shape[0],):
                raise ValueError(f"surface_z must have {pts.shape[0]} entries, got {z.shape}")
            self.surface_z = z

    @property
    def length_m(self) -> float:
        """Centreline length in metres."""
        deltas = np.diff(self.points, axis=0)
        return float(np.hypot(deltas[:, 0], deltas[:, 1]).sum())


@dataclass(slots=True)
class TerrainBuild:
    """The result of building one terrain grid.

    Attributes:
        elevations: ``(n, n)`` float64 elevations in metres above sea level.
        layers: ``(n, n)`` uint8 material indices into :attr:`materials`.
        materials: Material keys, indexed by :attr:`layers`.
        base_z: World Z corresponding to stored height 0, in metres.
        max_height: Vertical range in metres.
        origin_x: World X of the grid's minimum corner.
        origin_y: World Y of the grid's minimum corner.
        cell_size_m: Ground spacing between vertices in metres.
        graded_cells: Number of cells whose elevation a road corridor changed.
        stats: Extra diagnostics for logging and metadata.
    """

    elevations: NDArray[np.float64]
    layers: NDArray[np.uint8]
    materials: tuple[str, ...]
    base_z: float
    max_height: float
    origin_x: float
    origin_y: float
    cell_size_m: float
    graded_cells: int = 0
    stats: dict[str, Any] = field(default_factory=dict)
    satellite_image: str | None = None
    bridge_profiles: dict[int, tuple[NDArray[np.float64], NDArray[np.float64]]] = field(default_factory=dict)

    @property
    def grid_size(self) -> int:
        """Vertices per side."""
        return int(self.elevations.shape[0])

    @property
    def extent_m(self) -> float:
        """Ground extent of the grid in metres."""
        return (self.grid_size - 1) * self.cell_size_m

    def to_terrain_file(self) -> TerrainFile:
        """Quantise this build into a BeamNG :class:`TerrainFile`."""
        return TerrainFile.from_elevations(
            self.elevations,
            self.layers,
            self.materials,
            base_z=self.base_z,
            max_height=self.max_height,
        )


# ---------------------------------------------------------------------------
# DEM sampling onto the grid
# ---------------------------------------------------------------------------


def sample_dem_onto_grid(
    dem: DemSampler,
    projection: RegionProjection,
    origin_x: float,
    origin_y: float,
    grid_size: int,
    cell_size_m: float,
    *,
    block_rows: int = 128,
) -> NDArray[np.float64]:
    """Sample a DEM onto the world grid.

    Works in row blocks so peak memory stays small while avoiding per-row
    projection and raster-window overhead on 2048+ grids.

    Args:
        dem: Elevation source.
        projection: World plane transform.
        origin_x: World X of the grid's minimum corner.
        origin_y: World Y of the grid's minimum corner.
        grid_size: Vertices per side.
        cell_size_m: Ground spacing in metres.
        block_rows: Number of rows sampled per DEM batch.

    Returns:
        ``(grid_size, grid_size)`` float64 elevations in metres, indexed
        ``[row, col]`` with row 0 at the southern edge.
    """
    x_vector = origin_x + np.arange(grid_size, dtype=np.float64) * cell_size_m
    y_vector = origin_y + np.arange(grid_size, dtype=np.float64) * cell_size_m
    out = np.empty((grid_size, grid_size), dtype=np.float64)
    step = max(1, block_rows)
    for row_start in range(0, grid_size, step):
        row_end = min(grid_size, row_start + step)
        rows = row_end - row_start
        xs = np.broadcast_to(x_vector[None, :], (rows, grid_size)).ravel()
        ys = np.broadcast_to(y_vector[row_start:row_end, None], (rows, grid_size)).ravel()
        lons, lats = projection.world_to_geo_array(xs, ys)
        samples = np.asarray(dem.sample_array(lons, lats), dtype=np.float64)
        out[row_start:row_end, :] = samples.reshape(rows, grid_size)
    return out


# ---------------------------------------------------------------------------
# Road profile derivation
# ---------------------------------------------------------------------------


def resample_polyline(
    points: NDArray[np.float64],
    step_m: float,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Resample a polyline at a near-uniform arc length.

    Args:
        points: ``(n, 2)`` vertex array.
        step_m: Target spacing between output points in metres.

    Returns:
        ``(resampled_points, source_arc_lengths)`` where the second array gives,
        for each resampled point, its arc length along the original polyline.

    Raises:
        ValueError: If ``step_m`` is not positive.
    """
    if step_m <= 0:
        raise ValueError(f"step_m must be positive, got {step_m}")
    deltas = np.diff(points, axis=0)
    seg_lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    cumulative = np.concatenate(([0.0], np.cumsum(seg_lengths)))
    total = float(cumulative[-1])
    if total <= 0:
        return points[:1].copy(), np.zeros(1)

    n_steps = max(2, math.ceil(total / step_m) + 1)
    targets = np.linspace(0.0, total, n_steps)
    xs = np.interp(targets, cumulative, points[:, 0])
    ys = np.interp(targets, cumulative, points[:, 1])
    return np.column_stack((xs, ys)), targets


def smooth_profile(z: NDArray[np.float64], window: int) -> NDArray[np.float64]:
    """Smooth a longitudinal profile with a centred moving average.

    Edges are handled by shrinking the window rather than padding, so the ends
    of a road are not pulled towards an invented elevation.

    Args:
        z: Profile samples.
        window: Odd window length in samples. Values below 3 return a copy.

    Returns:
        The smoothed profile, same length as ``z``.
    """
    if window < 3 or z.size < 3:
        return z.copy()
    if window % 2 == 0:
        window += 1
    half = window // 2
    padded = np.pad(z, half, mode="edge")
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(padded, kernel, mode="valid")


def enforce_max_grade(
    z: NDArray[np.float64],
    spacing_m: float,
    max_grade: float,
) -> NDArray[np.float64]:
    """Clamp a profile so no segment exceeds a maximum gradient.

    Runs a forward pass then a backward pass, each limiting the change between
    neighbouring samples. This is the standard earthworks construction: the
    result is the closest profile to the input that never exceeds the grade,
    and it is the reason terrain elevation is *locally* adjusted rather than
    globally destroyed.

    Args:
        z: Profile elevations in metres.
        spacing_m: Distance between consecutive samples in metres.
        max_grade: Maximum absolute gradient (rise over run).

    Returns:
        A new, grade-limited profile.

    Raises:
        ValueError: If ``spacing_m`` or ``max_grade`` are not positive.
    """
    if spacing_m <= 0:
        raise ValueError(f"spacing_m must be positive, got {spacing_m}")
    if max_grade <= 0:
        raise ValueError(f"max_grade must be positive, got {max_grade}")
    limit = max_grade * spacing_m
    out = z.astype(np.float64, copy=True)
    for i in range(1, out.size):
        out[i] = min(max(out[i], out[i - 1] - limit), out[i - 1] + limit)
    for i in range(out.size - 2, -1, -1):
        out[i] = min(max(out[i], out[i + 1] - limit), out[i + 1] + limit)
    return out


def derive_road_profile(
    dem: DemSampler,
    projection: RegionProjection,
    points: NDArray[np.float64],
    *,
    sample_step_m: float = 5.0,
    smooth_window_m: float = 120.0,
    max_grade: float = 0.12,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Derive a smooth, drivable longitudinal profile for a centreline.

    The raw DEM along a road is noisy at the road's own scale; sampling it
    directly would give the player a washboard. The profile is therefore
    resampled, smoothed over a distance comparable to a real vertical curve,
    and finally grade-limited.

    Args:
        dem: Elevation source.
        projection: World plane transform.
        points: ``(n, 2)`` centreline vertices in world metres.
        sample_step_m: Resampling spacing in metres.
        smooth_window_m: Moving-average window length in metres.
        max_grade: Maximum permitted gradient.

    Returns:
        ``(resampled_points, profile_z)``; the points correspond one-to-one with
        the returned elevations.
    """
    resampled, _ = resample_polyline(points, sample_step_m)
    if hasattr(dem, "sample_world_array"):
        raw = np.asarray(dem.sample_world_array(resampled[:, 0], resampled[:, 1]), dtype=np.float64)
    else:
        lons, lats = projection.world_to_geo_array(resampled[:, 0], resampled[:, 1])
        raw = np.asarray(dem.sample_array(lons, lats), dtype=np.float64)

    # A DEM can return NaN at the edge of coverage; fill from neighbours so a
    # single gap does not poison the whole profile.
    if not np.all(np.isfinite(raw)):
        finite = np.isfinite(raw)
        if not finite.any():
            raise ValueError("DEM returned no finite elevations along a road centreline")
        indices = np.arange(raw.size)
        raw = np.interp(indices, indices[finite], raw[finite])

    window = max(3, round(smooth_window_m / sample_step_m))
    smoothed = smooth_profile(raw, window)
    limited = enforce_max_grade(smoothed, sample_step_m, max_grade)
    return resampled, limited


# ---------------------------------------------------------------------------
# Corridor grading
# ---------------------------------------------------------------------------


def _smoothstep(edge0: float, edge1: float, value: NDArray[np.float64]) -> NDArray[np.float64]:
    """Hermite smoothstep, clamped to ``[0, 1]``."""
    if edge1 <= edge0:
        return np.where(np.asarray(value, dtype=np.float64) > edge0, 1.0, 0.0)
    t = np.clip((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def pin_junction_profiles(
    corridors: Sequence[RoadCorridor],
    *,
    ramp_m: float = 40.0,
    through_cos: float = -0.7,
    fork_cos: float = 0.7,
) -> int:
    """Pin shared road intersection endpoints to a unified grade-consistent Z.

    When two opposing arms form a through road (``dot <= through_cos``), the
    through road retains its elevation and side arms bend smoothly into it over
    ``ramp_m``. Otherwise meeting arms converge on a priority-weighted consensus
    elevation. Bridgehead ramp abutment ends (``ramp_*``) stay locked.

    Adapted from MapNG (https://github.com/nikkiluzader/mapng), MIT License.
    """
    active = [c for c in corridors if c.grade_terrain and c.surface_z is not None and c.points.shape[0] >= 2]
    if len(active) < 2:
        return 0

    def _key(pt: NDArray[np.float64]) -> tuple[int, int]:
        return (round(float(pt[0]) * 2.0), round(float(pt[1]) * 2.0))

    def _cum_len(pts: NDArray[np.float64]) -> NDArray[np.float64]:
        d = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1]))
        return np.concatenate(([0.0], np.cumsum(d)))

    def _outward_dir(pts: NDArray[np.float64], cum: NDArray[np.float64], at_end: bool) -> tuple[float, float]:
        n_pts = pts.shape[0]
        i0 = n_pts - 1 if at_end else 0
        step = -1 if at_end else 1
        i = i0
        while 0 <= i + step < n_pts and abs(float(cum[i] - cum[i0])) < 10.0:
            i += step
        dx = float(pts[i, 0] - pts[i0, 0])
        dy = float(pts[i, 1] - pts[i0, 1])
        norm = math.hypot(dx, dy)
        return (dx / norm, dy / norm) if norm > 1e-6 else (0.0, 0.0)

    cums = [_cum_len(c.points) for c in active]
    arms_by_key: dict[tuple[int, int], list[tuple[int, bool, float, float]]] = {}
    for idx, c in enumerate(active):
        k_start = _key(c.points[0])
        k_end = _key(c.points[-1])
        if k_start == k_end:
            continue
        dx0, dy0 = _outward_dir(c.points, cums[idx], False)
        dx1, dy1 = _outward_dir(c.points, cums[idx], True)
        arms_by_key.setdefault(k_start, []).append((idx, False, dx0, dy0))
        arms_by_key.setdefault(k_end, []).append((idx, True, dx1, dy1))

    pinned = 0
    for arms in arms_by_key.values():
        if len(arms) < 2:
            continue

        # Identify opposing through-road arms if 3+ arms meet
        through_indices: set[int] = set()
        if len(arms) >= 3:
            for a in range(len(arms)):
                for b in range(a + 1, len(arms)):
                    dot = arms[a][2] * arms[b][2] + arms[a][3] * arms[b][3]
                    if dot <= through_cos:
                        through_indices.add(a)
                        through_indices.add(b)

        ref_pool = [arms[i] for i in through_indices] if through_indices and len(through_indices) < len(arms) else arms

        w_sum = 0.0
        z_sum = 0.0
        for c_idx, at_end, _, _ in ref_pool:
            c = active[c_idx]
            assert c.surface_z is not None
            w = 1.0 + max(0, c.priority) / 50.0
            z_end = float(c.surface_z[-1] if at_end else c.surface_z[0])
            w_sum += w
            z_sum += w * z_end
        if w_sum <= 0.0:
            continue
        consensus_z = z_sum / w_sum

        for c_idx, at_end, _dx, _dy in arms:
            c = active[c_idx]
            assert c.surface_z is not None
            # Keep bridge-facing portal end of bridgehead ramps pinned to deck_z
            if c.key.startswith("ramp_start_") and at_end:
                continue
            if c.key.startswith("ramp_end_") and not at_end:
                continue

            cum = cums[c_idx]
            total_len = float(cum[-1])
            eff_ramp = min(ramp_m, total_len * 0.5)
            if eff_ramp <= 1e-3:
                continue

            z_arr = c.surface_z
            n_pts = z_arr.size
            if at_end:
                delta = consensus_z - float(z_arr[-1])
                if abs(delta) < 1e-4:
                    continue
                for i in range(n_pts - 1, -1, -1):
                    f = 1.0 - (total_len - float(cum[i])) / eff_ramp
                    if f <= 0.0:
                        break
                    z_arr[i] += delta * f
            else:
                delta = consensus_z - float(z_arr[0])
                if abs(delta) < 1e-4:
                    continue
                for i in range(n_pts):
                    f = 1.0 - float(cum[i]) / eff_ramp
                    if f <= 0.0:
                        break
                    z_arr[i] += delta * f
            pinned += 1

    return pinned


def grade_corridors(
    elevations: NDArray[np.float64],
    layers: NDArray[np.uint8],
    material_index: dict[str, int],
    corridors: Sequence[RoadCorridor],
    *,
    origin_x: float,
    origin_y: float,
    cell_size_m: float,
    min_blend_skirt_m: float = 0.0,
) -> tuple[int, dict[str, Any]]:
    """Carve graded road corridors into an elevation grid, in place.

    Uses winner-takes-stronger priority claim resolution (with near-tie averaging
    at equal-class intersections) plus a 2 m seam blur pass, while keeping
    carriageway material painting strictly inside ``half_width_m``. When
    ``min_blend_skirt_m > 0``, terrain height blends over at least
    ``half_width_m + min_blend_skirt_m`` so roads never leave steep 2 m cliffs.

    Winner-takes-stronger priority resolution and 2 m seam blur adapted from
    MapNG (https://github.com/nikkiluzader/mapng), MIT License.
    """
    n = elevations.shape[0]
    influence = np.zeros((n, n), dtype=np.float32)
    best_claim = np.zeros((n, n), dtype=np.float32)
    tie_z_sum = np.zeros((n, n), dtype=np.float32)
    tie_w_sum = np.zeros((n, n), dtype=np.float32)
    seam_mask = np.zeros((n, n), dtype=bool)
    carriageway_mask = np.zeros((n, n), dtype=bool)
    shoulder_mask = np.zeros((n, n), dtype=bool)
    owner_material = np.zeros((n, n), dtype=np.uint8)
    owner_shoulder = np.zeros((n, n), dtype=np.uint8)
    per_material: dict[str, int] = {}
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m

    tie_tol = np.float32(0.05)
    graded = 0
    for corridor in corridors:
        if not corridor.grade_terrain or corridor.surface_z is None:
            continue
        pts = corridor.points
        z = corridor.surface_z
        half_width = corridor.half_width_m
        corridor_half = corridor.corridor_half_width_m
        blend_half = (
            max(corridor_half, half_width + float(min_blend_skirt_m))
            if (min_blend_skirt_m > 0.0 and corridor_half > half_width)
            else corridor_half
        )
        priority_weight = 1.0 + max(0, corridor.priority) / 100.0

        material_idx = material_index.get(corridor.material_key, 0)
        shoulder_idx = material_index.get(corridor.shoulder_material_key, material_idx)
        touched = 0

        for i in range(pts.shape[0] - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            z0, z1 = z[i], z[i + 1]
            dx, dy = x1 - x0, y1 - y0
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 0:
                continue

            col_lo = max(0, math.floor((min(x0, x1) - blend_half - origin_x) / cell_size_m))
            col_hi = min(n - 1, math.ceil((max(x0, x1) + blend_half - origin_x) / cell_size_m))
            row_lo = max(0, math.floor((min(y0, y1) - blend_half - origin_y) / cell_size_m))
            row_hi = min(n - 1, math.ceil((max(y0, y1) + blend_half - origin_y) / cell_size_m))
            if col_hi < col_lo or row_hi < row_lo:
                continue

            grid_x = all_x[col_lo : col_hi + 1][None, :]
            grid_y = all_y[row_lo : row_hi + 1][:, None]

            rel_x = grid_x - x0
            rel_y = grid_y - y0
            t = np.clip((rel_x * dx + rel_y * dy) / seg_len_sq, 0.0, 1.0)
            closest_x = x0 + t * dx
            closest_y = y0 + t * dy
            dist = np.hypot(grid_x - closest_x, grid_y - closest_y)

            inside = dist <= blend_half
            if not inside.any():
                continue

            if blend_half <= half_width:
                alpha = np.where(dist <= half_width, 1.0, 0.0)
            else:
                flat_half = (
                    min(blend_half, max(half_width, 0.72 * cell_size_m))
                    if cell_size_m <= 4.0
                    else half_width
                )
                if blend_half <= flat_half:
                    alpha = np.where(dist <= flat_half, 1.0, 0.0)
                else:
                    frac = np.clip((dist - flat_half) / (blend_half - flat_half), 0.0, 1.0)
                    alpha = 0.5 * (1.0 + np.cos(math.pi * frac))
            road_z = (z0 + t * (z1 - z0)).astype(np.float32)

            win_inf = influence[row_lo : row_hi + 1, col_lo : col_hi + 1]
            win_claim = best_claim[row_lo : row_hi + 1, col_lo : col_hi + 1]
            win_tz = tie_z_sum[row_lo : row_hi + 1, col_lo : col_hi + 1]
            win_tw = tie_w_sum[row_lo : row_hi + 1, col_lo : col_hi + 1]
            win_seam = seam_mask[row_lo : row_hi + 1, col_lo : col_hi + 1]
            win_cw = carriageway_mask[row_lo : row_hi + 1, col_lo : col_hi + 1]
            win_sh_mask = shoulder_mask[row_lo : row_hi + 1, col_lo : col_hi + 1]
            win_mat = owner_material[row_lo : row_hi + 1, col_lo : col_hi + 1]
            win_sh = owner_shoulder[row_lo : row_hi + 1, col_lo : col_hi + 1]

            seg_alpha = np.where(inside, alpha, 0.0).astype(np.float32)
            seg_claim = (seg_alpha * np.float32(priority_weight)).astype(np.float32)

            # Flag seams where two strong road claims disagree vertically by > 2 cm
            has_prev = win_tw > 0.0
            prev_z = np.where(has_prev, win_tz / np.where(has_prev, win_tw, 1.0), road_z)
            win_seam |= (win_claim >= 0.5) & (seg_claim >= 0.5) & (np.abs(road_z - prev_z) > 0.02)

            np.maximum(win_inf, seg_alpha, out=win_inf)

            wins = inside & (seg_claim > win_claim + tie_tol)
            ties = inside & (~wins) & (seg_claim + tie_tol >= win_claim)

            if wins.any():
                win_claim[wins] = seg_claim[wins]
                win_tz[wins] = seg_claim[wins] * road_z[wins]
                win_tw[wins] = seg_claim[wins]
                win_mat[wins] = material_idx
                win_sh[wins] = shoulder_idx

            if ties.any():
                win_tz[ties] += seg_claim[ties] * road_z[ties]
                win_tw[ties] += seg_claim[ties]
                win_claim[ties] = np.maximum(win_claim[ties], seg_claim[ties])

            # Keep carriageway Asphalt strictly inside half_width_m (with sub-cell inset on paved
            # roads so 1 m square terrain pixels never poke outside feathered DecalRoad edges),
            # and paint a Gravel shoulder rim right along the decal edge so tall Grass
            # GroundCover billboards do not poke through white edge markings or narrow streets
            # on 2 m / 4 m terrain grids.
            is_paved_surface = corridor.material_key in {"Asphalt", "Asphalt_worn", "Concrete"}
            is_paved = is_paved_surface and half_width >= 2.0
            paint_inset = min(0.22, 0.22 * cell_size_m) if is_paved else 0.0
            in_cw = dist <= max(0.5, half_width - paint_inset)
            if in_cw.any():
                win_cw[in_cw] = True
                cw_owner = in_cw & (seg_claim >= win_claim - np.float32(1e-5))
                win_mat[cw_owner] = material_idx

            # Limit painted shoulder verge width so 12 m grading skirts never paint
            # 24 m-wide Gravel/Dirt highways along narrow rural tracks, while scaling
            # paved Gravel rim on 2 m / 4 m grids so Grass vertices never sit under asphalt.
            use_gravel_verge = is_paved_surface and corridor.shoulder_material_key == "Grass" and "Gravel" in material_index
            sh_pad = (
                (0.55 if cell_size_m <= 1.5 else max(0.85, 0.55 * cell_size_m))
                if use_gravel_verge
                else (
                    max(2.0, cell_size_m * 0.6)
                    if corridor.shoulder_material_key == "Grass"
                    else max(0.8, cell_size_m * 0.6)
                )
            )
            eff_shoulder_idx = material_index["Gravel"] if use_gravel_verge else shoulder_idx
            in_sh_paint = (dist > max(0.5, half_width - paint_inset)) & (dist <= min(corridor_half, half_width + sh_pad))
            if in_sh_paint.any():
                sh_owner = in_sh_paint & (seg_claim >= win_claim - np.float32(1e-5))
                win_sh[sh_owner] = eff_shoulder_idx
                win_sh_mask[in_sh_paint] = True

            touched += int(inside.sum())

        graded += touched
        per_material[corridor.key or f"road_{id(corridor)}"] = touched

    blended = influence > 0
    if blended.any():
        inf_b = influence[blended].astype(np.float64)
        w_b = tie_w_sum[blended].astype(np.float64)
        surface_z = tie_z_sum[blended].astype(np.float64) / np.where(w_b > 0.0, w_b, 1.0)
        elevations[blended] = elevations[blended] * (1.0 - inf_b) + surface_z * inf_b

        # 2 m seam blur pass over junction transitions within 4.0 m height delta
        if seam_mask.any():
            blur_r = max(1, round(2.0 / cell_size_m))
            blur_band = _morph_mask(seam_mask, blur_r, erode=False) & blended
            if blur_band.any():
                br, bc = np.where(blur_band)
                z_base = elevations[br, bc]
                acc = z_base.copy()
                cnt = np.ones_like(z_base)
                for dr in range(-blur_r, blur_r + 1):
                    for dc in range(-blur_r, blur_r + 1):
                        if dr == 0 and dc == 0:
                            continue
                        nr = np.clip(br + dr, 0, n - 1)
                        nc = np.clip(bc + dc, 0, n - 1)
                        nz = elevations[nr, nc]
                        ok = blended[nr, nc] & (np.abs(nz - z_base) <= 4.0)
                        acc = np.where(ok, acc + nz, acc)
                        cnt = np.where(ok, cnt + 1.0, cnt)
                elevations[br, bc] = acc / cnt

        # Paint carriageway strictly inside half_width_m and shoulders inside shoulder verge
        carriageway = carriageway_mask & (influence >= np.float32(1.0 - 1e-6))
        shoulder = shoulder_mask & blended & ~carriageway
        layers[carriageway] = owner_material[carriageway]
        layers[shoulder] = owner_shoulder[shoulder]

        # Relax artificial steep steps in road shoulders/skirts (never altering flat carriageways)
        # when nearby road corridors meet at different heights on steep hillsides.
        if cell_size_m <= 4.0:
            skirt = blended & ~carriageway_mask
            if skirt.any():
                max_step = 0.45 * cell_size_m
                sr, sc = np.where(skirt)
                for _ in range(6):
                    cur = elevations[sr, sc]
                    nbr_sum = np.zeros_like(cur)
                    nbr_cnt = np.zeros_like(cur)
                    max_diff = np.zeros_like(cur)
                    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        nr = np.clip(sr + dr, 0, n - 1)
                        nc = np.clip(sc + dc, 0, n - 1)
                        nz = elevations[nr, nc]
                        nbr_sum += nz
                        nbr_cnt += 1.0
                        max_diff = np.maximum(max_diff, np.abs(nz - cur))
                    steep_idx = max_diff > max_step
                    if not steep_idx.any():
                        break
                    elevations[sr[steep_idx], sc[steep_idx]] = (
                        0.35 * cur[steep_idx] + 0.65 * (nbr_sum[steep_idx] / nbr_cnt[steep_idx])
                    )

    stats = {
        "corridors": len([c for c in corridors if c.grade_terrain and c.surface_z is not None]),
        "graded_cells": graded,
        "cells_touched": int(blended.sum()),
        "per_corridor": per_material,
    }
    return graded, stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _fill_dem_gaps(
    elevations: NDArray[np.float64],
    invalid: NDArray[np.bool_],
    *,
    max_passes: int = 16,
) -> tuple[int, float]:
    """Fill non-finite DEM cells from their valid neighbours.

    A nearest-neighbour search over every gap cell is quadratic and, on a
    4096 x 4096 grid where the DEM only partially covers the area, asks for
    terabytes. Instead the gaps are grown inwards: each pass replaces every
    unfilled cell that has a filled neighbour with the mean of those neighbours.
    That is linear in the number of cells per pass, and a boundary sliver of
    missing data closes in a single pass.

    When a hole is too large to close within ``max_passes``, whatever remains is
    set to the mean of the valid data and the count is returned so the caller can
    warn.

    Args:
        elevations: ``(n, n)`` elevation grid, modified in place.
        invalid: ``(n, n)`` boolean mask of cells to fill.
        max_passes: Maximum dilation passes.

    Returns:
        ``(cells_filled, remaining_after_passes)``.
    """
    if not invalid.any():
        return (0, 0.0)

    filled = elevations.copy()
    remaining = invalid.copy()
    total_filled = 0

    for _ in range(max_passes):
        if not remaining.any():
            break
        valid = ~remaining
        # Pad by one so every real cell has four in-grid neighbours.
        value = np.pad(filled, 1, mode="edge")
        known = np.pad(valid, 1, mode="constant", constant_values=False)

        acc = np.zeros_like(filled)
        count = np.zeros(filled.shape, dtype=np.int32)
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            shifted_value = value[1 + dy : 1 + dy + filled.shape[0], 1 + dx : 1 + dx + filled.shape[1]]
            shifted_known = known[1 + dy : 1 + dy + filled.shape[0], 1 + dx : 1 + dx + filled.shape[1]]
            acc += np.where(shifted_known, shifted_value, 0.0)
            count += shifted_known

        newly = remaining & (count > 0)
        if not newly.any():
            break
        filled[newly] = acc[newly] / count[newly]
        remaining &= ~newly
        total_filled += int(newly.sum())

    leftover = int(remaining.sum())
    if leftover:
        valid_values = elevations[~invalid]
        fallback = float(np.mean(valid_values)) if valid_values.size else 0.0
        filled[remaining] = fallback

    elevations[invalid] = filled[invalid]
    return (total_filled, float(leftover))


# ---------------------------------------------------------------------------
# Hydrology carving and landcover painting
# ---------------------------------------------------------------------------


def _morph_mask(mask: NDArray[np.bool_], radius: int, *, erode: bool) -> NDArray[np.bool_]:
    """Separably erode or dilate a 2D boolean mask by ``radius`` cells."""
    if radius <= 0 or mask.shape[0] == 0 or mask.shape[1] == 0:
        return mask.copy()
    width = 2 * radius + 1
    pad_x = np.pad(mask, ((0, 0), (radius, radius)), mode="constant", constant_values=False)
    view_x = sliding_window_view(pad_x, width, axis=1)
    row_pass = view_x.all(axis=-1) if erode else view_x.any(axis=-1)
    pad_y = np.pad(row_pass, ((radius, radius), (0, 0)), mode="constant", constant_values=False)
    view_y = sliding_window_view(pad_y, width, axis=0)
    return view_y.all(axis=-1) if erode else view_y.any(axis=-1)


def _normalize_polygonal(geom: Any) -> Any | None:
    """Extract polygonal parts from a shapely geometry after validation/clipping."""
    if geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
        if not polys:
            return None
        return shapely.unary_union(polys)
    return None


def build_road_keep_mask(
    corridors: Sequence[RoadCorridor],
    *,
    origin_x: float,
    origin_y: float,
    grid_size: int,
    cell_size_m: float,
    road_clearance_m: float = 8.0,
) -> NDArray[np.bool_]:
    """Rasterize a keep-out mask protecting ground roads and bridgehead ramps.

    Ground roads stay protected within ``half_width_m + road_clearance_m`` (8 m),
    while bridgehead ramps (``ramp_*``) protect their carriageway + 1.5 m shoulder
    so riverbank carving does not wash out bridge abutments.
    """
    keep = np.zeros((grid_size, grid_size), dtype=bool)
    all_x = origin_x + np.arange(grid_size, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(grid_size, dtype=np.float64) * cell_size_m

    for corridor in corridors:
        if not corridor.grade_terrain:
            continue
        is_ramp = corridor.key.startswith("ramp_")
        pad = float(corridor.half_width_m) + (1.5 if is_ramp else float(road_clearance_m))
        pts = corridor.points
        for i in range(pts.shape[0] - 1):
            x0, y0 = float(pts[i, 0]), float(pts[i, 1])
            x1, y1 = float(pts[i + 1, 0]), float(pts[i + 1, 1])
            dx, dy = x1 - x0, y1 - y0
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 0.0:
                continue
            col_lo = max(0, math.floor((min(x0, x1) - pad - origin_x) / cell_size_m))
            col_hi = min(grid_size - 1, math.ceil((max(x0, x1) + pad - origin_x) / cell_size_m))
            row_lo = max(0, math.floor((min(y0, y1) - pad - origin_y) / cell_size_m))
            row_hi = min(grid_size - 1, math.ceil((max(y0, y1) + pad - origin_y) / cell_size_m))
            if col_hi < col_lo or row_hi < row_lo:
                continue
            gx = all_x[col_lo : col_hi + 1][None, :]
            gy = all_y[row_lo : row_hi + 1][:, None]
            t = np.clip(((gx - x0) * dx + (gy - y0) * dy) / seg_len_sq, 0.0, 1.0)
            dist = np.hypot(gx - (x0 + t * dx), gy - (y0 + t * dy))
            keep[row_lo : row_hi + 1, col_lo : col_hi + 1] |= dist <= pad
    return keep


def _box_blur_float(arr: NDArray[np.float64], radius: int, *, passes: int = 2) -> NDArray[np.float64]:
    """Fast separable box blur using cumulative sums (edge-replicated)."""
    if radius <= 0 or arr.shape[0] == 0 or arr.shape[1] == 0:
        return arr.astype(np.float64, copy=True)
    out = arr.astype(np.float64, copy=True)
    w = 2 * radius + 1
    for _ in range(max(1, passes)):
        # Horizontal pass
        p_h = np.pad(out, ((0, 0), (radius, radius)), mode="edge")
        cs_h = np.empty((out.shape[0], p_h.shape[1] + 1), dtype=np.float64)
        cs_h[:, 0] = 0.0
        np.cumsum(p_h, axis=1, out=cs_h[:, 1:])
        out = (cs_h[:, w:] - cs_h[:, :-w]) / float(w)
        # Vertical pass
        p_v = np.pad(out, ((radius, radius), (0, 0)), mode="edge")
        cs_v = np.empty((p_v.shape[0] + 1, out.shape[1]), dtype=np.float64)
        cs_v[0, :] = 0.0
        np.cumsum(p_v, axis=0, out=cs_v[1:, :])
        out = (cs_v[w:, :] - cs_v[:-w, :]) / float(w)
    return out


def _deterministic_hash_noise(
    rows: NDArray[np.intp] | int,
    cols: NDArray[np.intp] | int,
    *,
    seed: int = 1337,
) -> NDArray[np.float64]:
    """Return deterministic pseudo-random values in ``[0, 1)`` for integer grid coords."""
    r = np.asarray(rows, dtype=np.uint64)
    c = np.asarray(cols, dtype=np.uint64)
    h = r * np.uint64(374761393) + c * np.uint64(668265263) + np.uint64(seed * 1442695041)
    h = (h ^ (h >> np.uint64(13))) * np.uint64(1274126177)
    h = h ^ (h >> np.uint64(16))
    return (h & np.uint64(0xFFFFFF)).astype(np.float64) / float(0x1000000)


def _fbm_noise_grid(
    n: int,
    cell_size_m: float,
    origin_x: float,
    origin_y: float,
    *,
    seed: int = 42,
    octaves_m: tuple[tuple[float, float], ...] = ((96.0, 0.45), (32.0, 0.30), (10.0, 0.17), (3.5, 0.08)),
) -> NDArray[np.float64]:
    """Generate world-anchored multi-scale 2D smooth value noise in ``[0, 1]``."""
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m
    total = np.zeros((n, n), dtype=np.float64)
    w_sum = 0.0

    for idx, (scale_m, weight) in enumerate(octaves_m):
        fx = all_x / scale_m
        fy = all_y / scale_m
        ix0 = np.floor(fx).astype(np.int64)
        iy0 = np.floor(fy).astype(np.int64)
        tx = fx - ix0
        ty = fy - iy0
        sx = tx * tx * (3.0 - 2.0 * tx)
        sy = ty * ty * (3.0 - 2.0 * ty)

        x0 = ix0[None, :]
        x1 = x0 + 1
        y0 = iy0[:, None]
        y1 = y0 + 1
        s = seed + idx * 97
        v00 = _deterministic_hash_noise(y0, x0, seed=s)
        v01 = _deterministic_hash_noise(y0, x1, seed=s)
        v10 = _deterministic_hash_noise(y1, x0, seed=s)
        v11 = _deterministic_hash_noise(y1, x1, seed=s)
        oct_val = (
            v00 * (1.0 - sx[None, :]) * (1.0 - sy[:, None])
            + v01 * sx[None, :] * (1.0 - sy[:, None])
            + v10 * (1.0 - sx[None, :]) * sy[:, None]
            + v11 * sx[None, :] * sy[:, None]
        )
        total += weight * oct_val
        w_sum += weight

    return np.clip(total / max(1e-9, w_sum), 0.0, 1.0)


def _catmull_rom_subdivide(
    pts: NDArray[np.float64],
    zw: NDArray[np.float64],
    *,
    max_step_m: float = 6.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Subdivide a river polyline along a smooth Catmull-Rom spline matching BeamNG ``River``."""
    n_pts = pts.shape[0]
    if n_pts < 3 or max_step_m <= 0.0:
        return pts, zw

    out_pts: list[NDArray[np.float64]] = []
    out_z: list[float] = []
    for i in range(n_pts - 1):
        p0 = pts[max(0, i - 1)]
        p1 = pts[i]
        p2 = pts[i + 1]
        p3 = pts[min(n_pts - 1, i + 2)]
        z1 = float(zw[i])
        z2 = float(zw[i + 1])
        seg_len = float(math.hypot(float(p2[0] - p1[0]), float(p2[1] - p1[1])))
        steps = max(1, math.ceil(seg_len / max_step_m))
        for k in range(steps):
            t = k / steps
            t2 = t * t
            t3 = t2 * t
            pt = 0.5 * (
                (2.0 * p1)
                + (-p0 + p2) * t
                + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
            )
            out_pts.append(pt)
            out_z.append(z1 + t * (z2 - z1))
    out_pts.append(pts[-1])
    out_z.append(float(zw[-1]))
    return np.asarray(out_pts, dtype=np.float64), np.asarray(out_z, dtype=np.float64)


def _reanchor_water_to_terrain(
    elevations: NDArray[np.float64],
    water_network: Any,
    *,
    origin_x: float,
    origin_y: float,
    cell_size_m: float,
    road_keep_mask: NDArray[np.bool_] | None = None,
) -> int:
    """Lower raised river/pond water levels after forest canopy stripping.

    Because ``extract_water`` runs against raw Copernicus DEM (which includes +10..15 m
    tree canopy height in forested corridors), waterways inside forests can sit metres
    above the canopy-stripped ground. Clamping raised nodes against the post-canopy
    terrain keeps ``carve_water`` and BeamNG scene objects aligned with the ground.
    """
    n = elevations.shape[0]
    adjusted = 0

    def _sample(wx: float, wy: float) -> float | None:
        c = round((wx - origin_x) / cell_size_m)
        r = round((wy - origin_y) / cell_size_m)
        if 0 <= r < n and 0 <= c < n:
            if road_keep_mask is not None and road_keep_mask[r, c]:
                return None
            return float(elevations[r, c])
        return None

    for river in getattr(water_network, "rivers", ()):
        pts = river.points
        zw = river.z_water
        if pts.shape[0] < 2:
            continue
        hw = max(3.0, float(river.width_m) * 0.5 + 2.0)
        deltas = np.diff(pts, axis=0)
        lens = np.hypot(deltas[:, 0], deltas[:, 1])
        safe = np.where(lens < 1e-6, 1.0, lens)
        seg_t = deltas / safe[:, None]
        tang = np.zeros_like(pts)
        tang[0] = seg_t[0]
        tang[-1] = seg_t[-1]
        if pts.shape[0] > 2:
            mid = seg_t[:-1] + seg_t[1:]
            ml = np.hypot(mid[:, 0], mid[:, 1])
            tang[1:-1] = np.where(ml[:, None] > 1e-6, mid / np.maximum(ml[:, None], 1e-9), seg_t[:-1])

        ground_caps = np.full(pts.shape[0], np.inf, dtype=np.float64)
        for i in range(pts.shape[0]):
            px, py = float(pts[i, 0]), float(pts[i, 1])
            nx, ny = -float(tang[i, 1]), float(tang[i, 0])
            samples: list[float] = []
            for frac in (-1.0, -0.5, 0.0, 0.5, 1.0):
                val = _sample(px + nx * (frac * hw), py + ny * (frac * hw))
                if val is not None:
                    samples.append(val)
            if samples:
                ground_caps[i] = min(samples)

        changed = False
        for i in range(pts.shape[0]):
            if np.isfinite(ground_caps[i]) and zw[i] > ground_caps[i]:
                zw[i] = ground_caps[i]
                changed = True
                adjusted += 1
        if changed:
            for i in range(1, zw.size):
                zw[i] = min(zw[i - 1], zw[i])

    for body in getattr(water_network, "water_bodies", ()):
        outer = body.outer_ring
        if len(outer) < 3:
            continue
        min_x, min_y, max_x, max_y = body.bounds
        col_lo = max(0, math.floor((min_x - origin_x) / cell_size_m))
        col_hi = min(n - 1, math.ceil((max_x - origin_x) / cell_size_m))
        row_lo = max(0, math.floor((min_y - origin_y) / cell_size_m))
        row_hi = min(n - 1, math.ceil((max_y - origin_y) / cell_size_m))
        if col_hi < col_lo or row_hi < row_lo:
            continue
        poly = Polygon(outer, holes=[h for h in body.inner_rings if len(h) >= 4])
        if not poly.is_valid:
            poly = _normalize_polygonal(make_valid(poly))
            if poly is None:
                continue
        step = max(1, round(max(col_hi - col_lo, row_hi - row_lo) / 16))
        rs = np.arange(row_lo, row_hi + 1, step)
        cs = np.arange(col_lo, col_hi + 1, step)
        gx, gy = np.meshgrid(origin_x + cs * cell_size_m, origin_y + rs * cell_size_m)
        inside = shapely.contains_xy(poly, gx.ravel(), gy.ravel()).reshape(rs.size, cs.size)
        if inside.any():
            vals = elevations[np.ix_(rs, cs)][inside]
            if vals.size:
                target_z = float(np.percentile(vals, 25))
                if body.z_water > target_z + 0.75:
                    body.z_water = target_z
                    adjusted += 1

    return adjusted


def carve_water(
    elevations: NDArray[np.float64],
    layers: NDArray[np.uint8],
    material_index: dict[str, int],
    water_network: Any,
    *,
    origin_x: float,
    origin_y: float,
    cell_size_m: float,
    bank_transition_m: float = 8.0,
    road_keep_mask: NDArray[np.bool_] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Carve water channels, rivers, lakes, and basins into the terrain.

    Couples the river channel bed strictly inside the ``River`` ribbon width so
    water sheets never float above dry troughs, slopes banks gently outward to
    prevent vertical mud cliffs, anti-aliases shorelines, and paints subtle
    noise-dithered wet-bank materials. Preserves island rings inside water bodies.

    Args:
        elevations: (n, n) elevation grid, modified in place.
        layers: (n, n) material index grid, modified in place.
        material_index: Mapping of material keys to uint8 indices.
        water_network: WaterNetwork containing rivers and water_bodies.
        origin_x: World X of minimum corner.
        origin_y: World Y of minimum corner.
        cell_size_m: Ground spacing in metres.
        bank_transition_m: Bank slope transition width in metres (6-12m).

    Returns:
        (touched_cells_count, stats_dict).
    """
    n = elevations.shape[0]
    idx_pebbles = material_index.get("Pebbles")
    idx_sand = material_index.get("Sand")
    idx_riverbank_soil = material_index.get("RiverbankSoil")

    bw = max(6.0, min(12.0, float(bank_transition_m)))
    max_outer_reach = max(bw, 22.0)
    touched_mask = np.zeros((n, n), dtype=bool)
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m
    terrain_max_x = origin_x + (n - 1) * cell_size_m
    terrain_max_y = origin_y + (n - 1) * cell_size_m
    clip_pad = max_outer_reach + cell_size_m
    clip_min_x = origin_x - clip_pad
    clip_min_y = origin_y - clip_pad
    clip_max_x = terrain_max_x + clip_pad
    clip_max_y = terrain_max_y + clip_pad
    shore_pad_cells = math.ceil(bw / cell_size_m) + 1
    outer_pad_cells = math.ceil(max_outer_reach / cell_size_m) + 1

    # Smooth road protection weight (1.0 on keep mask, tapering smoothly to 0 over ~5 m)
    # so road embankments next to water never form 5 m vertical cliffs.
    road_protect: NDArray[np.float64] | None = None
    if road_keep_mask is not None and road_keep_mask.any():
        keep_f = road_keep_mask.astype(np.float64)
        blur_r = round(4.0 / cell_size_m) if cell_size_m <= 4.0 else 0
        if blur_r > 0:
            dil_f = _morph_mask(road_keep_mask, 1, erode=False).astype(np.float64)
            road_protect = np.maximum(keep_f, _box_blur_float(dil_f, blur_r, passes=3))
        else:
            road_protect = keep_f

    # 1. Carve linear river channels along Catmull-Rom curve matching BeamNG River
    rivers = getattr(water_network, "rivers", ())
    for river in rivers:
        raw_pts = river.points
        raw_zw = river.z_water
        if raw_pts.shape[0] < 2:
            continue
        pts, zw = _catmull_rom_subdivide(raw_pts, raw_zw, max_step_m=max(4.0, cell_size_m * 4.0))
        depth_m = float(river.depth_m)
        hw = float(river.width_m) / 2.0
        # Waterline sits slightly inside hw so the BeamNG River ribbon overlaps rising banks cleanly
        shore_hw = max(0.8, hw - 0.8) if hw >= 3.0 else hw * 0.85
        total_reach = shore_hw + max_outer_reach

        r_min_x, r_min_y = float(pts[:, 0].min()) - total_reach, float(pts[:, 1].min()) - total_reach
        r_max_x, r_max_y = float(pts[:, 0].max()) + total_reach, float(pts[:, 1].max()) + total_reach
        r_col_lo = max(0, math.floor((r_min_x - origin_x) / cell_size_m))
        r_col_hi = min(n - 1, math.ceil((r_max_x - origin_x) / cell_size_m))
        r_row_lo = max(0, math.floor((r_min_y - origin_y) / cell_size_m))
        r_row_hi = min(n - 1, math.ceil((r_max_y - origin_y) / cell_size_m))
        if r_col_hi < r_col_lo or r_row_hi < r_row_lo:
            continue

        r_ny = r_row_hi - r_row_lo + 1
        r_nx = r_col_hi - r_col_lo + 1
        best_dist = np.full((r_ny, r_nx), np.inf, dtype=np.float64)
        best_zw = np.zeros((r_ny, r_nx), dtype=np.float64)

        for i in range(pts.shape[0] - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            z0, z1 = zw[i], zw[i + 1]
            dx, dy = x1 - x0, y1 - y0
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 0:
                continue

            col_lo = max(r_col_lo, math.floor((min(x0, x1) - total_reach - origin_x) / cell_size_m))
            col_hi = min(r_col_hi, math.ceil((max(x0, x1) + total_reach - origin_x) / cell_size_m))
            row_lo = max(r_row_lo, math.floor((min(y0, y1) - total_reach - origin_y) / cell_size_m))
            row_hi = min(r_row_hi, math.ceil((max(y0, y1) + total_reach - origin_y) / cell_size_m))
            if col_hi < col_lo or row_hi < row_lo:
                continue

            grid_x = all_x[col_lo : col_hi + 1][None, :]
            grid_y = all_y[row_lo : row_hi + 1][:, None]

            rel_x = grid_x - x0
            rel_y = grid_y - y0
            t = np.clip((rel_x * dx + rel_y * dy) / seg_len_sq, 0.0, 1.0)
            dist = np.hypot(grid_x - (x0 + t * dx), grid_y - (y0 + t * dy))
            water_z = z0 + t * (z1 - z0)

            sy0, sy1 = row_lo - r_row_lo, row_hi - r_row_lo + 1
            sx0, sx1 = col_lo - r_col_lo, col_hi - r_col_lo + 1
            sub_best = best_dist[sy0:sy1, sx0:sx1]
            better = (dist <= total_reach) & (dist < sub_best)
            if better.any():
                sub_best[better] = dist[better]
                best_zw[sy0:sy1, sx0:sx1][better] = water_z[better]

        inside_channel = best_dist <= shore_hw
        in_bank = (best_dist > shore_hw) & (best_dist <= total_reach)
        if not (inside_channel.any() or in_bank.any()):
            continue

        sub_keep = (
            road_keep_mask[r_row_lo : r_row_hi + 1, r_col_lo : r_col_hi + 1]
            if road_keep_mask is not None
            else None
        )
        sub_prot = (
            road_protect[r_row_lo : r_row_hi + 1, r_col_lo : r_col_hi + 1]
            if road_protect is not None
            else None
        )
        chan_mask = inside_channel & (~sub_keep) if sub_keep is not None else inside_channel
        bank_mask = in_bank & (~sub_keep) if sub_keep is not None else in_bank
        if not (chan_mask.any() or bank_mask.any()):
            continue

        sub_elev = elevations[r_row_lo : r_row_hi + 1, r_col_lo : r_col_hi + 1]
        sub_layers = layers[r_row_lo : r_row_hi + 1, r_col_lo : r_col_hi + 1]
        sub_touched = touched_mask[r_row_lo : r_row_hi + 1, r_col_lo : r_col_hi + 1]

        # Deterministic spatial noise for organic wet-bank dithering
        r_idx = np.arange(r_row_lo, r_row_hi + 1, dtype=np.intp)[:, None]
        c_idx = np.arange(r_col_lo, r_col_hi + 1, dtype=np.intp)[None, :]
        bank_noise = _deterministic_hash_noise(r_idx, c_idx, seed=701)

        if chan_mask.any():
            u = best_dist[chan_mask] / shore_hw
            # Smooth U-shaped channel rising from (z_water - depth) at centerline to (z_water + 0.05) at shore_hw
            rise = 0.5 * (1.0 - np.cos(math.pi * u))
            target_chan = (best_zw[chan_mask] - depth_m) + (depth_m + 0.05) * rise
            cur_chan = sub_elev[chan_mask]
            new_chan = np.minimum(cur_chan, target_chan)
            if sub_prot is not None:
                p_c = sub_prot[chan_mask]
                new_chan = cur_chan * p_c + new_chan * (1.0 - p_c)
            sub_elev[chan_mask] = new_chan
            sub_touched[chan_mask] = True

            # Submerged bed -> Pebbles; outer wet rim (<= 0.8m inside shore) -> subtle Sand/RiverbankSoil
            rim_w = min(shore_hw * 0.3, max(0.7, cell_size_m * 0.6))
            deep_bed = best_dist[chan_mask] <= max(0.3, shore_hw - rim_w)
            if idx_pebbles is not None and idx_sand is not None:
                rim_mat = np.where(bank_noise[chan_mask] < 0.65, idx_sand, idx_pebbles)
                sub_layers[chan_mask] = np.where(deep_bed, idx_pebbles, rim_mat)
            elif idx_pebbles is not None:
                sub_layers[chan_mask] = np.where(deep_bed, idx_pebbles, sub_layers[chan_mask])

        if bank_mask.any():
            dist_from_shore = best_dist[bank_mask] - shore_hw
            cur_bank = sub_elev[bank_mask]
            shore_z = best_zw[bank_mask] + 0.05
            drop = np.maximum(0.0, cur_bank - shore_z)
            # Gentle bank slope <= ~30%: widen transition adaptively for tall bluffs
            eff_bw = np.clip(np.maximum(bw, drop / 0.30), bw, max_outer_reach)
            frac = np.clip(dist_from_shore / eff_bw, 0.0, 1.0)
            smooth = 0.5 * (1.0 - np.cos(math.pi * frac))
            target_bank = shore_z + smooth * (cur_bank - shore_z)
            new_bank = np.minimum(cur_bank, target_bank)
            if sub_prot is not None:
                p_b = sub_prot[bank_mask]
                new_bank = cur_bank * p_b + new_bank * (1.0 - p_b)
            sub_elev[bank_mask] = new_bank
            sub_touched[bank_mask] = True

            # Narrow damp shoreline -> Sand; narrow bank -> dithered RiverbankSoil.
            # Keep mud/soil bands compact on coarse 4 m grids and narrow streams.
            sand_out_w = min(1.2, max(0.75, cell_size_m * 0.45))
            soil_out_w = max(2.8, sand_out_w + max(1.8, cell_size_m * 0.9))
            if hw <= 3.0:
                soil_out_w = min(soil_out_w, max(2.2, cell_size_m * 0.85))
            in_sand = dist_from_shore <= sand_out_w
            in_soil = (dist_from_shore > sand_out_w) & (dist_from_shore <= soil_out_w)
            soil_prob = np.clip(1.0 - (dist_from_shore - sand_out_w) / max(0.5, soil_out_w - sand_out_w), 0.0, 1.0)
            paint_soil = in_soil & (bank_noise[bank_mask] < (0.35 + 0.65 * soil_prob))
            cur_l = sub_layers[bank_mask]
            if idx_sand is not None:
                cur_l = np.where(in_sand, idx_sand, cur_l)
            if idx_riverbank_soil is not None:
                cur_l = np.where(paint_soil, idx_riverbank_soil, cur_l)
            sub_layers[bank_mask] = cur_l

    # 2. Carve standing water bodies (lakes, reservoirs, ponds)
    bodies = getattr(water_network, "water_bodies", ())
    for body in bodies:
        outer = body.outer_ring
        if len(outer) < 4:
            continue
        inners = [h for h in body.inner_rings if len(h) >= 4]
        z_water = float(body.z_water)
        depth_m = float(body.depth_m)

        poly = Polygon(outer, holes=inners)
        if not poly.is_valid:
            poly = _normalize_polygonal(make_valid(poly))
            if poly is None:
                continue

        # Clip to terrain bounds (+ bank transition) so off-map water bodies
        # do not evaluate millions of interior/exterior cells.
        poly = _normalize_polygonal(shapely.clip_by_rect(poly, clip_min_x, clip_min_y, clip_max_x, clip_max_y))
        if poly is None:
            continue

        min_x, min_y, max_x, max_y = poly.bounds
        col_lo = max(0, math.floor((min_x - max_outer_reach - origin_x) / cell_size_m))
        col_hi = min(n - 1, math.ceil((max_x + max_outer_reach - origin_x) / cell_size_m))
        row_lo = max(0, math.floor((min_y - max_outer_reach - origin_y) / cell_size_m))
        row_hi = min(n - 1, math.ceil((max_y + max_outer_reach - origin_y) / cell_size_m))
        if col_hi < col_lo or row_hi < row_lo:
            continue

        ny = row_hi - row_lo + 1
        nx = col_hi - col_lo + 1
        grid_x = np.broadcast_to(all_x[col_lo : col_hi + 1][None, :], (ny, nx))
        grid_y = np.broadcast_to(all_y[row_lo : row_hi + 1][:, None], (ny, nx))

        # Vectorized containment: True for lake basin, False for islands (holes)
        inside_raw = shapely.contains_xy(poly, grid_x.ravel(), grid_y.ravel()).reshape(ny, nx)
        sub_keep = (
            road_keep_mask[row_lo : row_hi + 1, col_lo : col_hi + 1]
            if road_keep_mask is not None
            else None
        )
        sub_prot = (
            road_protect[row_lo : row_hi + 1, col_lo : col_hi + 1]
            if road_protect is not None
            else None
        )
        inside = inside_raw & (~sub_keep) if sub_keep is not None else inside_raw
        if not inside.any():
            continue

        sub_elev = elevations[row_lo : row_hi + 1, col_lo : col_hi + 1]
        sub_layers = layers[row_lo : row_hi + 1, col_lo : col_hi + 1]
        sub_touched = touched_mask[row_lo : row_hi + 1, col_lo : col_hi + 1]

        r_idx = np.arange(row_lo, row_hi + 1, dtype=np.intp)[:, None]
        c_idx = np.arange(col_lo, col_hi + 1, dtype=np.intp)[None, :]
        pond_noise = _deterministic_hash_noise(r_idx, c_idx, seed=709)

        # Erode/dilate so shapely.distance runs ONLY on the shoreline bands.
        deep_interior = _morph_mask(inside, shore_pad_cells, erode=True)
        if deep_interior.any():
            cur_d = sub_elev[deep_interior]
            new_d = np.minimum(cur_d, z_water - depth_m)
            if sub_prot is not None:
                p_d = sub_prot[deep_interior]
                new_d = cur_d * p_d + new_d * (1.0 - p_d)
            sub_elev[deep_interior] = new_d
            if idx_pebbles is not None:
                sub_layers[deep_interior] = idx_pebbles
            sub_touched[deep_interior] = True

        boundary = shapely.boundary(poly)
        shore_band = inside & ~deep_interior
        if shore_band.any():
            pts_shore = shapely.points(grid_x[shore_band], grid_y[shore_band])
            dist_to_shore = shapely.distance(boundary, pts_shore)
            band_rows, band_cols = np.where(shore_band)

            deep_mask = dist_to_shore >= bw
            if deep_mask.any():
                dr = band_rows[deep_mask]
                dc = band_cols[deep_mask]
                cur_dm = sub_elev[dr, dc]
                new_dm = np.minimum(cur_dm, z_water - depth_m)
                if sub_prot is not None:
                    p_dm = sub_prot[dr, dc]
                    new_dm = cur_dm * p_dm + new_dm * (1.0 - p_dm)
                sub_elev[dr, dc] = new_dm
                if idx_pebbles is not None:
                    sub_layers[dr, dc] = idx_pebbles
                sub_touched[dr, dc] = True

            shallow_mask = ~deep_mask
            if shallow_mask.any():
                sr = band_rows[shallow_mask]
                sc = band_cols[shallow_mask]
                t = dist_to_shore[shallow_mask] / bw
                smooth = t * t * (3.0 - 2.0 * t)
                # Depress at least 0.15m at shoreline so WaterBlock wave ripples never expose dry teeth
                shore_lip = min(0.15, depth_m * 0.25)
                bed_z = z_water - shore_lip - max(0.0, depth_m - shore_lip) * smooth
                cur_sm = sub_elev[sr, sc]
                new_sm = np.minimum(cur_sm, bed_z)
                if sub_prot is not None:
                    p_sm = sub_prot[sr, sc]
                    new_sm = cur_sm * p_sm + new_sm * (1.0 - p_sm)
                sub_elev[sr, sc] = new_sm
                sub_touched[sr, sc] = True

                pebbles_sub = dist_to_shore[shallow_mask] >= 1.2
                sand_sub = ~pebbles_sub
                if idx_pebbles is not None:
                    sub_layers[sr[pebbles_sub], sc[pebbles_sub]] = idx_pebbles
                if idx_sand is not None:
                    sub_layers[sr[sand_sub], sc[sand_sub]] = idx_sand

        # Smoothly slope outside shoreline up to surrounding terrain (eliminates vertical mud cliffs!)
        # and paint a subtle narrow dithered riverbank rim.
        outside_band = (~inside_raw) & _morph_mask(inside_raw, outer_pad_cells, erode=False)
        if sub_keep is not None:
            outside_band &= ~sub_keep
        if outside_band.any():
            pts_outside = shapely.points(grid_x[outside_band], grid_y[outside_band])
            dist_out = shapely.distance(boundary, pts_outside)
            out_rows, out_cols = np.where(outside_band)
            cur_out = sub_elev[out_rows, out_cols]
            shore_lip = min(0.15, depth_m * 0.25)
            shore_z = z_water - shore_lip
            drop = np.maximum(0.0, cur_out - shore_z)
            eff_bw = np.clip(np.maximum(bw, drop / 0.30), bw, max_outer_reach)
            slope_hit = dist_out <= eff_bw
            if slope_hit.any():
                hr = out_rows[slope_hit]
                hc = out_cols[slope_hit]
                frac = np.clip(dist_out[slope_hit] / eff_bw[slope_hit], 0.0, 1.0)
                smooth = 0.5 * (1.0 - np.cos(math.pi * frac))
                target_out = shore_z + smooth * (cur_out[slope_hit] - shore_z)
                new_out = np.minimum(cur_out[slope_hit], target_out)
                if sub_prot is not None:
                    p_o = sub_prot[hr, hc]
                    new_out = cur_out[slope_hit] * p_o + new_out * (1.0 - p_o)
                sub_elev[hr, hc] = new_out
                sub_touched[hr, hc] = True

            # Narrow wet rim: <=0.5m Sand, 0.5..2.4m dithered RiverbankSoil
            rim_sand = dist_out <= 0.5
            rim_soil = (dist_out > 0.5) & (dist_out <= 2.4)
            if idx_sand is not None and rim_sand.any():
                sub_layers[out_rows[rim_sand], out_cols[rim_sand]] = idx_sand
            if idx_riverbank_soil is not None and rim_soil.any():
                s_prob = np.clip(1.0 - (dist_out[rim_soil] - 0.5) / 1.9, 0.0, 1.0)
                keep_soil = pond_noise[out_rows[rim_soil], out_cols[rim_soil]] < (0.3 + 0.7 * s_prob)
                sr_o = out_rows[rim_soil][keep_soil]
                sc_o = out_cols[rim_soil][keep_soil]
                sub_layers[sr_o, sc_o] = idx_riverbank_soil
                sub_touched[sr_o, sc_o] = True

    touched_count = int(touched_mask.sum())
    stats = {
        "carved_cells": touched_count,
        "rivers_carved": len(rivers),
        "water_bodies_carved": len(bodies),
    }
    return touched_count, stats


def _rasterize_bridge_under_mask(
    bridge_spans: Sequence[tuple[NDArray[np.float64], float]],
    *,
    origin_x: float,
    origin_y: float,
    grid_size: int,
    cell_size_m: float,
    end_margin_m: float = 4.0,
) -> NDArray[np.bool_]:
    """Return boolean mask of cells beneath interior bridge spans (excluding abutments)."""
    mask = np.zeros((grid_size, grid_size), dtype=bool)
    all_x = origin_x + np.arange(grid_size, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(grid_size, dtype=np.float64) * cell_size_m

    for pts, half_w in bridge_spans:
        if pts.shape[0] < 2:
            continue
        diffs = np.diff(pts, axis=0)
        seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])
        cum = np.concatenate(([0.0], np.cumsum(seg_lens)))
        total_len = float(cum[-1])
        if total_len <= 2.0 * end_margin_m:
            continue
        pad = float(half_w) + 1.5
        for i in range(pts.shape[0] - 1):
            x0, y0 = float(pts[i, 0]), float(pts[i, 1])
            x1, y1 = float(pts[i + 1, 0]), float(pts[i + 1, 1])
            dx, dy = x1 - x0, y1 - y0
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 0.0:
                continue
            col_lo = max(0, math.floor((min(x0, x1) - pad - origin_x) / cell_size_m))
            col_hi = min(grid_size - 1, math.ceil((max(x0, x1) + pad - origin_x) / cell_size_m))
            row_lo = max(0, math.floor((min(y0, y1) - pad - origin_y) / cell_size_m))
            row_hi = min(grid_size - 1, math.ceil((max(y0, y1) + pad - origin_y) / cell_size_m))
            if col_hi < col_lo or row_hi < row_lo:
                continue
            gx = all_x[col_lo : col_hi + 1][None, :]
            gy = all_y[row_lo : row_hi + 1][:, None]
            t = np.clip(((gx - x0) * dx + (gy - y0) * dy) / seg_len_sq, 0.0, 1.0)
            dist = np.hypot(gx - (x0 + t * dx), gy - (y0 + t * dy))
            arc_s = float(cum[i]) + t * float(seg_lens[i])
            inside = (dist <= pad) & (arc_s >= end_margin_m) & (arc_s <= total_len - end_margin_m)
            mask[row_lo : row_hi + 1, col_lo : col_hi + 1] |= inside
    return mask


def carve_water_under_decks(
    elevations: NDArray[np.float64],
    layers: NDArray[np.uint8],
    material_index: dict[str, int],
    water_network: Any,
    bridge_spans: Sequence[tuple[NDArray[np.float64], float]],
    *,
    origin_x: float,
    origin_y: float,
    cell_size_m: float,
    road_keep_mask: NDArray[np.bool_] | None = None,
    hidden_bed_m: float = 1.5,
    hidden_slope_m: float = 2.0,
) -> int:
    """Clear DEM bridge dams under bridge spans where rivers or lakes pass.

    Adapted from MapNG ``carveWaterUnderDecks`` (MIT License).
    """
    if not bridge_spans or water_network is None:
        return 0
    n = elevations.shape[0]
    under_mask = _rasterize_bridge_under_mask(
        bridge_spans,
        origin_x=origin_x,
        origin_y=origin_y,
        grid_size=n,
        cell_size_m=cell_size_m,
    )
    if road_keep_mask is not None:
        under_mask &= ~road_keep_mask
    if not under_mask.any():
        return 0

    idx_pebbles = material_index.get("Pebbles")
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m
    carved = 0

    for river in getattr(water_network, "rivers", ()):
        pts = river.points
        zw = river.z_water
        hw = float(river.width_m) * 0.5
        for i in range(pts.shape[0] - 1):
            x0, y0 = float(pts[i, 0]), float(pts[i, 1])
            x1, y1 = float(pts[i + 1, 0]), float(pts[i + 1, 1])
            z0, z1 = float(zw[i]), float(zw[i + 1])
            dx, dy = x1 - x0, y1 - y0
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 0.0:
                continue
            col_lo = max(0, math.floor((min(x0, x1) - hw - origin_x) / cell_size_m))
            col_hi = min(n - 1, math.ceil((max(x0, x1) + hw - origin_x) / cell_size_m))
            row_lo = max(0, math.floor((min(y0, y1) - hw - origin_y) / cell_size_m))
            row_hi = min(n - 1, math.ceil((max(y0, y1) + hw - origin_y) / cell_size_m))
            if col_hi < col_lo or row_hi < row_lo:
                continue
            sub_under = under_mask[row_lo : row_hi + 1, col_lo : col_hi + 1]
            if not sub_under.any():
                continue
            gx = all_x[col_lo : col_hi + 1][None, :]
            gy = all_y[row_lo : row_hi + 1][:, None]
            t = np.clip(((gx - x0) * dx + (gy - y0) * dy) / seg_len_sq, 0.0, 1.0)
            dist = np.hypot(gx - (x0 + t * dx), gy - (y0 + t * dy))
            hit = sub_under & (dist <= hw)
            if not hit.any():
                continue
            surface = z0 + t * (z1 - z0)
            target = surface - hidden_bed_m * np.minimum(1.0, (hw - dist) / max(0.5, hidden_slope_m) + 0.2)
            sub_elev = elevations[row_lo : row_hi + 1, col_lo : col_hi + 1]
            lower = hit & (sub_elev > target)
            if lower.any():
                sub_elev[lower] = target[lower]
                if idx_pebbles is not None:
                    layers[row_lo : row_hi + 1, col_lo : col_hi + 1][lower] = idx_pebbles
                carved += int(lower.sum())

    return carved


def level_under_decks(
    elevations: NDArray[np.float64],
    bridge_spans: Sequence[tuple[NDArray[np.float64], float]],
    water_network: Any | None = None,
    *,
    origin_x: float,
    origin_y: float,
    cell_size_m: float,
    road_keep_mask: NDArray[np.bool_] | None = None,
    end_margin_m: float = 6.0,
) -> int:
    """Lower residual DEM ridges under bridge spans between lower banks on both sides.

    Adapted from MapNG ``levelUnderDecks`` (MIT License).
    """
    if not bridge_spans:
        return 0
    n = elevations.shape[0]
    step_d = max(0.7, cell_size_m * 0.7)
    lowered = 0

    def _at(wx: float, wy: float) -> tuple[int, int, float] | None:
        c = round((wx - origin_x) / cell_size_m)
        r = round((wy - origin_y) / cell_size_m)
        if 0 <= r < n and 0 <= c < n:
            return (r, c, float(elevations[r, c]))
        return None

    # Pre-check if water is nearby when water_network is supplied
    rivers = getattr(water_network, "rivers", ()) if water_network is not None else ()

    def _near_water(wx: float, wy: float) -> bool:
        if water_network is None:
            return True
        for rv in rivers:
            rpts = rv.points
            hw = float(rv.width_m) * 0.5 + 8.0
            if (
                wx < float(rpts[:, 0].min()) - hw
                or wx > float(rpts[:, 0].max()) + hw
                or wy < float(rpts[:, 1].min()) - hw
                or wy > float(rpts[:, 1].max()) + hw
            ):
                continue
            for j in range(rpts.shape[0] - 1):
                ax, ay = float(rpts[j, 0]), float(rpts[j, 1])
                bx, by = float(rpts[j + 1, 0]), float(rpts[j + 1, 1])
                dx, dy = bx - ax, by - ay
                l2 = dx * dx + dy * dy
                t = 0.0 if l2 <= 1e-9 else max(0.0, min(1.0, ((wx - ax) * dx + (wy - ay) * dy) / l2))
                if math.hypot(ax + dx * t - wx, ay + dy * t - wy) <= hw:
                    return True
        return False

    for pts, half_w in bridge_spans:
        if pts.shape[0] < 2:
            continue
        diffs = np.diff(pts, axis=0)
        seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])
        cum = np.concatenate(([0.0], np.cumsum(seg_lens)))
        total_len = float(cum[-1])
        h = float(half_w) + 0.4
        for i in range(pts.shape[0] - 1):
            seg_len = float(seg_lens[i])
            if seg_len < 1e-6:
                continue
            ax, ay = float(pts[i, 0]), float(pts[i, 1])
            ux, uy = float(diffs[i, 0]) / seg_len, float(diffs[i, 1]) / seg_len
            nx, ny = -uy, ux
            d = 0.0
            while d < seg_len:
                sd = float(cum[i]) + d
                if end_margin_m <= sd <= total_len - end_margin_m:
                    cx, cy = ax + ux * d, ay + uy * d
                    best_l: tuple[float, float] | None = None
                    best_r: tuple[float, float] | None = None
                    off = h + 1.0
                    while off <= h + 5.0 + 1e-6:
                        ql = _at(cx + nx * off, cy + ny * off)
                        if ql is not None and (best_l is None or ql[2] < best_l[0]):
                            best_l = (ql[2], off)
                        qr = _at(cx - nx * off, cy - ny * off)
                        if qr is not None and (best_r is None or qr[2] < best_r[0]):
                            best_r = (qr[2], off)
                        off += step_d
                    if best_l is not None and best_r is not None:
                        wet_l = _near_water(cx + nx * best_l[1], cy + ny * best_l[1])
                        wet_r = _near_water(cx - nx * best_r[1], cy - ny * best_r[1])
                        if wet_l or wet_r:
                            o = -(h + 2.0)
                            while o <= h + 2.0 + 1e-6:
                                q = _at(cx + nx * o, cy + ny * o)
                                if q is not None and (road_keep_mask is None or not road_keep_mask[q[0], q[1]]):
                                    frac = max(0.0, min(1.0, (o + best_r[1]) / max(1e-6, best_l[1] + best_r[1])))
                                    target = best_r[0] + (best_l[0] - best_r[0]) * frac
                                    if target < elevations[q[0], q[1]] - 0.05:
                                        elevations[q[0], q[1]] = target
                                        lowered += 1
                                o += step_d
                d += step_d
    return lowered


def _to_shapely_polygon(shape: Any) -> Any | None:
    """Convert a Shapely polygon, ``VegetationArea``, ``(outer, holes)`` tuple, or ring to a valid Shapely geometry."""
    if isinstance(shape, Polygon) or getattr(shape, "geom_type", None) == "MultiPolygon":
        poly: Any = shape
    elif hasattr(shape, "to_polygon"):
        poly = shape.to_polygon()
    elif isinstance(shape, tuple) and len(shape) == 2:
        outer, holes = shape
        outer_arr = np.asarray(outer, dtype=np.float64)
        if outer_arr.ndim != 2 or outer_arr.shape[0] < 3:
            return None
        hole_arrs = [np.asarray(h, dtype=np.float64) for h in (holes or ()) if len(h) >= 3]
        poly = Polygon(outer_arr, holes=hole_arrs)
    else:
        pts = np.asarray(shape, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[0] < 3:
            return None
        poly = Polygon(pts)

    if not poly.is_valid:
        poly = make_valid(poly)
        if poly.is_empty:
            return None
    return poly


def strip_forest_canopy(
    elevations: NDArray[np.float64],
    layers: NDArray[np.uint8],
    material_index: dict[str, int],
    forest_polygons: Sequence[Any],
    *,
    origin_x: float,
    origin_y: float,
    cell_size_m: float,
    fallback_canopy_m: float = 12.0,
    canopy_height_m: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """Rasterize forest mask, measure canopy jump across boundary, and subtract canopy height.

    Central Russian forests (birch, pine, spruce) add 10-15m of elevation in DSMs
    such as Copernicus DEM. Stripping this canopy height (clipped to 0-15m) inside
    woods before road grading prevents roads cut through forests from sitting in
    artificial 20m trenches.

    Args:
        elevations: (n, n) elevation grid, modified in place.
        layers: (n, n) material index grid, modified in place.
        material_index: Material key to index dictionary.
        forest_polygons: Sequence of forest polygons (coordinates or shapely Polygons).
        origin_x: World X of minimum corner.
        origin_y: World Y of minimum corner.
        cell_size_m: Ground spacing in metres.
        fallback_canopy_m: Default canopy height if boundary jump cannot be measured.

    Returns:
        (forest_cell_count, stats_dict).
    """
    n = elevations.shape[0]
    forest_mask = np.zeros((n, n), dtype=bool)
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m

    for shape in forest_polygons:
        poly = _to_shapely_polygon(shape)
        if poly is None:
            continue

        min_x, min_y, max_x, max_y = poly.bounds
        col_lo = max(0, math.floor((min_x - origin_x) / cell_size_m))
        col_hi = min(n - 1, math.ceil((max_x - origin_x) / cell_size_m))
        row_lo = max(0, math.floor((min_y - origin_y) / cell_size_m))
        row_hi = min(n - 1, math.ceil((max_y - origin_y) / cell_size_m))
        if col_hi < col_lo or row_hi < row_lo:
            continue

        ny = row_hi - row_lo + 1
        nx = col_hi - col_lo + 1
        grid_x = np.broadcast_to(all_x[col_lo : col_hi + 1][None, :], (ny, nx))
        grid_y = np.broadcast_to(all_y[row_lo : row_hi + 1][:, None], (ny, nx))

        inside = shapely.contains_xy(poly, grid_x.ravel(), grid_y.ravel()).reshape(ny, nx)
        forest_mask[row_lo : row_hi + 1, col_lo : col_hi + 1] |= inside

    if not forest_mask.any():
        return 0, {"forest_cells": 0, "canopy_height_m": 0.0}

    # Morphological boundary detection: 1-cell inside vs 1-cell outside
    pad = np.pad(forest_mask, 1, mode="edge")
    has_outside_neighbor = (
        ~pad[:-2, 1:-1] | ~pad[2:, 1:-1] | ~pad[1:-1, :-2] | ~pad[1:-1, 2:]
    )
    inside_edge = forest_mask & has_outside_neighbor

    has_inside_neighbor = (
        pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2] | pad[1:-1, 2:]
    )
    outside_edge = (~forest_mask) & has_inside_neighbor

    if canopy_height_m is not None:
        jump = float(canopy_height_m)
    elif inside_edge.any() and outside_edge.any():
        z_in = elevations[inside_edge]
        z_out = elevations[outside_edge]
        jump = float(np.mean(z_in) - np.mean(z_out))
    else:
        jump = fallback_canopy_m

    canopy_height = float(np.clip(jump, 0.0, 15.0))
    if canopy_height > 0.0:
        blur_r = round(3.5 / cell_size_m) if cell_size_m <= 4.0 else 0
        if blur_r > 0:
            # Smoothly feather canopy subtraction across the forest boundary (~7-8m)
            # and relax any residual DSM step inside the transition band so neither
            # smooth 30m DEM ramps nor sharp canopy boundaries create cliffs.
            canopy_weight = _box_blur_float(forest_mask.astype(np.float64), blur_r, passes=2)
            elevations -= canopy_height * canopy_weight
            edge_blend = np.clip(4.0 * canopy_weight * (1.0 - canopy_weight), 0.0, 1.0)
            if edge_blend.any():
                smoothed = _box_blur_float(elevations, blur_r, passes=2)
                elevations[:] = elevations * (1.0 - edge_blend) + smoothed * edge_blend
        else:
            elevations[forest_mask] -= canopy_height

    idx_forest_floor = material_index.get("ForestFloor")
    if idx_forest_floor is not None:
        if cell_size_m <= 4.0:
            mat_r = max(2, round(6.5 / cell_size_m))
            forest_weight = _box_blur_float(forest_mask.astype(np.float64), mat_r, passes=2)
            noise = _fbm_noise_grid(
                n,
                cell_size_m,
                origin_x,
                origin_y,
                seed=313,
                octaves_m=((64.0, 0.45), (22.0, 0.35), (max(5.0, 2.2 * cell_size_m), 0.20)),
            )
            # Deep forest cores stay ForestFloor; edges and small sparse tree clumps dither naturally
            paint_ff = forest_mask & ((forest_weight >= 0.84) | (forest_weight > (0.28 + 0.54 * noise)))
            layers[paint_ff] = idx_forest_floor
        else:
            layers[forest_mask] = idx_forest_floor

    forest_cells = int(forest_mask.sum())
    stats = {
        "forest_cells": forest_cells,
        "canopy_height_m": canopy_height,
    }
    return forest_cells, stats


def paint_landcover(
    layers: NDArray[np.uint8],
    material_index: dict[str, int],
    polygons_by_material: dict[str, Sequence[Any]],
    *,
    origin_x: float,
    origin_y: float,
    cell_size_m: float,
    natural_variation: bool = False,
) -> int:
    """Paint landcover polygons onto the terrain layer map.

    When ``natural_variation=True``, softens hard polygon boundaries with
    multi-scale noise dithering, breaks up monotonous ``Field`` blocks and open
    ``Grass`` meadows with ``Meadow`` / ``DryGrass`` patches, and avoids painting
    harsh brown ``ForestFloor`` slabs under tiny sparse tree clumps.

    Args:
        layers: (n, n) material index grid, modified in place.
        material_index: Mapping of material keys to layer indices.
        polygons_by_material: Mapping of material key to list of polygon shapes/rings.
        origin_x: World X of minimum corner.
        origin_y: World Y of minimum corner.
        cell_size_m: Ground spacing in metres.
        natural_variation: Enable organic noise-dithered edges and biome breakup.

    Returns:
        Total cells painted.
    """
    total_painted = 0
    n = layers.shape[0]
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m

    noise_macro: NDArray[np.float64] | None = None
    noise_detail: NDArray[np.float64] | None = None
    if natural_variation:
        noise_macro = _fbm_noise_grid(
            n,
            cell_size_m,
            origin_x,
            origin_y,
            seed=101,
            octaves_m=((120.0, 0.45), (44.0, 0.35), (max(14.0, 3.0 * cell_size_m), 0.20)),
        )
        noise_detail = _fbm_noise_grid(
            n,
            cell_size_m,
            origin_x,
            origin_y,
            seed=202,
            octaves_m=((36.0, 0.45), (14.0, 0.35), (max(3.5, 2.2 * cell_size_m), 0.20)),
        )

    for mat_key, poly_list in polygons_by_material.items():
        mat_idx = material_index.get(mat_key)
        if mat_idx is None:
            continue
        mask = np.zeros((n, n), dtype=bool)
        for shape in poly_list:
            poly = _to_shapely_polygon(shape)
            if poly is None:
                continue
            min_x, min_y, max_x, max_y = poly.bounds
            col_lo = max(0, math.floor((min_x - origin_x) / cell_size_m))
            col_hi = min(n - 1, math.ceil((max_x - origin_x) / cell_size_m))
            row_lo = max(0, math.floor((min_y - origin_y) / cell_size_m))
            row_hi = min(n - 1, math.ceil((max_y - origin_y) / cell_size_m))
            if col_hi < col_lo or row_hi < row_lo:
                continue
            ny = row_hi - row_lo + 1
            nx = col_hi - col_lo + 1
            gx = np.broadcast_to(all_x[col_lo : col_hi + 1][None, :], (ny, nx))
            gy = np.broadcast_to(all_y[row_lo : row_hi + 1][:, None], (ny, nx))
            inside = shapely.contains_xy(poly, gx.ravel(), gy.ravel()).reshape(ny, nx)
            mask[row_lo : row_hi + 1, col_lo : col_hi + 1] |= inside

        if not mask.any():
            continue

        if not natural_variation or mat_key == "Concrete" or cell_size_m > 4.0:
            layers[mask] = mat_idx
            total_painted += int(mask.sum())
            continue

        assert noise_macro is not None and noise_detail is not None
        feather_r = max(2, round(6.5 / cell_size_m))
        weight = _box_blur_float(mask.astype(np.float64), feather_r, passes=2)

        if mat_key == "ForestFloor":
            # Keep ForestFloor in deep woods with organic mossy Grass/Meadow glades so large
            # forest interiors never render as solid dark-brown dirt slabs; dither edges smoothly.
            paint = mask & ((weight >= 0.84) | (weight > (0.28 + 0.54 * noise_detail)))
            idx_grass = material_index.get("Grass")
            idx_meadow = material_index.get("Meadow")
            chosen_ff = np.full((n, n), mat_idx, dtype=np.uint8)
            glade_val = noise_macro * 0.60 + noise_detail * 0.40
            if idx_grass is not None:
                grass_glade = glade_val > 0.50
                chosen_ff = np.where(grass_glade, idx_grass, chosen_ff)
            if idx_meadow is not None:
                meadow_glade = (glade_val > 0.68) & (noise_detail > 0.52)
                chosen_ff = np.where(meadow_glade, idx_meadow, chosen_ff)
            layers[paint] = chosen_ff[paint]
            total_painted += int(paint.sum())
        elif mat_key == "Field":
            # Organic dithered field borders + stubble/DryGrass/Meadow breakup inside large fields
            dilated = _morph_mask(mask, feather_r, erode=False)
            paint = dilated & (weight > (0.18 + 0.62 * noise_detail))
            idx_dry = material_index.get("DryGrass")
            idx_meadow = material_index.get("Meadow")
            chosen = np.full((n, n), mat_idx, dtype=np.uint8)
            if idx_dry is not None:
                dry_patch = (noise_macro * 0.65 + noise_detail * 0.35) < 0.48
                chosen = np.where(dry_patch, idx_dry, chosen)
            if idx_meadow is not None:
                meadow_patch = (noise_macro > 0.62) & (noise_detail > 0.44)
                chosen = np.where(meadow_patch, idx_meadow, chosen)
            layers[paint] = chosen[paint]
            total_painted += int(paint.sum())
        else:
            # Grass or other soft cover: feather boundary smoothly
            dilated = _morph_mask(mask, feather_r, erode=False)
            paint = dilated & (weight > (0.20 + 0.60 * noise_detail))
            layers[paint] = mat_idx
            total_painted += int(paint.sum())

    if natural_variation and cell_size_m <= 8.0:
        assert noise_macro is not None and noise_detail is not None
        idx_grass = material_index.get("Grass", 0)
        idx_meadow = material_index.get("Meadow")
        idx_dry = material_index.get("DryGrass")
        open_grass = layers == idx_grass
        if open_grass.any() and (idx_meadow is not None or idx_dry is not None):
            blend_val = noise_macro * 0.65 + noise_detail * 0.35
            if idx_meadow is not None:
                meadow_mask = open_grass & (blend_val > 0.54) & (blend_val <= 0.74)
                layers[meadow_mask] = idx_meadow
                total_painted += int(meadow_mask.sum())
            if idx_dry is not None:
                dry_mask = open_grass & (blend_val > 0.74)
                layers[dry_mask] = idx_dry
                total_painted += int(dry_mask.sum())

    return total_painted


class _GridOverlayDem:
    """DemSampler backed by the prepared terrain grid with DEM fallback outside."""

    def __init__(
        self,
        elevations: NDArray[np.float64],
        projection: RegionProjection,
        origin_x: float,
        origin_y: float,
        cell_size_m: float,
        fallback_dem: DemSampler,
    ) -> None:
        self._elevations = elevations
        self._projection = projection
        self._origin_x = origin_x
        self._origin_y = origin_y
        self._cell_size_m = cell_size_m
        self._fallback = fallback_dem

    def sample_world_array(self, xs: Any, ys: Any) -> NDArray[np.float64]:
        xs_arr = np.asarray(xs, dtype=np.float64)
        ys_arr = np.asarray(ys, dtype=np.float64)
        shape = xs_arr.shape
        flat_x = xs_arr.ravel()
        flat_y = ys_arr.ravel()
        n = self._elevations.shape[0]
        fc = (flat_x - self._origin_x) / self._cell_size_m
        fr = (flat_y - self._origin_y) / self._cell_size_m
        in_grid = (fc >= 0.0) & (fc <= n - 1) & (fr >= 0.0) & (fr <= n - 1)

        out = np.empty(flat_x.shape, dtype=np.float64)
        if in_grid.any():
            fc_in = fc[in_grid]
            fr_in = fr[in_grid]
            c0 = np.floor(fc_in).astype(np.intp)
            r0 = np.floor(fr_in).astype(np.intp)
            c0 = np.minimum(c0, n - 2)
            r0 = np.minimum(r0, n - 2)
            dc = fc_in - c0
            dr = fr_in - r0
            z00 = self._elevations[r0, c0]
            z01 = self._elevations[r0, c0 + 1]
            z10 = self._elevations[r0 + 1, c0]
            z11 = self._elevations[r0 + 1, c0 + 1]
            out[in_grid] = (
                z00 * (1.0 - dc) * (1.0 - dr)
                + z01 * dc * (1.0 - dr)
                + z10 * (1.0 - dc) * dr
                + z11 * dc * dr
            )
        outside = ~in_grid
        if outside.any():
            out_lons, out_lats = self._projection.world_to_geo_array(flat_x[outside], flat_y[outside])
            out[outside] = np.asarray(
                self._fallback.sample_array(out_lons, out_lats),
                dtype=np.float64,
            )
        return out.reshape(shape)

    def sample_array(self, lons: Any, lats: Any) -> NDArray[np.float64]:
        lons_arr = np.asarray(lons, dtype=np.float64)
        lats_arr = np.asarray(lats, dtype=np.float64)
        xs, ys = self._projection.geo_to_world_array(lons_arr.ravel(), lats_arr.ravel())
        return self.sample_world_array(xs, ys).reshape(lons_arr.shape)


def build_terrain(
    dem: DemSampler,
    projection: RegionProjection,
    *,
    origin_x: float,
    origin_y: float,
    grid_size: int,
    cell_size_m: float,
    corridors: Sequence[RoadCorridor] = (),
    material_order: Sequence[str] = DEFAULT_MATERIAL_ORDER,
    ground_margin_m: float = 5.0,
    max_height_m: float | None = None,
    sample_step_m: float | None = None,
    smooth_window_m: float = 120.0,
    max_grade: float = 0.12,
    progress: Any = None,
    water_network: Any | None = None,
    forest_polygons: Sequence[Any] | None = None,
    landcover_polygons: dict[str, Sequence[Any]] | None = None,
    satellite_tex: str | None = None,
    canopy_height_m: float | None = None,
    carve_water_fn: Any | None = None,
    paint_landcover_fn: Any | None = None,
    bridge_roads: Sequence[Any] | None = None,
    road_settings: Any | None = None,
) -> TerrainBuild:
    """Build a complete terrain grid from a DEM plus optional road corridors.

    Args:
        dem: Elevation source.
        projection: World plane transform.
        origin_x: World X of the grid's minimum corner.
        origin_y: World Y of the grid's minimum corner.
        grid_size: Vertices per side.
        cell_size_m: Ground spacing in metres.
        corridors: Roads whose ground should be graded. Corridors without a
            ``surface_z`` get one derived from the DEM.
        material_order: Material keys in layer-map index order. Index 0 is the
            background fill.
        ground_margin_m: Headroom above and below the elevation range.
        max_height_m: Fixed vertical range, or ``None`` to derive it.
        sample_step_m: Road profile resampling spacing.
        smooth_window_m: Road profile smoothing window.
        max_grade: Maximum road gradient.
        progress: Optional callable ``progress(fraction, message)``.
        water_network: Extracted WaterNetwork for channel and basin carving.
        forest_polygons: Sequence of forest polygons for canopy stripping before grading.
        landcover_polygons: Mapping of material keys to polygon lists.
        satellite_tex: Optional path to satellite imagery / orthophoto base texture.
        canopy_height_m: Fixed canopy height override in metres.
        carve_water_fn: Optional custom water carving callable.
        paint_landcover_fn: Optional custom landcover painting callable.

    Returns:
        The completed :class:`TerrainBuild`.

    Raises:
        ValueError: If the grid geometry is invalid or the DEM has no data.
    """
    from ivanovo2beam.beamng.ter import MAX_SIZE, MIN_SIZE

    if not (MIN_SIZE <= grid_size <= MAX_SIZE) or grid_size & (grid_size - 1):
        raise ValueError(f"grid_size {grid_size} must be a power of two in [{MIN_SIZE}, {MAX_SIZE}]")
    if cell_size_m <= 0:
        raise ValueError(f"cell_size_m must be positive, got {cell_size_m}")

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    materials = tuple(material_order)
    material_index = {key: i for i, key in enumerate(materials)}

    report(0.05, f"sampling DEM onto a {grid_size}x{grid_size} grid")
    elevations = sample_dem_onto_grid(dem, projection, origin_x, origin_y, grid_size, cell_size_m)
    invalid = ~np.isfinite(elevations)
    if invalid.all():
        raise ValueError("DEM returned no finite elevations over the terrain extent")
    gap_cells = int(invalid.sum())
    leftover = 0.0
    if gap_cells:
        report(0.35, f"filling {gap_cells} DEM gaps")
        _, leftover = _fill_dem_gaps(elevations, invalid)
        if leftover:
            report(0.36, f"{leftover:.0f} cells had no valid neighbour; used the mean elevation")

    grid_extent = (grid_size - 1) * cell_size_m
    stats: dict[str, Any] = {
        "grid_size": grid_size,
        "cell_size_m": cell_size_m,
        "extent_m": grid_extent,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "dem_min_m": float(elevations.min()),
        "dem_max_m": float(elevations.max()),
        "dem_gap_cells": gap_cells,
        "dem_gap_cells_unfilled": int(leftover),
    }

    layers = np.zeros((grid_size, grid_size), dtype=np.uint8)

    # 1. Forest canopy stripping filter (BEFORE road grading!)
    if forest_polygons:
        report(0.38, f"stripping canopy from {len(forest_polygons)} forest polygons")
        forest_cells, forest_stats = strip_forest_canopy(
            elevations,
            layers,
            material_index,
            forest_polygons,
            origin_x=origin_x,
            origin_y=origin_y,
            cell_size_m=cell_size_m,
            fallback_canopy_m=canopy_height_m if canopy_height_m is not None else 12.0,
            canopy_height_m=canopy_height_m,
        )
        stats["forest_cells"] = forest_cells
        stats.update(forest_stats)

    # 2. Landcover material painting + Russian summer meadow variation
    if landcover_polygons or paint_landcover_fn is not None:
        report(0.39, "painting landcover polygons")
        if paint_landcover_fn is not None:
            paint_landcover_fn(
                layers,
                material_index,
                origin_x=origin_x,
                origin_y=origin_y,
                cell_size_m=cell_size_m,
            )
        elif landcover_polygons:
            paint_landcover(
                layers,
                material_index,
                landcover_polygons,
                origin_x=origin_x,
                origin_y=origin_y,
                cell_size_m=cell_size_m,
                natural_variation=True,
            )
    elif cell_size_m <= 8.0 and ("Meadow" in material_index or "DryGrass" in material_index):
        paint_landcover(
            layers,
            material_index,
            {},
            origin_x=origin_x,
            origin_y=origin_y,
            cell_size_m=cell_size_m,
            natural_variation=True,
        )

    # Re-anchor raised water elevations after canopy stripping so forest streams/ponds
    # align with the stripped ground rather than floating at canopy-top DEM heights.
    if water_network is not None:
        reanchored = _reanchor_water_to_terrain(
            elevations,
            water_network,
            origin_x=origin_x,
            origin_y=origin_y,
            cell_size_m=cell_size_m,
        )
        if reanchored:
            stats["water_reanchored_nodes"] = reanchored

    # 3. Road corridor grading + coupled bridge deck & ramp profiles
    graded_cells = 0
    prepared: list[RoadCorridor] = []
    bridge_profiles: dict[int, tuple[NDArray[np.float64], NDArray[np.float64]]] = {}
    bridge_spans: list[tuple[NDArray[np.float64], float]] = []

    profile_dem: DemSampler = _GridOverlayDem(
        elevations,
        projection,
        origin_x,
        origin_y,
        cell_size_m,
        dem,
    )
    if corridors:
        report(0.40, f"deriving profiles for {len(corridors)} road corridors")
        effective_sample_step = sample_step_m if sample_step_m is not None else max(8.0, cell_size_m)
        for i, corridor in enumerate(corridors):
            if corridor.surface_z is None and corridor.grade_terrain:
                points, profile = derive_road_profile(
                    profile_dem,
                    projection,
                    corridor.points,
                    sample_step_m=effective_sample_step,
                    smooth_window_m=smooth_window_m,
                    max_grade=max_grade,
                )
                corridor.points = points
                corridor.surface_z = profile
            elif not corridor.grade_terrain and corridor.key.startswith("way"):
                bridge_spans.append((corridor.points, corridor.half_width_m))
            prepared.append(corridor)
            if i % 50 == 0:
                report(0.40 + 0.25 * (i / max(1, len(corridors))), f"profiling road {i + 1}")

        pinned_count = pin_junction_profiles(prepared)
        stats["pinned_junctions"] = pinned_count

    if bridge_roads:
        from ivanovo2beam.config import RoadSettings
        from ivanovo2beam.roads.bridges import create_bridgehead_ramps, solve_coupled_bridge_profile

        r_settings = road_settings if road_settings is not None else RoadSettings()
        # Index ground-road endpoint elevations so bridge portals match smoothed approach grades
        ground_end_z: dict[tuple[int, int], tuple[float, int]] = {}
        for c in prepared:
            if not c.grade_terrain or c.surface_z is None:
                continue
            for pt_xy, z_val in ((c.points[0], float(c.surface_z[0])), (c.points[-1], float(c.surface_z[-1]))):
                k = (round(float(pt_xy[0]) * 2.0), round(float(pt_xy[1]) * 2.0))
                prev = ground_end_z.get(k)
                if prev is None or c.priority > prev[1]:
                    ground_end_z[k] = (z_val, c.priority)

        def _dem_world(wx: float, wy: float) -> float:
            return float(profile_dem.sample_world_array(np.array([wx]), np.array([wy]))[0])  # type: ignore[attr-defined]

        def _blend_approach_end(pt_key: tuple[int, int], target_z: float, ramp_len_m: float = 70.0) -> float | None:
            """Smoothly blend any ground road corridor ending at ``pt_key`` to ``target_z``."""
            matched_far_z: float | None = None
            for c in prepared:
                if not c.grade_terrain or c.surface_z is None or c.points.shape[0] < 2:
                    continue
                k_s = (round(float(c.points[0, 0]) * 2.0), round(float(c.points[0, 1]) * 2.0))
                k_e = (round(float(c.points[-1, 0]) * 2.0), round(float(c.points[-1, 1]) * 2.0))
                if k_s != pt_key and k_e != pt_key:
                    continue
                diffs_c = np.diff(c.points, axis=0)
                cum_c = np.concatenate(([0.0], np.cumsum(np.hypot(diffs_c[:, 0], diffs_c[:, 1]))))
                tot_c = float(cum_c[-1])
                eff_r = min(ramp_len_m, tot_c)
                if eff_r <= 1e-3:
                    continue
                if k_s == pt_key:
                    delta = target_z - float(c.surface_z[0])
                    for idx_p in range(c.surface_z.size):
                        d_p = float(cum_c[idx_p])
                        if d_p >= eff_r:
                            break
                        w = 0.5 * (1.0 + math.cos(math.pi * d_p / eff_r))
                        c.surface_z[idx_p] += delta * w
                    if tot_c >= ramp_len_m:
                        matched_far_z = float(np.interp(ramp_len_m, cum_c, c.surface_z))
                if k_e == pt_key:
                    delta = target_z - float(c.surface_z[-1])
                    for idx_p in range(c.surface_z.size - 1, -1, -1):
                        d_p = tot_c - float(cum_c[idx_p])
                        if d_p >= eff_r:
                            break
                        w = 0.5 * (1.0 + math.cos(math.pi * d_p / eff_r))
                        c.surface_z[idx_p] += delta * w
                    if tot_c >= ramp_len_m:
                        matched_far_z = float(np.interp(tot_c - ramp_len_m, cum_c, c.surface_z))
            return matched_far_z

        for br in bridge_roads:
            if not getattr(br, "bridge", False) or br.points.shape[0] < 2:
                continue
            k0 = (round(float(br.points[0, 0]) * 2.0), round(float(br.points[0, 1]) * 2.0))
            k1 = (round(float(br.points[-1, 0]) * 2.0), round(float(br.points[-1, 1]) * 2.0))
            ez0 = ground_end_z[k0][0] if k0 in ground_end_z else None
            ez1 = ground_end_z[k1][0] if k1 in ground_end_z else None
            b_pts, b_z = solve_coupled_bridge_profile(
                br,
                ground_sampler=_dem_world,
                water_network=water_network,
                approach_end_z=(ez0, ez1),
            )
            bridge_profiles[br.osm_id] = (b_pts, b_z)
            bridge_spans.append((b_pts, float(br.half_width_m) + 0.6))
            far_z0 = _blend_approach_end(k0, float(b_z[0]))
            far_z1 = _blend_approach_end(k1, float(b_z[-1]))
            from dataclasses import replace as _dc_replace

            br_solved = _dc_replace(br, points=b_pts)
            prepared.extend(
                create_bridgehead_ramps(
                    br_solved,
                    r_settings,
                    b_z,
                    ground_sampler=_dem_world,
                    far_z_override=(far_z0, far_z1),
                )
            )

        # Re-pin junctions so ground approach roads seamlessly meet lifted bridgeheads
        pin_junction_profiles(prepared)

    if prepared:
        report(0.65, "carving road corridors into the terrain")
        graded_cells, grade_stats = grade_corridors(
            elevations,
            layers,
            material_index,
            prepared,
            origin_x=origin_x,
            origin_y=origin_y,
            cell_size_m=cell_size_m,
            min_blend_skirt_m=15.0,
        )
        stats.update(grade_stats)
        stats["road_corridors"] = len(prepared)

    # 4. Water channel and basin carving (8 m clear of ground roads; clear dams under bridges)
    if water_network is not None or carve_water_fn is not None:
        report(0.75, "carving water channels and basins")
        if carve_water_fn is not None:
            carve_water_fn(
                elevations,
                layers,
                material_index,
                origin_x=origin_x,
                origin_y=origin_y,
                cell_size_m=cell_size_m,
            )
        elif water_network is not None:
            road_keep_mask = (
                build_road_keep_mask(
                    prepared,
                    origin_x=origin_x,
                    origin_y=origin_y,
                    grid_size=grid_size,
                    cell_size_m=cell_size_m,
                    road_clearance_m=8.0,
                )
                if prepared
                else None
            )
            water_cells, water_stats = carve_water(
                elevations,
                layers,
                material_index,
                water_network,
                origin_x=origin_x,
                origin_y=origin_y,
                cell_size_m=cell_size_m,
                road_keep_mask=road_keep_mask,
            )
            if bridge_spans:
                deck_carved = carve_water_under_decks(
                    elevations,
                    layers,
                    material_index,
                    water_network,
                    bridge_spans,
                    origin_x=origin_x,
                    origin_y=origin_y,
                    cell_size_m=cell_size_m,
                    road_keep_mask=road_keep_mask,
                )
                dam_lowered = level_under_decks(
                    elevations,
                    bridge_spans,
                    water_network,
                    origin_x=origin_x,
                    origin_y=origin_y,
                    cell_size_m=cell_size_m,
                    road_keep_mask=road_keep_mask,
                )
                water_stats["under_deck_carved"] = deck_carved
                water_stats["under_deck_leveled"] = dam_lowered
            stats["water_cells"] = water_cells
            stats.update(water_stats)

    # Clear GroundCover-spawning grass/meadow beneath bridge decks so tall
    # billboards never poke through bridge abutments or deck edges.
    if bridge_spans:
        idx_under = material_index.get("Gravel", material_index.get("Pebbles"))
        if idx_under is not None:
            under_bridge = _rasterize_bridge_under_mask(
                bridge_spans,
                origin_x=origin_x,
                origin_y=origin_y,
                grid_size=grid_size,
                cell_size_m=cell_size_m,
                end_margin_m=0.5,
            )
            grass_like = np.zeros((grid_size, grid_size), dtype=bool)
            for gk in ("Grass", "Meadow", "DryGrass", "Field", "ForestFloor"):
                gi = material_index.get(gk)
                if gi is not None:
                    grass_like |= layers == gi
            layers[under_bridge & grass_like] = idx_under

    report(0.80, "choosing the vertical range")
    if max_height_m is not None:
        base_z = float(np.floor(elevations.min())) - ground_margin_m
        max_height = float(max_height_m)
    else:
        base_z, max_height = TerrainFile.fit_vertical_range(elevations, margin_m=ground_margin_m)

    stats["base_z"] = base_z
    stats["max_height"] = max_height
    stats["vertical_step_m"] = max_height / 65536.0
    stats["materials"] = list(materials)
    stats["layer_histogram"] = {key: int((layers == idx).sum()) for idx, key in enumerate(materials)}
    if satellite_tex:
        stats["satellite_tex"] = satellite_tex

    report(0.95, "terrain ready")
    return TerrainBuild(
        elevations=elevations,
        layers=layers,
        materials=materials,
        base_z=base_z,
        max_height=max_height,
        origin_x=float(origin_x),
        origin_y=float(origin_y),
        cell_size_m=float(cell_size_m),
        graded_cells=graded_cells,
        stats=stats,
        satellite_image=satellite_tex,
        bridge_profiles=bridge_profiles,
    )
