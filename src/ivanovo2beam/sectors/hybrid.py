"""Hybrid Terrain experimental pipeline (P1): 1 coarse base TerrainBlock + 1m RTIN corridor meshes.

Builds an isolated BeamNG level combining:
1. A single coarse base ``TerrainBlock`` (e.g. ``4096x4096 @ 4.0 m = 16.380 km``)
   covering the full sector for far terrain, rivers/lakes, forests, and
   ``GroundCover``, depressed smoothly (``-0.55 m``) strictly beneath highway
   corridor footprints so tires and raycasts never double-collide or poke through.
2. High-detail static COLLADA ``.dae`` corridor ground meshes (chunked in
   ``512 m`` sectors at ``1.0 m`` resolution, RTIN-simplified within
   ``0.05-0.15 m`` tolerance) covering P152 and major connecting corridors,
   feathered smoothly down below the base ``TerrainBlock`` outer edge with zero
   floating gaps, ``collisionType="Visible Mesh Final"``, ``decalType="Visible Mesh"``,
   and UV-mapped native BeamNG PBR materials.
3. Corridor ``DecalRoad`` surfaces, markings, and edge blends projected onto the
   corridor meshes with ``overObjects=True`` and stratified ``decalBias``.
"""

from __future__ import annotations

import json
import math
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import numpy as np
import yaml

from ivanovo2beam.beamng.level import (
    SceneTree,
    TerrainPlacement,
    add_spawn_point,
    build_info_json,
    build_sun_and_sky,
    stable_uuid,
    write_level_files,
)
from ivanovo2beam.beamng.materials import (
    DEFAULT_BASE_TEX_SIZE,
    DEFAULT_MATERIAL_ORDER,
    MATERIAL_LIBRARY,
    _encode_png,
    _tileable_color_patch,
    build_materials_json,
    referenced_asset_paths,
    write_terrain_base_textures,
)
from ivanovo2beam.beamng.preview import generate_level_preview_png
from ivanovo2beam.beamng.ter import HOLE, MAX_SIZE, MIN_SIZE, TerrainFile, TerrainFormatError, read_ter, write_ter
from ivanovo2beam.build import (
    ROAD_RENDER_MARGIN_M,
    _choose_spawn,
    build_terrain_json,
    fetch_level_osm,
    package_level,
    write_json,
)
from ivanovo2beam.buildings.export import export_buildings
from ivanovo2beam.buildings.footprints import extract_buildings
from ivanovo2beam.config import ConfigError, ProjectConfig
from ivanovo2beam.gis.projection import BBox
from ivanovo2beam.provenance import build_attribution_manifest, write_attribution_files
from ivanovo2beam.roads.bridges import (
    compute_bridge_deck_profile,
    create_bridgehead_ramps,
    emit_bridges_materials,
    export_bridge_bundle,
)
from ivanovo2beam.roads.build import (
    _should_generate_markings,
    add_network_to_scene,
    build_carriageway_index,
    generate_lane_markings,
    road_to_decal_objects,
    roads_to_corridors,
    sample_points_z,
    sample_surface_z,
)
from ivanovo2beam.roads.materials import write_road_materials
from ivanovo2beam.roads.network import extract_roads, road_network_stats
from ivanovo2beam.roads.props import add_roadside_props_to_scene
from ivanovo2beam.roads.railways import export_railways, extract_railways, railways_to_corridors
from ivanovo2beam.sectors.multiblock import resolve_dem_for_bbox
from ivanovo2beam.terrain.builder import (
    RoadCorridor,
    TerrainBuild,
    _rasterize_bridge_under_mask,
    build_terrain,
    grade_corridors,
)
from ivanovo2beam.terrain.dem import DemReader
from ivanovo2beam.validation.level import (
    ValidationReport,
    _check_materials,
    _check_scale,
    _check_scene_tree,
    _check_terrain,
)
from ivanovo2beam.vegetation.extract import extract_vegetation
from ivanovo2beam.vegetation.forest import export_vegetation
from ivanovo2beam.water.export import export_water_to_scene
from ivanovo2beam.water.extract import extract_water

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from numpy.typing import NDArray

#: Fixed deterministic COLLADA timestamp.
_FIXED_TIMESTAMP = "2026-01-01T00:00:00Z"

#: RTIN force sentinel error.
_RTIN_FORCE: float = 1e9

#: Default highway classes included in the high-detail static corridor mesh.
DEFAULT_CORRIDOR_HIGHWAYS: tuple[str, ...] = (
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
)

_ANNOTATION_BY_MATERIAL: dict[str, str] = {
    "Asphalt": "ASPHALT",
    "Asphalt_worn": "ASPHALT",
    "Concrete": "CONCRETE",
}


@dataclass(frozen=True, slots=True)
class HybridSpec:
    """Configuration for a P1 Hybrid Terrain build."""

    corridor_cell_size_m: float = 1.0
    tile_squares: int = 512
    rtin_max_error_m: float = 0.08
    corridor_highways: tuple[str, ...] = DEFAULT_CORRIDOR_HIGHWAYS
    depress_depth_m: float = 0.55
    depress_inner_extra_m: float = 6.0
    depress_outer_extra_m: float = 12.0
    feather_width_m: float = 4.0
    feather_depth_m: float = 0.55
    base_tex_size: int = DEFAULT_BASE_TEX_SIZE
    groundcover_enabled: bool = True
    max_buildings: int | None = 6000
    max_trees: int = 45000
    extra_spawns: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not math.isfinite(self.corridor_cell_size_m) or self.corridor_cell_size_m <= 0.0:
            raise ConfigError(f"corridor_cell_size_m must be positive, got {self.corridor_cell_size_m!r}")
        if (
            not isinstance(self.tile_squares, int)
            or isinstance(self.tile_squares, bool)
            or self.tile_squares < 16
            or (self.tile_squares & (self.tile_squares - 1)) != 0
        ):
            raise ConfigError(f"tile_squares must be a power of two >= 16, got {self.tile_squares!r}")
        if not math.isfinite(self.rtin_max_error_m) or self.rtin_max_error_m <= 0.0:
            raise ConfigError(f"rtin_max_error_m must be positive, got {self.rtin_max_error_m!r}")
        if not (0.20 <= self.depress_depth_m <= 1.50):
            raise ConfigError(f"depress_depth_m must be in [0.20, 1.50], got {self.depress_depth_m!r}")
        if self.depress_outer_extra_m <= self.depress_inner_extra_m:
            raise ConfigError("depress_outer_extra_m must exceed depress_inner_extra_m")
        if self.feather_width_m <= 0.0 or self.feather_depth_m <= 0.0:
            raise ConfigError("feather_width_m and feather_depth_m must be positive")

    @property
    def tile_extent_m(self) -> float:
        """Metric span of one corridor mesh chunk."""
        return float(self.tile_squares) * float(self.corridor_cell_size_m)

    @property
    def mesh_outer_extra_m(self) -> float:
        """Total extra half-width beyond road half_width_m covered by the mesh."""
        return self.depress_outer_extra_m + self.feather_width_m

    @classmethod
    def from_yaml(cls, path: str | Path) -> HybridSpec:
        """Load the ``hybrid`` section from a YAML config file."""
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a top-level mapping")
        hb = raw.get("hybrid") or {}
        if not isinstance(hb, dict):
            raise ConfigError(f"{path}: 'hybrid' must be a mapping")

        hw_raw = hb.get("corridor_highways")
        highways = tuple(str(h) for h in hw_raw) if hw_raw is not None else DEFAULT_CORRIDOR_HIGHWAYS
        max_b_raw = hb.get("max_buildings", 6000)
        extra_spawns = tuple(dict(s) for s in (hb.get("extra_spawns") or ()))
        return cls(
            corridor_cell_size_m=float(hb.get("corridor_cell_size_m", 1.0)),
            tile_squares=int(hb.get("tile_squares", 512)),
            rtin_max_error_m=float(hb.get("rtin_max_error_m", 0.08)),
            corridor_highways=highways,
            depress_depth_m=float(hb.get("depress_depth_m", 0.55)),
            depress_inner_extra_m=float(hb.get("depress_inner_extra_m", 6.0)),
            depress_outer_extra_m=float(hb.get("depress_outer_extra_m", 12.0)),
            feather_width_m=float(hb.get("feather_width_m", 4.0)),
            feather_depth_m=float(hb.get("feather_depth_m", 0.55)),
            base_tex_size=int(hb.get("base_tex_size", DEFAULT_BASE_TEX_SIZE)),
            groundcover_enabled=bool(hb.get("groundcover_enabled", True)),
            max_buildings=None if max_b_raw is None else int(max_b_raw),
            max_trees=int(hb.get("max_trees", 45000)),
            extra_spawns=extra_spawns,
        )


@dataclass(slots=True)
class CorridorTileMesh:
    """One simplified RTIN ground/corridor mesh chunk."""

    tx: int
    ty: int
    key: str
    origin_x: float
    origin_y: float
    base_z: float
    cell_size_m: float
    tile_squares: int
    positions: NDArray[np.float64]  # (V, 3) world-space relative to (origin_x, origin_y, base_z)
    normals: NDArray[np.float64]  # (V, 3)
    uvs: NDArray[np.float64]  # (V, 2)
    triangles_by_material: dict[str, NDArray[np.int32]]  # mat_name -> (K, 3) vertex indices
    z_grid_unfeathered: NDArray[np.float64]  # (T+1, T+1) exact surface Z before outer feather
    active_mask: NDArray[np.bool_]  # (T+1, T+1) True where mesh is active (above depressed terrain)
    triangle_count: int
    full_triangle_count: int
    refine_passes: int = 0
    dae_path: Path | None = None
    dae_bytes: int = 0


