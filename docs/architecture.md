# Architecture

## The shape of the problem

Turn open geodata into a BeamNG.drive level at 1:1 scale for a region roughly
220 × 145 km (or high-resolution vertical slices such as the 4.095 × 4.095 km
Golden Sector and the 32.76 × 32.76 km Ivanovo–Shuya corridor), reproducibly,
from a clean checkout, without hand-authoring anything.

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
   The region must be tiled, sectorised, and instanced using native zero-copy
   formats.

## Pipeline

Each stage is a separate module, cacheable on disk, and callable from the CLI.

```
   OpenStreetMap (ODbL)             Copernicus DEM GLO-30         ESA WorldCover 2021 (10 m)
     (.osm.pbf or Overpass)                    │                             │
            │                                  │                             │
   osm/pbf.py & osm/fetch.py                   ▼                             ▼
   osm/parse.py & osm/merge.py         terrain/dem.py             vegetation/worldcover.py
            │                          (GLO-30 / AWS / SRTM)       (cached .npz + .json)
            ├───────────────────┬──────────────┼──────────┬──────────────────┤
            ▼                   ▼              │          ▼                  ▼
   roads/network.py     water/extract.py       │   vegetation/extract.py  buildings/footprints.py
   roads/railways.py            │              │          │                  │
            │                   ├──────────────┘          │                  │
            ▼                   ▼                         │                  │
   roads/bridges.py ◄──► terrain/builder.py               │                  │
   (coupled profiles,     (DEM grid, road grading,        │                  │
    split .dae, ramps)     under-deck water carving,      │                  │
            │              landcover painting, u16/u8)    │                  │
            ▼                   │                         ▼                  ▼
   roads/build.py               │                  vegetation/forest.py   buildings/export.py
   (3D Z-aware decals,          │                  (.link stock assets +  (procedural .dae +
    crossing cuts,              │                   .forest4.json)         material prototypes)
    renderPriority, markings)   │                         │                  │
            └───────────────────┴──────────────┬──────────┴──────────────────┘
                                               ▼
                                 beamng/level.py + beamng/ter.py + provenance.py
                                 (SceneTree, .ter v9, info.json, LevelInfo/ScatterSky,
                                  12 terrain + road/bridge/forest/rail materials)
                                               │
                                               ▼
                              validation/level.py  →  build/levels/<id>/  →  build/<id>.zip
```

## Module boundaries

| Module | Responsibility | Key dependencies |
|---|---|---|
| `gis/` (`projection.py`, `_wgs84.py`) | WGS84 ↔ metric Transverse Mercator (`k0 = 1 − W²/4`), geodesic scale error | `pyproj` |
| `osm/` (`pbf.py`, `fetch.py`, `parse.py`, `merge.py`, `types.py`) | Streaming pure-Python `.osm.pbf` (plus optional `osmium` fast path) & tiled Overpass fetch/merge | stdlib (optional `osmium`) |
| `terrain/` (`dem.py`, `_geotiff.py`, `builder.py`) | DEM acquisition, row-by-row sampling, road grading, coupled water carving, landcover + natural variation painting | `numpy`, `shapely`, optional `rasterio`/`scipy` |
| `roads/` (`network.py`, `build.py`, `materials.py`, `bridges.py`, `railways.py`, `props.py`) | Road extraction, Z-aware DecalRoad export, crossing cuts, `renderPriority`, split bridges, 1520 mm rail, GOST roadside props & guardrails | `numpy`, `shapely` |
| `water/` (`extract.py`, `export.py`) | Waterways & waterbodies → elevation-clamped `River` splines & buffered curved `WaterBlock` tiling | `numpy`, `shapely` |
| `vegetation/` (`worldcover.py`, `extract.py`, `forest.py`, `groundcover.py`) | ESA WorldCover 2021 + OSM woods → keepout masks, organic tapered `.link`/`.forest4.json` trees, native 8-layer `GroundCover` | `numpy`, `shapely`, `Pillow` |
| `buildings/` (`footprints.py`, `mesh.py`, `export.py`) | Optional OSM footprints, terrain-bounds clipping, 500 m chunked Collada blocks with slope skirts & PBR materials | `numpy`, `shapely` |
| `beamng/` (`ter.py`, `level.py`, `materials.py`, `preview.py`) | `.ter` v9 codec, `SceneTree` writer, `LevelInfo` (`decalBias`), 14-surface terrain PBR library + isotropic FBM base maps | `numpy` |
| `sectors/` (`grid.py`, `multiblock.py`, `hybrid.py`) | World ↔ signed square sector grid mapping, P2 multi-`TerrainBlock` experiment & P1 RTIN hybrid corridor prototype | `numpy`, `shapely` |
| `validation/level.py` | Structural checks: scene tree, `.ter`/`.terrain.json` (`HOLE=255` safe), scale, base + extra materials | `numpy` |
| `provenance.py` | SHA-256 input manifests and ODbL / Copernicus attribution notices | stdlib |
| `build.py` & `cli.py` | End-to-end orchestration (`info`, `download`, `build`, `package`, `install`, `validate`), `DecalRoad` bounds clipping, optional config caps | all of the above |

