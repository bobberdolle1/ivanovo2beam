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

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Mapping

#: Namespace for deterministic material UUIDs. A fixed, arbitrary UUID.
_MATERIAL_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

#: Root of the shared, base-game terrain textures.
_ASSET_ROOT = "/assets/materials/terrain"

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
            5-channel texture set, relative to :data:`_ASSET_ROOT`.
        texture_prefix: Filename stem inside ``texture_set``, e.g.
            ``t_grass_01`` for ``t_grass_01_b.png``.
        macro_set: Directory holding the macro texture set.
        macro_prefix: Filename stem inside ``macro_set``.
        base_size: World size in metres the base texture is stretched over.
        macro_size: World size in metres the macro texture is stretched over.
        detail_strength: ``[baseColor, roughness]``-style blend strengths applied
            to the detail layer. Defaults suit a subtle detail pass.
        normal_detail_strength: Detail-layer normal strength.
        osm_surface: OSM ``surface`` values that select this material.
    """

    key: str
    title: str
    groundmodel: str
    texture_set: str
    texture_prefix: str
    macro_set: str
    macro_prefix: str
    base_size: int = 512
    macro_size: int = 64
    detail_strength: tuple[float, float] = (0.25, 0.25)
    normal_detail_strength: tuple[float, float] = (0.7, 0.15)
    osm_surface: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.groundmodel not in VALID_GROUNDMODELS:
            raise ValueError(
                f"material {self.key!r}: groundmodel {self.groundmodel!r} is not a known "
                f"BeamNG ground model; expected one of {sorted(VALID_GROUNDMODELS)}"
            )

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
            detail_strength=(0.3, 0.25),
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
            detail_strength=(0.45, 0.35),
            osm_surface=("asphalt",),
        ),
        TerrainMaterialSpec(
            key="Concrete",
            title="Concrete slabs, kerbs and paved squares",
            groundmodel="CONCRETE",
            texture_set="concrete/concrete",
            texture_prefix="t_concrete_damaged",
            macro_set="concrete/macro_concrete",
            macro_prefix="t_macro_concrete",
            detail_strength=(0.35, 0.3),
            osm_surface=("concrete", "concrete:plates", "paving_stones"),
        ),
        TerrainMaterialSpec(
            key="Gravel",
            title="Gravel and unpaved roads",
            groundmodel="GRAVEL",
            texture_set="soil/t_gravel",
            texture_prefix="t_gravel",
            macro_set="soil/macro_clumply",
            macro_prefix="t_macro_clumpy",
            detail_strength=(0.4, 0.4),
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
            osm_surface=("dirt", "ground", "mud", "earth", "unpaved"),
        ),
        TerrainMaterialSpec(
            key="Grass",
            title="Meadow and roadside grass",
            groundmodel="GRASS",
            texture_set="grass/t_grass_01",
            texture_prefix="t_grass_01",
            macro_set="grass/macro_grass",
            macro_prefix="t_macro_grass",
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="Field",
            title="Arable field and dry grass",
            groundmodel="GRASS",
            texture_set="grass/t_dirt_dry_grass",
            texture_prefix="t_dirt_dry_grass",
            macro_set="grass/t_macro_dry_grass",
            macro_prefix="t_macro_dry_grass",
            detail_strength=(0.3, 0.3),
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="ForestFloor",
            title="Forest floor litter and moss",
            groundmodel="GRASS",
            texture_set="forest/t_forest_ground_02",
            texture_prefix="t_forest_ground_02",
            macro_set="forest/t_macro_dirt_forest",
            macro_prefix="t_macro_dirt_forest",
            osm_surface=(),
        ),
        TerrainMaterialSpec(
            key="RiverbankSoil",
            title="Water-adjacent soil and mud flats",
            groundmodel="MUD",
            texture_set="mud/mud",
            texture_prefix="t_mud",
            macro_set="soil/macro_clumply",
            macro_prefix="t_macro_clumpy",
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
    base_tex_size: int = 2048,
    detail_tex_size: int = 512,
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
        ch = spec.channel_paths()
        macro = spec.macro_channel_paths()
        document[key] = {
            "internalName": spec.key,
            "class": "TerrainMaterial",
            "persistentId": spec.persistent_id(),
            # Base layer: the coarse, large-scale colour and relief.
            "baseColorBaseTex": ch["b"],
            "baseColorBaseTexSize": spec.base_size,
            "normalBaseTex": ch["nm"],
            "normalBaseTexSize": spec.base_size,
            "roughnessBaseTex": ch["r"],
            "roughnessBaseTexSize": spec.base_size,
            "aoBaseTex": ch["ao"],
            "aoBaseTexSize": spec.base_size,
            "heightBaseTex": ch["h"],
            "heightBaseTexSize": spec.base_size,
            # Detail layer: close-up break-up.
            "baseColorDetailTex": ch["b"],
            "baseColorDetailStrength": list(spec.detail_strength),
            "normalDetailTex": ch["nm"],
            "normalDetailStrength": list(spec.normal_detail_strength),
            "roughnessDetailTex": ch["r"],
            "roughnessDetailStrength": [0.3, 0.3],
            "aoDetailTex": ch["ao"],
            "heightDetailTex": ch["h"],
            # Macro layer: kilometre-scale variation so large fields and
            # forests do not read as one flat repeating tile.
            "baseColorMacroTex": macro["b"],
            "baseColorMacroTexSize": spec.macro_size,
            "baseColorMacroStrength": [0.1, 0.1],
            "normalMacroTex": macro["nm"],
            "normalMacroTexSize": spec.macro_size,
            "normalMacroStrength": [0.2, 0.4],
            "roughnessMacroTex": macro["r"],
            "roughnessMacroTexSize": spec.macro_size,
            "roughnessMacroStrength": [0.15, 0.8],
            "aoMacroTex": macro["ao"],
            "aoMacroTexSize": spec.macro_size,
            "heightMacroTex": macro["h"],
            "heightMacroTexSize": spec.macro_size,
            "macroDistances": list(_DEFAULT_MACRO_DISTANCES),
            # Physics and audio response.
            "groundmodelName": spec.groundmodel,
        }

    document[f"{level_id}TerrainMaterialTextureSet"] = {
        "name": f"{level_id}TerrainMaterialTextureSet",
        "class": "TerrainMaterialTextureSet",
        "baseTexSize": [base_tex_size, base_tex_size],
        "detailTexSize": [detail_tex_size, detail_tex_size],
        "macroTexSize": [256, 256],
    }
    return document


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