@dataclass(slots=True)
class HybridSurfaceIndex:
    """Fast spatial lookup for the high-detail 1 m corridor surface over the sector."""

    tile_extent_m: float
    origin_x: float
    origin_y: float
    cell_size_m: float
    tiles_by_coord: dict[tuple[int, int], CorridorTileMesh] = field(default_factory=dict)

    def sample_z(self, x: float, y: float, fallback_z: float) -> tuple[float, bool]:
        """Return ``(z, on_corridor_mesh)`` at world ``(x, y)``."""
        rx = (x - self.origin_x) / self.tile_extent_m
        ry = (y - self.origin_y) / self.tile_extent_m
        tx = math.floor(rx)
        ty = math.floor(ry)
        tile = self.tiles_by_coord.get((tx, ty))
        if tile is None:
            if abs(rx - tx) < 1e-6:
                tile = self.tiles_by_coord.get((tx - 1, ty))
            if tile is None and abs(ry - ty) < 1e-6:
                tile = self.tiles_by_coord.get((tx, ty - 1))
            if tile is None:
                return fallback_z, False

        fx = (x - tile.origin_x) / tile.cell_size_m
        fy = (y - tile.origin_y) / tile.cell_size_m
        t = tile.tile_squares
        if fx < -1e-6 or fy < -1e-6 or fx > t + 1e-6 or fy > t + 1e-6:
            return fallback_z, False

        fx_c = min(max(fx, 0.0), float(t))
        fy_c = min(max(fy, 0.0), float(t))
        c0 = min(int(math.floor(fx_c)), t - 1)
        r0 = min(int(math.floor(fy_c)), t - 1)
        wx = fx_c - c0
        wy = fy_c - r0

        m = tile.active_mask
        if not (m[r0, c0] or m[r0, c0 + 1] or m[r0 + 1, c0] or m[r0 + 1, c0 + 1]):
            return fallback_z, False

        zg = tile.z_grid_unfeathered
        z00 = float(zg[r0, c0])
        z01 = float(zg[r0, c0 + 1])
        z10 = float(zg[r0 + 1, c0])
        z11 = float(zg[r0 + 1, c0 + 1])
        z = (z00 * (1.0 - wx) + z01 * wx) * (1.0 - wy) + (z10 * (1.0 - wx) + z11 * wx) * wy
        return z, True


@dataclass(slots=True)
class HybridBuildResult:
    """Outcome of a P1 hybrid terrain experimental build."""

    level_id: str
    level_dir: Path
    mod_zip: Path | None
    corridor_tiles: list[CorridorTileMesh]
    stats: dict[str, Any] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Return a concise multi-line build summary."""
        total_tris = sum(t.triangle_count for t in self.corridor_tiles)
        total_full = sum(t.full_triangle_count for t in self.corridor_tiles)
        total_bytes = sum(t.dae_bytes for t in self.corridor_tiles)
        ratio = (100.0 * total_tris / total_full) if total_full else 0.0
        lines = [
            f"level {self.level_id} (P1 Hybrid Terrain)",
            f"  output: {self.level_dir}",
            (
                f"  corridor meshes: {len(self.corridor_tiles)} chunks, "
                f"{total_tris:,} tris ({ratio:.1f}% of {total_full:,} full), "
                f"{total_bytes / 1e6:.2f} MB DAE"
            ),
            (
                f"  base terrain: {self.stats.get('base_grid_size')}x{self.stats.get('base_grid_size')} "
                f"@ {self.stats.get('base_square_size_m')} m, "
                f"ter={self.stats.get('terrain_bytes', 0) / 1e6:.2f} MB, "
                f"depressed_cells={self.stats.get('depressed_cells', 0):,}"
            ),
        ]
        if self.mod_zip:
            lines.append(f"  mod zip: {self.mod_zip}")
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vectorized RTIN simplification (adapted from MapNG groundMesh.js, MIT)
# ---------------------------------------------------------------------------

_RTIN_PLAN_CACHE: dict[int, list[tuple[Any, ...]]] = {}


def _get_rtin_plan(tile_squares: int) -> list[tuple[Any, ...]]:
    """Build and cache per-depth RTIN triangle arrays for ``tile_squares``."""
    cached = _RTIN_PLAN_CACHE.get(tile_squares)
    if cached is not None:
        return cached

    t = tile_squares
    depth = 2 * int(round(math.log2(t)))
    ax = np.array([0, t], dtype=np.int32)
    ay = np.array([0, t], dtype=np.int32)
    bx = np.array([t, 0], dtype=np.int32)
    by = np.array([t, 0], dtype=np.int32)
    cx = np.array([t, 0], dtype=np.int32)
    cy = np.array([0, t], dtype=np.int32)

    levels: list[tuple[Any, ...]] = []
    for d in range(depth):
        mx = (ax + bx) >> 1
        my = (ay + by) >> 1
        if d < depth - 1:
            # Left child (c, a, m) and right child (b, c, m) midpoint indices
            lx = (cx + ax) >> 1
            ly = (cy + ay) >> 1
            rx = (bx + cx) >> 1
            ry = (by + cy) >> 1
            levels.append((ax, ay, bx, by, mx, my, lx, ly, rx, ry))
        else:
            levels.append((ax, ay, bx, by, mx, my, None, None, None, None))
            break

        # Expand to next level
        n_ax = np.empty(ax.size * 2, dtype=np.int32)
        n_ay = np.empty_like(n_ax)
        n_bx = np.empty_like(n_ax)
        n_by = np.empty_like(n_ax)
        n_cx = np.empty_like(n_ax)
        n_cy = np.empty_like(n_ax)

        n_ax[0::2], n_ay[0::2] = cx, cy
        n_bx[0::2], n_by[0::2] = ax, ay
        n_cx[0::2], n_cy[0::2] = mx, my

        n_ax[1::2], n_ay[1::2] = bx, by
        n_bx[1::2], n_by[1::2] = cx, cy
        n_cx[1::2], n_cy[1::2] = mx, my

        ax, ay, bx, by, cx, cy = n_ax, n_ay, n_bx, n_by, n_cx, n_cy

    _RTIN_PLAN_CACHE[tile_squares] = levels
    return levels


def rtin_errors(
    heights: NDArray[np.float64],
    force: NDArray[np.bool_],
    tile_squares: int,
) -> NDArray[np.float32]:
    """Compute bottom-up RTIN midpoint approximation errors for a tile."""
    plan = _get_rtin_plan(tile_squares)
    errors = np.where(force, _RTIN_FORCE, 0.0).astype(np.float32)

    for ax, ay, bx, by, mx, my, lx, ly, rx, ry in reversed(plan):
        e = np.abs(0.5 * (heights[ay, ax] + heights[by, bx]) - heights[my, mx]).astype(np.float32)
        m = np.maximum(errors[my, mx], e)
        if lx is not None:
            child_err = np.maximum(errors[ly, lx], errors[ry, rx])
            m = np.maximum(m, child_err)
        np.maximum.at(errors, (my, mx), m)

    return errors


def _build_keep_sat(keep_mask: NDArray[np.bool_]) -> NDArray[np.int32]:
    """Build 2D summed-area table over ``(T, T)`` kept squares."""
    t = keep_mask.shape[0]
    sat = np.zeros((t + 1, t + 1), dtype=np.int32)
    sat[1:, 1:] = keep_mask.astype(np.int32)
    np.cumsum(sat, axis=0, out=sat)
    np.cumsum(sat, axis=1, out=sat)
    return sat


def rtin_triangles(
    errors: NDArray[np.float32],
    tile_squares: int,
    max_error: float,
    keep_mask: NDArray[np.bool_] | None = None,
) -> list[tuple[int, int, int, int, int, int]]:
    """Extract RTIN triangles as ``(ax, ay, cx, cy, bx, by)`` tuples (CCW from above).

    When ``keep_mask`` is supplied, subtrees whose axis-aligned bounding box
    contains zero kept squares are pruned immediately.
    """
    t = tile_squares
    sat = _build_keep_sat(keep_mask) if keep_mask is not None else None
    out: list[tuple[int, int, int, int, int, int]] = []

    stack: list[tuple[int, int, int, int, int, int]] = [
        (0, 0, t, t, t, 0),
        (t, t, 0, 0, 0, t),
    ]
    while stack:
        ax, ay, bx, by, cx, cy = stack.pop()
        if sat is not None:
            x0 = min(ax, bx, cx)
            x1 = max(ax, bx, cx)
            y0 = min(ay, by, cy)
            y1 = max(ay, by, cy)
            if sat[y1, x1] - sat[y0, x1] - sat[y1, x0] + sat[y0, x0] <= 0:
                continue

        if abs(ax - cx) + abs(ay - cy) > 1:
            mx = (ax + bx) >> 1
            my = (ay + by) >> 1
            if float(errors[my, mx]) > max_error:
                stack.append((bx, by, cx, cy, mx, my))
                stack.append((cx, cy, ax, ay, mx, my))
                continue
        out.append((ax, ay, cx, cy, bx, by))

    return out


def _refine_rtin_exact(
    heights: NDArray[np.float64],
    force: NDArray[np.bool_],
    tile_squares: int,
    max_error: float,
    keep_mask: NDArray[np.bool_],
) -> tuple[list[tuple[int, int, int, int, int, int]], int]:
    """Iteratively split any triangle whose interior violates ``max_error``."""
    refine_passes = 0
    for _ in range(12):
        errors = rtin_errors(heights, force, tile_squares)
        tris = rtin_triangles(errors, tile_squares, max_error, keep_mask=keep_mask)
        added = 0
        for ax, ay, cx, cy, bx, by in tris:
            if abs(ax - cx) + abs(ay - cy) <= 1:
                continue
            ca = (ax + cx + bx) // 3
            cb = (ay + cy + by) // 3
            if not keep_mask[cb, ca]:
                continue
            ha = float(heights[ay, ax])
            hb = float(heights[by, bx])
            hc = float(heights[cy, cx])
            d = float((bx - ax) * (cy - ay) - (cx - ax) * (by - ay))
            if abs(d) < 1e-12:
                continue
            x0, x1 = min(ax, bx, cx), max(ax, bx, cx)
            y0, y1 = min(ay, by, cy), max(ay, by, cy)
            xs = np.arange(x0, x1 + 1, dtype=np.float64)[None, :]
            ys = np.arange(y0, y1 + 1, dtype=np.float64)[:, None]
            lb = ((xs - ax) * (cy - ay) - (cx - ax) * (ys - ay)) / d
            lc = ((bx - ax) * (ys - ay) - (xs - ax) * (by - ay)) / d
            la = 1.0 - lb - lc
            inside = (la >= -1e-9) & (lb >= -1e-9) & (lc >= -1e-9)
            if not inside.any():
                continue
            plane_z = la * ha + lb * hb + lc * hc
            sub_h = heights[y0 : y1 + 1, x0 : x1 + 1]
            max_diff = float(np.max(np.abs(plane_z[inside] - sub_h[inside])))
            if max_diff > max_error:
                mx = (ax + bx) >> 1
                my = (ay + by) >> 1
                if not force[my, mx]:
                    force[my, mx] = True
                    added += 1
        if not added:
            return tris, refine_passes
        refine_passes += 1

    errors = rtin_errors(heights, force, tile_squares)
    return rtin_triangles(errors, tile_squares, max_error, keep_mask=keep_mask), refine_passes


# ---------------------------------------------------------------------------
# Corridor distance field, base terrain depression, and tile synthesis
# ---------------------------------------------------------------------------


def _bilinear_sample_grid(
    grid: NDArray[np.float64],
    grid_ox: float,
    grid_oy: float,
    grid_cell: float,
    xs: NDArray[np.float64],
    ys: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Bilinearly sample a 2D ``[row, col]`` grid at arbitrary 2D coordinate arrays."""
    n = grid.shape[0]
    fx = np.clip((xs - grid_ox) / grid_cell, 0.0, float(n - 1))
    fy = np.clip((ys - grid_oy) / grid_cell, 0.0, float(n - 1))
    c0 = np.minimum(np.floor(fx).astype(np.intp), n - 2)
    r0 = np.minimum(np.floor(fy).astype(np.intp), n - 2)
    tx = fx - c0
    ty = fy - r0
    z00 = grid[r0, c0]
    z01 = grid[r0, c0 + 1]
    z10 = grid[r0 + 1, c0]
    z11 = grid[r0 + 1, c0 + 1]
    return (z00 * (1.0 - tx) + z01 * tx) * (1.0 - ty) + (z10 * (1.0 - tx) + z11 * tx) * ty


