"""ODbL and Copernicus DEM provenance, attribution and manifest generation.

Design & Legal Architecture
---------------------------
Ivanovo2Beam generates game environments derived from open geospatial datasets:
1. OpenStreetMap: Licensed under the Open Database License (ODbL) 1.0.
   Section 4.3 of the ODbL requires any Produced Work to provide a notice
   attributing the database to OpenStreetMap and referencing the ODbL.
2. Copernicus DEM (GLO-30): Licensed under the Copernicus DEM Instrument Licence /
   European Space Agency. Requires copyright attribution to the European Union and
   ESA Copernicus Programme.

This module ensures full legal compliance and reproducibility by:
1. Tracking the SHA-256 digests, sizes, timestamps, source URLs, and cache
   sidecar metadata of all ingested OSM and DEM files.
2. Writing ``attribution.json`` both inside the level directory (``levels/<id>/attribution.json``)
   for offline distribution with mod archives, and in the build root (``build/attribution.json``).
3. Providing helpers to append human-readable attribution notices directly into
   BeamNG's ``info.json`` description so players and reviewers see required credits
   in the level selector UI.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "COPERNICUS_COPYRIGHT",
    "COPERNICUS_LICENSE_NAME",
    "COPERNICUS_LICENSE_URL",
    "COPERNICUS_NOTICE",
    "ODBL_COPYRIGHT",
    "ODBL_LICENSE_NAME",
    "ODBL_LICENSE_URL",
    "ODBL_NOTICE",
    "append_attribution_to_description",
    "build_attribution_manifest",
    "compute_file_sha256",
    "format_attribution_notice",
    "read_sidecar_meta",
    "write_attribution_files",
]

# --------------------------------------------------------------------------- #
# Legal Notices & License Metadata
# --------------------------------------------------------------------------- #

ODBL_LICENSE_NAME = "Open Database License 1.0 (ODbL 1.0)"
ODBL_LICENSE_URL = "https://opendatacommons.org/licenses/odbl/1.0/"
ODBL_COPYRIGHT = "© OpenStreetMap contributors"
ODBL_NOTICE = (
    "Map data © OpenStreetMap contributors, available under the "
    "Open Database License (ODbL) 1.0 — https://www.openstreetmap.org/copyright"
)

COPERNICUS_LICENSE_NAME = "Copernicus DEM Instrument Licence"
COPERNICUS_LICENSE_URL = (
    "https://spacedata.copernicus.eu/documents/20126/0/Copernicus+DEM+Licence+Agreement.pdf"
)
COPERNICUS_COPYRIGHT = (
    "© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under "
    "COPERNICUS by the European Union and ESA; all rights reserved."
)
COPERNICUS_NOTICE = (
    "Elevation data derived from Copernicus DEM GLO-30 © European Union, "
    "ESA Copernicus Programme."
)


# --------------------------------------------------------------------------- #
# Hashing & Sidecar Helpers
# --------------------------------------------------------------------------- #


def compute_file_sha256(path: Path | str, chunk_size: int = 65536) -> str:
    """Compute the hexadecimal SHA-256 digest of a file.

    Args:
        path: Path to the target file.
        chunk_size: Streaming read buffer size in bytes.

    Returns:
        64-character lowercase hexadecimal hash, or ``""`` if the file does not exist.
    """
    path_obj = Path(path)
    if not path_obj.is_file():
        return ""

    hasher = hashlib.sha256()
    with open(path_obj, "rb") as fh:
        while chunk := fh.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def read_sidecar_meta(path: Path | str) -> dict[str, Any]:
    """Read the companion ``.json`` sidecar metadata for a cached asset.

    Args:
        path: Path to the raw data file (e.g. ``.osm``, ``.osm.pbf``, ``.tif``).

    Returns:
        Parsed dictionary of sidecar contents, or ``{}`` if missing or malformed.
    """
    candidate = Path(path)
    if candidate.suffix.lower() != ".json":
        candidate = candidate.with_suffix(".json")

    if not candidate.is_file():
        return {}

    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _inspect_source_file(path: Path | str) -> dict[str, Any]:
    """Inspect a data file and extract filename, SHA-256, size, and sidecar info."""
    p = Path(path)
    sidecar = read_sidecar_meta(p)
    info: dict[str, Any] = {
        "filename": p.name,
        "path": str(p),
        "exists": p.is_file(),
    }
    if p.is_file():
        info["size_bytes"] = p.stat().st_size
        info["sha256"] = compute_file_sha256(p)
    else:
        info["size_bytes"] = 0
        info["sha256"] = ""

    # Extract useful fields from sidecar if present
    if sidecar:
        info["sidecar"] = sidecar
        if "url" in sidecar:
            info["source_url"] = sidecar["url"]
        elif "endpoint" in sidecar:
            info["source_url"] = sidecar["endpoint"]
        if "fetched_at" in sidecar:
            info["timestamp"] = sidecar["fetched_at"]
        elif "timestamp" in sidecar:
            info["timestamp"] = sidecar["timestamp"]

    return info


# --------------------------------------------------------------------------- #
# Manifest Generation
# --------------------------------------------------------------------------- #


def build_attribution_manifest(
    level_id: str,
    *,
    osm_sources: Sequence[Path | str] = (),
    dem_sources: Sequence[Path | str] = (),
    extra_sources: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Construct an ``attribution.json`` provenance and licensing manifest.

    Args:
        level_id: BeamNG level identifier.
        osm_sources: Sequence of OSM cache files (``.osm``, ``.osm.pbf``).
        dem_sources: Sequence of DEM cache files (``.tif``, etc.).
        extra_sources: Optional additional source descriptors.

    Returns:
        Structured dictionary adhering to the Ivanovo2Beam attribution schema.
    """
    now_iso = datetime.now(UTC).isoformat()

    sources: list[dict[str, Any]] = []

    # 1. OpenStreetMap source entry
    osm_file_records = [_inspect_source_file(src) for src in osm_sources]
    sources.append(
        {
            "id": "osm",
            "name": "OpenStreetMap",
            "license": ODBL_LICENSE_NAME,
            "license_url": ODBL_LICENSE_URL,
            "copyright": ODBL_COPYRIGHT,
            "notice": ODBL_NOTICE,
            "files": osm_file_records,
        }
    )

    # 2. Copernicus DEM GLO-30 source entry
    dem_file_records = [_inspect_source_file(src) for src in dem_sources]
    sources.append(
        {
            "id": "copernicus_glo30",
            "name": "Copernicus DEM (GLO-30)",
            "license": COPERNICUS_LICENSE_NAME,
            "license_url": COPERNICUS_LICENSE_URL,
            "copyright": COPERNICUS_COPYRIGHT,
            "notice": COPERNICUS_NOTICE,
            "files": dem_file_records,
        }
    )

    # 3. Any additional sources (e.g. imagery, satellite, custom models)
    for extra in extra_sources:
        sources.append(extra)

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "generator": "ivanovo2beam",
        "level_id": level_id,
        "generated_at": now_iso,
        "summary_notice": format_attribution_notice(),
        "sources": sources,
    }
    return manifest


