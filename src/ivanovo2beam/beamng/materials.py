"""BeamNG terrain material library.

BeamNG's terrain shading is driven by ``TerrainMaterial`` assets: each one
binds a *base*, a *detail* and a *macro* texture for five channels (base colour,
normal, roughness, ambient occlusion and height) plus a ``groundmodelName``
which selects the physics and audio response the tyres feel.

Those textures are shipped with the game and shared between levels. Shipped
levels reference them directly, for example::

    "/assets/materials/terrain/grass/ind_grass/t_ind_grass_b.png"

This module follows exactly that pattern. It emits a
``levels/<id>/art/terrains/main.materials.json`` whose materials point at
base-game textures **by path**. Nothing is copied: the generated mod contains no
BeamNG asset bytes, which is both the intended modding workflow and what keeps
the project legally clean (see docs/data_sources.md).

The palette is chosen for the Russian central belt: asphalt, worn asphalt,
gravel, dirt, concrete, meadow grass, arable field, forest floor, riverbank
soil, sand and river pebbles.

Every material's ``persistentId`` is a UUID derived deterministically from its
key, so rebuilding a level yields byte-identical output (see docs/architecture.md
on determinism).
"""

from __future__ import annotations

import binascii
import hashlib
import math
import struct
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Mapping

    from numpy.typing import NDArray

#: Namespace for deterministic material UUIDs. A fixed, arbitrary UUID.
_MATERIAL_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

#: Root of the shared, base-game terrain textures.
_ASSET_ROOT = "/assets/materials/terrain"

#: Default edge length in pixels for terrain base maps and TerrainMaterialTextureSet.baseTexSize.
#: Matches shipped BeamNG levels (e.g. template uses [512, 512]), reducing VRAM 4x vs 1024.
DEFAULT_BASE_TEX_SIZE: int = 512

#: Valid ``groundmodelName`` values observed in shipped levels. Anything else
#: silently loses the tyre/audio response, so the library is restricted to this
#: set and :func:`build_materials` rejects anything outside it.
VALID_GROUNDMODELS = frozenset(
    {
        "ASPHALT",
        "ASPHALT_OLD",
        "ASPHALT_PREPPED",
        "CONCRETE",
        "DIRT",
        "DIRT_ROCKY_LARGE",
        "GRASS",
        "GRAVEL",
        "MUD",
        "ROCK",
        "SAND",
        "SNOW",
    }
)

#: Blend distances for the close-up detail texture layer, in metres.
_DEFAULT_DETAIL_DISTANCES = (0, 0, 35, 70)

#: Blend distances for the macro texture layer, in metres. Copied from the
#: shipped materials so the large-scale variation fades in the same way.
_DEFAULT_MACRO_DISTANCES = (0, 10, 100, 3000)


