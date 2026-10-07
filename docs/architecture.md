# Architecture

## The shape of the problem

Turn open geodata into a BeamNG.drive level at 1:1 scale for a region roughly
220 × 145 km, reproducibly, from a clean checkout, without hand-authoring
anything.

Three constraints shape every decision below:

1. **The projection must be metric.** Degrees are not metres, and at Ivanovo's
   latitude a degree of longitude is 1.83× shorter than a degree of latitude, so
   raw WGS84 coordinates would produce a sheared map. See
   [coordinate_system.md](coordinate_system.md).
2. **In BeamNG the terrain is the road.** A `DecalRoad` is a decal with no
   collision; the drivable surface is the terrain underneath it. Roads therefore
   have to be *carved* into the heightmap, not draped over it.
3. **The region is far too large for static authoring.** Naive scaling of shipped
   content implies tens of millions of roads and hundreds of millions of objects.
   The region must be tiled and sectorised.

## Pipeline

Each stage is a separate module, cacheable on disk, and callable from the CLI.

```
   OpenStreetMap (ODbL)                    Copernicus DEM GLO-30
            │                                        │
   osm/fetch.py   tiled Overpass,                     │
            │     mirror failover, cache              │
   osm/parse.py   streaming XML                       │
            │                                        │
   osm/merge.py   merge tiles by element id           │
            │                                        │
            ▼                                        ▼
   roads/network.py                       terrain/dem.py
     project to metres                      source chain:
     parse width/lanes/surface/             Copernicus → AWS Terrain
     maxspeed/oneway/bridge/tunnel           Tiles → SRTM GL1
            │                                        │
            │                                        │
            ├──────────────┬─────────────────────────┘
            ▼              ▼
   roads/build.py    terrain/builder.py
     → RoadCorridor    sample DEM onto the grid
                         (row by row, memory-bounded)
                       grade corridors: resample,
                         smooth ~120 m, clamp to
                         max grade, blend into the grid
                       quantise → u16 heights + u8 layers
            │              │
            └──────┬───────┘
                   ▼
            beamng/level.py  ← beamng/materials.py
              scene tree, info.json, TerrainBlock, SpawnSphere,
              ScatterSky/TimeOfDay for the real latitude, DecalRoads
                   │
            beamng/ter.py   .ter v9 encoder
                   │
                   ▼
            build/levels/<id>/…  →  build/<id>.zip
```

## Module boundaries

| Module | Responsibility | Depends on |
|---|---|---|
| `gis/projection.py` | WGS84 ↔ metric world plane, scale-error measurement | pyproj |
| `osm/` | Acquire and parse OSM; merge tiles | stdlib only |
| `terrain/dem.py` | Multi-source DEM acquisition, sampling, caching | rasterio |
| `terrain/builder.py` | Grid construction, corridor grading | numpy, DEM protocol |
| `roads/` | OSM ways → road network → corridors + DecalRoads | shapely-free; numpy |
| `beamng/ter.py` | `.ter` v9 codec | numpy |
| `beamng/level.py` | Scene tree and level file writing | stdlib |
| `beamng/materials.py` | Terrain material library | stdlib |
| `sectors/grid.py` | World ↔ sector mapping | stdlib |
| `validation/` | Structural checks on built levels | stdlib |
| `build.py` | Orchestration of the above | all of the above |
| `cli.py` | Command line | `build.py` |

Dependency arrows point one way. `beamng/` never imports `roads/`; `roads/`
never imports `terrain/`; both are wired together in `build.py`. That keeps each
stage testable with synthetic input — `tests/test_build_offline.py` runs the
entire pipeline with a hand-built DEM and OSM extract and no network.

## Why the terrain is the road

This is the decision the whole project turns on, so it is worth stating plainly.

A BeamNG `DecalRoad` is a spline ribbon drawn over the terrain. It carries no
collision. If you generate a road network of decals over unmodified ground, the
player sees asphalt and physically drives on a ploughed field.

A 30 m DEM — the best freely available data for this region — cannot express a
7 m road on its own: adjacent samples are further apart than the road is wide.
So the ground under each road is re-profiled:

1. the centreline is sampled from the DEM at ~5 m spacing;
2. the profile is smoothed over ~120 m, which is roughly a real vertical curve;
3. it is then clamped to a maximum gradient by a forward and a backward pass —
   the standard earthworks construction, and the reason the result is the
   closest achievable profile rather than an arbitrary smoothed one;
