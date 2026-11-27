# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions are assigned by what actually works, not by schedule. A version number
that claims a capability the project does not have would be worse than no
release at all.

## [Unreleased]

### Added
- **33 km Corridor Stabilization, Optimization & Continuous Round-Trip QA (`9f0fc11` → `7985014`)**:
  - **Live Before → After D3D12 Benchmarks (`docs/benchmarks/{stabilization_before,stabilization_after,golden_sector_v2_regression}.json`, `scripts/qa_scaling_experiment.py`)**:
    - Urban Ivanovo: `45.65 / 36.61 FPS` → **`99.72 / 80.04 FPS`** (`+118.4%`); Urban Shuya: `47.73 / 38.82 FPS` → **`111.16 / 88.18 FPS`** (`+132.9%`); Rural Р152: `50.99 / 39.13 FPS` → **`106.53 / 87.62 FPS`** (`+108.9%`).
    - 720° fast camera-spin stress test: **`166.40 FPS`** avg / **`115.65`** 1% low (`8.73 ms` max) in Urban Ivanovo and **`159.99 FPS`** avg / **`119.14`** 1% low (`8.52 ms` max) in Forest Corridor with **`0` hitches `> 33 ms`**.
    - Cold load time `91.38 s` → **`64.30 s`** (`-29.6%`); static collision `735.64 MB` (`18.01M` tris) → **`166.66 MB`** (`4.10M` tris, **`-77.3%`**); physics memory `618.46 MB` → **`140.78 MB`** (`-77.2%`); `Bounds collision not supported` errors **`0`** (from `1,001`); `SceneStaticDecalRoadManager` batch warnings `38` → **`19`** (`0` in Golden Sector V2).
    - **Continuous unbroken 5-leg round-trip (`59.646 km`, Ivanovo → Kokhma → Kitovo → Shuya → Kokhma → Ivanovo)**: **`5/5` `reached_target = true`** at **`113.19 FPS`** avg / **`91.67`** 1% low (`0` new damage on legs 2/3/4 and clean twin-bridge portal crossings; minor `way493492348` `10.8%` sag scrape on `rt_1/5`).
    - **Golden Sector V2 Regression (`7985014`)**: `142.88 FPS` avg / `105.77 FPS` 1% low (`4,700 MiB` VRAM, `35.81 s` cold load, `0` level errors, `damageSum = 0` over `1.986 km`).
  - **Level Picker Metadata, Real Previews & Cleanup (`beamng/level.py`, `beamng/previews/`, `configs/ivanovo_shuya_v2.yaml`, `scripts/cleanup_beamng_levels.py`)**: Added curated `LEVEL_METADATA`, `install_bundled_previews()` copying real `1280×720` in-game screenshots to `<level_id>_preview.png` and `preview.png`, and `scripts/cleanup_beamng_levels.py` (`--dry-run` / `--stop-beamng`; identifies `14` obsolete builds / `1,030.14 MiB` in `F:\Games\BeamNG.drive\current` while preserving `ivanovo_golden_sector`, `ivanovo_shuya_v2`, and third-party maps).
  - **Long-Term Roadmap & External Asset Reuse Architecture (`docs/ROADMAP.md`, `docs/research/asset_reuse.md`)**: Authored 8-phase roadmap including the dedicated **Procedural Urban Environment Subsystem** (`src/ivanovo2beam/urban/`) and license-isolated `assets_external/` + `asset_manifest.yaml` / `ExternalAssetIR` research audit covering `ccd2beam` and `UUBI_Ivanovo`.