@dataclass(frozen=True, slots=True)
class TerrainMaterialSpec:
    """One entry in the project's terrain material library.

    Attributes:
        key: The material's ``internalName``. This is the string stored in the
            terrain's layer map, so it must survive a round trip through
            ``max 254`` entries.
        title: Human-readable description for documentation.
        groundmodel: BeamNG ground model, one of :data:`VALID_GROUNDMODELS`.
        texture_set: Directory under ``/assets/materials/terrain`` holding the
            5-channel detail texture set, relative to :data:`_ASSET_ROOT`.
        texture_prefix: Filename stem inside ``texture_set``, e.g.
            ``t_grass_01`` for ``t_grass_01_b.png``.
        macro_set: Directory holding the macro texture set.
        macro_prefix: Filename stem inside ``macro_set``.
        base_color: Representative sRGB colour ``(r, g, b)`` used to generate the
            level's smooth, tileable ``t_terrain_base_<key>_b.png`` base map.
        base_size: World size in metres the base texture is stretched over.
        detail_size: World size in metres the close-up detail texture tiles at.
        macro_size: World size in metres the macro texture is stretched over.
        detail_strength: ``[baseColor, roughness]``-style blend strengths applied
            to the detail layer. Defaults suit a subtle detail pass.
        normal_detail_strength: Detail-layer normal strength.
        detail_distances: Blend distances for the detail layer, in metres.
        macro_distances: Blend distances for the macro layer, in metres.
        osm_surface: OSM ``surface`` values that select this material.
    """

    key: str
    title: str
    groundmodel: str
    texture_set: str
    texture_prefix: str
    macro_set: str
    macro_prefix: str
    base_color: tuple[int, int, int] = (120, 120, 120)
    base_size: int = 256
    detail_size: int = 4
    macro_size: int = 64
    detail_strength: tuple[float, float] = (0.25, 0.15)
    normal_detail_strength: tuple[float, float] = (0.6, 0.15)
    detail_distances: tuple[int, int, int, int] = _DEFAULT_DETAIL_DISTANCES
    macro_distances: tuple[int, int, int, int] = _DEFAULT_MACRO_DISTANCES
    osm_surface: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.groundmodel not in VALID_GROUNDMODELS:
            raise ValueError(
                f"material {self.key!r}: groundmodel {self.groundmodel!r} is not a known "
                f"BeamNG ground model; expected one of {sorted(VALID_GROUNDMODELS)}"
            )

    def base_channel_paths(self, level_id: str) -> dict[str, str]:
        """Return the per-level smooth base-layer texture path for every channel.

        In BeamNG's PBR terrain shader, shared ``/assets/materials/terrain/``
        textures are neutral-grey modulation detail/macro maps; the biome colour
        must come from ``baseColorBaseTex`` while normal/height/AO/roughness
        base maps stay smooth so coarse tiling never produces giant pits.
        """
        root = f"/levels/{level_id}/art/terrains"
        slug = self.key.lower()
        return {
            "b": f"{root}/t_terrain_base_{slug}_b.png",
            "nm": f"{root}/t_terrain_base_nm.png",
            "r": f"{root}/t_terrain_base_r.png",
            "ao": f"{root}/t_terrain_base_ao.png",
            "h": f"{root}/t_terrain_base_h.png",
        }

    def channel_paths(self) -> dict[str, str]:
        """Return the shared-texture path for every channel.

        Returns:
            Mapping of channel suffix (``b``, ``nm``, ``r``, ``ao``, ``h``) to
            the absolute in-game asset path.

        Raises:
            ValueError: If the texture set filenames do not follow the
                ``<prefix>_<channel>.png`` convention.
        """
        return {
            "b": f"{_ASSET_ROOT}/{self.texture_set}/{self.texture_prefix}_b.png",
            "nm": f"{_ASSET_ROOT}/{self.texture_set}/{self.texture_prefix}_nm.png",
            "r": f"{_ASSET_ROOT}/{self.texture_set}/{self.texture_prefix}_r.png",
            "ao": f"{_ASSET_ROOT}/{self.texture_set}/{self.texture_prefix}_ao.png",
            "h": f"{_ASSET_ROOT}/{self.texture_set}/{self.texture_prefix}_h.png",
        }

    def macro_channel_paths(self) -> dict[str, str]:
        """Return the shared macro-texture path for every channel."""
        return {
            "b": f"{_ASSET_ROOT}/{self.macro_set}/{self.macro_prefix}_b.png",
            "nm": f"{_ASSET_ROOT}/{self.macro_set}/{self.macro_prefix}_nm.png",
            "r": f"{_ASSET_ROOT}/{self.macro_set}/{self.macro_prefix}_r.png",
            "ao": f"{_ASSET_ROOT}/{self.macro_set}/{self.macro_prefix}_ao.png",
            "h": f"{_ASSET_ROOT}/{self.macro_set}/{self.macro_prefix}_h.png",
        }

    def all_asset_paths(self) -> tuple[str, ...]:
        """Every base-game texture path this material references.

        The base and macro layers use the same channel keys, so the two
        mappings are combined by value rather than merged by key; merging would
        silently drop the base textures.
        """
        paths = set(self.channel_paths().values())
        paths.update(self.macro_channel_paths().values())
        return tuple(sorted(paths))

    def persistent_id(self) -> str:
        """A stable UUID for this material, derived from its key."""
        return str(uuid.uuid5(_MATERIAL_NAMESPACE, f"TerrainMaterial:{self.key}"))


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------

