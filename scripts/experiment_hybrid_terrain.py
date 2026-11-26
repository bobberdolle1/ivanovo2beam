"""Reproducible CLI builder & validator for the P1 Hybrid Terrain experiment.

Usage::

    python scripts/experiment_hybrid_terrain.py
    python scripts/experiment_hybrid_terrain.py --deploy
    python scripts/experiment_hybrid_terrain.py --build-dir .tmp/hybrid_build --json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ivanovo2beam.config import ProjectConfig  # noqa: E402
from ivanovo2beam.sectors.hybrid import (  # noqa: E402
    HybridSpec,
    build_hybrid_level,
    validate_hybrid_level,
)

DEFAULT_CONFIG = REPO_ROOT / "configs" / "experiment_hybrid_corridor.yaml"
DEFAULT_BEAMNG_INSTALL = Path(r"F:\SteamLibrary\steamapps\common\BeamNG.drive")
DEFAULT_BEAMNG_LEVELS_DIR = Path(r"F:\Games\BeamNG.drive\current\levels")


def _progress(fraction: float, message: str) -> None:
    pct = max(0.0, min(100.0, fraction * 100.0))
    print(f"  [{pct:5.1f}%] {message}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build & validate ivanovo_hybrid_corridor (P1)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Experiment YAML config")
    parser.add_argument("--cache-dir", type=Path, default=None, help="Override cache directory")
    parser.add_argument("--build-dir", type=Path, default=None, help="Override build directory")
    parser.add_argument(
        "--beamng-install",
        type=Path,
        default=DEFAULT_BEAMNG_INSTALL,
        help="BeamNG install root for asset validation",
    )
    parser.add_argument("--max-error", type=float, default=None, help="Override RTIN max error (m)")
    parser.add_argument("--no-groundcover", action="store_true", help="Disable GroundCover")
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
    parser.add_argument("--json", action="store_true", help="Print build + validation summary as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = ProjectConfig.load(args.config, root=REPO_ROOT)
    spec = HybridSpec.from_yaml(args.config)

    if args.cache_dir is not None:
        config = dataclasses.replace(config, cache_dir=args.cache_dir.resolve())
    elif not config.cache_dir.is_dir():
        main_repo_cache = Path(r"F:\Projects\ivanovobeamng\.cache")
        if main_repo_cache.is_dir():
            config = dataclasses.replace(config, cache_dir=main_repo_cache)

    if args.build_dir is not None:
        config = dataclasses.replace(config, build_dir=args.build_dir.resolve())

    if args.max_error is not None:
        spec = dataclasses.replace(spec, rtin_max_error_m=args.max_error)
    if args.no_groundcover:
        spec = dataclasses.replace(spec, groundcover_enabled=False)

    result = build_hybrid_level(
        config,
        spec,
        progress=None if args.json else _progress,
        make_zip=args.zip,
    )
    beamng_install = args.beamng_install if args.beamng_install and args.beamng_install.is_dir() else None
    validation = validate_hybrid_level(
        result.level_dir,
        config,
        beamng_install=beamng_install,
    )

    deployed_to: str | None = None
    if args.deploy:
        dst_dir = args.beamng_levels_dir / result.level_id
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(result.level_dir, dst_dir)
        deployed_to = str(dst_dir)

    if args.json:
        payload = {
            "stats": result.stats,
            "durations": result.durations,
            "warnings": result.warnings,
            "validation": validation.as_dict(),
            "deployed_to": deployed_to,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(result.summary())
        print(validation.describe())
        if deployed_to:
            print(f"Deployed to: {deployed_to}")

    return 0 if validation.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
