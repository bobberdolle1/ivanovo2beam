"""Tests for the OpenStreetMap acquisition and parsing module.

The suite is split in two:

* **offline tests** drive the parser against hand-written fixtures in
  ``tests/fixtures/osm/`` and drive the fetch logic against a fake HTTP
  transport, so endpoint failover, rate-limit handling and cache identity are
  all checked deterministically and without touching the network;
* **one network test** (marked ``network``) fetches a real 2 x 2 km extract of
  central Ivanovo from the public Overpass API. It is the only proof that the
  query, the parser and the cache work against reality, so it is written to
  print -- not assert -- the feature-class breakdown.

Note on temporary directories
-----------------------------
These tests do not use pytest's ``tmp_path``. ``tempfile.mkdtemp`` creates its
directory with mode 0700, which a Windows sandboxed process (DSH workspace
confinement) is then not allowed to write into, so every scratch directory here
is created with default permissions via ``os.makedirs`` and removed afterwards.
"""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import tracemalloc
import urllib.error
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import formatdate
from pathlib import Path

import pytest

from ivanovo2beam.gis.projection import BBox
from ivanovo2beam.osm import (
    DEFAULT_FEATURE_ORDER,
    FEATURE_CLASSES,
    OsmData,
    OsmNode,
    OsmParseError,
    OsmRelation,
    OsmWay,
    OverpassError,
    cache_key,
    cache_paths,
    default_query,
    fetch_overpass,
    parse_osm,
    parse_osm_pbf,
    read_cache_meta,
)
from ivanovo2beam.osm import fetch as fetch_module

FIXTURES = Path(__file__).parent / "fixtures" / "osm"

#: The bounding box the task brief specifies: central Ivanovo, ~2 x 2 km.
IVANOVO_BBOX = BBox(west=40.96, south=56.99, east=40.98, north=57.01)

ENDPOINT_A = "https://mirror-a.invalid/api/interpreter"
ENDPOINT_B = "https://mirror-b.invalid/api/interpreter"


# --------------------------------------------------------------------------- #
# Scratch directories
# --------------------------------------------------------------------------- #


def _make_scratch(prefix: str) -> Path:
    """Create a scratch directory the sandboxed process can actually write to."""
    root = Path(tempfile.gettempdir()) / f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    os.makedirs(root, exist_ok=True)
    return root


@pytest.fixture
def work() -> Iterator[Path]:
    """A writable scratch directory, unique per test."""
    root = _make_scratch("ivanovo2beam-osm-test")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def overpass_cache() -> Iterator[Path]:
    """A scratch cache directory shared by the network test's two calls."""
    root = _make_scratch("ivanovo2beam-osm-network")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Fake HTTP transport
# --------------------------------------------------------------------------- #

#: A minimal but realistic Overpass response: the `<note>` and `<meta>` elements
#: every real answer carries, plus enough geometry to parse.
OSM_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="Overpass API 0.7.62.4 2390de5a">
  <note>The data included in this document is from www.openstreetmap.org.</note>
  <meta osm_base="2026-10-07T18:00:00Z"/>
  <bounds minlat="56.99" minlon="40.96" maxlat="57.01" maxlon="40.98"/>
  <node id="1" lat="56.9900" lon="40.9600"/>
  <node id="2" lat="56.9905" lon="40.9650"/>
  <way id="10">
    <nd ref="1"/>
    <nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
"""

#: What overpass.osm.ch actually returns with status 200 for a repeat query.
HTML_200_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<body><p><strong>Error</strong>: runtime error: duplicate_query</p></body>
</html>
"""

REMARK_PAYLOAD = b"""<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="Overpass API 0.7.62.4 2390de5a">
  <note>The data included in this document is from www.openstreetmap.org.</note>
  <meta osm_base="2026-10-07T18:00:00Z"/>
  <remark> runtime error: Query timed out in "query" at line 3 after 180 seconds. </remark>
</osm>
"""


class FakeResponse(io.BytesIO):
    """Stand-in for the object :func:`urllib.request.urlopen` returns."""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        super().__init__(payload)
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False


def http_error(code: int, retry_after: str | None = None, body: bytes = b"") -> urllib.error.HTTPError:
    """Build an :class:`urllib.error.HTTPError` for the fake transport."""
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://mirror.invalid/api/interpreter", code, f"HTTP {code}", headers, io.BytesIO(body)
    )


@dataclass
class Call:
    """One recorded HTTP attempt."""

    url: str
    query: str
    timeout: float
    headers: dict[str, str]