#: Terrain materials generated for every level, keyed by ``internalName``.
MATERIAL_LIBRARY: dict[str, TerrainMaterialSpec] = {
    m.key: m
    for m in (
        TerrainMaterialSpec(
            key="Asphalt",
            title="Main road asphalt",
            groundmodel="ASPHALT",
            texture_set="asphalt/asphalt",
            texture_prefix="t_asphalt",
            macro_set="asphalt/macro_asphalt",
            macro_prefix="t_macro_asphalt",
            base_color=(56, 58, 62),
            base_size=512,
            detail_size=3,
            detail_strength=(0.3, 0.2),
            osm_surface=("asphalt", "paved", "chipseal"),
        ),
        TerrainMaterialSpec(
            key="Asphalt_worn",
            title="Worn / patched asphalt for secondary roads",
            groundmodel="ASPHALT_OLD",
            texture_set="asphalt/t_asphalt_03",
            texture_prefix="t_asphalt_03",
            macro_set="asphalt/macro_asphalt",
            macro_prefix="t_macro_asphalt",
            base_color=(76, 78, 80),
            base_size=512,
            detail_size=3,
            detail_strength=(0.4, 0.3),
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="Concrete",
            title="Concrete slabs, kerbs and paved squares",
            groundmodel="CONCRETE",
            texture_set="concrete/concrete",
            texture_prefix="t_concrete_damaged",
            macro_set="concrete/macro_concrete",
            macro_prefix="t_macro_concrete",
            base_color=(128, 126, 120),
            base_size=512,
            detail_size=4,
            detail_strength=(0.35, 0.25),
            osm_surface=("concrete", "concrete:plates", "paving_stones"),
        ),
        TerrainMaterialSpec(
            key="Gravel",
            title="Gravel and unpaved roads",
            groundmodel="GRAVEL",
            texture_set="soil/t_gravel",
            texture_prefix="t_gravel",
            # Shipped BeamNG asset zips spell this directory "soil/macro_clumply"
            # while the filenames use "t_macro_clumpy_*" (same in east_coast_usa).
            macro_set="soil/macro_clumply",
            macro_prefix="t_macro_clumpy",
            base_color=(114, 110, 102),
            base_size=512,
            detail_size=3,
            detail_strength=(0.35, 0.3),
            osm_surface=("gravel", "fine_gravel", "pebblestone", "compacted"),
        ),
        TerrainMaterialSpec(
            key="Dirt",
            title="Bare dirt and unsurfaced ground",
            groundmodel="DIRT",
            texture_set="soil/dirt_crumbly",
            texture_prefix="t_dirt_crumbly",
            macro_set="soil/macro_clumply",
            macro_prefix="t_macro_clumpy",
            base_color=(96, 80, 58),
            base_size=512,
            detail_size=3,
            detail_strength=(0.25, 0.2),
            osm_surface=("dirt", "ground", "mud", "earth", "unpaved"),
        ),
        TerrainMaterialSpec(
            key="Grass",
            title="Summer meadow and roadside grass",
            groundmodel="GRASS",
            texture_set="grass/t_grass_01",
            texture_prefix="t_grass_01",
            macro_set="grass/macro_grass",
            macro_prefix="t_macro_grass",
            base_color=(74, 94, 50),
            base_size=512,
            detail_size=3,
            macro_size=48,
            detail_strength=(0.20, 0.12),
            normal_detail_strength=(0.75, 0.14),
            detail_distances=(0, 0, 45, 90),
            macro_distances=(0, 0, 120, 3000),
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="Meadow",
            title="Lush summer meadow with clover and wildflowers",
            groundmodel="GRASS",
            texture_set="grass/t_grass_02",
            texture_prefix="t_grass_02",
            macro_set="grass/t_macro_grass",
            macro_prefix="t_macro_grass",
            base_color=(82, 98, 52),
            base_size=512,
            detail_size=3,
            macro_size=45,
            detail_strength=(0.22, 0.12),
            normal_detail_strength=(0.75, 0.14),
            detail_distances=(0, 0, 45, 90),
            macro_distances=(0, 0, 120, 3000),
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="DryGrass",
            title="Sun-warmed dry grass, scrub and field margins",
            groundmodel="GRASS",
            texture_set="soil/t_dirt_dry_grassy",
            texture_prefix="t_dirt_dry_grassy",
            macro_set="grass/macro_dry_grass_02",
            macro_prefix="t_macro_dry_grass_02",
            base_color=(108, 102, 64),
            base_size=512,
            detail_size=3,
            macro_size=50,
            detail_strength=(0.24, 0.18),
            normal_detail_strength=(0.70, 0.15),
            detail_distances=(0, 0, 40, 80),
            macro_distances=(0, 0, 150, 3000),
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="Field",
            title="Arable field and warm summer dry-grass stubble",
            groundmodel="GRASS",
            texture_set="grass/t_dirt_dry_grass",
            texture_prefix="t_dirt_dry_grass",
            macro_set="grass/macro_dry_grass_02",
            macro_prefix="t_macro_dry_grass_02",
            base_color=(134, 124, 72),
            base_size=512,
            detail_size=4,
            macro_size=56,
            detail_strength=(0.26, 0.18),
            normal_detail_strength=(0.65, 0.18),
            detail_distances=(0, 0, 50, 100),
            macro_distances=(0, 0, 180, 3000),
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="ForestFloor",
            title="Earthy forest floor litter, humus and moss",
            groundmodel="GRASS",
            texture_set="forest/t_forest_ground_02",
            texture_prefix="t_forest_ground_02",
            macro_set="forest/t_macro_dirt_forest",
            macro_prefix="t_macro_dirt_forest",
            base_color=(58, 50, 34),
            base_size=512,
            detail_size=4,
            macro_size=48,
            detail_strength=(0.28, 0.22),
            normal_detail_strength=(0.70, 0.20),
            detail_distances=(0, 0, 45, 90),
            macro_distances=(0, 0, 120, 3000),
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="RiverbankSoil",
            title="Water-adjacent wet soil and mud flats",
            groundmodel="MUD",
            texture_set="mud/mud",
            texture_prefix="t_mud",
            macro_set="soil/macro_clumply",
            macro_prefix="t_macro_clumpy",
            base_color=(64, 54, 42),
            base_size=512,
            detail_size=3,
            detail_strength=(0.30, 0.25),
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="Rock",
            title="Exposed rock and stony ground",
            groundmodel="ROCK",
            texture_set="rock/t_dirt_rocky",
            texture_prefix="t_dirt_rocky",
            macro_set="rock/macro_rocky",
            macro_prefix="t_macro_rocky",
            base_color=(96, 96, 94),
            base_size=512,
            detail_size=4,
            detail_strength=(0.3, 0.25),
            osm_surface=("rock",),
        ),
        TerrainMaterialSpec(
            key="Sand",
            title="Sand, including river beaches",
            groundmodel="SAND",
            texture_set="sand/t_sand",
            texture_prefix="t_sand",
            macro_set="sand/t_macro_sand",
            macro_prefix="t_macro_sand",
            base_color=(146, 132, 100),
            base_size=512,
            detail_size=3,
            detail_strength=(0.28, 0.22),
            osm_surface=("sand",),
        ),
        TerrainMaterialSpec(
            key="Pebbles",
            title="River pebbles and shingle",
            groundmodel="GRAVEL",
            texture_set="rock/t_pebbles",
            texture_prefix="t_pebbles",
            macro_set="rock/macro_rocky",
            macro_prefix="t_macro_rocky",
            base_color=(96, 92, 86),
            base_size=512,
            detail_size=3,
            detail_strength=(0.3, 0.25),
            osm_surface=("pebbles",),
        ),
    )
}

