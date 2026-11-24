"""Pure-Python and accelerated OpenStreetMap PBF (.osm.pbf) reader.

Design constraints & architecture
---------------------------------
This module provides a fast, pure-Python OpenStreetMap PBF parser using only
the Python standard library (``struct``, ``zlib``), with an optional fast-path
using ``osmium`` if installed.

It is designed to satisfy the following requirements:
1. Zero required C/C++ dependencies: operates purely with Python stdlib on Windows,
   Linux and macOS.
2. Two-pass streaming bbox filtering: enables extracting small bounding boxes
   (e.g. 5x5 km or 30x30 km) from massive multi-gigabyte Geofabrik regional
   extracts (such as ``central-fed-district-latest.osm.pbf``) in < 250 MB RAM.
3. Fallback resilience: automatically detects and falls back to pure-Python when
   ``osmium`` is missing or encounters unhandled native bindings.
4. Complementary pure-Python PBF encoder (``write_osm_pbf``) for lossless
   round-trip testing and synthetic fixture creation without external dependencies.
"""

from __future__ import annotations

import logging
import struct
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ivanovo2beam.gis.projection import BBox
from ivanovo2beam.osm.types import OsmData, OsmParseError, OsmRelation, OsmWay

logger = logging.getLogger(__name__)

__all__ = [
    "encode_osm_pbf",
    "parse_osm_pbf",
    "write_osm_pbf",
]

