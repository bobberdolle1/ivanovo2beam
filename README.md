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

ivanovo2beam download --config configs/golden_sector.yaml
ivanovo2beam build    --config configs/golden_sector.yaml
ivanovo2beam install  --config configs/golden_sector.yaml --beamng "<BeamNG_userdata_dir>"
```

---

## Current status

The end-to-end pipeline generates drivable, 1:1-scale BeamNG.drive levels with
terrain grading, road markings, coupled 3D bridges, rivers/waterbodies,
1520 mm railways, ESA WorldCover summer forests, and structural validation.

| Stage | State | Notes |
|---|---|---|
| Coordinate system (1:1 metric plane) | **Done, verified** | Custom Transverse Mercator (`k0 = 1 − W²/4`); Ivanovo→Shuya 29.138 km vs 29.141 km WGS84 geodesic |
| BeamNG `.ter` terrain format v9 | **Done, verified byte-exactly** | Shipped game terrains round-trip byte-identically |
| Geodata acquisition (DEM + OSM PBF/Overpass + WorldCover) | **Done, cached** | Copernicus GLO-30 / AWS / SRTM DEM, pure-Python `.osm.pbf` streaming + Overpass, ESA WorldCover 2021 10 m `.npz` cache |
| Road network, markings & `renderPriority` | **Done** | Z-aware 3D smoothing, bend taming, junction elevation pinning, crisp crossing cuts (`1.5 m` margin), pruned minor edge decals (`-41.2%` `DecalRoad`s) |
| Coupled hydrology, split bridges & ramps | **Done** | Clamped river channels, scanline-coalesced `WaterBlock`s (`-81.0%`), `2 m` Hermite welded bridge decks with snapped portals & twin inner-parapet suppression |
| Summer vegetation (`.link`, `.forest4.json` & `GroundCover`) | **Done** | 32 zero-copy `.link` species + organic edge tapering, 8 distance-dissolved `GroundCover` layers, `4 m` urban road anti-bleed masking |
| Roadside infrastructure & buildings | **Done** | `Colmesh-1` GOST signs/poles/shelters/guardrails (`-77.3%` static collision), `750 m` chunked OSM buildings with `Colmesh-1` prism colliders & `nulldetail6` LOD |
| Structural validator & provenance | **Done** | Validates scene tree, `.ter` (`HOLE=255` safe), base + extra `main.materials.json` files, ODbL/Copernicus notices |
| Live BeamNG.drive 0.40+ verification | **Stabilized (1080p Ultra)** | Golden Sector V2: `142.88 / 105.77 FPS` (`0` errors, `damageSum=0`); 33 km `ivanovo_shuya_v2`: `99.72–111.16` urban / `106.53` rural / `160–166` 720° spin (`0` hitches `> 33 ms`), `59.646 km` `5/5` unbroken round-trip; `2560×1440` `NOT_TESTED` |
| Scaling architecture study (P0/P1/P2) | **Verified (`Single 8192` wins)** | `Single 8192×8192 @ 4.0 m` (`ivanovo_shuya_v2`) beat Hybrid (`4m + 1m DAE`) and Dual-`TerrainBlock` (see [`docs/p3_scaling_study.md`](docs/p3_scaling_study.md)) |

---

## Level targets

| Config | Level ID | Grid & Extent | Focus |
|---|---|---|---|
| `configs/golden_sector.yaml` | `ivanovo_golden_sector` | `4096 × 4096 @ 1.0 m` (`4.095 × 4.095 km`) | Kokhma SE / Р152 Uvod crossing: primary highway, `way/274102985` bridge, hydrology, summer forest, D3D12 baseline |
| `configs/ivanovo_shuya_v2.yaml` | `ivanovo_shuya_v2` | `8192 × 8192 @ 4.0 m` (`32.764 × 32.764 km`) | **Production 33 km Ivanovo–Kokhma–Shuya corridor**: stabilized `Colmesh-1` props/buildings, `120k` `.link` trees, `GroundCover`, `82` bridges, real in-game previews |
| `configs/ivanovo_shuya.yaml` | `ivanovo_shuya` | `4096 × 4096 @ 8.0 m` (`32.760 × 32.760 km`) | Legacy lightweight Ivanovo → Shuya corridor |
| `configs/ivanovo_mvp.yaml` | `ivanovo_mvp` | `4096 × 4096 @ 1.25 m` (`5.118 × 5.118 km`) | Central Ivanovo road-network stress test |
| `configs/experiment_single_8192_2m.yaml` | `ivanovo_single_8192_2m` | `8192 × 8192 @ 2.0 m` (`16.382 × 16.382 km`) | P0 single-block scaling benchmark |
| `configs/experiment_hybrid_corridor.yaml` | `ivanovo_hybrid_corridor` | `4096 @ 4.0 m` + `134` RTIN `.dae`s (`16.380 km`) | P1 hybrid base+RTIN corridor benchmark |

### Live D3D12 benchmark summary

Measured on Ryzen 5 7500F / RTX 3080 10 GB / BeamNG 0.40.6.0 D3D12 at
`1920×1080 Borderless, Overall: Ultra` (DLSS; `2560×1440`: **`NOT_TESTED`** — active
monitor caps at `1920×1080 @ 144 Hz`):

- **Golden Sector V2 (`ivanovo_golden_sector`, 4.1 km, cold)**: `142.88 / 105.77 FPS`, `35.81 s` wall (`29.28 s` `world_ready_s`), `4,700 MiB` VRAM, `49.90 MB` static collision, `0` level errors, `damageSum = 0` (`1.986 km` @ `95.5 km/h` max)
- **P0 Single `8192@2m` (`16.4 km`) vs P1 Hybrid (`16.4 km`)**: P0 `105.50 / 89.64 FPS`, `27.72 s` warm, `4,930 MiB` VRAM, `damageSum = 0` (`6.37 km`) vs P1 `97.82 / 78.66 FPS`, `33.05 s` warm, `5,021 MiB` VRAM, `-40%` collision in P0
- **Stabilized 33 km Corridor (`ivanovo_shuya_v2`, 32.8 km / 1,073.5 km², cold)**: improved from `45.65–50.99 FPS` → **`99.72–111.16 FPS`** (urban Ivanovo/Shuya) and **`106.53 FPS`** (rural Р152), **`160.0–166.4 FPS`** in 720° camera-spin (`0` hitches `> 33 ms`), `-77.3%` static collision (`735.64 → 166.66 MB`), `64.30 s` cold load, `0` `Bounds` errors, `38 → 19` `DecalRoad` pack warnings, and **`59.646 km` continuous 5/5 unbroken round-trip** (`113.19 FPS` avg, twin-bridge portals clean; minor `way493492348` `10.8%` sag scrape on `rt_1/5`) — see [Performance & budgets](docs/performance.md) and [Project status](docs/STATUS.md)

## How it works

```
  OpenStreetMap (.osm.pbf / Overpass)     Copernicus DEM GLO-30      ESA WorldCover 2021
                  │                                │                         │
                  ▼                                ▼                         ▼
     roads / water / rail / buildings  ◄──►  terrain/builder.py  ◄──  vegetation/forest.py
     (coupled bridges, ramps, cuts)          (grading, water carve,   (.link + .forest4.json)
                  │                           landcover, u16 + u8)           │
                  └────────────────────────────────┼─────────────────────────┘
                                                   ▼
                                     levels/<id>/ (SceneTree + .ter v9 + PBR materials)
                                                   │
                                                   ▼
                                      ivanovo2beam validate → build/<id>.zip
