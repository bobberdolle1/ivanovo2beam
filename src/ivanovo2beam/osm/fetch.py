"""Overpass API acquisition for OpenStreetMap extracts.

What this module does
---------------------
:func:`fetch_overpass` turns a :class:`~ivanovo2beam.gis.projection.BBox` into a
locally cached ``.osm`` XML file, and :func:`default_query` builds the Overpass
QL that asks for exactly the feature classes the map generator consumes.

The pipeline is cacheable and reproducible by design: downloads are written
under a content-addressed name derived from the *request*, so re-running a build
never re-downloads, and two builds of the same region provably used the same
input bytes (the sidecar records the payload's SHA-256). This is what makes
"pin an extract, build offline" possible -- see ``docs/research/prior_art.md``,
which lists the same multi-endpoint fallback strategy as the pattern worth
copying from the reference implementation.

Politeness and resilience
-------------------------
The public Overpass instances are donated infrastructure and are frequently
loaded. This module therefore:

* identifies itself with a descriptive ``User-Agent`` (Overpass blocks generic
  library user agents);
* never issues two requests less than :data:`MIN_REQUEST_INTERVAL_S` apart;
* honours ``Retry-After`` on HTTP 429/503 instead of hammering;
* falls back through several mirrors with exponential backoff, and validates
  what came back.

That last point is not paranoia. Two failure modes seen in practice and
handled here:

* a mirror answering **HTTP 200 with an HTML error page** (for example
  ``overpass.osm.ch`` replying ``Dispatcher_Client::request_read_and_idx::
  duplicate_query`` with status 200);
* a mirror answering **HTTP 200 with an ``<osm>`` document containing a
  ``<remark>``** element, which is how Overpass reports a query timeout.

Both are detected before anything is written to the cache, so a poisoned
response can never look like a successful build input.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ivanovo2beam.gis.projection import BBox
from ivanovo2beam.osm.types import OsmFetchError, OverpassError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

__all__ = [
    "DEFAULT_ENDPOINTS",
    "DEFAULT_FEATURE_ORDER",
    "FEATURE_CLASSES",
    "MIN_REQUEST_INTERVAL_S",
    "USER_AGENT",
    "OsmFetchError",
    "cache_key",
    "cache_paths",
    "default_query",
    "fetch_overpass",
    "parse_retry_after",
    "read_cache_meta",
]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

#: Overpass mirrors, tried in this order. Verified against the live API on
#: 2026-10-07 with a small Ivanovo bounding box.
#:
#: * ``overpass-api.de`` -- the reference instance and the only one that is not
#:   somebody's spare capacity, so it goes first despite being the slowest under
#:   load (it intermittently answers 504 while a query waits in its queue).
#: * ``overpass.kumi.systems`` -- full-planet mirror; fast when it is up, but it
#:   intermittently answers 500, especially for an immediately repeated query.
#: * ``overpass.private.coffee`` -- full-planet mirror, same profile: correct
#:   data, occasional instant 500 on a repeated query.
#:
#: Data freshness differs between the mirrors, which is why the order above
#: matters and why the sidecar records ``osm_base``: on 2026-10-07 an Ivanovo
#: extract from ``overpass-api.de`` reported ``osm_base=2026-10-07T18:07:53Z``
#: while the same query against ``overpass.private.coffee`` reported
#: ``osm_base=2026-05-31T22:37:44Z``, i.e. four months behind. A fallback mirror
#: is still better than no extract, but a build must be able to see which one it
#: got -- :func:`~ivanovo2beam.osm.fetch.read_cache_meta` exposes exactly that.
#:
#: Three mirrors that were checked and rejected:
#:
#: * ``https://overpass.osm.ch`` -- answers 200, but it serves a Switzerland-only
#:   extract (an Ivanovo bounding box returns an empty ``<osm>`` document while a
#:   Bern bounding box returns 64 kB of data), and it answers a repeated or
#:   unknown query with an **HTML error page carrying HTTP 200**
#:   (``Dispatcher_Client::request_read_and_idx::duplicate_query``). Using it as
#:   a fallback would silently produce empty extracts.
#: * ``https://overpass.osm.jp`` -- TLS certificate does not match the hostname.
#: * ``https://maps.mail.ru/osm/tools/overpass`` -- 504 on every attempt.
DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

#: Descriptive User-Agent. Overpass mirrors reject or throttle anonymous
#: library user agents, and the project URL is the accepted way to identify a
#: non-interactive client.
USER_AGENT = (
    "ivanovo2beam/0.1 (open-source BeamNG.drive map generator; "
    "OpenStreetMap extract fetch; +https://github.com/bobberdolle1/ivanovo2beam)"
)

#: Minimum wall-clock interval between two Overpass requests, in seconds.
MIN_REQUEST_INTERVAL_S = 2.0

#: Decimal places kept when a bounding box is written into a query and hashed
#: into a cache key. 1e-6 degrees is ~11 cm of latitude, far below OSM's own
#: precision, and it makes cache keys stable against float noise in configs.
_COORD_DECIMALS = 6

#: Bumped when the on-disk cache layout or the meaning of a cache key changes.
_CACHE_KEY_VERSION = 1
_CACHE_META_VERSION = 1

#: Attempts per endpoint before moving on to the next mirror.
_MAX_ATTEMPTS_PER_ENDPOINT = 2
_BACKOFF_BASE_S = 5.0
_MAX_BACKOFF_S = 60.0
_MAX_RETRY_AFTER_S = 90.0

#: Refuse to buffer an absurd response. 8 GiB is far above a regional extract
#: (Ivanovo Oblast with all buildings is a few hundred MB) and exists only so a
#: runaway query cannot fill the disk.
_MAX_RESPONSE_BYTES = 8 << 30

_CHUNK = 1 << 16
_REMARK_NEEDLE = b"<remark"

#: Feature classes the default query asks for, keyed by the name accepted in
#: ``include=``. Each value is an Overpass QL statement without its bounding
#: box, so ``bbox`` can be appended in one place.
FEATURE_CLASSES: dict[str, str] = {
    # --- fetched by default: everything the map generator needs ------------
    "highway": 'way["highway"]',
    "railway": 'way["railway"]',
    "waterway": 'way["waterway"]',
    "natural": 'nwr["natural"~"^(water|wood)$"]',
    "landuse": 'nwr["landuse"]',
    "building": 'nwr["building"]',
    "boundary": 'nwr["boundary"]',
    "place": 'nwr["place"]',
    "power": 'way["power"="line"]',
    # --- opt-in extras, not part of the default set ------------------------
    "leisure": 'nwr["leisure"]',
    "amenity": 'nwr["amenity"]',
    "man_made": 'nwr["man_made"]',
    "barrier": 'way["barrier"]',
    "aeroway": 'nwr["aeroway"]',
}

#: Feature classes the default query asks for, in query order.
DEFAULT_FEATURE_ORDER: tuple[str, ...] = (
    "highway",
    "railway",
    "waterway",
    "natural",
    "landuse",
    "building",
    "boundary",
    "place",
    "power",
)

#: Default Overpass QL timeout, in seconds, for :func:`default_query`. Kept
#: below the default HTTP timeout so a server-side timeout surfaces as a
#: ``<remark>`` rather than as a dead socket.
DEFAULT_QL_TIMEOUT = 180


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #


def _fmt_coord(value: float) -> str:
    """Format one degree value for a query, rounded consistently.

    Rounding here (rather than in the cache key only) means two callers whose
    bounding boxes differ by less than 1e-6 degrees issue a byte-identical
    query, so they share one cache entry.
    """
    return f"{round(float(value) + 0.0, _COORD_DECIMALS):.{_COORD_DECIMALS}f}"


def _fmt_bbox(bbox: BBox) -> tuple[str, str, str, str]:
    """Return the bbox as ``(west, south, east, north)`` formatted strings."""
    west, south, east, north = (_fmt_coord(v) for v in bbox.as_tuple())
    return west, south, east, north


def _overpass_bbox(bbox: BBox) -> str:
    """Return the ``south,west,north,east`` literal used by Overpass QL."""
    west, south, east, north = _fmt_bbox(bbox)
    return f"{south},{west},{north},{east}"


def default_query(
    bbox: BBox,
    *,
    include: Sequence[str] | None = None,
    timeout: int = DEFAULT_QL_TIMEOUT,
) -> str:
    """Build the Overpass QL query for ``bbox``.

    The default asks for every feature class the map generator renders: roads,
    railways, waterways, water and woodland, land use, buildings, boundaries,
    place labels and power lines. All of them are returned as one union in a
    single request, which is both faster and kinder to the Overpass servers
    than nine separate queries, and it guarantees the extract is internally
    consistent (the same ``osm_base`` timestamp for everything).

    Args:
        bbox: Region to fetch.
        include: Restrict the fetch to these feature classes, for example
            ``["highway", "building"]``. Names are keys of
            :data:`FEATURE_CLASSES`; unknown names raise ``ValueError``.
            Defaults to the full set: highway, railway, waterway, natural
            (water/wood), landuse, building, boundary, place, power=line.
        timeout: Overpass-side query timeout in seconds, written into the
            ``[timeout:...]`` setting.

    Returns:
        A single Overpass QL query string ending in ``out body;>;out skel qt;``
        so that ways and relations arrive with their tags and every referenced
        node arrives with its geometry.

    Raises:
        ValueError: If ``include`` is empty or names an unknown class.
    """
    names = list(DEFAULT_FEATURE_ORDER) if include is None else [str(n) for n in include]
    if not names:
        raise ValueError("include must name at least one feature class")
    unknown = [name for name in names if name not in FEATURE_CLASSES]
    if unknown:
        raise ValueError(f"unknown feature class(es) {unknown}; known classes: {sorted(FEATURE_CLASSES)}")

    box = _overpass_bbox(bbox)
    lines = [f"[out:xml][timeout:{int(timeout)}];", "("]
    lines.extend(f"  {FEATURE_CLASSES[name]}({box});" for name in names)
    lines.append(");")
    lines.append("out body;>;out skel qt;")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Cache identity
# --------------------------------------------------------------------------- #


def _resolve_query(bbox: BBox, query: str | None, include: Sequence[str] | None) -> str:
    """Return the effective query, validating an explicit one."""
    if query is None:
        return default_query(bbox, include=include)
    if not query.strip():
        raise ValueError("query must not be empty; pass None to use the default query")
    return query


def cache_key(bbox: BBox, query: str | None = None, *, include: Sequence[str] | None = None) -> str:
    """Return the cache key for a request.

    The key is ``sha256`` over a canonical description of the request: a
    version tag, the bounding box rounded to :data:`_COORD_DECIMALS` decimals,
    and the SHA-256 of the Overpass QL. Hashing the *query text* rather than the
    Python arguments means any change that would produce a different request --
    a different feature set, a different timeout, an explicit query -- produces
    a different key, while float noise in a bounding box does not.

    Args:
        bbox: Region requested.
        query: Explicit Overpass QL, or ``None`` for :func:`default_query`.
        include: Feature classes, used only when ``query`` is ``None``.

    Returns:
        A 64-character hexadecimal digest.
    """
    effective = _resolve_query(bbox, query, include)
    west, south, east, north = _fmt_bbox(bbox)
    canonical = "\n".join(
        (
            f"ivanovo2beam-osm-cache-v{_CACHE_KEY_VERSION}",
            f"bbox={west},{south},{east},{north}",
            f"query-sha256={hashlib.sha256(effective.encode('utf-8')).hexdigest()}",
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_paths(
    cache_dir: Path,
    bbox: BBox,
    query: str | None = None,
    *,
    include: Sequence[str] | None = None,
) -> tuple[Path, Path]:
    """Return ``(osm_path, meta_path)`` for a request without touching the disk.

    Args:
        cache_dir: Directory holding cached extracts.
        bbox: Region requested.
        query: Explicit Overpass QL, or ``None`` for :func:`default_query`.
        include: Feature classes, used only when ``query`` is ``None``.

    Returns:
        The ``.osm`` payload path and its ``.json`` sidecar path.
    """
    key = cache_key(bbox, query, include=include)
    directory = Path(cache_dir)
    return (directory / f"{key}.osm", directory / f"{key}.json")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def parse_retry_after(value: str | None, *, default: float = 30.0, now: float | None = None) -> float:
    """Parse an HTTP ``Retry-After`` header into seconds to wait.

    Both forms from :rfc:`9110` are accepted: a delay in seconds and an
    HTTP-date. Anything unparseable falls back to ``default``.

    Args:
        value: Raw header value, or ``None`` when the header is absent.
        default: Value returned when the header is missing or malformed.
        now: Current POSIX timestamp, injectable for tests.

    Returns:
        A non-negative number of seconds, never above :data:`_MAX_RETRY_AFTER_S`.
    """
    if not value:
        return min(default, _MAX_RETRY_AFTER_S)
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return min(max(seconds, 0.0), _MAX_RETRY_AFTER_S)

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return min(default, _MAX_RETRY_AFTER_S)
    if when is None:
        return min(default, _MAX_RETRY_AFTER_S)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = time.time() if now is None else now
    return min(max(when.timestamp() - reference, 0.0), _MAX_RETRY_AFTER_S)


def _sleep(seconds: float) -> None:
    """Sleep, isolated so tests can replace it."""
    if seconds > 0:
        time.sleep(seconds)


def _open(request: urllib.request.Request, timeout: float):
    """Open a URL request, isolated so tests can replace it."""
    return urllib.request.urlopen(request, timeout=timeout)


_last_request_at = 0.0


def _polite_pause() -> None:
    """Wait so that two consecutive requests are never closer than the minimum."""
    global _last_request_at
    now = time.monotonic()
    remaining = MIN_REQUEST_INTERVAL_S - (now - _last_request_at)
    if remaining > 0:
        _sleep(remaining)
    _last_request_at = time.monotonic()


class _RateLimited(Exception):
    """The endpoint asked us to come back later (HTTP 429/503 + Retry-After)."""

    __slots__ = ("retry_after", "status")

    def __init__(self, status: int, retry_after: float) -> None:
        super().__init__(f"HTTP {status}, retry after {retry_after:.1f}s")
        self.status = status
        self.retry_after = retry_after


class _Transient(Exception):
    """A failure that is worth retrying, on this endpoint or the next one."""


@dataclass(slots=True)
class _Download:
    """Outcome of one successful HTTP exchange."""

    endpoint: str
    sha256: str
    size: int
    remark: str | None = None
    provenance: dict[str, str] = field(default_factory=dict)


def _headers() -> dict[str, str]:
    """Return the request headers sent to Overpass."""
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml, text/xml, */*",
        # urllib does not transparently gunzip, so ask for the identity coding.
        "Accept-Encoding": "identity",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    }


def _download(endpoint: str, query: str, timeout: float, dest: Path, max_bytes: int) -> _Download:
    """POST ``query`` to ``endpoint`` and stream the response into ``dest``.

    The body is streamed in chunks so that a multi-hundred-megabyte extract is
    never held in memory, and the SHA-256 is computed on the fly.

    Args:
        endpoint: Interpreter URL.
        query: Overpass QL.
        timeout: Socket timeout in seconds.
        dest: File to write the raw response to.
        max_bytes: Abort above this many bytes.

    Returns:
        A :class:`_Download` describing what arrived. The caller decides
        whether it is usable; this function only fails for transport errors.

    Raises:
        _RateLimited: The endpoint returned 429/503 and asked us to wait.
        _Transient: Any other transport or protocol failure.
    """
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, headers=_headers(), method="POST")

    try:
        response = _open(request, timeout)
    except urllib.error.HTTPError as exc:
        detail = _peek_error(exc)
        if exc.code in {429, 503}:
            retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            raise _RateLimited(exc.code, retry_after) from exc
        raise _Transient(f"HTTP {exc.code} {exc.reason} {detail}".strip()) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Transient(f"{type(exc).__name__}: {exc}") from exc

    digest = hashlib.sha256()
    size = 0
    remark_offset: int | None = None
    tail = b""
    try:
        with response:
            status = getattr(response, "status", None) or getattr(response, "code", 200)
            if status != 200:
                raise _Transient(f"HTTP {status}")
            with open(dest, "wb") as out:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    if isinstance(chunk, str):  # defensive: text-mode response object
                        chunk = chunk.encode("utf-8", "replace")
                    out.write(chunk)
                    digest.update(chunk)
                    if remark_offset is None:
                        window = tail + chunk
                        index = window.find(_REMARK_NEEDLE)
                        if index >= 0:
                            remark_offset = size - len(tail) + index
                    tail = chunk[-len(_REMARK_NEEDLE) :]
                    size += len(chunk)
                    if size > max_bytes:
                        raise _Transient(
                            f"response exceeded {max_bytes} bytes; is the bounding box too large?"
                        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Transient(f"{type(exc).__name__} while reading: {exc}") from exc

    if size == 0:
        raise _Transient("empty response body")

    result = _Download(endpoint=endpoint, sha256=digest.hexdigest(), size=size)
    if remark_offset is not None:
        result.remark = _read_remark(dest, remark_offset) or "unknown Overpass error"
    result.provenance = _read_provenance(dest)
    return result


def _peek_error(exc: urllib.error.HTTPError, limit: int = 400) -> str:
    """Return a short, printable excerpt of an error response body."""
    try:
        raw = exc.read(limit) or b""
    except Exception:  # pragma: no cover - the body may already be consumed
        return ""
    text = raw.decode("utf-8", "replace").strip()
    text = re.sub(r"\s+", " ", text)
    return f"- {text[:limit]}" if text else ""


def _read_remark(path: Path, offset: int, window: int = 4096) -> str | None:
    """Read the text of the ``<remark>`` element near ``offset``."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(offset, 0))
            raw = fh.read(window)
    except OSError:  # pragma: no cover - the file was just written
        return None
    text = raw.decode("utf-8", "replace")
    start = text.find("<remark")
    if start < 0:
        return None
    start = text.find(">", start)
    if start < 0:
        return None
    end = text.find("</remark>", start)
    body = text[start + 1 : end if end >= 0 else None]
    body = re.sub(r"\s+", " ", body).strip()
    return body or None


