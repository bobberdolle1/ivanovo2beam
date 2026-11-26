# Performance & Runtime Budgets

This document tracks `ivanovo2beam` runtime budgets, engine optimizations, and
measured BeamNG.drive D3D12 benchmark results.

## Measurement methodology

Live engine benchmarks are driven reproducibly by
[`scripts/benchmark_level.py`](../scripts/benchmark_level.py) and the scaling QA
suite [`scripts/qa_scaling_experiment.py`](../scripts/qa_scaling_experiment.py)
(optionally routed through the localhost allowlisted
[`scripts/beamng_host_bridge.py`](../scripts/beamng_host_bridge.py) when running
from sandboxed terminals on Windows):

1. **Static & log audit** (no game launch): records on-disk `.ter`, mod `.zip`,
   `DecalRoad` node, `.forest4.json` item, `TSStatic`, and `.dae` counts, and
   parses `beamng.log` for stage load times, collision memory, and errors.
2. **Live D3D12 benchmark (`--run` / `--mode`)**: launches `BeamNG.drive.x64.exe`
   with `-enablemcp` (after wiping `temp/levels/<id>` and
   `temp/art/terrainMaterialCache` for cold-load runs), drives an `etk800`
   passenger vehicle via AI across the level, and samples:
   - Cold & warm load times (`worldReadyState == 1/2` and `beamng.log` total)
   - Frame pacing (`$fps::instantaneous` average, `$fps::p99` 1% low, CPU/GPU ms)
   - GPU & host memory (`nvidia-smi` VRAM, `WorkingSet`, `PrivateMemory`)
   - Static & physics collision budgets from `beamng.log`
   - High-speed passability, vertical velocity (`vel.z`), `damageSum`, `castRayStatic` surface accuracy, and `navgraph` continuity

## Key pipeline optimizations

| Subsystem | Optimization | Impact |
|---|---|---|
| Terrain materials (`beamng/materials.py`) | `DEFAULT_BASE_TEX_SIZE = 512` isotropic FBM base maps + 14 summer terrain PBR layers (sampled from `east_coast_usa` palette) | 4× lower base-map VRAM footprint without checkerboard macro-tiling |
| Vegetation (`vegetation/forest.py`, `vegetation/groundcover.py`) | Native `.forest4.json` instancing + 32 zero-copy `.link` stock summer species (`vegetation.max_trees`: `45k` Golden Sector / `120k` 33 km corridor) + 8 distance-dissolved `GroundCover` layers (`322k` max elements, strictly excluded from paved/bridge/river surfaces) | Dense multi-layer summer foliage and 3D grass with `0.44–1.32 s` forest load |
| Roads & props (`roads/build.py`, `roads/props.py`, `build.py`) | Tiered LOD, 800 m AABB splitting, `decalBias` stratification (`0.0015..0.005`), `_clip_ground_decal_roads_to_bounds`, `32 m` spatial-hashed `bus_stop`/`crossing` matching, and localized W-beam guardrails | Keeps `DecalRoad`s inside `TerrainBlock` bounds (`0` `checkBins` errors) and eliminates `O(N×M)` prop build stalls across `16k+` roads |
| Bridges (`roads/bridges.py`) | C1 Hermite coupled profiles (`DECK_STEP_M = 2.0`) with split Collada geometry (`_deck` + `_structure`) and flush portals | Zero bump at 150–173 km/h bridge crossings |
| Buildings (`buildings/export.py`, `config.py`) | Terrain-bounds filtering, optional `buildings.max_buildings` cap (`12,000` on 33 km corridor), and 500 m × 500 m spatial chunk batching with slope skirts | `1,426` buildings in `34` chunks (Golden Sector) / `11,945` in `797` chunks (33 km corridor) |
| Water (`water/export.py`) | Elevation-clamped river channels (`_reanchor_water_to_terrain` + transverse/longitudinal bank checks) + buffered oxbow `WaterBlock` tiling (`gridElementSize` `8..96 m`) | Eliminates floating rivers, cliff trenches, and angular pond corners |

## Measured D3D12 benchmarks

Hardware: **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM**,
**BeamNG.drive 0.40.6.0 D3D12**, `Overall: Ultra`, `Borderless`, DLSS.

> [!NOTE]
> Physical monitor `DISPLAY1` caps at **`1920×1080 @ 144 Hz`**. True
> **`2560×1440`** output is **`NOT_TESTED`** (`resolution_warning` recorded in
> benchmark JSONs).

### Golden Sector V2 (`ivanovo_golden_sector`) vs P0, P1, P2 Corridor V2 & Baselines