def _nearest_sample_grid_u8(
    grid: NDArray[np.uint8],
    grid_ox: float,
    grid_oy: float,
    grid_cell: float,
    xs: NDArray[np.float64],
    ys: NDArray[np.float64],
) -> NDArray[np.uint8]:
    """Nearest-neighbour sample a 2D ``[row, col]`` uint8 layer grid."""
    n = grid.shape[0]
    cols = np.clip(np.rint((xs - grid_ox) / grid_cell).astype(np.intp), 0, n - 1)
    rows = np.clip(np.rint((ys - grid_oy) / grid_cell).astype(np.intp), 0, n - 1)
    return grid[rows, cols]


def _clearance_from_corridors(
    corridors: Sequence[RoadCorridor],
    *,
    grid_ox: float,
    grid_oy: float,
    rows: int,
    cols: int,
    cell_size_m: float,
    max_reach_extra_m: float,
    exclude_mask: NDArray[np.bool_] | None = None,
) -> tuple[NDArray[np.float64], NDArray[np.uint8]]:
    """Compute signed clearance ``min(dist - half_width_m)`` and dominating corridor flags.

    Returns:
        ``(clearance_m, in_carriageway)`` arrays of shape ``(rows, cols)``.
    """
    clearance = np.full((rows, cols), np.inf, dtype=np.float64)
    in_cw = np.zeros((rows, cols), dtype=np.bool_)
    if not corridors:
        return clearance, in_cw

    all_x = grid_ox + np.arange(cols, dtype=np.float64) * cell_size_m
    all_y = grid_oy + np.arange(rows, dtype=np.float64) * cell_size_m
    grid_min_x = float(all_x[0])
    grid_max_x = float(all_x[-1])
    grid_min_y = float(all_y[0])
    grid_max_y = float(all_y[-1])

    for corridor in corridors:
        if not corridor.grade_terrain:
            continue
        pts = corridor.points
        if pts.shape[0] < 2:
            continue
        hw = float(corridor.half_width_m)
        reach = hw + max_reach_extra_m
        pmin_x = float(pts[:, 0].min()) - reach
        pmax_x = float(pts[:, 0].max()) + reach
        pmin_y = float(pts[:, 1].min()) - reach
        pmax_y = float(pts[:, 1].max()) + reach
        if pmax_x < grid_min_x or pmin_x > grid_max_x or pmax_y < grid_min_y or pmin_y > grid_max_y:
            continue

        for i in range(pts.shape[0] - 1):
            x0, y0 = float(pts[i, 0]), float(pts[i, 1])
            x1, y1 = float(pts[i + 1, 0]), float(pts[i + 1, 1])
            dx = x1 - x0
            dy = y1 - y0
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 1e-12:
                continue

            col_lo = max(0, math.floor((min(x0, x1) - reach - grid_ox) / cell_size_m))
            col_hi = min(cols - 1, math.ceil((max(x0, x1) + reach - grid_ox) / cell_size_m))
            row_lo = max(0, math.floor((min(y0, y1) - reach - grid_oy) / cell_size_m))
            row_hi = min(rows - 1, math.ceil((max(y0, y1) + reach - grid_oy) / cell_size_m))
            if col_hi < col_lo or row_hi < row_lo:
                continue

            gx = all_x[col_lo : col_hi + 1][None, :]
            gy = all_y[row_lo : row_hi + 1][:, None]
            t = np.clip(((gx - x0) * dx + (gy - y0) * dy) / seg_len_sq, 0.0, 1.0)
            dist = np.hypot(gx - (x0 + t * dx), gy - (y0 + t * dy))
            signed = dist - hw
            win = clearance[row_lo : row_hi + 1, col_lo : col_hi + 1]
            np.minimum(win, signed, out=win)
            in_cw[row_lo : row_hi + 1, col_lo : col_hi + 1] |= signed <= 0.0

    if exclude_mask is not None and exclude_mask.any():
        clearance[exclude_mask] = np.inf
        in_cw[exclude_mask] = False

    return clearance, in_cw


def depress_base_terrain(
    base_build: TerrainBuild,
    corridor_roads_only: Sequence[RoadCorridor],
    spec: HybridSpec,
    *,
    bridge_spans: Sequence[tuple[NDArray[np.float64], float]] = (),
) -> tuple[TerrainBuild, NDArray[np.float64], int]:
    """Return a depressed copy of ``base_build`` under corridor footprints.

    Inside ``clearance <= depress_inner_extra_m``, elevations are depressed by
    ``-spec.depress_depth_m`` so the coarse ``TerrainBlock`` never pokes through
    or double-collides with the high-detail corridor ``.dae`` meshes. Across
    ``(depress_inner_extra_m, depress_outer_extra_m]``, the depression ramps
    smoothly back to ``0.0 m``. Materials inside ``depress_outer_extra_m`` are
    painted as ``Asphalt`` / ``Gravel`` so ``GroundCover`` never spawns under
    corridor meshes.
    """
    n = base_build.grid_size
    under_bridge = None
    if bridge_spans:
        under_bridge = _rasterize_bridge_under_mask(
            bridge_spans,
            origin_x=base_build.origin_x,
            origin_y=base_build.origin_y,
            grid_size=n,
            cell_size_m=base_build.cell_size_m,
            end_margin_m=0.0,
        )

    clearance, in_cw = _clearance_from_corridors(
        corridor_roads_only,
        grid_ox=base_build.origin_x,
        grid_oy=base_build.origin_y,
        rows=n,
        cols=n,
        cell_size_m=base_build.cell_size_m,
        max_reach_extra_m=spec.depress_outer_extra_m,
        exclude_mask=under_bridge,
    )

    elev = base_build.elevations.copy()
    layers = base_build.layers.copy()
    mat_idx = {k: i for i, k in enumerate(base_build.materials)}
    asphalt_idx = mat_idx.get("Asphalt", 0)
    gravel_idx = mat_idx.get("Gravel", asphalt_idx)

    inner = spec.depress_inner_extra_m
    outer = spec.depress_outer_extra_m
    depth = spec.depress_depth_m

    affected = clearance <= outer
    depressed_count = int(affected.sum())
    if depressed_count:
        offset = np.zeros_like(elev)
        flat = clearance <= inner
        ramp = affected & ~flat
        offset[flat] = -depth
        if ramp.any():
            frac = np.clip((clearance[ramp] - inner) / (outer - inner), 0.0, 1.0)
            offset[ramp] = -depth * 0.5 * (1.0 + np.cos(math.pi * frac))
        elev += offset

        # Suppress GroundCover beneath corridor meshes without overwriting Asphalt carriageways.
        grass_like = np.zeros((n, n), dtype=bool)
        for gk in ("Grass", "Meadow", "DryGrass", "Field", "ForestFloor"):
            gi = mat_idx.get(gk)
            if gi is not None:
                grass_like |= layers == gi
        layers[affected & in_cw] = asphalt_idx
        layers[affected & ~in_cw & grass_like] = gravel_idx

    base_z, max_height = TerrainFile.fit_vertical_range(
        elev,
        margin_m=max(4.0, base_build.base_z - float(elev.min()) + 5.0)
        if base_build.max_height <= 0
        else 8.0,
    )
    depressed_build = TerrainBuild(
        elevations=elev,
        layers=layers,
        materials=base_build.materials,
        base_z=min(base_z, base_build.base_z),
        max_height=max(max_height, base_build.max_height + depth),
        origin_x=base_build.origin_x,
        origin_y=base_build.origin_y,
        cell_size_m=base_build.cell_size_m,
        graded_cells=base_build.graded_cells,
        stats=dict(base_build.stats),
        satellite_image=base_build.satellite_image,
        bridge_profiles=dict(base_build.bridge_profiles),
    )
    return depressed_build, clearance, depressed_count


# ---------------------------------------------------------------------------
# Corridor RTIN tile meshing & COLLADA 1.4.1 serialization
# ---------------------------------------------------------------------------


