"""Unit tests for ODbL and Copernicus provenance and attribution tracking."""

from __future__ import annotations

import json
from pathlib import Path

from ivanovo2beam.provenance import (
    COPERNICUS_LICENSE_NAME,
    COPERNICUS_NOTICE,
    ODBL_LICENSE_NAME,
    ODBL_NOTICE,
    append_attribution_to_description,
    build_attribution_manifest,
    compute_file_sha256,
    format_attribution_notice,
    read_sidecar_meta,
    write_attribution_files,
)


def test_compute_file_sha256(tmp_path: Path) -> None:
    """Verify SHA-256 hashing of files."""
    test_file = tmp_path / "sample.txt"
    test_file.write_text("ivanovo2beam open data", encoding="utf-8")

    # Calculate via python's hashlib to be independent of newline differences
    import hashlib

    expected = hashlib.sha256(b"ivanovo2beam open data").hexdigest()
    assert compute_file_sha256(test_file) == expected

    missing = tmp_path / "absent.txt"
    assert compute_file_sha256(missing) == ""


def test_read_sidecar_meta(tmp_path: Path) -> None:
    """Verify reading companion sidecar metadata."""
    data_file = tmp_path / "cache_extract.osm"
    data_file.write_bytes(b"<osm></osm>")

    sidecar_file = tmp_path / "cache_extract.json"
    sidecar_file.write_text(
        json.dumps({"url": "https://overpass-api.de", "fetched_at": "2026-11-24T12:00:00Z"}),
        encoding="utf-8",
    )

    meta = read_sidecar_meta(data_file)
    assert meta["url"] == "https://overpass-api.de"
    assert meta["fetched_at"] == "2026-11-24T12:00:00Z"

    # Missing sidecar
    no_sidecar = tmp_path / "no_sidecar.tif"
    assert read_sidecar_meta(no_sidecar) == {}


def test_build_attribution_manifest(tmp_path: Path) -> None:
    """Verify attribution manifest construction with file hashes and license data."""
    osm_file = tmp_path / "extract.osm.pbf"
    osm_file.write_bytes(b"mock_osm_pbf_data")
    osm_sidecar = tmp_path / "extract.osm.json"
    osm_sidecar.write_text(
        json.dumps({"url": "https://download.geofabrik.de/russia/central-fed-district.html"}),
        encoding="utf-8",
    )

    dem_file = tmp_path / "elevation.tif"
    dem_file.write_bytes(b"mock_dem_geotiff")
    dem_sidecar = tmp_path / "elevation.json"
    dem_sidecar.write_text(
        json.dumps({"url": "https://copernicus-dem-30m.s3.amazonaws.com"}),
        encoding="utf-8",
    )

    manifest = build_attribution_manifest(
        "plyos_volga",
        osm_sources=[osm_file],
        dem_sources=[dem_file],
    )

    assert manifest["level_id"] == "plyos_volga"
    assert manifest["generator"] == "ivanovo2beam"
    assert "generated_at" in manifest

    source_map = {s["id"]: s for s in manifest["sources"]}
    assert "osm" in source_map
    assert "copernicus_glo30" in source_map

    osm_src = source_map["osm"]
    assert osm_src["license"] == ODBL_LICENSE_NAME
    assert "ODbL" in osm_src["notice"]
    assert len(osm_src["files"]) == 1
    assert osm_src["files"][0]["filename"] == "extract.osm.pbf"
    assert osm_src["files"][0]["size_bytes"] == len(b"mock_osm_pbf_data")
    assert len(osm_src["files"][0]["sha256"]) == 64
    assert osm_src["files"][0]["source_url"] == "https://download.geofabrik.de/russia/central-fed-district.html"

    dem_src = source_map["copernicus_glo30"]
    assert dem_src["license"] == COPERNICUS_LICENSE_NAME
    assert "Copernicus DEM" in dem_src["notice"]
    assert len(dem_src["files"]) == 1
    assert dem_src["files"][0]["filename"] == "elevation.tif"
    assert dem_src["files"][0]["size_bytes"] == len(b"mock_dem_geotiff")


def test_write_attribution_files(tmp_path: Path) -> None:
    """Verify writing attribution.json to level directory and build root."""
    level_dir = tmp_path / "levels" / "plyos_volga"
    build_dir = tmp_path / "build"

    manifest = build_attribution_manifest("plyos_volga")
    written = write_attribution_files(manifest, level_dir=level_dir, build_dir=build_dir)

    assert len(written) == 2
    level_attr = level_dir / "attribution.json"
    build_attr = build_dir / "attribution.json"
    assert level_attr.is_file()
    assert build_attr.is_file()

    loaded_level = json.loads(level_attr.read_text(encoding="utf-8"))
    assert loaded_level["level_id"] == "plyos_volga"
    loaded_build = json.loads(build_attr.read_text(encoding="utf-8"))
    assert loaded_build["level_id"] == "plyos_volga"


def test_append_attribution_to_description() -> None:
    """Verify info.json description decoration and idempotence."""
    initial_desc = "Scenic Volga bluffs and historic merchant houses."
    decorated = append_attribution_to_description(initial_desc)

    assert initial_desc in decorated
    assert "OpenStreetMap" in decorated
    assert "Copernicus DEM" in decorated
    assert "ODbL" in decorated

    # Second pass should be a no-op (idempotent)
    double_decorated = append_attribution_to_description(decorated)
    assert double_decorated == decorated


def test_format_attribution_notice() -> None:
    """Verify formatted string notices."""
    notice = format_attribution_notice()
    assert ODBL_NOTICE in notice
    assert COPERNICUS_NOTICE in notice