- **P0/P1/P2 Scaling Study & 33 km Production Corridor V2 (`configs/ivanovo_shuya_v2.yaml`, `docs/p3_scaling_study.md`)**:
  - **P0 (`configs/experiment_single_8192_2m.yaml`, `scripts/experiment_single_8192.py`, `terrain/dem.py`)**: `8192×8192 @ 2.0 m` (`16.382 × 16.382 km`) single `TerrainBlock` benchmark + offline overlapping GeoTIFF DEM cropping/resampling fallback. Live D3D12 (`1920×1080 Ultra DLSS`): `105.50 FPS` avg / `89.64 FPS` 1% low, `4,930 MiB` VRAM, `27.72 s` warm / `37.08 s` cold load, `6.37 km` `etk800` drive with `damageSum = 0`.
  - **P1 Hybrid (`sectors/hybrid.py`, `configs/experiment_hybrid_corridor.yaml`, `scripts/experiment_hybrid_terrain.py`)**: `4096×4096 @ 4.0 m` depressed base `TerrainBlock` + `134` RTIN `1.0 m` Collada corridor chunks (`0.08 m` tolerance, `1.65M` tris). Verified `DecalRoad overObjects=true` on `Visible Mesh` and clean `0.54–0.55 m` collision separation, but measured `97.82 FPS` avg / `78.66 FPS` 1% low, `+40%` collision memory, `33.05 s` warm / `52.98 s` cold load, baked checkerboard/sawtooth shoulder seams, and lost `GroundCover` on `.dae`s.
  - **P2 Production Corridor (`configs/ivanovo_shuya_v2.yaml`, `config.py`, `build.py`)**: Selected **Single `TerrainBlock 8192×8192 @ 4.0 m`** (`32.764 × 32.764 km`, `192.00 MiB` `.ter`, `corridor_half_width_m = 14.0`) with optional `buildings.max_buildings = 12000` (`11,945` placed across Ivanovo, Kokhma, and Shuya), `vegetation.max_trees = 120000` (`32` `.link` species + `8` `GroundCover` layers), `82` 3D bridges, `23,711` roadside props, and `6` corridor `SpawnSphere` presets. Live D3D12 (`p2_ivanovo_shuya_v2.json`, cold): `80.32 FPS` avg / `48.68 FPS` 1% low, `6,333 MiB` VRAM, `78.35 s` cold load, `25.229 km` unbroken Ivanovo→Shuya `navgraph` (`486` nodes, `0` `z=0` nodes), `0.0000–0.0051 m` `castRayStatic` accuracy, `damageSum = 0` on Ivanovo→Kokhma and Shuya legs, and `0` `GroundCover`/`ForestData`/`TerrainBlock`/`WarningMaterial`/`SceneContainer::checkBins` errors (`2560×1440`: `NOT_TESTED`).
  - **Reusable Live Scaling QA Suite (`scripts/qa_scaling_experiment.py`, `docs/benchmarks/`)**: Automated anonymized D3D12 benchmark JSONs for Golden Sector V2 regression, P0, P1, and P2 (`1920×1080`).
- **Golden Sector V2 Visuals, GroundCover, Roadside Props & Buildings**:
  - **14-surface Central Russian summer terrain PBR (`beamng/materials.py`, `terrain/builder.py`)**: Muted `east_coast_usa`-inspired palette, added `Meadow` and `DryGrass` materials, isotropic 512 px FBM base maps (eliminating directional banding), multi-scale organic landcover dithering/breakup, feathered canopy-strip transitions, and narrowed `Gravel` road shoulder trim so grass billboards never poke onto asphalt edges or bridge decks.
  - **Native `GroundCover` (`vegetation/groundcover.py`)**: 8 distance-dissolved `GroundCover` layers (`322,000` maxElements, `.link` 3D foliage shapes + stock billboards) masked strictly to meadow/grass/field/forest soils.
  - **Denser organic summer forests (`vegetation/forest.py`)**: Expanded from 26 to 30 zero-copy `.link` species with 14 m noise-feathered edge tapering.
  - **Russian roadside infrastructure (`roads/props.py`)**: 604 scene objects (`TSStatic` signs for speed/priority/give-way/crosswalk/bridge/bend, streetlights, wooden utility poles, OSM bus stops, and localized W-beam steel guardrails on bridge approaches & steep curves).
  - **Regional OSM buildings in Golden Sector (`configs/golden_sector.yaml`, `buildings/`)**: Enabled 500 m spatial chunked houses, dachas, sheds, apartments, and industrial buildings (`1,426` placed inside terrain bounds across `34` `.dae` chunks with slope skirts and pitched/skillion roofs; out-of-bounds footprints clipped).
- **P2 Multi-`TerrainBlock` Experiment (`sectors/multiblock.py`, `configs/experiment_dual_terrain.yaml`, `scripts/experiment_dual_terrain.py`, `docs/p2_multiblock_experiment.md`)**: Isolated 2×1 `2048×2048 @ 1.0 m` dual-block level (`0.000 m` seam ΔZ) with live BeamNG 0.40 proof of single-`TerrainBlock` bindings for `castRayStatic`, `core_terrain`, `GroundCover`, and cross-seam `DecalRoad` projection, plus hybrid scaling recommendation.
- **Localhost BeamNG Host Bridge (`scripts/beamng_host_bridge.py`, `scripts/benchmark_level.py`)**: Allowlisted HTTP bridge (`127.0.0.1:29300`, token-authenticated) bypassing Windows Low-IL sandbox isolation (`antigravity-cli#1575`) for autonomous level install, cache clearing, start/stop, MCP forwarding, and log inspection.
- **Test & V2 live verification status**: `777 passed, 3 deselected` (`pytest tests/ -m "not network"`), `ivanovo2beam validate` `9 passed, 0 failed, 0 warnings`. Live D3D12 verification on `ivanovo_golden_sector` V2 (`1920×1080 Borderless, Overall: Ultra`) measured `133.39 FPS` avg / `106.03 FPS` 1% low (`4,729 MiB` VRAM, `27.89 s` cold load) and `134.39 FPS` avg / `108.70 FPS` 1% low (`4,666 MiB` VRAM, `21.82 s` warm load), high-speed `etk800` Р152 crossings up to `176.19` / `178.60 km/h` (`151.48–173.04 km/h` over `way/274102985` bridge) with `damageSum = 0`, and `0` level/material/GroundCover errors in `beamng.log` (`2560×1440`: `NOT_TESTED` due to `1920×1080` monitor cap).

