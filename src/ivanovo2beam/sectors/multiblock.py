"""Multi-TerrainBlock experimental pipeline for BeamNG scaling evaluation (P2).

Builds a BeamNG level composed of multiple adjacent ``TerrainBlock`` tiles
sliced from a single continuous graded terrain domain so shared border rows
and columns carry bit-for-bit identical quantized heights and material indices.
This lets us empirically test BeamNG's cross-block behavior for:

- Terrain seam geometry, materials, and vertex normals
- Continuous vs. split ``DecalRoad`` raycasting and driving physics
- ``GroundCover`` terrain-binding behavior across multiple ``TerrainBlock``s
- ``Forest`` (``.forest4.json``), ``River``, ``WaterBlock``, and ``TSStatic``
  continuity across tile boundaries
"""

from __future__ import annotations

import json
import math
import shutil
import time
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    build_materials_json,
    referenced_asset_paths,
    write_terrain_base_textures,
)
from ivanovo2beam.beamng.preview import generate_level_preview_png
from ivanovo2beam.beamng.ter import (
    HOLE,
    MAX_SIZE,
    MIN_SIZE,
    TerrainFile,
    TerrainFormatError,
    read_ter,
    write_ter,
)
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
    emit_bridges_materials,
    export_bridge_bundle,
)
from ivanovo2beam.roads.build import (
    _should_generate_markings,
    add_network_to_scene,
    generate_lane_markings,
    road_to_decal_objects,
    roads_to_corridors,
    sample_points_z,
    sample_surface_z,
)
from ivanovo2beam.roads.materials import write_road_materials
from ivanovo2beam.roads.network import extract_roads, road_network_stats
from ivanovo2beam.roads.railways import (
    export_railways,
    extract_railways,
    railways_to_corridors,
)
from ivanovo2beam.terrain.builder import TerrainBuild, build_terrain
from ivanovo2beam.terrain.dem import DemFetchError, DemReader, fetch_dem
from ivanovo2beam.validation.level import ValidationReport, _check_materials, _check_scale, _check_scene_tree
from ivanovo2beam.vegetation.extract import extract_vegetation
from ivanovo2beam.vegetation.forest import export_vegetation
from ivanovo2beam.water.export import export_water_to_scene
from ivanovo2beam.water.extract import extract_water

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from numpy.typing import NDArray


_VALID_ROAD_SEAM_MODES = frozenset({"continuous", "split"})


@dataclass(frozen=True, slots=True)
class MultiBlockSpec:
    """Configuration of a multi-``TerrainBlock`` grid layout.

    Attributes:
        tile_grid_size: Vertices per side of each ``TerrainBlock`` (power of 2
            in ``[128, 8192]``).
        square_size_m: Ground spacing between adjacent vertices, in metres.
        cols: Number of terrain blocks along world X (west to east).
        rows: Number of terrain blocks along world Y (south to north).
        origin_world: Optional explicit ``(x, y)`` SW corner of block ``(0, 0)``.
            When ``None``, the combined footprint is centred on ``level.center``.
        base_tex_size: Terrain blend-map edge length in texels per block.
        road_seam_mode: ``"continuous"`` leaves ``DecalRoad`` objects intact
            across block boundaries; ``"split"`` clips road polylines at seams.
        groundcover_enabled: Whether to emit experimental ``GroundCover`` nodes
            for testing multi-block vegetation layer binding.
        max_buildings: Optional cap on exported buildings.
        extra_spawns: Optional list of ``{"name", "title", "geo": [lon, lat]}``
            additional spawn presets near seam crossings.
    """

    tile_grid_size: int = 2048
    square_size_m: float = 1.0
    cols: int = 2
    rows: int = 1
    origin_world: tuple[float, float] | None = None
    base_tex_size: int = DEFAULT_BASE_TEX_SIZE
    road_seam_mode: str = "continuous"
    groundcover_enabled: bool = True
    max_buildings: int | None = 400
    extra_spawns: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tile_grid_size, int) or isinstance(self.tile_grid_size, bool):
            raise ConfigError(f"tile_grid_size must be an int, got {type(self.tile_grid_size).__name__}")
        if not (MIN_SIZE <= self.tile_grid_size <= MAX_SIZE) or (self.tile_grid_size & (self.tile_grid_size - 1)):
            raise ConfigError(
                f"tile_grid_size {self.tile_grid_size} must be a power of two in [{MIN_SIZE}, {MAX_SIZE}]"
            )
        if not math.isfinite(self.square_size_m) or self.square_size_m <= 0.0:
            raise ConfigError(f"square_size_m must be positive, got {self.square_size_m!r}")
        if self.cols < 1 or self.rows < 1:
            raise ConfigError(f"cols and rows must be >= 1, got ({self.cols}, {self.rows})")
        if self.road_seam_mode not in _VALID_ROAD_SEAM_MODES:
            raise ConfigError(
                f"road_seam_mode must be one of {sorted(_VALID_ROAD_SEAM_MODES)}, got {self.road_seam_mode!r}"
            )
        if self.parent_grid_size > MAX_SIZE:
            raise ConfigError(
                f"combined grid ({self.combined_cols}x{self.combined_rows}) requires parent power-of-two "
                f"{self.parent_grid_size} > BeamNG MAX_SIZE ({MAX_SIZE})"
            )

    @property
    def tile_extent_m(self) -> float:
        """Metric span of a single ``TerrainBlock`` along one axis."""
        return (self.tile_grid_size - 1) * self.square_size_m

    @property
    def total_width_m(self) -> float:
        """Total east-west metric extent across all columns."""
        return self.cols * self.tile_extent_m

    @property
    def total_height_m(self) -> float:
        """Total north-south metric extent across all rows."""
        return self.rows * self.tile_extent_m

    @property
    def combined_cols(self) -> int:
        """Total unique vertices along X (adjacent blocks share 1 seam column)."""
        return self.cols * (self.tile_grid_size - 1) + 1

    @property
    def combined_rows(self) -> int:
        """Total unique vertices along Y (adjacent blocks share 1 seam row)."""
        return self.rows * (self.tile_grid_size - 1) + 1

    @property
    def parent_grid_size(self) -> int:
        """Smallest power-of-two square grid covering the combined tile domain."""
        needed = max(self.combined_cols, self.combined_rows)
        size = MIN_SIZE
        while size < needed:
            size *= 2
        return size

    @classmethod
    def from_yaml(cls, path: str | Path) -> MultiBlockSpec:
        """Load the ``multiblock`` section from a YAML config file."""
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a top-level mapping")
        mb = raw.get("multiblock") or {}
        if not isinstance(mb, dict):
            raise ConfigError(f"{path}: 'multiblock' must be a mapping")

        level_terrain = (raw.get("level") or {}).get("terrain") or {}
        tile_grid_size = int(mb.get("tile_grid_size", level_terrain.get("grid_size", 2048)))
        square_size_m = float(mb.get("square_size_m", level_terrain.get("square_size_m", 1.0)))
        origin_raw = mb.get("origin_world")
        origin_world = (
            (float(origin_raw[0]), float(origin_raw[1]))
            if isinstance(origin_raw, (list, tuple)) and len(origin_raw) == 2
            else None
        )
        max_b_raw = mb.get("max_buildings", 400)
        extra_spawns = tuple(dict(s) for s in (mb.get("extra_spawns") or ()))
        return cls(
            tile_grid_size=tile_grid_size,
            square_size_m=square_size_m,
            cols=int(mb.get("cols", 2)),
            rows=int(mb.get("rows", 1)),
            origin_world=origin_world,
            base_tex_size=int(mb.get("base_tex_size", DEFAULT_BASE_TEX_SIZE)),
            road_seam_mode=str(mb.get("road_seam_mode", "continuous")),
            groundcover_enabled=bool(mb.get("groundcover_enabled", True)),
            max_buildings=None if max_b_raw is None else int(max_b_raw),
            extra_spawns=extra_spawns,
        )


