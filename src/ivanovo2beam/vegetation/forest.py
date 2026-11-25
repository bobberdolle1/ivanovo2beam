"""Native BeamNG 0.39 forest instancing and deterministic placement.

Generates:
1. ``levels/<id>/forest/<item>.forest4.json`` for high-FPS tree instancing;
2. ``levels/<id>/art/forest/managedItemData.json`` registering Central Russian
   species mix referencing stock BeamNG meshes by path (zero copied assets!);
3. ``levels/<id>/art/forest/main.materials.json`` mapping bark, branch, and leaf
   materials with alpha test and double-sided lighting;
4. Scene objects: ``theForest`` and ``ForestWindEmitter`` in ``level_objects/vegetation``.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely import contains_xy
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.validation import make_valid

try:
    from PIL import Image, ImageDraw

    # Quick probe to ensure C extension (_imaging) is actually usable
    _probe = Image.new("1", (1, 1), 0)
    _HAS_PIL = True
except (ImportError, OSError, Exception):
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    _HAS_PIL = False

try:
    from affine import Affine
    from rasterio.features import rasterize as _rio_rasterize

    _HAS_RASTERIO = True
except (ImportError, OSError, Exception):
    Affine = None  # type: ignore[assignment,misc]
    _rio_rasterize = None  # type: ignore[assignment]
    _HAS_RASTERIO = False

from ivanovo2beam.beamng.level import stable_uuid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ivanovo2beam.buildings.footprints import BuildingFootprint
    from ivanovo2beam.vegetation.extract import VegetationArea

#: Deterministic UUID namespace for forest items and materials.
_FOREST_NS = uuid.UUID("9c42a5e0-4182-41f6-b192-3a55b1f0981b")

#: Priority order for area kinds when allocating max_trees budget.
_KIND_PRIORITY: dict[str, int] = {
    "forest": 0,
    "scrub": 1,
    "meadow": 2,
}

#: Road-corridor distance bucket (m) for prioritizing roadside forests.
_CORRIDOR_BUCKET_M = 250.0

#: Priority highway classes used when ranking roadside forest proximity.
_PRIORITY_HIGHWAY_CLASSES = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
    }
)

#: Per-polygon tree cap in Pass 1 so every roadside forest gets coverage.
_PASS1_MAX_TREES_PER_POLY = 300

#: Default terrain grid resolution / cell size fallbacks.
_DEFAULT_GRID_SIZE = 2048
_DEFAULT_CELL_SIZE_M = 1.0

#: Minimum trunk-to-trunk spacing in metres to prevent overlapping trees.
_MIN_TREE_SPACING_M = 2.5

#: Lean 7-species Central Russian summer mix referencing verified /assets/ meshes.
SPECIES_DATA: dict[str, dict[str, Any]] = {
    # 1. Silver Birch (Берёза)
    "tree_aspen_forest_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/birch/tree_aspen_forest_a.dae",
        "radius": 0.35,
        "mass": 10.0,
        "windScale": 0.5,
        "lodReflectScalar": 0.0,
        "category": "birch",
    },
    "tree_aspen_forest_b": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/birch/tree_aspen_forest_b.dae",
        "radius": 0.35,
        "mass": 10.0,
        "windScale": 0.5,
        "lodReflectScalar": 0.0,
        "category": "birch",
    },
    "tree_aspen_large_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/birch/tree_aspen_large_a.dae",
        "radius": 0.35,
        "mass": 10.0,
        "windScale": 0.5,
        "lodReflectScalar": 0.0,
        "category": "birch",
    },
    "tree_aspen_small_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/birch/tree_aspen_small_a.dae",
        "radius": 0.30,
        "mass": 10.0,
        "windScale": 0.5,
        "lodReflectScalar": 0.0,
        "category": "birch",
    },
    # 2. Broadleaf Canopy & Undergrowth (Бук)
    "tree_beech_forest_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/beech/tree_beech_forest_a.dae",
        "radius": 0.40,
        "mass": 10.0,
        "windScale": 0.5,
        "lodReflectScalar": 0.0,
        "category": "beech",
    },
    "tree_beech_small_b": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/beech/tree_beech_small_b.dae",
        "radius": 0.32,
        "mass": 10.0,
        "windScale": 0.5,
        "lodReflectScalar": 0.0,
        "category": "beech",
    },
    "tree_beech_bush_a": {
        "class": "ForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/beech/tree_beech_bush_a.dae",
        "radius": 0.30,
        "snapRotationToTerrain": True,
        "lodReflectScalar": 0.0,
        "category": "bush",
    },
}

#: Category weights in Central Russian forests (birch 70%, beech 30%).
FOREST_SPECIES_DISTRIBUTION: list[tuple[str, float]] = [
    ("tree_aspen_large_a", 0.20),
    ("tree_aspen_forest_a", 0.20),
    ("tree_aspen_forest_b", 0.15),
    ("tree_aspen_small_a", 0.15),
    ("tree_beech_forest_a", 0.15),
    ("tree_beech_small_b", 0.15),
]

SCRUB_SPECIES_DISTRIBUTION: list[tuple[str, float]] = [
    ("tree_beech_bush_a", 0.75),
    ("tree_aspen_small_a", 0.15),
    ("tree_beech_small_b", 0.10),
]

#: Native tree materials referenced by the 7 lean /assets/meshes/foliage/ meshes.
#: All texture paths resolve directly inside /assets/materials/tree.zip.
STOCK_TREE_MATERIALS: dict[str, dict[str, Any]] = {
    # Birch
    "Birch_Bark_01": {
        "baseColorMap": "/assets/materials/tree/birch/t_birch_bark/t_birch_bark_b.color.dds",
        "normalMap": "/assets/materials/tree/birch/t_birch_bark/t_birch_bark_nm.normal.dds",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_bark/t_birch_bark_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/birch/t_birch_bark/t_birch_bark_ao.data.dds",
        "groundType": "WOOD",
    },
    "Birch_Branch_01": {
        "baseColorMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_b.color.dds",
        "normalMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_o.data.dds",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 60,
        "doubleSided": True,
        "groundType": "WOOD",
    },
    "m_ind_birch_leaves_01": {
        "baseColorMap": "/assets/materials/tree/birch/t_birch_leaves_ind/t_birch_leaves_ind_b.color.dds",
        "normalMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 100,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    # Beech
    "beech_trunk": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_trunk/t_beech_bark_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_trunk/t_beech_bark_nm.normal.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_trunk/t_beech_bark_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_trunk/t_beech_bark_ao.data.dds",
        "groundType": "WOOD",
    },
    "beech_branch_01": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 75,
        "doubleSided": True,
        "groundType": "WOOD",
    },
    "beech_leaves": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 105,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "beech_blocker": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 90,
        "doubleSided": True,
        "groundType": "WOOD",
    },
    # Compiled .cdae material names for beech meshes
    "m_ind_beech_leaves": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 105,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "m_ind_beech_blocker": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 90,
        "doubleSided": True,
        "groundType": "WOOD",
    },
    # Collision proxy material
    "leaves_strong": {
        "collisionProxy": True,
        "groundDepth": 0.6,
        "groundType": "leaves_strong",
    },
}


@dataclass(slots=True)
class VegetationExportStats:
    """Diagnostics from vegetation/forest export."""

    total_trees: int = 0
    by_species: dict[str, int] = field(default_factory=dict)
    species_types: int = 0
    files_written: int = 0
    bytes_written: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serialisable summary."""
        return {
            "total_trees": self.total_trees,
            "species_types": self.species_types,
            "by_species": dict(sorted(self.by_species.items(), key=lambda kv: -kv[1])),
            "files_written": self.files_written,
            "bytes_written": self.bytes_written,
        }


