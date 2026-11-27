# Performance & Runtime Budgets

This document tracks `ivanovo2beam` runtime budgets, engine optimizations, and
measured BeamNG.drive D3D12 benchmark results.

## Measurement methodology

Live engine benchmarks are driven reproducibly by
[`scripts/benchmark_level.py`](../scripts/benchmark_level.py) and the scaling &
stabilization QA suite [`scripts/qa_scaling_experiment.py`](../scripts/qa_scaling_experiment.py)
(optionally routed through the localhost allowlisted
[`scripts/beamng_host_bridge.py`](../scripts/beamng_host_bridge.py) when running
from sandboxed terminals on Windows):

1. **Static & log audit** (no game launch): records on-disk `.ter`, mod `.zip`,
   `DecalRoad` node, `.forest4.json` item, `TSStatic`, `WaterBlock`, and `.dae`
   counts, and parses `beamng.log` for stage load times, collision memory, and
   engine errors.
2. **Live D3D12 benchmark (`--run` / `--mode`)**: launches `BeamNG.drive.x64.exe`
   with `-enablemcp` (after wiping `temp/levels/<id>` and
   `temp/art/terrainMaterialCache` for cold-load runs), evaluates static regimes
   (Urban Ivanovo, Urban Shuya, Rural Р152), a **720° fast camera-spin stress
   test** (`extensions.hook` per-frame `dtReal` hitch instrumentation), and an
   unbroken AI `etk800` round-trip drive, sampling:
   - Cold & warm load times (`worldReadyState == 1/2` and `beamng.log` total)
   - Frame pacing (`$fps::instantaneous` average, `$fps::p99` 1% low, CPU/GPU ms, `> 33 ms` / `> 50 ms` frame hitches)
   - GPU & host memory (`nvidia-smi` VRAM, `WorkingSet`, `PrivateMemory`)
   - Static & physics collision budgets from `beamng.log`
   - High-speed passability, vertical velocity (`vel.z`), `damageSum`, `castRayStatic` surface accuracy, and `navgraph` continuity

## Key pipeline optimizations

| Subsystem | Optimization | Impact |
|---|---|---|
| Terrain materials & landcover (`beamng/materials.py`, `terrain/builder.py`) | `DEFAULT_BASE_TEX_SIZE = 512` isotropic FBM base maps + 14 summer terrain PBR layers, softened `4 m` `Field`/`Meadow`/`ForestFloor` transitions, and road-mask anti-bleed rasterization | 4× lower base-map VRAM footprint and zero urban `4 m` grass-through-asphalt bleed |
| Vegetation (`vegetation/forest.py`, `vegetation/groundcover.py`) | Native `.forest4.json` instancing + 32 zero-copy `.link` stock summer species (`vegetation.max_trees`: `45k` Golden Sector / `120k` 33 km corridor) + 8 tuned distance-dissolved `GroundCover` layers | Dense multi-layer summer foliage and 3D grass with `1.01–1.11 s` forest load and `0` camera-spin hitches |
| Roads & DecalRoads (`roads/build.py`, `build.py`) | Tiered LOD, 800 m AABB splitting, `decalBias` stratification (`0.0015..0.005`), `_clip_ground_decal_roads_to_bounds`, bend taming (`min_seg` cleanup), and pruned minor `_edge_` decals | Cut `DecalRoad`s from `69,000` → `40,601` (`-41.2%`), load time `11.88 s` → `3.80 s`, and `DecalRoad` batch warnings `38` → `19` (`0` in Golden Sector) |
| Bridges (`roads/bridges.py`) | C1 Hermite coupled profiles (`DECK_STEP_M = 2.0`), split Collada geometry (`_deck` + `_structure`) with welded vertices, `0.000 m` snapped portals, and twin-bridge inner parapet suppression | Zero portal bump or inner-parapet collision across all twin/single bridges |
| Props & guardrails (`roads/props.py`) | `Colmesh-1` / `"Collision Mesh"` proxies on poles, bus stops, signs, and guardrails (eliminating `Visible Mesh Final` high-poly colliders & `"Bounds"` errors), `useInstanceRenderData` + `meshCulling`, smart residential pole thinning, and full carriageway clearance checks | Halved `road_props`/`TSStatic` (`24,743` → `12,464`), eliminated all `112` prop / `140` guardrail carriageway overlaps and `1,001` `"Bounds"` log errors |
| Buildings (`buildings/export.py`, `buildings/mesh.py`) | Terrain-bounds filtering, `12,000` selective cap on 33 km corridor, `750 m` spatial chunk batching, simplified flat-top `Colmesh-1` prism colliders, `nulldetail6` LOD, and lean `main.materials.json` | Reduced chunks (`797` → `456`), building collision tris (`-36.9%`), and `buildings/main.materials.json` size (`-98.0%`) |
| Water (`water/export.py`) | `terrain_bounds` polygon clipping before tiling + `O(nx·ny)` scanline RLE maximal-rectangle `WaterBlock` coalescing (`8..96 m` cells) | Slashed `WaterBlock` count from `9,764` → `1,859` (`-81.0%`) |

