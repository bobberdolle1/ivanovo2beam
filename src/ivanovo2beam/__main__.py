"""Allow ``python -m ivanovo2beam`` as an alternative to the console script."""

from __future__ import annotations

from ivanovo2beam.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