def _rasterize_disk(mask: np.ndarray, cx: float, cy: float, r_px: float) -> None:
    """Rasterize a filled circle of radius r_px at (cx, cy) onto mask."""
    if r_px <= 0:
        return
    sz_r, sz_c = mask.shape
    min_r = max(0, math.floor(cy - r_px))
    max_r = min(sz_r - 1, math.ceil(cy + r_px))
    r2 = r_px * r_px
    for r in range(min_r, max_r + 1):
        dy = (r + 0.5) - cy
        rem = r2 - dy * dy
        if rem >= 0:
            dx = math.sqrt(rem)
            c_start = max(0, math.ceil(cx - dx - 0.5))
            c_end = min(sz_c - 1, math.floor(cx + dx - 0.5))
            if c_start <= c_end:
                mask[r, c_start : c_end + 1] = True


def _rasterize_polygon(mask: np.ndarray, poly_pts: Sequence[tuple[float, float]]) -> None:
    """Rasterize a 2D polygon (given as [(col, row), ...]) onto mask using scanline fill."""
    n = len(poly_pts)
    if n < 3:
        return
    sz_r, sz_c = mask.shape
    min_r = max(0, math.floor(min(p[1] for p in poly_pts)))
    max_r = min(sz_r - 1, math.ceil(max(p[1] for p in poly_pts)))
    if min_r > max_r:
        return

    # Collect non-horizontal edges
    edges: list[tuple[float, float, float, float]] = []
    for i in range(n):
        p1 = poly_pts[i]
        p2 = poly_pts[(i + 1) % n]
        if p1[1] == p2[1]:
            continue
        if p1[1] < p2[1]:
            edges.append((p1[0], p1[1], p2[0], p2[1]))
        else:
            edges.append((p2[0], p2[1], p1[0], p1[1]))

    for r in range(min_r, max_r + 1):
        y = r + 0.5
        nodes: list[float] = []
        for x1, y1, x2, y2 in edges:
            if y1 <= y < y2:
                nodes.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        nodes.sort()
        for i in range(0, len(nodes) - 1, 2):
            c_start = max(0, math.ceil(nodes[i] - 0.5))
            c_end = min(sz_c - 1, math.floor(nodes[i + 1] - 0.5))
            if c_start <= c_end:
                mask[r, c_start : c_end + 1] = True