_OSM_ROOT_RE = re.compile(r"<osm\b[^>]*>")
_ATTR_RE = re.compile(r'([A-Za-z_:][-\w:.]*)\s*=\s*"([^"]*)"')
_META_RE = re.compile(r"<meta\b[^>]*>")
_BOUNDS_RE = re.compile(r"<bounds\b[^>]*>")


def _looks_like_osm(path: Path, head_bytes: int = 8192) -> bool:
    """Return whether the payload starts with an OSM XML ``<osm>`` element."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(head_bytes)
    except OSError:  # pragma: no cover - the file was just written
        return False
    return _OSM_ROOT_RE.search(head.decode("utf-8", "replace")) is not None


def _read_provenance(path: Path, head_bytes: int = 8192) -> dict[str, str]:
    """Extract generator/osm_base/bounds metadata from the document head.

    These few fields are what makes an extract reproducible: ``osm_base`` is
    the database timestamp the answer was rendered from, so two builds that
    recorded the same value are known to have used the same source data even
    after the cache is gone.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(head_bytes).decode("utf-8", "replace")
    except OSError:  # pragma: no cover - the file was just written
        return {}

    out: dict[str, str] = {}
    root = _OSM_ROOT_RE.search(head)
    if root:
        attrs = dict(_ATTR_RE.findall(root.group(0)))
        for key in ("version", "generator"):
            if key in attrs:
                out[key] = attrs[key]
    meta = _META_RE.search(head)
    if meta:
        attrs = dict(_ATTR_RE.findall(meta.group(0)))
        if "osm_base" in attrs:
            out["osm_base"] = attrs["osm_base"]
    bounds = _BOUNDS_RE.search(head)
    if bounds:
        attrs = dict(_ATTR_RE.findall(bounds.group(0)))
        if attrs:
            out["bounds"] = ",".join(f"{k}={attrs[k]}" for k in sorted(attrs))
    return out