@dataclass(slots=True)
class TerrainTileSlice:
    """One ``TerrainBlock`` tile sliced from a shared parent terrain build."""

    ix: int
    iy: int
    name: str
    origin_x: float
    origin_y: float
    extent_m: float
    elevations: NDArray[np.float64]
    layers: NDArray[np.uint8]
    terrain_file: TerrainFile
    placement: TerrainPlacement


@dataclass(slots=True)
class SeamReport:
    """Verification report for shared borders between adjacent ``TerrainBlock``s."""

    seams_checked: int
    seam_vertices_checked: int
    max_elevation_diff_m: float
    max_quantized_diff: int
    layer_mismatches: int
    seam_x_positions: list[float]
    seam_y_positions: list[float]
    roads_crossing_seam: int = 0
    rivers_crossing_seam: int = 0
    forests_crossing_seam: int = 0
    buildings_crossing_seam: int = 0

    @property
    def all_seams_exact(self) -> bool:
        """True when all shared vertices match bit-for-bit in height and layer."""
        return self.seams_checked > 0 and self.max_quantized_diff == 0 and self.layer_mismatches == 0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary."""
        return {
            "seams_checked": self.seams_checked,
            "seam_vertices_checked": self.seam_vertices_checked,
            "max_elevation_diff_m": self.max_elevation_diff_m,
            "max_quantized_diff": self.max_quantized_diff,
            "layer_mismatches": self.layer_mismatches,
            "all_seams_exact": self.all_seams_exact,
            "seam_x_positions": self.seam_x_positions,
            "seam_y_positions": self.seam_y_positions,
            "roads_crossing_seam": self.roads_crossing_seam,
            "rivers_crossing_seam": self.rivers_crossing_seam,
            "forests_crossing_seam": self.forests_crossing_seam,
            "buildings_crossing_seam": self.buildings_crossing_seam,
        }


@dataclass(slots=True)
class MultiBlockBuildResult:
    """Outcome of a multi-``TerrainBlock`` experimental build."""

    level_id: str
    level_dir: Path
    mod_zip: Path | None
    tiles: list[TerrainTileSlice]
    seam_report: SeamReport
    stats: dict[str, Any] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Return a concise multi-line build summary."""
        lines = [
            f"level {self.level_id} ({len(self.tiles)} TerrainBlocks)",
            f"  output: {self.level_dir}",
            (
                f"  seams: {self.seam_report.seams_checked} checked, "
                f"{self.seam_report.seam_vertices_checked} shared vertices, "
                f"max_dz={self.seam_report.max_elevation_diff_m:.6f} m, "
                f"u16_diff={self.seam_report.max_quantized_diff}, "
                f"layer_mismatches={self.seam_report.layer_mismatches}"
            ),
            (
                f"  crossings: roads={self.seam_report.roads_crossing_seam}, "
                f"rivers={self.seam_report.rivers_crossing_seam}, "
                f"forests={self.seam_report.forests_crossing_seam}, "
                f"buildings={self.seam_report.buildings_crossing_seam}"
            ),
        ]
        if self.mod_zip:
            lines.append(f"  mod zip: {self.mod_zip}")
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layout, slicing, and seam verification
# ---------------------------------------------------------------------------


def resolve_multiblock_origin(config: ProjectConfig, spec: MultiBlockSpec) -> tuple[float, float]:
    """Return the SW world origin ``(x, y)`` of block ``(0, 0)``."""
    if spec.origin_world is not None:
        return spec.origin_world
    cx, cy = config.level_grid_center()
    return (cx - 0.5 * spec.total_width_m, cy - 0.5 * spec.total_height_m)


def plan_tile_layout(
    spec: MultiBlockSpec,
    origin_x: float,
    origin_y: float,
) -> list[tuple[int, int, str, float, float]]:
    """Compute ``(ix, iy, block_name, block_ox, block_oy)`` for every tile.

    Adjacent tiles are spaced by ``(tile_grid_size - 1) * square_size_m`` so
    the last column/row of one block coincides identically in world metres with
    the first column/row of its neighbour.
    """
    step = spec.tile_extent_m
    layout: list[tuple[int, int, str, float, float]] = []
    for iy in range(spec.rows):
        for ix in range(spec.cols):
            name = f"theTerrain_{ix}_{iy}"
            layout.append((ix, iy, name, origin_x + ix * step, origin_y + iy * step))
    return layout


