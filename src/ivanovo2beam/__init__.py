"""ivanovo2beam — a reproducible GIS -> BeamNG.drive map generation pipeline.

The project turns open geodata (OpenStreetMap + Copernicus DEM) into a
drivable BeamNG.drive level at true 1:1 scale: one metre in the game world
equals one metre in the real world.

The pipeline is staged and cacheable::

    download -> osm -> terrain -> roads -> buildings -> vegetation -> export -> package

See ``docs/architecture.md`` for the design and ``docs/coordinate_system.md``
for the projection contract.
"""

from __future__ import annotations

import platform
import sys

if sys.platform == "win32":
    # In restricted Windows sandboxes where WMI COM access is unavailable,
    # Python 3.13's _wmi extension raises a delay-load SEH exception inside
    # platform._wmi_query(). Disabling _wmi makes platform.* fall back cleanly
    # to sys.getwindowsversion().
    platform._wmi = None  # type: ignore[attr-defined]

__version__ = "0.1.0"

__all__ = ["__version__"]
