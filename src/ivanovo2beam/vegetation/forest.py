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
from shapely.geometry import Point, Polygon

try:
    from PIL import Image, ImageDraw

    # Quick probe to ensure C extension (_imaging) is actually usable
    _probe = Image.new("1", (1, 1), 0)
    _HAS_PIL = True
except (ImportError, OSError, Exception):
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    _HAS_PIL = False

from ivanovo2beam.beamng.level import stable_uuid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ivanovo2beam.buildings.footprints import BuildingFootprint
    from ivanovo2beam.vegetation.extract import VegetationArea

#: Deterministic UUID namespace for forest items and materials.
_FOREST_NS = uuid.UUID("9c42a5e0-4182-41f6-b192-3a55b1f0981b")

#: Central Russian species definitions referencing base-game BeamNG meshes.
SPECIES_DATA: dict[str, dict[str, Any]] = {
    # 1. Silver Birch (Берёза, 35% in forest)
    "tree_aspen_large_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/birch/tree_aspen_large_a.dae",
        "radius": 0.35,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "birch",
    },
    "tree_aspen_small_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/birch/tree_aspen_small_a.dae",
        "radius": 0.30,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "birch",
    },
    "tree_aspen_forest_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/birch/tree_aspen_forest_a.dae",
        "radius": 0.35,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "birch",
    },
    # 2. Scots Pine (Сосна обыкновенная, 30% in forest)
    "scots_pine": {
        "class": "TSForestItemData",
        "shapeFile": "/levels/italy/art/shapes/trees/trees_italy/scots_pine.dae",
        "radius": 0.40,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "pine",
    },
    "scots_pine_filler": {
        "class": "TSForestItemData",
        "shapeFile": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_filler.dae",
        "radius": 0.35,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "pine",
    },
    # 3. Spruce / Fir (Ель, 20% in forest)
    "tree_douglasfir_large_a": {
        "class": "TSForestItemData",
        "shapeFile": "/levels/east_coast_usa/art/shapes/trees/trees_douglasfir/tree_douglasfir_large_a.dae",
        "radius": 0.45,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "fir",
    },
    "tree_douglasfir_small_a": {
        "class": "TSForestItemData",
        "shapeFile": "/levels/east_coast_usa/art/shapes/trees/trees_douglasfir/tree_douglasfir_small_a.dae",
        "radius": 0.35,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "fir",
    },
    # 4. Oak & Mixed Broadleaf (Дуб, 15% in forest)
    "tree_oak_large_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/oak/tree_oak_large_a.dae",
        "radius": 0.50,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "oak",
    },
    "tree_beech_large_b": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/beech/tree_beech_large_b.dae",
        "radius": 0.45,
        "mass": 10.0,
        "windScale": 0.5,
        "category": "oak",
    },
    # 5. Undergrowth (Подлесок / scrub / forest edges)
    "generibush": {
        "class": "ForestItemData",
        "shapeFile": "/assets/meshes/foliage/bushes/generibush.dae",
        "radius": 0.25,
        "snapRotationToTerrain": True,
        "category": "bush",
    },
    "generibush_small": {
        "class": "ForestItemData",
        "shapeFile": "/assets/meshes/foliage/bushes/generibush_small.dae",
        "radius": 0.20,
        "snapRotationToTerrain": True,
        "category": "bush",
    },
    "tree_aspen_bush_a": {
        "class": "ForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/birch/tree_aspen_bush_a.dae",
        "radius": 0.30,
        "snapRotationToTerrain": True,
        "category": "bush",
    },
}

#: Category weights in Central Russian forests
FOREST_SPECIES_DISTRIBUTION: list[tuple[str, float]] = [
    ("tree_aspen_large_a", 0.15),
    ("tree_aspen_small_a", 0.10),
    ("tree_aspen_forest_a", 0.10),
    ("scots_pine", 0.20),
    ("scots_pine_filler", 0.10),
    ("tree_douglasfir_large_a", 0.12),
    ("tree_douglasfir_small_a", 0.08),
    ("tree_oak_large_a", 0.10),
    ("tree_beech_large_b", 0.05),
]

