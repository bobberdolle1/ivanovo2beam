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
import os
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely import STRtree, contains_xy, unary_union
from shapely import distance as shapely_distance
from shapely import points as shapely_points
from shapely.geometry import LineString, MultiPolygon, Polygon, box
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
from ivanovo2beam.vegetation.extract import VegetationArea

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ivanovo2beam.buildings.footprints import BuildingFootprint

#: Deterministic UUID namespace for forest items and materials.
_FOREST_NS = uuid.UUID("9c42a5e0-4182-41f6-b192-3a55b1f0981b")

#: Priority order for area kinds when allocating max_trees budget.
_KIND_PRIORITY: dict[str, int] = {
    "forest": 0,
    "scrub": 1,
    "meadow": 2,
    "farmland": 3,
}

#: Road-corridor distance bucket (m) for prioritizing roadside forests.
_CORRIDOR_BUCKET_M = 250.0

#: Spatial density zoning distances and target densities (stems / ha).
_ROADSIDE_ZONE_M = 60.0
_FOREST_EDGE_BUFFER_M = 35.0
_BACKDROP_DISTANCE_M = 400.0
_DEFAULT_ROADSIDE_DENSITY_PER_HA = 200.0
_DEFAULT_CORE_DENSITY_PER_HA = 125.0
_DEFAULT_BACKDROP_DENSITY_PER_HA = 50.0
_DEFAULT_EDGE_UNDERGROWTH_PER_HA = 100.0
_DEFAULT_CORE_FERN_PER_HA = 120.0
_DEFAULT_CORE_BUSH_FALLBACK_PER_HA = 20.0

#: Minimum groundcover fern spacing (m) and minimum clearance from woody trunks (m).
_FERN_SPACING_M = 0.9
_FERN_TRUNK_CLEARANCE_M = 0.8

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

#: Standard BeamNG install directories probed when auto-detecting .link sources.
_DEFAULT_BEAMNG_CANDIDATES: tuple[Path, ...] = (
    Path(r"F:\SteamLibrary\steamapps\common\BeamNG.drive"),
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive"),
    Path(r"D:\SteamLibrary\steamapps\common\BeamNG.drive"),
)

#: Offline Central Russian summer mix (10 species: birch, beech, oak, bushes) using /assets/.
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
    # 3. English Oak (Дуб)
    "tree_oak_forest_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/oak/tree_oak_forest_a.dae",
        "radius": 0.50,
        "mass": 100.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "oak",
    },
    "tree_oak_sml_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/oak/tree_oak_sml_a.dae",
        "radius": 0.35,
        "mass": 20.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "oak",
    },
    "tree_oak_bush_a": {
        "class": "TSForestItemData",
        "shapeFile": "/assets/meshes/foliage/trees_library/oak/tree_oak_bush_a.dae",
        "radius": 0.30,
        "mass": 2.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "bush",
    },
}

#: Stock tree/fern shapes linked from east_coast_usa & italy via .link files.
#: Linking east_coast_usa birch/beech/oak ships their 1024x1024 pre-baked summer
#: green .imposter.dds files cleanly (avoiding global temp/assets/ cache).
#: Notice ``scots_pine_tree`` avoids colliding with Material ``scots_pine``.
LINKED_SPECIES_DATA: dict[str, dict[str, Any]] = {
    "tree_aspen_forest_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_aspen/tree_aspen_forest_a.dae",
        "radius": 0.35,
        "mass": 40.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "birch",
    },
    "tree_aspen_forest_b": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_aspen/tree_aspen_forest_b.dae",
        "radius": 0.35,
        "mass": 45.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "birch",
    },
    "tree_aspen_large_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_aspen/tree_aspen_large_a.dae",
        "radius": 0.35,
        "mass": 50.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "birch",
    },
    "tree_aspen_small_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_aspen/tree_aspen_small_a.dae",
        "radius": 0.30,
        "mass": 15.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "birch",
    },
    "tree_beech_forest_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_beech/tree_beech_forest_a.dae",
        "radius": 0.40,
        "mass": 20.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "beech",
    },
    "tree_beech_small_b": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_beech/tree_beech_small_b.dae",
        "radius": 0.32,
        "mass": 10.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "beech",
    },
    "tree_beech_bush_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_beech/tree_beech_bush_a.dae",
        "radius": 0.30,
        "mass": 2.0,
        "windScale": 0.4,
        "snapRotationToTerrain": True,
        "lodReflectScalar": 0.0,
        "category": "bush",
    },
    "tree_oak_forest_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_oak/tree_oak_forest_a.dae",
        "radius": 0.50,
        "mass": 100.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "oak",
    },
    "tree_oak_sml_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_oak/tree_oak_sml_a.dae",
        "radius": 0.35,
        "mass": 20.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "oak",
    },
    "tree_oak_bush_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_oak/tree_oak_bush_a.dae",
        "radius": 0.30,
        "mass": 2.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "bush",
    },
    "tree_douglasfir_large_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_douglasfir/tree_douglasfir_large_a.dae",
        "radius": 0.50,
        "mass": 70.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "conifer",
    },
    "tree_douglasfir_small_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_douglasfir/tree_douglasfir_small_a.dae",
        "radius": 0.35,
        "mass": 15.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "conifer",
    },
    "tree_douglasfir_small_b": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_douglasfir/tree_douglasfir_small_b.dae",
        "radius": 0.35,
        "mass": 10.0,
        "windScale": 0.4,
        "lodReflectScalar": 0.0,
        "category": "conifer",
    },
    "tree_douglasfir_bush_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/trees/trees_douglasfir/tree_douglasfir_bush_a.dae",
        "radius": 0.25,
        "mass": 2.0,
        "windScale": 0.5,
        "lodReflectScalar": 0.0,
        "category": "conifer_bush",
    },
    "scots_pine_tree": {
        "class": "TSForestItemData",
        "sourceShape": "levels/italy/art/shapes/trees/trees_italy/scots_pine.dae",
        "radius": 0.60,
        "mass": 10.0,
        "windScale": 0.5,
        "lodReflectScalar": 0.0,
        "category": "conifer",
    },
    "lush_ferns_01_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/groundcover/lush_ferns_01_a.dae",
        "radius": 0.25,
        "mass": 2.0,
        "windScale": 0.4,
        "snapRotationToTerrain": True,
        "lodReflectScalar": 0.0,
        "category": "fern",
    },
    "lush_ferns_02_a": {
        "class": "TSForestItemData",
        "sourceShape": "levels/east_coast_usa/art/shapes/groundcover/lush_ferns_02_a.dae",
        "radius": 0.20,
        "mass": 1.0,
        "windScale": 0.4,
        "snapRotationToTerrain": True,
        "lodReflectScalar": 0.0,
        "category": "fern",
    },
}