def _rasterize_polyline(
    mask: np.ndarray, pts: Sequence[tuple[float, float]], width_px: int | float
) -> None:
    """Rasterize a thick polyline onto mask using capsule segments."""
    if len(pts) < 2:
        return
    h = max(0.5, float(width_px) / 2.0)
    for i in range(len(pts) - 1):
        c1, r1 = pts[i]
        c2, r2 = pts[i + 1]
        _rasterize_disk(mask, c1, r1, h)
        _rasterize_disk(mask, c2, r2, h)
        dc = c2 - c1
        dr = r2 - r1
        dist = math.hypot(dc, dr)
        if dist > 1e-6:
            nc = -dr / dist
            nr = dc / dist
            p0 = (c1 + nc * h, r1 + nr * h)
            p1 = (c2 + nc * h, r2 + nr * h)
            p2 = (c2 - nc * h, r2 - nr * h)
            p3 = (c1 - nc * h, r1 - nr * h)
            _rasterize_polygon(mask, [p0, p1, p2, p3])


class _RasterCanvas:
    """Unified raster canvas supporting PIL (if available) with pure-NumPy fallback."""

    def __init__(self, sz: int) -> None:
        if _HAS_PIL and Image is not None and ImageDraw is not None:
            self._img: Any | None = Image.new("1", (sz, sz), 0)
            self._draw: Any | None = ImageDraw.Draw(self._img)
            self._mask: np.ndarray | None = None
        else:
            self._img = None
            self._draw = None
            self._mask = np.zeros((sz, sz), dtype=bool)

    def line(self, pts: list[tuple[float, float]], width: int) -> None:
        if self._draw is not None:
            self._draw.line(pts, fill=1, width=width)
        elif self._mask is not None:
            _rasterize_polyline(self._mask, pts, width)

    def polygon(self, pts: list[tuple[float, float]]) -> None:
        if self._draw is not None:
            self._draw.polygon(pts, fill=1)
        elif self._mask is not None:
            _rasterize_polygon(self._mask, pts)

    def to_mask(self) -> np.ndarray:
        if self._img is not None:
            return np.asarray(self._img, dtype=bool)
        if self._mask is not None:
            return self._mask
        return np.zeros((1, 1), dtype=bool)


def _resolve_float(obj: Any, attr: str, fallback: float | None, default: float) -> float:
    """Resolve a float attribute from ``obj``, treating ``None`` as missing."""
    val = getattr(obj, attr, None) if obj is not None else None
    if val is not None:
        return float(val)
    if fallback is not None:
        return float(fallback)
    return default


def _resolve_grid_params(
    build: Any,
    *,
    grid_size: int | None = None,
    cell_size_m: float | None = None,
    origin_x: float | None = None,
    origin_y: float | None = None,
) -> tuple[int, float, float, float]:
    """Resolve terrain grid (size, origin_x, origin_y, cell_size_m) consistently."""
    elevations = getattr(build, "elevations", None) if build is not None else None
    if elevations is not None:
        sz = int(elevations.shape[0])
    else:
        sz = int(grid_size) if grid_size is not None else _DEFAULT_GRID_SIZE
    ox = _resolve_float(build, "origin_x", origin_x, 0.0)
    oy = _resolve_float(build, "origin_y", origin_y, 0.0)
    cell = _resolve_float(build, "cell_size_m", cell_size_m, _DEFAULT_CELL_SIZE_M)
    return sz, ox, oy, cell


def _rasterize_buffered_ring(
    canvas: _RasterCanvas,
    ring: np.ndarray,
    to_pixel: Any,
    buffer_m: float,
) -> None:
    """Rasterize a world-space polygon ring onto ``canvas`` with optional outward buffer."""
    if buffer_m > 0:
        try:
            geom: Any = Polygon(ring)
            if not geom.is_valid:
                geom = make_valid(geom)
            buffered = geom.buffer(buffer_m)
            polys: list[Polygon] = []
            if isinstance(buffered, Polygon):
                polys = [buffered]
            elif isinstance(buffered, MultiPolygon):
                polys = list(buffered.geoms)
            elif hasattr(buffered, "geoms"):
                polys = [g for g in buffered.geoms if isinstance(g, Polygon)]
            if polys:
                for p in polys:
                    if not p.is_empty and p.area > 0:
                        pix_pts = [
                            to_pixel(float(c[0]), float(c[1])) for c in p.exterior.coords
                        ]
                        canvas.polygon(pix_pts)
                return
        except Exception:
            pass
    pix_pts = [to_pixel(float(p[0]), float(p[1])) for p in ring]
    canvas.polygon(pix_pts)


