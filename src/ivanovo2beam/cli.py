"""Command line interface.

Usage::

    ivanovo2beam info      --config configs/ivanovo_mvp.yaml
    ivanovo2beam download  --config configs/ivanovo_mvp.yaml
    ivanovo2beam build     --config configs/ivanovo_mvp.yaml
    ivanovo2beam install   --config configs/ivanovo_mvp.yaml
    ivanovo2beam validate  --config configs/ivanovo_mvp.yaml

The commands mirror the pipeline stages, so a failure can be isolated to the
stage that produced it. Every stage caches its output under ``.cache/``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ivanovo2beam import __version__
from ivanovo2beam.config import REPO_ROOT, ConfigError, ProjectConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

DEFAULT_CONFIG = "configs/ivanovo_mvp.yaml"


class Console:
    """Minimal progress reporter.

    Progress goes to stderr so that ``--json`` output on stdout stays machine
    readable.
    """

    def __init__(self, *, quiet: bool = False, stream: object = None) -> None:
        self.quiet = quiet
        self.stream = stream or sys.stderr
        self._last = -1.0

    def __call__(self, fraction: float, message: str) -> None:
        if self.quiet:
            return
        # Throttle: only redraw when the percentage actually moves.
        if fraction - self._last < 0.01 and fraction < 1.0:
            return
        self._last = fraction
        bar_width = 24
        filled = int(round(bar_width * max(0.0, min(1.0, fraction))))
        bar = "#" * filled + "-" * (bar_width - filled)
        print(f"\r  [{bar}] {fraction * 100:5.1f}%  {message[:70]:<70}", end="", file=self.stream)
        if fraction >= 1.0:
            print(file=self.stream)


def _load(args: argparse.Namespace) -> ProjectConfig:
    """Load the project config named on the command line."""
    return ProjectConfig.load(args.config, root=REPO_ROOT)


def _emit(payload: object, *, as_json: bool) -> None:
    """Print a result either as JSON or as indented text."""
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(payload)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    """Report what the configuration describes, without building anything."""
    config = _load(args)
    level = config.level
    payload: dict[str, object] = {
        "version": __version__,
        "region": {"id": config.region_id, "title": config.region_title},
        "bbox": config.bbox.as_tuple(),
        "projection": {
            "name": config.projection.name,
            "proj_string": config.projection.proj_string,
            "origin_lon": config.projection.origin_lon,
            "origin_lat": config.projection.origin_lat,
        },
        "scale": config.projection.measure_scale_error_over(config.bbox, samples=7).__str__(),
        "terrain_source": config.terrain_source,
        "terrain_resolution_m": config.terrain_resolution_m,
        "sector_size_m": config.sector_size_m,
        "road_classes": sorted(config.roads.classes),
        "cache_dir": str(config.cache_dir),
        "build_dir": str(config.build_dir),
    }
    if level is not None:
        origin_x, origin_y = config.level_terrain_origin(level)
        payload["level"] = {
            "id": level.id,
            "title": level.title,
            "centre_lonlat": [level.center_lon, level.center_lat],
            "grid_size": level.grid.grid_size,
            "square_size_m": level.grid.square_size_m,
            "extent_m": level.grid.extent_m,
            "terrain_origin_world": [origin_x, origin_y],
        }
    if args.json:
        _emit(payload, as_json=True)
        return 0

    print(f"ivanovo2beam {__version__}")
    print(f"region      {config.region_title} ({config.region_id})")
    print(f"bbox        {config.bbox.as_tuple()}")
    print(f"projection  {config.projection.name}")
    print(f"scale       {payload['scale']}")
    print(f"terrain     {config.terrain_source} @ {config.terrain_resolution_m} m")
    print(f"roads       {len(config.roads.classes)} classes")
    print(f"cache       {config.cache_dir}")
    print(f"build       {config.build_dir}")
    if level is not None:
        print(
            f"level       {level.id}: {level.grid.grid_size}x{level.grid.grid_size} @ "
            f"{level.grid.square_size_m} m = {level.grid.extent_m / 1000:.3f} km"
        )
    else:
        print("level       (none configured)")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    """Fetch and cache the geodata a level needs, without building."""
    from ivanovo2beam.build import _level_geographic_bbox, fetch_level_osm
    from ivanovo2beam.terrain.dem import fetch_dem

    config = _load(args)
    console = Console(quiet=args.quiet)
    bbox, _, _ = _level_geographic_bbox(config, margin_deg=0.004)

    console(0.02, "fetching DEM")
    dem_path = fetch_dem(
        bbox,
        config.cache_path("dem"),
        source=config.terrain_source,
        resolution_m=config.terrain_resolution_m,
        force=args.force,
    )
    console(0.4, "fetching OSM")
    _, osm_stats = fetch_level_osm(
        config, bbox, tile_km=args.osm_tile_km, progress=console, force=args.force
    )
    console(1.0, "download complete")
    _emit(
        {
            "dem": {"path": str(dem_path), "bytes": dem_path.stat().st_size},
            "osm": osm_stats,
        },
        as_json=args.json,
    )
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    """Build the level end to end."""
    from ivanovo2beam.build import build_level

    config = _load(args)
    console = Console(quiet=args.quiet)
    started = time.perf_counter()
    result = build_level(
        config,
        progress=console,
        make_zip=not args.no_zip,
        osm_tile_km=args.osm_tile_km,
        osm_budget_s=args.osm_budget_s,
        force_download=args.force,
    )
    if args.json:
        _emit(
            {
                "level_id": result.level_id,
                "mod_zip": str(result.mod_zip) if result.mod_zip else None,
                "stats": result.stats,
                "durations": result.durations,
                "warnings": result.warnings,
            },
            as_json=True,
        )
    else:
        print(result.summary())
        print(f"  total       {time.perf_counter() - started:6.1f}s")
    return 0


def cmd_package(args: argparse.Namespace) -> int:
    """Package an already-built level directory into a mod zip."""
    from ivanovo2beam.build import package_level

    config = _load(args)
    if config.level is None:
        raise ConfigError("config has no 'level' block")
    target = package_level(config.build_dir, config.level.id)
    _emit({"mod_zip": str(target), "bytes": target.stat().st_size}, as_json=args.json)
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Install a built level into BeamNG's user data folder.

    The level is copied as a loose directory under ``levels/`` rather than
    dropped in ``mods/``. That is the documented workflow for local map
    development and it makes iteration far easier, because the game reloads a
    loose level folder without a restart.
    """
    config = _load(args)
    if config.level is None:
        raise ConfigError("config has no 'level' block")
    source = config.build_dir / "levels" / config.level.id
    if not source.is_dir():
        raise ConfigError(f"nothing built yet at {source}; run 'ivanovo2beam build' first")

    userdata = Path(args.beamng) if args.beamng else _find_beamng_userdata()
    if userdata is None:
        raise ConfigError(
            "could not find the BeamNG user data folder; pass --beamng <path>. "
            "It is normally %LOCALAPPDATA%\\BeamNG\\BeamNG.drive\\current"
        )
    target = userdata / "levels" / config.level.id
    if target.exists() and not args.force:
        raise ConfigError(f"{target} already exists; pass --force to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    _emit(
        {
            "installed_to": str(target),
            "userdata": str(userdata),
            "level_id": config.level.id,
            "size_bytes": sum(f.stat().st_size for f in target.rglob("*") if f.is_file()),
        },
        as_json=args.json,
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Check a built level for the mistakes that stop BeamNG loading it."""
    from ivanovo2beam.validation.level import validate_level

    config = _load(args)
    if config.level is None:
        raise ConfigError("config has no 'level' block")
    level_dir = config.build_dir / "levels" / config.level.id
    report = validate_level(level_dir, config, beamng_install=args.beamng)
    if args.json:
        _emit(report.as_dict(), as_json=True)
    else:
        print(report.describe())
    return 0 if report.ok else 1


def _find_beamng_userdata() -> Path | None:
    """Locate BeamNG's user data folder.

    Returns:
        The active user data directory, or ``None`` when not found.
    """
    import os

    env = os.environ.get("BEAMNG_USERDATA")
    if env and Path(env).is_dir():
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        modern = Path(local) / "BeamNG" / "BeamNG.drive"
        if modern.is_dir():
            current = modern / "current"
            return current if current.is_dir() else modern
    documents = Path.home() / "Documents" / "BeamNG.drive"
    return documents if documents.is_dir() else None


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    parser = argparse.ArgumentParser(
        prog="ivanovo2beam",
        description=(
            "Generate a 1:1 scale, drivable BeamNG.drive map from open geodata. "
            "One metre in the game equals one metre in the real world."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ivanovo2beam {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        help=f"region/level config file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress output")

    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", help="show what the config describes")
    info.set_defaults(func=cmd_info)

    download = sub.add_parser("download", help="fetch and cache DEM and OSM data")
    download.add_argument("--force", action="store_true", help="ignore existing caches")
    download.add_argument("--osm-tile-km", type=float, default=1.5, help="OSM fetch tile size in km")
    download.set_defaults(func=cmd_download)

    build = sub.add_parser("build", help="build the level end to end")
    build.add_argument("--force", action="store_true", help="ignore existing caches")
    build.add_argument("--no-zip", action="store_true", help="skip packaging the mod archive")
    build.add_argument("--osm-tile-km", type=float, default=1.5, help="OSM fetch tile size in km")
    build.add_argument(
        "--osm-budget-s",
        type=float,
        default=240.0,
        help="seconds to spend on OSM before continuing with partial data",
    )
    build.set_defaults(func=cmd_build)

    package = sub.add_parser("package", help="package a built level into a mod zip")
    package.set_defaults(func=cmd_package)

    install = sub.add_parser("install", help="copy a built level into BeamNG")
    install.add_argument("--beamng", help="BeamNG user data folder (auto-detected if omitted)")
    install.add_argument("--force", action="store_true", help="overwrite an existing install")
    install.set_defaults(func=cmd_install)

    validate = sub.add_parser("validate", help="validate a built level")
    validate.add_argument("--beamng", help="BeamNG install root, to check asset references")
    validate.set_defaults(func=cmd_validate)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