def _select_active_corridor_tiles(
    corridor_roads_only: Sequence[RoadCorridor],
    spec: HybridSpec,
    *,
    origin_x: float,
    origin_y: float,
    total_extent_m: float,
) -> list[tuple[int, int]]:
    """Return sorted ``(tx, ty)`` indices of corridor mesh tiles intersecting corridors."""
    tile_m = spec.tile_extent_m
    max_tx = max(0, math.ceil(total_extent_m / tile_m) - 1)
    max_ty = max(0, math.ceil(total_extent_m / tile_m) - 1)
    active: set[tuple[int, int]] = set()

    for c in corridor_roads_only:
        if not c.grade_terrain or c.points.shape[0] < 2:
            continue
        reach = float(c.half_width_m) + spec.mesh_outer_extra_m + spec.corridor_cell_size_m * 2.0
        pts = c.points
        for i in range(pts.shape[0] - 1):
            min_x = min(float(pts[i, 0]), float(pts[i + 1, 0])) - reach
            max_x = max(float(pts[i, 0]), float(pts[i + 1, 0])) + reach
            min_y = min(float(pts[i, 1]), float(pts[i + 1, 1])) - reach
            max_y = max(float(pts[i, 1]), float(pts[i + 1, 1])) + reach
            tx0 = max(0, math.floor((min_x - origin_x) / tile_m))
            tx1 = min(max_tx, math.floor((max_x - origin_x) / tile_m))
            ty0 = max(0, math.floor((min_y - origin_y) / tile_m))
            ty1 = min(max_ty, math.floor((max_y - origin_y) / tile_m))
            for ty in range(ty0, ty1 + 1):
                for tx in range(tx0, tx1 + 1):
                    active.add((tx, ty))

    return sorted(active)


def _filter_corridors_for_bbox(
    corridors: Sequence[RoadCorridor],
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
) -> list[RoadCorridor]:
    """Return corridors whose bounding box overlaps ``[min_x, max_x] x [min_y, max_y]``."""
    out: list[RoadCorridor] = []
    for c in corridors:
        if not c.grade_terrain or c.surface_z is None or c.points.shape[0] < 2:
            continue
        pad = max(float(c.corridor_half_width_m), float(c.half_width_m) + 15.0)
        xs = c.points[:, 0]
        ys = c.points[:, 1]
        if (
            float(xs.max()) + pad < min_x
            or float(xs.min()) - pad > max_x
            or float(ys.max()) + pad < min_y
            or float(ys.min()) - pad > max_y
        ):
            continue
        out.append(c)
    return out


def build_corridor_rtin_tile(
    tx: int,
    ty: int,
    *,
    un_depressed_build: TerrainBuild,
    all_solved_corridors: Sequence[RoadCorridor],
    corridor_roads_only: Sequence[RoadCorridor],
    spec: HybridSpec,
    bridge_spans: Sequence[tuple[NDArray[np.float64], float]] = (),
) -> CorridorTileMesh | None:
    """Build one high-resolution RTIN-simplified corridor ground mesh chunk."""
    t = spec.tile_squares
    cell = spec.corridor_cell_size_m
    tile_m = spec.tile_extent_m
    tile_ox = un_depressed_build.origin_x + tx * tile_m
    tile_oy = un_depressed_build.origin_y + ty * tile_m

    # Pad by 1 grid square on every side so central-difference normals and
    # boundary material changes match seamlessly across neighbouring chunks.
    ext_n = t + 3
    ext_ox = tile_ox - cell
    ext_oy = tile_oy - cell
    xs = ext_ox + np.arange(ext_n, dtype=np.float64) * cell
    ys = ext_oy + np.arange(ext_n, dtype=np.float64) * cell
    grid_x = np.broadcast_to(xs[None, :], (ext_n, ext_n))
    grid_y = np.broadcast_to(ys[:, None], (ext_n, ext_n))

    local_elev = _bilinear_sample_grid(
        un_depressed_build.elevations,
        un_depressed_build.origin_x,
        un_depressed_build.origin_y,
        un_depressed_build.cell_size_m,
        grid_x,
        grid_y,
    )
    local_layers = _nearest_sample_grid_u8(
        un_depressed_build.layers,
        un_depressed_build.origin_x,
        un_depressed_build.origin_y,
        un_depressed_build.cell_size_m,
        grid_x,
        grid_y,
    )

    pad_m = 20.0
    local_corridors = _filter_corridors_for_bbox(
        all_solved_corridors,
        ext_ox - pad_m,
        ext_oy - pad_m,
        ext_ox + (ext_n - 1) * cell + pad_m,
        ext_oy + (ext_n - 1) * cell + pad_m,
    )
    mat_index = {k: i for i, k in enumerate(un_depressed_build.materials)}
    if local_corridors:
        grade_corridors(
            local_elev,
            local_layers,
            mat_index,
            local_corridors,
            origin_x=ext_ox,
            origin_y=ext_oy,
            cell_size_m=cell,
            min_blend_skirt_m=15.0,
        )

    under_bridge = None
    if bridge_spans:
        under_bridge = _rasterize_bridge_under_mask(
            bridge_spans,
            origin_x=ext_ox,
            origin_y=ext_oy,
            grid_size=ext_n,
            cell_size_m=cell,
            end_margin_m=0.0,
        )

    clearance, _ = _clearance_from_corridors(
        corridor_roads_only,
        grid_ox=ext_ox,
        grid_oy=ext_oy,
        rows=ext_n,
        cols=ext_n,
        cell_size_m=cell,
        max_reach_extra_m=spec.mesh_outer_extra_m + cell * 2.0,
        exclude_mask=under_bridge,
    )

    # Interior (T+1, T+1) heights before outer feather (used for DecalRoad / prop Z)
    z_unfeathered = local_elev[1 : t + 2, 1 : t + 2].copy()
    clr_inner = clearance[1 : t + 2, 1 : t + 2]
    active_mask = clr_inner <= spec.depress_outer_extra_m

    # Determine kept squares (T+2, T+2) around the (T, T) tile so edge transitions force splits
    # A square (a, b) with SW corner at local index (a, b) in ext grid is kept if any corner
    # has clearance <= mesh_outer_extra_m and none of its corners are excluded by bridge mask.
    outer_limit = spec.mesh_outer_extra_m
    sq_c00 = clearance[:-1, :-1]
    sq_c10 = clearance[:-1, 1:]
    sq_c01 = clearance[1:, :-1]
    sq_c11 = clearance[1:, 1:]
    sq_min = np.minimum(np.minimum(sq_c00, sq_c10), np.minimum(sq_c01, sq_c11))
    sq_kept_ext = sq_min <= outer_limit
    if under_bridge is not None:
        ub_any = (
            under_bridge[:-1, :-1]
            | under_bridge[:-1, 1:]
            | under_bridge[1:, :-1]
            | under_bridge[1:, 1:]
        )
        sq_kept_ext &= ~ub_any

    # Tile's own (T, T) kept squares live at [1:T+1, 1:T+1] in sq_kept_ext
    keep_mask = sq_kept_ext[1 : t + 1, 1 : t + 1]
    if not keep_mask.any():
        return None

    # Apply smooth outer feather beyond depress_outer_extra_m so mesh dips cleanly underground
    feather_mask = np.isfinite(clearance) & (clearance > spec.depress_outer_extra_m)
    if feather_mask.any():
        frac = np.clip(
            (clearance[feather_mask] - spec.depress_outer_extra_m) / spec.feather_width_m,
            0.0,
            1.0,
        )
        local_elev[feather_mask] -= 0.5 * (1.0 - np.cos(math.pi * frac)) * spec.feather_depth_m

    # Square material kind (-1 = outside, >= 0 = material index)
    sq_kind_ext = np.where(sq_kept_ext, local_layers[:-1, :-1].astype(np.int16), np.int16(-1))
    # Never let HOLE (255) enter
    sq_kind_ext = np.where(sq_kind_ext == HOLE, np.int16(-1), sq_kind_ext)

    # Tile vertex heights (T+1, T+1) relative to tile_base_z
    tile_h_abs = local_elev[1 : t + 2, 1 : t + 2]
    tile_base_z = float(np.min(tile_h_abs))
    tile_h = tile_h_abs - tile_base_z

    # Force-keep tile boundaries and any vertex where the 4 incident squares differ in kind
    force = np.zeros((t + 1, t + 1), dtype=bool)
    force[0, :] = True
    force[-1, :] = True
    force[:, 0] = True
    force[:, -1] = True

    k00 = sq_kind_ext[:-1, :-1]
    k10 = sq_kind_ext[:-1, 1:]
    k01 = sq_kind_ext[1:, :-1]
    k11 = sq_kind_ext[1:, 1:]
    force |= (k00 != k10) | (k00 != k01) | (k00 != k11)

    tris, refine_passes = _refine_rtin_exact(tile_h, force, t, spec.rtin_max_error_m, keep_mask)
    if not tris:
        return None

    # Group kept triangles by square material kind
    tile_kind = sq_kind_ext[1 : t + 1, 1 : t + 1]
    per_kind: dict[int, list[tuple[int, int, int, int, int, int]]] = {}
    for tri in tris:
        ax, ay, cx, cy, bx, by = tri
        ca = (ax + cx + bx) // 3
        cb = (ay + cy + by) // 3
        if ca < 0 or ca >= t or cb < 0 or cb >= t:
            continue
        k = int(tile_kind[cb, ca])
        if k < 0:
            continue
        per_kind.setdefault(k, []).append(tri)

    if not per_kind:
        return None

    # Central-difference normals across the full 1 m height grid (matching across seams)
    dx_n = (local_elev[1 : t + 2, 2 : t + 3] - local_elev[1 : t + 2, 0 : t + 1]) / (2.0 * cell)
    dy_n = (local_elev[2 : t + 3, 1 : t + 2] - local_elev[0 : t + 1, 1 : t + 2]) / (2.0 * cell)
    inv_len = 1.0 / np.sqrt(dx_n * dx_n + dy_n * dy_n + 1.0)
    nx_grid = -dx_n * inv_len
    ny_grid = -dy_n * inv_len
    nz_grid = inv_len

    # Deduplicate used vertices for compact COLLADA output
    vert_map: dict[tuple[int, int], int] = {}
    vert_coords: list[tuple[int, int]] = []
    triangles_by_material: dict[str, NDArray[np.int32]] = {}
    total_tris = 0

    def _vid(vx: int, vy: int) -> int:
        key_v = (vx, vy)
        idx = vert_map.get(key_v)
        if idx is None:
            idx = len(vert_coords)
            vert_map[key_v] = idx
            vert_coords.append(key_v)
        return idx

    for mat_idx_val, tri_list in sorted(per_kind.items()):
        mat_key = un_depressed_build.materials[mat_idx_val]
        mat_name = f"ivanovo_corr_{mat_key}"
        arr = np.empty((len(tri_list), 3), dtype=np.int32)
        for ti, (ax, ay, cx, cy, bx, by) in enumerate(tri_list):
            arr[ti, 0] = _vid(ax, ay)
            arr[ti, 1] = _vid(cx, cy)
            arr[ti, 2] = _vid(bx, by)
        triangles_by_material[mat_name] = arr
        total_tris += len(tri_list)

    n_verts = len(vert_coords)
    vc = np.asarray(vert_coords, dtype=np.int32)
    vx_arr = vc[:, 0]
    vy_arr = vc[:, 1]

    positions = np.column_stack(
        (
            vx_arr.astype(np.float64) * cell,
            vy_arr.astype(np.float64) * cell,
            tile_h[vy_arr, vx_arr],
        )
    )
    normals = np.column_stack(
        (
            nx_grid[vy_arr, vx_arr],
            ny_grid[vy_arr, vx_arr],
            nz_grid[vy_arr, vx_arr],
        )
    )

    # Phase-locked world UVs matching 512 m TerrainBlock baseTex tiling
    uv_period = 512.0
    u0 = math.floor((tile_ox - un_depressed_build.origin_x) / uv_period) * uv_period
    v0 = math.floor((tile_oy - un_depressed_build.origin_y) / uv_period) * uv_period
    uvs = np.column_stack(
        (
            (tile_ox - un_depressed_build.origin_x + positions[:, 0] - u0) / uv_period,
            (tile_oy - un_depressed_build.origin_y + positions[:, 1] - v0) / uv_period,
        )
    )

    full_tris = 2 * int(keep_mask.sum())
    return CorridorTileMesh(
        tx=tx,
        ty=ty,
        key=f"{tx}_{ty}",
        origin_x=tile_ox,
        origin_y=tile_oy,
        base_z=tile_base_z,
        cell_size_m=cell,
        tile_squares=t,
        positions=positions,
        normals=normals,
        uvs=uvs,
        triangles_by_material=triangles_by_material,
        z_grid_unfeathered=z_unfeathered,
        active_mask=active_mask,
        triangle_count=total_tris,
        full_triangle_count=full_tris,
        refine_passes=refine_passes,
    )


