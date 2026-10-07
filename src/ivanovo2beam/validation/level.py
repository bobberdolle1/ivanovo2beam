"""Structural validation of a built BeamNG level.

Checks run in increasing order of cost: file presence, then JSON validity, then
cross-file consistency, then (optionally) resolution of every asset path against
a real BeamNG installation.

The most valuable check is the ``.ter`` versus ``.terrain.json`` comparison. A
descriptor whose ``size`` or material list disagrees with the binary terrain is
exactly the kind of error that produces a level that loads to a black screen,
and it costs nothing to detect.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ivanovo2beam.beamng.ter import HOLE, TerrainFormatError, read_ter

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ivanovo2beam.config import ProjectConfig

#: Files every generated level must contain. Collected from a shipped level.
REQUIRED_FILES: tuple[str, ...] = (
    "info.json",
    "main/items.level.json",
    "main/MissionGroup/items.level.json",
    "art/terrains/main.materials.json",
    "theTerrain.ter",
    "theTerrain.terrain.json",
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One validation check.

    Attributes:
        name: Short identifier.
        ok: Whether it passed.
        detail: Human-readable explanation.
        severity: ``"error"`` blocks loading; ``"warning"`` is informational.
    """

    name: str
    ok: bool
    detail: str
    severity: str = "error"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        mark = "OK  " if self.ok else ("WARN" if self.severity == "warning" else "FAIL")
        return f"[{mark}] {self.name}: {self.detail}"


