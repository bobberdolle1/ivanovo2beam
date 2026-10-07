# Development

## Setup

```bash
git clone https://github.com/bobberdolle1/ivanovo2beam.git
cd ivanovo2beam
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate
pip install -e ".[all]"
```

`[all]` pulls in rasterio (GeoTIFF DEMs), scipy (profile smoothing) and the dev
tools. The core install works without them; only the features that need them
will complain.

Check the environment:

```bash
ivanovo2beam info --config configs/ivanovo_mvp.yaml
```

This prints the resolved projection, the measured scale error and the terrain
geometry, and needs no network access.

## Running the tests

```bash
pytest -m "not network and not beamng"    # what CI runs; no network at all
pytest -m network                         # live Overpass and DEM fetches
pytest -m beamng                          # needs a BeamNG installation
pytest tests/test_projection.py -v        # one module
```

Markers are registered in `pyproject.toml`, so `--strict-markers` keeps them
honest.

Three test groups carry most of the weight:

| Test | What it proves |
|---|---|
| `tests/test_projection.py` | distances match independent WGS84 geodesics, not the projection's own arithmetic |
| `tests/test_ter.py::TestAgainstShippedBeamNGTerrain` | the `.ter` codec is byte-exact against six terrains shipped inside the real game |
| `tests/test_build_offline.py` | the whole pipeline runs end to end on synthetic data and produces a valid, packageable level |

That last one is the regression net. It has already caught three real bugs that
no unit test would have found, because they only appear when the stages are
composed.

## Building a level

```bash
ivanovo2beam build --config configs/ivanovo_mvp.yaml --osm-tile-km 1.2
```

Everything lands under `build/`, everything downloaded lands under `.cache/`,
and both are gitignored. A failed run is resumable: cached tiles are reused, so
re-running after a network failure only fetches what is missing.

Useful flags:

| Flag | Effect |
|---|---|
| `--force` | ignore caches and re-download |
| `--no-zip` | skip packaging, for a faster inner loop |
| `--osm-tile-km` | Overpass request size; smaller tiles are more reliable |
| `--osm-budget-s` | give up on remaining tiles after this many seconds |
| `--json` | machine-readable output (global flag, before the subcommand) |

## Configuring a different region

Nothing about Ivanovo is hard-coded. The region lives entirely in
`configs/*.yaml`:

```yaml
region:
  id: yaroslavl
  bbox: {west: 37.3, south: 56.5, east: 40.0, north: 58.7}
  projection:
    central_meridian: 38.7        # region centre; k0 is derived from the bbox
    latitude_of_origin: 57.6
    origin: [39.87, 57.63]        # world origin, in degrees
level:
  id: yaroslavl_mvp
  center: [39.87, 57.63]
  terrain:
    grid_size: 4096
    square_size_m: 1.25
```

`ivanovo2beam info` will tell you the resulting scale error before you build
anything. `optimal_k0()` derives the scale factor from the bounding box; set
`scale_factor` explicitly to override it.

## Code style

```bash
ruff check .
ruff format .
```

Line length 110. Type hints are expected on public functions; the code base
targets Python 3.12 and uses `from __future__ import annotations` throughout.

## Platform notes and sandbox gotchas

The generator is platform-neutral and CI builds levels on Linux. Two Windows
specific traps are worth knowing, because both produce failures that look like
bugs in the code:

**`tempfile.mkdtemp` is hostile under a restricted Windows sandbox.** `mkdtemp`
applies `chmod 0o700`, and on Windows that writes a *protected* DACL which
strips inherited permissions. The resulting directory cannot be written into by
the process that just created it. Use `pathlib.Path.mkdir()` with an explicit
name instead, as `tests/conftest.py` does.

**pytest's `tmp_path` and cache providers create directories by atomic rename.**
Where renames are restricted this leaves an unreadable directory behind and can
fail an entire session at teardown. `tests/conftest.py` overrides `tmp_path`
with a plain-`mkdir` implementation, and `-p no:cacheprovider` is set in
`pyproject.toml`.

Both are documented rather than worked around silently, because a contributor
hitting them on a normal machine should get a clear answer rather than a mystery.

## Adding a new pipeline stage

1. Put the logic in its own module under `src/ivanovo2beam/<stage>/`.
2. Take a config dataclass rather than reading globals; add the defaults to
   `DEFAULT_ROAD_CLASSES`-style tables in `config.py`.
3. Make it deterministic. If you need variety, derive a seed from a stable
   identifier such as the OSM element id.
4. Give it a progress callback of the form `progress(fraction, message)`.
5. Add a CLI subcommand in `cli.py`.
6. Test it against synthetic input first, then add a `network`-marked live test.

## Debugging a generated level

`ivanovo2beam validate` checks the things that stop BeamNG loading a level:

```bash
ivanovo2beam validate --config configs/ivanovo_mvp.yaml
ivanovo2beam validate --config configs/ivanovo_mvp.yaml --beamng "F:\SteamLibrary\steamapps\common\BeamNG.drive"
```

Without `--beamng` it checks structure only: required files present, every
`items.level.json` parseable, every `__parent` resolving to a real group, the
`.ter` parsing, and the `.terrain.json` descriptor agreeing with it. With
`--beamng` it additionally resolves all 40 referenced textures against the
game's asset archives.

The `.ter`/`.terrain.json` consistency check earns its place: a descriptor whose
`size` or material list disagrees with the binary terrain produces a level that
lists correctly and then loads to a black screen.

## Release checklist

1. `pytest -m "not network and not beamng"` is green.
2. `ruff check .` and `ruff format --check .` are clean.
3. `ivanovo2beam build` on `configs/test_sector.yaml` succeeds.
4. `ivanovo2beam validate` passes on the result.
5. CHANGELOG updated with what actually works, and what does not.
6. README status table updated — including `BEAMNG_TEST`, which must not claim
   more than was measured.
7. Tag `vX.Y.Z`, and write release notes that restate the limitations.