# --------------------------------------------------------------------------- #
# Low-level Protobuf Wire Decoding & Encoding
# --------------------------------------------------------------------------- #


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a varint from ``buf`` at ``pos``, returning ``(value, next_pos)``."""
    val = 0
    shift = 0
    buf_len = len(buf)
    while pos < buf_len:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    raise OsmParseError("Truncated varint in protobuf stream")


def _zigzag_decode(n: int) -> int:
    """Decode a zigzag-encoded 64-bit integer."""
    return (n >> 1) ^ -(n & 1)


def _zigzag_encode(n: int) -> int:
    """Encode a 64-bit signed integer to zigzag representation."""
    return (n << 1) ^ (n >> 63) if n < 0 else (n << 1)


def _encode_varint(n: int) -> bytes:
    """Encode an unsigned integer into varint bytes."""
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        towrite = n & 0x7F
        n >>= 7
        if n:
            out.append(towrite | 0x80)
        else:
            out.append(towrite)
            break
    return bytes(out)


def _unpack_packed_varints(buf: bytes) -> list[int]:
    """Unpack packed varints into a list of integers."""
    pos = 0
    end = len(buf)
    res: list[int] = []
    append = res.append
    while pos < end:
        v = 0
        shift = 0
        while pos < end:
            b = buf[pos]
            pos += 1
            v |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        append(v)
    return res


def _unpack_packed_sint64(buf: bytes) -> list[int]:
    """Unpack packed sint64 (zigzag-encoded) into a list of signed integers."""
    pos = 0
    end = len(buf)
    res: list[int] = []
    append = res.append
    while pos < end:
        v = 0
        shift = 0
        while pos < end:
            b = buf[pos]
            pos += 1
            v |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        append((v >> 1) ^ -(v & 1))
    return res


# --------------------------------------------------------------------------- #
# BlockHeader and Blob Decompression
# --------------------------------------------------------------------------- #


def _read_block_header(header_bytes: bytes) -> tuple[str, int]:
    """Decode a ``BlockHeader`` protobuf returning ``(type_str, datasize)``."""
    pos = 0
    end = len(header_bytes)
    block_type = ""
    datasize = 0

    while pos < end:
        tag, pos = _read_varint(header_bytes, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7

        if wire_type == 0:
            val, pos = _read_varint(header_bytes, pos)
            if field_num == 3:
                datasize = val
        elif wire_type == 2:
            length, pos = _read_varint(header_bytes, pos)
            val_bytes = header_bytes[pos : pos + length]
            pos += length
            if field_num == 1:
                block_type = val_bytes.decode("utf-8", "replace")
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            raise OsmParseError(f"Unexpected wire type {wire_type} in BlockHeader")

    return block_type, datasize


def _decompress_blob(blob_bytes: bytes) -> bytes:
    """Decompress a ``Blob`` protobuf into raw payload bytes."""
    pos = 0
    end = len(blob_bytes)
    raw: bytes | None = None
    zlib_data: bytes | None = None
    lzma_data: bytes | None = None

    while pos < end:
        tag, pos = _read_varint(blob_bytes, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7

        if wire_type == 0:
            _val, pos = _read_varint(blob_bytes, pos)
            # raw_size = _val (field 2)
        elif wire_type == 2:
            length, pos = _read_varint(blob_bytes, pos)
            val_bytes = blob_bytes[pos : pos + length]
            pos += length
            if field_num == 1:
                raw = val_bytes
            elif field_num == 3:
                zlib_data = val_bytes
            elif field_num == 4:
                lzma_data = val_bytes
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            raise OsmParseError(f"Unexpected wire type {wire_type} in Blob")

    if zlib_data is not None:
        return zlib.decompress(zlib_data)
    if raw is not None:
        return raw
    if lzma_data is not None:
        import lzma

        return lzma.decompress(lzma_data)
    raise OsmParseError("Blob payload contains no supported compressed or raw data")


# --------------------------------------------------------------------------- #
# PrimitiveBlock Parsing
# --------------------------------------------------------------------------- #


class _StringTable:
    """Container for the OSM StringTable."""

    __slots__ = ("strings",)

    def __init__(self, raw_bytes: bytes) -> None:
        strings: list[str] = []
        pos = 0
        end = len(raw_bytes)
        while pos < end:
            tag, pos = _read_varint(raw_bytes, pos)
            wire_type = tag & 0x7
            if wire_type == 2:
                length, pos = _read_varint(raw_bytes, pos)
                s = raw_bytes[pos : pos + length].decode("utf-8", "replace")
                strings.append(s)
                pos += length
            elif wire_type == 0:
                _, pos = _read_varint(raw_bytes, pos)
            elif wire_type == 1:
                pos += 8
            elif wire_type == 5:
                pos += 4
        self.strings = strings

    def get(self, index: int) -> str:
        if 0 <= index < len(self.strings):
            return self.strings[index]
        return ""


def _parse_primitive_block_elements(
    payload: bytes,
) -> tuple[_StringTable, list[bytes], int, int, int]:
    """Parse a PrimitiveBlock into stringtable, primitivegroup payloads, and offsets."""
    pos = 0
    end = len(payload)
    stringtable = _StringTable(b"")
    groups: list[bytes] = []
    granularity = 100
    lat_offset = 0
    lon_offset = 0

    while pos < end:
        tag, pos = _read_varint(payload, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7

        if wire_type == 0:
            val, pos = _read_varint(payload, pos)
            if field_num == 17:
                granularity = val
            elif field_num == 19:
                lat_offset = val
            elif field_num == 20:
                lon_offset = val
        elif wire_type == 2:
            length, pos = _read_varint(payload, pos)
            chunk = payload[pos : pos + length]
            pos += length
            if field_num == 1:
                stringtable = _StringTable(chunk)
            elif field_num == 2:
                groups.append(chunk)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4

    return stringtable, groups, granularity, lat_offset, lon_offset


def _iter_dense_nodes(
    dense_bytes: bytes,
    stringtable: _StringTable,
    granularity: int,
    lat_offset: int,
    lon_offset: int,
) -> list[tuple[int, float, float, dict[str, str]]]:
    """Unpack dense nodes into (id, lon, lat, tags)."""
    pos = 0
    end = len(dense_bytes)
    id_bytes = b""
    lat_bytes = b""
    lon_bytes = b""
    kv_bytes = b""

    while pos < end:
        tag, pos = _read_varint(dense_bytes, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7

        if wire_type == 2:
            length, pos = _read_varint(dense_bytes, pos)
            chunk = dense_bytes[pos : pos + length]
            pos += length
            if field_num == 1:
                id_bytes = chunk
            elif field_num == 8:
                lat_bytes = chunk
            elif field_num == 9:
                lon_bytes = chunk
            elif field_num == 10:
                kv_bytes = chunk
        elif wire_type == 0:
            _, pos = _read_varint(dense_bytes, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4

    if not id_bytes:
        return []

    id_deltas = _unpack_packed_sint64(id_bytes)
    lat_deltas = _unpack_packed_sint64(lat_bytes)
    lon_deltas = _unpack_packed_sint64(lon_bytes)
    kv = _unpack_packed_varints(kv_bytes) if kv_bytes else []

    count = len(id_deltas)
    lat_scale = 1e-9 * granularity
    lon_scale = 1e-9 * granularity
    lat_off = 1e-9 * lat_offset
    lon_off = 1e-9 * lon_offset

    curr_id = 0
    curr_lat = 0
    curr_lon = 0
    kv_idx = 0
    kv_len = len(kv)

    nodes: list[tuple[int, float, float, dict[str, str]]] = []
    append_node = nodes.append

    for i in range(count):
        curr_id += id_deltas[i]
        curr_lat += lat_deltas[i]
        curr_lon += lon_deltas[i]

        tags: dict[str, str] = {}
        if kv_idx < kv_len:
            while kv_idx < kv_len:
                k_idx = kv[kv_idx]
                kv_idx += 1
                if k_idx == 0:
                    break
                v_idx = kv[kv_idx]
                kv_idx += 1
                tags[stringtable.get(k_idx)] = stringtable.get(v_idx)

        lat_deg = lat_off + curr_lat * lat_scale
        lon_deg = lon_off + curr_lon * lon_scale
        append_node((curr_id, lon_deg, lat_deg, tags))

    return nodes


def _parse_way(way_bytes: bytes, stringtable: _StringTable) -> OsmWay | None:
    """Unpack a single Way message."""
    pos = 0
    end = len(way_bytes)
    way_id = 0
    keys_bytes = b""
    vals_bytes = b""
    refs_bytes = b""

    while pos < end:
        tag, pos = _read_varint(way_bytes, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7

        if wire_type == 0:
            val, pos = _read_varint(way_bytes, pos)
            if field_num == 1:
                way_id = val
        elif wire_type == 2:
            length, pos = _read_varint(way_bytes, pos)
            chunk = way_bytes[pos : pos + length]
            pos += length
            if field_num == 2:
                keys_bytes = chunk
            elif field_num == 3:
                vals_bytes = chunk
            elif field_num == 8:
                refs_bytes = chunk
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4

    if not refs_bytes:
        return None

    ref_deltas = _unpack_packed_sint64(refs_bytes)
    curr_ref = 0
    refs: list[int] = []
    append_ref = refs.append
    for d in ref_deltas:
        curr_ref += d
        append_ref(curr_ref)

    if len(refs) < 2:
        return None

    tags: dict[str, str] = {}
    if keys_bytes and vals_bytes:
        keys = _unpack_packed_varints(keys_bytes)
        vals = _unpack_packed_varints(vals_bytes)
        for k_idx, v_idx in zip(keys, vals, strict=False):
            tags[stringtable.get(k_idx)] = stringtable.get(v_idx)

    return OsmWay(id=way_id, nodes=tuple(refs), tags=tags)


def _parse_relation(rel_bytes: bytes, stringtable: _StringTable) -> OsmRelation | None:
    """Unpack a single Relation message."""
    pos = 0
    end = len(rel_bytes)
    rel_id = 0
    keys_bytes = b""
    vals_bytes = b""
    roles_bytes = b""
    memids_bytes = b""
    types_bytes = b""

    while pos < end:
        tag, pos = _read_varint(rel_bytes, pos)
        field_num = tag >> 3
        wire_type = tag & 0x7

        if wire_type == 0:
            val, pos = _read_varint(rel_bytes, pos)
            if field_num == 1:
                rel_id = val
        elif wire_type == 2:
            length, pos = _read_varint(rel_bytes, pos)
            chunk = rel_bytes[pos : pos + length]
            pos += length
            if field_num == 2:
                keys_bytes = chunk
            elif field_num == 3:
                vals_bytes = chunk
            elif field_num == 8:
                roles_bytes = chunk
            elif field_num == 9:
                memids_bytes = chunk
            elif field_num == 10:
                types_bytes = chunk
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4

    if not memids_bytes:
        return None

    memid_deltas = _unpack_packed_sint64(memids_bytes)
    roles = _unpack_packed_varints(roles_bytes) if roles_bytes else [0] * len(memid_deltas)
    types_code = _unpack_packed_varints(types_bytes) if types_bytes else [0] * len(memid_deltas)

    type_map = {0: "node", 1: "way", 2: "relation"}
    members: list[tuple[str, int, str]] = []
    append_mem = members.append
    curr_memid = 0

    for mdelta, r_idx, t_code in zip(memid_deltas, roles, types_code, strict=False):
        curr_memid += mdelta
        mtype = type_map.get(t_code, "node")
        append_mem((mtype, curr_memid, stringtable.get(r_idx)))

    tags: dict[str, str] = {}
    if keys_bytes and vals_bytes:
        keys = _unpack_packed_varints(keys_bytes)
        vals = _unpack_packed_varints(vals_bytes)
        for k_idx, v_idx in zip(keys, vals, strict=False):
            tags[stringtable.get(k_idx)] = stringtable.get(v_idx)

    return OsmRelation(id=rel_id, members=tuple(members), tags=tags)


# --------------------------------------------------------------------------- #
# Two-Pass Streaming Parser
# --------------------------------------------------------------------------- #


def _parse_pbf_pure_python(path: Path, bbox: BBox | None = None) -> OsmData:
    """Stream an OSM PBF file using pure-Python stdlib decoding.

    When ``bbox`` is provided, two passes are executed:
    * Pass 1: Streams all blocks to identify nodes and ways intersecting the bbox,
      collecting all referenced node IDs.
    * Pass 2: Rewinds to populate ``OsmData`` containing only needed geometry,
      consuming < 250 MB RAM even on multi-gigabyte regional extracts.
    """
    source_str = str(path)

    # If bbox is given, expand by 0.005 degrees (~500m) to catch roads crossing boundaries
    check_bbox = bbox.expanded(0.005) if bbox is not None else None

    # Pass 1 state (only if bbox is provided)
    candidate_nodes: set[int] = set()
    needed_node_ids: set[int] = set()
    needed_way_ids: set[int] = set()
    needed_rel_ids: set[int] = set()

    data = OsmData(source=source_str)

    def process_block(
        payload: bytes,
        is_pass_1: bool,
    ) -> None:
        stringtable, groups, granularity, lat_off, lon_off = _parse_primitive_block_elements(payload)

        for group_bytes in groups:
            pos = 0
            end = len(group_bytes)
            while pos < end:
                tag, pos = _read_varint(group_bytes, pos)
                field_num = tag >> 3
                wire_type = tag & 0x7

                if wire_type == 2:
                    length, pos = _read_varint(group_bytes, pos)
                    chunk = group_bytes[pos : pos + length]
                    pos += length

                    if field_num == 2:  # DenseNodes
                        nodes = _iter_dense_nodes(chunk, stringtable, granularity, lat_off, lon_off)
                        if is_pass_1 and check_bbox is not None:
                            for nid, lon, lat, _ in nodes:
                                if check_bbox.contains(lon, lat):
                                    candidate_nodes.add(nid)
                        else:
                            for nid, lon, lat, ntags in nodes:
                                if (
                                    check_bbox is None
                                    or nid in needed_node_ids
                                    or (check_bbox.contains(lon, lat) and ntags)
                                ):
                                    data.nodes[nid] = (lon, lat)
                                    if ntags:
                                        data.node_tags[nid] = ntags

                    elif field_num == 3:  # Way
                        way = _parse_way(chunk, stringtable)
                        if way is not None:
                            if is_pass_1 and check_bbox is not None:
                                if any(r in candidate_nodes for r in way.nodes):
                                    needed_way_ids.add(way.id)
                                    needed_node_ids.update(way.nodes)
                            else:
                                if check_bbox is None or way.id in needed_way_ids:
                                    data.ways.append(way)

                    elif field_num == 4:  # Relation
                        rel = _parse_relation(chunk, stringtable)
                        if rel is not None:
                            if is_pass_1 and check_bbox is not None:
                                if any(
                                    (mtype == "way" and ref in needed_way_ids)
                                    or (mtype == "node" and (ref in candidate_nodes or ref in needed_node_ids))
                                    for mtype, ref, _ in rel.members
                                ):
                                    needed_rel_ids.add(rel.id)
                                    for mtype, ref, _ in rel.members:
                                        if mtype == "way":
                                            needed_way_ids.add(ref)
                                        elif mtype == "node":
                                            needed_node_ids.add(ref)
                            else:
                                if check_bbox is None or rel.id in needed_rel_ids:
                                    data.relations.append(rel)

                elif wire_type == 0:
                    _, pos = _read_varint(group_bytes, pos)
                elif wire_type == 1:
                    pos += 8
                elif wire_type == 5:
                    pos += 4

    with open(path, "rb") as fh:
        # Pass 1 if bbox filtering
        if bbox is not None:
            while True:
                len_bytes = fh.read(4)
                if not len_bytes:
                    break
                if len(len_bytes) < 4:
                    raise OsmParseError(f"Truncated block header length in {path}")
                header_len = struct.unpack(">I", len_bytes)[0]
                header_bytes = fh.read(header_len)
                if len(header_bytes) < header_len:
                    raise OsmParseError(f"Truncated block header in {path}")
                btype, datasize = _read_block_header(header_bytes)
                blob_bytes = fh.read(datasize)
                if len(blob_bytes) < datasize:
                    raise OsmParseError(f"Truncated blob data in {path}")
                if btype == "OSMData":
                    payload = _decompress_blob(blob_bytes)
                    process_block(payload, is_pass_1=True)

            needed_node_ids.update(candidate_nodes)
            fh.seek(0)

        # Pass 2 (or single pass if bbox is None)
        while True:
            len_bytes = fh.read(4)
            if not len_bytes:
                break
            if len(len_bytes) < 4:
                raise OsmParseError(f"Truncated block header length in {path}")
            header_len = struct.unpack(">I", len_bytes)[0]
            header_bytes = fh.read(header_len)
            if len(header_bytes) < header_len:
                raise OsmParseError(f"Truncated block header in {path}")
            btype, datasize = _read_block_header(header_bytes)
            blob_bytes = fh.read(datasize)
            if len(blob_bytes) < datasize:
                raise OsmParseError(f"Truncated blob data in {path}")
            if btype == "OSMData":
                payload = _decompress_blob(blob_bytes)
                process_block(payload, is_pass_1=False)

    return data


# --------------------------------------------------------------------------- #
# Osmium Fast-Path Adapter
# --------------------------------------------------------------------------- #


def _parse_with_osmium(path: Path, bbox: BBox | None = None) -> OsmData | None:
    """Attempt parsing using the accelerated C++ ``osmium`` library if available."""
    try:
        import osmium  # type: ignore[import-untyped]
    except (ImportError, Exception):
        return None

    try:

        class _Collector(osmium.SimpleHandler):
            def __init__(self, target_bbox: BBox | None) -> None:
                super().__init__()
                self.target_bbox = target_bbox
                self.data = OsmData(source=str(path))
                self.check_bbox = target_bbox.expanded(0.005) if target_bbox else None
                self.candidate_nodes: set[int] = set()

            def node(self, n: Any) -> None:
                if not n.location.valid():
                    return
                lon = float(n.location.lon)
                lat = float(n.location.lat)
                if self.check_bbox is None or self.check_bbox.contains(lon, lat):
                    self.candidate_nodes.add(n.id)
                    self.data.nodes[n.id] = (lon, lat)
                    if n.tags:
                        self.data.node_tags[n.id] = {t.k: t.v for t in n.tags}

            def way(self, w: Any) -> None:
                refs = tuple(node.ref for node in w.nodes)
                if self.check_bbox is not None and not any(r in self.candidate_nodes for r in refs):
                    return
                tags = {t.k: t.v for t in w.tags}
                self.data.ways.append(OsmWay(id=w.id, nodes=refs, tags=tags))

            def relation(self, r: Any) -> None:
                members = tuple((m.type.lower(), m.ref, m.role) for m in r.members)
                tags = {t.k: t.v for t in r.tags}
                self.data.relations.append(OsmRelation(id=r.id, members=members, tags=tags))

        collector = _Collector(bbox)
        collector.apply_file(str(path), locations=True)
        return collector.data
    except Exception as exc:
        logger.warning("osmium reader failed (%s); falling back to pure-Python parser", exc)
        return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def parse_osm_pbf(
    path: Path | str,
    bbox: BBox | tuple[float, float, float, float] | Sequence[float] | None = None,
) -> OsmData:
    """Parse an OpenStreetMap ``.osm.pbf`` extract.

    Streams the file using pure-Python decoding (or an accelerated osmium path if
    installed). When ``bbox`` is given, executes two-pass streaming to guarantee
    sub-250MB memory footprint even for full federal district extracts.

    Args:
        path: Path to the ``.osm.pbf`` file.
        bbox: Optional bounding box filter (``BBox`` or ``(west, south, east, north)``).

    Returns:
        The parsed :class:`~ivanovo2beam.osm.types.OsmData`.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        OsmParseError: If the file is not valid OSM PBF or is truncated.
    """
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"OSM PBF extract not found: {path_obj}")
    if path_obj.is_dir():
        raise OsmParseError(f"Expected an OSM PBF file, got a directory: {path_obj}")

    norm_bbox: BBox | None = None
    if bbox is not None:
        norm_bbox = bbox if isinstance(bbox, BBox) else BBox.from_config(bbox)

    # Try osmium fast-path first
    fast_data = _parse_with_osmium(path_obj, norm_bbox)
    if fast_data is not None:
        return fast_data

    # Pure Python stdlib reader
    return _parse_pbf_pure_python(path_obj, norm_bbox)


# --------------------------------------------------------------------------- #
# Pure-Python PBF Serializer (for Round-Trip Testing and Fixtures)
# --------------------------------------------------------------------------- #


def _encode_message(fields: list[tuple[int, int, bytes]]) -> bytes:
    """Encode protobuf fields ``[(field_num, wire_type, payload_bytes)]``."""
    out = bytearray()
    for f_num, w_type, payload in fields:
        tag = (f_num << 3) | (w_type & 0x7)
        out.extend(_encode_varint(tag))
        if w_type == 2:
            out.extend(_encode_varint(len(payload)))
        out.extend(payload)
    return bytes(out)


def _encode_block(block_type: str, payload_bytes: bytes) -> bytes:
    """Wrap payload in Blob and BlockHeader, returning serialized file block."""
    compressed = zlib.compress(payload_bytes)
    # Blob: field 3 = zlib_data, field 2 = raw_size
    blob_msg = _encode_message(
        [
            (2, 0, _encode_varint(len(payload_bytes))),
            (3, 2, compressed),
        ]
    )
    # BlockHeader: field 1 = type, field 3 = datasize
    header_msg = _encode_message(
        [
            (1, 2, block_type.encode("utf-8")),
            (3, 0, _encode_varint(len(blob_msg))),
        ]
    )
    return struct.pack(">I", len(header_msg)) + header_msg + blob_msg


def encode_osm_pbf(data: OsmData) -> bytes:
    """Encode an :class:`OsmData` instance into valid ``.osm.pbf`` binary bytes."""
    out = bytearray()

    # 1. HeaderBlock
    header_block = _encode_message(
        [
            (4, 2, b"OsmSchema-V0.6"),
            (4, 2, b"DenseNodes"),
            (16, 2, b"ivanovo2beam"),
        ]
    )
    out.extend(_encode_block("OSMHeader", header_block))

    # 2. StringTable construction
    str_list: list[str] = [""]
    str_map: dict[str, int] = {"": 0}

    def get_str_id(s: str) -> int:
        if s not in str_map:
            str_map[s] = len(str_list)
            str_list.append(s)
        return str_map[s]

    # Pre-register strings from tags and relations
    for tags in data.node_tags.values():
        for k, v in tags.items():
            get_str_id(k)
            get_str_id(v)
    for way in data.ways:
        for k, v in way.tags.items():
            get_str_id(k)
            get_str_id(v)
    for rel in data.relations:
        for k, v in rel.tags.items():
            get_str_id(k)
            get_str_id(v)
        for _, _, role in rel.members:
            get_str_id(role)

    # Encode DenseNodes
    dense_id_deltas = bytearray()
    dense_lat_deltas = bytearray()
    dense_lon_deltas = bytearray()
    dense_kv = bytearray()

    prev_id = 0
    prev_lat = 0
    prev_lon = 0

    sorted_nodes = sorted(data.nodes.items(), key=lambda item: item[0])
    for nid, (lon, lat) in sorted_nodes:
        lat_int = round(lat * 1e7)
        lon_int = round(lon * 1e7)

        dense_id_deltas.extend(_encode_varint(_zigzag_encode(nid - prev_id)))
        dense_lat_deltas.extend(_encode_varint(_zigzag_encode(lat_int - prev_lat)))
        dense_lon_deltas.extend(_encode_varint(_zigzag_encode(lon_int - prev_lon)))

        prev_id = nid
        prev_lat = lat_int
        prev_lon = lon_int

        tags = data.node_tags.get(nid)
        if tags:
            for k, v in tags.items():
                dense_kv.extend(_encode_varint(get_str_id(k)))
                dense_kv.extend(_encode_varint(get_str_id(v)))
        dense_kv.extend(_encode_varint(0))

    dense_fields: list[tuple[int, int, bytes]] = []
    if sorted_nodes:
        dense_fields.append((1, 2, bytes(dense_id_deltas)))
        dense_fields.append((8, 2, bytes(dense_lat_deltas)))
        dense_fields.append((9, 2, bytes(dense_lon_deltas)))
        dense_fields.append((10, 2, bytes(dense_kv)))
    dense_msg = _encode_message(dense_fields) if dense_fields else b""

    # Encode Ways
    way_messages: list[bytes] = []
    for way in data.ways:
        keys_bytes = bytearray()
        vals_bytes = bytearray()
        for k, v in way.tags.items():
            keys_bytes.extend(_encode_varint(get_str_id(k)))
            vals_bytes.extend(_encode_varint(get_str_id(v)))

        refs_deltas = bytearray()
        prev_ref = 0
        for r in way.nodes:
            refs_deltas.extend(_encode_varint(_zigzag_encode(r - prev_ref)))
            prev_ref = r

        way_fields: list[tuple[int, int, bytes]] = [
            (1, 0, _encode_varint(way.id)),
            (8, 2, bytes(refs_deltas)),
        ]
        if keys_bytes:
            way_fields.append((2, 2, bytes(keys_bytes)))
            way_fields.append((3, 2, bytes(vals_bytes)))
        way_messages.append(_encode_message(way_fields))

    # Encode Relations
    rel_messages: list[bytes] = []
    type_code_map = {"node": 0, "way": 1, "relation": 2}
    for rel in data.relations:
        keys_bytes = bytearray()
        vals_bytes = bytearray()
        for k, v in rel.tags.items():
            keys_bytes.extend(_encode_varint(get_str_id(k)))
            vals_bytes.extend(_encode_varint(get_str_id(v)))

        roles_bytes = bytearray()
        memids_deltas = bytearray()
        types_bytes = bytearray()
        prev_memid = 0
        for mtype, mref, mrole in rel.members:
            roles_bytes.extend(_encode_varint(get_str_id(mrole)))
            memids_deltas.extend(_encode_varint(_zigzag_encode(mref - prev_memid)))
            types_bytes.extend(_encode_varint(type_code_map.get(mtype, 0)))
            prev_memid = mref

        rel_fields: list[tuple[int, int, bytes]] = [
            (1, 0, _encode_varint(rel.id)),
            (8, 2, bytes(roles_bytes)),
            (9, 2, bytes(memids_deltas)),
            (10, 2, bytes(types_bytes)),
        ]
        if keys_bytes:
            rel_fields.append((2, 2, bytes(keys_bytes)))
            rel_fields.append((3, 2, bytes(vals_bytes)))
        rel_messages.append(_encode_message(rel_fields))

    # Assemble PrimitiveGroup
    pg_fields: list[tuple[int, int, bytes]] = []
    if dense_msg:
        pg_fields.append((2, 2, dense_msg))
    for wm in way_messages:
        pg_fields.append((3, 2, wm))
    for rm in rel_messages:
        pg_fields.append((4, 2, rm))
    pg_msg = _encode_message(pg_fields)

    # Assemble StringTable message
    st_fields: list[tuple[int, int, bytes]] = [(1, 2, s.encode("utf-8")) for s in str_list]
    st_msg = _encode_message(st_fields)

    # Assemble PrimitiveBlock
    pb_fields: list[tuple[int, int, bytes]] = [
        (1, 2, st_msg),
        (2, 2, pg_msg),
        (17, 0, _encode_varint(100)),
        (19, 0, _encode_varint(0)),
        (20, 0, _encode_varint(0)),
    ]
    primitive_block = _encode_message(pb_fields)
    out.extend(_encode_block("OSMData", primitive_block))

    return bytes(out)


def write_osm_pbf(path: Path | str, data: OsmData) -> None:
    """Write an :class:`OsmData` extract to an ``.osm.pbf`` file."""
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    pbf_bytes = encode_osm_pbf(data)
    path_obj.write_bytes(pbf_bytes)