## Measured D3D12 benchmarks

Hardware: **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM**,
**BeamNG.drive 0.40.6.0 D3D12**, `Overall: Ultra`, `Borderless`, DLSS.

> [!NOTE]
> Physical monitor `DISPLAY1` caps at **`1920×1080 @ 144 Hz`**. True
> **`2560×1440`** output is **`NOT_TESTED`** (`resolution_warning` recorded in
> benchmark JSONs).

### 33 km Corridor (`ivanovo_shuya_v2`) Stabilization: Before (`9f0fc11`) vs Final After (`7985014`)

| Metric | Before (`stabilization_before.json`, Cold) | Final After (`stabilization_after.json`, Cold) | Change |
|---|---|---|---|
| Load time (`wall` / `world_ready_s`) | `91.38 s` / `91.33 s` | **`64.30 s` / `64.18 s`** | **`-29.6%`** |
| **Urban Ivanovo** (avg / 1% low / min) | `45.65 / 36.61 / 15.47 FPS` (`cpu 25.22 ms`) | **`99.72 / 80.04 / 59.71 FPS`** (`cpu 10.23 ms`) | **`+118.4%`** avg |
| **Urban Shuya** (avg / 1% low / min) | `47.73 / 38.82 / 5.50 FPS` (`cpu 38.75 ms`) | **`111.16 / 88.18 / 33.46 FPS`** (`cpu 9.85 ms`) | **`+132.9%`** avg |
| **Rural Р152** (avg / 1% low / min) | `50.99 / 39.13 / 34.22 FPS` (`cpu 20.97 ms`) | **`106.53 / 87.62 / 68.70 FPS`** (`cpu 9.47 ms`) | **`+108.9%`** avg |
| **720° Camera-Spin (Urban Ivanovo)** | `69.59 FPS` avg / `47.16` 1% low (`21.35 ms` max) | **`166.40 FPS`** avg / **`115.65`** 1% low (`8.73 ms` max, `0` hitches `> 33 ms`) | **`+139.1%`** |
| **720° Camera-Spin (Forest Corridor)** | `63.51 FPS` avg / `41.53` 1% low (`24.27 ms` max) | **`159.99 FPS`** avg / **`119.14`** 1% low (`8.52 ms` max, `0` hitches `> 33 ms`) | **`+151.9%`** |
| Peak VRAM (`nvidia-smi`, regimes) | `6,293.0 MiB` (`rural_p152`) | **`5,694.0 MiB`** (`6,128.0 MiB` round-trip peak) | **`-9.5%`** |
| Process RAM (`WorkingSet` / `Private`) | `7,924.0` / `12,497.7 MiB` | **`6,661.1–6,818.8`** / **`10,485.2–10,995.6 MiB`** | **`-14%` to `-16%`** |
| Static / Physics collision | `735.64 MB` (`18.01M` tris) / `618.46 MB` | **`166.66 MB`** (`4.10M` tris) / **`140.78 MB`** | **`-77.3%`** / **`-77.2%`** |
| Unbroken 5-leg round-trip (`rt_1..rt_5`) | Blocked on Leg 2 Kokhma guardrail | **`59.646 km`** (`5/5` `reached_target=true`, **`113.19 FPS`** avg / `91.67` 1% low) | **Complete** |
| `beamng.log` (`Bounds` / `DecalRoad` errors) | `0` / `38` | **`0`** / **`19`** | **`-50.0%`** |

### Golden Sector V2 (`ivanovo_golden_sector`) vs P0, P1, P2 Corridor V2 & Baselines