#: The default material set for a level, in layer-map index order.
#:
#: Index 0 must be the background material: anything the pipeline does not
#: explicitly paint stays grass. Keeping this list short and ordered matters
#: because the index is what the terrain's layer map stores, so reordering it
#: changes every generated terrain.
DEFAULT_MATERIAL_ORDER: tuple[str, ...] = (
    "Grass",
    "Meadow",
    "DryGrass",
    "Field",
    "ForestFloor",
    "Dirt",
    "RiverbankSoil",
    "Asphalt",
    "Asphalt_worn",
    "Concrete",
    "Gravel",
    "Rock",
    "Sand",
    "Pebbles",
)


def resolve_materials(keys: Iterable[str]) -> list[TerrainMaterialSpec]:
    """Look up material specs by key, preserving order.

    Args:
        keys: Material ``internalName`` values.

    Returns:
        The matching specs, in the order given.

    Raises:
        KeyError: If a key is not in :data:`MATERIAL_LIBRARY`.
    """
    return [MATERIAL_LIBRARY[k] for k in keys]


def surface_to_material(surface: str | None) -> str:
    """Map an OSM ``surface`` tag to a material key.

    Args:
        surface: The OSM tag value, or ``None`` when untagged.

    Returns:
        The material key to use. Untagged and unknown surfaces fall back to
        ``"Asphalt"``, which is the overwhelmingly common case for the roads
        this pipeline renders.
    """
    if not surface:
        return "Asphalt"
    needle = surface.strip().lower()
    for spec in MATERIAL_LIBRARY.values():
        if needle in spec.osm_surface:
            return spec.key
    return "Asphalt"