| Metric | V2 Regression (`golden_sector_v2_regression.json`) | P0 Single `8192@2m` (`p0_single_8192_2m.json`) | P1 Hybrid `4m+1m` (`p1_hybrid_corridor.json`) | **P2 Corridor V2 `8192@4m` (`p2_ivanovo_shuya_v2.json`)** | P2 Dual-Block (`ivanovo_dual_terrain_exp`) | `mapng_golden_sector` baseline |
|---|---|---|---|---|---|---|
| Cache state | Warm | Warm (`37.08 s` cold) | Warm (`52.98 s` cold) | **Cold** | Cold | Warm / Cold |
| Load time (`world_ready_s`) | `24.30 s` (`27.89 s` cold) | `27.72 s` (`37.08 s` cold) | `33.05 s` (`52.98 s` cold) | **`78.35 s`** (`78.49 s` wall) | `147.21 s` | `37.7 s` / `75.6 s` |
| Average FPS (`$fps::instantaneous`) | **`143.17 FPS`** | **`105.50 FPS`** | `97.82 FPS` | **`80.32 FPS`** | `175.96 FPS` | `47.27 FPS` |
| 1% low FPS (`$fps::p99`) | **`111.07 FPS`** | **`89.64 FPS`** | `78.66 FPS` | **`48.68 FPS`** | `140.82 FPS` | `35.58 FPS` |
| Frametime (avg / 1% low) | `6.98 ms` / `9.00 ms` (`cpu 5.96`, `gpu 6.65 ms`) | `9.48 ms` / `11.16 ms` (`cpu 9.41`, `gpu 5.45 ms`) | `10.22 ms` / `12.71 ms` (`cpu 10.26`, `gpu 5.77 ms`) | `12.45 ms` / `20.54 ms` (`cpu 16.18`, `gpu 26.54 ms`) | `5.68 ms` / `7.10 ms` | — |
| GPU VRAM peak (`nvidia-smi`) | **`4,575.0 MiB`** (`4,533.5` avg) | **`4,930.0 MiB`** (`4,923.0` avg) | `5,021.0 MiB` (`5,162` cold) | **`6,333.0 MiB`** (`6,236.6` avg) | `4,861.0 MiB` | `8,893 MiB` |
| Process RAM (`WorkingSet` / `Private`) | `3,957.4` / `7,076.6 MiB` | `5,562.6` / `9,209.4 MiB` | `4,888.7` / `8,409.5 MiB` | `8,093.3` / `12,737.7 MiB` | `94.1` / `72.3 MiB` (subset) | — |
| Static / Physics collision | `63.56 MB` / `53.32 MB` | `160.54 MB` / `134.66 MB` | `224.53 MB` / `189.13 MB` | `735.64 MB` / `618.46 MB` | `53.36 MB` / `44.42 MB` | — |
| Live P152 `etk800` passability | `2.22 km` (`178.63 km/h` max), `damageSum = 0` | `6.37 km` (`98.04 km/h` max), `damageSum = 0` | `4.56 km`, `damage = 6,416` (Leg 1) / `0` (Leg 2) | `8.69 km` (`104.59 km/h` max), `damageSum = 0` on Legs 1 & 3; `25.23 km` navgraph connected | Stops at `x=8350` seam (`navgraph` `z=0` / missing raycast) | — |
| `2560×1440` physical display | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | — |

### Key Takeaways (Scaling Study P0 vs P1 vs P2)

- **P0 Single `8192 × 8192` vs P1 Hybrid (`16.38 × 16.38 km`):** P0 beat P1 in avg FPS (`+7.9%`), 1% low FPS (`+14.0%`), load time (`27.72 s` vs `33.05 s` warm), VRAM (`4,930` vs `5,021–5,162 MiB`), collision memory (`-40%` tris), drive safety (`damageSum = 0` vs `6,416`), native `GroundCover`, and smooth 5-channel `TerrainMaterial` blending without baked checkerboard/sawtooth `.dae` shoulder seams. See [`docs/p3_scaling_study.md`](p3_scaling_study.md).
- **P2 33 km Corridor (`ivanovo_shuya_v2` at `8192 × 8192 @ 4.0 m`):** Scales smoothly across `1,073.5 km²` (`103,697` objects, `69,000` `DecalRoad`s, `120,000` `.link` trees, `11,945` buildings, `82` bridges) at **`80.32 FPS` avg** and **`6,333 MiB` VRAM** (within 10 GB budget), with `0` `SceneContainer::checkBins`, `GroundCover`, `ForestData`, or `WarningMaterial` errors.
- **P2 Multi-`TerrainBlock` (`ivanovo_dual_terrain_exp`):** Two `2048×2048` blocks inflated load initialization to `147.21 s` and hit hard Torque3D single-block raycast/physics/`GroundCover`/`DecalRoad` binding limits. See [`docs/p2_multiblock_experiment.md`](p2_multiblock_experiment.md).

## Level size budgets (on-disk specs)

| Config | Terrain grid | Ground spacing | World extent | `.ter` file size | Mod `.zip` |
|---|---|---|---|---|---|
| `configs/golden_sector.yaml` (`ivanovo_golden_sector`) | `4096 × 4096` | `1.0 m` | `4.095 × 4.095 km` | `48.00 MiB` (`50,331,771 B`) | `32.66 MiB` (`34,245,409 B`) |
| `configs/experiment_dual_terrain.yaml` (`ivanovo_dual_terrain_exp`) | `2 × 2048 × 2048` | `1.0 m` | `4.094 × 2.047 km` | `2 × 12.0 MiB` | — |
| `configs/experiment_single_8192_2m.yaml` (`ivanovo_single_8192_2m`) | `8192 × 8192` | `2.0 m` | `16.382 × 16.382 km` | `192.00 MiB` (`201,326,715 B`) | — |
| `configs/experiment_hybrid_corridor.yaml` (`ivanovo_hybrid_corridor`) | `4096 × 4096` + `134` DAEs | `4.0 m` (`1.0 m` RTIN) | `16.380 × 16.380 km` | `48.00 MiB` (+`155.10 MiB` DAE) | — |
| `configs/ivanovo_shuya.yaml` (`ivanovo_shuya`) | `4096 × 4096` | `8.0 m` | `32.760 × 32.760 km` | `48.00 MiB` | — |
| `configs/ivanovo_shuya_v2.yaml` (`ivanovo_shuya_v2`) | `8192 × 8192` | `4.0 m` | `32.764 × 32.764 km` | `192.00 MiB` (`201,326,715 B`) | `159.52 MiB` (`167,267,461 B`) |