4. the profile is blended into the heightmap with an influence field that is 1
   inside the carriageway and falls smoothly to 0 at the corridor edge, so the
   road blends into the landscape instead of cutting a trench.

The same influence field then classifies the surface for material painting:
`alpha == 1` exactly inside a carriageway, intermediate on the shoulder. Two
real bugs came out of getting this wrong, both now covered by tests — painting
per segment (which let a neighbouring segment repaint a carriageway as shoulder)
and comparing a running maximum against itself.

Bridges and tunnels set `grade_terrain=False`: their ground must not be
flattened. They are still exported as decals until real geometry exists
(issue #9).

## Determinism

Regenerating an unchanged level produces byte-identical output. This is a
licensing requirement as much as an engineering one: ODbL section 4.6 can be
satisfied by publishing the *method* of deriving a produced work, and the method
has to actually reproduce the result.

* Every scene object `persistentId` is a UUIDv5 of stable keys
  (`stable_uuid(level_id, class, key)`).
* Material `persistentId`s are UUIDv5 of the material key.
* No unseeded randomness anywhere. Procedural variation, when it arrives, derives
  from the OSM element id.
* Mod zip entries are written in sorted order with a fixed timestamp.
* Caches are content-addressed, so a cache hit and a fresh download produce the
  same input.

Asserted by `tests/test_build_offline.py::test_build_is_deterministic`.

## Caching

```
.cache/
  dem/<sha256>.tif          + .json sidecar (source, URLs, checksum, licence)
  osm/<sha256>.osm          + .json sidecar (endpoint, osm_base, checksum)
```

Keys are SHA-256 over a canonical description of the request — a bounding box
rounded to 1e-6 degrees plus source and resolution — not over an arbitrary float
representation. Sidecars record which source *actually* delivered the data, so
the DEM fallback chain stays honest about attribution.

A failed build is resumable: cached tiles are reused, so a re-run after a network
failure fetches only what is missing.

## Sectors

`sectors/grid.py` maps world coordinates to fixed-size square sectors and back.
Sectors are the unit of caching, of validation and, eventually, of runtime
streaming. Identifiers encode signed grid indices (`sector_x0042_y0108`), because
a world origin inside the region necessarily produces negative indices in every
direction.

The default sector size is 2 km: a 220 × 145 km region is 111 × 73 sectors, large
enough that per-sector overhead is negligible and small enough to be a
comfortable unit of work.

Sectors are currently used for organisation only. Whether they can drive runtime
streaming is an open question — issue #12 — and depends on engine behaviour that
has not been measured yet.

## Why nothing from the game is copied

Generated levels reference BeamNG's own shared assets by path: 40 base-game
terrain textures across 12 materials, and decal-road materials by name from
`art/shapes/common/decalroads`. This is the intended modding workflow, and it
keeps the repository free of game content. The produced archive contains only
JSON and one `.ter` — asserted by a test.

Reading the game's files to learn formats is a different activity from shipping
them, and the distinction is maintained deliberately: the `roadArchitect`
profiles that ship with the game were read on a local installation to learn the
schema, and are neither committed nor shipped.

## Error handling philosophy

* **A partial result beats no result, provided it is labelled.** Failed OSM tiles
  do not abort a build; they produce a warning naming how many are missing. A
  degraded network is still worth driving.
* **External services must not be able to hang a build.** Overpass mirror
  failover uses exponential backoff up to 60 s per attempt, which once stalled a
  run for twenty minutes. Acquisition now takes a wall-clock budget and proceeds
  with what it has.
* **Validation reports everything, then exits non-zero.** `validate_level`
  returns a report rather than raising on the first problem, because when a level
  will not load you want the whole list, not the first item.

## Testing strategy

| Layer | Approach |
|---|---|
| Projection | compared against independent `pyproj.Geod` geodesics, never against itself |
| `.ter` codec | byte-exact round trip of six terrains shipped inside the real game |
| OSM | offline fixtures plus one live, `network`-marked test |
| DEM | synthetic rasters; the provider chain driven by fake providers |
| Corridor grading | numerical invariants: carriageway flat, grade respected, distant terrain untouched |
| Whole pipeline | `test_build_offline.py`, synthetic data, no network |
| Built level | structural validator mirroring what BeamNG checks |

CI runs everything except the `network` and `beamng` marked tests, on Python 3.12
and 3.13. Network and engine tests are run deliberately, by hand, because a red
build caused by someone else's outage teaches nothing.