```

Four design decisions matter most:

1. **The terrain is the road.** In BeamNG a `DecalRoad` is only a decal — it has no
   collision. Each road's centreline is sampled from the DEM, smoothed, grade-limited,
   pinned at shared junctions, and blended into the heightmap so the carriageway is
   smooth and drivable.
2. **Hydrology and bridges are coupled.** Rivers and waterbodies are extracted with
   DEM elevations *before* terrain grading so under-deck carving, bridge vertical
   curves, split Collada `_deck`/`_structure` meshes, and bridgehead ramps meet with
   zero step lip.
3. **Real OSM tags always win.** `width`, `lanes`, `surface`, `maxspeed`, `oneway`,
   `bridge`, `tunnel`, `gauge`, and `layer` are read per way; config defaults only fill gaps.
4. **Nothing from the game is copied.** Terrain (`12` surfaces across `100` base-game
   textures), decal roads, bridges, and summer vegetation (`.link` pointers to stock
   `east_coast_usa` / `italy` species) reference BeamNG's shipped assets *by path*,
   keeping the repository and produced `.zip` archives clean of game asset bytes.

## Installation

Requires **Python 3.12+** and BeamNG.drive **0.39+ / 0.40+**.

```bash
pip install -e ".[all]"
```

| Extra | Pulls in | Needed for |
|---|---|---|
| *(core)* | numpy, pyproj, shapely, PyYAML, Pillow | everything (including pure-Python `.osm.pbf` streaming) |
| `raster` | rasterio | reading GeoTIFF DEMs |
| `smooth` | scipy | road profile smoothing |
| `pbf` | osmium | optional native `.osm.pbf` acceleration |
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
  osm/         Overpass fetch, streaming XML + .osm.pbf, tile merge
  terrain/     DEM sources (Copernicus/SRTM/Terrain Tiles), corridor grading, water carve, landcover + breakup
  roads/       road network, DecalRoads, markings/cuts, coupled 3D bridges, 1520 mm rail, GOST props & guardrails
  water/       OSM waterways/waterbodies → clamped River splines & curved WaterBlock tiling
  vegetation/  ESA WorldCover 2021 + OSM woods → .link summer species, .forest4.json & GroundCover
  buildings/   optional OSM footprints & 750 m chunked Collada blocks with Colmesh-1 & slope skirts
  beamng/      .ter v9 codec, scene tree writer, previews/, 14-surface terrain PBR library
  sectors/     spatial grid, P2 multi-TerrainBlock & P1 hybrid corridor harnesses
  validation/  structural & material validation of built levels
  provenance.py SHA-256 input manifests & ODbL/Copernicus attribution notices
  config.py    config loading and validation
  build.py     pipeline orchestration
  cli.py       command line interface
configs/       golden_sector.yaml, ivanovo_shuya_v2.yaml, ivanovo_shuya.yaml, ivanovo_mvp.yaml, experiment_*.yaml
docs/          ROADMAP, architecture, performance, status, p2_multiblock_experiment, p3_scaling_study, benchmarks/, research/
scripts/       live BeamNG MCP benchmark, scaling QA, host bridge, cleanup_beamng_levels.py & experiment helpers
tests/         offline unit/integration suite (`-m "not network"`) + 3 live-network tests
```