def _collect_keepout_shapes(
    roads: Sequence[Any] | None,
    buildings: Sequence[BuildingFootprint] | None,
    water: Sequence[Any] | None,
    railways: Sequence[Any] | None,
) -> list[Any]:
    """Build world-space Shapely polygons for rasterio keep-out rasterization."""
    shapes: list[Any] = []

    if roads:
        for road in roads:
            pts = getattr(road, "points", None)
            if pts is None and isinstance(road, (list, tuple)):
                pts = road
            if pts is None or len(pts) < 2:
                continue
            width_m = float(getattr(road, "width_m", 6.0) or 6.0)
            half_w = (width_m + 6.0) / 2.0
            geom = LineString([(float(p[0]), float(p[1])) for p in pts]).buffer(half_w)
            if not geom.is_empty:
                shapes.append(geom)

    if railways:
        for rail in railways:
            pts = getattr(rail, "points", None)
            if pts is None and isinstance(rail, (list, tuple)):
                pts = rail
            if pts is None or len(pts) < 2:
                continue
            geom = LineString([(float(p[0]), float(p[1])) for p in pts]).buffer(4.0)
            if not geom.is_empty:
                shapes.append(geom)

    if buildings:
        for bld in buildings:
            ring = getattr(bld, "ring", None)
            if ring is None and isinstance(bld, np.ndarray):
                ring = bld
            if ring is None or len(ring) < 3:
                continue
            poly: Any = Polygon(ring)
            if not poly.is_valid:
                poly = make_valid(poly)
            geom = poly.buffer(2.0)
            if not geom.is_empty:
                shapes.append(geom)

    if water:
        for w in water:
            ring = getattr(w, "ring", None)
            if ring is None and isinstance(w, np.ndarray):
                ring = w
            if ring is not None and len(ring) >= 3:
                poly_w: Any = Polygon(ring)
                if not poly_w.is_valid:
                    poly_w = make_valid(poly_w)
                geom = poly_w.buffer(2.0)
                if not geom.is_empty:
                    shapes.append(geom)
            pts = getattr(w, "points", None)
            if pts is not None and len(pts) >= 2:
                width_m = float(getattr(w, "width_m", 8.0) or 8.0)
                geom = LineString([(float(p[0]), float(p[1])) for p in pts]).buffer(
                    (width_m + 4.0) / 2.0
                )
                if not geom.is_empty:
                    shapes.append(geom)

    return shapes


def build_keepout_mask(
    build: Any,
    roads: Sequence[Any] | None = None,
    buildings: Sequence[BuildingFootprint] | None = None,
    water: Sequence[Any] | None = None,
    railways: Sequence[Any] | None = None,
    *,
    grid_size: int | None = None,
    cell_size_m: float | None = None,
    origin_x: float | None = None,
    origin_y: float | None = None,
) -> tuple[np.ndarray, float, float, float]:
    """Build a fast 2D boolean keep-out mask raster (True = keep out).

    Burns road corridors + shoulders, railways, water bodies (+2m shoreline buffer),
    and building footprints (+2m wall clearance) into a boolean raster mask for
    O(1) clearance checks. Uses ``PIL.ImageDraw``, ``rasterio.features.rasterize``,
    or a pure-NumPy scanline fallback.
    """
    sz, ox, oy, cell = _resolve_grid_params(
        build,
        grid_size=grid_size,
        cell_size_m=cell_size_m,
        origin_x=origin_x,
        origin_y=origin_y,
    )

    if not _HAS_PIL and _HAS_RASTERIO and _rio_rasterize is not None and Affine is not None:
        shapes = _collect_keepout_shapes(roads, buildings, water, railways)
        if not shapes:
            return np.zeros((sz, sz), dtype=bool), ox, oy, cell
        transform = Affine(cell, 0.0, ox - 0.5 * cell, 0.0, cell, oy - 0.5 * cell)
        burned = _rio_rasterize(
            [(g, 1) for g in shapes],
            out_shape=(sz, sz),
            transform=transform,
            fill=0,
            dtype=np.uint8,
            all_touched=True,
        )
        return burned.astype(bool), ox, oy, cell

    canvas = _RasterCanvas(sz)

    def to_pixel(wx: float, wy: float) -> tuple[float, float]:
        col = (wx - ox) / cell
        row = (wy - oy) / cell
        return (col, row)

    # 1. Roads
    if roads:
        for road in roads:
            pts = getattr(road, "points", None)
            if pts is None and isinstance(road, (list, tuple)):
                pts = road
            if pts is None or len(pts) < 2:
                continue
            width_m = float(getattr(road, "width_m", 6.0) or 6.0)
            clearance_m = width_m + 6.0  # includes shoulders
            width_px = max(1, round(clearance_m / cell))
            pix_pts = [to_pixel(float(p[0]), float(p[1])) for p in pts]
            canvas.line(pix_pts, width=width_px)

    # 2. Railways
    if railways:
        for rail in railways:
            pts = getattr(rail, "points", None)
            if pts is None and isinstance(rail, (list, tuple)):
                pts = rail
            if pts is None or len(pts) < 2:
                continue
            width_px = max(1, round(8.0 / cell))
            pix_pts = [to_pixel(float(p[0]), float(p[1])) for p in pts]
            canvas.line(pix_pts, width=width_px)

    # 3. Buildings (+2m clearance around walls/eaves)
    if buildings:
        for bld in buildings:
            ring = getattr(bld, "ring", None)
            if ring is None and isinstance(bld, np.ndarray):
                ring = bld
            if ring is None or len(ring) < 3:
                continue
            _rasterize_buffered_ring(canvas, np.asarray(ring, dtype=np.float64), to_pixel, 2.0)

    # 4. Water (+2m shoreline buffer on polygons, +4m on rivers)
    if water:
        for w in water:
            ring = getattr(w, "ring", None)
            if ring is None and isinstance(w, np.ndarray):
                ring = w
            if ring is not None and len(ring) >= 3:
                _rasterize_buffered_ring(
                    canvas, np.asarray(ring, dtype=np.float64), to_pixel, 2.0
                )
            pts = getattr(w, "points", None)
            if pts is not None and len(pts) >= 2:
                width_m = float(getattr(w, "width_m", 8.0) or 8.0)
                width_px = max(1, round((width_m + 4.0) / cell))
                pix_pts = [to_pixel(float(p[0]), float(p[1])) for p in pts]
                canvas.line(pix_pts, width=width_px)

    mask = canvas.to_mask()
    return mask, ox, oy, cell