def slice_terrain_tiles(
    parent_build: TerrainBuild,
    spec: MultiBlockSpec,
    *,
    level_id: str,
) -> tuple[list[TerrainTileSlice], float, float]:
    """Slice a continuous ``TerrainBuild`` into seam-aligned ``TerrainBlock`` tiles.

    All tiles share the exact union ``(base_z, max_height)`` vertical range so
    quantisation onto ``uint16`` produces bit-for-bit identical values on every
    shared border row and column.
    """
    n = spec.tile_grid_size
    if parent_build.grid_size < max(spec.combined_cols, spec.combined_rows):
        raise ValueError(
            f"parent grid_size {parent_build.grid_size} is smaller than required "
            f"{spec.combined_cols}x{spec.combined_rows}"
        )
    if not math.isclose(parent_build.cell_size_m, spec.square_size_m, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(
            f"parent cell_size_m ({parent_build.cell_size_m}) != spec.square_size_m ({spec.square_size_m})"
        )

    union_elev = parent_build.elevations[: spec.combined_rows, : spec.combined_cols]
    base_z = parent_build.base_z
    max_height = parent_build.max_height
    if not np.isfinite(union_elev).all():
        raise ValueError("parent elevations contain non-finite values inside multiblock footprint")

    layout = plan_tile_layout(spec, parent_build.origin_x, parent_build.origin_y)
    tiles: list[TerrainTileSlice] = []
    stride = n - 1
    for ix, iy, name, tile_ox, tile_oy in layout:
        r0 = iy * stride
        c0 = ix * stride
        tile_elev = np.ascontiguousarray(parent_build.elevations[r0 : r0 + n, c0 : c0 + n], dtype=np.float64)
        tile_layers = np.ascontiguousarray(parent_build.layers[r0 : r0 + n, c0 : c0 + n], dtype=np.uint8)
        ter_file = TerrainFile.from_elevations(
            tile_elev,
            tile_layers,
            parent_build.materials,
            base_z=base_z,
            max_height=max_height,
        )
        placement = TerrainPlacement(
            name=name,
            origin_x=tile_ox,
            origin_y=tile_oy,
            base_z=base_z,
            max_height=max_height,
            square_size=spec.square_size_m,
            grid_size=n,
            terrain_file=f"/levels/{level_id}/{name}.ter",
            base_tex_size=spec.base_tex_size,
            material_texture_set=f"{level_id}TerrainMaterialTextureSet",
        )
        tiles.append(
            TerrainTileSlice(
                ix=ix,
                iy=iy,
                name=name,
                origin_x=tile_ox,
                origin_y=tile_oy,
                extent_m=spec.tile_extent_m,
                elevations=tile_elev,
                layers=tile_layers,
                terrain_file=ter_file,
                placement=placement,
            )
        )
    return tiles, base_z, max_height


def verify_tile_seams(tiles: Sequence[TerrainTileSlice]) -> SeamReport:
    """Verify exact height and material alignment across all adjacent block borders."""
    by_coord = {(t.ix, t.iy): t for t in tiles}
    seams_checked = 0
    seam_vertices = 0
    max_elev_diff = 0.0
    max_u16_diff = 0
    layer_mismatches = 0
    seam_xs: list[float] = []
    seam_ys: list[float] = []

    for (ix, iy), left in sorted(by_coord.items()):
        right = by_coord.get((ix + 1, iy))
        if right is not None:
            seams_checked += 1
            seam_x = left.origin_x + left.extent_m
            if seam_x not in seam_xs:
                seam_xs.append(seam_x)
            if not math.isclose(right.origin_x, seam_x, abs_tol=1e-6):
                raise ValueError(
                    f"horizontal seam misalignment: {left.name} ends at {seam_x}, "
                    f"{right.name} starts at {right.origin_x}"
                )
            dz = np.abs(left.elevations[:, -1] - right.elevations[:, 0])
            du16 = np.abs(
                left.terrain_file.heights[:, -1].astype(np.int32)
                - right.terrain_file.heights[:, 0].astype(np.int32)
            )
            l_diff = int(np.count_nonzero(left.terrain_file.layers[:, -1] != right.terrain_file.layers[:, 0]))
            seam_vertices += int(left.elevations.shape[0])
            max_elev_diff = max(max_elev_diff, float(dz.max()))
            max_u16_diff = max(max_u16_diff, int(du16.max()))
            layer_mismatches += l_diff

        top = by_coord.get((ix, iy + 1))
        if top is not None:
            seams_checked += 1
            seam_y = left.origin_y + left.extent_m
            if seam_y not in seam_ys:
                seam_ys.append(seam_y)
            if not math.isclose(top.origin_y, seam_y, abs_tol=1e-6):
                raise ValueError(
                    f"vertical seam misalignment: {left.name} ends at {seam_y}, "
                    f"{top.name} starts at {top.origin_y}"
                )
            dz = np.abs(left.elevations[-1, :] - top.elevations[0, :])
            du16 = np.abs(
                left.terrain_file.heights[-1, :].astype(np.int32)
                - top.terrain_file.heights[0, :].astype(np.int32)
            )
            l_diff = int(np.count_nonzero(left.terrain_file.layers[-1, :] != top.terrain_file.layers[0, :]))
            seam_vertices += int(left.elevations.shape[1])
            max_elev_diff = max(max_elev_diff, float(dz.max()))
            max_u16_diff = max(max_u16_diff, int(du16.max()))
            layer_mismatches += l_diff

    return SeamReport(
        seams_checked=seams_checked,
        seam_vertices_checked=seam_vertices,
        max_elevation_diff_m=max_elev_diff,
        max_quantized_diff=max_u16_diff,
        layer_mismatches=layer_mismatches,
        seam_x_positions=sorted(seam_xs),
        seam_y_positions=sorted(seam_ys),
    )


# ---------------------------------------------------------------------------
# Road seam splitting (optional A/B test helper)
# ---------------------------------------------------------------------------


def split_polyline_at_seams(
    pts: NDArray[np.float64],
    seam_xs: Sequence[float],
    seam_ys: Sequence[float],
) -> list[NDArray[np.float64]]:
    """Split a polyline at exact vertical (``x``) and horizontal (``y``) seam lines.

    Each crossing inserts an interpolated seam vertex shared by both halves so
    adjacent ``DecalRoad`` pieces meet cleanly at ``startEndFade = (0, 0)``.
    """
    arr = np.asarray(pts, dtype=np.float64)
    if arr.shape[0] < 2:
        return [arr]

    pieces: list[NDArray[np.float64]] = [arr]
    for axis, thresholds in ((0, seam_xs), (1, seam_ys)):
        for thresh in thresholds:
            next_pieces: list[NDArray[np.float64]] = []
            for piece in pieces:
                current: list[NDArray[np.float64]] = [piece[0]]
                for i in range(piece.shape[0] - 1):
                    p0 = piece[i]
                    p1 = piece[i + 1]
                    v0 = float(p0[axis])
                    v1 = float(p1[axis])
                    if (v0 < thresh < v1) or (v1 < thresh < v0):
                        t = (thresh - v0) / (v1 - v0)
                        cross = p0 + t * (p1 - p0)
                        cross[axis] = thresh
                        current.append(cross)
                        next_pieces.append(np.vstack(current))
                        current = [cross.copy(), p1]
                    elif math.isclose(v1, thresh, abs_tol=1e-6) and i + 1 < piece.shape[0] - 1:
                        current.append(p1)
                        next_pieces.append(np.vstack(current))
                        current = [p1.copy()]
                    else:
                        current.append(p1)
                if len(current) >= 2:
                    next_pieces.append(np.vstack(current))
            pieces = next_pieces
    return [p for p in pieces if p.shape[0] >= 2]


def _split_roads_at_seams(
    roads: Sequence[Any],
    seam_xs: Sequence[float],
    seam_ys: Sequence[float],
) -> list[Any]:
    """Return roads split at seam lines, assigning unique synthetic negative IDs to extra segments."""
    out: list[Any] = []
    next_synth_id = -9_000_000_000
    for r in roads:
        pieces = split_polyline_at_seams(r.points, seam_xs, seam_ys)
        if len(pieces) <= 1:
            out.append(r)
            continue
        for idx, piece in enumerate(pieces):
            diffs = np.diff(piece, axis=0)
            length_m = float(np.hypot(diffs[:, 0], diffs[:, 1]).sum())
            if length_m < 0.5:
                continue
            oid = r.osm_id if idx == 0 else next_synth_id
            if idx > 0:
                next_synth_id -= 1
            out.append(replace(r, osm_id=oid, points=piece))
    return out


# ---------------------------------------------------------------------------
# Experimental GroundCover (zero-copy stock grass textures)
# ---------------------------------------------------------------------------


def build_experiment_groundcover(
    level_id: str,
    *,
    center_x: float,
    center_y: float,
    center_z: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build lightweight ``GroundCover`` test objects and materials.

    Used to verify empirically in BeamNG.drive whether ``GroundCover`` renders
    across both ``TerrainBlock``s or binds only to the first ``TerrainBlock``.
    """
    mat_long = "exp_gc_grass_long"
    mat_short = "exp_gc_grass_short"
    materials = {
        mat_long: {
            "name": mat_long,
            "mapTo": mat_long,
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", mat_long),
            "Stages": [
                {
                    "ambientOcclusionMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_ao.data.dds",
                    "baseColorMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_b.color.dds",
                    "normalMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_nm.normal.dds",
                    "opacityMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_o.data.dds",
                    "roughnessMap": "/assets/materials/foliage/grass/t_grass_green_long_01/t_grass_green_long_01_r.data.dds",
                },
                {},
                {},
                {},
            ],
            "alphaRef": 25,
            "alphaTest": True,
            "doubleSided": True,
            "dynamicCubemap": True,
            "subSurface": True,
            "subSurfaceIntensity": 1,
            "version": 1.5,
        },
        mat_short: {
            "name": mat_short,
            "mapTo": mat_short,
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", mat_short),
            "Stages": [
                {
                    "ambientOcclusionMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_ao.data.dds",
                    "baseColorMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_b.color.dds",
                    "normalMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_nm.normal.dds",
                    "opacityMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_o.data.dds",
                    "roughnessMap": "/assets/materials/foliage/grass/t_grass_green_short_01/t_grass_green_short_01_r.data.dds",
                },
                {},
                {},
                {},
            ],
            "alphaRef": 40,
            "alphaTest": True,
            "doubleSided": True,
            "dynamicCubemap": True,
            "subSurface": True,
            "subSurfaceIntensity": 1,
            "version": 1.5,
        },
    }

    empty_slot: dict[str, Any] = {
        "billboardUVs": None,
        "clumpRadius": None,
        "maxClumpCount": None,
        "minClumpCount": None,
        "probability": None,
        "sizeMax": None,
        "sizeMin": None,
        "windScale": None,
    }
    pos = [round(center_x, 3), round(center_y, 3), round(center_z, 3)]
    covers = [
        {
            "name": "exp_meadow_grass_cover",
            "class": "GroundCover",
            "persistentId": stable_uuid(level_id, "GroundCover", "exp_meadow_grass_cover"),
            "__parent": "vegetation",
            "position": pos,
            "material": mat_long,
            "gridSize": 8,
            "radius": 110,
            "dissolveRadius": 75,
            "shapeCullRadius": 90,
            "maxBillboardTiltAngle": 40,
            "maxElements": 120000,
            "seed": 11,
            "shapesCastShadows": False,
            "windGustFrequency": 6,
            "windTurbulenceFrequency": 0.35,
            "windTurbulenceStrength": 0.35,
            "Types": [
                {
                    "billboardUVs": [0, 0, 1, 0.4918],
                    "clumpRadius": 1.8,
                    "layer": "Grass",
                    "maxClumpCount": 4,
                    "minClumpCount": 2,
                    "probability": 1.0,
                    "sizeMin": 0.35,
                    "sizeMax": 0.65,
                    "windScale": 0.2,
                },
                {
                    "billboardUVs": [0, 0.5117, 1, 0.4883],
                    "clumpRadius": 1.8,
                    "layer": "Field",
                    "maxClumpCount": 4,
                    "minClumpCount": 2,
                    "probability": 0.8,
                    "sizeMin": 0.35,
                    "sizeMax": 0.60,
                    "windScale": 0.2,
                },
                *[dict(empty_slot) for _ in range(6)],
            ],
        },
        {
            "name": "exp_short_grass_cover",
            "class": "GroundCover",
            "persistentId": stable_uuid(level_id, "GroundCover", "exp_short_grass_cover"),
            "__parent": "vegetation",
            "position": pos,
            "material": mat_short,
            "gridSize": 8,
            "radius": 90,
            "dissolveRadius": 65,
            "shapeCullRadius": 80,
            "maxBillboardTiltAngle": 40,
            "maxElements": 80000,
            "seed": 23,
            "shapesCastShadows": False,
            "windGustFrequency": 5,
            "windTurbulenceFrequency": 0.3,
            "windTurbulenceStrength": 0.25,
            "Types": [
                {
                    "billboardUVs": [0, 0, 1, 0.4936],
                    "clumpRadius": 1.5,
                    "layer": "Grass",
                    "maxClumpCount": 4,
                    "minClumpCount": 2,
                    "probability": 0.9,
                    "sizeMin": 0.18,
                    "sizeMax": 0.35,
                    "windScale": 0.15,
                },
                {
                    "billboardUVs": [0, 0.5234, 1, 0.4488],
                    "clumpRadius": 1.5,
                    "layer": "ForestFloor",
                    "maxClumpCount": 3,
                    "minClumpCount": 1,
                    "probability": 0.6,
                    "sizeMin": 0.16,
                    "sizeMax": 0.30,
                    "windScale": 0.12,
                },
                *[dict(empty_slot) for _ in range(6)],
            ],
        },
    ]
    return covers, materials


# ---------------------------------------------------------------------------
# Cached DEM helper (allows offline slicing from regional corridor GeoTIFFs)
# ---------------------------------------------------------------------------


def resolve_dem_for_bbox(
    bbox: BBox,
    cache_dir: Path,
    *,
    source: str,
    resolution_m: float,
    force: bool = False,
) -> Path:
    """Resolve a DEM GeoTIFF covering ``bbox``, falling back to any cached superset."""
    try:
        return fetch_dem(
            bbox,
            cache_dir,
            source=source,
            resolution_m=resolution_m,
            force=force,
        )
    except DemFetchError:
        if not cache_dir.is_dir():
            raise
        candidates: list[tuple[float, Path]] = []
        tol = 1e-4
        for sidecar_path in sorted(cache_dir.glob("*.json")):
            try:
                meta = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            mb = meta.get("bbox")
            if not isinstance(mb, dict):
                continue
            tif_path = sidecar_path.with_suffix(".tif")
            if not tif_path.is_file():
                continue
            if (
                float(mb.get("west", 999.0)) <= bbox.west + tol
                and float(mb.get("south", 999.0)) <= bbox.south + tol
                and float(mb.get("east", -999.0)) >= bbox.east - tol
                and float(mb.get("north", -999.0)) >= bbox.north - tol
            ):
                area = (float(mb["east"]) - float(mb["west"])) * (float(mb["north"]) - float(mb["south"]))
                candidates.append((area, tif_path))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]
        raise


# ---------------------------------------------------------------------------
# Multi-block validation
# ---------------------------------------------------------------------------


def validate_multiblock_level(
    level_dir: Path,
    config: ProjectConfig | None = None,
    *,
    beamng_install: str | Path | None = None,
) -> ValidationReport:
    """Validate a built multi-``TerrainBlock`` level directory on disk."""
    report = ValidationReport(level_dir=level_dir)
    if not level_dir.is_dir():
        report.add("level_dir", False, f"{level_dir} does not exist")
        return report

    for rel in (
        "info.json",
        "main/items.level.json",
        "main/MissionGroup/items.level.json",
        "art/terrains/main.materials.json",
    ):
        p = level_dir / rel
        if not p.is_file():
            report.add("required_file", False, f"missing {rel}")
        elif p.stat().st_size == 0 and rel != "main/items.level.json":
            report.add("required_file", False, f"{rel} is empty")

    _check_scene_tree(report)

    terrain_blocks: list[dict[str, Any]] = []
    for item_file in sorted((level_dir / "main").rglob("items.level.json")):
        for line in item_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("class") == "TerrainBlock":
                terrain_blocks.append(obj)

    report.add(
        "terrain_blocks",
        len(terrain_blocks) >= 2,
        f"found {len(terrain_blocks)} TerrainBlock objects (expected >= 2)",
    )

    loaded_blocks: list[tuple[dict[str, Any], TerrainFile]] = []
    prefix = f"/levels/{level_dir.name}/"
    for tb in terrain_blocks:
        name = str(tb.get("name", "<unnamed>"))
        tf_rel = str(tb.get("terrainFile", ""))
        if not tf_rel.startswith(prefix):
            report.add("terrain_file", False, f"{name}: unexpected terrainFile {tf_rel!r}")
            continue
        rel_ter = tf_rel.removeprefix(prefix)
        ter_path = level_dir / rel_ter
        meta_path = ter_path.with_suffix(".terrain.json")
        if not ter_path.is_file() or not meta_path.is_file():
            report.add("terrain_file", False, f"{name}: missing {rel_ter} or {meta_path.name}")
            continue
        try:
            tf = read_ter(ter_path)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (TerrainFormatError, OSError, json.JSONDecodeError) as exc:
            report.add("terrain_binary", False, f"{name}: unreadable ({exc})")
            continue

        problems: list[str] = []
        if meta.get("size") != tf.size:
            problems.append(f"size {meta.get('size')} != {tf.size}")
        if list(meta.get("materials", [])) != list(tf.materials):
            problems.append("materials mismatch")
        non_hole = tf.layers[tf.layers != HOLE]
        if non_hole.size and int(non_hole.max()) >= len(tf.materials):
            problems.append("layer index out of range")
        report.add(
            f"terrain_block_{name}",
            not problems,
            f"{tf.size}x{tf.size}, {len(tf.materials)} materials"
            + (f" ({'; '.join(problems)})" if problems else ""),
        )
        loaded_blocks.append((tb, tf))

    # Verify touching seams between loaded blocks on disk
    seams_verified = 0
    seam_problems: list[str] = []
    for i in range(len(loaded_blocks)):
        tb_a, tf_a = loaded_blocks[i]
        pa = tb_a["position"]
        sq_a = float(tb_a.get("squareSize", 1.0))
        ext_a = (tf_a.size - 1) * sq_a
        for j in range(i + 1, len(loaded_blocks)):
            tb_b, tf_b = loaded_blocks[j]
            pb = tb_b["position"]
            sq_b = float(tb_b.get("squareSize", 1.0))
            if not math.isclose(sq_a, sq_b, abs_tol=1e-6):
                continue
            # Horizontal adjacency (A west of B, same Y)
            if math.isclose(pb[1], pa[1], abs_tol=1e-3) and math.isclose(pb[0], pa[0] + ext_a, abs_tol=1e-3):
                seams_verified += 1
                if not np.array_equal(tf_a.heights[:, -1], tf_b.heights[:, 0]):
                    seam_problems.append(f"{tb_a['name']}/{tb_b['name']} height mismatch")
                if not np.array_equal(tf_a.layers[:, -1], tf_b.layers[:, 0]):
                    seam_problems.append(f"{tb_a['name']}/{tb_b['name']} layer mismatch")
            elif math.isclose(pa[1], pb[1], abs_tol=1e-3) and math.isclose(pa[0], pb[0] + ext_a, abs_tol=1e-3):
                seams_verified += 1
                if not np.array_equal(tf_b.heights[:, -1], tf_a.heights[:, 0]):
                    seam_problems.append(f"{tb_b['name']}/{tb_a['name']} height mismatch")
                if not np.array_equal(tf_b.layers[:, -1], tf_a.layers[:, 0]):
                    seam_problems.append(f"{tb_b['name']}/{tb_a['name']} layer mismatch")
            # Vertical adjacency (A south of B, same X)
            elif math.isclose(pb[0], pa[0], abs_tol=1e-3) and math.isclose(pb[1], pa[1] + ext_a, abs_tol=1e-3):
                seams_verified += 1
                if not np.array_equal(tf_a.heights[-1, :], tf_b.heights[0, :]):
                    seam_problems.append(f"{tb_a['name']}/{tb_b['name']} height mismatch")
                if not np.array_equal(tf_a.layers[-1, :], tf_b.layers[0, :]):
                    seam_problems.append(f"{tb_a['name']}/{tb_b['name']} layer mismatch")
            elif math.isclose(pa[0], pb[0], abs_tol=1e-3) and math.isclose(pa[1], pb[1] + ext_a, abs_tol=1e-3):
                seams_verified += 1
                if not np.array_equal(tf_b.heights[-1, :], tf_a.heights[0, :]):
                    seam_problems.append(f"{tb_b['name']}/{tb_a['name']} height mismatch")
                if not np.array_equal(tf_b.layers[-1, :], tf_a.layers[0, :]):
                    seam_problems.append(f"{tb_b['name']}/{tb_a['name']} layer mismatch")

    report.add(
        "terrain_seams",
        seams_verified > 0 and not seam_problems,
        f"{seams_verified} adjacent seam(s) verified"
        + (f": {'; '.join(seam_problems)}" if seam_problems else ", 0 height/layer mismatches"),
    )

    install = Path(beamng_install) if beamng_install else None
    _check_materials(report, install)
    if config is not None:
        _check_scale(report, config)
    return report


# ---------------------------------------------------------------------------
# End-to-end experimental build
# ---------------------------------------------------------------------------


def _count_seam_crossings(
    seam_xs: Sequence[float],
    seam_ys: Sequence[float],
    *,
    roads: Sequence[Any],
    water_network: Any,
    vegetation_areas: Sequence[Any],
    buildings: Sequence[Any],
) -> tuple[int, int, int, int]:
    def crosses(min_x: float, min_y: float, max_x: float, max_y: float) -> bool:
        return any(min_x < sx < max_x for sx in seam_xs) or any(min_y < sy < max_y for sy in seam_ys)

    r_cnt = sum(
        1
        for r in roads
        if r.points.shape[0] >= 2
        and crosses(
            float(r.points[:, 0].min()),
            float(r.points[:, 1].min()),
            float(r.points[:, 0].max()),
            float(r.points[:, 1].max()),
        )
    )
    rv_cnt = 0
    if water_network is not None:
        for rv in water_network.rivers:
            if rv.points.shape[0] >= 2 and crosses(
                float(rv.points[:, 0].min()),
                float(rv.points[:, 1].min()),
                float(rv.points[:, 0].max()),
                float(rv.points[:, 1].max()),
            ):
                rv_cnt += 1
        for wb in water_network.water_bodies:
            if crosses(*wb.bounds):
                rv_cnt += 1
    f_cnt = sum(
        1
        for v in vegetation_areas
        if v.kind == "forest"
        and len(v.ring) >= 3
        and crosses(
            float(v.ring[:, 0].min()),
            float(v.ring[:, 1].min()),
            float(v.ring[:, 0].max()),
            float(v.ring[:, 1].max()),
        )
    )
    b_cnt = sum(
        1
        for b in buildings
        if len(b.ring) >= 3
        and crosses(
            float(b.ring[:, 0].min()),
            float(b.ring[:, 1].min()),
            float(b.ring[:, 0].max()),
            float(b.ring[:, 1].max()),
        )
    )
    return r_cnt, rv_cnt, f_cnt, b_cnt


def build_multiblock_level(
    config: ProjectConfig,
    spec: MultiBlockSpec,
    *,
    progress: Callable[[float, str], None] | None = None,
    make_zip: bool = False,
    osm_tile_km: float = 1.5,
    osm_budget_s: float = 240.0,
    max_decal_node_spacing_m: float = 40.0,
    building_simplify_m: float = 1.0,
    force_download: bool = False,
) -> MultiBlockBuildResult:
    """Build an experimental multi-``TerrainBlock`` BeamNG level."""
    start = time.perf_counter()
    level = config.level
    if level is None:
        raise ValueError("config has no 'level' block; nothing to build")

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    durations: dict[str, float] = {}
    warnings: list[str] = []

    origin_x, origin_y = resolve_multiblock_origin(config, spec)
    parent_grid = spec.parent_grid_size
    parent_extent = (parent_grid - 1) * spec.square_size_m
    total_w = spec.total_width_m
    total_h = spec.total_height_m

    # Geographic bbox covering the entire parent build grid + margin
    margin_deg = 0.004
    corners_x = [origin_x, origin_x + parent_extent]
    corners_y = [origin_y, origin_y + parent_extent]
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
    report(0.01, f"multiblock {level.id}: {spec.cols}x{spec.rows} blocks ({total_w:.0f}x{total_h:.0f} m)")

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

    # -- Features clipped to the active multi-block footprint --------------
    margin = ROAD_RENDER_MARGIN_M
    clip_bounds = (
        origin_x - margin,
        origin_y - margin,
        origin_x + total_w + margin,
        origin_y + total_h + margin,
    )
    water_clip = (origin_x, origin_y, origin_x + total_w, origin_y + total_h)

    stage = time.perf_counter()
    report(0.28, "extracting roads")
    roads = extract_roads(osm_data, config.projection, config.roads, clip_bounds=clip_bounds)
    road_stats = road_network_stats(roads)
    durations["roads"] = time.perf_counter() - stage

    stage = time.perf_counter()
    railways: list[Any] = []
    if config.railways_enabled:
        report(0.32, "extracting railways")
        railways = extract_railways(osm_data, config.projection, clip_bounds=clip_bounds)
    durations["railways"] = time.perf_counter() - stage

    stage = time.perf_counter()
    buildings: list[Any] = []
    if config.buildings_enabled:
        report(0.35, "extracting buildings")
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
        report(0.38, "extracting vegetation")
        vegetation_areas, _ = extract_vegetation(
            osm_data,
            config.projection,
            clip_bounds=clip_bounds,
        )
    durations["vegetation_extract"] = time.perf_counter() - stage

    # -- Joint continuous parent TerrainBuild ------------------------------
    water_network = None
    bridge_roads = [r for r in roads if r.bridge and len(r.points) >= 2] if config.bridges_enabled else []
    with DemReader(dem_path) as dem:
        w_stage = time.perf_counter()
        if config.water_enabled:
            report(0.40, "extracting hydrology")
            water_network = extract_water(
                osm_data,
                config.projection,
                dem=dem,
                clip_bounds=water_clip,
            )
        durations["water"] = time.perf_counter() - w_stage

        stage = time.perf_counter()
        report(0.42, f"building shared {parent_grid}x{parent_grid} terrain domain")
        corridors = roads_to_corridors(roads, config.roads)
        if config.railways_enabled and railways:
            corridors.extend(railways_to_corridors(railways))

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
        if buildings:
            landcover_polys["Concrete"] = [b.ring for b in buildings]

        parent_build = build_terrain(
            dem,
            config.projection,
            origin_x=origin_x,
            origin_y=origin_y,
            grid_size=parent_grid,
            cell_size_m=spec.square_size_m,
            corridors=corridors,
            material_order=DEFAULT_MATERIAL_ORDER,
            ground_margin_m=level.ground_margin_m,
            max_height_m=level.max_height_m,
            max_grade=config.roads.max_grade,
            water_network=water_network,
            forest_polygons=forest_geoms or None,
            landcover_polygons=landcover_polys or None,
            bridge_roads=bridge_roads or None,
            road_settings=config.roads,
            progress=lambda f, m: report(0.42 + 0.30 * f, m),
        )
    durations["terrain"] = time.perf_counter() - stage

    # -- Slice into adjacent TerrainBlocks & verify seams ------------------
    stage = time.perf_counter()
    report(0.74, "slicing into adjacent TerrainBlocks and verifying seams")
    tiles, base_z, max_height = slice_terrain_tiles(parent_build, spec, level_id=level.id)
    seam_report = verify_tile_seams(tiles)
    r_cross, rv_cross, f_cross, b_cross = _count_seam_crossings(
        seam_report.seam_x_positions,
        seam_report.seam_y_positions,
        roads=roads,
        water_network=water_network,
        vegetation_areas=vegetation_areas,
        buildings=buildings,
    )
    seam_report.roads_crossing_seam = r_cross
    seam_report.rivers_crossing_seam = rv_cross
    seam_report.forests_crossing_seam = f_cross
    seam_report.buildings_crossing_seam = b_cross
    if not seam_report.all_seams_exact:
        warnings.append(
            f"seam check found u16_diff={seam_report.max_quantized_diff}, "
            f"layer_mismatches={seam_report.layer_mismatches}"
        )
    durations["slice_seams"] = time.perf_counter() - stage

    # -- Scene tree assembly -----------------------------------------------
    stage = time.perf_counter()
    report(0.76, "assembling multi-TerrainBlock scene")
    tree = SceneTree(level_id=level.id)
    level_dir = config.build_path("levels", level.id)
    if level_dir.exists():
        shutil.rmtree(level_dir)

    tree.add_group(("level_objects", "terrain"))
    for tile in tiles:
        tree.add_object(("level_objects", "terrain"), tile.placement.to_object(level.id))

    ground_z = float(np.median(parent_build.elevations[: spec.combined_rows, : spec.combined_cols]))
    build_sun_and_sky(
        tree,
        ("level_objects", "sky_and_sun"),
        latitude=level.center_lat,
        longitude=level.center_lon,
        elevation_m=ground_z,
        time_of_day=level.time_of_day,
        axis_tilt=level.axis_tilt,
        visible_distance_m=min(float(max(4000.0, max(total_w, total_h) * 2.5)), 12000.0),
        fog_density=0.00018,
    )

    bridge_profiles = getattr(parent_build, "bridge_profiles", {}) or {}
    ground_roads = [r for r in roads if not (r.bridge and r.osm_id in bridge_profiles)]
    if spec.road_seam_mode == "split":
        ground_roads = _split_roads_at_seams(
            ground_roads, seam_report.seam_x_positions, seam_report.seam_y_positions
        )
    export_stats = add_network_to_scene(
        tree,
        ground_roads,
        parent_build,
        group_path=("level_objects", "road_network"),
        max_node_spacing_m=max_decal_node_spacing_m,
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

    bridges_dir = level_dir / "art" / "shapes" / "bridges"
    if config.bridges_enabled and bridge_roads:
        bridges_dir.mkdir(parents=True, exist_ok=True)

        def _bridge_ground_sampler(x: float, y: float) -> float:
            return float(sample_points_z(np.array([[x, y]], dtype=np.float64), parent_build)[0])

        exported_bridges = 0
        for r in bridge_roads:
            if r.osm_id in bridge_profiles:
                b_pts, deck_z = bridge_profiles[r.osm_id]
                r_solved = replace(r, points=b_pts)
            else:
                gz = sample_surface_z(r, parent_build)
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

    building_stats: dict[str, Any] = {"enabled": config.buildings_enabled}
    if config.buildings_enabled and buildings:
        building_objects, building_export = export_buildings(
            buildings,
            parent_build,
            level_id=level.id,
            level_dir=level_dir,
            max_instances=spec.max_buildings,
            batch_chunks=True,
        )
        for obj in building_objects:
            tree.add_object(("level_objects", "buildings"), obj)
        building_stats.update(building_export.as_dict())

    veg_stats: dict[str, Any] = {"enabled": config.vegetation_enabled}
    if config.vegetation_enabled and vegetation_areas:
        water_items: list[Any] = []
        if water_network is not None:
            water_items.extend(water_network.rivers)
            water_items.extend(water_network.water_bodies)
        veg_objects, v_exp = export_vegetation(
            vegetation_areas,
            parent_build,
            level_id=level.id,
            level_dir=level_dir,
            roads=roads,
            buildings=buildings,
            water=water_items,
            railways=railways,
        )
        for obj in veg_objects:
            tree.add_object(("level_objects", "vegetation"), obj)
        veg_stats.update(v_exp.as_dict())

    if spec.groundcover_enabled:
        gc_cx = origin_x + 0.5 * total_w
        gc_cy = origin_y + 0.5 * total_h
        gc_objs, gc_mats = build_experiment_groundcover(
            level.id,
            center_x=gc_cx,
            center_y=gc_cy,
            center_z=ground_z,
        )
        for obj in gc_objs:
            tree.add_object(("level_objects", "vegetation"), obj)
        gc_mat_path = level_dir / "art" / "shapes" / "groundcover" / "main.materials.json"
        merged_gc_mats: dict[str, Any] = {}
        if gc_mat_path.is_file():
            try:
                merged_gc_mats.update(json.loads(gc_mat_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        merged_gc_mats.update(gc_mats)
        write_json(gc_mat_path, merged_gc_mats)
        veg_stats["groundcover_objects"] = len(gc_objs)

    # -- Spawn points (default near P152 seam + any extra presets) ---------
    spawn_lon = level.spawn_lon if level.spawn_lon is not None else level.center_lon
    spawn_lat = level.spawn_lat if level.spawn_lat is not None else level.center_lat
    spawn_presets: list[dict[str, Any]] = [
        {
            "key": "player_spawn",
            "name": "spawn_default",
            "title": f"{level.title} (P152 Seam Approach)",
            "geo": (spawn_lon, spawn_lat),
        }
    ]
    for idx, extra in enumerate(spec.extra_spawns, start=1):
        geo = extra.get("geo")
        if isinstance(geo, (list, tuple)) and len(geo) == 2:
            spawn_presets.append(
                {
                    "key": str(extra.get("key", f"spawn_extra_{idx}")),
                    "name": str(extra.get("name", f"spawn_extra_{idx}")),
                    "title": str(extra.get("title", f"Spawn {idx}")),
                    "geo": (float(geo[0]), float(geo[1])),
                }
            )

    spawn_points_info: list[dict[str, Any]] = []
    for sp in spawn_presets:
        lon, lat = sp["geo"]
        ax, ay = config.projection.geo_to_world(lon, lat)
        spawn = _choose_spawn(roads, parent_build, anchor=(ax, ay))
        add_spawn_point(
            tree,
            ("PlayerDropPoints",),
            key=sp["key"],
            name=sp["name"],
            x=spawn[0],
            y=spawn[1],
            z=spawn[2] + 0.35,
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

    # -- Write files -------------------------------------------------------
    files = tree.render()
    info = build_info_json(
        level.id,
        level.title,
        (
            f"P2 multi-TerrainBlock experiment: {spec.cols}x{spec.rows} adjacent "
            f"{spec.tile_grid_size}x{spec.tile_grid_size} @ {spec.square_size_m} m blocks "
            f"({total_w:.0f}x{total_h:.0f} m), road_seam_mode={spec.road_seam_mode}."
        ),
        extent_m=max(total_w, total_h),
        default_spawn_point_name="spawn_default",
        spawn_points=spawn_points_info,
    )
    info["size"] = [round(total_w), round(total_h)]
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

    write_terrain_base_textures(level_dir, DEFAULT_MATERIAL_ORDER, size=spec.base_tex_size)
    write_json(
        level_dir / "art" / "terrains" / "main.materials.json",
        build_materials_json(level.id, DEFAULT_MATERIAL_ORDER, base_tex_size=spec.base_tex_size),
    )
    write_road_materials(level_dir, level.id)

    total_ter_bytes = 0
    for tile in tiles:
        write_json(
            level_dir / f"{tile.name}.terrain.json",
            build_terrain_json(
                level.id,
                tile.name,
                size=spec.tile_grid_size,
                materials=tile.terrain_file.materials,
            ),
        )
        total_ter_bytes += write_ter(level_dir / f"{tile.name}.ter", tile.terrain_file)

    generate_level_preview_png(level_dir, level.id)
    try:
        manifest = build_attribution_manifest(level.id, dem_sources=[dem_path])
        write_attribution_files(manifest, level_dir=level_dir, build_dir=config.build_dir)
    except Exception as exc:
        warnings.append(f"attribution manifest generation failed: {exc}")

    # Emit a machine-readable experiment manifest for QA scripts
    exp_manifest = {
        "level_id": level.id,
        "tile_grid_size": spec.tile_grid_size,
        "square_size_m": spec.square_size_m,
        "cols": spec.cols,
        "rows": spec.rows,
        "origin_world": [origin_x, origin_y],
        "total_extent_m": [total_w, total_h],
        "base_z": base_z,
        "max_height": max_height,
        "road_seam_mode": spec.road_seam_mode,
        "groundcover_enabled": spec.groundcover_enabled,
        "tiles": [
            {
                "name": t.name,
                "ix": t.ix,
                "iy": t.iy,
                "origin": [t.origin_x, t.origin_y],
                "extent_m": t.extent_m,
                "terrain_file": f"{t.name}.ter",
            }
            for t in tiles
        ],
        "seam_report": seam_report.as_dict(),
    }
    write_json(level_dir / "multiblock_experiment.json", exp_manifest)
    durations["scene"] = time.perf_counter() - stage

    stage = time.perf_counter()
    mod_zip: Path | None = None
    if make_zip:
        report(0.95, "packaging mod zip")
        mod_zip = package_level(config.build_dir, level.id)
    durations["package"] = time.perf_counter() - stage

    stats: dict[str, Any] = {
        "level_id": level.id,
        "blocks": len(tiles),
        "terrain_bytes": total_ter_bytes,
        "roads": road_stats,
        "export": export_stats,
        "water": water_stats,
        "buildings": building_stats,
        "vegetation": veg_stats,
        "osm": osm_stats,
        "seams": seam_report.as_dict(),
        "asset_references": len(referenced_asset_paths(DEFAULT_MATERIAL_ORDER)),
        "total_seconds": time.perf_counter() - start,
    }
    report(1.0, "multiblock build complete")
    return MultiBlockBuildResult(
        level_id=level.id,
        level_dir=level_dir,
        mod_zip=mod_zip,
        tiles=tiles,
        seam_report=seam_report,
        stats=stats,
        durations=durations,
        warnings=warnings,
    )