@dataclass
class FakeOverpass:
    """A scripted Overpass transport.

    Each endpoint is given a queue of responses. A queued response is either
    ``bytes`` (a 200 with that body) or an exception instance to raise.
    """

    routes: dict[str, list[object]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)
    offline: bool = False

    def route(self, endpoint: str, *items: object) -> FakeOverpass:
        """Queue responses for ``endpoint`` and return self for chaining."""
        self.routes.setdefault(endpoint, []).extend(items)
        return self

    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        if self.offline:
            raise AssertionError("the test went to the network while marked offline")
        body = request.data or b""
        query = urllib.parse.parse_qs(body.decode("utf-8")).get("data", [""])[0]
        self.calls.append(
            Call(
                url=request.full_url,
                query=query,
                timeout=timeout,
                headers={k.lower(): v for k, v in request.headers.items()},
            )
        )
        queue = self.routes.get(request.full_url)
        if not queue:
            raise AssertionError(f"unexpected request to {request.full_url}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            payload, status = item
            return FakeResponse(payload, status=status)
        return FakeResponse(item)

    @property
    def urls(self) -> list[str]:
        """The endpoints contacted, in order."""
        return [call.url for call in self.calls]


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> FakeOverpass:
    """Install a fake Overpass transport, with sleeping neutralised."""
    fake = FakeOverpass()
    monkeypatch.setattr(fetch_module, "_open", fake)
    monkeypatch.setattr(fetch_module, "_sleep", fake.slept.append)
    return fake


# --------------------------------------------------------------------------- #
# Fixtures for parsing
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def tiny() -> OsmData:
    """The well-formed fixture, parsed once for the whole module."""
    return parse_osm(FIXTURES / "tiny.osm")


# --------------------------------------------------------------------------- #
# Element model
# --------------------------------------------------------------------------- #


class TestElements:
    def test_node_coord_is_lon_lat(self) -> None:
        node = OsmNode(id=7, lon=40.96, lat=56.99, tags={"place": "city"})
        assert node.coord == (40.96, 56.99)
        assert node.tags["place"] == "city"

    def test_way_is_closed_requires_two_nodes(self) -> None:
        assert OsmWay(id=1, nodes=(5, 5)).is_closed
        assert not OsmWay(id=1, nodes=(5,)).is_closed
        assert not OsmWay(id=1, nodes=(5, 6)).is_closed

    def test_closed_building_is_an_area(self) -> None:
        way = OsmWay(id=1, nodes=(1, 2, 3, 4, 1), tags={"building": "yes"})
        assert way.is_area

    def test_closed_roundabout_is_not_an_area(self) -> None:
        way = OsmWay(id=1, nodes=(1, 2, 3, 4, 1), tags={"highway": "primary", "junction": "roundabout"})
        assert way.is_closed
        assert not way.is_area

    def test_area_tag_overrides_the_key_heuristic(self) -> None:
        as_area = OsmWay(id=1, nodes=(1, 2, 3, 4, 1), tags={"highway": "pedestrian", "area": "yes"})
        not_area = OsmWay(id=1, nodes=(1, 2, 3, 4, 1), tags={"building": "yes", "area": "no"})
        assert as_area.is_area
        assert not not_area.is_area

    def test_closed_coastline_is_not_an_area(self) -> None:
        way = OsmWay(id=1, nodes=(1, 2, 3, 4, 1), tags={"natural": "coastline"})
        assert not way.is_area

    def test_ring_needs_four_node_references(self) -> None:
        assert not OsmWay(id=1, nodes=(1, 2, 1), tags={"building": "yes"}).is_area

    def test_untagged_closed_ring_is_not_an_area(self) -> None:
        assert not OsmWay(id=1, nodes=(1, 2, 3, 4, 1)).is_area

    def test_relation_member_lookup(self) -> None:
        rel = OsmRelation(
            id=1,
            members=(("way", 5, "outer"), ("way", 6, "inner"), ("node", 7, "admin_centre")),
            tags={"type": "multipolygon"},
        )
        assert rel.type == "multipolygon"
        assert rel.members_of("way") == (5, 6)
        assert rel.members_of("way", "inner") == (6,)
        assert rel.members_of("node") == (7,)
        assert rel.members_of("relation") == ()
        assert OsmRelation(id=2, members=()).type == ""


class TestContainer:
    def test_len_counts_all_elements(self) -> None:
        data = OsmData(
            nodes={1: (0.0, 0.0)},
            ways=[OsmWay(id=1, nodes=(1, 1))],
            relations=[OsmRelation(id=1, members=())],
        )
        assert len(data) == 3

    def test_empty_and_stats(self) -> None:
        assert len(OsmData.empty("nowhere")) == 0
        assert OsmData.empty("nowhere").stats()["source"] == "nowhere"

    def test_repr_is_a_summary(self) -> None:
        data = OsmData(nodes=dict.fromkeys(range(1000), (0.0, 0.0)))
        assert repr(data).startswith("OsmData(nodes=1000")
        assert len(repr(data)) < 120

    def test_way_coords_skips_missing_nodes_but_keeps_order(self) -> None:
        data = OsmData(nodes={1: (40.0, 56.0), 3: (40.2, 56.2)})
        way = OsmWay(id=1, nodes=(1, 2, 3))
        assert data.way_coords(way) == [(40.0, 56.0), (40.2, 56.2)]

    def test_way_coords_of_a_fully_present_ring_stays_closed(self) -> None:
        data = OsmData(nodes={1: (0.0, 0.0), 2: (1.0, 0.0), 3: (1.0, 1.0)})
        way = OsmWay(id=1, nodes=(1, 2, 3, 1))
        coords = data.way_coords(way)
        assert coords[0] == coords[-1]
        assert len(coords) == 4

    def test_ways_with_tag_filters_by_value(self) -> None:
        data = OsmData(
            ways=[
                OsmWay(id=1, nodes=(1, 2), tags={"highway": "residential"}),
                OsmWay(id=2, nodes=(1, 2), tags={"highway": "primary"}),
                OsmWay(id=3, nodes=(1, 2), tags={"railway": "rail"}),
            ]
        )
        assert [w.id for w in data.ways_with_tag("highway")] == [1, 2]
        assert [w.id for w in data.ways_with_tag("highway", ["primary"])] == [2]
        assert [w.id for w in data.ways_with_tag("highway", ("primary", "residential"))] == [1, 2]
        assert data.ways_with_tag("highway", ["track"]) == []
        assert data.ways_with_tag("nope") == []
        assert data.count_ways_with_tag("highway") == 2
        assert data.count_ways_with_tag("highway", ["primary"]) == 1

    def test_relations_with_tag(self) -> None:
        data = OsmData(
            relations=[
                OsmRelation(id=1, members=(), tags={"type": "multipolygon"}),
                OsmRelation(id=2, members=(), tags={"type": "boundary"}),
            ]
        )
        assert [r.id for r in data.relations_with_tag("type", ["boundary"])] == [2]


# --------------------------------------------------------------------------- #
# Parsing the well-formed fixture
# --------------------------------------------------------------------------- #


class TestParseFixture:
    def test_element_counts(self, tiny: OsmData) -> None:
        assert len(tiny.nodes) == 26
        assert len(tiny.ways) == 6
        assert len(tiny.relations) == 2
        assert len(tiny) == 34

    def test_source_records_the_path(self, tiny: OsmData) -> None:
        assert tiny.source == str(FIXTURES / "tiny.osm")

    def test_coordinates_are_lon_lat(self, tiny: OsmData) -> None:
        assert tiny.nodes[101] == pytest.approx((40.9600, 56.9900))
        assert tiny.node_coord(105) == pytest.approx((40.9800, 56.9920))
        assert tiny.node_coord(99999) is None

    def test_tagged_nodes_keep_unicode_and_entities(self, tiny: OsmData) -> None:
        assert tiny.node_tags[121]["name"] == "Кафе «Уют»"
        assert tiny.node_tags[122]["name"] == "Хлеб & Соль"
        assert tiny.node_tags[120] == {"place": "suburb", "name": "Центр"}
        assert 101 not in tiny.node_tags, "untagged geometry nodes must not be stored"

    def test_way_tags_and_geometry(self, tiny: OsmData) -> None:
        leningradskaya = next(w for w in tiny.ways if w.id == 202)
        assert leningradskaya.tags["name"] == "улица Ленина"
        assert leningradskaya.tags["highway"] == "residential"
        assert leningradskaya.nodes == (101, 102, 103, 104, 105)
        assert tiny.way_coords(leningradskaya)[0] == pytest.approx((40.9600, 56.9900))

    def test_way_coords_skips_nodes_outside_the_extract(self, tiny: OsmData) -> None:
        river = next(w for w in tiny.ways if w.id == 204)
        assert river.nodes == (114, 115, 999)
        assert len(tiny.way_coords(river)) == 2

    def test_closed_and_area_classification(self, tiny: OsmData) -> None:
        by_id = {way.id: way for way in tiny.ways}
        assert by_id[201].is_closed and by_id[201].is_area  # building footprint
        assert by_id[203].is_closed and not by_id[203].is_area  # roundabout
        assert by_id[205].is_closed and not by_id[205].is_area  # untagged ring
        assert not by_id[202].is_closed and not by_id[202].is_area
        assert tiny.stats()["closed_ways"] == 4
        assert tiny.stats()["areas"] == 1

    def test_ways_with_tag_on_the_fixture(self, tiny: OsmData) -> None:
        assert [w.id for w in tiny.ways_with_tag("highway")] == [202, 203]
        assert [w.id for w in tiny.ways_with_tag("highway", ["primary"])] == [203]
        assert [w.id for w in tiny.ways_with_tag("building")] == [201]
        assert tiny.count_ways_with_tag("highway") == 2

    def test_relations_and_their_members(self, tiny: OsmData) -> None:
        pond, boundary = tiny.relations
        assert pond.id == 301
        assert pond.type == "multipolygon"
        assert pond.tags["name"] == "пруд у мельницы"  # noqa: RUF001 - real Russian text
        assert pond.members == (("way", 205, "outer"), ("way", 206, "inner"))
        assert pond.members_of("way", "inner") == (206,)
        assert boundary.members_of("node") == (120,)
        assert [r.id for r in tiny.relations_with_tag("type", ["multipolygon"])] == [301]

    def test_untagged_multipolygon_member_ways_are_kept(self, tiny: OsmData) -> None:
        member = next(w for w in tiny.ways if w.id == 205)
        assert member.tags == {}
        assert len(tiny.way_coords(member)) == 5

    def test_overpass_envelope_elements_are_tolerated(self, tiny: OsmData) -> None:
        # <bounds>, <meta> and <note> are present in the fixture; reaching this
        # point without an exception is the assertion.
        assert len(tiny.nodes) > 0


# --------------------------------------------------------------------------- #
# Parsing: robustness
# --------------------------------------------------------------------------- #


class TestParseRobustness:
    def test_malformed_elements_are_skipped_not_fatal(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="ivanovo2beam.osm.parse"):
            data = parse_osm(FIXTURES / "malformed.osm")

        assert sorted(data.nodes) == [1, 5, 6]
        assert [w.id for w in data.ways] == [10, 12]
        assert [r.id for r in data.relations] == [20]
        assert data.nodes[6] == pytest.approx((40.9605, 56.9905))
        assert 6 not in data.node_tags, "the <tag> without a v attribute must be dropped"
        assert data.relations[0].members == (("way", 10, "outer"),)
        assert data.ways[1].tags == {"highway": "track"}
        assert "skipping malformed" in caplog.text
        assert "skipped" in caplog.text

    def test_overpass_remark_raises(self) -> None:
        with pytest.raises(OsmParseError) as excinfo:
            parse_osm(FIXTURES / "remark.osm")
        message = str(excinfo.value)
        assert "remark" in message
        assert "Query timed out" in message

    def test_truncated_file_returns_partial_data_with_a_warning(
        self, work: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = (FIXTURES / "tiny.osm").read_bytes()
        truncated = work / "truncated.osm"
        truncated.write_bytes(raw[: int(len(raw) * 0.7)])

        with caplog.at_level(logging.WARNING, logger="ivanovo2beam.osm.parse"):
            data = parse_osm(truncated)
        assert len(data.nodes) > 0
        assert len(data) < 34
        assert "malformed XML" in caplog.text

        with pytest.raises(OsmParseError, match="malformed XML"):
            parse_osm(truncated, strict=True)

    def test_non_osm_document_raises(self, work: Path) -> None:
        html = work / "error.osm"
        html.write_text(
            "<!DOCTYPE html><html><body><h1>Error</h1><p>runtime error: duplicate_query</p></body></html>",
            encoding="utf-8",
        )
        with pytest.raises(OsmParseError):
            parse_osm(html)

    def test_json_response_raises_a_clear_error(self, work: Path) -> None:
        payload = work / "response.osm"
        payload.write_text('{"version": 0.6, "elements": []}', encoding="utf-8")
        with pytest.raises(OsmParseError, match="JSON"):
            parse_osm(payload)

    def test_empty_file_raises(self, work: Path) -> None:
        empty = work / "empty.osm"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(OsmParseError):
            parse_osm(empty)

    def test_empty_osm_document_is_valid(self, work: Path) -> None:
        empty = work / "empty-but-valid.osm"
        empty.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6"/>\n', "utf-8")
        data = parse_osm(empty)
        assert len(data) == 0
        assert data.source == str(empty)

    def test_missing_file_raises_filenotfound(self, work: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_osm(work / "does-not-exist.osm")

    def test_directory_raises(self, work: Path) -> None:
        with pytest.raises(OsmParseError, match="directory"):
            parse_osm(work)

    def test_unknown_top_level_elements_are_ignored(self, work: Path) -> None:
        path = work / "extra.osm"
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<osm version="0.6">\n'
            "  <something-new/>\n"
            '  <node id="1" lat="56.9" lon="40.9"/>\n'
            "</osm>\n",
            encoding="utf-8",
        )
        assert sorted(parse_osm(path).nodes) == [1]

    def test_pbf_is_refused_with_a_clear_message(self, work: Path) -> None:
        path = work / "extract.osm.pbf"
        path.write_bytes(b"\x00\x00\x00\x0dOSMHeader")
        with pytest.raises(NotImplementedError, match="pyosmium"):
            parse_osm(path)
        with pytest.raises(NotImplementedError, match="pyosmium"):
            parse_osm_pbf(path)

    def test_invalid_utf8_bytes_are_replaced_not_fatal(self, work: Path) -> None:
        path = work / "badbytes.osm"
        path.write_bytes(
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<osm version="0.6">\n'
            b'  <node id="1" lat="56.9" lon="40.9">\n'
            b'    <tag k="name" v="Ivanovo \xff\xfe street"/>\n'
            b"  </node>\n"
            b"</osm>\n"
        )
        data = parse_osm(path)
        assert sorted(data.nodes) == [1]
        assert "Ivanovo" in data.node_tags[1]["name"]


class TestStreaming:
    """Prove that parsing streams rather than building a DOM."""

    @staticmethod
    def _write_large(path: Path, node_count: int) -> Path:
        """Write a generated extract big enough for memory use to be visible."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">\n')
            for index in range(node_count):
                lon = 40.9 + (index % 1000) * 1e-5
                lat = 56.9 + (index // 1000) * 1e-5
                fh.write(f'  <node id="{index + 1}" lat="{lat:.7f}" lon="{lon:.7f}" version="3"/>\n')
            for way in range(node_count // 20):
                start = way * 20 + 1
                fh.write(f'  <way id="{1_000_000 + way}">\n')
                for ref in range(start, start + 10):
                    fh.write(f'    <nd ref="{ref}"/>\n')
                fh.write('    <tag k="highway" v="residential"/>\n  </way>\n')
            fh.write("</osm>\n")
        return path

    def test_streaming_uses_far_less_memory_than_a_dom(self, work: Path, capsys) -> None:
        node_count = 60_000
        path = self._write_large(work / "large.osm", node_count)
        assert path.stat().st_size > 3_000_000

        tracemalloc.start()
        tree = ET.parse(path)
        dom_peak = tracemalloc.get_traced_memory()[1]
        del tree
        tracemalloc.stop()

        tracemalloc.start()
        data = parse_osm(path)
        stream_peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        with capsys.disabled():
            print(
                f"\n[streaming] {node_count} nodes: DOM peak {dom_peak / 1e6:.1f} MB "
                f"vs streaming peak {stream_peak / 1e6:.1f} MB"
            )

        assert len(data.nodes) == node_count
        assert data.count_ways_with_tag("highway") == node_count // 20
        # A DOM holds one object per node/way/nd/tag; the streaming parser holds
        # one tuple per node. The gap must be unmistakable, not marginal.
        assert stream_peak < dom_peak * 0.6, (
            f"streaming peak {stream_peak} is not clearly below the DOM peak {dom_peak}; "
            "the parser may be materialising the document"
        )

    def test_round_trip_through_the_minimal_writer(self, work: Path, tiny: OsmData) -> None:
        from ivanovo2beam.osm.parse import dumps_minimal

        path = work / "round-trip.osm"
        path.write_text(dumps_minimal(tiny), encoding="utf-8")
        again = parse_osm(path)

        assert again.nodes == tiny.nodes
        assert [(w.id, w.nodes, dict(w.tags)) for w in again.ways] == [
            (w.id, w.nodes, dict(w.tags)) for w in tiny.ways
        ]
        assert [(r.id, r.members, dict(r.tags)) for r in again.relations] == [
            (r.id, r.members, dict(r.tags)) for r in tiny.relations
        ]


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #


class TestDefaultQuery:
    def test_contains_the_bbox_in_overpass_order(self) -> None:
        query = default_query(IVANOVO_BBOX)
        assert "(56.990000,40.960000,57.010000,40.980000)" in query
        assert query.startswith("[out:xml][timeout:180];")

    def test_uses_the_standard_recursion_idiom(self) -> None:
        query = default_query(IVANOVO_BBOX)
        assert "out body;>;out skel qt;" in query

    def test_asks_for_every_feature_class_the_generator_needs(self) -> None:
        query = default_query(IVANOVO_BBOX)
        assert 'way["highway"]' in query
        assert 'way["railway"]' in query
        assert 'way["waterway"]' in query
        assert 'nwr["natural"~"^(water|wood)$"]' in query
        assert 'nwr["landuse"]' in query
        assert 'nwr["building"]' in query
        assert 'nwr["boundary"]' in query
        assert 'nwr["place"]' in query
        assert 'way["power"="line"]' in query

    def test_query_is_structurally_sound(self) -> None:
        query = default_query(IVANOVO_BBOX)
        assert query.count("(") == query.count(")")
        assert query.count("[") == query.count("]")
        assert query.count('"') % 2 == 0

        lines = [line.strip() for line in query.splitlines() if line.strip()]
        assert lines[0].startswith("[out:xml]")
        assert lines[1] == "("
        assert lines[-2] == ");"
        assert lines[-1] == "out body;>;out skel qt;"
        statements = lines[2:-2]
        assert len(statements) == len(DEFAULT_FEATURE_ORDER)
        assert all(statement.endswith(";") for statement in statements)
        for name in DEFAULT_FEATURE_ORDER:
            assert f"{FEATURE_CLASSES[name]}(" in query

    def test_is_deterministic(self) -> None:
        assert default_query(IVANOVO_BBOX) == default_query(IVANOVO_BBOX)

    def test_rounds_coordinates_consistently(self) -> None:
        noisy = BBox(west=40.96 + 1e-12, south=56.99 - 1e-12, east=40.98, north=57.01)
        assert default_query(noisy) == default_query(IVANOVO_BBOX)

    def test_include_selects_a_subset(self) -> None:
        query = default_query(IVANOVO_BBOX, include=["highway", "building"])
        assert 'way["highway"]' in query
        assert 'nwr["building"]' in query
        assert "landuse" not in query
        assert "railway" not in query

    def test_include_can_add_an_optional_class(self) -> None:
        query = default_query(IVANOVO_BBOX, include=["highway", "leisure"])
        assert 'nwr["leisure"]' in query

    def test_unknown_feature_class_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown feature class"):
            default_query(IVANOVO_BBOX, include=["highway", "ski_lift"])

    def test_empty_include_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            default_query(IVANOVO_BBOX, include=[])

    def test_timeout_is_configurable(self) -> None:
        assert "[out:xml][timeout:600];" in default_query(IVANOVO_BBOX, timeout=600)


# --------------------------------------------------------------------------- #
# Cache identity
# --------------------------------------------------------------------------- #


class TestCacheKey:
    def test_is_a_64_character_hex_digest(self) -> None:
        key = cache_key(IVANOVO_BBOX)
        assert len(key) == 64
        assert set(key) <= set("0123456789abcdef")

    def test_is_stable_across_calls(self) -> None:
        assert cache_key(IVANOVO_BBOX) == cache_key(IVANOVO_BBOX)

    def test_is_frozen_for_a_known_request(self) -> None:
        # Golden value: if this changes, cached extracts are silently orphaned,
        # so the change must be deliberate (and bump _CACHE_KEY_VERSION).
        assert cache_key(IVANOVO_BBOX) == "325614316df88d95bd478a119c075789b59df06fcf56cd8aa03512eed0662e62"

    def test_ignores_float_noise_below_the_rounding_step(self) -> None:
        noisy = BBox(west=40.96 + 1e-9, south=56.99, east=40.98, north=57.01)
        assert cache_key(noisy) == cache_key(IVANOVO_BBOX)

    def test_is_sensitive_to_the_bounding_box(self) -> None:
        moved = BBox(west=40.97, south=56.99, east=40.98, north=57.01)
        assert cache_key(moved) != cache_key(IVANOVO_BBOX)

    def test_is_sensitive_to_an_explicit_query(self) -> None:
        assert cache_key(IVANOVO_BBOX, 'way["highway"](56.99,40.96,57.01,40.98);') != cache_key(IVANOVO_BBOX)

    def test_is_sensitive_to_the_feature_set(self) -> None:
        assert cache_key(IVANOVO_BBOX, include=["highway"]) != cache_key(IVANOVO_BBOX)

    def test_two_custom_queries_do_not_collide(self) -> None:
        first = cache_key(IVANOVO_BBOX, 'way["highway"](1,2,3,4);')
        second = cache_key(IVANOVO_BBOX, 'way["building"](1,2,3,4);')
        assert first != second

    def test_cache_paths_are_derived_from_the_key(self, work: Path) -> None:
        osm_path, meta_path = cache_paths(work, IVANOVO_BBOX)
        assert osm_path == work / f"{cache_key(IVANOVO_BBOX)}.osm"
        assert meta_path == work / f"{cache_key(IVANOVO_BBOX)}.json"
        assert osm_path.suffix == ".osm"


# --------------------------------------------------------------------------- #
# Fetching: offline behaviour
# --------------------------------------------------------------------------- #


class TestFetchOffline:
    def test_caches_the_payload_and_writes_a_sidecar(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, OSM_PAYLOAD)
        path = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A])

        assert path.exists()
        assert path.read_bytes() == OSM_PAYLOAD
        assert path.parent == work

        meta = json.loads((work / f"{cache_key(IVANOVO_BBOX)}.json").read_text(encoding="utf-8"))
        assert meta["bytes"] == len(OSM_PAYLOAD)
        assert meta["sha256"] == hashlib.sha256(OSM_PAYLOAD).hexdigest()
        assert meta["endpoint"] == ENDPOINT_A
        assert meta["endpoints_tried"] == [ENDPOINT_A]
        assert meta["request"]["bbox"] == {
            "west": 40.96,
            "south": 56.99,
            "east": 40.98,
            "north": 57.01,
        }
        assert meta["request"]["query"] == default_query(IVANOVO_BBOX)
        assert meta["request"]["overpass_bbox"] == "56.990000,40.960000,57.010000,40.980000"
        assert meta["provenance"]["generator"] == "Overpass API 0.7.62.4 2390de5a"
        assert meta["provenance"]["osm_base"] == "2026-10-07T18:00:00Z"
        fetched = datetime.strptime(meta["fetched_utc"], "%Y-%m-%dT%H:%M:%SZ")
        assert abs((datetime.now(UTC) - fetched.replace(tzinfo=UTC)).total_seconds()) < 300

        assert read_cache_meta(path) == meta
        assert parse_osm(path).count_ways_with_tag("highway") == 1

    def test_sends_a_descriptive_user_agent_and_the_query(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, OSM_PAYLOAD)
        fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A])

        call = net.calls[0]
        assert "ivanovo2beam" in call.headers["user-agent"]
        assert "github.com/bobberdolle1/ivanovo2beam" in call.headers["user-agent"]
        assert call.query == default_query(IVANOVO_BBOX)
        assert call.timeout == 300.0

    def test_custom_query_is_sent_verbatim(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, OSM_PAYLOAD)
        query = '[out:xml];way["highway"](56.99,40.96,57.01,40.98);out body;>;out skel qt;'
        path = fetch_overpass(IVANOVO_BBOX, work, query=query, endpoints=[ENDPOINT_A])

        assert net.calls[0].query == query
        assert path == cache_paths(work, IVANOVO_BBOX, query)[0]
        assert read_cache_meta(path)["request"]["query"] == query

    def test_blank_custom_query_is_rejected(self, net: FakeOverpass, work: Path) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            fetch_overpass(IVANOVO_BBOX, work, query="   ", endpoints=[ENDPOINT_A])

    def test_second_call_is_served_from_cache(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, OSM_PAYLOAD)
        first = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A])

        net.offline = True
        second = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A])
        assert second == first
        assert len(net.calls) == 1, "the cached extract must not be re-downloaded"

    def test_cache_written_by_an_earlier_run_is_reused(self, net: FakeOverpass, work: Path) -> None:
        """The cache key must depend on the request only, never on process state."""
        osm_path, meta_path = cache_paths(work, IVANOVO_BBOX)
        osm_path.write_bytes(OSM_PAYLOAD)
        meta_path.write_text(
            json.dumps({"bytes": len(OSM_PAYLOAD), "fetched_utc": "2026-10-07T18:00:00Z"}),
            encoding="utf-8",
        )

        net.offline = True
        assert fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A]) == osm_path
        assert net.calls == []

    def test_force_bypasses_the_cache(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, OSM_PAYLOAD, OSM_PAYLOAD)
        fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A])
        fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A], force=True)
        assert len(net.calls) == 2

    def test_short_cache_file_is_refetched(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, OSM_PAYLOAD, OSM_PAYLOAD)
        path = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A])

        path.write_bytes(OSM_PAYLOAD[: len(OSM_PAYLOAD) // 2])
        again = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A])
        assert again == path
        assert path.read_bytes() == OSM_PAYLOAD
        assert len(net.calls) == 2

    def test_missing_cache_directory_is_created(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, OSM_PAYLOAD)
        nested = work / "deeply" / "nested" / "cache"
        path = fetch_overpass(IVANOVO_BBOX, nested, endpoints=[ENDPOINT_A])
        assert path.parent == nested

    def test_empty_endpoint_list_is_rejected(self, net: FakeOverpass, work: Path) -> None:
        with pytest.raises(ValueError, match="at least one"):
            fetch_overpass(IVANOVO_BBOX, work, endpoints=[])


class TestFetchFailover:
    def test_falls_through_to_the_next_endpoint(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, http_error(504), http_error(504))
        net.route(ENDPOINT_B, OSM_PAYLOAD)

        path = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A, ENDPOINT_B])
        assert path.read_bytes() == OSM_PAYLOAD
        assert net.urls == [ENDPOINT_A, ENDPOINT_A, ENDPOINT_B]
        meta = read_cache_meta(path)
        assert meta["endpoint"] == ENDPOINT_B
        assert meta["endpoints_tried"] == [ENDPOINT_A, ENDPOINT_A, ENDPOINT_B]
        assert net.slept, "a retry must wait before hitting the mirror again"

    def test_retries_the_same_endpoint_before_moving_on(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, http_error(500), OSM_PAYLOAD)
        path = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A, ENDPOINT_B])
        assert net.urls == [ENDPOINT_A, ENDPOINT_A]
        assert path.read_bytes() == OSM_PAYLOAD

    def test_respects_retry_after_on_429(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, http_error(429, retry_after="7"), OSM_PAYLOAD)
        fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A, ENDPOINT_B])

        assert net.urls == [ENDPOINT_A, ENDPOINT_A]
        assert 7.0 in net.slept, f"Retry-After was not honoured: slept={net.slept}"

    def test_gives_up_on_an_endpoint_that_keeps_rate_limiting(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, http_error(429, retry_after="1"), http_error(429, retry_after="1"))
        net.route(ENDPOINT_B, OSM_PAYLOAD)
        path = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A, ENDPOINT_B])
        assert net.urls == [ENDPOINT_A, ENDPOINT_A, ENDPOINT_B]
        assert path.read_bytes() == OSM_PAYLOAD

    def test_html_with_status_200_is_not_accepted(self, net: FakeOverpass, work: Path) -> None:
        # This is exactly what overpass.osm.ch does for a repeated query: an
        # error page with HTTP 200. It must never reach the cache.
        net.route(ENDPOINT_A, HTML_200_PAYLOAD, HTML_200_PAYLOAD)
        net.route(ENDPOINT_B, OSM_PAYLOAD)

        path = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A, ENDPOINT_B])
        assert path.read_bytes() == OSM_PAYLOAD
        assert net.urls == [ENDPOINT_A, ENDPOINT_A, ENDPOINT_B]

    def test_remark_in_a_200_response_raises_and_caches_nothing(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, REMARK_PAYLOAD)
        net.route(ENDPOINT_B, REMARK_PAYLOAD)

        with pytest.raises(OverpassError) as excinfo:
            fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A, ENDPOINT_B])
        assert "Query timed out" in str(excinfo.value)
        # One attempt per endpoint: a timed-out query must not be repeated on
        # the mirror that just timed out.
        assert net.urls == [ENDPOINT_A, ENDPOINT_B]
        assert list(work.iterdir()) == [], "a failed fetch must leave nothing behind"

    def test_all_endpoints_failing_raises_with_every_reason(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, http_error(504), http_error(502))
        net.route(ENDPOINT_B, urllib.error.URLError("name resolution failed"), http_error(500))

        with pytest.raises(OverpassError) as excinfo:
            fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A, ENDPOINT_B])
        message = str(excinfo.value)
        assert ENDPOINT_A in message and ENDPOINT_B in message
        assert "504" in message and "502" in message
        assert "URLError" in message or "name resolution" in message
        assert list(work.iterdir()) == []

    def test_connection_reset_is_retried(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, TimeoutError("timed out"), OSM_PAYLOAD)
        path = fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A])
        assert net.urls == [ENDPOINT_A, ENDPOINT_A]
        assert path.read_bytes() == OSM_PAYLOAD

    def test_oversized_response_is_rejected(self, net: FakeOverpass, work: Path) -> None:
        net.route(ENDPOINT_A, OSM_PAYLOAD, OSM_PAYLOAD)
        with pytest.raises(OverpassError, match="exceeded"):
            fetch_overpass(IVANOVO_BBOX, work, endpoints=[ENDPOINT_A], max_bytes=64)
        assert list(work.iterdir()) == []


class TestRetryAfterParsing:
    def test_seconds(self) -> None:
        assert fetch_module.parse_retry_after("7") == pytest.approx(7.0)
        assert fetch_module.parse_retry_after(" 12 ") == pytest.approx(12.0)

    def test_is_capped(self) -> None:
        assert fetch_module.parse_retry_after("100000") == pytest.approx(fetch_module._MAX_RETRY_AFTER_S)

    def test_negative_is_clamped_to_zero(self) -> None:
        assert fetch_module.parse_retry_after("-30") == pytest.approx(0.0)

    def test_http_date(self) -> None:
        now = 1_800_000_000.0
        assert fetch_module.parse_retry_after(formatdate(now + 30, usegmt=True), now=now) == pytest.approx(
            30.0, abs=1.0
        )

    def test_http_date_in_the_past_is_zero(self) -> None:
        now = 1_800_000_000.0
        assert fetch_module.parse_retry_after(formatdate(now - 600, usegmt=True), now=now) == pytest.approx(
            0.0
        )

    def test_missing_or_garbage_falls_back_to_the_default(self) -> None:
        assert fetch_module.parse_retry_after(None, default=11.0) == pytest.approx(11.0)
        assert fetch_module.parse_retry_after("soon-ish", default=11.0) == pytest.approx(11.0)


class TestEndpoints:
    def test_defaults_are_https_interpreters(self) -> None:
        assert fetch_module.DEFAULT_ENDPOINTS, "at least one mirror is required"
        for endpoint in fetch_module.DEFAULT_ENDPOINTS:
            assert endpoint.startswith("https://")
            assert endpoint.endswith("/api/interpreter")

    def test_overpass_osm_ch_is_not_used(self) -> None:
        # Verified 2026-10-07: it serves Switzerland only, and answers a repeat
        # query with an HTML error page carrying HTTP 200.
        assert all("osm.ch" not in endpoint for endpoint in fetch_module.DEFAULT_ENDPOINTS)

    def test_no_duplicate_mirrors(self) -> None:
        assert len(set(fetch_module.DEFAULT_ENDPOINTS)) == len(fetch_module.DEFAULT_ENDPOINTS)

    def test_minimum_request_interval_is_polite(self) -> None:
        assert fetch_module.MIN_REQUEST_INTERVAL_S >= 1.0

    def test_user_agent_is_descriptive(self) -> None:
        assert "ivanovo2beam" in fetch_module.USER_AGENT
        assert "http" in fetch_module.USER_AGENT


# --------------------------------------------------------------------------- #
# The real thing
# --------------------------------------------------------------------------- #

#: Feature classes whose counts are printed (never asserted) for the report.
_REPORT_CLASSES = (
    ("highway", None),
    ("railway", None),
    ("waterway", None),
    ("natural", None),
    ("landuse", None),
    ("building", None),
    ("boundary", None),
    ("place", None),
    ("power", ["line"]),
    ("leisure", None),
    ("amenity", None),
    ("barrier", None),
)


@pytest.mark.network
def test_fetch_and_parse_central_ivanovo(
    overpass_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The end-to-end proof: real Overpass, real extract, real cache."""
    path = fetch_overpass(IVANOVO_BBOX, overpass_cache, timeout=300.0)

    assert path.exists()
    assert path.parent == overpass_cache
    assert path.stat().st_size > 5_000, "a 2x2 km city-centre extract must not be nearly empty"

    meta = read_cache_meta(path)
    assert meta is not None
    assert meta["endpoint"] in fetch_module.DEFAULT_ENDPOINTS
    assert hashlib.sha256(path.read_bytes()).hexdigest() == meta["sha256"]
    assert meta["bytes"] == path.stat().st_size
    assert meta["provenance"].get("osm_base"), "Overpass must report its database timestamp"

    data = parse_osm(path)
    highways = data.ways_with_tag("highway")
    assert len(highways) >= 20, f"only {len(highways)} highway ways in central Ivanovo"

    # 'out body;>;out skel qt;' must bring every node the ways reference, or the
    # road mesh would have holes at the edges of the extract.
    complete = sum(1 for way in data.ways if len(data.way_coords(way)) == len(way.nodes))
    assert complete >= len(data.ways) * 0.99, (
        f"only {complete}/{len(data.ways)} ways have all their nodes in the extract"
    )

    lines = [
        "",
        f"[OSM] central Ivanovo bbox {IVANOVO_BBOX.as_overpass()}",
        f"[OSM] endpoint {meta['endpoint']}",
        f"[OSM] {path.stat().st_size / 1024:.1f} KiB, sha256 {meta['sha256'][:16]}..., "
        f"osm_base {meta['provenance'].get('osm_base')}",
        f"[OSM] nodes {len(data.nodes)} (tagged {len(data.node_tags)}), "
        f"ways {len(data.ways)}, relations {len(data.relations)}",
        f"[OSM] closed ways {data.stats()['closed_ways']}, areas {data.stats()['areas']}, "
        f"ways with complete geometry {complete}/{len(data.ways)}",
    ]
    for key, values in _REPORT_CLASSES:
        lines.append(f"[OSM]   ways with {key}={values or '*'}: {data.count_ways_with_tag(key, values)}")
    counts: dict[str, int] = {}
    for way in highways:
        counts[way.tags["highway"]] = counts.get(way.tags["highway"], 0) + 1
    lines.append(
        "[OSM]   highway breakdown: "
        + ", ".join(f"{value}={count}" for value, count in sorted(counts.items(), key=lambda kv: -kv[1]))
    )
    lines.append(
        "[OSM]   buildings: "
        + ", ".join(
            f"{value}={data.count_ways_with_tag('building', [value])}"
            for value in ("yes", "house", "apartments", "garage", "commercial", "industrial")
            if data.count_ways_with_tag("building", [value])
        )
    )
    with capsys.disabled():
        print("\n".join(lines))

    # Second call: same request, same path, no network at all.
    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the second call must be served from cache")

    monkeypatch.setattr(fetch_module, "_open", _explode)
    monkeypatch.setattr(fetch_module, "_sleep", lambda seconds: None)
    again = fetch_overpass(IVANOVO_BBOX, overpass_cache)
    assert again == path
    assert len(parse_osm(again)) == len(data)