def highway_to_material(highway: str | None, surface: str | None = None) -> str:
    """Choose a terrain material for a road.

    Args:
        highway: OSM ``highway`` class, e.g. ``"track"``.
        surface: OSM ``surface`` tag when present; it takes precedence.

    Returns:
        A material key.
    """
    if surface:
        return surface_to_material(surface)
    if highway in {"track", "path"}:
        return "Gravel"
    return "Asphalt"


def build_materials_json(
    level_id: str,
    material_keys: Iterable[str] = DEFAULT_MATERIAL_ORDER,
    *,
    base_tex_size: int = DEFAULT_BASE_TEX_SIZE,
    detail_tex_size: int = 1024,
) -> dict[str, object]:
    """Build the contents of ``levels/<id>/art/terrains/main.materials.json``.

    Args:
        level_id: Level identifier; names the texture set.
        material_keys: Materials to include, in layer-map index order.
        base_tex_size: Terrain blend-map edge length in texels.
        detail_tex_size: Detail texture edge length in texels.

    Returns:
        A JSON-serialisable mapping ready to ``json.dump``.

    Raises:
        ValueError: If more than 254 materials are requested.
    """
    keys = list(material_keys)
    if len(keys) > 254:
        raise ValueError(f"a terrain may reference at most 254 materials, got {len(keys)}")

    document: dict[str, object] = {}
    for key in keys:
        spec = MATERIAL_LIBRARY[key]
        base_ch = spec.base_channel_paths(level_id)
        ch = spec.channel_paths()
        macro = spec.macro_channel_paths()
        document[key] = {
            "internalName": spec.key,
            "class": "TerrainMaterial",
            "persistentId": spec.persistent_id(),
            # Base layer: smooth biome colour and flat foundation so coarse
            # tiling never stretches close-up detail bumps across hundreds of metres.
            "baseColorBaseTex": base_ch["b"],
            "baseColorBaseTexSize": spec.base_size,
            "normalBaseTex": base_ch["nm"],
            "normalBaseTexSize": spec.base_size,
            "roughnessBaseTex": base_ch["r"],
            "roughnessBaseTexSize": spec.base_size,
            "aoBaseTex": base_ch["ao"],
            "aoBaseTexSize": spec.base_size,
            "heightBaseTex": base_ch["h"],
            "heightBaseTexSize": spec.base_size,
            # Detail layer: close-up surface grain (2-4 m tiles, faded at distance).
            "baseColorDetailTex": ch["b"],
            "baseColorDetailTexSize": spec.detail_size,
            "baseColorDetailStrength": list(spec.detail_strength),
            "normalDetailTex": ch["nm"],
            "normalDetailTexSize": spec.detail_size,
            "normalDetailStrength": list(spec.normal_detail_strength),
            "roughnessDetailTex": ch["r"],
            "roughnessDetailTexSize": spec.detail_size,
            "roughnessDetailStrength": [0.3, 0.3],
            "aoDetailTex": ch["ao"],
            "aoDetailTexSize": spec.detail_size,
            "aoDetailStrength": [0.35, 0.5],
            "heightDetailTex": ch["h"],
            "heightDetailTexSize": spec.detail_size,
            "heightDetailStrength": [0.5, 0.0],
            "detailDistances": list(spec.detail_distances),
            "detailDistAtten": [0, 0.9],
            # Macro layer: medium-scale variation (40-80 m tiles) so fields and
            # forests do not read as one flat repeating tile.
            "baseColorMacroTex": macro["b"],
            "baseColorMacroTexSize": spec.macro_size,
            "baseColorMacroStrength": [0.18, 0.20],
            "normalMacroTex": macro["nm"],
            "normalMacroTexSize": spec.macro_size,
            "normalMacroStrength": [0.15, 0.25],
            "roughnessMacroTex": macro["r"],
            "roughnessMacroTexSize": spec.macro_size,
            "roughnessMacroStrength": [0.15, 0.4],
            "aoMacroTex": macro["ao"],
            "aoMacroTexSize": spec.macro_size,
            "aoMacroStrength": [0.2, 0.4],
            "heightMacroTex": macro["h"],
            "heightMacroTexSize": spec.macro_size,
            "heightMacroStrength": [0.0, 0.3],
            "macroDistances": list(spec.macro_distances),
            # Physics and audio response.
            "groundmodelName": spec.groundmodel,
        }

    document[f"{level_id}TerrainMaterialTextureSet"] = {
        "name": f"{level_id}TerrainMaterialTextureSet",
        "class": "TerrainMaterialTextureSet",
        "persistentId": str(uuid.uuid5(_MATERIAL_NAMESPACE, f"TerrainMaterialTextureSet:{level_id}")),
        "baseTexSize": [base_tex_size, base_tex_size],
        "detailTexSize": [detail_tex_size, detail_tex_size],
        "macroTexSize": [1024, 1024],
    }
    return document


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _encode_png(pixels: NDArray[np.uint8]) -> bytes:
    """Encode a 2D grayscale or 3D RGB uint8 array as a deterministic PNG."""
    if pixels.ndim == 2:
        height, width = pixels.shape
        color_type = 0
        rows = pixels
    elif pixels.ndim == 3 and pixels.shape[2] == 3:
        height, width, _ = pixels.shape
        color_type = 2
        rows = pixels.reshape(height, width * 3)
    else:
        raise ValueError(f"expected (H, W) or (H, W, 3) uint8 array, got {pixels.shape}")

    scanlines = np.empty((height, rows.shape[1] + 1), dtype=np.uint8)
    scanlines[:, 0] = 0
    scanlines[:, 1:] = rows
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanlines.tobytes(), level=6))
        + _png_chunk(b"IEND", b"")
    )


