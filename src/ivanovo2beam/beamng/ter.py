"""Reader and writer for BeamNG's ``.ter`` terrain file, format version 9.

The layout below was recovered by reading real terrain files shipped with
BeamNG.drive 0.39.4.0.20972 and measuring their exact byte counts. It is
reproduced here so the project never depends on a game installation to build
terrain.

Binary layout (little-endian)::

    u8                        version        (9)
    u32                       size           (vertices per side)
    u16[size * size]          heightMap
    u8 [size * size]          layerMap       (material index per cell)
    u32                       materialCount
    { u8 length, u8[length] } * materialCount   UTF-8 material names

so the payload is exactly ``5 + 3 * size * size + trailer`` bytes, where the
trailer is ``4 + sum(1 + len(name))``. This was verified byte-exactly against
seven shipped files (``payload / size**2 == 3.000000`` in every case).

Note that ``<name>.terrain.json`` advertises a ``layerTextureMap`` section in
its ``binaryFormat`` string, but no such section exists in the v9 payload. That
descriptor string is descriptive legacy text and must not be trusted; the
byte accounting is the authority.

Height encoding
---------------
Heights are stored as unsigned 16-bit values which BeamNG maps onto
``[0, maxHeight]`` metres above the terrain block's ``position.z``::

    elevation_m = position_z + stored * (maxHeight / 65536)

The ``/65536`` (not ``/65535``) is what the engine uses, so ``stored == 65535``
yields slightly less than ``maxHeight``. Vertical precision for a 120 m range is
therefore 120 / 65536 = 1.83 mm, far below DEM accuracy.

Materials
---------
The layer map is one byte per cell indexing the material list. Index **255 is
reserved to mean "hole"** (no terrain drawn) and the engine ignores material
entries after the 254th, so a terrain may reference at most 254 real materials.

BeamNG requires ``size`` to be a power of two between 128 and 8192 inclusive.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from numpy.typing import NDArray

#: ``.ter`` format version this module reads and writes.
TER_VERSION = 9

#: Smallest and largest legal ``size`` (vertices per side).
MIN_SIZE = 128
MAX_SIZE = 8192

#: Layer-map value meaning "hole" (terrain not drawn in this cell).
HOLE = 255

#: The engine ignores materials after the 254th, so at most this many are usable.
MAX_MATERIALS = 254

#: Divisors BeamNG uses to convert stored heights to metres. Note 65536, not 65535.
_HEIGHT_DIVISOR = 65536.0

_HEADER = struct.Struct("<BI")


class TerrainFormatError(ValueError):
    """Raised when a ``.ter`` payload is malformed or unsupported."""


def _validate_size(size: int) -> None:
    if not isinstance(size, int):
        raise TerrainFormatError(f"size must be an int, got {type(size).__name__}")
    if not (MIN_SIZE <= size <= MAX_SIZE):
        raise TerrainFormatError(
            f"terrain size {size} outside the engine's supported range "
            f"[{MIN_SIZE}, {MAX_SIZE}]"
        )
    if size & (size - 1):
        raise TerrainFormatError(f"terrain size {size} must be a power of two")


def _validate_materials(materials: Sequence[str]) -> tuple[str, ...]:
    names = tuple(str(m) for m in materials)
    if not names:
        raise TerrainFormatError("a terrain must declare at least one material")
    if len(names) > MAX_MATERIALS:
        raise TerrainFormatError(
            f"{len(names)} materials declared but the engine only honours {MAX_MATERIALS}"
        )
    seen: set[str] = set()
    for name in names:
        if not name:
            raise TerrainFormatError("material names must not be empty")
        encoded = name.encode("utf-8")
        if len(encoded) > 255:
            raise TerrainFormatError(f"material name too long for a u8 length prefix: {name!r}")
        if name in seen:
            raise TerrainFormatError(f"duplicate material name: {name!r}")
        seen.add(name)
    return names


@dataclass(frozen=True, slots=True)
class TerrainFile:
    """An in-memory ``.ter`` terrain.

    Attributes:
        size: Vertices per side. The grid is square.
        heights: ``(size, size)`` uint16 array of stored heights.
        layers: ``(size, size)`` uint8 array of material indices, or
            :data:`HOLE` for a hole.
        materials: Material names, indexed by :attr:`layers`.
        version: Format version. Only :data:`TER_VERSION` is supported.
    """

    size: int
    heights: NDArray[np.uint16]
    layers: NDArray[np.uint8]
    materials: tuple[str, ...]
    version: int = TER_VERSION

    def __post_init__(self) -> None:
        if self.version != TER_VERSION:
            raise TerrainFormatError(
                f"unsupported .ter version {self.version}; this build writes {TER_VERSION}"
            )
        _validate_size(self.size)
        expected = (self.size, self.size)
        if self.heights.shape != expected:
            raise TerrainFormatError(f"heights shape {self.heights.shape} != {expected}")
        if self.layers.shape != expected:
            raise TerrainFormatError(f"layers shape {self.layers.shape} != {expected}")
        if self.heights.dtype != np.uint16:
            raise TerrainFormatError(f"heights dtype must be uint16, got {self.heights.dtype}")
        if self.layers.dtype != np.uint8:
            raise TerrainFormatError(f"layers dtype must be uint8, got {self.layers.dtype}")
        object.__setattr__(self, "materials", _validate_materials(self.materials))
        n_materials = len(self.materials)
        top = int(self.layers.max()) if self.layers.size else 0
        if top >= n_materials and top != HOLE:
            raise TerrainFormatError(
                f"layer map references material index {top} but only {n_materials} are declared"
            )

    # ------------------------------------------------------------------ helpers

    @property
    def cell_count(self) -> int:
        """Number of cells in the grid (``size * size``)."""
        return self.size * self.size

    def payload_size(self) -> int:
        """Exact on-disk size in bytes of this terrain."""
        trailer = 4 + sum(1 + len(m.encode("utf-8")) for m in self.materials)
        return _HEADER.size + 3 * self.cell_count + trailer

    def material_histogram(self) -> dict[str, int]:
        """Count cells per material name, including ``"hole"`` if present."""
        counts = np.bincount(self.layers.ravel(), minlength=len(self.materials))
        out = {name: int(counts[i]) for i, name in enumerate(self.materials)}
        if counts.size > HOLE and counts[HOLE]:
            out["hole"] = int(counts[HOLE])
        return out

    def with_layers(self, layers: NDArray[np.uint8]) -> TerrainFile:
        """Return a copy of this terrain with a replacement layer map."""
        return TerrainFile(
            size=self.size,
            heights=self.heights,
            layers=layers,
            materials=self.materials,
            version=self.version,
        )

    # --------------------------------------------------------- height conversion

    def to_elevations(self, base_z: float, max_height: float) -> NDArray[np.float64]:
        """Decode stored heights to metres above sea level.

        Args:
            base_z: The terrain block's ``position.z`` in metres.
            max_height: The terrain block's ``maxHeight`` in metres.

        Returns:
            ``(size, size)`` float64 array of elevations in metres.
        """
        if max_height <= 0:
            raise ValueError(f"max_height must be positive, got {max_height}")
        scale = max_height / _HEIGHT_DIVISOR
        return base_z + self.heights.astype(np.float64) * scale

    @classmethod
    def from_elevations(
        cls,
        elevations: NDArray[np.floating],
        layers: NDArray[np.uint8],
        materials: Sequence[str],
        base_z: float,
        max_height: float,
    ) -> TerrainFile:
        """Quantise metric elevations into a terrain.

        Args:
            elevations: ``(n, n)`` float array of metres above sea level.
            layers: ``(n, n)`` uint8 material indices.
            materials: Material names.
            base_z: Terrain block ``position.z`` in metres. Elevations below
                this are clamped.
            max_height: Vertical range in metres above ``base_z``.

        Returns:
            A quantised :class:`TerrainFile`.

        Raises:
            ValueError: If shapes disagree or ``max_height`` is not positive.
        """
        elev = np.asarray(elevations, dtype=np.float64)
        if elev.ndim != 2 or elev.shape[0] != elev.shape[1]:
            raise ValueError(f"elevations must be a square 2-D array, got {elev.shape}")
        if max_height <= 0:
            raise ValueError(f"max_height must be positive, got {max_height}")
        if layers.shape != elev.shape:
            raise ValueError(f"layers shape {layers.shape} != elevations shape {elev.shape}")

        scale = max_height / _HEIGHT_DIVISOR
        stored = np.rint((elev - base_z) / scale)
        # Clamp rather than wrap: silent wrap-around would produce a terrain
        # that looks plausible but is metres wrong in the wrong direction.
        np.clip(stored, 0.0, 65535.0, out=stored)
        return cls(
            size=int(elev.shape[0]),
            heights=stored.astype(np.uint16),
            layers=np.ascontiguousarray(layers, dtype=np.uint8),
            materials=tuple(materials),
        )

    @staticmethod
    def fit_vertical_range(
        elevations: NDArray[np.floating],
        margin_m: float = 5.0,
    ) -> tuple[float, float]:
        """Choose ``(base_z, max_height)`` covering ``elevations`` with margin.

        Quantisation error is ``max_height / 65536``; a small margin keeps real
        elevation data away from both clamped ends while retaining millimetre
        precision.

        Args:
            elevations: Elevation samples in metres.
            margin_m: Metres of headroom added below and above the data.

        Returns:
            ``(base_z, max_height)`` ready for :meth:`from_elevations`.

        Raises:
            ValueError: If ``elevations`` has no finite values.
        """
        finite = np.asarray(elevations, dtype=np.float64)
        finite = finite[np.isfinite(finite)]
        if finite.size == 0:
            raise ValueError("no finite elevations supplied")
        lo = float(finite.min()) - margin_m
        hi = float(finite.max()) + margin_m
        if hi <= lo:
            hi = lo + max(1.0, margin_m)
        return lo, hi - lo


# --------------------------------------------------------------------- codec


def encode_ter(terrain: TerrainFile) -> bytes:
    """Serialise a terrain to its exact ``.ter`` byte representation."""
    parts = [_HEADER.pack(terrain.version, terrain.size)]
    parts.append(np.ascontiguousarray(terrain.heights, dtype="<u2").tobytes())
    parts.append(np.ascontiguousarray(terrain.layers, dtype=np.uint8).tobytes())
    parts.append(struct.pack("<I", len(terrain.materials)))
    for name in terrain.materials:
        encoded = name.encode("utf-8")
        parts.append(struct.pack("<B", len(encoded)))
        parts.append(encoded)
    blob = b"".join(parts)
    if len(blob) != terrain.payload_size():  # pragma: no cover - internal invariant
        raise AssertionError(
            f"encoded {len(blob)} bytes but payload_size() predicted {terrain.payload_size()}"
        )
    return blob


def decode_ter(data: bytes) -> TerrainFile:
    """Parse a ``.ter`` payload.

    Args:
        data: Raw file contents.

    Returns:
        The decoded :class:`TerrainFile`.

    Raises:
        TerrainFormatError: If the payload is truncated, has an unsupported
            version, or is internally inconsistent.
    """
    if len(data) < _HEADER.size:
        raise TerrainFormatError(f"file too short: {len(data)} bytes")
    version, size = _HEADER.unpack_from(data, 0)
    if version != TER_VERSION:
        raise TerrainFormatError(
            f"unsupported .ter version {version}; this build supports {TER_VERSION}"
        )
    _validate_size(size)

    n_cells = size * size
    heights_end = _HEADER.size + 2 * n_cells
    layers_end = heights_end + n_cells
    if len(data) < layers_end:
        raise TerrainFormatError(
            f"truncated .ter: need {layers_end} bytes for a {size}x{size} grid, got {len(data)}"
        )

    heights = np.frombuffer(data, dtype="<u2", count=n_cells, offset=_HEADER.size).reshape(size, size)
    layers = np.frombuffer(data, dtype=np.uint8, count=n_cells, offset=heights_end).reshape(size, size)

    if layers_end + 4 > len(data):
        raise TerrainFormatError("truncated .ter: missing material count")
    (n_materials,) = struct.unpack_from("<I", data, layers_end)
    if n_materials > MAX_MATERIALS:
        raise TerrainFormatError(
            f"material count {n_materials} exceeds the engine maximum of {MAX_MATERIALS}"
        )

    cursor = layers_end + 4
    materials: list[str] = []
    for i in range(n_materials):
        if cursor >= len(data):
            raise TerrainFormatError(f"truncated .ter: missing material name {i + 1}/{n_materials}")
        (length,) = struct.unpack_from("<B", data, cursor)
        cursor += 1
        if cursor + length > len(data):
            raise TerrainFormatError(f"truncated .ter: material name {i + 1} runs past end of file")
        materials.append(data[cursor : cursor + length].decode("utf-8", "replace"))
        cursor += length

    return TerrainFile(
        size=size,
        # Copy so the caller owns writable memory rather than a view into `data`.
        heights=heights.copy(),
        layers=layers.copy(),
        materials=tuple(materials),
        version=version,
    )


def read_ter(path: str | Path) -> TerrainFile:
    """Read and decode a ``.ter`` file from disk."""
    return decode_ter(Path(path).read_bytes())


def write_ter(path: str | Path, terrain: TerrainFile) -> int:
    """Encode and write a ``.ter`` file.

    The write is direct rather than atomic-by-rename: rename-based replacement
    is not available under every sandbox this project runs in, and a partially
    written terrain is always re-derivable from the pipeline's cache.

    Args:
        path: Destination path. Parent directories are created.
        terrain: Terrain to write.

    Returns:
        Number of bytes written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = encode_ter(terrain)
    target.write_bytes(blob)
    return len(blob)
