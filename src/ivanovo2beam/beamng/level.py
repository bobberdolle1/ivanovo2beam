"""BeamNG level (scene tree) writer.

A BeamNG 0.39 level is a directory of JSON, not a Torque3D ``.mis`` file. The
scene is a tree of ``SimGroup`` nodes; each node owns a directory, and that
directory's ``items.level.json`` lists the node's children and objects as one
JSON object per line::

    levels/<id>/main/items.level.json                     the MissionGroup root
    levels/<id>/main/MissionGroup/items.level.json        its children
    levels/<id>/main/MissionGroup/<group>/items.level.json
    ...

Every object carries ``__parent`` naming the group that owns it, which is how
BeamNG rebuilds the hierarchy. Object ``persistentId`` values are derived
deterministically from the level id and a stable key, so regenerating a level
produces byte-identical files.

World coordinates
-----------------
BeamNG 0.39 uses **(x, y, z) = (east, north, up)** in metres. This was
confirmed two ways against the shipped game: ``core_terrain.getTerrainHeight``
is called as ``newPos.z = core_terrain.getTerrainHeight(newPos)``, and Utah's
DecalRoads sit at ``z`` values inside that level's terrain height range. The
project's internal world frame is (east, north, up) as well, so positions map
across unchanged.

Objects created here:
    TerrainBlock   the landscape
    SpawnSphere    player drop points
    ScatterSky     sun, atmosphere and sky gradients
    TimeOfDay      time-of-day driver, configured for Ivanovo's latitude
    LevelInfo      fog, gravity and view distance
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

#: Namespace for deterministic level-object UUIDs.
_SCENE_NAMESPACE = uuid.UUID("1b4e28ba-2fa1-11d2-883f-0016d3cca427")

#: Root group every level's scene hangs from.
ROOT_GROUP = "MissionGroup"


def stable_uuid(*parts: object) -> str:
    """Return a deterministic UUID string for a scene object.

    Args:
        *parts: Components identifying the object, e.g. level id, class and key.

    Returns:
        A UUID in canonical string form, stable across runs.
    """
    return str(uuid.uuid5(_SCENE_NAMESPACE, "|".join(str(p) for p in parts)))


def json_line(obj: dict[str, Any]) -> str:
    """Serialise one scene object the way BeamNG writes it.

    BeamNG puts one compact JSON object per line. Keeping that shape makes
    generated files diffable and matches shipped levels byte-for-byte in style.

    Args:
        obj: The object to serialise.

    Returns:
        A single line of compact JSON without trailing whitespace.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


# ---------------------------------------------------------------------------
# Scene tree
# ---------------------------------------------------------------------------


@dataclass
class Group:
    """One ``SimGroup`` node in the scene tree.

    Attributes:
        name: Group name, which is also its directory name.
        parent: Parent group path, empty for the root.
        class_name: Usually ``"SimGroup"``.
        enabled: Value for the ``enabled`` field, or ``None`` to omit it.
    """

    name: str
    parent: tuple[str, ...] = ()
    class_name: str = "SimGroup"
    enabled: str | None = None

    @property
    def path(self) -> tuple[str, ...]:
        """Full path of this group below the root."""
        return (*self.parent, self.name)


