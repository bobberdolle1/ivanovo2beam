"""Streaming OpenStreetMap XML parser.

Design constraints
------------------
A 5x5 km extract of a Russian city is a couple of megabytes, but the same code
path has to survive an Ivanovo Oblast regional extract, which is easily
several hundred megabytes of XML. Building a DOM with ``ElementTree.parse``
would hold one Python object per node, way, nd, tag and member -- hundreds of
bytes each, so a 30 million node extract would need tens of gigabytes. That is
not a theoretical worry: it is the difference between "the pipeline runs" and
"the machine swaps".

This module therefore uses :func:`xml.etree.ElementTree.iterparse` and drops
every top-level element as soon as it has been folded into the plain-data
structures of :mod:`ivanovo2beam.osm.types`. Peak memory is dominated by the
node dictionary we deliberately keep (~100 bytes per node), not by parser
overhead.

Robustness
----------
Real-world ``.osm`` files are messy: they are produced by many tools, they get
truncated by interrupted downloads, and they occasionally contain an element
with a non-numeric id or an unparseable coordinate. The parser is therefore
*tolerant by default*:

* a malformed ``node``/``way``/``relation``/``tag``/``nd``/``member`` is
  skipped and logged, the rest of the file is still parsed;
* an ``<osm>`` document with an unexpected top-level element (``<bounds>``,
  ``<meta>``, ``<note>``, ``<remark>``) is handled explicitly rather than
  blowing up;
* a truncated document returns the elements that were complete, with a
  warning -- pass ``strict=True`` to turn that into an exception.

The one thing that is never tolerated is an Overpass ``<remark>``: Overpass
reports query timeouts and syntax errors inside an HTTP 200 response via that
element, and silently returning a half-empty extract would poison every
downstream stage. See :class:`~ivanovo2beam.osm.types.OsmParseError`.
"""

from __future__ import annotations

import io
import logging
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import quoteattr