def build_managed_item_data_json() -> dict[str, Any]:
    """Generate ``managedItemData.json`` registering all Central Russian species."""
    out: dict[str, Any] = {}
    for key, spec in sorted(SPECIES_DATA.items()):
        item_id = str(uuid.uuid5(_FOREST_NS, f"item_{key}"))
        entry: dict[str, Any] = {
            "name": key,
            "internalName": key,
            "class": spec.get("class", "TSForestItemData"),
            "persistentId": item_id,
            "shapeFile": spec["shapeFile"],
            "lodReflectScalar": float(spec.get("lodReflectScalar", 0.0)),
        }
        if "radius" in spec:
            entry["radius"] = spec["radius"]
        if "mass" in spec:
            entry["mass"] = spec["mass"]
        if "windScale" in spec:
            entry["windScale"] = spec["windScale"]
        if "snapRotationToTerrain" in spec:
            entry["snapRotationToTerrain"] = spec["snapRotationToTerrain"]
        if "dampingCoefficient" in spec:
            entry["dampingCoefficient"] = spec["dampingCoefficient"]
        out[key] = entry
    return out


def build_forest_materials_json() -> dict[str, Any]:
    """Generate ``art/forest/main.materials.json`` for tree textures."""
    out: dict[str, Any] = {}
    for key, spec in sorted(STOCK_TREE_MATERIALS.items()):
        mat_id = str(uuid.uuid5(_FOREST_NS, f"mat_{key}"))
        if spec.get("collisionProxy"):
            entry: dict[str, Any] = {
                "name": key,
                "mapTo": key,
                "class": "Material",
                "persistentId": mat_id,
                "Stages": [{}, {}, {}, {}],
                "annotation": "NATURE",
                "groundDepth": spec.get("groundDepth", 0.6),
                "groundType": spec.get("groundType", key),
            }
            out[key] = entry
            continue

        stage: dict[str, Any] = {}
        if "baseColorMap" in spec:
            stage["baseColorMap"] = spec["baseColorMap"]
        elif "colorMap" in spec:
            stage["baseColorMap"] = spec["colorMap"]
        if "normalMap" in spec:
            stage["normalMap"] = spec["normalMap"]
        if "roughnessMap" in spec:
            stage["roughnessMap"] = spec["roughnessMap"]
        if "ambientOcclusionMap" in spec:
            stage["ambientOcclusionMap"] = spec["ambientOcclusionMap"]
        if "opacityMap" in spec:
            stage["opacityMap"] = spec["opacityMap"]

        entry = {
            "name": key,
            "mapTo": key,
            "class": "Material",
            "persistentId": mat_id,
            "Stages": [stage, {}, {}, {}],
            "annotation": "NATURE",
            "groundType": spec.get("groundType", "LEAVES_THIN"),
            "materialTag0": "vegetation",
            "materialTag1": "beamng",
            "materialTag2": "Natural",
            "version": 1.5,
        }
        if spec.get("alphaTest"):
            entry["alphaTest"] = True
            entry["alphaRef"] = int(spec.get("alphaRef", 75))
        if spec.get("doubleSided"):
            entry["doubleSided"] = True
            entry["invertBackFaceNormals"] = True
        out[key] = entry
    return out


