# Performance & Runtime Budgets

This document tracks `ivanovo2beam` runtime budgets, engine optimizations, and
measured BeamNG.drive D3D12 benchmark results.

## Measurement methodology

Live engine benchmarks are driven reproducibly by
[`scripts/benchmark_level.py`](../scripts/benchmark_level.py) (optionally routed
through the localhost allowlisted [`scripts/beamng_host_bridge.py`](../scripts/beamng_host_bridge.py)
when running from sandboxed terminals on Windows):

1. **Static & log audit** (no game launch): records on-disk `.ter`, mod `.zip`,
   `DecalRoad` node, `.forest4.json` item, `TSStatic`, and `.dae` counts, and
   parses `beamng.log` for stage load times, collision memory, and errors.
2. **Live D3D12 benchmark (`--run`)**: launches `BeamNG.drive.x64.exe` with
   `-enablemcp` (after wiping `temp/levels/<id>` and
   `temp/art/terrainMaterialCache` for cold-load runs), drives an `etk800`
   passenger vehicle via AI across the level, and samples:
   - Cold & warm load times (`worldReadyState == 1/2` and `beamng.log` total)
   - Frame pacing (`$fps::instantaneous` average, `$fps::p99` 1% low, CPU/GPU ms)
   - GPU & host memory (`nvidia-smi` VRAM, `WorkingSet`, `PrivateMemory`)
   - Static & physics collision budgets from `beamng.log`
   - High-speed 80–178 km/h passability, vertical velocity (`vel.z`), and `damageSum`

## Key pipeline optimizations

| Subsystem | Optimization | Impact |
|---|---|---|
| Terrain materials (`beamng/materials.py`) | `DEFAULT_BASE_TEX_SIZE = 512` isotropic FBM base maps + 14 summer terrain PBR layers (sampled from `east_coast_usa` palette) | 4× lower base-map VRAM footprint without checkerboard macro-tiling |
| Vegetation (`vegetation/forest.py`, `vegetation/groundcover.py`) | Native `.forest4.json` instancing + 30 zero-copy `.link` stock summer species (`200 / 125 / 50` stems/ha zones) + 8 distance-dissolved `GroundCover` layers (`322k` max elements, strictly excluded from paved/bridge/river surfaces) | Dense multi-layer summer foliage and 3D grass with minimal VRAM/draw-call cost |
| Roads & props (`roads/build.py`, `roads/props.py`) | Tiered LOD (`distance_fade`, `break_angle`, `detail = 0.5`), 800 m AABB splitting, `decalBias` stratification (`0.0015..0.0025`), spatial-indexed roadside prop placement (`604` GOST signs, poles, bus stops, localized W-beam guardrails) | Keeps `DecalRoad` batches crisp and inside BeamNG's 2048 m packing limit |
| Bridges (`roads/bridges.py`) | C1 Hermite coupled profiles (`DECK_STEP_M = 2.0`) with split Collada geometry (`_deck` + `_structure`) and flush portals | Zero bump at 150–173 km/h bridge crossings |
| Buildings (`buildings/export.py`) | Terrain-bounds filtering + 500 m × 500 m spatial chunk batching with slope skirts and `Visible Mesh Final` collision | `1,426` buildings collapsed into `34` `TSStatic` draw chunks (`0` floating border buildings) |
| Water (`water/export.py`) | Elevation-clamped river channels (`_reanchor_water_to_terrain` + transverse/longitudinal bank checks) + buffered oxbow `WaterBlock` tiling (`gridElementSize` `8..96 m`) | Eliminates floating rivers, cliff trenches, and angular pond corners |

## Measured D3D12 benchmarks

Hardware: **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM**,
**BeamNG.drive 0.40.6.0 D3D12**, `Overall: Ultra`, `Borderless`.

> [!NOTE]
> Physical monitor `DISPLAY1` caps at **`1920×1080 @ 144 Hz`**. True
> **`2560×1440`** output is **`NOT_TESTED`** (`resolution_warning` recorded in
> benchmark JSONs).

### Golden Sector V2 (`ivanovo_golden_sector`) vs V1 & P2 Experiment

