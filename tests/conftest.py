"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import os
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

#: Repository root, derived from this file's location.
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Iterator[Path]:
    """A fresh temporary directory, backed by ``build/tmp``.

    Two deliberate differences from pytest's built-in ``tmp_path``:

    1. The built-in creates its directory with an atomic rename, which the
       Windows sandbox this project is developed under denies. That leaves an
       unreadable directory behind and fails the whole session at teardown.
    2. The directory is made with a plain :meth:`pathlib.Path.mkdir` rather
       than :func:`tempfile.mkdtemp`. On Windows ``mkdtemp`` applies
       ``chmod 0o700``, which writes a *protected* DACL and therefore strips
       the inherited sandbox grant, leaving a directory the creating process
       cannot write into.

    From a test's point of view the behaviour is the same as ``tmp_path`` and
    it works on Linux CI unchanged.

    Yields:
        Path to an empty directory, removed after the test.
    """
    safe_name = request.node.name.replace("[", "_").replace("]", "_").replace("/", "_")[:40]
    base = REPO_ROOT / "build" / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    directory = base / f"{safe_name}-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)

#: Steam application id of BeamNG.drive.
BEAMNG_STEAM_APPID = "284160"

#: Default Steam library roots to search for a BeamNG installation.
_STEAM_ROOTS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"D:\Steam",
    r"E:\Steam",
    r"F:\Steam",
    r"D:\SteamLibrary",
    r"E:\SteamLibrary",
    r"F:\SteamLibrary",
)


def _candidate_installs() -> list[Path]:
    """Return plausible BeamNG install directories, best guess first."""
    candidates: list[Path] = []
    env = os.environ.get("BEAMNG_INSTALL")
    if env:
        candidates.append(Path(env))
    candidates.extend(Path(root) / "steamapps" / "common" / "BeamNG.drive" for root in _STEAM_ROOTS)
    candidates.extend(
        Path(f"{letter}:\\SteamLibrary\\steamapps\\common\\BeamNG.drive")
        for letter in "DEFGH"
    )
    return candidates


def find_beamng_install() -> Path | None:
    """Locate a BeamNG.drive installation, or return ``None``.

    Honours the ``BEAMNG_INSTALL`` environment variable first so CI or a
    non-standard install can point at the game directly.

    Returns:
        Path to the installation root containing ``BeamNG.drive.exe``, or
        ``None`` when no installation is present.
    """
    for candidate in _candidate_installs():
        try:
            if (candidate / "BeamNG.drive.exe").is_file():
                return candidate
        except OSError:  # pragma: no cover - unreadable drive
            continue
    return None


def find_beamng_userdata() -> Path | None:
    """Locate BeamNG's user data folder (where ``mods/`` and ``levels/`` live).

    Modern BeamNG versions use ``%LOCALAPPDATA%\\BeamNG\\BeamNG.drive\\current``;
    older ones used ``Documents\\BeamNG.drive``.

    Returns:
        Path to the active user data folder, or ``None`` if not found.
    """
    env = os.environ.get("BEAMNG_USERDATA")
    if env and Path(env).is_dir():
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        modern = Path(local) / "BeamNG" / "BeamNG.drive"
        if modern.is_dir():
            current = modern / "current"
            return current if current.is_dir() else modern
    docs = Path.home() / "Documents" / "BeamNG.drive"
    return docs if docs.is_dir() else None


@pytest.fixture(scope="session")
def beamng_install() -> Path:
    """Session fixture yielding a BeamNG installation, skipping if absent."""
    found = find_beamng_install()
    if found is None:
        pytest.skip("no BeamNG.drive installation found (set BEAMNG_INSTALL to override)")
    return found


@pytest.fixture(scope="session")
def beamng_levels_dir(beamng_install: Path) -> Path:
    """Directory containing the shipped level archives."""
    return beamng_install / "content" / "levels"


def open_shipped_level(levels_dir: Path, level: str) -> zipfile.ZipFile:
    """Open a shipped level archive by name.

    Args:
        levels_dir: The installation's ``content/levels`` directory.
        level: Level identifier, e.g. ``"template"``.

    Returns:
        An open :class:`zipfile.ZipFile`. The caller must close it.
    """
    return zipfile.ZipFile(levels_dir / f"{level}.zip")