from ivanovo2beam.osm.types import (
    OsmData,
    OsmParseError,
    OsmRelation,
    OsmWay,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator, Mapping
    from xml.etree.ElementTree import Element

__all__ = ["parse_osm", "parse_osm_pbf"]

logger = logging.getLogger(__name__)

#: Read size handed to the XML parser. 64 KiB is large enough that a regional
#: extract is not dominated by read syscalls and small enough that the parse
#: buffer stays irrelevant next to the node dictionary.
_CHUNK = 1 << 16

#: Number of detailed "skipping malformed X" warnings emitted per tag before
#: the parser falls back to counting them silently. Keeps a corrupt regional
#: file from producing a million-line log.
_MAX_WARNINGS_PER_TAG = 10

#: Bytes inspected when guessing whether a file is JSON rather than XML.
_SNIFF_BYTES = 64


class _Skipped:
    """Counter for elements the parser had to drop, with capped logging."""

    __slots__ = ("_counts", "_warned")

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._warned: dict[str, int] = {}

    def note(self, kind: str, detail: str) -> None:
        """Record one dropped element and log it while under the cap."""
        count = self._counts.get(kind, 0) + 1
        self._counts[kind] = count
        warned = self._warned.get(kind, 0)
        if warned < _MAX_WARNINGS_PER_TAG:
            self._warned[kind] = warned + 1
            suffix = "" if warned + 1 < _MAX_WARNINGS_PER_TAG else " (further warnings suppressed)"
            logger.warning("skipping malformed <%s> element: %s%s", kind, detail, suffix)

    @property
    def total(self) -> int:
        """Total number of dropped elements."""
        return sum(self._counts.values())

    def summary(self) -> str:
        """Return ``"node=3, tag=1"``-style summary text."""
        return ", ".join(f"{kind}={count}" for kind, count in sorted(self._counts.items()))


# --------------------------------------------------------------------------- #
# Primitive coercion helpers
# --------------------------------------------------------------------------- #


def _localname(tag: object) -> str:
    """Return the local name of an XML tag, stripping any ``{namespace}``."""
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _as_int(value: str | None) -> int | None:
    """Parse an OSM id, returning ``None`` when it is absent or malformed."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: str | None) -> float | None:
    """Parse a coordinate, rejecting ``NaN``/``inf`` and non-numeric text."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _tags_of(elem: Element, skipped: _Skipped) -> dict[str, str]:
    """Collect ``<tag k v/>`` children of ``elem`` into a dictionary.

    Later tags with a duplicate key overwrite earlier ones, matching the
    behaviour of every other OSM reader.
    """
    tags: dict[str, str] = {}
    for child in elem:
        if _localname(child.tag) != "tag":
            continue
        key = child.get("k")
        value = child.get("v")
        if key is None or value is None:
            skipped.note("tag", f"missing k or v in <tag> of {_localname(elem.tag)} {elem.get('id')}")
            continue
        tags[key] = value
    return tags


# --------------------------------------------------------------------------- #
# Element handlers
# --------------------------------------------------------------------------- #


def _handle_node(elem: Element, data: OsmData, skipped: _Skipped) -> None:
    """Fold one ``<node>`` element into ``data``."""
    node_id = _as_int(elem.get("id"))
    if node_id is None:
        skipped.note("node", f"bad id {elem.get('id')!r}")
        return

    lon = _as_float(elem.get("lon"))
    lat = _as_float(elem.get("lat"))
    if lon is None or lat is None:
        skipped.note("node", f"bad coordinates lon={elem.get('lon')!r} lat={elem.get('lat')!r}")
        return
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        skipped.note("node", f"coordinates out of range lon={lon} lat={lat}")
        return

    data.nodes[node_id] = (lon, lat)
    tags = _tags_of(elem, skipped)
    if tags:
        data.node_tags[node_id] = tags


def _handle_way(elem: Element, data: OsmData, skipped: _Skipped) -> None:
    """Fold one ``<way>`` element into ``data``."""
    way_id = _as_int(elem.get("id"))
    if way_id is None:
        skipped.note("way", f"bad id {elem.get('id')!r}")
        return

    refs: list[int] = []
    for child in elem:
        if _localname(child.tag) != "nd":
            continue
        ref = _as_int(child.get("ref"))
        if ref is None:
            skipped.note("nd", f"bad ref {child.get('ref')!r} in way {way_id}")
            continue
        refs.append(ref)

    tags = _tags_of(elem, skipped)
    if len(refs) < 2:
        # A way needs two node references to describe anything at all; these
        # come from truncated or hand-edited files.
        skipped.note("way", f"id={way_id} has {len(refs)} usable node reference(s)")
        return

    data.ways.append(OsmWay(id=way_id, nodes=tuple(refs), tags=tags))


_MEMBER_TYPES = frozenset({"node", "way", "relation"})


def _handle_relation(elem: Element, data: OsmData, skipped: _Skipped) -> None:
    """Fold one ``<relation>`` element into ``data``."""
    rel_id = _as_int(elem.get("id"))
    if rel_id is None:
        skipped.note("relation", f"bad id {elem.get('id')!r}")
        return

    members: list[tuple[str, int, str]] = []
    for child in elem:
        if _localname(child.tag) != "member":
            continue
        member_type = child.get("type")
        ref = _as_int(child.get("ref"))
        if member_type not in _MEMBER_TYPES or ref is None:
            skipped.note(
                "member",
                f"type={member_type!r} ref={child.get('ref')!r} in relation {rel_id}",
            )
            continue
        members.append((member_type, ref, child.get("role") or ""))

    data.relations.append(OsmRelation(id=rel_id, members=tuple(members), tags=_tags_of(elem, skipped)))


# --------------------------------------------------------------------------- #
# Streaming driver
# --------------------------------------------------------------------------- #


def _iter_top_level(fh: io.TextIOBase) -> Iterator[Element]:
    """Yield completed direct children of the document root, freeing each one.

    ``iterparse`` still attaches every finished element to its parent, so the
    loop drops the root's children as soon as they have been handed out. That
    is what keeps memory proportional to the *data* rather than to the file.
    """
    context = ET.iterparse(fh, events=("start", "end"))
    stack: list[Element] = []
    root: Element | None = None
    root_tag = ""
    for event, elem in context:
        if event == "start":
            if not stack:
                root = elem
                root_tag = _localname(elem.tag)
            stack.append(elem)
            continue

        stack.pop()
        if len(stack) != 1:
            # Not a direct child of the root: the root itself ending, or a
            # deeply nested element whose ancestors are still open.
            continue

        yield elem

        if root is not None:
            root.clear()

    if root_tag and root_tag != "osm":
        raise OsmParseError(f"expected an <osm> root element, found <{root_tag}>")


def parse_osm(path: Path, *, strict: bool = False) -> OsmData:
    """Parse an OSM XML file into an :class:`~ivanovo2beam.osm.types.OsmData`.

    The file is streamed: only one top-level element is ever held in the XML
    tree, so peak memory tracks the size of the parsed result, not the size of
    the document.

    Accepted input is any OSM XML 0.6 document, including Overpass responses
    with ``<bounds>``, ``<meta>`` and ``<note>`` elements. The file is decoded
    as UTF-8 (a byte order mark is tolerated) with ``errors="replace"``, so a
    stray byte in a tag value degrades to U+FFFD instead of aborting a
    multi-minute download's worth of parsing.

    Args:
        path: Path to a ``.osm`` XML file. A ``.pbf`` path is forwarded to
            :func:`parse_osm_pbf`, which raises without ``pyosmium``.
        strict: When ``True``, a document that ends mid-element raises instead
            of returning the elements parsed so far.

    Returns:
        The parsed extract. ``OsmData.source`` is the string form of ``path``.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        OsmParseError: If the file is not OSM XML at all (HTML error page,
            JSON response, wrong root element), if it contains an Overpass
            ``<remark>``, or if it is truncated and ``strict=True``.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pbf":
        return parse_osm_pbf(path)
    if not path.exists():
        raise FileNotFoundError(f"OSM extract not found: {path}")
    if path.is_dir():
        raise OsmParseError(f"expected an OSM XML file, got a directory: {path}")

    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        head = fh.read(_SNIFF_BYTES)
        fh.seek(0)
        if head.lstrip()[:1] in {"{", "["}:
            raise OsmParseError(
                f"{path} looks like an Overpass JSON response, but only OSM XML is "
                "supported; request the extract with '[out:xml]' or convert it first"
            )
        data, parsed_elements, parse_error = _stream_osm(fh, str(path))

    if parse_error is not None:
        if parsed_elements == 0:
            raise OsmParseError(
                f"{path} is not valid OSM XML (no element could be parsed): {parse_error}"
            ) from parse_error
        if strict:
            raise OsmParseError(
                f"{path} ends with malformed XML after {parsed_elements} element(s): {parse_error}"
            ) from parse_error
        logger.warning(
            "%s ends with malformed XML after %d element(s); returning partial data (%s)",
            path,
            parsed_elements,
            parse_error,
        )

    return data


def _stream_osm(fh: io.TextIOBase, source: str) -> tuple[OsmData, int, ET.ParseError | None]:
    """Run the streaming loop over an open text stream.

    Args:
        fh: Open text stream positioned at the start of the document.
        source: Value for :attr:`OsmData.source`.

    Returns:
        ``(data, parsed_element_count, parse_error)``. The count is returned even
        when the document ends early, so the caller can tell "this file is
        garbage" (0 elements, an HTML error page) from "this file is truncated"
        (many elements, then the end of the data).
    """
    data = OsmData(source=source)
    skipped = _Skipped()
    parsed = 0
    parse_error: ET.ParseError | None = None

    try:
        for elem in _iter_top_level(fh):
            tag = _localname(elem.tag)
            if tag == "node":
                parsed += 1
                _handle_node(elem, data, skipped)
            elif tag == "way":
                parsed += 1
                _handle_way(elem, data, skipped)
            elif tag == "relation":
                parsed += 1
                _handle_relation(elem, data, skipped)
            elif tag == "remark":
                text = (elem.text or "").strip()
                raise OsmParseError(
                    "Overpass returned an error inside HTTP 200: "
                    f"<remark>{text}</remark>. The extract is incomplete; re-run the query "
                    "with a smaller bounding box or a longer timeout."
                )
            elif tag in {"bounds", "meta", "note", "osm", ""}:
                continue
            else:
                logger.debug("ignoring unknown top-level element <%s>", tag)
    except ET.ParseError as exc:
        parse_error = exc

    if skipped.total:
        logger.warning("%s: skipped %d malformed element(s) [%s]", source, skipped.total, skipped.summary())
    return data, parsed, parse_error


def parse_osm_pbf(path: Path) -> OsmData:  # pragma: no cover - always raises here
    """Parse an ``.osm.pbf`` extract -- **not available in this build**.

    The PBF container needs a protobuf/zlib decoder with the OSM schema, which
    means a new dependency. Rather than shipping a half-working parser this
    function refuses, and the pipeline is expected to convert PBF to XML with
    an external tool first::

        osmium cat -o extract.osm extract.osm.pbf
        # or:  osmconvert extract.osm.pbf -o=extract.osm

    Args:
        path: Path to the ``.osm.pbf`` file (unused).

    Raises:
        NotImplementedError: Always, naming the missing ``pyosmium`` extra.
    """
    raise NotImplementedError(
        f"cannot parse {Path(path).name}: reading .osm.pbf requires the optional 'pyosmium' "
        "dependency, which is not installed. Install it with "
        "`pip install 'ivanovo2beam[pbf]'`, or convert the extract to XML first "
        "(`osmium cat -o extract.osm extract.osm.pbf`) and call parse_osm() instead."
    )


def dumps_minimal(data: OsmData) -> str:
    """Serialise an :class:`OsmData` back to compact OSM XML.

    This exists for tests and debugging: it round-trips through
    :func:`parse_osm`, which makes fixtures and generated extracts easy to
    build without hand-writing XML. It is deliberately not a general OSM XML
    writer -- there is no version metadata, no ``<bounds>`` and no attribute
    ordering guarantee.

    Coordinates are written with :func:`repr`, which is the shortest string
    that parses back to the identical ``float``, and every attribute value goes
    through :func:`xml.sax.saxutils.quoteattr`, so tag values containing ``&``,
    ``<`` or quotes survive the round trip.

    Args:
        data: Extract to serialise.

    Returns:
        An OSM XML 0.6 document as a string.
    """

    def tag_lines(tags: Mapping[str, str]) -> list[str]:
        return [f"    <tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items()]

    out: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
    for node_id, (lon, lat) in data.nodes.items():
        attrs = f"id={quoteattr(str(node_id))} lat={quoteattr(repr(lat))} lon={quoteattr(repr(lon))}"
        tags = data.node_tags.get(node_id)
        if tags:
            out.append(f"  <node {attrs}>")
            out.extend(tag_lines(tags))
            out.append("  </node>")
        else:
            out.append(f"  <node {attrs}/>")
    for way in data.ways:
        out.append(f"  <way id={quoteattr(str(way.id))}>")
        out.extend(f'    <nd ref="{ref}"/>' for ref in way.nodes)
        out.extend(tag_lines(way.tags))
        out.append("  </way>")
    for rel in data.relations:
        out.append(f"  <relation id={quoteattr(str(rel.id))}>")
        out.extend(
            f'    <member type={quoteattr(mtype)} ref="{ref}" role={quoteattr(role)}/>'
            for mtype, ref, role in rel.members
        )
        out.extend(tag_lines(rel.tags))
        out.append("  </relation>")
    out.append("</osm>")
    return "\n".join(out) + "\n"
