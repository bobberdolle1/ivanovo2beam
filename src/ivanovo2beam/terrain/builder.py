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


def grade_corridors(
    elevations: NDArray[np.float64],
    layers: NDArray[np.uint8],
    material_index: dict[str, int],
    corridors: Sequence[RoadCorridor],
    *,
    origin_x: float,
    origin_y: float,
    cell_size_m: float,
) -> tuple[int, dict[str, Any]]:
    """Carve graded road corridors into an elevation grid, in place.

    For every cell the algorithm accumulates, over the corridors that reach it,
    a *surface* elevation (the road's own smoothed profile, priority-weighted
    across overlapping roads) and an *influence* alpha that is 1 inside a
    carriageway and falls smoothly to 0 at the corridor edge. The final
    elevation is a blend of the two, so the road blends into the landscape
    instead of cutting a trench into it.

    Args:
        elevations: ``(n, n)`` elevation grid, modified in place.
        layers: ``(n, n)`` material index grid, modified in place.
        material_index: Mapping of material key to layer index.
        corridors: Corridors to carve.
        origin_x: World X of the grid's minimum corner.
        origin_y: World Y of the grid's minimum corner.
        cell_size_m: Ground spacing in metres.

    Returns:
        ``(graded_cell_count, statistics)``.
    """
    n = elevations.shape[0]
    # The accumulator planes are float32 rather than float64. At 8192^2 that is
    # the difference between 1.6 GB and 800 MB of scratch, which is what makes a
    # region-scale grid fit in memory at all. The precision cost is immaterial
    # for this use: influence is a 0..1 blend weight, and the weighted elevation
    # sum is around 100-300 m, where float32 resolves about 1e-5 m -- five orders
    # of magnitude below the 0.9 mm the .ter format itself can store.
    influence = np.zeros((n, n), dtype=np.float32)
    weighted_z = np.zeros((n, n), dtype=np.float32)
    weight_sum = np.zeros((n, n), dtype=np.float32)
    # Material ownership, resolved by "which road has the most influence here".
    # Painting cannot be done inside the segment loop: a cell lying on the
    # centreline of one segment is beyond the endpoints of many others, and
    # those neighbours would repaint the carriageway as shoulder.
    owner_material = np.zeros((n, n), dtype=np.uint8)
    owner_shoulder = np.zeros((n, n), dtype=np.uint8)
    per_material: dict[str, int] = {}
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m

    graded = 0
    for corridor in corridors:
        if not corridor.grade_terrain or corridor.surface_z is None:
            continue
        pts = corridor.points
        z = corridor.surface_z
        half_width = corridor.half_width_m
        corridor_half = corridor.corridor_half_width_m
        # Higher priority roads pull the shared surface more strongly.
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

            # Grid window covering the segment's corridor.
            col_lo = max(0, math.floor((min(x0, x1) - corridor_half - origin_x) / cell_size_m))
            col_hi = min(n - 1, math.ceil((max(x0, x1) + corridor_half - origin_x) / cell_size_m))
            row_lo = max(0, math.floor((min(y0, y1) - corridor_half - origin_y) / cell_size_m))
            row_hi = min(n - 1, math.ceil((max(y0, y1) + corridor_half - origin_y) / cell_size_m))
            if col_hi < col_lo or row_hi < row_lo:
                continue

            grid_x = all_x[col_lo : col_hi + 1][None, :]
            grid_y = all_y[row_lo : row_hi + 1][:, None]

            # Distance from each cell centre to the segment.
            rel_x = grid_x - x0
            rel_y = grid_y - y0
            t = np.clip((rel_x * dx + rel_y * dy) / seg_len_sq, 0.0, 1.0)
            closest_x = x0 + t * dx
            closest_y = y0 + t * dy
            dist = np.hypot(grid_x - closest_x, grid_y - closest_y)

            inside = dist <= corridor_half
            if not inside.any():
                continue

            curvature = _smoothstep(half_width, corridor_half, dist)
            # 1 exactly inside the carriageway, tapering to 0 at the corridor edge.
            alpha = 1.0 - curvature
            road_z = z0 + t * (z1 - z0)

            window_influence = influence[row_lo : row_hi + 1, col_lo : col_hi + 1]
            segment_alpha = np.where(inside, alpha, 0.0)
            # Test for improvement *before* folding the segment in, otherwise
            # the comparison runs against the already-updated maximum and is
            # never true.
            better = segment_alpha > window_influence
            np.maximum(window_influence, segment_alpha, out=window_influence)

            weight = np.where(inside, alpha * priority_weight, 0.0)
            weighted_z[row_lo : row_hi + 1, col_lo : col_hi + 1] += weight * road_z
            weight_sum[row_lo : row_hi + 1, col_lo : col_hi + 1] += weight

            # The road with the most influence here owns the surface's appearance.
            if better.any():
                owner_material[row_lo : row_hi + 1, col_lo : col_hi + 1][better] = material_idx
                owner_shoulder[row_lo : row_hi + 1, col_lo : col_hi + 1][better] = shoulder_idx

            touched += int(inside.sum())

        graded += touched
        per_material[corridor.key or f"road_{id(corridor)}"] = touched

    blended = influence > 0
    if blended.any():
        inf_b = influence[blended].astype(np.float64)
        w_b = weight_sum[blended].astype(np.float64)
        surface_z = weighted_z[blended].astype(np.float64) / np.where(w_b > 0.0, w_b, 1.0)
        elevations[blended] = elevations[blended] * (1.0 - inf_b) + surface_z * inf_b

        # Paint materials from the accumulated influence. In the graded band
        # ``alpha`` reaches 1 only inside a carriageway, so the same field that
        # drives the blend also classifies the surface. Because ``influence``
        # is float32 (ULP ~6e-8), the tolerance must exceed float32 rounding.
        carriageway = influence >= np.float32(1.0 - 1e-6)
        shoulder = blended & ~carriageway
        layers[carriageway] = owner_material[carriageway]
        layers[shoulder] = owner_shoulder[shoulder]

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
) -> tuple[int, dict[str, Any]]:
    """Carve water channels, rivers, lakes, and basins into the terrain.

    Depresses terrain inside water channels and polygons down to z_water - depth_m
    with smoothstep bank transitions (6-12m) and paints bank materials:
    - Submerged bed: Pebbles (idx 11)
    - Shallow beaches: Sand (idx 10)
    - Wet banks: RiverbankSoil (idx 4)
    Preserves island rings inside water bodies.

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
    touched_mask = np.zeros((n, n), dtype=bool)
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m
    terrain_max_x = origin_x + (n - 1) * cell_size_m
    terrain_max_y = origin_y + (n - 1) * cell_size_m
    clip_pad = bw + cell_size_m
    clip_min_x = origin_x - clip_pad
    clip_min_y = origin_y - clip_pad
    clip_max_x = terrain_max_x + clip_pad
    clip_max_y = terrain_max_y + clip_pad
    shore_pad_cells = math.ceil(bw / cell_size_m) + 1
    outer_pad_cells = math.ceil(0.75 * bw / cell_size_m) + 1

    # 1. Carve linear river channels
    rivers = getattr(water_network, "rivers", ())
    for river in rivers:
        pts = river.points
        zw = river.z_water
        depth_m = float(river.depth_m)
        hw = float(river.width_m) / 2.0
        total_reach = hw + bw

        for i in range(pts.shape[0] - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            z0, z1 = zw[i], zw[i + 1]
            dx, dy = x1 - x0, y1 - y0
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 0:
                continue

            col_lo = max(0, math.floor((min(x0, x1) - total_reach - origin_x) / cell_size_m))
            col_hi = min(n - 1, math.ceil((max(x0, x1) + total_reach - origin_x) / cell_size_m))
            row_lo = max(0, math.floor((min(y0, y1) - total_reach - origin_y) / cell_size_m))
            row_hi = min(n - 1, math.ceil((max(y0, y1) + total_reach - origin_y) / cell_size_m))
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
            water_z = z0 + t * (z1 - z0)

            inside_channel = dist <= hw
            in_bank = (dist > hw) & (dist <= total_reach)
            if not (inside_channel.any() or in_bank.any()):
                continue

            # Channel bed: depress to water_z - depth_m
            sub_elev = elevations[row_lo : row_hi + 1, col_lo : col_hi + 1]
            sub_layers = layers[row_lo : row_hi + 1, col_lo : col_hi + 1]
            sub_touched = touched_mask[row_lo : row_hi + 1, col_lo : col_hi + 1]

            if inside_channel.any():
                bed_z = water_z - depth_m
                sub_elev[inside_channel] = np.minimum(sub_elev[inside_channel], bed_z[inside_channel])
                if idx_pebbles is not None:
                    sub_layers[inside_channel] = idx_pebbles
                sub_touched[inside_channel] = True

            if in_bank.any():
                frac = (dist[in_bank] - hw) / bw
                smooth = frac * frac * (3.0 - 2.0 * frac)
                bed_z = water_z[in_bank] - depth_m
                bank_z = bed_z + smooth * (sub_elev[in_bank] - bed_z)
                sub_elev[in_bank] = np.minimum(sub_elev[in_bank], bank_z)
                sub_touched[in_bank] = True

                shallow_bank = frac <= 0.45
                if idx_sand is not None and idx_riverbank_soil is not None:
                    sub_layers[in_bank] = np.where(shallow_bank, idx_sand, idx_riverbank_soil)
                elif idx_sand is not None:
                    sub_layers[in_bank] = np.where(shallow_bank, idx_sand, sub_layers[in_bank])
                elif idx_riverbank_soil is not None:
                    sub_layers[in_bank] = np.where(~shallow_bank, idx_riverbank_soil, sub_layers[in_bank])

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
        col_lo = max(0, math.floor((min_x - bw - origin_x) / cell_size_m))
        col_hi = min(n - 1, math.ceil((max_x + bw - origin_x) / cell_size_m))
        row_lo = max(0, math.floor((min_y - bw - origin_y) / cell_size_m))
        row_hi = min(n - 1, math.ceil((max_y + bw - origin_y) / cell_size_m))
        if col_hi < col_lo or row_hi < row_lo:
            continue

        ny = row_hi - row_lo + 1
        nx = col_hi - col_lo + 1
        grid_x = np.broadcast_to(all_x[col_lo : col_hi + 1][None, :], (ny, nx))
        grid_y = np.broadcast_to(all_y[row_lo : row_hi + 1][:, None], (ny, nx))

        # Vectorized containment: True for lake basin, False for islands (holes)
        inside = shapely.contains_xy(poly, grid_x.ravel(), grid_y.ravel()).reshape(ny, nx)
        if not inside.any():
            continue

        sub_elev = elevations[row_lo : row_hi + 1, col_lo : col_hi + 1]
        sub_layers = layers[row_lo : row_hi + 1, col_lo : col_hi + 1]
        sub_touched = touched_mask[row_lo : row_hi + 1, col_lo : col_hi + 1]

        # Erode/dilate so shapely.distance runs ONLY on the narrow shoreline band.
        deep_interior = _morph_mask(inside, shore_pad_cells, erode=True)
        if deep_interior.any():
            sub_elev[deep_interior] = np.minimum(sub_elev[deep_interior], z_water - depth_m)
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
                sub_elev[dr, dc] = np.minimum(sub_elev[dr, dc], z_water - depth_m)
                if idx_pebbles is not None:
                    sub_layers[dr, dc] = idx_pebbles
                sub_touched[dr, dc] = True

            shallow_mask = ~deep_mask
            if shallow_mask.any():
                sr = band_rows[shallow_mask]
                sc = band_cols[shallow_mask]
                t = dist_to_shore[shallow_mask] / bw
                smooth = t * t * (3.0 - 2.0 * t)
                bed_z = z_water - depth_m * smooth
                sub_elev[sr, sc] = np.minimum(sub_elev[sr, sc], bed_z)
                sub_touched[sr, sc] = True

                pebbles_sub = t > 0.4
                sand_sub = ~pebbles_sub
                if idx_pebbles is not None:
                    sub_layers[sr[pebbles_sub], sc[pebbles_sub]] = idx_pebbles
                if idx_sand is not None:
                    sub_layers[sr[sand_sub], sc[sand_sub]] = idx_sand

        # Paint wet bank immediately outside shoreline (within 0.75 * bw)
        outside_band = (~inside) & _morph_mask(inside, outer_pad_cells, erode=False)
        if outside_band.any():
            pts_outside = shapely.points(grid_x[outside_band], grid_y[outside_band])
            dist_out = shapely.distance(boundary, pts_outside)
            wet_out_mask = dist_out <= (0.75 * bw)
            if wet_out_mask.any():
                out_rows, out_cols = np.where(outside_band)
                wr = out_rows[wet_out_mask]
                wc = out_cols[wet_out_mask]
                if idx_riverbank_soil is not None:
                    sub_layers[wr, wc] = idx_riverbank_soil
                sub_touched[wr, wc] = True

    touched_count = int(touched_mask.sum())
    stats = {
        "carved_cells": touched_count,
        "rivers_carved": len(rivers),
        "water_bodies_carved": len(bodies),
    }
    return touched_count, stats


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
        if isinstance(shape, Polygon):
            poly = shape
        else:
            pts = np.asarray(shape, dtype=np.float64)
            if pts.shape[0] < 3:
                continue
            poly = Polygon(pts)
        if not poly.is_valid:
            poly = make_valid(poly)
            if poly.is_empty:
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
        elevations[forest_mask] -= canopy_height

    idx_forest_floor = material_index.get("ForestFloor")
    if idx_forest_floor is not None:
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
) -> int:
    """Paint landcover polygons onto the terrain layer map.

    Args:
        layers: (n, n) material index grid, modified in place.
        material_index: Mapping of material keys to layer indices.
        polygons_by_material: Mapping of material key to list of polygon shapes/rings.
        origin_x: World X of minimum corner.
        origin_y: World Y of minimum corner.
        cell_size_m: Ground spacing in metres.

    Returns:
        Total cells painted.
    """
    total_painted = 0
    n = layers.shape[0]
    all_x = origin_x + np.arange(n, dtype=np.float64) * cell_size_m
    all_y = origin_y + np.arange(n, dtype=np.float64) * cell_size_m
    for mat_key, poly_list in polygons_by_material.items():
        mat_idx = material_index.get(mat_key)
        if mat_idx is None:
            continue
        for shape in poly_list:
            if isinstance(shape, Polygon):
                poly = shape
            else:
                pts = np.asarray(shape, dtype=np.float64)
                if pts.shape[0] < 3:
                    continue
                poly = Polygon(pts)
            if not poly.is_valid:
                poly = make_valid(poly)
                if poly.is_empty:
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
            layers[row_lo : row_hi + 1, col_lo : col_hi + 1][inside] = mat_idx
            total_painted += int(inside.sum())
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

    # 2. Landcover material painting
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
            )

    # 3. Road corridor grading
    graded_cells = 0
    if corridors:
        report(0.40, f"deriving profiles for {len(corridors)} road corridors")
        profile_dem: DemSampler = _GridOverlayDem(
            elevations,
            projection,
            origin_x,
            origin_y,
            cell_size_m,
            dem,
        )
        effective_sample_step = sample_step_m if sample_step_m is not None else max(8.0, cell_size_m)
        prepared: list[RoadCorridor] = []
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
            prepared.append(corridor)
            if i % 50 == 0:
                report(0.40 + 0.25 * (i / max(1, len(corridors))), f"profiling road {i + 1}")
        report(0.65, "carving road corridors into the terrain")
        graded_cells, grade_stats = grade_corridors(
            elevations,
            layers,
            material_index,
            prepared,
            origin_x=origin_x,
            origin_y=origin_y,
            cell_size_m=cell_size_m,
        )
        stats.update(grade_stats)
        stats["road_corridors"] = len(prepared)

    # 4. Water channel and basin carving
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
            water_cells, water_stats = carve_water(
                elevations,
                layers,
                material_index,
                water_network,
                origin_x=origin_x,
                origin_y=origin_y,
                cell_size_m=cell_size_m,
            )
            stats["water_cells"] = water_cells
            stats.update(water_stats)

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
    )