def _sample_ground(build: Any, x: float, y: float) -> float:
    """Return ground elevation at world point."""
    if build is None:
        return 0.0
    elevations = getattr(build, "elevations", None)
    if elevations is None:
        return 0.0
    n, ox, oy, cell = _resolve_grid_params(build)
    col = int(np.clip(round((x - ox) / cell), 0, n - 1))
    row = int(np.clip(round((y - oy) / cell), 0, n - 1))
    return float(elevations[row, col])


def _extract_valid_polygons(area: VegetationArea) -> list[Polygon]:
    """Build valid Polygon(s) from a VegetationArea, repairing self-intersections."""
    geom: Any = Polygon(area.ring, area.holes)
    if not geom.is_valid:
        geom = make_valid(geom)
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 0 else []
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty and g.area > 0]
    if hasattr(geom, "geoms"):
        return [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty and g.area > 0]
    return []


def _collect_corridor_points(
    roads: Sequence[Any] | None,
    railways: Sequence[Any] | None,
) -> np.ndarray | None:
    """Collect sample points from priority highways (falling back to all roads/railways)."""
    priority_chunks: list[np.ndarray] = []
    all_chunks: list[np.ndarray] = []

    if roads:
        for item in roads:
            pts = getattr(item, "points", None)
            if pts is None and isinstance(item, (list, tuple)):
                pts = item
            if pts is None or len(pts) == 0:
                continue
            arr = np.asarray(pts, dtype=np.float64)[:, :2]
            step = max(1, len(arr) // 16)
            sampled = arr[::step]
            all_chunks.append(sampled)
            hw = str(getattr(item, "highway", "") or "").strip().lower()
            if hw in _PRIORITY_HIGHWAY_CLASSES:
                priority_chunks.append(sampled)

    if priority_chunks:
        return np.vstack(priority_chunks)

    if railways:
        for item in railways:
            pts = getattr(item, "points", None)
            if pts is None and isinstance(item, (list, tuple)):
                pts = item
            if pts is None or len(pts) == 0:
                continue
            arr = np.asarray(pts, dtype=np.float64)[:, :2]
            step = max(1, len(arr) // 16)
            all_chunks.append(arr[::step])

    if not all_chunks:
        return None
    return np.vstack(all_chunks)


def _area_corridor_distance(area: VegetationArea, corridor_pts: np.ndarray | None) -> float:
    """Return distance (m) from ``area`` bounding box to nearest road/railway point."""
    if corridor_pts is None or corridor_pts.shape[0] == 0:
        return 0.0
    minx, miny, maxx, maxy = area.bounds
    dx = np.maximum(0.0, np.maximum(minx - corridor_pts[:, 0], corridor_pts[:, 0] - maxx))
    dy = np.maximum(0.0, np.maximum(miny - corridor_pts[:, 1], corridor_pts[:, 1] - maxy))
    return float(np.sqrt(np.min(dx * dx + dy * dy)))


def _populate_polygon(
    poly: Polygon,
    n_target: int,
    rng: np.random.Generator,
    species_keys: list[str],
    species_weights: np.ndarray,
    *,
    mask: np.ndarray,
    ox: float,
    oy: float,
    cell: float,
    elevations: Any,
    g_sz: int,
    g_ox: float,
    g_oy: float,
    g_cell: float,
    spacing_cells: dict[tuple[int, int], list[tuple[float, float]]],
    trees_by_type: dict[str, list[dict[str, Any]]],
) -> int:
    """Place up to ``n_target`` deterministic trees inside ``poly``."""
    if n_target <= 0:
        return 0
    minx, miny, maxx, maxy = poly.bounds
    if maxx <= minx or maxy <= miny:
        return 0

    n_mask = mask.shape[0]
    min_spacing_sq = _MIN_TREE_SPACING_M * _MIN_TREE_SPACING_M
    placed = 0
    max_attempts = max(n_target * 15, n_target + 200)
    attempts = 0

    while placed < n_target and attempts < max_attempts:
        rem_attempts = max_attempts - attempts
        rem_needed = n_target - placed
        batch_size = min(rem_attempts, max(rem_needed * 3, 64))
        attempts += batch_size

        cand_xs = rng.uniform(minx, maxx, size=batch_size)
        cand_ys = rng.uniform(miny, maxy, size=batch_size)

        cols = np.rint((cand_xs - ox) / cell).astype(np.int64)
        rows = np.rint((cand_ys - oy) / cell).astype(np.int64)
        valid = (cols >= 0) & (cols < n_mask) & (rows >= 0) & (rows < n_mask)
        if not np.any(valid):
            continue

        cand_xs = cand_xs[valid]
        cand_ys = cand_ys[valid]
        clear = ~mask[rows[valid], cols[valid]]
        if not np.any(clear):
            continue

        cand_xs = cand_xs[clear]
        cand_ys = cand_ys[clear]
        inside = contains_xy(poly, cand_xs, cand_ys)
        if not np.any(inside):
            continue

        cand_xs = cand_xs[inside]
        cand_ys = cand_ys[inside]

        for cx_f, cy_f in zip(cand_xs.tolist(), cand_ys.tolist(), strict=True):
            gx = math.floor(cx_f / _MIN_TREE_SPACING_M)
            gy = math.floor(cy_f / _MIN_TREE_SPACING_M)
            too_close = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for px, py in spacing_cells.get((gx + dx, gy + dy), ()):
                        if (cx_f - px) ** 2 + (cy_f - py) ** 2 < min_spacing_sq:
                            too_close = True
                            break
                    if too_close:
                        break
                if too_close:
                    break
            if too_close:
                continue

            if elevations is not None:
                ec = int(np.clip(round((cx_f - g_ox) / g_cell), 0, g_sz - 1))
                er = int(np.clip(round((cy_f - g_oy) / g_cell), 0, g_sz - 1))
                gz = float(elevations[er, ec])
            else:
                gz = 0.0

            sp_key = str(rng.choice(species_keys, p=species_weights))
            yaw = float(rng.uniform(0.0, 2 * math.pi))
            cos_y = math.cos(yaw)
            sin_y = math.sin(yaw)
            scale = float(round(rng.uniform(0.85, 1.25), 4))

            rot_mat = [
                round(cos_y, 6),
                round(sin_y, 6),
                0.0,
                round(-sin_y, 6),
                round(cos_y, 6),
                0.0,
                0.0,
                0.0,
                1.0,
            ]

            trees_by_type[sp_key].append(
                {
                    "ctxid": 0,
                    "pos": [round(cx_f, 4), round(cy_f, 4), round(gz, 4)],
                    "rotationMatrix": rot_mat,
                    "scale": scale,
                    "type": sp_key,
                }
            )
            spacing_cells.setdefault((gx, gy), []).append((cx_f, cy_f))
            placed += 1
            if placed >= n_target:
                break

    return placed


def export_vegetation(
    vegetation_areas: Sequence[VegetationArea],
    build: Any,
    *,
    level_id: str,
    level_dir: Path,
    roads: Sequence[Any] | None = None,
    buildings: Sequence[BuildingFootprint] | None = None,
    water: Sequence[Any] | None = None,
    railways: Sequence[Any] | None = None,
    max_trees: int = 35000,
    target_density_trees_per_ha: float = 120.0,
) -> tuple[list[dict[str, Any]], VegetationExportStats]:
    """Generate BeamNG .forest4.json, managedItemData.json, materials, and scene objects.

    Uses deterministic PCG64(osm_id) placement, forest-first & priority-highway
    proximity ordering, 2-pass allocation so roadside forests are never starved,
    vectorized ``shapely.contains_xy`` candidate filtering, minimum trunk
    spacing, and fast raster keep-out masking.
    """
    stats = VegetationExportStats()
    forest_dir = level_dir / "forest"
    art_forest_dir = level_dir / "art" / "forest"
    forest_dir.mkdir(parents=True, exist_ok=True)
    art_forest_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale .forest4.json files from previous builds
    for stale_forest in forest_dir.glob("*.forest4.json"):
        stale_forest.unlink()

    # 1. Write managedItemData.json and main.materials.json
    mid_path = art_forest_dir / "managedItemData.json"
    mid_data = build_managed_item_data_json()
    mid_text = json.dumps(mid_data, indent=2, ensure_ascii=False)
    mid_path.write_text(mid_text, encoding="utf-8")
    stats.files_written += 1
    stats.bytes_written += len(mid_text)

    mat_path = art_forest_dir / "main.materials.json"
    mat_data = build_forest_materials_json()
    mat_text = json.dumps(mat_data, indent=2, ensure_ascii=False)
    mat_path.write_text(mat_text, encoding="utf-8")
    stats.files_written += 1
    stats.bytes_written += len(mat_text)

    # 2. Build keep-out mask
    mask, ox, oy, cell = build_keepout_mask(
        build, roads=roads, buildings=buildings, water=water, railways=railways
    )
    n_mask = mask.shape[0]
    elevations = getattr(build, "elevations", None) if build is not None else None
    g_sz, g_ox, g_oy, g_cell = _resolve_grid_params(build)

    # 3. Deterministic 2-pass placement per kind tier (forest > scrub > meadow)
    # Within each tier, Pass 1 caps per-polygon count at _PASS1_MAX_TREES_PER_POLY
    # so every roadside polygon receives coverage before Pass 2 fills remaining deficits.
    trees_by_type: dict[str, list[dict[str, Any]]] = {k: [] for k in SPECIES_DATA}
    total_placed = 0
    spacing_cells: dict[tuple[int, int], list[tuple[float, float]]] = {}

    corridor_pts = _collect_corridor_points(roads, railways)
    areas_by_tier: dict[int, list[VegetationArea]] = {}
    for area in vegetation_areas:
        tier = _KIND_PRIORITY.get(area.kind, 2)
        areas_by_tier.setdefault(tier, []).append(area)

    for tier in sorted(areas_by_tier):
        if total_placed >= max_trees:
            break

        tier_areas = sorted(
            areas_by_tier[tier],
            key=lambda a: (
                int(_area_corridor_distance(a, corridor_pts) // _CORRIDOR_BUCKET_M),
                -a.area_m2,
            ),
        )
        pass2_queue: list[tuple[Polygon, int, np.random.Generator, list[str], np.ndarray]] = []

        for area in tier_areas:
            if total_placed >= max_trees:
                break

            polys = _extract_valid_polygons(area)
            if not polys:
                continue

            if area.kind == "forest":
                density = target_density_trees_per_ha
                species_dist = FOREST_SPECIES_DISTRIBUTION
                min_target = 1
            elif area.kind == "scrub":
                density = target_density_trees_per_ha * 0.4
                species_dist = SCRUB_SPECIES_DISTRIBUTION
                min_target = 1
            else:  # meadow / grass: sparse bushes only if budget remains after forests
                density = target_density_trees_per_ha * 0.05
                species_dist = SCRUB_SPECIES_DISTRIBUTION
                min_target = 0

            rng = np.random.Generator(np.random.PCG64(abs(int(area.osm_id))))
            species_keys = [s[0] for s in species_dist]
            species_weights = np.array([s[1] for s in species_dist], dtype=np.float64)
            species_weights /= species_weights.sum()

            for poly in polys:
                if total_placed >= max_trees:
                    break
                n_total = max(min_target, round((poly.area / 10000.0) * density))
                if n_total <= 0:
                    continue

                p1_target = min(n_total, _PASS1_MAX_TREES_PER_POLY, max_trees - total_placed)
                placed_p1 = _populate_polygon(
                    poly,
                    p1_target,
                    rng,
                    species_keys,
                    species_weights,
                    mask=mask,
                    ox=ox,
                    oy=oy,
                    cell=cell,
                    elevations=elevations,
                    g_sz=g_sz,
                    g_ox=g_ox,
                    g_oy=g_oy,
                    g_cell=g_cell,
                    spacing_cells=spacing_cells,
                    trees_by_type=trees_by_type,
                )
                total_placed += placed_p1
                if n_total > placed_p1 and placed_p1 >= _PASS1_MAX_TREES_PER_POLY:
                    pass2_queue.append(
                        (poly, n_total - placed_p1, rng, species_keys, species_weights)
                    )

        for poly, remaining, rng, species_keys, species_weights in pass2_queue:
            if total_placed >= max_trees:
                break
            p2_target = min(remaining, max_trees - total_placed)
            total_placed += _populate_polygon(
                poly,
                p2_target,
                rng,
                species_keys,
                species_weights,
                mask=mask,
                ox=ox,
                oy=oy,
                cell=cell,
                elevations=elevations,
                g_sz=g_sz,
                g_ox=g_ox,
                g_oy=g_oy,
                g_cell=g_cell,
                spacing_cells=spacing_cells,
                trees_by_type=trees_by_type,
            )

    # 4. Write .forest4.json for each species with instances
    for sp_key, items in sorted(trees_by_type.items()):
        if not items:
            continue
        file_path = forest_dir / f"{sp_key}.forest4.json"
        lines = [json.dumps(item, separators=(",", ":"), ensure_ascii=False) for item in items]
        content = "\n".join(lines) + "\n"
        file_path.write_text(content, encoding="utf-8")
        stats.files_written += 1
        stats.bytes_written += len(content)
        stats.by_species[sp_key] = len(items)

    stats.total_trees = total_placed
    stats.species_types = sum(1 for items in trees_by_type.values() if items)

    # 5. Emit scene objects for level SceneTree
    # BeamNG 0.39 auto-discovers levels/<level_id>/forest/*.forest4.json;
    # official levels (e.g. east_coast_usa) omit dataFile on theForest.
    scene_objects: list[dict[str, Any]] = [
        {
            "class": "Forest",
            "name": "theForest",
            "persistentId": stable_uuid(level_id, "forest", "theForest"),
            "__parent": "vegetation",
            "lodReflectScalar": 0.0,
        },
        {
            "class": "ForestWindEmitter",
            "name": "windEmitter",
            "persistentId": stable_uuid(level_id, "forest", "windEmitter"),
            "__parent": "vegetation",
            "position": [round(ox + cell * (n_mask / 2.0), 4), round(oy + cell * (n_mask / 2.0), 4), 100.0],
            "strength": 0.15,
            "gustYawFrequency": 0.1,
        },
    ]

    return scene_objects, stats
