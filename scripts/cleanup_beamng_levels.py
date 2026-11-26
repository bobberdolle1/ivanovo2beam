"""Clean obsolete/duplicate ivanovo2beam builds from the BeamNG level picker.

Preserves the two canonical playable maps (``ivanovo_golden_sector`` and
``ivanovo_shuya_v2``) plus any unrelated third-party levels/mods, while removing
superseded MVP, MapNG, and scaling-experiment builds from BeamNG's user
directory (``levels/``, ``mods/``, and ``temp/levels/``).

Never modifies git-tracked ``configs/``, ``docs/``, ``src/``, or ``tests/``.

Usage (prefer Medium IL system Python on Windows when modifying F:\\Games\\BeamNG.drive):

    C:\\Users\\kiril\\AppData\\Local\\Programs\\Python\\Python313\\python.exe scripts/cleanup_beamng_levels.py --dry-run
    C:\\Users\\kiril\\AppData\\Local\\Programs\\Python\\Python313\\python.exe scripts/cleanup_beamng_levels.py --stop-beamng
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from beamng_host_bridge import (  # noqa: E402
    DEFAULT_USER_DIR,
    REPO_ROOT,
    BeamNgBridgeClient,
    BeamNgHostController,
    find_beamng_pids,
)

#: Canonical maps preserved in BeamNG's level picker.
KEEP_LEVELS: frozenset[str] = frozenset(
    {
        "ivanovo_golden_sector",
        "ivanovo_shuya_v2",
    }
)

#: Exact / prefix patterns of obsolete or superseded ivanovo2beam/MapNG builds.
_OBSOLETE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^ivanovo_mvp(?:_.*)?$"),
    re.compile(r"^ivanovo_shuya$"),
    re.compile(r"^ivanovo_shuya_(?!v2$).*$"),
    re.compile(r"^mapng_golden_sector(?:_.*)?$"),
    re.compile(r"^ivanovo_dual_terrain(?:_.*)?$"),
    re.compile(r"^ivanovo_hybrid(?:_.*)?$"),
    re.compile(r"^ivanovo_single_8192(?:_.*)?$"),
    re.compile(r"^ivanovo_test_sector(?:_.*)?$"),
)


@dataclass(frozen=True, slots=True)
class CleanupTarget:
    """One obsolete level directory, mod zip, or temp cache directory."""

    kind: str  # "level_dir" | "mod_zip" | "temp_dir" | "build_dir" | "build_zip"
    level_id: str
    path: Path
    size_bytes: int


def is_obsolete_level_id(level_id: str) -> bool:
    """Return True if ``level_id`` is a known obsolete/duplicate build to prune."""
    if level_id in KEEP_LEVELS:
        return False
    return any(pat.fullmatch(level_id) for pat in _OBSOLETE_PATTERNS)


def _path_size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        total = 0
        for child in path.rglob("*"):
            with contextlib.suppress(OSError):
                if child.is_file():
                    total += child.stat().st_size
        return total
    return 0


def discover_cleanup_targets(
    user_dir: Path = DEFAULT_USER_DIR,
    *,
    repo_root: Path | None = None,
    include_repo_build: bool = False,
) -> tuple[list[CleanupTarget], list[str]]:
    """Scan BeamNG user dir (and optionally repo ``build/``) for obsolete builds.

    Returns:
        ``(targets, preserved_levels)`` where ``preserved_levels`` lists non-obsolete
        entries found under ``user_dir/levels``.
    """
    targets: list[CleanupTarget] = []
    preserved: list[str] = []

    levels_dir = user_dir / "levels"
    if levels_dir.is_dir():
        for entry in sorted(levels_dir.iterdir()):
            if not entry.is_dir():
                continue
            if is_obsolete_level_id(entry.name):
                targets.append(
                    CleanupTarget(
                        kind="level_dir",
                        level_id=entry.name,
                        path=entry,
                        size_bytes=_path_size_bytes(entry),
                    )
                )
            else:
                preserved.append(entry.name)

    mods_dir = user_dir / "mods"
    if mods_dir.is_dir():
        for entry in sorted(mods_dir.iterdir()):
            if entry.is_file() and entry.suffix.lower() == ".zip" and is_obsolete_level_id(entry.stem):
                targets.append(
                    CleanupTarget(
                        kind="mod_zip",
                        level_id=entry.stem,
                        path=entry,
                        size_bytes=_path_size_bytes(entry),
                    )
                )

    temp_levels_dir = user_dir / "temp" / "levels"
    if temp_levels_dir.is_dir():
        for entry in sorted(temp_levels_dir.iterdir()):
            if entry.is_dir() and is_obsolete_level_id(entry.name):
                targets.append(
                    CleanupTarget(
                        kind="temp_dir",
                        level_id=entry.name,
                        path=entry,
                        size_bytes=_path_size_bytes(entry),
                    )
                )

    if include_repo_build and repo_root is not None:
        build_levels = repo_root / "build" / "levels"
        if build_levels.is_dir():
            for entry in sorted(build_levels.iterdir()):
                if entry.is_dir() and is_obsolete_level_id(entry.name):
                    targets.append(
                        CleanupTarget(
                            kind="build_dir",
                            level_id=entry.name,
                            path=entry,
                            size_bytes=_path_size_bytes(entry),
                        )
                    )
        build_root = repo_root / "build"
        if build_root.is_dir():
            for entry in sorted(build_root.iterdir()):
                if entry.is_file() and entry.suffix.lower() == ".zip" and is_obsolete_level_id(entry.stem):
                    targets.append(
                        CleanupTarget(
                            kind="build_zip",
                            level_id=entry.stem,
                            path=entry,
                            size_bytes=_path_size_bytes(entry),
                        )
                    )

    return targets, preserved


def _stop_beamng_if_needed(*, stop_beamng: bool) -> dict[str, Any]:
    """Check running BeamNG process and stop via host bridge or controller if requested."""
    pids = find_beamng_pids()
    if not pids:
        return {"was_running": False, "stopped": False}
    if not stop_beamng:
        raise RuntimeError(
            f"BeamNG.drive is running (PIDs {pids}); re-run with --stop-beamng or close the game first "
            "to avoid Windows file-lock errors."
        )
    # Prefer the localhost host bridge if active so state stays synchronized.
    with contextlib.suppress(Exception):
        client = BeamNgBridgeClient()
        res = client.post("/beamng/stop", {"timeout_s": 12.0})
        time.sleep(0.8)
        return {"was_running": True, "method": "bridge", "stop_result": res}

    ctrl = BeamNgHostController()
    res = ctrl.stop_beamng(timeout_s=12.0)
    time.sleep(0.8)
    return {"was_running": True, "method": "direct", "stop_result": res}


def cleanup_beamng_levels(
    *,
    user_dir: Path = DEFAULT_USER_DIR,
    repo_root: Path = REPO_ROOT,
    include_repo_build: bool = False,
    dry_run: bool = False,
    stop_beamng: bool = False,
) -> dict[str, Any]:
    """Remove obsolete/duplicate ivanovo2beam level builds while keeping canonical maps."""
    targets, preserved = discover_cleanup_targets(
        user_dir=user_dir,
        repo_root=repo_root,
        include_repo_build=include_repo_build,
    )

    stop_info: dict[str, Any] = {"was_running": False, "stopped": False}
    removed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    reclaimed_bytes = 0

    if not dry_run and targets:
        if ".venv" in Path(sys.executable).parts:
            print(
                "WARNING: Running from .venv\\Scripts\\python.exe may inherit Low IL and fail\n"
                "         with PermissionError on F:\\Games\\BeamNG.drive.\n"
                "         Prefer system Python: C:\\Users\\kiril\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
                file=sys.stderr,
                flush=True,
            )
        stop_info = _stop_beamng_if_needed(stop_beamng=stop_beamng)

        for target in targets:
            try:
                if target.path.is_dir():
                    shutil.rmtree(target.path)
                elif target.path.is_file():
                    target.path.unlink()
                reclaimed_bytes += target.size_bytes
                removed.append(
                    {
                        "kind": target.kind,
                        "level_id": target.level_id,
                        "path": str(target.path),
                        "size_bytes": target.size_bytes,
                    }
                )
            except OSError as exc:
                errors.append({"path": str(target.path), "error": str(exc)})

    planned = [
        {
            "kind": t.kind,
            "level_id": t.level_id,
            "path": str(t.path),
            "size_bytes": t.size_bytes,
        }
        for t in targets
    ]
    total_target_bytes = sum(t.size_bytes for t in targets)

    return {
        "dry_run": dry_run,
        "user_dir": str(user_dir),
        "keep_levels": sorted(KEEP_LEVELS),
        "preserved_levels": preserved,
        "targets_found": len(targets),
        "total_target_mb": round(total_target_bytes / (1024 * 1024), 2),
        "targets": planned,
        "removed": removed,
        "reclaimed_mb": round(reclaimed_bytes / (1024 * 1024), 2),
        "stop_info": stop_info,
        "errors": errors,
        "ok": not errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clean obsolete/duplicate ivanovo2beam builds from BeamNG user dir while preserving canonical maps."
    )
    parser.add_argument("--user-dir", type=Path, default=DEFAULT_USER_DIR)
    parser.add_argument("--include-repo-build", action="store_true", help="Also prune obsolete folders/zips in build/")
    parser.add_argument("--dry-run", action="store_true", help="List obsolete targets without deleting anything")
    parser.add_argument("--stop-beamng", action="store_true", help="Stop BeamNG cleanly via host bridge before deleting")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")
    args = parser.parse_args(argv)

    result = cleanup_beamng_levels(
        user_dir=args.user_dir,
        repo_root=REPO_ROOT,
        include_repo_build=args.include_repo_build,
        dry_run=args.dry_run,
        stop_beamng=args.stop_beamng,
    )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        mode = "DRY-RUN" if result["dry_run"] else "CLEANUP"
        print(f"[{mode}] BeamNG user dir: {result['user_dir']}")
        print(f"Preserved levels: {', '.join(result['preserved_levels']) or '(none)'}")
        if not result["targets"]:
            print("No obsolete level builds found.")
        else:
            verb = "Would remove" if result["dry_run"] else "Removed"
            print(f"{verb} {result['targets_found']} target(s) ({result['total_target_mb']} MiB):")
            for item in result["targets"]:
                mb = round(item["size_bytes"] / (1024 * 1024), 2)
                print(f"  - [{item['kind']}] {item['level_id']} ({mb} MiB): {item['path']}")
        if result["errors"]:
            print("Errors:", file=sys.stderr)
            for err in result["errors"]:
                print(f"  - {err['path']}: {err['error']}", file=sys.stderr)
            return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