# --------------------------------------------------------------------------- #
# Cache maintenance
# --------------------------------------------------------------------------- #


def _read_meta(meta_path: Path) -> dict | None:
    """Return the parsed sidecar, or ``None`` when missing or unreadable."""
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ignoring unreadable OSM cache sidecar %s", meta_path)
        return None
    return meta if isinstance(meta, dict) else None


def _cache_hit(osm_path: Path, meta_path: Path) -> bool:
    """Return whether a cached extract can be reused without touching the network.

    The check is deliberately cheap: a non-empty file is enough, plus a size
    comparison against the sidecar when one is present. Re-hashing a
    multi-hundred-megabyte extract on every build would cost more than it is
    worth; ``force=True`` re-downloads when in doubt.
    """
    try:
        stat = osm_path.stat()
    except OSError:
        return False
    if stat.st_size == 0:
        logger.warning("cached OSM extract %s is empty; refetching", osm_path)
        return False

    meta = _read_meta(meta_path)
    if meta is not None:
        recorded = meta.get("bytes")
        if isinstance(recorded, int) and recorded != stat.st_size:
            logger.warning(
                "cached OSM extract %s is %d bytes but the sidecar records %d; refetching",
                osm_path,
                stat.st_size,
                recorded,
            )
            return False
    return True


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write ``payload`` as JSON through a temporary file and an atomic rename."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _discard(path: Path) -> None:
    """Delete a temporary file, ignoring races and permission noise."""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best effort cleanup
        logger.debug("could not remove temporary file %s", path)


