# Performance & Runtime Budgets

This document tracks `ivanovo2beam` runtime budgets, engine optimizations, and
measured BeamNG.drive D3D12 benchmark results.

## Measurement methodology

Live engine benchmarks are driven reproducibly by
[`scripts/benchmark_level.py`](../scripts/benchmark_level.py):

1. **Static & log audit** (no game launch): records on-disk `.ter`, mod `.zip`,
   `DecalRoad` node, `.forest4.json` item, `TSStatic`, and `.dae` counts, and
   parses `beamng.log` for stage load times, collision memory, and errors.
2. **Live D3D12 benchmark (`--run`)**: launches `BeamNG.drive.x64.exe` with
   `-enablemcp` after wiping level/material/foliage caches (`temp/levels/<id>`,
   `temp/art/terrainMaterialCache`, `temp/assets/meshes/foliage`) for cold-load
   runs, drives the configured route, and samples:
   - Cold & warm load times (`worldReadyState == 1/2` and `loading screen is unloaded`)
   - Frame pacing (`$fps::instantaneous` average, `$fps::p99` 1% low, CPU/GPU ms)
   - GPU & host memory (`nvidia-smi` VRAM, DXGI VRAM, `WorkingSet`, `PrivateMemory`)
   - Static & physics collision budgets from `beamng.log`
   - Vehicle suspension smoothness (`vel.z` min/max/std across bridges) and `damage`

## Key pipeline optimizations

| Subsystem | Optimization | Impact |
|---|---|---|
| Terrain materials (`beamng/materials.py`) | `DEFAULT_BASE_TEX_SIZE = 512` (down from 1024) | 4× lower base-map VRAM footprint |
| Vegetation (`vegetation/forest.py`) | Native `.forest4.json` instancing + zero-copy `.link` stock summer assets + spatial density zoning (`200 / 125 / 50` stems/ha across roadside/core/backdrop) | Eliminates duplicated mesh bytes and bounds distant forest triangle budget |
| Roads (`roads/build.py`) | Tiered LOD (`distance_fade`, `break_angle`, `detail = 0.5`) + 800 m AABB splitting (`MAX_SEGMENT_BBOX_SPAN_M`) | Keeps all `DecalRoad` batches inside BeamNG's 2048 m local range and reduces distant draw overhead |
| Bridges (`roads/bridges.py`) | Split Collada geometry (`_deck` + `_structure`) with separate `TSStatic` entries | Clean deck collision without inflating detailed pier collision overhead |
| Water (`water/export.py`) | Adaptive `WaterBlock` `gridElementSize` (`8.0..96.0 m`) | Avoids BeamNG >100 m water grid scale errors while capping tessellation |
| OSM & DEM ingestion (`osm/pbf.py`, `terrain/builder.py`) | Two-pass streaming `.osm.pbf` bbox filter + row-by-row DEM grid sampling | Extracts regional corridors in `< 250 MB` RAM |

## Measured D3D12 benchmarks

Hardware: **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM**,
**BeamNG.drive 0.40.6.0 D3D12**, `Overall: Ultra`, `Borderless`.

> [!NOTE]
> Physical monitor `DISPLAY1` caps at **`1920×1080 @ 143 Hz`**. True
> **`2560×1440`** output is **`NOT_TESTED`** (`resolution_warning` recorded in
> `build/benchmarks/golden_sector_1080p_{warm,native}.json`).

| Metric | `ivanovo_golden_sector` (`1920×1080` DLSS, warm) | `ivanovo_golden_sector` (`1920×1080` `render_scale=1.0`) | `mapng_golden_sector` baseline (`1920×1080`) |
|---|---|---|---|
| Cold load (all 3 caches wiped) | `26.59 s` (`worldReadyState=1`, `27.1 s` MCP) | — | `75.6 s` / `122.2 s` |
| Warm load (`worldReadyState=1`) | `21.60 s` (`22.43 s` wall-clock) | `21.50 s` (`22.18 s` wall-clock) | `37.7 s` / `38.9 s` |
| Average FPS (`$fps::instantaneous`) | **`133.09 FPS`** (30 samples) | **`139.25 FPS`** (20 samples) | `47.27 FPS` |
| 1% low FPS (`$fps::p99`) | **`114.42 FPS`** | **`118.44 FPS`** | `35.58 FPS` |
| Frametime (avg / 1% low) | `7.51 ms` / `8.74 ms` (`cpu 4.91`, `gpu 5.41 ms`) | `7.18 ms` / `8.44 ms` (`cpu 4.58`, `gpu 6.57 ms`) | — |
| GPU VRAM peak (`nvidia-smi`) | **`3,965.0 MiB`** (`3,912.6 MiB` avg) | **`3,999.0 MiB`** (`3,920.9 MiB` avg) | `8,893 MiB` |
| Process RAM (`WorkingSet` / `Private`) | `3,809.3 MiB` / `6,658.6 MiB` | `3,825.4 MiB` / `6,670.7 MiB` | — |
| Static / Physics collision | `31.73 MB` (`594,943` verts) / `26.38 MB` (`48,805` inst) | same (`31.73 MB` / `26.38 MB`) | — |
| Bridge both-ways drive (`way274102985`) | `damage_max = 0`, `bridge_vz_abs_max = 0.65 m/s` (from live drive test) | — | — |
| `beamng.log` level/road/forest errors | **`0`** (`0` `SceneStaticDecalRoadManager`, `MaterialList`, `ForestData`, `WaterBlock`) | `0` | — |
| `2560×1440` physical display | **`NOT_TESTED`** (monitor cap `1920×1080`) | **`NOT_TESTED`** | — |

### Corridor scaling guidance (`ivanovo_golden_sector` → `ivanovo_shuya`)

- Keep `4096 × 4096 @ 8.0 m` (`48.00 MiB` `.ter`, `baseTexSize = 512`) as the
  full-corridor terrain with `LevelInfo.visibleDistance` bounded to
  `10,000–12,000 m` (`fogDensity ~ 0.00015–0.0002`) so distant cells cull.
- Weight the `.forest4.json` tree budget (`40–60k` summer stems,
  `lodReflectScalar: 0.0`, no `.imposter.dds.link` fallbacks) along a
  `500–800 m` Р152 roadside corridor.
- Retain tiered `DecalRoad` `distanceFade`, 800 m AABB splitting, `2 m` bridge
  resampling, `clip_bounds` + adaptive `WaterBlock` `gridElementSize`, and `4 km`
  sector chunking for bridge/railway `TSStatic`s to target `4–5 GB` VRAM and
  `>= 60 FPS` on RTX 3080-class GPUs.

## Level size budgets (on-disk specs)

| Config | Terrain grid | Ground spacing | World extent | `.ter` file size | Mod `.zip` |
|---|---|---|---|---|---|
| `configs/golden_sector.yaml` (`ivanovo_golden_sector`) | `4096 × 4096` | `1.0 m` | `4.095 × 4.095 km` | `48.00 MiB` (`50,331,755 B`) | `30.86 MiB` (`32,358,030 B`) |
| `configs/ivanovo_shuya.yaml` (`ivanovo_shuya`) | `4096 × 4096` | `8.0 m` | `32.760 × 32.760 km` | `48.00 MiB` | — |
