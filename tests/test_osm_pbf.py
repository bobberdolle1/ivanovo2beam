"""Unit tests for OpenStreetMap .osm.pbf pure-Python and accelerated parser."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from ivanovo2beam.gis.projection import BBox
from ivanovo2beam.osm import (
    OsmData,
    OsmParseError,
    OsmRelation,
    OsmWay,
    parse_osm,
    parse_osm_pbf,
    write_osm_pbf,
)


def _build_sample_osm_data() -> OsmData:
    """Build a rich synthetic OsmData extract for testing."""
    data = OsmData(source="synthetic_test")
    # Coordinates in Ivanovo area
    data.nodes = {
        1001: (41.5136, 57.4586),
        1002: (41.5140, 57.4590),
        1003: (41.5150, 57.4600),
        1004: (41.5200, 57.4700),  # Further away
        2001: (40.9739, 56.9998),  # Outside Plyos bbox
        2002: (40.9800, 57.0100),
    }
    data.node_tags = {
        1001: {"amenity": "cafe", "name": "Volga View"},
        2001: {"place": "city", "name": "Ivanovo"},
    }
    data.ways = [
        OsmWay(
            id=5001,
            nodes=(1001, 1002, 1003),
            tags={"highway": "secondary", "name": "Volga Embankment", "lanes": "2"},
        ),
        OsmWay(
            id=5002,
            nodes=(1001, 1002, 1003, 1001),
            tags={"building": "yes", "name": "Historic Manor"},
        ),
        OsmWay(
            id=5003,
            nodes=(2001, 2002),
            tags={"highway": "primary", "name": "Lenin Prospekt"},
        ),
    ]
    data.relations = [
        OsmRelation(
            id=9001,
            members=(
                ("way", 5002, "outer"),
                ("node", 1001, "admin_centre"),
            ),
            tags={"type": "multipolygon", "name": "Manor Complex"},
        ),
        OsmRelation(
            id=9002,
            members=(("way", 5003, "outer"),),
            tags={"type": "route", "route": "road"},
        ),
    ]
    return data


def test_round_trip_pure_python_pbf(tmp_path: Path) -> None:
    """Verify lossless encoding and pure-Python parsing of nodes, ways, and relations."""
    original = _build_sample_osm_data()
    pbf_file = tmp_path / "test_extract.osm.pbf"

    write_osm_pbf(pbf_file, original)
    assert pbf_file.exists()
    assert pbf_file.stat().st_size > 0

    parsed = parse_osm_pbf(pbf_file)

    # Check nodes
    assert len(parsed.nodes) == len(original.nodes)
    for nid, (lon, lat) in original.nodes.items():
        assert nid in parsed.nodes
        p_lon, p_lat = parsed.nodes[nid]
        assert pytest.approx(lon, abs=1e-6) == p_lon
        assert pytest.approx(lat, abs=1e-6) == p_lat

    # Check tagged nodes
    assert len(parsed.node_tags) == len(original.node_tags)
    assert parsed.node_tags[1001] == {"amenity": "cafe", "name": "Volga View"}
    assert parsed.node_tags[2001] == {"place": "city", "name": "Ivanovo"}

    # Check ways
    assert len(parsed.ways) == len(original.ways)
    w_map = {w.id: w for w in parsed.ways}
    assert 5001 in w_map
    assert w_map[5001].nodes == (1001, 1002, 1003)
    assert w_map[5001].tags["highway"] == "secondary"
    assert w_map[5002].is_closed is True
    assert w_map[5002].is_area is True

    # Check relations
    assert len(parsed.relations) == len(original.relations)
    r_map = {r.id: r for r in parsed.relations}
    assert 9001 in r_map
    assert r_map[9001].type == "multipolygon"
    assert r_map[9001].members == (("way", 5002, "outer"), ("node", 1001, "admin_centre"))


def test_bbox_filtering_two_pass(tmp_path: Path) -> None:
    """Verify that two-pass streaming with bbox extracts only relevant elements."""
    original = _build_sample_osm_data()
    pbf_file = tmp_path / "test_bbox.osm.pbf"
    write_osm_pbf(pbf_file, original)

    # BBox around Plyos only (contains 1001, 1002, 1003)
    plyos_bbox = BBox(west=41.5100, south=57.4500, east=41.5160, north=57.4650)

    parsed = parse_osm_pbf(pbf_file, bbox=plyos_bbox)

    # Should contain Plyos nodes
    assert 1001 in parsed.nodes
    assert 1002 in parsed.nodes
    assert 1003 in parsed.nodes
    # Should NOT contain Ivanovo nodes (2001, 2002)
    assert 2001 not in parsed.nodes
    assert 2002 not in parsed.nodes

    # Should contain Plyos ways (5001, 5002), not Ivanovo way 5003
    way_ids = {w.id for w in parsed.ways}
    assert 5001 in way_ids
    assert 5002 in way_ids
    assert 5003 not in way_ids

    # Should contain relation 9001, not 9002
    rel_ids = {r.id for r in parsed.relations}
    assert 9001 in rel_ids
    assert 9002 not in rel_ids


def test_parse_osm_dispatcher(tmp_path: Path) -> None:
    """Verify that parse_osm() informs users to call parse_osm_pbf() for .pbf files."""
    original = _build_sample_osm_data()
    pbf_file = tmp_path / "auto.osm.pbf"
    write_osm_pbf(pbf_file, original)

    with pytest.raises(NotImplementedError, match="parse_osm_pbf"):
        parse_osm(pbf_file)

    # Calling parse_osm_pbf directly succeeds
    result = parse_osm_pbf(pbf_file)
    assert isinstance(result, OsmData)
    assert len(result.nodes) == len(original.nodes)
    assert len(result.ways) == len(original.ways)


def test_error_handling(tmp_path: Path) -> None:
    """Verify expected error behaviour on missing or corrupt files."""
    missing = tmp_path / "nonexistent.osm.pbf"
    with pytest.raises(FileNotFoundError, match="not found"):
        parse_osm_pbf(missing)

    folder = tmp_path / "a_dir"
    folder.mkdir()
    with pytest.raises(OsmParseError, match="directory"):
        parse_osm_pbf(folder)

    # Corrupt header length
    corrupt = tmp_path / "corrupt.osm.pbf"
    corrupt.write_bytes(b"xyz")
    with pytest.raises(OsmParseError, match="Truncated"):
        parse_osm_pbf(corrupt)

    # Empty payload
    empty_block = tmp_path / "empty_block.osm.pbf"
    # Header length 10, but file only has 4 bytes
    empty_block.write_bytes(struct.pack(">I", 10))
    with pytest.raises(OsmParseError):
        parse_osm_pbf(empty_block)


def test_dense_nodes_empty_tags(tmp_path: Path) -> None:
    """Verify dense nodes with no tags round-trip properly."""
    data = OsmData(source="un-tagged")
    data.nodes = {10: (41.0, 57.0), 20: (41.1, 57.1)}
    pbf_file = tmp_path / "no_tags.osm.pbf"
    write_osm_pbf(pbf_file, data)

    parsed = parse_osm_pbf(pbf_file)
    assert len(parsed.nodes) == 2
    assert parsed.node_tags == {}


def test_bbox_as_tuple_filter(tmp_path: Path) -> None:
    """Verify parse_osm_pbf works with bbox as a 4-element tuple."""
    original = _build_sample_osm_data()
    pbf_file = tmp_path / "tuple_bbox.osm.pbf"
    write_osm_pbf(pbf_file, original)

    # (west, south, east, north)
    parsed = parse_osm_pbf(pbf_file, bbox=(41.5100, 57.4500, 41.5160, 57.4650))
    assert 1001 in parsed.nodes
    assert 2001 not in parsed.nodes


def test_negative_coords_and_large_deltas(tmp_path: Path) -> None:
    """Verify negative lat/lon coordinates and large delta steps."""
    data = OsmData(source="world_extremes")
    data.nodes = {
        1: (-73.9851, 40.7488),   # New York (W, N)
        100_000_000: (-0.1276, 51.5074),  # London
        200_000_000: (151.2093, -33.8688), # Sydney (E, S)
    }
    pbf_file = tmp_path / "extremes.osm.pbf"
    write_osm_pbf(pbf_file, data)

    parsed = parse_osm_pbf(pbf_file)
    assert len(parsed.nodes) == 3
    for nid, (lon, lat) in data.nodes.items():
        p_lon, p_lat = parsed.nodes[nid]
        assert pytest.approx(lon, abs=1e-6) == p_lon
        assert pytest.approx(lat, abs=1e-6) == p_lat