def _tileable_color_patch(base_rgb: tuple[int, int, int], seed_key: str, size: int = 256) -> NDArray[np.uint8]:
    """Create a deterministic, isotropic, seamlessly tileable base-colour RGB array.

    Uses periodic multi-octave bilinear value noise rather than low-frequency
    directional cosines so large flat fields never exhibit diagonal wallpaper stripes.
    """
    digest = hashlib.sha256(seed_key.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "little")
    rng = np.random.default_rng(seed)

    def _periodic_octave(cells: int) -> NDArray[np.float64]:
        grid = rng.uniform(-1.0, 1.0, size=(cells, cells))
        coords = np.arange(size, dtype=np.float64) * (cells / size)
        c0 = np.floor(coords).astype(np.intp) % cells
        c1 = (c0 + 1) % cells
        t = coords - np.floor(coords)
        s = t * t * (3.0 - 2.0 * t)
        wy = s[:, None]
        wx = s[None, :]
        v00 = grid[c0[:, None], c0[None, :]]
        v01 = grid[c0[:, None], c1[None, :]]
        v10 = grid[c1[:, None], c0[None, :]]
        v11 = grid[c1[:, None], c1[None, :]]
        return (
            v00 * (1.0 - wx) * (1.0 - wy)
            + v01 * wx * (1.0 - wy)
            + v10 * (1.0 - wx) * wy
            + v11 * wx * wy
        )

    field_luma = np.zeros((size, size), dtype=np.float64)
    field_warm = np.zeros((size, size), dtype=np.float64)
    for cells, weight in ((4, 0.45), (8, 0.30), (16, 0.17), (32, 0.08)):
        eff = min(max(2, cells), size)
        field_luma += weight * _periodic_octave(eff)
        field_warm += weight * _periodic_octave(eff)

    r = np.clip(base_rgb[0] + field_luma * 6.0 + field_warm * 3.0, 0.0, 255.0)
    g = np.clip(base_rgb[1] + field_luma * 6.5 - field_warm * 1.5, 0.0, 255.0)
    b = np.clip(base_rgb[2] + field_luma * 4.5 - field_warm * 2.5, 0.0, 255.0)
    return np.stack((r, g, b), axis=-1).round().astype(np.uint8)