def _utc_now() -> str:
    """Return the current UTC time as an ISO-8601 ``Z`` string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_cache_meta(osm_path: Path) -> dict | None:
    """Return the sidecar metadata for a cached extract, or ``None``.

    Args:
        osm_path: Path returned by :func:`fetch_overpass`.

    Returns:
        The parsed sidecar JSON, or ``None`` when it is missing or corrupt.
    """
    return _read_meta(Path(osm_path).with_suffix(".json"))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def fetch_overpass(
    bbox: BBox,
    cache_dir: Path,
    *,
    query: str | None = None,
    timeout: float = 300.0,
    force: bool = False,
    endpoints: Sequence[str] | None = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> Path:
    """Download OSM data for ``bbox`` (or run an explicit query) and cache it.

    On a cache hit nothing is sent over the network and the cached path is
    returned immediately, which is what makes repeated builds offline-capable.

    On a miss the query is POSTed to each endpoint in turn, with one retry per
    endpoint, until one returns a payload that validates as OSM XML. Failures
    fall through to the next mirror; the raw response is written to the cache
    and a JSON sidecar records the request, the endpoints tried, the fetch
    time, the byte size and the payload's SHA-256.

    Args:
        bbox: Region to fetch. Also part of the cache key.
        cache_dir: Directory for cached extracts; created if missing.
        query: Explicit Overpass QL. When ``None`` :func:`default_query` is
            used. A custom query still hashes ``bbox`` into the cache key, so
            two different custom queries for one region cannot collide.
        timeout: Socket timeout per attempt, in seconds.
        force: Re-download even when a cached copy exists.
        endpoints: Override the mirror list (mainly for tests). Defaults to
            :data:`DEFAULT_ENDPOINTS`.
        max_bytes: Refuse responses larger than this.

    Returns:
        Path to the cached ``.osm`` file. It is complete and validated: partial
        or error payloads are never left behind.

    Raises:
        OverpassError: Every endpoint failed, or a mirror returned an Overpass
            ``<remark>`` (query timeout / syntax error) instead of data.
        ValueError: ``query`` is blank or ``endpoints`` is empty.
    """
    cache_dir = Path(cache_dir)
    effective_query = _resolve_query(bbox, query, None)
    osm_path, meta_path = cache_paths(cache_dir, bbox, query)

    if not force and _cache_hit(osm_path, meta_path):
        meta = _read_meta(meta_path)
        if meta is not None:
            logger.info(
                "using cached OSM extract %s (%s bytes, fetched %s)",
                osm_path,
                meta.get("bytes", "?"),
                meta.get("fetched_utc", "?"),
            )
        return osm_path

    endpoint_list = [str(e) for e in (DEFAULT_ENDPOINTS if endpoints is None else endpoints)]
    if not endpoint_list:
        raise ValueError("endpoints must contain at least one Overpass interpreter URL")

    cache_dir.mkdir(parents=True, exist_ok=True)
    part_path = osm_path.with_name(osm_path.name + ".part")
    tried: list[str] = []
    failures: list[str] = []

    for endpoint in endpoint_list:
        for attempt in range(1, _MAX_ATTEMPTS_PER_ENDPOINT + 1):
            tried.append(endpoint)
            _polite_pause()
            try:
                result = _download(endpoint, effective_query, timeout, part_path, max_bytes)
            except _RateLimited as exc:
                failures.append(f"{endpoint}: {exc}")
                _discard(part_path)
                if attempt < _MAX_ATTEMPTS_PER_ENDPOINT:
                    logger.info(
                        "endpoint %s asked us to wait %.0fs (HTTP %d)",
                        endpoint,
                        exc.retry_after,
                        exc.status,
                    )
                    _sleep(exc.retry_after)
                    continue
                logger.warning("endpoint %s is still rate limiting us; trying the next mirror", endpoint)
                break
            except _Transient as exc:
                failures.append(f"{endpoint}: {exc}")
                _discard(part_path)
                if attempt < _MAX_ATTEMPTS_PER_ENDPOINT:
                    backoff = min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
                    logger.info("endpoint %s failed (%s); retrying in %.0fs", endpoint, exc, backoff)
                    _sleep(backoff)
                    continue
                logger.warning("endpoint %s failed twice (%s); trying the next mirror", endpoint, exc)
                break

            if result.remark is not None:
                # A <remark> means the query itself failed (usually a timeout).
                # Repeating it on the same mirror is pointless; try another.
                failures.append(f"{endpoint}: Overpass error: {result.remark}")
                _discard(part_path)
                logger.warning("endpoint %s returned an Overpass error: %s", endpoint, result.remark)
                break

            if not _looks_like_osm(part_path):
                head = _peek_head(part_path)
                failures.append(f"{endpoint}: response is not OSM XML ({head})")
                _discard(part_path)
                if attempt < _MAX_ATTEMPTS_PER_ENDPOINT:
                    backoff = min(_BACKOFF_BASE_S * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
                    logger.info("endpoint %s returned a non-OSM body; retrying in %.0fs", endpoint, backoff)
                    _sleep(backoff)
                    continue
                logger.warning("endpoint %s returned a non-OSM body twice; trying the next mirror", endpoint)
                break

            os.replace(part_path, osm_path)
            _atomic_write_json(
                meta_path,
                {
                    "version": _CACHE_META_VERSION,
                    "cache_key": cache_key(bbox, query),
                    "request": {
                        "bbox": {
                            "west": bbox.west,
                            "south": bbox.south,
                            "east": bbox.east,
                            "north": bbox.north,
                        },
                        "overpass_bbox": _overpass_bbox(bbox),
                        "query": effective_query,
                        "query_sha256": hashlib.sha256(effective_query.encode("utf-8")).hexdigest(),
                    },
                    "endpoint": endpoint,
                    "endpoints_tried": tried,
                    "fetched_utc": _utc_now(),
                    "file": osm_path.name,
                    "bytes": result.size,
                    "sha256": result.sha256,
                    "provenance": result.provenance,
                },
            )
            logger.info(
                "fetched %s from %s (%d bytes, sha256 %s)",
                osm_path,
                endpoint,
                result.size,
                result.sha256[:16],
            )
            return osm_path

    raise OverpassError(
        f"all Overpass endpoints failed for bbox {bbox.as_overpass()!r} "
        f"({len(tried)} attempt(s) on {len(endpoint_list)} endpoint(s)):\n  - " + "\n  - ".join(failures)
    )


def _peek_head(path: Path, limit: int = 160) -> str:
    """Return a one-line excerpt of a response head, for error messages."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit)
    except OSError:  # pragma: no cover - the file was just written
        return "unreadable"
    text = re.sub(r"\s+", " ", raw.decode("utf-8", "replace")).strip()
    return repr(text[:limit])


# Re-exported for callers that catch a fetch failure without importing types.
__all__ += ["OsmFetchError"]