| Metric | Golden Sector V2 (`golden_sector_v2_regression.json`, `7985014`) | P0 Single `8192@2m` (`p0_single_8192_2m.json`) | P1 Hybrid `4m+1m` (`p1_hybrid_corridor.json`) | **Stabilized P2 Corridor `8192@4m` (`stabilization_after.json`, `7985014`)** | P2 Dual-Block (`ivanovo_dual_terrain_exp`) | `mapng_golden_sector` baseline |
|---|---|---|---|---|---|---|
| Cache state | **Cold** | Warm (`37.08 s` cold) | Warm (`52.98 s` cold) | **Cold** | Cold | Warm / Cold |
| Load time (`world_ready_s`) | `29.28 s` (`35.81 s` wall) | `27.72 s` (`37.08 s` cold) | `33.05 s` (`52.98 s` cold) | **`64.18 s`** (`64.30 s` wall) | `147.21 s` | `37.7 s` / `75.6 s` |
| Average FPS (`$fps::instantaneous`) | **`142.88 FPS`** | `105.50 FPS` | `97.82 FPS` | **`99.72–111.16` (urban) / `106.53` (rural) / `113.19` (round-trip)** | `175.96 FPS` | `47.27 FPS` |
| 1% low FPS (`$fps::p99`) | **`105.77 FPS`** | `89.64 FPS` | `78.66 FPS` | **`80.04–88.18` (urban) / `87.62` (rural) / `91.67` (round-trip)** | `140.82 FPS` | `35.58 FPS` |
| GPU VRAM peak (`nvidia-smi`) | **`4,700.0 MiB`** (`4,653.8` avg) | `4,930.0 MiB` (`4,923.0` avg) | `5,021.0 MiB` (`5,162` cold) | `5,283–5,694 MiB` (`6,128` round-trip peak) | `4,861.0 MiB` | `8,893 MiB` |
| Process RAM (`WorkingSet` / `Private`) | `3,923.9` / `7,099.8 MiB` | `5,562.6` / `9,209.4 MiB` | `4,888.7` / `8,409.5 MiB` | `6,655.9–6,818.8` / `10,485.2–10,995.6 MiB` | `94.1` / `72.3 MiB` (subset) | — |
| Static / Physics collision | `49.90 MB` / `41.94 MB` | `160.54 MB` / `134.66 MB` | `224.53 MB` / `189.13 MB` | **`166.66 MB` / `140.78 MB`** | `53.36 MB` / `44.42 MB` | — |
| Live P152 `etk800` passability | `1.986 km` (`95.5 km/h` max), `damageSum = 0`, `0` level errors | `6.37 km` (`98.04 km/h` max), `damageSum = 0` | `4.56 km`, `damage = 6,416` (Leg 1) / `0` (Leg 2) | `59.646 km` continuous 5/5 round-trip (`rt_2/3/4` `0` new damage; minor `way493492348` `10.8%` sag scrape on `rt_1/5`) | Stops at `x=8350` seam (`navgraph` `z=0` / missing raycast) | — |
| `2560×1440` physical display | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | — |

### Key Takeaways

- **Stabilization impact (`ivanovo_shuya_v2`):** Replacing `Visible Mesh Final` prop/building collision with `Colmesh-1` / `"Collision Mesh"`, thinning minor residential poles, pruning redundant `_edge_` decals, and coalescing `WaterBlock`s cut static & physics collision by **`77.3%`**, doubled urban/rural FPS (**`45.7–51.0 → 99.7–111.2 FPS`**), boosted 720° camera-spin FPS to **`160.0–166.4 FPS`** with **`0` hitches `> 33 ms`**, and cleared the full **`59.646 km`** Ivanovo–Kokhma–Kitovo–Shuya–Ivanovo round-trip.
- **P0 Single `8192 × 8192` vs P1 Hybrid (`16.38 × 16.38 km`):** P0 beat P1 in avg FPS (`+7.9%`), 1% low FPS (`+14.0%`), load time, VRAM, collision memory (`-40%` tris), drive safety, native `GroundCover`, and smooth 5-channel `TerrainMaterial` blending ([`p3_scaling_study.md`](p3_scaling_study.md)).

## Level size budgets (on-disk specs)

| Config | Terrain grid | Ground spacing | World extent | `.ter` file size | Mod `.zip` |
|---|---|---|---|---|---|
| `configs/golden_sector.yaml` (`ivanovo_golden_sector`) | `4096 × 4096` | `1.0 m` | `4.095 × 4.095 km` | `48.00 MiB` (`50,331,771 B`) | `35.88 MiB` |
| `configs/experiment_dual_terrain.yaml` (`ivanovo_dual_terrain_exp`) | `2 × 2048 × 2048` | `1.0 m` | `4.094 × 2.047 km` | `2 × 12.0 MiB` | — |
| `configs/experiment_single_8192_2m.yaml` (`ivanovo_single_8192_2m`) | `8192 × 8192` | `2.0 m` | `16.382 × 16.382 km` | `192.00 MiB` (`201,326,715 B`) | — |
| `configs/experiment_hybrid_corridor.yaml` (`ivanovo_hybrid_corridor`) | `4096 × 4096` + `134` DAEs | `4.0 m` (`1.0 m` RTIN) | `16.380 × 16.380 km` | `48.00 MiB` (+`155.10 MiB` DAE) | — |
| `configs/ivanovo_shuya.yaml` (`ivanovo_shuya`) | `4096 × 4096` | `8.0 m` | `32.760 × 32.760 km` | `48.00 MiB` | — |
| `configs/ivanovo_shuya_v2.yaml` (`ivanovo_shuya_v2`) | `8192 × 8192` | `4.0 m` | `32.764 × 32.764 km` | `192.00 MiB` (`201,326,715 B`) | `160.26 MiB` |
