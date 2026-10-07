# ivanovo2beam

**A reproducible GIS → BeamNG.drive pipeline that turns open geodata into a
drivable, true 1:1 scale map of Ivanovo Oblast, Russia.**

One metre in the game world equals one metre in the real world. Not
approximately — measurably, and asserted by tests against independent
WGS84 geodesics.

```bash
git clone https://github.com/bobberdolle1/ivanovo2beam.git
cd ivanovo2beam
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[all]"

ivanovo2beam download --config configs/ivanovo_mvp.yaml
ivanovo2beam build    --config configs/ivanovo_mvp.yaml
ivanovo2beam install  --config configs/ivanovo_mvp.yaml
```

---

## Current status — read this first

**This is an early, working pipeline. It is not yet a finished map of the
oblast.** The table below is the honest state of each piece.

| Stage | State | Evidence |
|---|---|---|
| Coordinate system (1:1 metric plane) | **Done, verified** | 37 tests; 29.138 km projected vs 29.141 km geodesic (Ivanovo→Shuya) |
| BeamNG `.ter` terrain format v9 | **Done, verified byte-exactly** | 6 shipped terrains round-trip byte-identically |
| Terrain from Copernicus DEM GLO-30 | **Done, working** | 4096×4096 grid @ 1.25 m, real elevations 105.0–153.4 m |
| OSM acquisition (tiled Overpass) | **Working** | 5,688 ways fetched and merged |
| Road network extraction and projection | **Working** | 5,688 roads / 638 km, 19 bridges, 37 tunnels |
| Road corridor grading into terrain | **Working** | roads are flattened and grade-limited into the heightmap |
| BeamNG `DecalRoad` export | **Working** | 5,688 DecalRoads, 34,599 nodes |
| Level scene, `info.json`, spawn point, sun | **Working** | validator 6/6 pass |
| Mod packaging | **Working** | `build/ivanovo_mvp.zip`, 13 entries, 31 MB |
| **Loading the level in BeamNG** | **NOT TESTED** | See the blocker below |
| Buildings, vegetation, water, rail, props | **Not implemented** | milestone M3+ |
| Whole-oblast build | **Not implemented** | milestone M6 |

### The in-game test is blocked, and here is exactly why

The project has not yet been proven to load inside BeamNG.drive on this
machine. Two independent environmental conditions prevented it:

1. **The development sandbox cannot write outside the working directory.** The
   level must be placed in BeamNG's user data folder
   (`%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\levels\`), and that write is
   denied. The bundled `ivanovo2beam install` command does exactly this and will
   work on an ordinary machine.
2. **Steam was not running.** BeamNG.drive 0.39 is a Steam title; launched
   directly from `BeamNG.drive.exe` it exits immediately with code 0.

So the correct status is **`BEAMNG_TEST=NOT_TESTED`**, not "works". Everything
that can be verified without launching the game *has* been verified, including
byte-level comparison against six terrains shipped inside the game itself and a
structural validator that mirrors the checks BeamNG performs.

**To complete the test yourself** (about two minutes):

```bash
# 1. Start Steam first — BeamNG will not run without it.
# 2. Copy the built level in:
ivanovo2beam install --config configs/ivanovo_mvp.yaml
#    or simply drop build/ivanovo_mvp.zip into
#    %LOCALAPPDATA%\BeamNG\BeamNG.drive\current\mods\
# 3. Launch BeamNG, pick "Ivanovo 1:1 - City Centre (MVP)" and spawn.
```

If it loads, please open an issue saying so — that single data point is the most
valuable thing anyone can contribute right now.

---

## What is actually generated

The MVP level `ivanovo_mvp` is a **5.12 × 5.12 km** slice of central Ivanovo:

| Property | Value |
|---|---|
| Terrain | 4096 × 4096 vertices at 1.25 m spacing |
| Real-world extent | 5.118 km per side |
| Elevation range | 105.0 – 153.4 m (real Copernicus DEM) |
| Vertical quantisation | 0.89 mm |
| Materials | 12 surfaces: asphalt, worn asphalt, concrete, gravel, dirt, grass, field, forest floor, riverbank soil, rock, sand, pebbles |
| Roads | 5,688 ways, 638 km total, 19 bridges, 37 tunnels |
| Road decals | 5,688 DecalRoads, 34,599 nodes |
| Download size | 31 MB |
| Build time | ~4.5 min (2 s of which is terrain; most of it is polite OSM fetching) |

## How it works

```
                    ┌─────────────────────────────────────────────┐
  OpenStreetMap ────►  tiled Overpass fetch (1.2 km tiles, cached)│
  (ODbL)            │  parse → merge → project to metres          │
                    └──────────────────┬──────────────────────────┘
                                       │ roads in world metres
                                       ▼
                    ┌─────────────────────────────────────────────┐
  Copernicus DEM ───►  sample onto the terrain grid                │
  GLO-30 (30 m)     │  grade a smooth, grade-limited road corridor │
                    │  quantify: u16 heights + u8 material layers  │
                    └──────────────────┬──────────────────────────┘
                                       │ .ter + level scene
                                       ▼
                    ┌─────────────────────────────────────────────┐
                    │  levels/<id>/info.json                      │
                    │  levels/<id>/main/**/items.level.json       │
                    │  levels/<id>/theTerrain.ter                 │
                    │  levels/<id>/art/terrains/materials.json    │
                    └──────────────────┬──────────────────────────┘
                                       ▼
                              build/<id>.zip  → BeamNG mod
```

Three design decisions matter more than the rest:

**The terrain is the road.** In BeamNG a `DecalRoad` is only a decal — it has no
collision. The drivable surface *is* the terrain underneath. So the pipeline does
not merely drape a road over the landscape: it **carves**. Each road's centreline
is sampled from the DEM, smoothed over ~120 m, clamped to a maximum 12 % grade,
and then blended into the heightmap so the carriageway ends up flat and the
ground returns smoothly to its natural elevation across the shoulder. A 30 m DEM
cannot express a 7 m road on its own; the graded corridor is what makes driving
possible.

**Real OSM tags always win.** `width`, `lanes`, `surface`, `maxspeed`, `oneway`,
`bridge`, `tunnel` and `layer` are read per way. Config values only fill gaps.

**Nothing from the game is copied.** Generated levels reference BeamNG's own
shared textures and decal materials *by path* — 40 base-game terrain textures for
the 12 materials — which is the intended modding workflow and keeps the project
legally clean. The only files in the archive are JSON and one `.ter`.

## Installation

Requires **Python 3.12+** and BeamNG.drive **0.39.x**.

```bash
pip install -e ".[all]"
```

| Extra | Pulls in | Needed for |
|---|---|---|
| *(core)* | numpy, pyproj, shapely, PyYAML, Pillow | everything |
| `raster` | rasterio | reading GeoTIFF DEMs |
| `smooth` | scipy | road profile smoothing |
| `dev` | pytest, ruff | development |

Windows is the primary target because that is where BeamNG runs; the generator
tooling itself is platform-neutral and CI builds levels on Linux.

## Commands

```bash
ivanovo2beam info      # what the config describes, including projected scale error
ivanovo2beam download  # fetch and cache DEM + OSM only
ivanovo2beam build     # full build → build/levels/<id> + build/<id>.zip
ivanovo2beam package   # re-package an existing build
ivanovo2beam validate  # structural checks on a built level
ivanovo2beam install   # copy the level into BeamNG's user data folder
```

Every stage caches under `.cache/`, so a re-run after a failure only redoes what
changed. Add `--json` for machine-readable output.

## Repository layout

```
src/ivanovo2beam/
  gis/         projection, geodesy, metric world plane
  osm/         Overpass fetch, streaming parse, tile merge
  terrain/     DEM sources (Copernicus/SRTM/Terrain Tiles), grid builder, road grading
  roads/       OSM → road network → terrain corridors + DecalRoads
  beamng/      .ter codec, level scene writer, terrain material library
  sectors/     spatial grid
  validation/  structural validation of built levels
  config.py    config loading and validation
  build.py     pipeline orchestration
  cli.py       command line interface
configs/       region and level definitions
docs/          architecture, coordinates, data sources, research
tests/         493 tests; 485 run offline, the rest need network or a BeamNG install
```

## Testing

```bash
pytest -m "not network and not beamng"   # 485 tests, everything CI runs
pytest -m network                        # live Overpass / DEM fetches
pytest -m beamng                         # tests needing a BeamNG install
```

The live-network tests are excluded from CI on purpose: Overpass is a donated
service that rate-limits and goes down, and a red build caused by someone else's
outage teaches nothing. They are run deliberately, by hand.

Two test classes are worth calling out because they are the reason to trust the
output beyond "it ran":

* `TestAgainstShippedBeamNGTerrain` decodes terrains shipped **inside the game**
  and re-encodes them, requiring byte-identical output. If the `.ter` layout
  description were wrong in any way, these six tests fail.
* `test_build_offline.py` runs the entire pipeline — DEM sampling, road
  projection, corridor grading, scene assembly, packaging — against synthetic
  geodata with no network at all. It caught three real bugs during development.

## Data sources and licensing

| Source | Licence | Used for |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) | ODbL 1.0 | roads, and later buildings/water/landuse |
| [Copernicus DEM GLO-30](https://dataspace.copernicus.eu/) | Copernicus DEM Instrument Licence (free, open) | elevation |
| AWS [Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) | mixed, attribution required | DEM fallback |
| [SRTM GL1](https://lpdaac.usgs.gov/products/srtmgl1v003/) | public domain (NASA/USGS) | DEM fallback |

The project code is **MIT**. OSM-derived geodata remains **ODbL** and is never
relicensed; see [`docs/data_sources.md`](docs/data_sources.md) for the full
obligation list, including why a baked game map qualifies as a *Produced Work*
and what that means for attribution.

No Google, Bing or Yandex imagery is used anywhere in the pipeline, and none may
be added: their terms prohibit building derived mapping datasets.

## Roadmap

| Milestone | Contents | State |
|---|---|---|
| **M0 Research** | Engine limits, prior art, licensing | Done |
| **M1 Data pipeline** | Coordinates, OSM, DEM, cache | Done |
| **M2 First drivable sector** | 5 × 5 km Ivanovo, terrain + roads + spawn | Generated; in-game test pending |
| **M3 Ivanovo city** | Buildings, vegetation, water, rail, landmarks | Not started |
| **M4 Ivanovo–Shuya corridor** | ~29 km intercity route | Not started |
| **M5 Regional road network** | Whole-oblast roads, tiled terrain | Not started |
| **M6 Full oblast** | Complete 220 × 145 km | Not started |
| **M7 Optimization** | LOD, sector streaming, quality presets | Not started |
| **M8 Public release** | v1.0.0 | Not started |

## Known limitations

* **The level has not been loaded in the game yet** (see above).
* Only roads and terrain are generated. No buildings, trees, water or rail yet.
* Terrain resolution is 1.25 m while the DEM is 30 m: away from graded road
  corridors the surface is a smooth interpolation of a 30 m grid, so it is
  correct in bulk but has no fine relief.
* `tunnel=*` ways are graded as though they were surface roads, which is wrong
  where a tunnel passes under a hill. Marked for M3.
* Overpass is a donated service. A large build can hit its rate limits; the
  tiled fetch degrades gracefully and reports which tiles are missing, but the
  road network will have gaps.
* The whole-oblast build is arithmetically possible but not implemented. At the
  engine's 8192-vertex limit, 220 × 145 km needs tens of GB of `.ter` data and
  must be tiled — see [`docs/research/beamng_engine_limits.md`](docs/research/beamng_engine_limits.md).

## Documentation

* [Coordinate system](docs/coordinate_system.md) — why this projection, and how accuracy is proven
* [Architecture](docs/architecture.md) — pipeline stages, data flow, and the decisions behind them
* [Data sources](docs/data_sources.md) — datasets, licences, attribution obligations
* [BeamNG install](docs/beamng_install.md) — getting a generated level into the game
* [Development](docs/development.md) — setup, testing, and the sandbox gotchas
* [BeamNG engine limits](docs/research/beamng_engine_limits.md) — measured limits of the real engine
* [Prior art](docs/research/prior_art.md) — what already existed and what we took from it

## Licence

MIT for the code — see [LICENSE](LICENSE). Geographic data keeps its own licence
and is not relicensed by the MIT grant.