@dataclass
class SceneTree:
    """A BeamNG scene: groups plus the objects each group owns.

    Attributes:
        level_id: Level identifier, used to derive deterministic UUIDs.
        groups: Groups keyed by their full path below the root.
        objects: Objects keyed by owning group path.
    """

    level_id: str
    groups: dict[tuple[str, ...], Group] = field(default_factory=dict)
    objects: dict[tuple[str, ...], list[dict[str, Any]]] = field(default_factory=dict)

    def add_group(
        self,
        path: Sequence[str],
        *,
        enabled: str | None = None,
        class_name: str = "SimGroup",
    ) -> tuple[str, ...]:
        """Create a group and any missing ancestors.

        Args:
            path: Group path below the root, e.g. ``("level_objects", "terrain")``.
            enabled: Optional ``enabled`` field value.
            class_name: Group class.

        Returns:
            The normalised group path.
        """
        parts = tuple(path)
        if not parts:
            return ()
        for depth in range(1, len(parts) + 1):
            key = parts[:depth]
            if key not in self.groups:
                self.groups[key] = Group(
                    name=key[-1],
                    parent=key[:-1],
                    class_name=class_name,
                    enabled=enabled if depth == len(parts) else None,
                )
        return parts

    def add_object(self, group_path: Sequence[str], obj: dict[str, Any]) -> dict[str, Any]:
        """Attach an object to a group, creating the group if needed.

        Args:
            group_path: Owning group path below the root.
            obj: Object fields. ``__parent`` is set automatically.

        Returns:
            The object as stored, with ``__parent`` filled in.
        """
        parts = self.add_group(group_path)
        obj = dict(obj)
        obj["__parent"] = parts[-1] if parts else ROOT_GROUP
        self.objects.setdefault(parts, []).append(obj)
        return obj

    # ------------------------------------------------------------------ writing

    def group_descriptor(self, path: tuple[str, ...]) -> dict[str, Any]:
        """Build the JSON object representing a group inside its parent."""
        group = self.groups[path]
        parent_name = path[-2] if len(path) > 1 else ROOT_GROUP
        descriptor: dict[str, Any] = {
            "name": group.name,
            "class": group.class_name,
            "persistentId": stable_uuid(self.level_id, "group", "/".join(path)),
            "__parent": parent_name,
        }
        if group.enabled is not None:
            descriptor["enabled"] = group.enabled
        return descriptor

    def render(self) -> dict[str, str]:
        """Render the tree to ``{relative path: file contents}``.

        Only ``main/**/items.level.json`` files are produced; the caller adds
        ``info.json``, the terrain and the material library.

        Returns:
            Mapping of level-relative POSIX paths to file contents.
        """
        files: dict[str, str] = {}

        # The root object lives in main/items.level.json and has no __parent.
        root_obj = {
            "name": ROOT_GROUP,
            "class": "SimGroup",
            "persistentId": stable_uuid(self.level_id, "group", ROOT_GROUP),
            "enabled": "1",
        }
        files["main/items.level.json"] = json_line(root_obj)

        # Children of the root are stored under main/MissionGroup/, so the empty
        # path must be iterated too. Omitting it leaves MissionGroup's own
        # items.level.json empty and every top-level group dangling.
        all_paths: set[tuple[str, ...]] = set(self.groups) | set(self.objects) | {()}
        for path in sorted(all_paths):
            lines: list[str] = []
            for child in sorted(p for p in all_paths if len(p) == len(path) + 1 and p[:-1] == path):
                lines.append(json_line(self.group_descriptor(child)))
            for obj in self.objects.get(path, ()):
                lines.append(json_line(obj))
            if not lines:
                continue
            directory = "/".join((ROOT_GROUP, *path)) if path else ROOT_GROUP
            files[f"main/{directory}/items.level.json"] = "\n".join(lines) + "\n"

        # Ensure every referenced directory exists even when empty, because
        # BeamNG walks the tree by directory.
        for path in sorted(all_paths):
            directory = "/".join((ROOT_GROUP, *path))
            files.setdefault(f"main/{directory}/items.level.json", "")
        files.setdefault(f"main/{ROOT_GROUP}/items.level.json", "")
        return files


# ---------------------------------------------------------------------------
# Level file assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TerrainPlacement:
    """Where a terrain block sits in the world and how its heights are scaled.

    Attributes:
        name: Object name of the ``TerrainBlock``, e.g. ``"theTerrain"``.
        origin_x: World X of the grid's minimum corner.
        origin_y: World Y of the grid's minimum corner.
        base_z: World Z of stored height 0, in metres.
        max_height: Vertical range encoded above ``base_z``, in metres.
        square_size: Ground spacing between vertices, in metres.
        grid_size: Vertices per side.
        terrain_file: In-game path to the ``.ter`` file.
        base_tex_size: Blend-map edge length in texels.
        material_texture_set: Name of the level's texture set.
        minimap_image: Optional in-game path to a minimap image.
    """

    name: str
    origin_x: float
    origin_y: float
    base_z: float
    max_height: float
    square_size: float
    grid_size: int
    terrain_file: str
    base_tex_size: int = 2048
    material_texture_set: str | None = None
    minimap_image: str | None = None

    @property
    def extent_m(self) -> float:
        """Ground extent of the terrain in metres."""
        return (self.grid_size - 1) * self.square_size

    def to_object(self, level_id: str) -> dict[str, Any]:
        """Build the ``TerrainBlock`` scene object.

        Args:
            level_id: Level identifier for the persistent id.

        Returns:
            The object ready for :meth:`SceneTree.add_object`.
        """
        obj: dict[str, Any] = {
            "name": self.name,
            "class": "TerrainBlock",
            "persistentId": stable_uuid(level_id, "TerrainBlock", self.name),
            "position": [round(self.origin_x, 5), round(self.origin_y, 5), round(self.base_z, 5)],
            "baseTexSize": self.base_tex_size,
            "maxHeight": round(self.max_height, 5),
            "squareSize": self.square_size,
            "terrainFile": self.terrain_file,
        }
        if self.material_texture_set:
            obj["materialTextureSet"] = self.material_texture_set
        if self.minimap_image:
            obj["minimapImage"] = self.minimap_image
        return obj


