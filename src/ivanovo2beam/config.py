"""Configuration loading and validation.

Every tunable in the pipeline lives in YAML under ``configs/``. Nothing is
hard-coded in the modules, so a different region is a different config file
rather than a code change. This module turns those files into validated,
immutable dataclasses and fails loudly on anything inconsistent.

The important derived quantity is the **terrain grid**::

    extent_m = (grid_size - 1) * square_size_m

BeamNG tiles its terrain as a vertex grid, so an ``n`` by ``n`` grid of
``squareSize`` metres spans ``(n - 1) * squareSize`` metres, not
``n * squareSize``. That off-by-one is real and was confirmed against shipped
levels: automation_test_track uses 4096 vertices at 0.5 m and sits at
``position.x = -1023.75``, which is exactly ``-(4096 - 1) * 0.5 / 2``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ivanovo2beam.beamng.ter import MAX_SIZE as TER_MAX_SIZE
from ivanovo2beam.beamng.ter import MIN_SIZE as TER_MIN_SIZE
from ivanovo2beam.gis.projection import BBox, RegionProjection

#: Repository root, i.e. the parent of the ``src`` directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Default relative cache and output directories.
DEFAULT_CACHE_DIR = ".cache"
DEFAULT_BUILD_DIR = "build"


class ConfigError(ValueError):
    """Raised when a configuration file is missing or inconsistent."""


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TerrainGridSpec:
    """Geometry of one BeamNG ``TerrainBlock``.

    Attributes:
        grid_size: Vertices per side. BeamNG requires a power of two in
            ``[128, 8192]``.
        square_size_m: Ground spacing between adjacent vertices, in metres.
            The world is 1:1, so this is a sampling density, not a scale.
    """

    grid_size: int
    square_size_m: float

    def __post_init__(self) -> None:
        size = self.grid_size
        if not isinstance(size, int):
            raise ConfigError(f"terrain.grid_size must be an int, got {type(size).__name__}")
        if not (TER_MIN_SIZE <= size <= TER_MAX_SIZE):
            raise ConfigError(
                f"terrain.grid_size {size} outside BeamNG's supported range [{TER_MIN_SIZE}, {TER_MAX_SIZE}]"
            )
        if size & (size - 1):
            raise ConfigError(f"terrain.grid_size {size} must be a power of two")
        if not math.isfinite(self.square_size_m) or self.square_size_m <= 0:
            raise ConfigError(
                f"terrain.square_size_m must be positive and finite, got {self.square_size_m!r}"
            )

    @property
    def extent_m(self) -> float:
        """Side length of the terrain in metres."""
        return (self.grid_size - 1) * self.square_size_m

    @property
    def vertical_step_m(self) -> float:
        """Height quantisation step for a given vertical range (see ``ter``)."""
        return 1.0 / 65536.0

    def vertical_step_for(self, max_height_m: float) -> float:
        """Return the height quantisation step for a vertical range.

        Args:
            max_height_m: The terrain block's ``maxHeight`` in metres.

        Returns:
            The smallest representable height difference, in metres.
        """
        return max_height_m / 65536.0

    @classmethod
    def from_config(cls, data: Any) -> TerrainGridSpec:
        """Build from a config mapping."""
        if not isinstance(data, dict):
            raise ConfigError(f"terrain config must be a mapping, got {type(data).__name__}")
        try:
            grid_size = int(data["grid_size"])
        except KeyError as exc:
            raise ConfigError("terrain.grid_size is required") from exc

        if "square_size_m" in data:
            square_size = float(data["square_size_m"])
        elif "extent_m" in data:
            # Derive the spacing that makes the grid span the requested extent.
            square_size = float(data["extent_m"]) / (grid_size - 1)
        else:
            raise ConfigError("terrain needs either square_size_m or extent_m")
        return cls(grid_size=grid_size, square_size_m=square_size)


# ---------------------------------------------------------------------------
# Roads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoadClassSpec:
    """Geometry and surface defaults for one OSM ``highway=*`` class.

    Real OSM tags always win: ``lanes``, ``width``, ``surface``, ``maxspeed``,
    ``oneway``, ``bridge`` and ``tunnel`` on an individual way override these
    defaults. These values only fill the gaps.

    Attributes:
        width_m: Carriageway width used when OSM gives no ``width``.
        lanes: Lane count used when OSM gives no ``lanes``.
        shoulder_m: Width of each shoulder, used by the corridor generator.
        surface: Default surface key, e.g. ``asphalt`` or ``gravel``.
        speed_kph: Default speed limit used when OSM gives no ``maxspeed``.
        drivable: Whether the class is rendered as a drivable road.
        priority: Junction precedence; higher wins when classes conflict.
    """

    width_m: float
    lanes: int
    shoulder_m: float
    surface: str
    speed_kph: float
    drivable: bool = True
    priority: int = 0


DEFAULT_ROAD_CLASSES: dict[str, RoadClassSpec] = {
    "motorway": RoadClassSpec(15.0, 4, 2.0, "asphalt", 110.0, priority=100),
    "motorway_link": RoadClassSpec(8.0, 2, 1.0, "asphalt", 60.0, priority=95),
    "trunk": RoadClassSpec(14.0, 4, 1.5, "asphalt", 90.0, priority=90),
    "trunk_link": RoadClassSpec(8.0, 2, 1.0, "asphalt", 60.0, priority=85),
    "primary": RoadClassSpec(10.0, 2, 1.5, "asphalt", 90.0, priority=80),
    "primary_link": RoadClassSpec(7.0, 2, 1.0, "asphalt", 60.0, priority=75),
    "secondary": RoadClassSpec(9.0, 2, 1.0, "asphalt", 90.0, priority=70),
    "secondary_link": RoadClassSpec(7.0, 2, 1.0, "asphalt", 60.0, priority=65),
    "tertiary": RoadClassSpec(8.0, 2, 1.0, "asphalt", 90.0, priority=60),
    "tertiary_link": RoadClassSpec(7.0, 2, 1.0, "asphalt", 60.0, priority=55),
    "unclassified": RoadClassSpec(6.5, 2, 0.5, "asphalt", 60.0, priority=50),
    "residential": RoadClassSpec(6.0, 2, 0.0, "asphalt", 60.0, priority=45),
    "living_street": RoadClassSpec(5.5, 2, 0.0, "asphalt", 20.0, priority=40),
    "service": RoadClassSpec(4.5, 2, 0.0, "asphalt", 20.0, priority=30),
    "track": RoadClassSpec(3.5, 1, 0.0, "gravel", 30.0, priority=20),
    "path": RoadClassSpec(2.0, 1, 0.0, "gravel", 10.0, drivable=False, priority=10),
    "footway": RoadClassSpec(2.0, 1, 0.0, "concrete", 5.0, drivable=False, priority=5),
    "cycleway": RoadClassSpec(2.0, 1, 0.0, "asphalt", 15.0, drivable=False, priority=5),
    "pedestrian": RoadClassSpec(3.0, 1, 0.0, "concrete", 5.0, drivable=False, priority=5),
    "steps": RoadClassSpec(1.5, 1, 0.0, "concrete", 5.0, drivable=False, priority=1),
}


@dataclass(frozen=True, slots=True)
class RoadSettings:
    """Road generation settings.

    Attributes:
        classes: Per-class defaults keyed by OSM ``highway`` value.
        junction_snap_m: Nodes closer than this are welded into one junction.
        simplify_tolerance_m: Douglas-Peucker tolerance applied to centrelines.
            Kept small because simplification moves the road away from reality.
        corridor_half_width_m: Extra graded width either side of the
            carriageway when carving the terrain corridor.
        max_grade: Maximum longitudinal gradient. Longer than this and the road
            is re-profiled rather than allowed to exceed it.
    """

    classes: dict[str, RoadClassSpec] = field(default_factory=lambda: dict(DEFAULT_ROAD_CLASSES))
    junction_snap_m: float = 0.5
    simplify_tolerance_m: float = 0.75
    corridor_half_width_m: float = 12.0
    max_grade: float = 0.12

    def spec_for(self, highway: str | None) -> RoadClassSpec | None:
        """Return the class spec for an OSM ``highway`` tag value.

        Args:
            highway: The tag value, e.g. ``"residential"``.

        Returns:
            The matching spec, or ``None`` when the class is not modelled.
        """
        if highway is None:
            return None
        return self.classes.get(highway.strip().lower())

    @classmethod
    def from_config(cls, data: Any) -> RoadSettings:
        """Build from a config mapping, merging over the built-in defaults."""
        data = data or {}
        classes = dict(DEFAULT_ROAD_CLASSES)
        for key, spec in (data.get("classes") or {}).items():
            base = classes.get(key)
            merged = {
                "width_m": spec.get("width_m", base.width_m if base else 6.0),
                "lanes": spec.get("lanes", base.lanes if base else 2),
                "shoulder_m": spec.get("shoulder_m", base.shoulder_m if base else 0.0),
                "surface": spec.get("surface", base.surface if base else "asphalt"),
                "speed_kph": spec.get("speed_kph", base.speed_kph if base else 60.0),
                "drivable": spec.get("drivable", base.drivable if base else True),
                "priority": spec.get("priority", base.priority if base else 0),
            }
            classes[key] = RoadClassSpec(**merged)
        return cls(
            classes=classes,
            junction_snap_m=float(data.get("junction_snap_m", 0.5)),
            simplify_tolerance_m=float(data.get("simplify_tolerance_m", 0.75)),
            corridor_half_width_m=float(data.get("corridor_half_width_m", 12.0)),
            max_grade=float(data.get("max_grade", 0.12)),
        )


# ---------------------------------------------------------------------------
# Level
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LevelSpec:
    """One buildable BeamNG level.

    A level is a single BeamNG world: one or more terrain blocks plus the
    objects placed on them. ``ivanovo_mvp`` is a small detailed sector;
    a regional build is a different :class:`LevelSpec` over the same region.

    Attributes:
        id: BeamNG level identifier, used as the folder name under ``levels/``.
        title: Human-readable title shown in the level selector.
        center_lon: Longitude of the level centre.
        center_lat: Latitude of the level centre.
        grid: Terrain grid geometry.
        max_height_m: Vertical range encoded in the heightmap. When ``None``
            the builder derives it from the DEM.
        ground_margin_m: Headroom added above and below the real elevation
            range when choosing the vertical range.
    """

    id: str
    title: str
    center_lon: float
    center_lat: float
    grid: TerrainGridSpec
    max_height_m: float | None = None
    ground_margin_m: float = 5.0

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("_", "").isalnum():
            raise ConfigError(
                f"level.id {self.id!r} must be non-empty and contain only letters, "
                "digits and underscores (it becomes a folder name in BeamNG)"
            )
        if not -180.0 <= self.center_lon <= 180.0:
            raise ConfigError(f"level center_lon out of range: {self.center_lon}")
        if not -90.0 <= self.center_lat <= 90.0:
            raise ConfigError(f"level center_lat out of range: {self.center_lat}")

    @classmethod
    def from_config(cls, data: Any) -> LevelSpec:
        """Build from a config mapping."""
        if not isinstance(data, dict):
            raise ConfigError("level config must be a mapping")
        center = data.get("center")
        if center is None:
            raise ConfigError("level.center [lon, lat] is required")
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", data["id"])),
            center_lon=float(center[0]),
            center_lat=float(center[1]),
            grid=TerrainGridSpec.from_config(data.get("terrain", {})),
            max_height_m=(None if data.get("max_height_m") is None else float(data["max_height_m"])),
            ground_margin_m=float(data.get("ground_margin_m", 5.0)),
        )


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Fully resolved project configuration.

    Attributes:
        root: Repository root the paths are relative to.
        region_id: Stable region identifier, e.g. ``"ivanovo_oblast"``.
        region_title: Human-readable region name.
        bbox: Geographic extent of the region.
        projection: The metric world plane.
        terrain_source: DEM provider key.
        terrain_resolution_m: Nominal DEM resolution to request.
        sectors: Sector grid settings.
        roads: Road generation settings.
        buildings_enabled: Whether to generate building footprints.
        vegetation_enabled: Whether to generate vegetation.
        level: Optional single-level specification (MVP builds).
        cache_dir: Root of the download/derivation cache.
        build_dir: Root for generated output.
    """

    root: Path
    region_id: str
    region_title: str
    bbox: BBox
    projection: RegionProjection
    terrain_source: str
    terrain_resolution_m: float
    sectors: dict[str, Any]
    roads: RoadSettings
    buildings_enabled: bool
    vegetation_enabled: bool
    level: LevelSpec | None
    cache_dir: Path
    build_dir: Path

    # ------------------------------------------------------------- derived paths

    def cache_path(self, *parts: str) -> Path:
        """Return a path inside the cache, creating nothing."""
        return self.cache_dir.joinpath(*parts)

    def build_path(self, *parts: str) -> Path:
        """Return a path inside the build output, creating nothing."""
        return self.build_dir.joinpath(*parts)

    @property
    def sector_size_m(self) -> float:
        """Sector edge length in metres."""
        return float(self.sectors["size_m"])

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, path: str | Path, root: Path | None = None) -> ProjectConfig:
        """Load and validate a config file.

        Args:
            path: Path to a YAML config. Relative paths resolve against
                ``root`` (or the repository root).
            root: Repository root. Defaults to the installed package's root.

        Returns:
            The validated configuration.

        Raises:
            ConfigError: If the file is missing, is not valid YAML, or any
                required setting is absent or inconsistent.
        """
        base = REPO_ROOT if root is None else Path(root)
        config_path = Path(path)
        if not config_path.is_absolute():
            config_path = base / config_path
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")
        return cls.from_mapping(raw, root=base, source=str(config_path))

    @classmethod
    def from_mapping(
        cls,
        raw: dict[str, Any],
        root: Path | None = None,
        source: str = "<mapping>",
    ) -> ProjectConfig:
        """Build a configuration from an already-parsed mapping.

        Args:
            raw: The parsed YAML mapping.
            root: Repository root for relative paths.
            source: Description used in error messages.

        Returns:
            The validated configuration.
        """
        base = REPO_ROOT if root is None else Path(root)

        region = raw.get("region")
        if not isinstance(region, dict):
            raise ConfigError(f"{source}: missing 'region' mapping")
        try:
            bbox = BBox.from_config(region["bbox"])
        except KeyError as exc:
            raise ConfigError(f"{source}: region.bbox is required") from exc

        projection = RegionProjection.from_config(region)

        terrain = raw.get("terrain") or {}
        sectors_cfg = raw.get("sectors") or {}
        sector_size = float(sectors_cfg.get("size_m", 2000.0))
        if not math.isfinite(sector_size) or sector_size <= 0:
            raise ConfigError(f"{source}: sectors.size_m must be positive, got {sector_size!r}")

        level_cfg = raw.get("level")
        level = LevelSpec.from_config(level_cfg) if level_cfg else None

        cache_dir = Path(raw.get("cache", {}).get("dir", DEFAULT_CACHE_DIR))
        build_dir = Path(raw.get("build", {}).get("dir", DEFAULT_BUILD_DIR))

        return cls(
            root=base,
            region_id=str(region.get("id", "region")),
            region_title=str(region.get("title", region.get("id", "Region"))),
            bbox=bbox,
            projection=projection,
            terrain_source=str(terrain.get("source", "copernicus_glo30")),
            terrain_resolution_m=float(terrain.get("resolution_m", 30.0)),
            sectors={"size_m": sector_size},
            roads=RoadSettings.from_config(raw.get("roads")),
            buildings_enabled=bool((raw.get("buildings") or {}).get("enabled", True)),
            vegetation_enabled=bool((raw.get("vegetation") or {}).get("enabled", False)),
            level=level,
            cache_dir=(base / cache_dir) if not cache_dir.is_absolute() else cache_dir,
            build_dir=(base / build_dir) if not build_dir.is_absolute() else build_dir,
        )

    # ------------------------------------------------------------------ helpers

    def level_grid_center(self, level: LevelSpec | None = None) -> tuple[float, float]:
        """Return the world coordinates of a level's centre.

        Args:
            level: Level to locate. Defaults to :attr:`level`.

        Returns:
            ``(x, y)`` in metres.

        Raises:
            ConfigError: If no level is configured.
        """
        spec = level if level is not None else self.level
        if spec is None:
            raise ConfigError("no level configured; pass one or set the 'level' block")
        return self.projection.geo_to_world(spec.center_lon, spec.center_lat)

    def level_terrain_origin(self, level: LevelSpec | None = None) -> tuple[float, float]:
        """Return the world coordinates of a terrain grid's minimum corner.

        This is the value that belongs in BeamNG's ``TerrainBlock.position``.

        Args:
            level: Level to locate. Defaults to :attr:`level`.

        Returns:
            ``(x, y)`` of the south-west corner in metres.

        Raises:
            ConfigError: If no level is configured.
        """
        spec = level if level is not None else self.level
        if spec is None:
            raise ConfigError("no level configured; pass one or set the 'level' block")
        cx, cy = self.projection.geo_to_world(spec.center_lon, spec.center_lat)
        half = spec.grid.extent_m / 2.0
        return (cx - half, cy - half)
