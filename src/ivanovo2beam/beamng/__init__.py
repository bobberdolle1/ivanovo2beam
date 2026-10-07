"""BeamNG.drive level writers.

This subpackage turns the project's internal metric representation into the
files BeamNG actually loads. Everything here is derived from formats that were
read back out of a real BeamNG 0.39.4 installation rather than from guesswork;
see ``docs/research/beamng_engine_limits.md`` for the evidence.

Modules:
    ter: the ``.ter`` terrain binary codec (format version 9).
    terrain_json: the ``<name>.terrain.json`` terrain descriptor.
    level: the ``info.json`` / ``items.level.json`` scene-tree writer.
    roads: DecalRoad emission.

Nothing in this package ships a BeamNG game asset. Generated levels reference
base-game assets by path only, which is the intended modding workflow.
"""

from __future__ import annotations

from ivanovo2beam.beamng.ter import (
    TER_VERSION,
    TerrainFile,
    decode_ter,
    encode_ter,
    read_ter,
    write_ter,
)

__all__ = [
    "TER_VERSION",
    "TerrainFile",
    "decode_ter",
    "encode_ter",
    "read_ter",
    "write_ter",
]