@dataclass(slots=True)
class ValidationReport:
    """The outcome of validating a level.

    Attributes:
        level_dir: Directory validated.
        checks: Every check performed.
    """

    level_dir: Path
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def errors(self) -> list[CheckResult]:
        """Failed checks that block loading."""
        return [c for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> list[CheckResult]:
        """Failed checks that do not block loading."""
        return [c for c in self.checks if not c.ok and c.severity == "warning"]

    @property
    def ok(self) -> bool:
        """Whether the level passed every blocking check."""
        return not self.errors

    def add(self, name: str, ok: bool, detail: str, severity: str = "error") -> None:
        """Record a check result."""
        self.checks.append(CheckResult(name=name, ok=ok, detail=detail, severity=severity))

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable summary."""
        return {
            "level_dir": str(self.level_dir),
            "ok": self.ok,
            "passed": sum(1 for c in self.checks if c.ok),
            "errors": [{"name": c.name, "detail": c.detail} for c in self.errors],
            "warnings": [{"name": c.name, "detail": c.detail} for c in self.warnings],
            "checks": [
                {"name": c.name, "ok": c.ok, "severity": c.severity, "detail": c.detail} for c in self.checks
            ],
        }

    def describe(self) -> str:
        """Return a multi-line human-readable report."""
        lines = [f"validating {self.level_dir}"]
        lines.extend(str(c) for c in self.checks)
        lines.append(
            f"{sum(1 for c in self.checks if c.ok)} passed, "
            f"{len(self.errors)} failed, {len(self.warnings)} warnings"
        )
        return "\n".join(lines)


def _check_required_files(report: ValidationReport) -> None:
    """Every required file must exist and be non-empty.

    An empty ``items.level.json`` is legal — it describes a group with no
    children — so emptiness is only flagged for files that must carry content.
    """
    for relative in REQUIRED_FILES:
        path = report.level_dir / relative
        if not path.is_file():
            report.add("required_file", False, f"missing {relative}")
        elif path.stat().st_size == 0 and relative != "main/items.level.json":
            report.add("required_file", False, f"{relative} is empty")


def _check_scene_tree(report: ValidationReport) -> None:
    """Parse every items.level.json and check parent references resolve."""
    main = report.level_dir / "main"
    if not main.is_dir():
        report.add("scene_tree", False, "main/ directory is missing")
        return

    known_groups: set[str] = set()
    objects: list[tuple[Path, dict]] = []
    files = sorted(main.rglob("items.level.json"))
    if not files:
        report.add("scene_tree", False, "no items.level.json files found")
        return

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            report.add("scene_tree", False, f"cannot read {path.name}: {exc}")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                report.add("scene_tree", False, f"{path.name}:{lineno} invalid JSON: {exc}")
                continue
            if not isinstance(obj, dict) or "class" not in obj:
                report.add("scene_tree", False, f"{path.name}:{lineno} has no 'class'")
                continue
            if obj.get("class") == "SimGroup" and "name" in obj:
                known_groups.add(str(obj["name"]))
            objects.append((path, obj))

    root = report.level_dir / "main" / "items.level.json"
    if root.is_file() and root.read_text(encoding="utf-8").strip():
        try:
            first = json.loads(root.read_text(encoding="utf-8").splitlines()[0])
            known_groups.add(str(first.get("name", "MissionGroup")))
        except (json.JSONDecodeError, IndexError):
            report.add("scene_tree", False, "main/items.level.json is not a valid root object")

    dangling = {
        str(obj.get("__parent"))
        for _, obj in objects
        if obj.get("__parent") and str(obj["__parent"]) not in known_groups
    }
    report.add(
        "scene_tree",
        not dangling,
        f"{len(objects)} objects in {len(files)} files, {len(known_groups)} groups"
        + (f", dangling parents: {sorted(dangling)}" if dangling else ""),
    )


def _check_terrain(report: ValidationReport) -> None:
    """The .ter must parse, and must agree with its .terrain.json."""
    ter_path = report.level_dir / "theTerrain.ter"
    meta_path = report.level_dir / "theTerrain.terrain.json"
    if not ter_path.is_file() or not meta_path.is_file():
        return

    try:
        terrain = read_ter(ter_path)
    except (TerrainFormatError, OSError) as exc:
        report.add("terrain_binary", False, f".ter is unreadable: {exc}")
        return

    trailer = 4 + sum(1 + len(m.encode()) for m in terrain.materials)
    expected_bytes = 5 + 3 * terrain.size * terrain.size + trailer
    report.add(
        "terrain_binary",
        ter_path.stat().st_size == expected_bytes,
        f"{terrain.size}x{terrain.size}, {len(terrain.materials)} materials, "
        f"{ter_path.stat().st_size:,} bytes (expected {expected_bytes:,})",
    )

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        report.add("terrain_json", False, f".terrain.json is unreadable: {exc}")
        return

    problems: list[str] = []
    if meta.get("size") != terrain.size:
        problems.append(f"size {meta.get('size')} != {terrain.size}")
    if meta.get("heightMapSize") != terrain.size**2:
        problems.append(f"heightMapSize {meta.get('heightMapSize')} != {terrain.size**2}")
    if meta.get("layerMapSize") != terrain.size**2:
        problems.append(f"layerMapSize {meta.get('layerMapSize')} != {terrain.size**2}")
    if list(meta.get("materials", [])) != list(terrain.materials):
        problems.append("materials list differs from the .ter")
    if meta.get("version") != terrain.version:
        problems.append(f"version {meta.get('version')} != {terrain.version}")
    report.add(
        "terrain_json",
        not problems,
        "descriptor matches the binary terrain" if not problems else "; ".join(problems),
    )

    # Material indices must stay inside the declared list.
    top = int(terrain.layers.max()) if terrain.layers.size else 0
    report.add(
        "terrain_layers",
        top < len(terrain.materials) or top == HOLE,
        f"highest layer index {top}, {len(terrain.materials)} materials declared",
    )


def _check_materials(report: ValidationReport, install: Path | None) -> None:
    """Material definitions must parse, and resolve if an install is given."""
    path = report.level_dir / "art" / "terrains" / "main.materials.json"
    if not path.is_file():
        return
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        report.add("materials_json", False, f"unreadable: {exc}")
        return

    materials = {k: v for k, v in doc.items() if isinstance(v, dict) and v.get("class") == "TerrainMaterial"}
    sets = {
        k: v for k, v in doc.items() if isinstance(v, dict) and v.get("class") == "TerrainMaterialTextureSet"
    }
    report.add(
        "materials_json",
        bool(materials) and bool(sets),
        f"{len(materials)} terrain materials, {len(sets)} texture sets",
    )

    paths: set[str] = set()
    for material in materials.values():
        for value in material.values():
            if isinstance(value, str) and value.startswith("/") and value.lower().endswith((".png", ".dds")):
                paths.add(value)
    if install is None or not paths:
        return

    index = _asset_index(install)
    if not index:
        report.add("asset_paths", True, "no asset zips found; skipped", severity="warning")
        return
    missing = sorted(p for p in paths if p.lstrip("/") not in index)
    report.add(
        "asset_paths",
        not missing,
        f"{len(paths)} referenced textures, {len(missing)} missing" + (f": {missing[:5]}" if missing else ""),
    )


def _asset_index(install: Path) -> set[str]:
    """Index the entries of every asset zip in a BeamNG installation."""
    root = install / "content" / "assets"
    if not root.is_dir():
        return set()
    index: set[str] = set()
    for archive in root.rglob("*.zip"):
        try:
            with zipfile.ZipFile(archive) as z:
                index.update(z.namelist())
        except (zipfile.BadZipFile, OSError):
            continue
    return index


def _check_scale(report: ValidationReport, config: ProjectConfig) -> None:
    """The configured projection must be metric to well under a metre per km."""
    bbox = config.bbox
    probe = config.projection.measure_scale_error_over(bbox, samples=5)
    report.add(
        "scale_accuracy",
        probe.error_metres_per_100km < 20.0,
        f"worst case {probe.error_metres_per_100km:.2f} m of error per 100 km driven",
        severity="warning",
    )


def validate_level(
    level_dir: Path,
    config: ProjectConfig | None = None,
    *,
    beamng_install: str | Path | None = None,
) -> ValidationReport:
    """Validate a built level directory.

    Args:
        level_dir: Directory holding ``info.json`` and ``main/``.
        config: Project configuration, used for the scale check.
        beamng_install: Path to a BeamNG installation, enabling asset checks.

    Returns:
        A :class:`ValidationReport`. It is returned rather than raised so a
        caller can print every problem at once.
    """
    report = ValidationReport(level_dir=level_dir)
    if not level_dir.is_dir():
        report.add("level_dir", False, f"{level_dir} does not exist")
        return report

    install = Path(beamng_install) if beamng_install else None
    _check_required_files(report)
    _check_scene_tree(report)
    _check_terrain(report)
    _check_materials(report, install)
    if config is not None:
        _check_scale(report, config)
    return report