### Changed
- **Stabilization pipeline optimizations (`roads/`, `buildings/`, `water/`, `terrain/`, `vegetation/`)**:
  - `roads/props.py`: switched stock poles/bus shelters/signs & procedural guardrails to `Colmesh-1` / `"Collision Mesh"`, enabled `useInstanceRenderData` + `meshCulling`, thinned minor residential dead-end poles (`14,008 → 4,460`), fixed carriageway clearance checks (`0` road-blocking props/guardrails), and expanded `TSStatic` reduction (`24,743 → 12,464`).
  - `buildings/mesh.py`, `buildings/export.py`: added simplified `Colmesh-1` prism colliders (`-36.9%` building collision tris), `nulldetail6` LOD, `750 m` spatial chunks (`797 → 456`), and pruned unused `buildings/main.materials.json` entries (`-98.0%`).
  - `roads/build.py`, `roads/bridges.py`: pruned minor `_edge_` decals (`DecalRoad`s `69,000 → 40,601`), tamed tight hairpin bends, welded bridge `.dae` vertices, snapped bridge portals to `0.000 m`, and suppressed twin-bridge inner parapets.
  - `water/export.py`: clipped `WaterBody` polygons to `terrain_bounds` before tiling and added `O(nx·ny)` scanline RLE maximal-rectangle `WaterBlock` coalescing (`9,764 → 1,859`, `-81.0%`).
  - `terrain/builder.py`, `vegetation/groundcover.py`, `vegetation/forest.py`: eliminated `4.0 m` urban road grass bleed, softened `Field`/`Meadow`/`ForestFloor` borders, and tuned `GroundCover` culling.
- **Ground `DecalRoad` bounds clipping & prop scaling (`build.py`, `roads/props.py`)**: Added `_clip_ground_decal_roads_to_bounds` to keep `overObjects=false` road decals strictly inside `TerrainBlock` bounds (eliminating `SceneContainer::checkBins` octree warnings without trimming `ROAD_RENDER_MARGIN_M = 250 m` grading/cuts) and replaced `O(N×M)` `bus_stop`/`crossing` road scanning with a `32 m` spatial hash grid.
- Tightened `roads/build.py` `CarriagewayIndex.is_blocked` circular endpoint balloons and T-junction fork partner logic (`crossing_margin_m = 1.5`, `min_piece_m = 1.5`), switched `m_road_asphalt_edge_grass` to `t_eca_asphalt_dirt_grass_edge_*.dds` without sawtooth alpha artifacts, and added `decalBias` (`0.0015..0.0025`).
- Upgraded `water/extract.py` and `terrain/builder.py` with transverse + longitudinal bank clamping (eliminating floating rivers and stepped vertical cliffs) and buffered curved `WaterBlock` tiling (`water/export.py`).
- Upgraded `roads/bridges.py` to `DECK_STEP_M = 2.0` C1 cubic Hermite profiles with textured concrete/steel parapets.
- Previously added: Golden Sector benchmark target (`configs/golden_sector.yaml`), Ivanovo–Shuya (`configs/ivanovo_shuya.yaml`), `.osm.pbf` streaming (`osm/pbf.py`), WorldCover 2021 (`vegetation/worldcover.py`), coupled hydrology & split bridges, railways, crossing cuts/LOD, provenance, and `benchmark_level.py`.

### Changed
- Reduced default terrain base texture resolution (`DEFAULT_BASE_TEX_SIZE = 512`)
  for 4× lower base-map VRAM usage, and expanded the terrain material library to
  12 surfaces across 100 base-game texture paths.
- Added `"decalBias": 0.0005` on `LevelInfo` in `beamng/level.py` to prevent
  decal z-fighting.