#: Category weights in offline broadleaved forests (birch 58%, beech 24%, oak 18%).
FOREST_SPECIES_DISTRIBUTION: list[tuple[str, float]] = [
    ("tree_aspen_large_a", 0.18),
    ("tree_aspen_forest_a", 0.16),
    ("tree_aspen_forest_b", 0.12),
    ("tree_aspen_small_a", 0.12),
    ("tree_beech_forest_a", 0.14),
    ("tree_beech_small_b", 0.10),
    ("tree_oak_forest_a", 0.10),
    ("tree_oak_sml_a", 0.08),
]

#: Central Russian mixed summer canopy when linked conifers are active.
FOREST_MIXED_CONIFER_DISTRIBUTION: list[tuple[str, float]] = [
    ("tree_aspen_large_a", 0.13),
    ("tree_aspen_forest_a", 0.12),
    ("tree_aspen_forest_b", 0.10),
    ("tree_aspen_small_a", 0.09),
    ("tree_beech_forest_a", 0.10),
    ("tree_beech_small_b", 0.07),
    ("tree_oak_forest_a", 0.07),
    ("tree_oak_sml_a", 0.05),
    ("tree_douglasfir_large_a", 0.08),
    ("tree_douglasfir_small_a", 0.06),
    ("tree_douglasfir_small_b", 0.06),
    ("scots_pine_tree", 0.07),
]

#: Needle-leaved forest weights when OSM ``leaf_type=needleleaved`` and conifers exist.
FOREST_NEEDLE_DISTRIBUTION: list[tuple[str, float]] = [
    ("tree_douglasfir_large_a", 0.26),
    ("tree_douglasfir_small_a", 0.18),
    ("tree_douglasfir_small_b", 0.16),
    ("scots_pine_tree", 0.22),
    ("tree_aspen_forest_a", 0.10),
    ("tree_aspen_small_a", 0.08),
]

SCRUB_SPECIES_DISTRIBUTION: list[tuple[str, float]] = [
    ("tree_beech_bush_a", 0.50),
    ("tree_oak_bush_a", 0.25),
    ("tree_aspen_small_a", 0.15),
    ("tree_beech_small_b", 0.10),
]

SCRUB_WITH_CONIFER_DISTRIBUTION: list[tuple[str, float]] = [
    ("tree_beech_bush_a", 0.40),
    ("tree_oak_bush_a", 0.22),
    ("tree_douglasfir_bush_a", 0.20),
    ("tree_aspen_small_a", 0.10),
    ("tree_beech_small_b", 0.08),
]

FERN_SPECIES_DISTRIBUTION: list[tuple[str, float]] = [
    ("lush_ferns_01_a", 0.55),
    ("lush_ferns_02_a", 0.45),
]