Dependency arrows point one way (`beamng/` never imports `roads/` or
`terrain/`; `build.py` wires stages together), keeping every subsystem testable
in isolation under `tests/test_build_offline.py`.

## Key subsystems

### Why the terrain is the road

A BeamNG `DecalRoad` is a spline ribbon drawn over the terrain with no
collision. Because a 30 m DEM cannot resolve a 7 m carriageway, each road's
centreline is sampled at ~5 m spacing, smoothed, clamped to the configured
`max_grade` via forward/backward passes, pinned at shared junctions
(`pin_junction_elevations`), and blended into the heightmap using an accumulated
influence field (`alpha == 1` inside the carriageway, smoothstep rolloff across
the shoulder). Bridges and tunnels set `grade_terrain=False` so they do not
flatten underlying valleys or rivers.

### Road markings, crossing cuts, and `renderPriority`

`roads/build.py` simplifies geometry in 3D (`_simplify_3d_z_aware`), strips
near-duplicate nodes (`MIN_ROAD_NODE_SPACING_M = 0.15`), removes hairline
reversals (`SPIKE_REVERSAL_DOT = -0.99`), splits polylines so every segment's
axis-aligned bounding box stays within `MAX_SEGMENT_BBOX_SPAN_M = 800.0` (well
inside BeamNG's `SceneStaticDecalRoadManager` 2048 m local packing range), and
sets `DecalRoad` `detail = 0.5`.

Visual layering uses BeamNG 0.40's `renderPriority` convention (lower draws on
top):
- `EDGE_LINE_RENDER_PRIORITY = 1` (inset white/yellow edge lines)
- `CENTER_LINE_RENDER_PRIORITY = 2` (dashed/solid/double dividers, cut cleanly at
  major carriageway crossings via `build_carriageway_index` and
  `cut_polyline_at_crossings`)
- `EDGE_GRASS_RENDER_PRIORITY = 9` (feathered shoulder transition strips)
- `SURFACE_RENDER_PRIORITY`: `13` (motorway/trunk/primary), `14` (secondary..unclassified),
  `15` (service/track/path)
- `AI_INVISIBLE_RENDER_PRIORITY = 20` (`road_invisible` AI graph helpers)

`LevelInfo` sets `"decalBias": 0.0005` in `beamng/level.py` to prevent z-fighting
between decals and graded terrain/bridge decks.

### Coupled hydrology, split 3D bridges, and bridgehead ramps

For crossed waterways, hydrology runs **before** terrain grading so water-surface
and riverbed elevations feed directly into both `carve_water` /
`carve_water_under_decks` / `level_under_decks` and `solve_coupled_bridge_profile`
in `roads/bridges.py`.

Each OSM `bridge=*` way emits:
- Two localized Collada 1.4.1 `.dae` meshes (`build_bridge_split_geometry`):
  `_deck` (slab, curbs/parapets, `ivanovo_bridge_deck` UV material) and
  `_structure` (girder, abutments, piers down to riverbed), referenced by
  separate `TSStatic` objects so deck collision and substructure LODs stay clean.
- Coupled bridgehead approach ramps (`create_bridgehead_ramps`) with eased
  vertical curves pinned to deck endpoints for a flush 0 cm step lip and
  emitted embankments (`15 m` skirts).
- Elevated bridge `DecalRoad` surfaces and lane markings aligned to the solved
  deck profile with zeroed `startEndFade` at bridge portals.

### Summer vegetation: ESA WorldCover 2021 + `.link` + `.forest4.json`