SCRUB_SPECIES_DISTRIBUTION: list[tuple[str, float]] = [
    ("generibush", 0.40),
    ("generibush_small", 0.30),
    ("tree_aspen_bush_a", 0.20),
    ("tree_aspen_small_a", 0.05),
    ("scots_pine_filler", 0.05),
]

#: Native tree materials referenced by stock meshes
STOCK_TREE_MATERIALS: dict[str, dict[str, Any]] = {
    # Birch
    "Birch_Bark_01": {
        "colorMap": "/assets/materials/tree/birch/t_birch_bark_01_b.color.png",
        "normalMap": "/assets/materials/tree/birch/t_birch_bark_01_nm.normal.png",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_bark_01_r.data.png",
        "groundType": "WOOD",
    },
    "Birch_Branch_01": {
        "colorMap": "/assets/materials/tree/birch/t_birch_branch_01_b.color.png",
        "normalMap": "/assets/materials/tree/birch/t_birch_branch_01_nm.normal.png",
        "opacityMap": "/assets/materials/tree/birch/t_birch_branch_01_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "m_ind_birch_leaves_01": {
        "colorMap": "/assets/materials/tree/birch/t_ind_birch_leaves_01_b.color.png",
        "normalMap": "/assets/materials/tree/birch/t_ind_birch_leaves_01_nm.normal.png",
        "opacityMap": "/assets/materials/tree/birch/t_ind_birch_leaves_01_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    # Pine
    "scots_pine": {
        "colorMap": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_b.color.png",
        "normalMap": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_nm.normal.png",
        "opacityMap": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "scots_pine_branches": {
        "colorMap": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_branches_b.color.png",
        "normalMap": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_branches_nm.normal.png",
        "opacityMap": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_branches_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "scots_pine_trunk": {
        "colorMap": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_trunk_b.color.png",
        "normalMap": "/levels/italy/art/shapes/trees/trees_italy/scots_pine_trunk_nm.normal.png",
        "groundType": "WOOD",
    },
    # Fir
    "fir_bark": {
        "colorMap": "/assets/materials/tree/fir/t_fir_bark/t_fir_bark_b.color.png",
        "normalMap": "/assets/materials/tree/fir/t_fir_bark/t_fir_bark_nm.normal.png",
        "roughnessMap": "/assets/materials/tree/fir/t_fir_bark/t_fir_bark_r.data.png",
        "groundType": "WOOD",
    },
    "fir_leaves": {
        "colorMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_b.color.png",
        "normalMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_nm.normal.png",
        "opacityMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "fir_leaves_dead": {
        "colorMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_dead_b.color.png",
        "normalMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_nm.normal.png",
        "opacityMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "m_fir_leaves_distant": {
        "colorMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_b.color.png",
        "normalMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_nm.normal.png",
        "opacityMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    # Oak
    "Oak_bark_01": {
        "colorMap": "/assets/materials/tree/oak/t_oak_bark_01/t_oak_bark_01_b.color.png",
        "normalMap": "/assets/materials/tree/oak/t_oak_bark_01/t_oak_bark_01_nm.normal.png",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_bark_01/t_oak_bark_01_r.data.png",
        "groundType": "WOOD",
    },
    "Oak_branch_01": {
        "colorMap": "/assets/materials/tree/oak/t_oak_branch_01/t_oak_branch_01_b.color.png",
        "normalMap": "/assets/materials/tree/oak/t_oak_branch_01/t_oak_branch_01_nm.normal.png",
        "opacityMap": "/assets/materials/tree/oak/t_oak_branch_01/t_oak_branch_01_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "Oak_branch_02": {
        "colorMap": "/assets/materials/tree/oak/t_oak_branch_02/t_oak_branch_02_b.color.png",
        "normalMap": "/assets/materials/tree/oak/t_oak_branch_02/t_oak_branch_02_nm.normal.png",
        "opacityMap": "/assets/materials/tree/oak/t_oak_branch_02/t_oak_branch_02_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "m_oak_leaves_01": {
        "colorMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_01_b.color.png",
        "normalMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_01_nm.normal.png",
        "opacityMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_01_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    # Beech
    "beech_trunk": {
        "colorMap": "/assets/materials/tree/beech/t_beech_trunk_b.color.png",
        "normalMap": "/assets/materials/tree/beech/t_beech_trunk_nm.normal.png",
        "groundType": "WOOD",
    },
    "beech_branch_01": {
        "colorMap": "/assets/materials/tree/beech/t_beech_branch_01_b.color.png",
        "normalMap": "/assets/materials/tree/beech/t_beech_branch_01_nm.normal.png",
        "opacityMap": "/assets/materials/tree/beech/t_beech_branch_01_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "m_ind_beech_leaves": {
        "colorMap": "/assets/materials/tree/beech/t_beech_leaves_b.color.png",
        "normalMap": "/assets/materials/tree/beech/t_beech_leaves_nm.normal.png",
        "opacityMap": "/assets/materials/tree/beech/t_beech_leaves_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "m_ind_beech_blocker": {
        "colorMap": "/assets/materials/tree/beech/t_beech_leaves_b.color.png",
        "normalMap": "/assets/materials/tree/beech/t_beech_leaves_nm.normal.png",
        "opacityMap": "/assets/materials/tree/beech/t_beech_leaves_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    # Bushes & Leaves collision proxies
    "generibush_leaves": {
        "colorMap": "/assets/materials/foliage/bush/t_generibush_leaves/t_generibush_leaves_b.color.png",
        "normalMap": "/assets/materials/foliage/bush/t_generibush_leaves/t_generibush_leaves_nm.normal.png",
        "opacityMap": "/assets/materials/foliage/bush/t_generibush_leaves/t_generibush_leaves_o.data.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "leaves_strong": {
        "colorMap": "/assets/materials/tree/birch/t_birch_branch_01_b.color.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
    },
    "leaves_thin": {
        "colorMap": "/assets/materials/tree/birch/t_birch_branch_01_b.color.png",
        "alphaTest": True,
        "doubleSided": True,
        "groundType": "LEAVES_THIN",
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

    Burns road corridors + shoulders, railways, water bodies, and building footprints
    into a boolean raster mask for O(1) clearance checks.
    """
    if build is not None:
        elevations = getattr(build, "elevations", None)
        sz = elevations.shape[0] if elevations is not None else (grid_size or 2048)
        ox = getattr(build, "origin_x", origin_x or 0.0)
        oy = getattr(build, "origin_y", origin_y or 0.0)
        cell = getattr(build, "cell_size_m", cell_size_m or 1.25)
    else:
        sz = grid_size or 2048
        ox = origin_x or 0.0
        oy = origin_y or 0.0
        cell = cell_size_m or 1.25

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
            width_m = getattr(road, "width_m", 6.0)
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

    # 3. Buildings
    if buildings:
        for bld in buildings:
            ring = getattr(bld, "ring", None)
            if ring is None and isinstance(bld, np.ndarray):
                ring = bld
            if ring is None or len(ring) < 3:
                continue
            pix_pts = [to_pixel(float(p[0]), float(p[1])) for p in ring]
            canvas.polygon(pix_pts)

    # 4. Water
    if water:
        for w in water:
            ring = getattr(w, "ring", None)
            if ring is None and isinstance(w, np.ndarray):
                ring = w
            if ring is not None and len(ring) >= 3:
                pix_pts = [to_pixel(float(p[0]), float(p[1])) for p in ring]
                canvas.polygon(pix_pts)
            pts = getattr(w, "points", None)
            if pts is not None and len(pts) >= 2:
                width_m = getattr(w, "width_m", 8.0)
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
        stage: dict[str, Any] = {}
        if "baseColorMap" in spec:
            stage["baseColorMap"] = spec["baseColorMap"]
        elif "colorMap" in spec:
            stage["baseColorMap"] = spec["colorMap"]
        if "normalMap" in spec:
            stage["normalMap"] = spec["normalMap"]
        if "roughnessMap" in spec:
            stage["roughnessMap"] = spec["roughnessMap"]
        if "opacityMap" in spec:
            stage["opacityMap"] = spec["opacityMap"]

        entry: dict[str, Any] = {
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
            entry["alphaRef"] = 75
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
    ox = getattr(build, "origin_x", 0.0)
    oy = getattr(build, "origin_y", 0.0)
    cell = getattr(build, "cell_size_m", 1.0)
    n = elevations.shape[0]
    col = int(np.clip(round((x - ox) / cell), 0, n - 1))
    row = int(np.clip(round((y - oy) / cell), 0, n - 1))
    return float(elevations[row, col])


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
    max_trees: int = 80000,
    target_density_trees_per_ha: float = 120.0,
) -> tuple[list[dict[str, Any]], VegetationExportStats]:
    """Generate BeamNG .forest4.json, managedItemData.json, materials, and scene objects.

    Uses deterministic PCG64(osm_id) placement and fast raster keep-out masking.
    """
    stats = VegetationExportStats()
    forest_dir = level_dir / "forest"
    art_forest_dir = level_dir / "art" / "forest"
    forest_dir.mkdir(parents=True, exist_ok=True)
    art_forest_dir.mkdir(parents=True, exist_ok=True)

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

    # 3. Deterministic placement per area
    trees_by_type: dict[str, list[dict[str, Any]]] = {k: [] for k in SPECIES_DATA}
    total_placed = 0

    # Sort areas by area_m2 descending
    sorted_areas = sorted(vegetation_areas, key=lambda a: -a.area_m2)

    for area in sorted_areas:
        if total_placed >= max_trees:
            break

        poly = Polygon(area.ring, area.holes)
        if not poly.is_valid or poly.area <= 0:
            continue

        minx, miny, maxx, maxy = area.bounds
        width = maxx - minx
        height = maxy - miny
        if width <= 0 or height <= 0:
            continue

        # Density based on kind
        if area.kind == "forest":
            density = target_density_trees_per_ha
            species_dist = FOREST_SPECIES_DISTRIBUTION
        elif area.kind == "scrub":
            density = target_density_trees_per_ha * 0.4
            species_dist = SCRUB_SPECIES_DISTRIBUTION
        else:  # meadow / grass
            density = target_density_trees_per_ha * 0.15
            species_dist = SCRUB_SPECIES_DISTRIBUTION

        n_target = max(1, round((area.area_m2 / 10000.0) * density))
        n_target = min(n_target, max_trees - total_placed)

        # Deterministic RNG per OSM polygon
        seed = abs(int(area.osm_id))
        rng = np.random.Generator(np.random.PCG64(seed))

        species_keys = [s[0] for s in species_dist]
        species_weights = np.array([s[1] for s in species_dist], dtype=np.float64)
        species_weights /= species_weights.sum()

        placed_in_area = 0
        max_attempts = n_target * 5
        attempts = 0

        while placed_in_area < n_target and attempts < max_attempts:
            attempts += 1
            cand_x = float(rng.uniform(minx, maxx))
            cand_y = float(rng.uniform(miny, maxy))

            # Polygon point-in-polygon check
            if not poly.contains(Point(cand_x, cand_y)):
                continue

            # Keep-out mask check
            col = round((cand_x - ox) / cell)
            row = round((cand_y - oy) / cell)
            if 0 <= col < n_mask and 0 <= row < n_mask and mask[row, col]:
                continue

            gz = _sample_ground(build, cand_x, cand_y)
            sp_key = str(rng.choice(species_keys, p=species_weights))

            yaw = float(rng.uniform(0.0, 2 * math.pi))
            cos_y = math.cos(yaw)
            sin_y = math.sin(yaw)
            scale = float(round(rng.uniform(0.85, 1.25), 4))

            # Standard BeamNG rotation matrix [c, s, 0, -s, c, 0, 0, 0, 1]
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

            tree_entry = {
                "ctxid": 0,
                "pos": [round(cand_x, 4), round(cand_y, 4), round(gz, 4)],
                "rotationMatrix": rot_mat,
                "scale": scale,
                "type": sp_key,
            }

            trees_by_type[sp_key].append(tree_entry)
            placed_in_area += 1
            total_placed += 1
            if total_placed >= max_trees:
                break

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
    scene_objects: list[dict[str, Any]] = [
        {
            "class": "Forest",
            "name": "theForest",
            "persistentId": stable_uuid(level_id, "forest", "theForest"),
            "__parent": "vegetation",
            "lodReflectScalar": 0.15,
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