def build_info_json(
    level_id: str,
    title: str,
    description: str,
    *,
    extent_m: float,
    authors: str = "ivanovo2beam",
    biome: str = "central Russian plain",
    roads: str = "Asphalt, gravel and dirt roads generated from OpenStreetMap",
    suitable_for: str = "free roam, traffic",
    features: str = "1:1 scale real-world terrain and road network",
    supports_traffic: bool = True,
    supports_time_of_day: bool = True,
    default_date: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a level's ``info.json``.

    Args:
        level_id: Level identifier (used only for the preview filename).
        title: Title shown in the level selector.
        description: Longer description.
        extent_m: Ground extent in metres, recorded as the nominal ``size``.
        authors: Author credit.
        biome: Biome label.
        roads: Road description.
        suitable_for: Suitability label.
        features: Feature summary.
        supports_traffic: Whether AI traffic may be enabled.
        supports_time_of_day: Whether time of day may be changed.
        default_date: Default in-game date.

    Returns:
        The ``info.json`` contents.
    """
    size = int(round(extent_m))
    return {
        "defaultDate": default_date or {"year": 2026, "month": 6, "day": 20},
        "title": title,
        "description": description,
        "previews": [f"{level_id}_preview.png"],
        "size": [size, size],
        "biome": biome,
        "roads": roads,
        "suitablefor": suitable_for,
        "features": features,
        "authors": authors,
        "supportsTraffic": supports_traffic,
        "supportsTimeOfDay": supports_time_of_day,
    }


def build_sun_and_sky(
    tree: SceneTree,
    group_path: Sequence[str],
    *,
    latitude: float,
    longitude: float,
    elevation_m: float,
    time_of_day: float = 0.35,
    visible_distance_m: float = 12000.0,
    gravity: float = -9.81,
) -> None:
    """Add sun, atmosphere and time-of-day objects to the scene.

    The sky gradients are base-game assets referenced by path. ``TimeOfDay`` is
    configured for the region's real latitude and longitude so the sun tracks
    the actual sky over Ivanovo rather than an arbitrary default.

    Args:
        tree: Scene to populate.
        group_path: Group that should own the objects.
        latitude: Real latitude of the level centre, degrees north.
        longitude: Real longitude of the level centre, degrees east.
        elevation_m: Representative ground elevation in metres.
        time_of_day: Initial time as a fraction of a day (0.5 is noon).
        visible_distance_m: Far clip distance in metres.
        gravity: Gravity in m/s^2.
    """
    origin_z = elevation_m

    tree.add_object(
        group_path,
        {
            "name": "sunsky",
            "class": "ScatterSky",
            "persistentId": stable_uuid(tree.level_id, "ScatterSky", "sunsky"),
            "position": [0, 0, origin_z + 1000.0],
            "azimuth": 315.0,
            "elevation": 45.0,
            "colorize": [0.215686277, 0.349019617, 0.603921592, 1],
            "colorizeGradientFile": "art/sky_gradients/default/gradient_colorize.png",
            "ambientScale": [1, 0.894117653, 0.78039217, 1],
            "ambientScaleGradientFile": "art/sky_gradients/default/gradient_ambient.png",
            "fogScale": [0.396078438, 0.666666687, 1, 1],
            "fogScaleGradientFile": "art/sky_gradients/default/gradient_fog.png",
            "sunScale": [0.996078432, 0.831372559, 0.729411781, 1],
            "sunScaleGradientFile": "art/sky_gradients/default/gradient_sunscale.png",
            "flareScale": 5,
            "flareType": "BNG_Sunflare_3",
            "skyBrightness": 40,
            "mieScattering": 0.00050246407,
            "moonEnabled": False,
            "starVisibility": 1,
        },
    )

    tree.add_object(
        group_path,
        {
            "name": "tod",
            "class": "TimeOfDay",
            "persistentId": stable_uuid(tree.level_id, "TimeOfDay", "tod"),
            "position": [0, 0, origin_z],
            "animate": "0",
            "play": False,
            # Real solar geometry for the mapped area.
            "latitude": round(latitude, 5),
            "longitude": round(longitude, 5),
            "axisTilt": -20,
            "startTime": time_of_day,
            "time": time_of_day,
        },
    )

    tree.add_object(
        group_path,
        {
            "name": "theLevelInfo",
            "class": "LevelInfo",
            "persistentId": stable_uuid(tree.level_id, "LevelInfo", "theLevelInfo"),
            "enabled": "1",
            "gravity": gravity,
            "visibleDistance": visible_distance_m,
            "canvasClearColor": [1, 1, 1, 255],
            "fogColor": [0.741176009, 0.815685987, 0.925490022, 1],
            "fogDensity": 4.99999987e-05,
            "fogAtmosphereHeight": 1600,
            "globalEnviromentMap": "BNG_Sky_02_cubemap",
        },
    )


def add_spawn_point(
    tree: SceneTree,
    group_path: Sequence[str],
    *,
    key: str,
    x: float,
    y: float,
    z: float,
    radius: float = 1.0,
) -> dict[str, Any]:
    """Add a player drop point.

    Args:
        tree: Scene to populate.
        group_path: Owning group, normally ``PlayerDropPoints``.
        key: Stable key for the deterministic UUID.
        x: World X in metres.
        y: World Y in metres.
        z: World Z in metres.
        radius: Spawn sphere radius in metres.

    Returns:
        The created object.
    """
    return tree.add_object(
        group_path,
        {
            "class": "SpawnSphere",
            "persistentId": stable_uuid(tree.level_id, "SpawnSphere", key),
            "position": [round(x, 5), round(y, 5), round(z, 5)],
            "dataBlock": "SpawnSphereMarker",
            "enabled": "1",
            "homingCount": "0",
            "indoorWeight": "1",
            "isAIControlled": "0",
            "lockCount": "0",
            "outdoorWeight": "1",
            "radius": radius,
            "sphereWeight": "1",
        },
    )


def build_road_object(
    level_id: str,
    *,
    key: str,
    nodes: Iterable[Sequence[float]],
    material: str,
    drivability: float = 1.0,
    parent: str,
    over_objects: bool = True,
    gated_road: bool = True,
) -> dict[str, Any]:
    """Build a ``DecalRoad`` scene object.

    A DecalRoad is a spline ribbon drawn over the terrain. The drivable surface
    is the terrain underneath, which is why the terrain builder grades a flat
    corridor beneath each road.

    Args:
        level_id: Level identifier for the deterministic UUID.
        key: Stable key identifying the road.
        nodes: Node quadruples ``[x, y, z, width]`` in world metres.
        material: Decal material name, e.g. ``"road_asphalt_2lane"``.
        drivability: AI drivability in ``[0, 1]``.
        parent: Owning group name.
        over_objects: Whether the decal draws over placed objects.
        gated_road: Whether AI treats it as a gated road.

    Returns:
        The object ready for :meth:`SceneTree.add_object`.

    Raises:
        ValueError: If fewer than two nodes are supplied.
    """
    node_list = [[round(float(v), 5) for v in node] for node in nodes]
    if len(node_list) < 2:
        raise ValueError(f"a DecalRoad needs at least 2 nodes, got {len(node_list)}")
    for node in node_list:
        if len(node) != 4:
            raise ValueError(f"each DecalRoad node must be [x, y, z, width], got {node}")
    return {
        "class": "DecalRoad",
        "persistentId": stable_uuid(level_id, "DecalRoad", key),
        "__parent": parent,
        "position": node_list[0][:3],
        "drivability": drivability,
        "gatedRoad": gated_road,
        "improvedSpline": True,
        "material": material,
        "nodes": node_list,
        "overObjects": over_objects,
    }


def write_level_files(level_dir: Path, files: dict[str, str]) -> list[Path]:
    """Write rendered level files under a level directory.

    Args:
        level_dir: Directory to write into, e.g.
            ``build/ivanovo_mvp/levels/ivanovo_mvp``.
        files: Mapping of level-relative path to contents.

    Returns:
        The paths written, sorted.

    Raises:
        ValueError: If any relative path attempts to escape ``level_dir``.
    """
    written: list[Path] = []
    root = level_dir.resolve()
    for relative, contents in sorted(files.items()):
        target = (level_dir / relative).resolve()
        if not str(target).startswith(str(root)):
            raise ValueError(f"refusing to write outside the level directory: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        written.append(target)
    return written