`vegetation/worldcover.py` merges 10 m ESA WorldCover 2021 v200 tree-cover
polygons (`WC_TREES = 10`, cached as pure-NumPy `.npz` + `.json` under
`.cache/landcover/`) with OSM forest, scrub, meadow, and riparian zones.
`vegetation/forest.py` rasterizes corridor, water, bridge, railway, and building
keepouts (`build_keepout_mask`), allocates trees across roadside (`60 m`), core,
and distant backdrop (`> 400 m`) density zones, and emits native
`forest/<species>.forest4.json` instance files plus `managedItemData.json` and
`art/forest/main.materials.json`.

Stock Central Russian summer species (birch, pine, spruce, aspen, oak, shrubs)
from BeamNG's `east_coast_usa` and `italy` levels are referenced via zero-copy
`.link` pointers (`prepare_linked_species`) when a BeamNG installation is
detected; no game asset bytes are duplicated into the repository or mod `.zip`.

### Level configurations: `golden_sector`, `ivanovo_shuya_v2`, and scaling targets

- `configs/golden_sector.yaml` (`ivanovo_golden_sector`): `4096 × 4096` @ `1.0 m`
  (`4.095 km` per side) centered on the Kokhma SE / Р152 (`24К-260`) Uvod river
  crossing, sharing Ivanovo Oblast's Transverse Mercator origin so every
  coordinate matches the corridor 1:1. Used as the high-resolution regression
  baseline and live D3D12 benchmark.
- `configs/ivanovo_shuya_v2.yaml` (`ivanovo_shuya_v2`): `8192 × 8192` @ `4.0 m`
  (`32.764 km` per side, `1,073.5 km²`) covering the continuous ~33 km
  Ivanovo–Kokhma–Shuya corridor with Golden Sector V2 fidelity (`120,000` `.link`
  trees, `8` `GroundCover` layers, `12,000`-building selective cap, `82` 3D
  bridges, `23,711` roadside props) selected from the P0/P1/P2 scaling study
  ([`docs/p3_scaling_study.md`](p3_scaling_study.md)).
- `configs/ivanovo_shuya.yaml` (`ivanovo_shuya`): legacy `4096 × 4096` @ `8.0 m`
  (`32.760 km` per side) lightweight corridor configuration.
- `configs/experiment_single_8192_2m.yaml` & `configs/experiment_hybrid_corridor.yaml`:
  apples-to-apples `16.38 km` P0 single-block vs P1 hybrid (base `TerrainBlock` +
  RTIN Collada corridor chunks) scaling benchmark configs.

## Determinism

Regenerating an unchanged level produces byte-identical output:
- Scene object and material `persistentId`s are UUIDv5 of stable keys
  (`stable_uuid(level_id, class, key)`).
- Vegetation placement and material noise patches use deterministic seeds.
- `.dae` meshes stamp a fixed ISO timestamp (`_FIXED_TIMESTAMP`) and mod `.zip`
  entries use sorted paths with a fixed ZIP timestamp.
- Caches under `.cache/` (`dem/`, `osm/`, `landcover/`, plus local `.osm.pbf`
  extracts) are keyed by SHA-256 over canonical parameters and carry provenance
  `.json` sidecars (`provenance.py`).

## Validation & live BeamNG verification

`validation/level.py` (`ivanovo2beam validate`) verifies required level files,
`SceneTree` parent resolution, `.ter`/`.terrain.json` agreement (excluding
`HOLE=255` terrain cells from layer-index bounds), world bounds/scale, and every
material file (`art/terrains/main.materials.json`, `art/road/main.materials.json`,
`art/forest/main.materials.json`, and `art/shapes/**/main.materials.json`)
including duplicate `persistentId` detection and base-game asset resolution.

Live engine verification uses [`scripts/benchmark_level.py`](../scripts/benchmark_level.py)
and [`scripts/qa_scaling_experiment.py`](../scripts/qa_scaling_experiment.py)
against BeamNG.drive's HTTP MCP telemetry (`--run`) to measure cold/warm load
times, VRAM, RAM, avg FPS, 1% low (`$fps::p99`), vehicle `vel.z` suspension
smoothness, `castRayStatic` collision accuracy, `navgraph` continuity, and
`beamng.log` error counts. See [performance.md](performance.md) and
[p3_scaling_study.md](p3_scaling_study.md).