| Metric | V2 Cold (`v2_cold_benchmark.json`) | V2 Warm (`v2_warm_benchmark.json`) | V1 Warm (DLSS) | P2 Dual-Block (`ivanovo_dual_terrain_exp`) | `mapng_golden_sector` baseline |
|---|---|---|---|---|---|
| Load time (`world_ready_s`) | `27.89 s` (`35.77 s` wall) | `21.82 s` (`35.79 s` wall) | `21.60 s` (`26.59 s` cold) | `147.21 s` | `37.7 s` warm / `75.6 s` cold |
| Average FPS (`$fps::instantaneous`) | **`133.39 FPS`** | **`134.39 FPS`** | `133.09 FPS` | `175.96 FPS` | `47.27 FPS` |
| 1% low FPS (`$fps::p99`) | **`106.03 FPS`** | **`108.70 FPS`** | `114.42 FPS` | `140.82 FPS` | `35.58 FPS` |
| Frametime (avg / 1% low) | `7.50 ms` / `9.43 ms` (`cpu 6.91`, `gpu 6.38 ms`) | `7.44 ms` / `9.20 ms` (`cpu 6.88`, `gpu 6.30 ms`) | `7.51 ms` / `8.74 ms` | `5.68 ms` / `7.10 ms` | — |
| GPU VRAM peak (`nvidia-smi`) | **`4,729.0 MiB`** (`4,671.0` avg) | **`4,666.0 MiB`** (`4,602.5` avg) | `3,965.0 MiB` | `4,861.0 MiB` (`4,860.4` avg) | `8,893 MiB` |
| Process RAM (`WorkingSet` / `Private`) | `4,057.1 MiB` / `7,285.1 MiB` | `3,971.0 MiB` / `7,127.6 MiB` | `3,809.3 MiB` / `6,658.6 MiB` | `94.1` / `72.3 MiB` (subset) | — |
| Static / Physics collision | `63.56 MB` (`1.15M` verts) / `53.32 MB` (`49.9k` inst) | `63.56 MB` / `53.32 MB` | `31.73 MB` / `26.38 MB` | `53.36 MB` / `44.42 MB` | — |
| Live P152 high-speed `etk800` passability | — | Eastbound `176.19 km/h` (`avg 103.54`), Westbound `178.60 km/h` (`avg 77.72`), `damageSum = 0` | `54.2 km/h`, `damage = 0` | Stops at `x=8350` seam (`navgraph` `z=0` / missing raycast) | — |
| `2560×1440` physical display | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | — |

### Key Takeaways (V1 → V2)

- Adding **8 native `GroundCover` layers (`322k` elements)**, **1,426 chunked buildings (`34` `.dae`s)**, and **604 roadside props** increased peak VRAM by only **~700 MiB** (`4.67–4.73 GiB` total vs 10 GB budget) while keeping warm average FPS slightly **above V1** (`134.39` vs `133.09 FPS`) and warm load time flat (`21.82 s` vs `21.60 s`).
- In the P2 multi-`TerrainBlock` test, loading two `2048×2048` blocks inflated load initialization to `147.21 s` and hit hard Torque3D engine limitations (single-block raycast/physics/GroundCover binding and broken cross-block `DecalRoad` projection). See [`docs/p2_multiblock_experiment.md`](p2_multiblock_experiment.md).

## Level size budgets (on-disk specs)

| Config | Terrain grid | Ground spacing | World extent | `.ter` file size | Mod `.zip` |
|---|---|---|---|---|---|
| `configs/golden_sector.yaml` (`ivanovo_golden_sector`) | `4096 × 4096` | `1.0 m` | `4.095 × 4.095 km` | `48.00 MiB` (`50,331,771 B`) | `32.66 MiB` (`34,245,409 B`) |
| `configs/experiment_dual_terrain.yaml` (`ivanovo_dual_terrain_exp`) | `2 × 2048 × 2048` | `1.0 m` | `4.094 × 2.047 km` | `2 × 12.0 MiB` | — |
| `configs/ivanovo_shuya.yaml` (`ivanovo_shuya`) | `4096 × 4096` | `8.0 m` | `32.760 × 32.760 km` | `48.00 MiB` | — |