- Hardened `ivanovo2beam install --force` (`cli.py`) to purge stale target
  subdirectories in-place before mirroring.
- Extended `validation/level.py` to ignore `HOLE=255` terrain cells in layer-index
  bounds checks and validate `art/road`, `art/forest`, and `art/shapes/**`
  `main.materials.json` files + duplicate `persistentId`s.

### Fixed
- Terrain landcover painting preserves interior polygon holes via
  `v.to_polygon()` in `Grass -> Field -> ForestFloor -> Concrete` order.
- Eliminated bridge portal fade artifacts by zeroing `startEndFade` on bridge
  surface and marking `DecalRoad`s.

## [0.1.0] — 2026-10-07

First working end-to-end pipeline. **Not yet verified inside BeamNG.drive** —
see `BEAMNG_TEST` in the README.

### Added

**Coordinate system**
- A custom Transverse Mercator centred on Ivanovo Oblast, with an
  automatically derived scale factor `k0 = 1 − W²/4` that balances distortion
  between the central meridian and the region edge.
- `geo_to_world` / `world_to_geo` with exact inverses and vectorised variants.
- Measured accuracy: 0.0106 % worst case; Ivanovo → Shuya projects to
  29.138 km against a 29.141 km WGS84 geodesic.

**BeamNG interchange**
- `.ter` terrain codec for format version 9, derived by byte-accounting real
  files and verified by re-encoding six terrains shipped with the game and
  requiring byte-identical output.
- Level scene writer producing `info.json`, `map.json` and the
  `main/**/items.level.json` scene tree with deterministic object UUIDs.
- Terrain material library covering 12 surfaces, referencing 40 base-game
  shared textures by path so that no game asset is copied.
- DecalRoad export with per-node widths.

**Geodata**
- Tiled Overpass acquisition with mirror failover, content-addressed caching and
  a wall-clock budget, merged by element id so a way reported by several tiles
  appears once with full geometry.
- Streaming OSM XML parser (13 MB peak versus 54 MB for a DOM parse on a
  60,000-node document).
- DEM acquisition from Copernicus GLO-30 with automatic fallback to AWS Terrain
  Tiles and SRTM GL1, each verified against live endpoints.
- Road extraction with real OSM tag precedence over class defaults.

**Terrain generation**
- DEM sampling onto the world grid, row by row, so a 4096² grid does not need
  16.7 million points in memory at once.
- Road corridor grading: centreline profiles are resampled, smoothed over
  ~120 m and clamped to a maximum grade, then blended into the heightmap so the
  carriageway is flat and the terrain returns smoothly across the shoulder.

**Tooling**
- `ivanovo2beam` CLI: `info`, `download`, `build`, `package`, `validate`,
  `install`.
- Structural level validator checking required files, scene-tree parent
  resolution, `.ter`/`.terrain.json` agreement and asset-path resolution.
- 550+ tests; CI runs lint, the offline suite on Python 3.12 and 3.13, a
  complete synthetic end-to-end build, and config sanity checks.

**First generated level**
- `ivanovo_mvp`: 5.118 km square of central Ivanovo. 4096² terrain at 1.25 m
  (elevations 105.0–153.4 m from real Copernicus DEM), 5,688 roads totalling
  638 km, 19 bridges, 37 tunnels, 34,599 DecalRoad nodes.

### Fixed

Bugs found and corrected during development, each caught by a test rather than
by inspection:

- Road corridor grading painted the carriageway as *shoulder* wherever a cell
  lay beyond the endpoints of the nearest segment, because painting happened
  per segment instead of from the accumulated influence field.
- The material-ownership test compared a running maximum against itself after
  updating it, so no surface was ever painted.
- `TerrainMaterialSpec.all_asset_paths` merged base and macro texture mappings
  by channel key, which silently discarded every base texture.
- The DEM gap filler was quadratic and attempted a 3.5 TiB allocation once
  unfilled cells exceeded a few thousand; replaced with a linear morphological
  fill.
- `SceneTree.render` never emitted the root group's own `items.level.json`,
  leaving every top-level group with a dangling parent — a level BeamNG would
  not have loaded correctly.
- `SectorId` parsing rejected negative indices because the sign consumed one
  character of the zero-padded field.

### Known issues

- **The level has not been loaded in BeamNG.drive.** The development
  environment cannot write to BeamNG's user data folder and Steam was not
  running. Status is reported as `BEAMNG_TEST=NOT_TESTED`.
- `tunnel=*` ways are graded as surface roads.
- Buildings, vegetation, water and rail are not implemented.
