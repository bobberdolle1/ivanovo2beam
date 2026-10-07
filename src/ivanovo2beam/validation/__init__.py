"""Validation of generated levels.

The point of this package is to catch the mistakes that make BeamNG silently
refuse to load a level, or load it in a broken state, *before* the game is
launched. Those mistakes are almost always structural: a missing file, a
``.terrain.json`` that disagrees with the ``.ter`` beside it, a material index
that points past the end of the material list, or a texture path that does not
exist in the game.

Checks are split into ones that always run and ones that need a BeamNG
installation to resolve asset paths.
"""

from __future__ import annotations

from ivanovo2beam.validation.level import (
    CheckResult,
    ValidationReport,
    validate_level,
)

__all__ = ["CheckResult", "ValidationReport", "validate_level"]