#: Verified summer tree and groundcover materials referencing direct /assets/ DDS textures.
STOCK_TREE_MATERIALS: dict[str, dict[str, Any]] = {
    # Birch (summer green t_birch_leaves — never autumn/industrial t_birch_leaves_ind!)
    "Birch_Bark_01": {
        "baseColorMap": "/assets/materials/tree/birch/t_birch_bark/t_birch_bark_b.color.dds",
        "normalMap": "/assets/materials/tree/birch/t_birch_bark/t_birch_bark_nm.normal.dds",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_bark/t_birch_bark_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/birch/t_birch_bark/t_birch_bark_ao.data.dds",
        "baseColorFactor": [0.854701996, 0.854701996, 0.854701996, 1.0],
        "groundType": "WOOD",
    },
    "Birch_Branch_01": {
        "baseColorMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_b.color.dds",
        "normalMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_o.data.dds",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/birch/t_birch_branch/t_birch_branch_ao.data.dds",
        "baseColorFactor": [0.608137012, 0.608137012, 0.608137012, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 60,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 0.01,
        "groundType": "WOOD",
    },
    "m_ind_birch_leaves_01": {
        "baseColorMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_b.color.dds",
        "normalMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_ao.data.dds",
        "baseColorFactor": [0.996078491, 0.996078491, 0.996078491, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 100,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    "Birch_Leaves_01": {
        "baseColorMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_b.color.dds",
        "normalMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/birch/t_birch_leaves/t_birch_leaves_ao.data.dds",
        "baseColorFactor": [0.996078491, 0.996078491, 0.996078491, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 100,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    "m_birch_leaves_distant": {
        "baseColorMap": "/assets/materials/tree/birch/t_birch_leaves_distant/t_birch_leaves_distant_b.color.dds",
        "normalMap": "/assets/materials/tree/birch/t_birch_leaves_distant/t_birch_leaves_distant_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/birch/t_birch_leaves_distant/t_birch_leaves_distant_o.data.dds",
        "roughnessMap": "/assets/materials/tree/birch/t_birch_leaves_distant/t_birch_leaves_distant_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/birch/t_birch_leaves_distant/t_birch_leaves_distant_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 80,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    # Beech (summer green)
    "beech_trunk": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_trunk/t_beech_bark_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_trunk/t_beech_bark_nm.normal.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_trunk/t_beech_bark_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_trunk/t_beech_bark_ao.data.dds",
        "baseColorFactor": [0.872313976, 0.854497015, 0.757090986, 1.0],
        "groundType": "WOOD",
    },
    "beech_branch_01": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_branch_01/t_beech_branch_ao.data.dds",
        "baseColorFactor": [0.766642988, 0.766642988, 0.766642988, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 75,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 0.01,
        "groundType": "WOOD",
    },
    "beech_leaves": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_ao.data.dds",
        "baseColorFactor": [0.992156923, 0.992156923, 0.992156923, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 105,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    "beech_blocker": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_ao.data.dds",
        "baseColorFactor": [0.769913971, 0.828284979, 0.682407975, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 90,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "WOOD",
    },
    # Compiled .cdae material names for beech meshes
    "m_ind_beech_leaves": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_leaves/t_beech_leaves_ao.data.dds",
        "baseColorFactor": [0.992156923, 0.992156923, 0.992156923, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 105,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    "m_ind_beech_blocker": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_blocker/t_beech_blocker_ao.data.dds",
        "baseColorFactor": [0.769913971, 0.828284979, 0.682407975, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 90,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "WOOD",
    },
    "m_beech_leaves_distant": {
        "baseColorMap": "/assets/materials/tree/beech/t_beech_leaves_distant/t_beech_leaves_distant_b.color.dds",
        "normalMap": "/assets/materials/tree/beech/t_beech_leaves_distant/t_beech_leaves_distant_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/beech/t_beech_leaves_distant/t_beech_leaves_distant_o.data.dds",
        "roughnessMap": "/assets/materials/tree/beech/t_beech_leaves_distant/t_beech_leaves_distant_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/beech/t_beech_leaves_distant/t_beech_leaves_distant_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 80,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    # Oak (summer green t_oak_leaves_02)
    "Oak_bark_01": {
        "baseColorMap": "/assets/materials/tree/oak/t_oak_bark/t_oak_bark_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_oak_bark/t_oak_bark_nm.normal.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_bark/t_oak_bark_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_oak_bark/t_oak_bark_ao.data.dds",
        "baseColorFactor": [0.862744987, 0.862744987, 0.862744987, 1.0],
        "groundType": "WOOD",
    },
    "Oak_branch_01": {
        "baseColorMap": "/assets/materials/tree/oak/t_oak_branch_01/t_oak_branch_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_oak_branch_01/t_oak_branch_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/oak/t_oak_branch_01/t_oak_branch_o.data.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_branch_01/t_oak_branch_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_oak_branch_01/t_oak_branch_ao.data.dds",
        "baseColorFactor": [0.590524971, 0.590524971, 0.590524971, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 50,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 0.01,
        "groundType": "WOOD",
    },
    "Oak_branch_02": {
        "baseColorMap": "/assets/materials/tree/oak/t_oak_branch_02/t_oak_branch_02_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_oak_branch_02/t_oak_branch_02_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/oak/t_oak_branch_02/t_oak_branch_02_o.data.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_branch_02/t_oak_branch_02_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_oak_branch_02/t_oak_branch_02_ao.data.dds",
        "baseColorFactor": [0.471644998, 0.471644998, 0.471644998, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 75,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 0.01,
        "groundType": "WOOD",
    },
    "Moss_01": {
        "baseColorMap": "/assets/materials/tree/oak/t_moss/t_moss_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_moss/t_moss_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/oak/t_moss/t_moss_o.data.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_moss/t_moss_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_moss/t_moss_ao.data.dds",
        "baseColorFactor": [0.996078491, 0.996078491, 0.996078491, 1.0],
        "alphaTest": True,
        "alphaRef": 139,
        "doubleSided": True,
        "translucentZWrite": True,
        "groundType": "GRASS",
    },
    "m_oak_leaves_01": {
        "baseColorMap": "/assets/materials/tree/oak/t_oak_leaves_02/t_oak_leaves_02_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_ao.data.dds",
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 120,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "translucentZWrite": True,
        "groundType": "LEAVES_THIN",
    },
    "m_oak_leaves_02": {
        "baseColorMap": "/assets/materials/tree/oak/t_oak_leaves_02/t_oak_leaves_02_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_ao.data.dds",
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 120,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "translucentZWrite": True,
        "groundType": "LEAVES_THIN",
    },
    "oak_leaves_01": {
        "baseColorMap": "/assets/materials/tree/oak/t_oak_leaves_02/t_oak_leaves_02_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_ao.data.dds",
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 120,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "translucentZWrite": True,
        "groundType": "LEAVES_THIN",
    },
    "oak_leaves_02": {
        "baseColorMap": "/assets/materials/tree/oak/t_oak_leaves_02/t_oak_leaves_02_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_oak_leaves_01/t_oak_leaves_ao.data.dds",
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 120,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "translucentZWrite": True,
        "groundType": "LEAVES_THIN",
    },
    "m_oak_leaves_distant": {
        "baseColorMap": "/assets/materials/tree/oak/t_oak_leaves_distant/t_oak_leaves_distant_b.color.dds",
        "normalMap": "/assets/materials/tree/oak/t_oak_leaves_distant/t_oak_leaves_distant_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/oak/t_oak_leaves_distant/t_oak_leaves_distant_o.data.dds",
        "roughnessMap": "/assets/materials/tree/oak/t_oak_leaves_distant/t_oak_leaves_distant_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/oak/t_oak_leaves_distant/t_oak_leaves_distant_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 90,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    # Douglas Fir / Spruce (linked meshes from east_coast_usa, direct /assets/ textures)
    "fir_bark": {
        "baseColorMap": "/assets/materials/tree/fir/t_fir_bark/t_fir_bark_b.color.dds",
        "normalMap": "/assets/materials/tree/fir/t_fir_bark/t_fir_bark_nm.normal.dds",
        "roughnessMap": "/assets/materials/tree/fir/t_fir_bark/t_fir_bark_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/fir/t_fir_bark/t_fir_bark_ao.data.dds",
        "baseColorFactor": [0.726626992, 0.726619005, 0.726619005, 1.0],
        "groundType": "WOOD",
    },
    "fir_leaves": {
        "baseColorMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_b.color.dds",
        "normalMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_ao.data.dds",
        "baseColorFactor": [0.996078491, 0.996078491, 0.996078491, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 75,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    "fir_leaves_02": {
        "baseColorMap": "/assets/materials/tree/fir/t_fir_leaves_02/t_fir_leaves_02_b.color.dds",
        "normalMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_o.data.dds",
        "roughnessMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/fir/t_fir_leaves/t_fir_leaves_ao.data.dds",
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 75,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    "fir_leaves_dead": {
        "baseColorMap": "/assets/materials/tree/fir/t_fir_leaves_dead/t_fir_leaves_dead_b.color.dds",
        "normalMap": "/assets/materials/tree/fir/t_fir_leaves_dead/t_fir_leaves_dead_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/fir/t_fir_leaves_dead/t_fir_leaves_dead_o.data.dds",
        "roughnessMap": "/assets/materials/tree/fir/t_fir_leaves_dead/t_fir_leaves_dead_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/fir/t_fir_leaves_dead/t_fir_leaves_dead_ao.data.dds",
        "baseColorFactor": [0.727016985, 0.727016985, 0.727016985, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 76,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 0.1,
        "groundType": "LEAVES_THIN",
    },
    "m_fir_leaves_distant": {
        "baseColorMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_b.color.dds",
        "normalMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_o.data.dds",
        "roughnessMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 65,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    "m_fir_leaves_distant_02": {
        "baseColorMap": "/assets/materials/tree/fir/t_fir_leaves_distant_02/t_fir_leaves_distant_02_b.color.dds",
        "normalMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_o.data.dds",
        "roughnessMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/fir/t_fir_leaves_distant/t_fir_leaves_distant_ao.data.dds",
        "alphaTest": True,
        "alphaRef": 65,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    # Scots Pine (linked mesh from italy, direct /assets/ textures)
    "scots_pine_trunk": {
        "baseColorMap": "/assets/materials/tree/scots_pine/t_scots_pine_trunk/t_scots_pine_bark_b.color.dds",
        "normalMap": "/assets/materials/tree/scots_pine/t_scots_pine_trunk/t_scots_pine_bark_nm.normal.dds",
        "roughnessMap": "/assets/materials/tree/scots_pine/t_scots_pine_trunk/t_scots_pine_bark_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/scots_pine/t_scots_pine_trunk/t_scots_pine_bark_ao.data.dds",
        "baseColorFactor": [0.674180984, 0.674180984, 0.674180984, 1.0],
        "groundType": "WOOD",
    },
    "scots_pine_branches": {
        "baseColorMap": "/assets/materials/tree/scots_pine/t_scots_pine_branches/t_scots_pine_branches_b.color.dds",
        "normalMap": "/assets/materials/tree/scots_pine/t_scots_pine_branches/t_scots_pine_branches_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/scots_pine/t_scots_pine_branches/t_scots_pine_branches_o.data.dds",
        "roughnessMap": "/assets/materials/tree/scots_pine/t_scots_pine_branches/t_scots_pine_branches_r.data.dds",
        "baseColorFactor": [0.586121976, 0.586121976, 0.586121976, 1.0],
        "alphaTest": True,
        "alphaRef": 80,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 0.01,
        "groundType": "WOOD",
    },
    "scots_pine": {
        "baseColorMap": "/assets/materials/tree/scots_pine/t_scots_pine/t_scots_pine_b.color.dds",
        "normalMap": "/assets/materials/tree/scots_pine/t_scots_pine/t_scots_pine_nm.normal.dds",
        "opacityMap": "/assets/materials/tree/scots_pine/t_scots_pine/t_scots_pine_o.data.dds",
        "roughnessMap": "/assets/materials/tree/scots_pine/t_scots_pine/t_scots_pine_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/tree/scots_pine/t_scots_pine/t_scots_pine_ao.data.dds",
        "baseColorFactor": [0.999989986, 1.0, 0.999994993, 1.0],
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 80,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    # Lush Ferns groundcover (linked meshes from east_coast_usa, direct /assets/ textures)
    "forest_vegetation": {
        "baseColorMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_b.color.dds",
        "normalMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_nm.normal.dds",
        "opacityMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_o.data.dds",
        "roughnessMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_r.data.dds",
        "ambientOcclusionMap": "/assets/materials/foliage/groundcover/t_forest_vegetation/t_undergrowth_01_ao.data.dds",
        "pixelSpecular": True,
        "alphaTest": True,
        "alphaRef": 80,
        "doubleSided": True,
        "subSurface": True,
        "subSurfaceIntensity": 1.0,
        "groundType": "LEAVES_THIN",
    },
    # Collision proxy materials
    "leaves_strong": {
        "collisionProxy": True,
        "groundDepth": 0.6,
        "groundType": "leaves_strong",
    },
    "leaves_thin": {
        "collisionProxy": True,
        "groundDepth": 0.3,
        "groundType": "leaves_thin",
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


def discover_beamng_dir(beamng_dir: str | Path | bool | None = None) -> Path | None:
    """Resolve a local BeamNG.drive installation directory if present.

    Pass ``beamng_dir=False`` to force offline mode (using ``/assets/`` fallback
    meshes only). When ``beamng_dir`` is ``None``, probes ``BEAMNG_DIR``,
    ``BEAMNG_INSTALL``, and standard Windows install locations.
    """
    if beamng_dir is False:
        return None
    if beamng_dir is not None:
        candidate = Path(beamng_dir)
        return candidate if (candidate / "content" / "levels").is_dir() else None

    for env_var in ("BEAMNG_DIR", "BEAMNG_INSTALL"):
        val = os.environ.get(env_var)
        if val:
            candidate = Path(val)
            if (candidate / "content" / "levels").is_dir():
                return candidate

    for candidate in _DEFAULT_BEAMNG_CANDIDATES:
        if (candidate / "content" / "levels").is_dir():
            return candidate
    return None


def _level_archive_entries(
    beamng_path: Path,
    src_level: str,
    cache: dict[str, set[str]],
) -> set[str]:
    """Return normalized archive/directory paths inside ``content/levels/<src_level>``."""
    if src_level in cache:
        return cache[src_level]
    entries: set[str] = set()
    zip_path = beamng_path / "content" / "levels" / f"{src_level}.zip"
    if zip_path.is_file():
        try:
            with zipfile.ZipFile(zip_path) as zf:
                entries = {name.replace("\\", "/").lstrip("/") for name in zf.namelist()}
        except (zipfile.BadZipFile, OSError):
            entries = set()
    else:
        dir_path = beamng_path / "content" / "levels" / src_level
        if dir_path.is_dir():
            content_root = beamng_path / "content"
            entries = {
                p.relative_to(content_root).as_posix()
                for p in dir_path.rglob("*")
                if p.is_file()
            }
    cache[src_level] = entries
    return entries


def prepare_linked_species(
    level_id: str,
    level_dir: Path,
    beamng_dir: Path | None,
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """Emit zero-copy ``.link`` pointers + companion imposter/asset links.

    Link files point from ``levels/<level_id>/art/shapes/linked/<src_level>/...``
    to stock meshes inside ``east_coast_usa`` (Douglas fir, lush ferns) and
    ``italy`` (Scots pine). Material textures in ``main.materials.json`` always
    reference ``/assets/...`` directly (never link-to-link).
    """
    linked_root = level_dir / "art" / "shapes" / "linked"
    if linked_root.exists():
        shutil.rmtree(linked_root, ignore_errors=True)
    if beamng_dir is None:
        return {}, 0, 0

    cache: dict[str, set[str]] = {}
    active_linked: dict[str, dict[str, Any]] = {}
    files_written = 0
    bytes_written = 0

    for key, spec in LINKED_SPECIES_DATA.items():
        source_shape = str(spec["sourceShape"]).lstrip("/")
        parts = source_shape.split("/")
        if len(parts) < 5 or parts[0] != "levels" or parts[2:4] != ["art", "shapes"]:
            continue
        src_level = parts[1]
        rel_after_shapes = "/".join(parts[4:])
        entries = _level_archive_entries(beamng_dir, src_level, cache)
        if source_shape not in entries:
            continue

        targets: list[tuple[str, str]] = [(source_shape, rel_after_shapes)]
        if source_shape.lower().endswith(".dae"):
            cdae_src = source_shape[:-4] + ".cdae"
            cdae_rel = rel_after_shapes[:-4] + ".cdae"
            if cdae_src in entries:
                targets.append((cdae_src, cdae_rel))
        for suffix in (".asset.json", ".imposter.dds", ".imposter_normals.dds"):
            comp_src = source_shape + suffix
            if comp_src in entries:
                targets.append((comp_src, rel_after_shapes + suffix))

        for src_rel, local_rel in targets:
            link_path = linked_root / src_level / f"{local_rel}.link"
            link_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"path": f"/{src_rel}", "type": "normal"}, indent=2) + "\n"
            link_path.write_text(payload, encoding="utf-8")
            files_written += 1
            bytes_written += len(payload)

        entry_spec = dict(spec)
        entry_spec["shapeFile"] = (
            f"/levels/{level_id}/art/shapes/linked/{src_level}/{rel_after_shapes}"
        )
        active_linked[key] = entry_spec

    return active_linked, files_written, bytes_written


def build_managed_item_data_json(
    extra_species: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate ``managedItemData.json`` registering all Central Russian species."""
    combined: dict[str, dict[str, Any]] = dict(SPECIES_DATA)
    if extra_species:
        combined.update(extra_species)
    out: dict[str, Any] = {}
    for key, spec in sorted(combined.items()):
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
        if "baseColorFactor" in spec:
            stage["baseColorFactor"] = list(spec["baseColorFactor"])
        if "normalMap" in spec:
            stage["normalMap"] = spec["normalMap"]
        if "roughnessMap" in spec:
            stage["roughnessMap"] = spec["roughnessMap"]
        if "ambientOcclusionMap" in spec:
            stage["ambientOcclusionMap"] = spec["ambientOcclusionMap"]
        if "opacityMap" in spec:
            stage["opacityMap"] = spec["opacityMap"]
        if spec.get("pixelSpecular"):
            stage["pixelSpecular"] = True

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
        if spec.get("subSurface"):
            entry["subSurface"] = True
            entry["subSurfaceIntensity"] = float(spec.get("subSurfaceIntensity", 1.0))
        if spec.get("translucentZWrite"):
            entry["translucentZWrite"] = True
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


def _geom_to_polygons(geom: Any) -> list[Polygon]:
    """Extract valid non-empty Polygon parts from any Shapely geometry."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 0 else []
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty and g.area > 0]
    if hasattr(geom, "geoms"):
        out: list[Polygon] = []
        for g in geom.geoms:
            out.extend(_geom_to_polygons(g))
        return out
    return []


def _extract_valid_polygons(area: VegetationArea) -> list[Polygon]:
    """Build valid Polygon(s) from a VegetationArea, repairing self-intersections."""
    geom: Any = Polygon(area.ring, area.holes)
    if not geom.is_valid:
        geom = make_valid(geom)
    return _geom_to_polygons(geom)


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


def _build_corridor_tree(
    roads: Sequence[Any] | None,
    railways: Sequence[Any] | None,
) -> tuple[STRtree | None, np.ndarray | None]:
    """Build an ``STRtree`` over all road and railway centre-lines for fast distance queries."""
    lines: list[LineString] = []
    for seq in (roads, railways):
        if not seq:
            continue
        for item in seq:
            pts = getattr(item, "points", None)
            if pts is None and isinstance(item, (list, tuple)):
                pts = item
            if pts is None or len(pts) < 2:
                continue
            line = LineString([(float(p[0]), float(p[1])) for p in pts])
            if not line.is_empty and line.length > 0:
                lines.append(line)
    if not lines:
        return None, None
    return STRtree(lines), np.asarray(lines, dtype=object)


def _query_corridor_distances(
    xs: np.ndarray,
    ys: np.ndarray,
    corridor_tree: STRtree,
    corridor_arr: np.ndarray,
) -> np.ndarray:
    """Return vectorized distance (m) from ``(xs, ys)`` to the nearest road/railway."""
    pts = shapely_points(xs, ys)
    nearest_idx = corridor_tree.nearest(pts)
    return np.asarray(shapely_distance(pts, corridor_arr[nearest_idx]), dtype=np.float64)


def _estimate_road_zone_fractions(
    poly: Polygon,
    corridor_tree: STRtree | None,
    corridor_arr: np.ndarray | None,
) -> tuple[float, float, float]:
    """Estimate area fractions inside ``poly`` at <=60m, 60..400m, and >400m from roads."""
    if corridor_tree is None or corridor_arr is None:
        return 0.0, 1.0, 0.0
    minx, miny, maxx, maxy = poly.bounds
    if maxx <= minx or maxy <= miny:
        return 0.0, 1.0, 0.0
    xs_g = np.linspace(minx, maxx, 16)[1:-1]
    ys_g = np.linspace(miny, maxy, 16)[1:-1]
    if xs_g.size == 0 or ys_g.size == 0:
        cx, cy = (minx + maxx) * 0.5, (miny + maxy) * 0.5
        d = float(_query_corridor_distances(np.array([cx]), np.array([cy]), corridor_tree, corridor_arr)[0])
        return (1.0, 0.0, 0.0) if d <= _ROADSIDE_ZONE_M else ((0.0, 1.0, 0.0) if d <= _BACKDROP_DISTANCE_M else (0.0, 0.0, 1.0))

    gx, gy = np.meshgrid(xs_g, ys_g)
    fx = gx.ravel()
    fy = gy.ravel()
    inside = contains_xy(poly, fx, fy)
    if not np.any(inside):
        rep = poly.representative_point()
        d = float(_query_corridor_distances(np.array([rep.x]), np.array([rep.y]), corridor_tree, corridor_arr)[0])
        return (1.0, 0.0, 0.0) if d <= _ROADSIDE_ZONE_M else ((0.0, 1.0, 0.0) if d <= _BACKDROP_DISTANCE_M else (0.0, 0.0, 1.0))

    dists = _query_corridor_distances(fx[inside], fy[inside], corridor_tree, corridor_arr)
    n = float(dists.size)
    near = float(np.sum(dists <= _ROADSIDE_ZONE_M)) / n
    mid = float(np.sum((dists > _ROADSIDE_ZONE_M) & (dists <= _BACKDROP_DISTANCE_M))) / n
    far = float(np.sum(dists > _BACKDROP_DISTANCE_M)) / n
    return near, mid, far


def _area_corridor_distance(area: VegetationArea, corridor_pts: np.ndarray | None) -> float:
    """Return distance (m) from ``area`` bounding box to nearest road/railway point."""
    if corridor_pts is None or corridor_pts.shape[0] == 0:
        return 0.0
    minx, miny, maxx, maxy = area.bounds
    dx = np.maximum(0.0, np.maximum(minx - corridor_pts[:, 0], corridor_pts[:, 0] - maxx))
    dy = np.maximum(0.0, np.maximum(miny - corridor_pts[:, 1], corridor_pts[:, 1] - maxy))
    return float(np.sqrt(np.min(dx * dx + dy * dy)))


def _normalize_distribution(
    dist: Sequence[tuple[str, float]],
    active_species: dict[str, dict[str, Any]],
) -> tuple[list[str], np.ndarray]:
    """Filter ``dist`` to ``active_species`` and return ``(keys, normalized_weights)``."""
    filtered = [(k, w) for k, w in dist if k in active_species and w > 0]
    if not filtered:
        return [], np.empty(0, dtype=np.float64)
    keys = [k for k, _ in filtered]
    weights = np.array([w for _, w in filtered], dtype=np.float64)
    weights /= weights.sum()
    return keys, weights


def _select_forest_canopy_mix(
    leaf_type: str | None,
    active_species: dict[str, dict[str, Any]],
) -> tuple[list[str], np.ndarray]:
    """Select canopy species mix respecting ``leaf_type`` and available linked conifers."""
    has_conifers = any(k in active_species for k in ("tree_douglasfir_large_a", "scots_pine_tree"))
    lt = (leaf_type or "").strip().lower()
    if has_conifers and lt == "needleleaved":
        return _normalize_distribution(FOREST_NEEDLE_DISTRIBUTION, active_species)
    if has_conifers and lt != "broadleaved":
        return _normalize_distribution(FOREST_MIXED_CONIFER_DISTRIBUTION, active_species)
    return _normalize_distribution(FOREST_SPECIES_DISTRIBUTION, active_species)


def _select_undergrowth_mix(
    active_species: dict[str, dict[str, Any]],
    *,
    prefer_ferns: bool = False,
) -> tuple[list[str], np.ndarray]:
    """Select shrub/fern undergrowth mix according to available species."""
    has_ferns = any(k in active_species for k in ("lush_ferns_01_a", "lush_ferns_02_a"))
    has_conifer_bush = "tree_douglasfir_bush_a" in active_species
    if prefer_ferns and has_ferns:
        return _normalize_distribution(FERN_SPECIES_DISTRIBUTION, active_species)
    scrub_dist = SCRUB_WITH_CONIFER_DISTRIBUTION if has_conifer_bush else SCRUB_SPECIES_DISTRIBUTION
    if has_ferns:
        combined = [(k, w * 0.55) for k, w in scrub_dist] + [
            (k, w * 0.45) for k, w in FERN_SPECIES_DISTRIBUTION
        ]
        return _normalize_distribution(combined, active_species)
    return _normalize_distribution(scrub_dist, active_species)


def _too_close_in_grid(
    cx: float,
    cy: float,
    grid: dict[tuple[int, int], list[tuple[float, float]]],
    cell_size: float,
    min_dist_sq: float,
) -> bool:
    """Return True if ``(cx, cy)`` is within ``sqrt(min_dist_sq)`` of any point in ``grid``."""
    gx = math.floor(cx / cell_size)
    gy = math.floor(cy / cell_size)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for px, py in grid.get((gx + dx, gy + dy), ()):
                if (cx - px) ** 2 + (cy - py) ** 2 < min_dist_sq:
                    return True
    return False


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
    min_spacing_m: float = _MIN_TREE_SPACING_M,
    avoid_cells: dict[tuple[int, int], list[tuple[float, float]]] | None = None,
    avoid_cell_size_m: float = _MIN_TREE_SPACING_M,
    avoid_radius_m: float = _FERN_TRUNK_CLEARANCE_M,
    corridor_tree: STRtree | None = None,
    corridor_arr: np.ndarray | None = None,
    road_dist_range: tuple[float, float] | None = None,
) -> int:
    """Place up to ``n_target`` deterministic instances inside ``poly``."""
    if n_target <= 0 or not species_keys:
        return 0
    minx, miny, maxx, maxy = poly.bounds
    if maxx <= minx or maxy <= miny:
        return 0

    n_mask = mask.shape[0]
    min_spacing_sq = min_spacing_m * min_spacing_m
    avoid_sq = avoid_radius_m * avoid_radius_m
    placed = 0
    max_attempts = max(n_target * 18, n_target + 250)
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

        if road_dist_range is not None and corridor_tree is not None and corridor_arr is not None:
            d_min, d_max = road_dist_range
            dists = _query_corridor_distances(cand_xs, cand_ys, corridor_tree, corridor_arr)
            in_band = (dists >= d_min) & (dists <= d_max)
            if not np.any(in_band):
                continue
            cand_xs = cand_xs[in_band]
            cand_ys = cand_ys[in_band]

        for cx_f, cy_f in zip(cand_xs.tolist(), cand_ys.tolist(), strict=True):
            if _too_close_in_grid(cx_f, cy_f, spacing_cells, min_spacing_m, min_spacing_sq):
                continue
            if avoid_cells is not None and _too_close_in_grid(
                cx_f, cy_f, avoid_cells, avoid_cell_size_m, avoid_sq
            ):
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
            gx = math.floor(cx_f / min_spacing_m)
            gy = math.floor(cy_f / min_spacing_m)
            spacing_cells.setdefault((gx, gy), []).append((cx_f, cy_f))
            placed += 1
            if placed >= n_target:
                break

    return placed


@dataclass(slots=True)
class _PlacementJob:
    """One zone placement task inside a polygon."""

    poly: Polygon
    target: int
    rng: np.random.Generator
    species_keys: list[str]
    species_weights: np.ndarray
    is_fern: bool = False
    road_dist_range: tuple[float, float] | None = None


def _build_forest_zone_jobs(
    poly: Polygon,
    area: VegetationArea,
    rng: np.random.Generator,
    active_species: dict[str, dict[str, Any]],
    density_scale: float,
    corridor_tree: STRtree | None,
    corridor_arr: np.ndarray | None,
) -> list[_PlacementJob]:
    """Split a forest polygon into edge/roadside, core, and far-backdrop jobs."""
    canopy_keys, canopy_weights = _select_forest_canopy_mix(area.leaf_type, active_species)
    edge_under_keys, edge_under_weights = _select_undergrowth_mix(active_species, prefer_ferns=False)
    has_ferns = any(k in active_species for k in ("lush_ferns_01_a", "lush_ferns_02_a"))
    core_under_keys, core_under_weights = _select_undergrowth_mix(
        active_species, prefer_ferns=has_ferns
    )

    core_geom: Any = poly.buffer(-_FOREST_EDGE_BUFFER_M)
    if core_geom is not None and not core_geom.is_valid:
        core_geom = make_valid(core_geom)
    core_polys = _geom_to_polygons(core_geom)

    if not core_polys:
        edge_polys = [poly]
    else:
        edge_geom: Any = poly.difference(core_geom)
        if not edge_geom.is_valid:
            edge_geom = make_valid(edge_geom)
        edge_polys = _geom_to_polygons(edge_geom)

    jobs: list[_PlacementJob] = []

    # 1. Edge perimeter band (<=35m from forest boundary / clearings): dense 200/ha + undergrowth
    for ep in edge_polys:
        ep_ha = ep.area / 10000.0
        n_canopy = max(1 if ep.area >= 200.0 else 0, round(ep_ha * _DEFAULT_ROADSIDE_DENSITY_PER_HA * density_scale))
        if n_canopy > 0:
            jobs.append(
                _PlacementJob(ep, n_canopy, rng, canopy_keys, canopy_weights, is_fern=False)
            )
        n_edge_under = round(ep_ha * _DEFAULT_EDGE_UNDERGROWTH_PER_HA * density_scale)
        if n_edge_under > 0 and edge_under_keys:
            jobs.append(
                _PlacementJob(
                    ep,
                    n_edge_under,
                    rng,
                    edge_under_keys,
                    edge_under_weights,
                    is_fern=has_ferns and any("fern" in k for k in edge_under_keys),
                )
            )

    # 2. Core interior: roadside core (<=60m), near core (60..400m), backdrop (>400m)
    for cp in core_polys:
        cp_ha = cp.area / 10000.0
        f_near, f_mid, f_far = _estimate_road_zone_fractions(cp, corridor_tree, corridor_arr)
        has_roads = corridor_tree is not None

        if f_near > 0 and has_roads:
            near_ha = cp_ha * f_near
            n_near = round(near_ha * _DEFAULT_ROADSIDE_DENSITY_PER_HA * density_scale)
            if n_near > 0:
                jobs.append(
                    _PlacementJob(
                        cp,
                        n_near,
                        rng,
                        canopy_keys,
                        canopy_weights,
                        road_dist_range=(0.0, _ROADSIDE_ZONE_M),
                    )
                )
            n_near_u = round(near_ha * _DEFAULT_EDGE_UNDERGROWTH_PER_HA * density_scale)
            if n_near_u > 0 and edge_under_keys:
                jobs.append(
                    _PlacementJob(
                        cp,
                        n_near_u,
                        rng,
                        edge_under_keys,
                        edge_under_weights,
                        is_fern=has_ferns and any("fern" in k for k in edge_under_keys),
                        road_dist_range=(0.0, _ROADSIDE_ZONE_M),
                    )
                )

        if f_mid > 0:
            mid_ha = cp_ha * f_mid
            mid_range = (_ROADSIDE_ZONE_M, _BACKDROP_DISTANCE_M) if has_roads else None
            n_mid = round(mid_ha * _DEFAULT_CORE_DENSITY_PER_HA * density_scale)
            if n_mid > 0:
                jobs.append(
                    _PlacementJob(
                        cp, n_mid, rng, canopy_keys, canopy_weights, road_dist_range=mid_range
                    )
                )
            core_u_rate = (
                _DEFAULT_CORE_FERN_PER_HA if has_ferns else _DEFAULT_CORE_BUSH_FALLBACK_PER_HA
            )
            n_mid_u = round(mid_ha * core_u_rate * density_scale)
            if n_mid_u > 0 and core_under_keys:
                jobs.append(
                    _PlacementJob(
                        cp,
                        n_mid_u,
                        rng,
                        core_under_keys,
                        core_under_weights,
                        is_fern=has_ferns,
                        road_dist_range=mid_range,
                    )
                )

        if f_far > 0 and has_roads:
            far_ha = cp_ha * f_far
            n_far = round(far_ha * _DEFAULT_BACKDROP_DENSITY_PER_HA * density_scale)
            if n_far > 0:
                jobs.append(
                    _PlacementJob(
                        cp,
                        n_far,
                        rng,
                        canopy_keys,
                        canopy_weights,
                        road_dist_range=(_BACKDROP_DISTANCE_M, math.inf),
                    )
                )

    return jobs


def _build_riparian_woodland_areas(
    water: Sequence[Any] | None,
    existing_areas: Sequence[VegetationArea],
    bounds: tuple[float, float, float, float],
) -> list[VegetationArea]:
    """Create narrow riverside/lakeshore woodland strips outside water keepout."""
    if not water:
        return []
    strip_geoms: list[Any] = []
    for w in water:
        ring = getattr(w, "ring", None)
        if ring is None and isinstance(w, np.ndarray):
            ring = w
        if ring is not None and len(ring) >= 3:
            poly_w: Any = Polygon(ring)
            if not poly_w.is_valid:
                poly_w = make_valid(poly_w)
            band = poly_w.buffer(16.0).difference(poly_w.buffer(3.0))
            if not band.is_empty:
                strip_geoms.append(band)

        pts = getattr(w, "points", None)
        if pts is not None and len(pts) >= 2:
            width_m = float(getattr(w, "width_m", 8.0) or 8.0)
            line = LineString([(float(p[0]), float(p[1])) for p in pts])
            if not line.is_empty and line.length > 0:
                inner_r = max(2.5, width_m * 0.5 + 3.0)
                outer_r = inner_r + 14.0
                band = line.buffer(outer_r, cap_style="flat").difference(
                    line.buffer(inner_r, cap_style="flat")
                )
                if not band.is_empty:
                    strip_geoms.append(band)

    if not strip_geoms:
        return []

    merged: Any = unary_union(strip_geoms)
    if not merged.is_valid:
        merged = make_valid(merged)
    merged = merged.intersection(box(*bounds))
    if not merged.is_valid:
        merged = make_valid(merged)

    covered = [
        a.to_polygon()
        for a in existing_areas
        if a.kind in {"forest", "farmland"}
    ]
    if covered:
        excl = unary_union([g for g in covered if not g.is_empty])
        if not excl.is_empty:
            merged = merged.difference(excl)
            if not merged.is_valid:
                merged = make_valid(merged)

    riparian: list[VegetationArea] = []
    polys = sorted(_geom_to_polygons(merged), key=lambda p: -p.area)
    for idx, poly in enumerate(polys[:24], start=1):
        if poly.area < 300.0:
            continue
        sp: Any = poly.simplify(3.0, preserve_topology=True)
        if not sp.is_valid:
            sp = make_valid(sp)
        for part in _geom_to_polygons(sp):
            if part.area < 300.0:
                continue
            ext = np.asarray(part.exterior.coords, dtype=np.float64)
            holes = tuple(
                np.asarray(i.coords, dtype=np.float64)
                for i in part.interiors
                if len(i.coords) >= 4
            )
            riparian.append(
                VegetationArea(
                    osm_id=-8_000_000_000 - idx,
                    ring=ext,
                    kind="forest",
                    holes=holes,
                    area_m2=float(part.area),
                    leaf_type="broadleaved",
                    name="Riparian Woodland",
                )
            )
    return riparian


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
    max_trees: int = 45000,
    target_density_trees_per_ha: float = _DEFAULT_CORE_DENSITY_PER_HA,
    beamng_dir: str | Path | bool | None = None,
) -> tuple[list[dict[str, Any]], VegetationExportStats]:
    """Generate BeamNG .forest4.json, managedItemData.json, materials, and scene objects.

    Uses deterministic PCG64(osm_id) placement, forest-first & priority-highway
    proximity ordering, spatial density zoning (dense roadside/edge canopy +
    undergrowth, medium core + ferns, sparse far backdrop), 2-pass allocation so
    roadside forests are never starved, vectorized ``shapely.contains_xy``
    candidate filtering, minimum trunk spacing, and fast raster keep-out masking.
    """
    stats = VegetationExportStats()
    forest_dir = level_dir / "forest"
    art_forest_dir = level_dir / "art" / "forest"
    forest_dir.mkdir(parents=True, exist_ok=True)
    art_forest_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale .forest4.json files from previous builds
    for stale_forest in forest_dir.glob("*.forest4.json"):
        stale_forest.unlink()

    # 1. Link optional stock conifers/ferns (when BeamNG install is available)
    resolved_beamng = discover_beamng_dir(beamng_dir)
    linked_species, linked_files, linked_bytes = prepare_linked_species(
        level_id, level_dir, resolved_beamng
    )
    stats.files_written += linked_files
    stats.bytes_written += linked_bytes

    active_species: dict[str, dict[str, Any]] = dict(SPECIES_DATA)
    active_species.update(linked_species)

    # 2. Write managedItemData.json and main.materials.json
    mid_path = art_forest_dir / "managedItemData.json"
    mid_data = build_managed_item_data_json(linked_species)
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

    # 3. Build keep-out mask & road proximity structures
    mask, ox, oy, cell = build_keepout_mask(
        build, roads=roads, buildings=buildings, water=water, railways=railways
    )
    n_mask = mask.shape[0]
    elevations = getattr(build, "elevations", None) if build is not None else None
    g_sz, g_ox, g_oy, g_cell = _resolve_grid_params(build)

    # 4. Deterministic 2-pass spatial placement per kind tier (forest > scrub > meadow; farmland=0)
    trees_by_type: dict[str, list[dict[str, Any]]] = {k: [] for k in active_species}
    total_placed = 0
    woody_cells: dict[tuple[int, int], list[tuple[float, float]]] = {}
    fern_cells: dict[tuple[int, int], list[tuple[float, float]]] = {}

    corridor_pts = _collect_corridor_points(roads, railways)
    corridor_tree, corridor_arr = _build_corridor_tree(roads, railways)
    density_scale = max(0.0, float(target_density_trees_per_ha) / _DEFAULT_CORE_DENSITY_PER_HA)

    terrain_bounds = (
        g_ox,
        g_oy,
        g_ox + g_sz * g_cell,
        g_oy + g_sz * g_cell,
    )
    all_areas = list(vegetation_areas)
    if water:
        all_areas.extend(
            _build_riparian_woodland_areas(water, vegetation_areas, terrain_bounds)
        )

    areas_by_tier: dict[int, list[VegetationArea]] = {}
    for area in all_areas:
        if area.kind == "farmland":
            continue
        tier = _KIND_PRIORITY.get(area.kind, 2)
        areas_by_tier.setdefault(tier, []).append(area)

    def _run_job(job: _PlacementJob, cap: int) -> int:
        if cap <= 0:
            return 0
        if job.is_fern:
            return _populate_polygon(
                job.poly,
                cap,
                job.rng,
                job.species_keys,
                job.species_weights,
                mask=mask,
                ox=ox,
                oy=oy,
                cell=cell,
                elevations=elevations,
                g_sz=g_sz,
                g_ox=g_ox,
                g_oy=g_oy,
                g_cell=g_cell,
                spacing_cells=fern_cells,
                trees_by_type=trees_by_type,
                min_spacing_m=_FERN_SPACING_M,
                avoid_cells=woody_cells,
                avoid_cell_size_m=_MIN_TREE_SPACING_M,
                avoid_radius_m=_FERN_TRUNK_CLEARANCE_M,
                corridor_tree=corridor_tree,
                corridor_arr=corridor_arr,
                road_dist_range=job.road_dist_range,
            )
        return _populate_polygon(
            job.poly,
            cap,
            job.rng,
            job.species_keys,
            job.species_weights,
            mask=mask,
            ox=ox,
            oy=oy,
            cell=cell,
            elevations=elevations,
            g_sz=g_sz,
            g_ox=g_ox,
            g_oy=g_oy,
            g_cell=g_cell,
            spacing_cells=woody_cells,
            trees_by_type=trees_by_type,
            min_spacing_m=_MIN_TREE_SPACING_M,
            corridor_tree=corridor_tree,
            corridor_arr=corridor_arr,
            road_dist_range=job.road_dist_range,
        )

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
        pass2_queue: list[_PlacementJob] = []

        for area in tier_areas:
            if total_placed >= max_trees:
                break

            polys = _extract_valid_polygons(area)
            if not polys:
                continue

            rng = np.random.Generator(np.random.PCG64(abs(int(area.osm_id))))

            for poly in polys:
                if total_placed >= max_trees:
                    break

                if area.kind == "forest":
                    jobs = _build_forest_zone_jobs(
                        poly,
                        area,
                        rng,
                        active_species,
                        density_scale,
                        corridor_tree,
                        corridor_arr,
                    )
                else:
                    if area.kind == "scrub":
                        density = target_density_trees_per_ha * 0.4
                        min_target = 1
                    else:  # meadow / grass: sparse shrubs only if budget remains
                        density = target_density_trees_per_ha * 0.05
                        min_target = 0
                    s_keys, s_weights = _select_undergrowth_mix(active_species, prefer_ferns=False)
                    n_total = max(min_target, round((poly.area / 10000.0) * density))
                    jobs = (
                        [_PlacementJob(poly, n_total, rng, s_keys, s_weights)]
                        if n_total > 0 and s_keys
                        else []
                    )

                poly_p1_remaining = _PASS1_MAX_TREES_PER_POLY
                for job in jobs:
                    if total_placed >= max_trees:
                        break
                    p1_cap = min(job.target, poly_p1_remaining, max_trees - total_placed)
                    placed_now = _run_job(job, p1_cap) if p1_cap > 0 else 0
                    total_placed += placed_now
                    poly_p1_remaining -= placed_now
                    leftover = job.target - placed_now
                    if leftover > 0:
                        pass2_queue.append(
                            _PlacementJob(
                                job.poly,
                                leftover,
                                job.rng,
                                job.species_keys,
                                job.species_weights,
                                is_fern=job.is_fern,
                                road_dist_range=job.road_dist_range,
                            )
                        )

        for job in pass2_queue:
            if total_placed >= max_trees:
                break
            p2_cap = min(job.target, max_trees - total_placed)
            total_placed += _run_job(job, p2_cap)

    # 5. Write .forest4.json for each species with instances
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

    # 6. Emit scene objects for level SceneTree
    # BeamNG 0.39+ auto-discovers levels/<level_id>/forest/*.forest4.json;
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