def write_terrain_base_textures(
    level_dir: Path,
    material_keys: Iterable[str] = DEFAULT_MATERIAL_ORDER,
    *,
    size: int = DEFAULT_BASE_TEX_SIZE,
) -> list[Path]:
    """Write smooth per-level ``t_terrain_base_*.png`` textures into ``art/terrains``.

    Args:
        level_dir: Built level root directory (``levels/<id>``).
        material_keys: Materials whose base colour textures should be generated.
        size: Edge length in pixels for each tileable base map texture (matches baseTexSize).

    Returns:
        List of paths written.
    """
    terrains_dir = Path(level_dir) / "art" / "terrains"
    terrains_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    flat_size = size
    normal_arr = np.empty((flat_size, flat_size, 3), dtype=np.uint8)
    normal_arr[:, :] = (128, 128, 255)
    flat_arrays: tuple[tuple[str, NDArray[np.uint8]], ...] = (
        ("t_terrain_base_nm.png", normal_arr),
        ("t_terrain_base_h.png", np.full((flat_size, flat_size), 128, dtype=np.uint8)),
        ("t_terrain_base_ao.png", np.full((flat_size, flat_size), 235, dtype=np.uint8)),
        ("t_terrain_base_r.png", np.full((flat_size, flat_size), 215, dtype=np.uint8)),
    )
    for filename, arr in flat_arrays:
        out = terrains_dir / filename
        out.write_bytes(_encode_png(arr))
        written.append(out)

    for key in material_keys:
        spec = MATERIAL_LIBRARY[key]
        out = terrains_dir / f"t_terrain_base_{spec.key.lower()}_b.png"
        out.write_bytes(_encode_png(_tileable_color_patch(spec.base_color, spec.key, size=size)))
        written.append(out)

    return written


def referenced_asset_paths(material_keys: Iterable[str] = DEFAULT_MATERIAL_ORDER) -> tuple[str, ...]:
    """Return every base-game texture path the given materials reference.

    Used by validation to check that a generated level will resolve all of its
    textures against a real BeamNG installation.

    Args:
        material_keys: Materials to inspect.

    Returns:
        Sorted, de-duplicated asset paths.
    """
    paths: set[str] = set()
    for key in material_keys:
        paths.update(MATERIAL_LIBRARY[key].all_asset_paths())
    return tuple(sorted(paths))


def asset_paths_by_material(
    material_keys: Iterable[str] = DEFAULT_MATERIAL_ORDER,
) -> Mapping[str, tuple[str, ...]]:
    """Return referenced asset paths grouped by material key."""
    return {key: MATERIAL_LIBRARY[key].all_asset_paths() for key in material_keys}