def write_corridor_tile_dae(path: Path, tile: CorridorTileMesh) -> int:
    """Write a corridor chunk mesh to a BeamNG-compatible COLLADA 1.4.1 file.

    Uses ``ground_a`` as the mesh/node name (avoiding trailing digits that
    BeamNG's Torque3D importer parses as LOD pixel sizes).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use "ground_a" inside every DAE (no trailing digits!)
    mesh_name = "ground_a"

    pos_str = " ".join(f"{v:.4f}" for v in tile.positions.ravel())
    norm_str = " ".join(f"{v:.5f}" for v in tile.normals.ravel())
    uv_str = " ".join(f"{v:.6f}" for v in tile.uvs.ravel())

    ordered_mats = sorted(tile.triangles_by_material)
    effects_xml = "\n".join(
        f"""    <effect id="{escape(mat)}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse>
              <color>0.55 0.58 0.50 1</color>
            </diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>"""
        for mat in ordered_mats
    )
    materials_xml = "\n".join(
        f"""    <material id="{escape(mat)}" name="{escape(mat)}">
      <instance_effect url="#{escape(mat)}-effect"/>
    </material>"""
        for mat in ordered_mats
    )
    triangles_chunks: list[str] = []
    for mat in ordered_mats:
        idx = tile.triangles_by_material[mat]
        p_str = " ".join(f"{int(v)} {int(v)} {int(v)}" for v in idx.ravel())
        triangles_chunks.append(
            f"""        <triangles material="{escape(mat)}" count="{idx.shape[0]}">
          <input semantic="VERTEX" source="#{mesh_name}-mesh-vertices" offset="0"/>
          <input semantic="NORMAL" source="#{mesh_name}-mesh-normals" offset="1"/>
          <input semantic="TEXCOORD" source="#{mesh_name}-mesh-map-0" offset="2" set="0"/>
          <p>{p_str}</p>
        </triangles>"""
        )
    triangles_xml = "\n".join(triangles_chunks)
    bind_xml = "\n".join(
        f'              <instance_material symbol="{escape(mat)}" target="#{escape(mat)}"/>'
        for mat in ordered_mats
    )

    n_verts = tile.positions.shape[0]
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor>
      <author>ivanovo2beam</author>
      <authoring_tool>ivanovo2beam hybrid corridor RTIN generator</authoring_tool>
    </contributor>
    <created>{_FIXED_TIMESTAMP}</created>
    <modified>{_FIXED_TIMESTAMP}</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
{effects_xml}
  </library_effects>
  <library_materials>
{materials_xml}
  </library_materials>
  <library_geometries>
    <geometry id="{mesh_name}-mesh" name="{mesh_name}">
      <mesh>
        <source id="{mesh_name}-mesh-positions">
          <float_array id="{mesh_name}-mesh-positions-array" count="{n_verts * 3}">{pos_str}</float_array>
          <technique_common>
            <accessor source="#{mesh_name}-mesh-positions-array" count="{n_verts}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{mesh_name}-mesh-normals">
          <float_array id="{mesh_name}-mesh-normals-array" count="{n_verts * 3}">{norm_str}</float_array>
          <technique_common>
            <accessor source="#{mesh_name}-mesh-normals-array" count="{n_verts}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{mesh_name}-mesh-map-0">
          <float_array id="{mesh_name}-mesh-map-0-array" count="{n_verts * 2}">{uv_str}</float_array>
          <technique_common>
            <accessor source="#{mesh_name}-mesh-map-0-array" count="{n_verts}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{mesh_name}-mesh-vertices">
          <input semantic="POSITION" source="#{mesh_name}-mesh-positions"/>
        </vertices>
{triangles_xml}
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="base00" name="base00" type="NODE">
        <node id="start01" name="start01" type="NODE">
          <node id="{mesh_name}" name="{mesh_name}" type="NODE">
            <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
            <instance_geometry url="#{mesh_name}-mesh" name="{mesh_name}">
              <bind_material>
                <technique_common>
{bind_xml}
                </technique_common>
              </bind_material>
            </instance_geometry>
          </node>
        </node>
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
    tile.dae_path = path
    tile.dae_bytes = len(data)
    return len(data)


def write_corridor_materials(
    level_dir: Path,
    level_id: str,
    material_keys: Sequence[str] = DEFAULT_MATERIAL_ORDER,
    *,
    base_tex_size: int = DEFAULT_BASE_TEX_SIZE,
) -> Path:
    """Emit ``art/shapes/corridor/main.materials.json`` and sRGB ``.color.png`` base maps."""
    corr_dir = level_dir / "art" / "shapes" / "corridor"
    corr_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {}
    uv_period = 512.0

    for key in material_keys:
        spec = MATERIAL_LIBRARY[key]
        slug = spec.key.lower()
        tex_name = f"t_corr_base_{slug}_b.color.png"
        (corr_dir / tex_name).write_bytes(
            _encode_png(_tileable_color_patch(spec.base_color, spec.key, size=base_tex_size))
        )
        ch = spec.channel_paths()
        reps = round(uv_period / max(1.0, float(spec.detail_size)), 4)
        mat_name = f"ivanovo_corr_{spec.key}"
        doc[mat_name] = {
            "name": mat_name,
            "mapTo": mat_name,
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", mat_name),
            "annotation": _ANNOTATION_BY_MATERIAL.get(spec.key, "NATURE"),
            "groundType": spec.groundmodel,
            "Stages": [
                {
                    "baseColorMap": f"/levels/{level_id}/art/shapes/corridor/{tex_name}",
                    "detailMap": ch["b"],
                    "detailNormalMap": ch["nm"],
                    "detailNormalMapStrength": 0.75,
                    "detailScale": [reps, reps],
                    "roughnessFactor": 0.92,
                    "metallicFactor": 0.0,
                },
                {},
                {},
                {},
            ],
            "translucentBlendOp": "None",
            "version": 1.5,
        }

    mat_path = corr_dir / "main.materials.json"
    write_json(mat_path, doc)
    return mat_path


def apply_hybrid_decal_projection(
    tree: SceneTree,
    surface_index: HybridSurfaceIndex,
    *,
    group_path: Sequence[str] = ("level_objects", "road_network"),
) -> dict[str, int]:
    """Project corridor/junction DecalRoads onto the 1 m corridor meshes.

    Any ``DecalRoad`` (base surface, edge blend, or lane marking) with at least
    one node over an active corridor mesh gets ``overObjects=True``, its node
    Z coordinates resampled against the 1 m corridor surface, and ``decalBias``
    preserved/floored at ``>= 0.002`` on base surfaces so z-fighting cannot occur.
    """
    objs = tree.objects.get(tuple(group_path), [])
    projected = 0
    nodes_updated = 0
    for obj in objs:
        if obj.get("class") != "DecalRoad":
            continue
        nodes = obj.get("nodes")
        if not isinstance(nodes, list) or len(nodes) < 2:
            continue
        touches_corridor = False
        new_nodes: list[list[float]] = []
        for nd in nodes:
            x, y, z = float(nd[0]), float(nd[1]), float(nd[2])
            z_corr, on_corr = surface_index.sample_z(x, y, z)
            if on_corr:
                touches_corridor = True
                nodes_updated += 1
            new_nodes.append([round(x, 5), round(y, 5), round(z_corr, 5), *nd[3:]])
        if touches_corridor:
            obj["nodes"] = new_nodes
            obj["position"] = new_nodes[0][:3]
            obj["overObjects"] = True
            obj["decalBias"] = max(float(obj.get("decalBias", 0.0015)), 0.002)
            projected += 1
    return {"projected_decals": projected, "projected_nodes": nodes_updated}


def _snap_props_to_corridor(
    tree: SceneTree,
    surface_index: HybridSurfaceIndex,
    un_depressed_build: TerrainBuild,
    *,
    group_path: Sequence[str] = ("level_objects", "road_props"),
) -> int:
    """Adjust roadside TSStatic prop Z coordinates onto the 1 m corridor surface."""
    snapped = 0
    for obj in tree.objects.get(tuple(group_path), []):
        if obj.get("class") != "TSStatic":
            continue
        pos = obj.get("position")
        if not isinstance(pos, list) or len(pos) < 3:
            continue
        x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
        z_base = float(sample_points_z(np.array([[x, y]], dtype=np.float64), un_depressed_build)[0])
        z_corr, on_corr = surface_index.sample_z(x, y, z_base)
        if on_corr:
            delta = z_corr - z_base
            obj["position"] = [round(x, 4), round(y, 4), round(z + delta, 4)]
            snapped += 1
    return snapped


# ---------------------------------------------------------------------------
# Hybrid validation
# ---------------------------------------------------------------------------


def validate_hybrid_level(
    level_dir: Path,
    config: ProjectConfig | None = None,
    *,
    beamng_install: str | Path | None = None,
) -> ValidationReport:
    """Validate a built P1 hybrid terrain level directory on disk."""
    report = ValidationReport(level_dir=level_dir)
    if not level_dir.is_dir():
        report.add("level_dir", False, f"{level_dir} does not exist")
        return report

    for rel in (
        "info.json",
        "main/items.level.json",
        "main/MissionGroup/items.level.json",
        "art/terrains/main.materials.json",
        "theTerrain.ter",
        "theTerrain.terrain.json",
        "art/shapes/corridor/main.materials.json",
        "hybrid_experiment.json",
    ):
        p = level_dir / rel
        if not p.is_file():
            report.add("required_file", False, f"missing {rel}")
        elif p.stat().st_size == 0 and rel != "main/items.level.json":
            report.add("required_file", False, f"{rel} is empty")

    _check_scene_tree(report)
    _check_terrain(report)

    corr_mat_path = level_dir / "art" / "shapes" / "corridor" / "main.materials.json"
    corr_mats: dict[str, Any] = {}
    if corr_mat_path.is_file():
        try:
            corr_mats = json.loads(corr_mat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report.add("corridor_materials", False, f"unreadable corridor materials: {exc}")

    # Inspect corridor TSStatic objects and .dae meshes
    corridor_statics: list[dict[str, Any]] = []
    decal_roads: list[dict[str, Any]] = []
    for item_file in sorted((level_dir / "main").rglob("items.level.json")):
        for line in item_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("class") == "TSStatic" and str(obj.get("name", "")).startswith("corridor_"):
                corridor_statics.append(obj)
            elif obj.get("class") == "DecalRoad":
                decal_roads.append(obj)

    report.add(
        "corridor_statics",
        len(corridor_statics) > 0,
        f"{len(corridor_statics)} corridor TSStatic chunks placed",
    )

    bad_statics: list[str] = []
    bad_daes: list[str] = []
    level_prefix = f"/levels/{level_dir.name}/"
    for st in corridor_statics:
        name = str(st.get("name", ""))
        if st.get("collisionType") != "Visible Mesh Final" or st.get("decalType") != "Visible Mesh":
            bad_statics.append(
                f"{name}(col={st.get('collisionType')},dec={st.get('decalType')})"
            )
        shape = str(st.get("shapeName", ""))
        if not shape.startswith(level_prefix):
            bad_daes.append(f"{name}: bad shapeName {shape!r}")
            continue
        dae_file = level_dir / shape.removeprefix(level_prefix)
        if not dae_file.is_file():
            bad_daes.append(f"{name}: missing {dae_file.name}")
            continue
        txt = dae_file.read_text(encoding="utf-8", errors="ignore")
        if "<up_axis>Z_UP</up_axis>" not in txt or 'id="ground_a"' not in txt or "<triangles " not in txt:
            bad_daes.append(f"{dae_file.name}: invalid COLLADA structure")
        for m_name in corr_mats:
            # Verify any referenced material exists in corr_mats (checked below via regex-free scan)
            _ = m_name
        pos = 0
        while True:
            idx = txt.find('<triangles material="', pos)
            if idx < 0:
                break
            start_q = idx + len('<triangles material="')
            end_q = txt.find('"', start_q)
            mat_ref = txt[start_q:end_q]
            if mat_ref not in corr_mats:
                bad_daes.append(f"{dae_file.name}: undefined material {mat_ref!r}")
                break
            pos = end_q + 1

    report.add(
        "corridor_collision_flags",
        not bad_statics,
        "all corridor TSStatics use Visible Mesh Final + Visible Mesh"
        if not bad_statics
        else f"invalid flags: {bad_statics[:5]}",
    )
    report.add(
        "corridor_daes",
        not bad_daes,
        f"verified {len(corridor_statics)} corridor .dae meshes and materials"
        if not bad_daes
        else f"bad .dae(s): {bad_daes[:5]}",
    )

    over_obj_cnt = sum(1 for d in decal_roads if d.get("overObjects") is True)
    report.add(
        "corridor_decal_projection",
        over_obj_cnt > 0,
        f"{over_obj_cnt}/{len(decal_roads)} DecalRoads have overObjects=true",
    )

    # Verify base TerrainBlock depression vs hybrid manifest
    manifest_path = level_dir / "hybrid_experiment.json"
    ter_path = level_dir / "theTerrain.ter"
    if manifest_path.is_file() and ter_path.is_file():
        try:
            mf = json.loads(manifest_path.read_text(encoding="utf-8"))
            dep_cells = int(mf.get("depressed_cells", 0))
            report.add(
                "base_terrain_depression",
                dep_cells > 0,
                f"{dep_cells:,} base TerrainBlock vertices depressed under corridor footprint",
            )
        except (OSError, ValueError) as exc:
            report.add("base_terrain_depression", False, f"manifest error: {exc}")

    install = Path(beamng_install) if beamng_install else None
    _check_materials(report, install)
    if config is not None:
        _check_scale(report, config)
    return report


# ---------------------------------------------------------------------------
# End-to-end hybrid build
# ---------------------------------------------------------------------------


def build_hybrid_level(
    config: ProjectConfig,
    spec: HybridSpec,
    *,
    progress: Callable[[float, str], None] | None = None,
    make_zip: bool = False,
    osm_tile_km: float = 1.5,
    osm_budget_s: float = 240.0,
    max_decal_node_spacing_m: float = 40.0,
    building_simplify_m: float = 1.0,
    force_download: bool = False,
) -> HybridBuildResult:
    """Build an isolated P1 Hybrid Terrain experimental BeamNG level."""
    start = time.perf_counter()
    level = config.level
    if level is None:
        raise ValueError("config has no 'level' block; nothing to build")

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    durations: dict[str, float] = {}
    warnings: list[str] = []

    origin_x, origin_y = config.level_terrain_origin(level)
    extent = level.grid.extent_m
    margin_deg = 0.004
    corners_x = [origin_x, origin_x + extent]
    corners_y = [origin_y, origin_y + extent]
    lons, lats = config.projection.world_to_geo_array(
        [corners_x[0], corners_x[1], corners_x[0], corners_x[1]],
        [corners_y[0], corners_y[0], corners_y[1], corners_y[1]],
    )
    bbox = BBox(
        west=float(lons.min()) - margin_deg,
        south=float(lats.min()) - margin_deg,
        east=float(lons.max()) + margin_deg,
        north=float(lats.max()) + margin_deg,
    )
    report(
        0.01,
        f"hybrid {level.id}: base {level.grid.grid_size}@{level.grid.square_size_m}m "
        f"+ {spec.corridor_cell_size_m}m RTIN corridor meshes",
    )

    # -- DEM ---------------------------------------------------------------
    stage = time.perf_counter()
    report(0.03, "resolving DEM")
    dem_path = resolve_dem_for_bbox(
        bbox,
        config.cache_path("dem"),
        source=config.terrain_source,
        resolution_m=config.terrain_resolution_m,
        force=force_download,
    )
    durations["dem"] = time.perf_counter() - stage

    # -- OSM ---------------------------------------------------------------
    stage = time.perf_counter()
    report(0.06, "acquiring OSM")
    osm_data, osm_stats = fetch_level_osm(
        config,
        bbox,
        tile_km=osm_tile_km,
        budget_s=osm_budget_s,
        progress=progress,
        force=force_download,
    )
    durations["osm"] = time.perf_counter() - stage

    margin = ROAD_RENDER_MARGIN_M
    clip_bounds = (
        origin_x - margin,
        origin_y - margin,
        origin_x + extent + margin,
        origin_y + extent + margin,
    )
    water_clip = (origin_x, origin_y, origin_x + extent, origin_y + extent)

    # -- Features ----------------------------------------------------------
    stage = time.perf_counter()
    report(0.24, "extracting roads")
    roads = extract_roads(osm_data, config.projection, config.roads, clip_bounds=clip_bounds)
    road_stats = road_network_stats(roads)
    durations["roads"] = time.perf_counter() - stage

    stage = time.perf_counter()
    railways: list[Any] = []
    if config.railways_enabled:
        report(0.27, "extracting railways")
        railways = extract_railways(osm_data, config.projection, clip_bounds=clip_bounds)
    durations["railways"] = time.perf_counter() - stage

    stage = time.perf_counter()
    buildings: list[Any] = []
    if config.buildings_enabled:
        report(0.30, "extracting buildings")
        buildings, _ = extract_buildings(
            osm_data,
            config.projection,
            clip_bounds=clip_bounds,
            simplify_tolerance_m=building_simplify_m,
            max_buildings=spec.max_buildings,
        )
    durations["buildings_extract"] = time.perf_counter() - stage

    stage = time.perf_counter()
    vegetation_areas: list[Any] = []
    if config.vegetation_enabled:
        report(0.33, "extracting vegetation")
        vegetation_areas, _ = extract_vegetation(
            osm_data,
            config.projection,
            clip_bounds=clip_bounds,
        )
    durations["vegetation_extract"] = time.perf_counter() - stage

    # -- Base TerrainBuild (4096 @ 4.0m) -----------------------------------
    water_network = None
    bridge_roads = [r for r in roads if r.bridge and len(r.points) >= 2] if config.bridges_enabled else []
    corridor_highway_set = set(spec.corridor_highways)
    corridors = roads_to_corridors(roads, config.roads)
    corridor_road_keys = {
        f"way{r.osm_id}"
        for r in roads
        if r.drivable and not r.tunnel and not r.bridge and r.highway in corridor_highway_set
    }
    bridge_corridor_keys = {
        f"way{r.osm_id}"
        for r in bridge_roads
        if r.highway in corridor_highway_set
    }

    with DemReader(dem_path) as dem:
        w_stage = time.perf_counter()
        if config.water_enabled:
            report(0.36, "extracting hydrology")
            water_network = extract_water(
                osm_data,
                config.projection,
                dem=dem,
                clip_bounds=water_clip,
            )
        durations["water"] = time.perf_counter() - w_stage

        stage = time.perf_counter()
        report(0.40, f"building base {level.grid.grid_size}x{level.grid.grid_size} terrain")
        all_corridors: list[RoadCorridor] = list(corridors)
        if config.railways_enabled and railways:
            all_corridors.extend(railways_to_corridors(railways))

        landcover_polys: dict[str, list[Any]] = {}
        field_geoms = [v.to_polygon() for v in vegetation_areas if v.kind in {"scrub", "farmland"}]
        grass_geoms = [v.to_polygon() for v in vegetation_areas if v.kind == "meadow"]
        forest_geoms = [v.to_polygon() for v in vegetation_areas if v.kind == "forest"]
        if grass_geoms:
            landcover_polys["Grass"] = grass_geoms
        if field_geoms:
            landcover_polys["Field"] = field_geoms
        if forest_geoms:
            landcover_polys["ForestFloor"] = forest_geoms
        large_building_rings = [
            b.ring
            for b in buildings
            if getattr(b, "archetype", "") in {"industrial", "commercial", "apartment"}
        ]
        if large_building_rings:
            landcover_polys["Concrete"] = large_building_rings

        un_depressed_build = build_terrain(
            dem,
            config.projection,
            origin_x=origin_x,
            origin_y=origin_y,
            grid_size=level.grid.grid_size,
            cell_size_m=level.grid.square_size_m,
            corridors=all_corridors,
            material_order=DEFAULT_MATERIAL_ORDER,
            ground_margin_m=level.ground_margin_m,
            max_height_m=level.max_height_m,
            max_grade=config.roads.max_grade,
            water_network=water_network,
            forest_polygons=forest_geoms or None,
            landcover_polygons=landcover_polys or None,
            bridge_roads=bridge_roads or None,
            road_settings=config.roads,
            progress=lambda f, m: report(0.40 + 0.22 * f, m),
        )

        # Reconstruct coupled bridgehead ramps with exact solved bridge profiles so
        # the 1 m corridor meshes grade bridge approaches flush to the portals.
        bridge_profiles = getattr(un_depressed_build, "bridge_profiles", {}) or {}
        bridge_spans: list[tuple[NDArray[np.float64], float]] = []
        solved_corridors = [c for c in all_corridors if c.grade_terrain and c.surface_z is not None]
        corridor_only_list: list[RoadCorridor] = [
            c for c in solved_corridors if c.key in corridor_road_keys
        ]
        if bridge_profiles:
            def _dem_world(wx: float, wy: float) -> float:
                return float(sample_points_z(np.array([[wx, wy]], dtype=np.float64), un_depressed_build)[0])

            for br in bridge_roads:
                if br.osm_id not in bridge_profiles:
                    continue
                b_pts, b_z = bridge_profiles[br.osm_id]
                bridge_spans.append((b_pts, float(br.half_width_m) + 1.5))
                br_solved = replace(br, points=b_pts)
                ramps = create_bridgehead_ramps(
                    br_solved,
                    config.roads,
                    b_z,
                    ground_sampler=_dem_world,
                )
                solved_corridors.extend(ramps)
                if f"way{br.osm_id}" in bridge_corridor_keys:
                    corridor_only_list.extend(ramps)
    durations["base_terrain"] = time.perf_counter() - stage

    # -- Depress base TerrainBlock under highway corridor footprints -------
    stage = time.perf_counter()
    report(0.64, "depressing base TerrainBlock beneath corridor footprints")
    depressed_build, _, depressed_cells = depress_base_terrain(
        un_depressed_build,
        corridor_only_list,
        spec,
        bridge_spans=bridge_spans,
    )
    durations["depress_base"] = time.perf_counter() - stage

    # -- Generate 1 m RTIN corridor .dae chunks ----------------------------
    stage = time.perf_counter()
    report(0.66, "generating 1 m RTIN corridor .dae meshes")
    level_dir = config.build_path("levels", level.id)
    if level_dir.exists():
        shutil.rmtree(level_dir)
    corr_dir = level_dir / "art" / "shapes" / "corridor"
    corr_dir.mkdir(parents=True, exist_ok=True)

    active_tile_coords = _select_active_corridor_tiles(
        corridor_only_list,
        spec,
        origin_x=origin_x,
        origin_y=origin_y,
        total_extent_m=extent,
    )
    corridor_tiles: list[CorridorTileMesh] = []
    for idx_t, (tx, ty) in enumerate(active_tile_coords):
        if idx_t % 10 == 0:
            report(
                0.66 + 0.14 * (idx_t / max(1, len(active_tile_coords))),
                f"meshing corridor chunk {idx_t + 1}/{len(active_tile_coords)}",
            )
        tile = build_corridor_rtin_tile(
            tx,
            ty,
            un_depressed_build=un_depressed_build,
            all_solved_corridors=solved_corridors,
            corridor_roads_only=corridor_only_list,
            spec=spec,
            bridge_spans=bridge_spans,
        )
        if tile is None:
            continue
        dae_path = corr_dir / f"corridor_{tile.key}.dae"
        write_corridor_tile_dae(dae_path, tile)
        corridor_tiles.append(tile)

    write_corridor_materials(
        level_dir,
        level.id,
        DEFAULT_MATERIAL_ORDER,
        base_tex_size=spec.base_tex_size,
    )
    surface_index = HybridSurfaceIndex(
        tile_extent_m=spec.tile_extent_m,
        origin_x=origin_x,
        origin_y=origin_y,
        cell_size_m=spec.corridor_cell_size_m,
        tiles_by_coord={(t.tx, t.ty): t for t in corridor_tiles},
    )
    durations["corridor_meshes"] = time.perf_counter() - stage

    # -- Scene tree assembly -----------------------------------------------
    stage = time.perf_counter()
    report(0.82, "assembling hybrid scene")
    tree = SceneTree(level_id=level.id)
    ground_z = float(np.median(un_depressed_build.elevations))
    terrain_placement = TerrainPlacement(
        name="theTerrain",
        origin_x=origin_x,
        origin_y=origin_y,
        base_z=depressed_build.base_z,
        max_height=depressed_build.max_height,
        square_size=level.grid.square_size_m,
        grid_size=depressed_build.grid_size,
        terrain_file=f"/levels/{level.id}/theTerrain.ter",
        base_tex_size=spec.base_tex_size,
        material_texture_set=f"{level.id}TerrainMaterialTextureSet",
    )
    tree.add_group(("level_objects", "terrain"))
    tree.add_object(("level_objects", "terrain"), terrain_placement.to_object(level.id))

    tree.add_group(("level_objects", "corridor_meshes"))
    for tile in corridor_tiles:
        tile_name = f"corridor_{tile.key}"
        tree.add_object(
            ("level_objects", "corridor_meshes"),
            {
                "name": tile_name,
                "class": "TSStatic",
                "persistentId": stable_uuid(level.id, "TSStatic", tile_name),
                "__parent": "corridor_meshes",
                "position": [round(tile.origin_x, 4), round(tile.origin_y, 4), round(tile.base_z, 4)],
                "shapeName": f"/levels/{level.id}/art/shapes/corridor/corridor_{tile.key}.dae",
                "scale": [1.0, 1.0, 1.0],
                "collisionType": "Visible Mesh Final",
                "decalType": "Visible Mesh",
            },
        )

    build_sun_and_sky(
        tree,
        ("level_objects", "sky_and_sun"),
        latitude=level.center_lat,
        longitude=level.center_lon,
        elevation_m=ground_z,
        time_of_day=level.time_of_day,
        axis_tilt=level.axis_tilt,
        visible_distance_m=min(float(max(4000.0, extent * 2.5)), 12000.0),
        fog_density=0.00018,
    )

    # Roads + markings + edge_grass + overObjects projection onto corridor meshes
    ground_roads = [r for r in roads if not (r.bridge and r.osm_id in bridge_profiles)]
    export_stats = add_network_to_scene(
        tree,
        ground_roads,
        un_depressed_build,
        group_path=("level_objects", "road_network"),
        max_node_spacing_m=max_decal_node_spacing_m,
        include_props=False,
    )
    if bridge_profiles:
        for r in roads:
            if not (r.bridge and r.osm_id in bridge_profiles):
                continue
            b_pts, deck_z = bridge_profiles[r.osm_id]
            r_solved = replace(r, points=b_pts)
            b_objs = road_to_decal_objects(
                r_solved,
                level.id,
                ground_z=deck_z,
                terrain_build=None,
                max_node_spacing_m=max_decal_node_spacing_m,
                parent="road_network",
                start_end_fade=(0.0, 0.0),
            )
            for obj in b_objs:
                tree.add_object(("level_objects", "road_network"), obj)
                export_stats["exported"] += 1
                export_stats["nodes"] += len(obj["nodes"])  # type: ignore[arg-type]
            if _should_generate_markings(r_solved):
                for seg_idx, obj in enumerate(b_objs):
                    obj_nodes = np.asarray(obj["nodes"], dtype=np.float64)[:, :3]
                    base_k = f"way{r.osm_id}_{seg_idx}" if len(b_objs) > 1 else f"way{r.osm_id}"
                    for mark_obj in generate_lane_markings(
                        r_solved,
                        level.id,
                        obj_nodes,
                        is_start_junction=False,
                        is_end_junction=False,
                        parent_key=base_k,
                        parent="road_network",
                    ):
                        tree.add_object(("level_objects", "road_network"), mark_obj)
                        export_stats["exported"] += 1
                        export_stats["nodes"] += len(mark_obj["nodes"])  # type: ignore[arg-type]

    proj_stats = apply_hybrid_decal_projection(
        tree,
        surface_index,
        group_path=("level_objects", "road_network"),
    )
    export_stats.update(proj_stats)

    # Roadside props snapped onto 1 m corridor surface
    prop_stats = add_roadside_props_to_scene(
        tree,
        roads,
        un_depressed_build,
        level_dir=level_dir,
        osm_data=osm_data,
        projection=config.projection,
        carriageway_index=build_carriageway_index(roads),
    )
    prop_stats["snapped_to_corridor"] = _snap_props_to_corridor(
        tree,
        surface_index,
        un_depressed_build,
    )
    export_stats.update(prop_stats)

    # Water
    water_stats: dict[str, Any] = {"enabled": config.water_enabled}
    if water_network is not None:
        water_objects_count = export_water_to_scene(tree, water_network)
        water_stats.update(
            {
                "rivers": len(water_network.rivers),
                "water_bodies": len(water_network.water_bodies),
                "scene_objects": water_objects_count,
            }
        )

    # Bridges 3D
    bridge_stats: dict[str, Any] = {"enabled": config.bridges_enabled, "bridges": 0}
    bridges_dir = level_dir / "art" / "shapes" / "bridges"
    if config.bridges_enabled and bridge_roads:
        bridges_dir.mkdir(parents=True, exist_ok=True)

        def _bridge_ground_sampler(x: float, y: float) -> float:
            fb = float(sample_points_z(np.array([[x, y]], dtype=np.float64), un_depressed_build)[0])
            z, _ = surface_index.sample_z(x, y, fb)
            return z

        exported_bridges = 0
        for r in bridge_roads:
            if r.osm_id in bridge_profiles:
                b_pts, deck_z = bridge_profiles[r.osm_id]
                r_solved = replace(r, points=b_pts)
            else:
                gz = sample_surface_z(r, un_depressed_build)
                deck_z = compute_bridge_deck_profile(r, ground_z=gz, water_network=water_network)
                r_solved = r
            for obj in export_bridge_bundle(
                r_solved,
                deck_z,
                level.id,
                bridges_dir,
                ground_sampler=_bridge_ground_sampler,
                parent="bridges",
            ):
                tree.add_object(("level_objects", "bridges"), obj)
            exported_bridges += 1
        if exported_bridges > 0:
            emit_bridges_materials(level_dir, level.id)
        bridge_stats["bridges"] = exported_bridges

    # Railways
    if config.railways_enabled and railways:
        rw_objects, _ = export_railways(
            railways,
            level.id,
            level_dir,
            roads=roads,
            chunk_size_m=float(config.sector_size_m),
            batch_chunks=True,
            parent="railways",
        )
        for obj in rw_objects:
            tree.add_object(("level_objects", "railways"), obj)

    # Buildings
    building_stats: dict[str, Any] = {"enabled": config.buildings_enabled}
    if config.buildings_enabled and buildings:
        bld_bounds = (origin_x + 2.0, origin_y + 2.0, origin_x + extent - 2.0, origin_y + extent - 2.0)
        building_objects, building_export = export_buildings(
            buildings,
            un_depressed_build,
            level_id=level.id,
            level_dir=level_dir,
            max_instances=spec.max_buildings,
            batch_chunks=True,
            terrain_bounds=bld_bounds,
        )
        for obj in building_objects:
            tree.add_object(("level_objects", "buildings"), obj)
        building_stats.update(building_export.as_dict())

    # Vegetation (.forest4.json + 8-layer GroundCover)
    veg_stats: dict[str, Any] = {"enabled": config.vegetation_enabled}
    if config.vegetation_enabled and vegetation_areas:
        water_items: list[Any] = []
        if water_network is not None:
            water_items.extend(water_network.rivers)
            water_items.extend(water_network.water_bodies)
        veg_objects, v_exp = export_vegetation(
            vegetation_areas,
            un_depressed_build,
            level_id=level.id,
            level_dir=level_dir,
            roads=roads,
            buildings=buildings,
            water=water_items,
            railways=railways,
            max_trees=spec.max_trees,
            include_groundcover=spec.groundcover_enabled,
            terrain_materials=depressed_build.materials,
        )
        for obj in veg_objects:
            tree.add_object(("level_objects", "vegetation"), obj)
        veg_stats.update(v_exp.as_dict())

    # Spawn points
    spawn_lon = level.spawn_lon if level.spawn_lon is not None else level.center_lon
    spawn_lat = level.spawn_lat if level.spawn_lat is not None else level.center_lat
    spawn_presets: list[dict[str, Any]] = [
        {
            "key": "player_spawn",
            "name": "spawn_default",
            "title": level.title,
            "geo": (spawn_lon, spawn_lat),
        }
    ]
    for idx_s, extra in enumerate(spec.extra_spawns, start=1):
        geo = extra.get("geo")
        if isinstance(geo, (list, tuple)) and len(geo) == 2:
            spawn_presets.append(
                {
                    "key": str(extra.get("key", f"spawn_extra_{idx_s}")),
                    "name": str(extra.get("name", f"spawn_extra_{idx_s}")),
                    "title": str(extra.get("title", f"Spawn {idx_s}")),
                    "geo": (float(geo[0]), float(geo[1])),
                }
            )

    spawn_points_info: list[dict[str, Any]] = []
    for sp in spawn_presets:
        lon, lat = sp["geo"]
        ax, ay = config.projection.geo_to_world(lon, lat)
        spawn = _choose_spawn(roads, un_depressed_build, anchor=(ax, ay))
        z_sp, _ = surface_index.sample_z(spawn[0], spawn[1], spawn[2])
        add_spawn_point(
            tree,
            ("PlayerDropPoints",),
            key=sp["key"],
            name=sp["name"],
            x=spawn[0],
            y=spawn[1],
            z=z_sp + 0.35,
            radius=5.0,
            heading_rad=spawn[4] if len(spawn) > 4 else 0.0,
        )
        spawn_points_info.append(
            {
                "objectname": sp["name"],
                "translationId": sp["title"],
                "preview": f"{level.id}_preview.png",
            }
        )

    # Write scene & terrain files
    files = tree.render()
    info = build_info_json(
        level.id,
        level.title,
        (
            f"P1 Hybrid Terrain: {level.grid.grid_size}x{level.grid.grid_size} @ "
            f"{level.grid.square_size_m} m depressed base TerrainBlock + "
            f"{len(corridor_tiles)} RTIN corridor .dae chunks ({spec.corridor_cell_size_m} m, "
            f"maxError={spec.rtin_max_error_m} m)."
        ),
        extent_m=extent,
        default_spawn_point_name="spawn_default",
        spawn_points=spawn_points_info,
    )
    files["info.json"] = json.dumps(info, indent=2, ensure_ascii=False) + "\n"
    files["map.json"] = json.dumps({"segments": {}}, indent=2) + "\n"
    files["main.decals.json"] = (
        json.dumps(
            {
                "header": {
                    "name": "DecalData File",
                    "comments": "// Instances format: rectIdx, size, renderPriority, position.x, position.y, position.z, normal.x, normal.y, normal.z, tangent.x, tangent.y, tangent.z, uid",
                    "version": 2,
                },
                "instances": {},
            },
            indent=2,
        )
        + "\n"
    )
    write_level_files(level_dir, files)

    write_json(
        level_dir / "theTerrain.terrain.json",
        build_terrain_json(
            level.id,
            "theTerrain",
            size=depressed_build.grid_size,
            materials=depressed_build.materials,
        ),
    )
    write_terrain_base_textures(level_dir, DEFAULT_MATERIAL_ORDER, size=spec.base_tex_size)
    write_json(
        level_dir / "art" / "terrains" / "main.materials.json",
        build_materials_json(level.id, DEFAULT_MATERIAL_ORDER, base_tex_size=spec.base_tex_size),
    )
    write_road_materials(level_dir, level.id)
    terrain_bytes = write_ter(level_dir / "theTerrain.ter", depressed_build.to_terrain_file())

    generate_level_preview_png(level_dir, level.id)
    try:
        manifest = build_attribution_manifest(level.id, dem_sources=[dem_path])
        write_attribution_files(manifest, level_dir=level_dir, build_dir=config.build_dir)
    except Exception as exc:
        warnings.append(f"attribution manifest generation failed: {exc}")

    total_tris = sum(t.triangle_count for t in corridor_tiles)
    total_full_tris = sum(t.full_triangle_count for t in corridor_tiles)
    total_dae_bytes = sum(t.dae_bytes for t in corridor_tiles)
    hybrid_manifest = {
        "level_id": level.id,
        "base_grid_size": level.grid.grid_size,
        "base_square_size_m": level.grid.square_size_m,
        "origin_world": [origin_x, origin_y],
        "extent_m": extent,
        "corridor_cell_size_m": spec.corridor_cell_size_m,
        "tile_squares": spec.tile_squares,
        "rtin_max_error_m": spec.rtin_max_error_m,
        "depress_depth_m": spec.depress_depth_m,
        "depress_inner_extra_m": spec.depress_inner_extra_m,
        "depress_outer_extra_m": spec.depress_outer_extra_m,
        "feather_width_m": spec.feather_width_m,
        "feather_depth_m": spec.feather_depth_m,
        "depressed_cells": depressed_cells,
        "corridor_chunks": len(corridor_tiles),
        "corridor_triangles": total_tris,
        "corridor_full_triangles": total_full_tris,
        "corridor_dae_bytes": total_dae_bytes,
        "projected_decals": export_stats.get("projected_decals", 0),
    }
    write_json(level_dir / "hybrid_experiment.json", hybrid_manifest)
    durations["scene"] = time.perf_counter() - stage

    stage = time.perf_counter()
    mod_zip: Path | None = None
    if make_zip:
        report(0.95, "packaging mod zip")
        mod_zip = package_level(config.build_dir, level.id)
    durations["package"] = time.perf_counter() - stage

    stats: dict[str, Any] = {
        "level_id": level.id,
        "base_grid_size": level.grid.grid_size,
        "base_square_size_m": level.grid.square_size_m,
        "extent_m": extent,
        "origin_world": [origin_x, origin_y],
        "terrain_bytes": terrain_bytes,
        "depressed_cells": depressed_cells,
        "corridor_chunks": len(corridor_tiles),
        "corridor_triangles": total_tris,
        "corridor_full_triangles": total_full_tris,
        "corridor_dae_bytes": total_dae_bytes,
        "roads": road_stats,
        "export": export_stats,
        "water": water_stats,
        "bridges": bridge_stats,
        "buildings": building_stats,
        "vegetation": veg_stats,
        "osm": osm_stats,
        "asset_references": len(referenced_asset_paths(DEFAULT_MATERIAL_ORDER)),
        "total_seconds": time.perf_counter() - start,
    }
    report(1.0, "hybrid build complete")
    return HybridBuildResult(
        level_id=level.id,
        level_dir=level_dir,
        mod_zip=mod_zip,
        corridor_tiles=corridor_tiles,
        stats=stats,
        durations=durations,
        warnings=warnings,
    )