def write_attribution_files(
    manifest: dict[str, Any],
    *,
    level_dir: Path | str | None = None,
    build_dir: Path | str | None = None,
) -> list[Path]:
    """Write ``attribution.json`` to distribution and build locations.

    Args:
        manifest: The dictionary produced by :func:`build_attribution_manifest`.
        level_dir: Path to the level mod directory (``levels/<id>``). When provided,
            writes ``levels/<id>/attribution.json`` for offline mod distribution.
        build_dir: Path to the build root directory. When provided, writes
            ``build/attribution.json``.

    Returns:
        List of written ``Path`` objects.
    """
    written: list[Path] = []
    content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

    targets: list[Path] = []
    if level_dir is not None:
        targets.append(Path(level_dir) / "attribution.json")
    if build_dir is not None:
        targets.append(Path(build_dir) / "attribution.json")

    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(target)

    return written


# --------------------------------------------------------------------------- #
# Human-Readable Formatting & info.json Integration
# --------------------------------------------------------------------------- #


def format_attribution_notice(
    *,
    include_osm: bool = True,
    include_dem: bool = True,
) -> str:
    """Format a human-readable attribution string for UI or documentation."""
    lines: list[str] = ["Data sources & legal attribution:"]
    if include_osm:
        lines.append(f"- {ODBL_NOTICE}")
    if include_dem:
        lines.append(f"- {COPERNICUS_NOTICE}")
    return "\n".join(lines)


def append_attribution_to_description(
    description: str,
    notice: str | None = None,
) -> str:
    """Append the required legal attribution notice into an ``info.json`` description.

    If the description already contains attribution markers (e.g. "ODbL" or
    "OpenStreetMap contributors"), it is returned unchanged to prevent duplicates.

    Args:
        description: Existing level description string.
        notice: Custom attribution notice, or ``None`` to use default formatted notice.

    Returns:
        Updated description string with attribution appended.
    """
    existing = description.strip()
    if "ODbL" in existing or "OpenStreetMap" in existing:
        return existing

    notice_to_append = notice.strip() if notice is not None else format_attribution_notice()
    if not existing:
        return notice_to_append

    return f"{existing}\n\n{notice_to_append}"
