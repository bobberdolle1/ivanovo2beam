# Project Status — Golden Sector V2 & 33 km Corridor (`ivanovo_shuya_v2`) Sign-Off

**Date:** 2026-11-26  
**Baseline:** `ivanovo_golden_sector` (`configs/golden_sector.yaml` — Kokhma SE / Р152 `24К-260` Uvod River Crossing, `4096×4096 @ 1.0 m`)  
**Corridor V2:** `ivanovo_shuya_v2` (`configs/ivanovo_shuya_v2.yaml` — Ivanovo–Kokhma–Shuya Р152, `8192×8192 @ 4.0 m = 32.764 × 32.764 km`)  
**Scaling Experiments:** `ivanovo_dual_terrain_exp` (P2 Dual-Block), `ivanovo_single_8192_2m` (P0), `ivanovo_hybrid_corridor` (P1)  
**Status:** **Verified on `1920×1080 Borderless, Overall: Ultra` (D3D12 + DLSS)** (`2560×1440`: **`NOT_TESTED`** due to `1920×1080` physical monitor cap)

---

## Verification & Quality Gates

| Check | Result | Notes |
|---|---|---|
| Offline test suite (`pytest tests/ -m "not network"`) | **All passed (exit 0)** | Includes P0/P1/P2/corridor config, bounds-clipping, and offline build tests (3 live-network tests excluded) |
| Golden Sector V2 (`ivanovo_golden_sector`) validate & live D3D12 | **9/9 passed (`0` warnings)** | `143.17 FPS` avg / `111.07 FPS` 1% low (`4,575 MiB` VRAM), `damageSum = 0` up to `178.63 km/h` |
| P0 Single Terrain (`ivanovo_single_8192_2m`, `8192@2m`) | **9/9 passed, live verified** | `105.50 FPS` avg / `89.64 FPS` 1% low (`4,930 MiB` VRAM), `6.37 km` `etk800` drive with `damageSum = 0` |
| P1 Hybrid (`ivanovo_hybrid_corridor`, `4096@4m + 1m DAE`) | **14/14 passed, live verified** | `97.82 FPS` avg / `78.66 FPS` 1% low (`5,021–5,162 MiB` VRAM); `overObjects=true` works on `Visible Mesh`, but loses `GroundCover` and shows baked checkerboard/sawtooth seams |
| P2 Dual-`TerrainBlock` (`ivanovo_dual_terrain_exp`) | **Empirically engine-limited** | `0.000 m` seam ΔZ offline; live BeamNG 0.40 binds `castRayStatic`, `core_terrain`, `GroundCover`, and `DecalRoad`s to block 0 ([`p2_multiblock_experiment.md`](p2_multiblock_experiment.md)) |
| **P2 33 km Corridor (`ivanovo_shuya_v2`, `8192@4m`)** | **9/9 passed, live verified** | `80.32 FPS` avg / `48.68 FPS` 1% low (`6,333 MiB` VRAM, `78.35 s` cold load); `25.229 km` unbroken Ivanovo→Shuya `navgraph` (`486` nodes, `0` `z=0`); `0` `GroundCover`/`ForestData`/`TerrainBlock`/`WarningMaterial`/`checkBins` errors |

---

## Live Benchmark Comparison (V2, P0, P1, P2 Corridor V2 vs MapNG Baseline)

Measured on **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM**,
**BeamNG.drive 0.40.6.0 D3D12**, `1920×1080 Borderless, Overall: Ultra`, DLSS:

| Metric | Golden Sector V2 Warm (`golden_sector_v2_regression.json`) | P0 Single `8192@2m` Warm (`p0_single_8192_2m.json`) | P1 Hybrid `4m+1m` Warm (`p1_hybrid_corridor.json`) | **P2 Corridor V2 `8192@4m` Cold (`p2_ivanovo_shuya_v2.json`)** | `mapng_golden_sector` (`a5ed648`) |
|---|---|---|---|---|---|
| World extent | `4.095 × 4.095 km` | `16.382 × 16.382 km` | `16.380 × 16.380 km` | **`32.764 × 32.764 km`** | `4.096 × 4.096 km` |
| Load time (`world_ready_s`) | `24.30 s` (`27.89 s` cold) | `27.72 s` (`37.08 s` cold) | `33.05 s` (`52.98 s` cold) | **`78.35 s`** (`78.49 s` wall) | `37.7 s` warm / `75.6 s` cold |
| Average FPS (`$fps::instantaneous`) | **`143.17 FPS`** | **`105.50 FPS`** | `97.82 FPS` | **`80.32 FPS`** | `47.27 FPS` |
| 1% low FPS (`$fps::p99`) | **`111.07 FPS`** | **`89.64 FPS`** | `78.66 FPS` | **`48.68 FPS`** | `35.58 FPS` |
| Min sampled FPS | `104.97 FPS` | `88.27 FPS` | `68.96 FPS` | `36.48 FPS` | — |
| GPU VRAM peak (`nvidia-smi`) | **`4,575.0 MiB`** (`4,729` cold) | **`4,930.0 MiB`** | `5,021.0 MiB` (`5,162` cold) | **`6,333.0 MiB`** (`6,236.6` avg) | `8,893 MiB` |
| Process RAM (`WorkingSet` / `Private`) | `3,957.4` / `7,076.6 MiB` | `5,562.6` / `9,209.4 MiB` | `4,888.7` / `8,409.5 MiB` | `8,093.3` / `12,737.7 MiB` | — |
| Static / Physics collision | `63.56 MB` / `53.32 MB` | `160.54 MB` / `134.66 MB` | `224.53 MB` / `189.13 MB` | `735.64 MB` / `618.46 MB` | — |
| Content summary | `1,733` decals, `45k` trees, `8` GC, `1,426` bld (`34` chunks), `604` props | `11,629` decals, `45k` trees, `8` GC, `8,601` bld, `4,158` props | `11,627` decals + `134` corridor DAEs, `45k` trees, `5,684` bld | **`69,000`** decals, **`120k`** trees (`32` spp), `8` GC, **`11,945`** bld, **`82`** bridges, **`23,711`** props | Static baked meshes (`groundAsModels`) |
| Mod ZIP / `.ter` size | `32.66 MiB` / `48.00 MiB` | — / `192.00 MiB` | — / `48.00 MiB` (+`155.1` DAE) | **`159.52 MiB`** / `192.00 MiB` | — |
| `2560×1440` physical display | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | — |

---

## Reproduction Commands

```powershell
# 1. Build, validate, and package Golden Sector V2 (4 km regression baseline)
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml build
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml validate --beamng "F:\SteamLibrary\steamapps\common\BeamNG.drive"
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml package

# 2. Build, validate, and package P2 33 km Corridor V2 (ivanovo_shuya_v2)
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/ivanovo_shuya_v2.yaml build
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/ivanovo_shuya_v2.yaml validate --beamng "F:\SteamLibrary\steamapps\common\BeamNG.drive"

# 3. Optional Windows host bridge (when running from sandboxed shells / CI)
# Run once in desktop PowerShell (Medium IL):
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/beamng_host_bridge.py serve

# 4. Install & run repeatable scaling QA suite via bridge (or directly at Medium IL)
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/beamng_host_bridge.py install --level ivanovo_shuya_v2
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/qa_scaling_experiment.py --level ivanovo_shuya_v2 --mode p2 --out docs/benchmarks/p2_ivanovo_shuya_v2.json
```

---

## Next Phase

1. **Internal `DecalRoad` `2048 m` batch packing cleanup**: Inspect the remaining `38` internal `SceneStaticDecalRoadManager` `2048 m` / `inf` overflow warnings in `ivanovo_shuya_v2` (and `2` in `golden_sector`).
2. **Urban `4 m` narrow-street grass trim**: Widen or smooth `TerrainMaterial` / grading rolloff on narrow un-graded city side streets when `square_size_m >= 4.0`.
3. **1440p verification**: Re-run `scripts/qa_scaling_experiment.py` on a native `2560×1440` display to upgrade `2560×1440` from `NOT_TESTED`.
4. **M5/M6 Whole-Oblast sector network**: Chain independent `8192 × 8192` single-`TerrainBlock` levels across the `220 × 145 km` Oblast using the shared Transverse Mercator projection (per [`docs/p3_scaling_study.md`](p3_scaling_study.md)).