## Testing

```bash
pytest -m "not network"                # 777 offline tests, everything CI runs
pytest -m network                        # 3 live Overpass / DEM fetch tests
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

See the full 8-phase engineering plan in [`docs/ROADMAP.md`](docs/ROADMAP.md) (including the dedicated **Procedural Urban Environment Subsystem** and **Universal External Asset Import** spec).

| Phase | Contents | State |
|---|---|---|
| **1. Corridor Stabilization & Optimization** | Zero `Bounds` errors, `-77.3%` static collision, `0` spin hitches, `59.646 km` round-trip, picker cleanup & previews | **Done** (`1920×1080` verified; `2560×1440` `NOT_TESTED`) |
| **2. Procedural Russian Architecture** | Private houses (`наличники`), `хрущёвки`/`брежневки`, balconies, red-brick factories, fences, landmark hooks | Planned |
| **3. Full Russian Road System** | Complete PDD & GOST markings, Cyrillic signs, overhead gantries, traffic lights, turn lanes & priorities | Planned |
| **4. Infrastructure & Urban Environment** | Dedicated `urban/` sidewalks, curbs (`бордюры`), dropped curbs (`пониженные бордюры`), parking, embankments, lighting, power/catenary lines | Planned |
| **5. Street Names, Labels & POI Markers** | Settlement boards, river plates, street plaques, in-game BigMap / QuickTravel POIs | Planned |
| **6. Realistic AI Traffic** | Lane-level PDD `navgraph`, speed/yield/signal rules, Russian vehicle pools, scheduled transit buses | Planned |
| **7. Full 220 × 145 km Ivanovo Oblast** | Tiled `8192 × 8192` sector federation on shared projection with seamless border handoff | Planned |
| **8. Assetto Corsa & Russian World Gen** | Backend-agnostic `WorldIR`, `.kn5`/CSP exporter, generalized multi-region Russian World Generator | Planned |

## Known limitations

* Terrain sampling resolution is 1.0–8.0 m per vertex depending on config while the
  input Copernicus DEM is 30 m: away from graded corridors and carved waterways,
  natural relief follows smooth bilinear DEM interpolation.
* `tunnel=*` ways set `grade_terrain=False` and suppress surface DecalRoad output
  rather than generating subterranean tunnel meshes.
* Overpass is a donated service. Regional builds should use a local Geofabrik
  `.osm.pbf` file in `.cache/` (`osm/pbf.py`); Overpass fallback degrades
  gracefully per tile if rate-limited.
* The whole-oblast build (220 × 145 km) requires multi-`TerrainBlock` tiling at
  BeamNG's 8192-vertex limit — see
  [`docs/research/beamng_engine_limits.md`](docs/research/beamng_engine_limits.md).

## Documentation

* [Roadmap](docs/ROADMAP.md) — 8-phase long-term roadmap, Procedural Urban Environment Subsystem, and external asset integration
* [Coordinate system](docs/coordinate_system.md) — why this projection, and how accuracy is proven
* [Architecture](docs/architecture.md) — pipeline stages, coupled subsystems, and module boundaries
* [Performance & budgets](docs/performance.md) — runtime optimizations, MCP benchmark harness, and D3D12 metrics
* [Project status](docs/STATUS.md) — Golden Sector & stabilized 33 km Corridor V2 sign-off, Before/After benchmarks, and cleanup/reproduction CLI
* [P2 multi-TerrainBlock experiment](docs/p2_multiblock_experiment.md) — empirical dual-block BeamNG 0.40 engine findings
* [P3 scaling study](docs/p3_scaling_study.md) — P0 single-block vs P1 hybrid vs P2 33 km corridor benchmark synthesis
* [External asset reuse](docs/research/asset_reuse.md) — `ccd2beam` & `UUBI_Ivanovo` audit, license isolation, and geo-pinned replacement design
* [Data sources](docs/data_sources.md) — datasets, licences, attribution obligations
* [BeamNG install](docs/beamng_install.md) — getting a generated level into the game
* [Development](docs/development.md) — setup, testing, and the sandbox gotchas
* [BeamNG engine limits](docs/research/beamng_engine_limits.md) — measured limits of the real engine
* [Prior art](docs/research/prior_art.md) — what already existed and what we took from it

## Licence

MIT for the code — see [LICENSE](LICENSE). Geographic data keeps its own licence
and is not relicensed by the MIT grant.
