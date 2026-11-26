"""Reproducible CLI builder & validator for the P0 Single-Terrain 8192x8192 @ 2.0m experiment.

Usage::

    python scripts/experiment_single_8192.py
    python scripts/experiment_single_8192.py --deploy
    python scripts/experiment_single_8192.py --json --build-dir .tmp/p0_build
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ivanovo2beam.build import build_level  # noqa: E402
from ivanovo2beam.config import ProjectConfig  # noqa: E402
from ivanovo2beam.validation.level import validate_level  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "configs" / "experiment_single_8192_2m.yaml"
DEFAULT_BEAMNG_INSTALL = Path(r"F:\SteamLibrary\steamapps\common\BeamNG.drive")
DEFAULT_BEAMNG_LEVELS_DIR = Path(r"F:\Games\BeamNG.drive\current\levels")


def _progress(fraction: float, message: str) -> None:
    pct = max(0.0, min(100.0, fraction * 100.0))
    print(f"  [{pct:5.1f}%] {message}", flush=True)


def _resolve_cache_dir(config: ProjectConfig, override: Path | None) -> ProjectConfig:
    if override is not None:
        return dataclasses.replace(config, cache_dir=override.resolve())
    if config.cache_dir.is_dir() and (config.cache_dir / "dem").is_dir():
        return config
    main_repo_cache = Path(r"F:\Projects\ivanovobeamng\.cache")
    if main_repo_cache.is_dir():
        return dataclasses.replace(config, cache_dir=main_repo_cache)
    return config


def _collect_scene_stats(level_dir: Path) -> dict[str, Any]:
    """Count BeamNG scene objects, .ter size, forest items, and shape files."""
    counts_by_class: dict[str, int] = {}
    decal_nodes = 0
    main_dir = level_dir / "main"
    if main_dir.is_dir():
        for item_file in main_dir.rglob("items.level.json"):
            for line in item_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cls = str(obj.get("class", "unknown"))
                counts_by_class[cls] = counts_by_class.get(cls, 0) + 1
                if cls == "DecalRoad":
                    decal_nodes += len(obj.get("nodes") or [])

    forest_items = 0
    forest_types = 0
    forest_dir = level_dir / "forest"
    if forest_dir.is_dir():
        for fp in forest_dir.glob("*.forest4.json"):
            cnt = sum(1 for ln in fp.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip())
            if cnt > 0:
                forest_items += cnt
                forest_types += 1

    ter_path = level_dir / "theTerrain.ter"
    ter_bytes = ter_path.stat().st_size if ter_path.is_file() else 0

    shapes_dir = level_dir / "art" / "shapes"
    dae_files = list(shapes_dir.rglob("*.dae")) if shapes_dir.is_dir() else []

    return {
        "ter_bytes": ter_bytes,
        "ter_mib": round(ter_bytes / (1024 * 1024), 2),
        "decal_nodes": decal_nodes,
        "forest_items": forest_items,
        "forest_types": forest_types,
        "dae_files": len(dae_files),
        "dae_mib": round(sum(p.stat().st_size for p in dae_files) / (1024 * 1024), 2),
        "scene_objects_by_class": dict(sorted(counts_by_class.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build & validate ivanovo_single_8192_2m (P0)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Experiment YAML config")
    parser.add_argument("--cache-dir", type=Path, default=None, help="Override cache directory")
    parser.add_argument("--build-dir", type=Path, default=None, help="Override build directory")
    parser.add_argument("--max-buildings", type=int, default=None, help="Optional cap on exported buildings")
    parser.add_argument("--zip", action="store_true", help="Also package mod zip")
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Copy built level to F:/Games/BeamNG.drive/current/levels/<id>",
    )
    parser.add_argument(
        "--beamng-levels-dir",
        type=Path,
        default=DEFAULT_BEAMNG_LEVELS_DIR,
        help="Target BeamNG user levels directory when --deploy is set",
    )
    parser.add_argument(
        "--beamng-install",
        type=Path,
        default=DEFAULT_BEAMNG_INSTALL,
        help="BeamNG installation root for asset validation",
    )
    parser.add_argument(
        "--no-beamng-check",
        action="store_true",
        help="Skip BeamNG install asset path check",
    )
    parser.add_argument("--json", action="store_true", help="Print build + validation summary as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = ProjectConfig.load(args.config, root=REPO_ROOT)
    config = _resolve_cache_dir(config, args.cache_dir)
    if args.build_dir is not None:
        config = dataclasses.replace(config, build_dir=args.build_dir.resolve())

    beamng_install: Path | None = None
    if not args.no_beamng_check and args.beamng_install and args.beamng_install.is_dir():
        beamng_install = args.beamng_install

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    else:
        tracemalloc.reset_peak()

    t0 = time.perf_counter()
    result = build_level(
        config,
        progress=None if args.json else _progress,
        make_zip=args.zip,
        max_buildings=args.max_buildings,
    )
    build_seconds = round(time.perf_counter() - t0, 2)
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    if not was_tracing:
        tracemalloc.stop()

    validation = validate_level(result.level_dir, config, beamng_install=beamng_install)
    scene_stats = _collect_scene_stats(result.level_dir)

    deployed_to: str | None = None
    if args.deploy:
        dst_dir = args.beamng_levels_dir / result.level_id
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(result.level_dir, dst_dir)
        deployed_to = str(dst_dir)

    memory_stats = {
        "peak_traced_mib": round(peak_bytes / (1024 * 1024), 2),
        "current_traced_mib": round(current_bytes / (1024 * 1024), 2),
        "build_wall_seconds": build_seconds,
    }

    if args.json:
        payload = {
            "memory": memory_stats,
            "stats": result.stats,
            "durations": result.durations,
            "warnings": result.warnings,
            "scene": scene_stats,
            "validation": validation.as_dict(),
            "deployed_to": deployed_to,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(result.summary())
        print(
            f"  memory      peak_traced={memory_stats['peak_traced_mib']} MiB, "
            f"wall={build_seconds}s"
        )
        print(
            f"  scene       .ter={scene_stats['ter_mib']} MiB ({scene_stats['ter_bytes']:,} bytes), "
            f"forest={scene_stats['forest_items']} items ({scene_stats['forest_types']} types), "
            f"daes={scene_stats['dae_files']} ({scene_stats['dae_mib']} MiB)"
        )
        print(f"  objects     {scene_stats['scene_objects_by_class']}")
        print(validation.describe())
        if deployed_to:
            print(f"Deployed to: {deployed_to}")

    return 0 if validation.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
