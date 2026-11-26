# Project Status — Golden Sector V2 Sign-Off

**Date:** 2026-11-26  
**Target:** `ivanovo_golden_sector` (`configs/golden_sector.yaml` — Kokhma SE / Р152 `24К-260` Uvod River Crossing)  
**Experiment:** `ivanovo_dual_terrain_exp` (`configs/experiment_dual_terrain.yaml` — P2 adjacent dual-`TerrainBlock` scaling test)  
**Status:** **Verified on `1920×1080 Borderless, Overall: Ultra` (D3D12 + DLSS)** (`2560×1440`: **`NOT_TESTED`** due to `1920×1080` physical monitor cap)

---

## Verification & Quality Gates

| Check | Result | Notes |
|---|---|---|
| Offline test suite (`pytest tests/ -m "not network"`) | **777 passed, 3 deselected** | Live-network Overpass/DEM tests excluded |
| Level validator (`ivanovo2beam validate --beamng`) | **9 passed, 0 failed, 0 warnings** | `2,818` objects in `12` files / `11` groups, `45` local `.dae` meshes, `109` extra material entries, `350` referenced textures, `0` missing |
| Live D3D12 `beamng.log` level scan | **0 level/material/forest/GroundCover errors** | `0` `GroundCover`, `ForestData`, `WaterBlock`, `buildings`, `road_props`, `bridge`, or `WarningMaterial` errors (`ActionMap`/CEF/`d3d12ma` exit noise is stock game; one off-terrain western stub `way274102983` AABB warning only) |
| High-speed both-ways Р152 drive (`qa_v2_live.py`, `etk800`) | **Pass (`damageSum = 0`)** | Eastbound `max = 176.19 km/h` (`avg = 103.54`, `bridge = 173.04 km/h`); Westbound `max = 178.60 km/h` (`avg = 77.72`, `bridge = 151.48 km/h`); `damageSum = 0` both ways |
| P2 Dual-`TerrainBlock` experiment (`ivanovo_dual_terrain_exp`) | **Empirically tested live** | `0.000 m` seam ΔZ & `0` layer mismatches offline; live BeamNG 0.40 engine proved single-`TerrainBlock` binding for `castRayStatic`, `core_terrain`, `GroundCover`, and `DecalRoad` across seams (see [`docs/p2_multiblock_experiment.md`](p2_multiblock_experiment.md)) |

---

## Live Benchmark Comparison (V2 vs V1 vs MapNG Baseline)

Measured on **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM**,
**BeamNG.drive 0.40.6.0 D3D12**, `1920×1080 Borderless, Overall: Ultra`, DLSS:

| Metric | Golden Sector V2 Cold (`v2_cold_benchmark.json`) | Golden Sector V2 Warm (`v2_warm_benchmark.json`) | Golden Sector V1 Warm (DLSS) | `mapng_golden_sector` (`a5ed648`) |
|---|---|---|---|---|
| Load time (`worldReadyState` / log) | `27.89 s` (`35.77 s` wall) | `21.82 s` (`35.79 s` wall) | `21.60 s` (`26.59 s` cold) | `37.7 s` warm / `75.6 s` cold |
| Average FPS (`$fps::instantaneous`) | **`133.39 FPS`** | **`134.39 FPS`** | `133.09 FPS` | `47.27 FPS` |
| 1% low FPS (`$fps::p99`) | **`106.03 FPS`** | **`108.70 FPS`** | `114.42 FPS` | `35.58 FPS` |
| Min sampled FPS | `105.48 FPS` | `106.48 FPS` | — | — |
| GPU VRAM peak (`nvidia-smi`) | **`4,729.0 MiB`** | **`4,666.0 MiB`** | `3,965.0 MiB` | `8,893 MiB` |
| Process RAM (`WorkingSet` / `Private`) | `4,057.1 MiB` / `7,285.1 MiB` | `3,971.0 MiB` / `7,127.6 MiB` | `3,809.3 MiB` / `6,658.6 MiB` | — |
| Static / Physics collision | `63.56 MB` / `53.32 MB` | `63.56 MB` / `53.32 MB` | `31.73 MB` / `26.38 MB` | — |
| Visual content | 14 terrain PBR + 8 `GroundCover` (`322k` max) + 30 forest species (`45k`) + `1,426` buildings (`34` chunks) + `604` roadside props | same | Roads, 2 bridges, 26 forest species (no `GroundCover`, buildings, or roadside props) | Static baked meshes (`groundAsModels`) |
| Mod ZIP size | **`32.66 MiB`** (`34,245,409 B`) | **`32.66 MiB`** | `30.86 MiB` | — |
| `2560×1440` physical display | **`NOT_TESTED`** (monitor cap `1920×1080`) | **`NOT_TESTED`** | **`NOT_TESTED`** | — |

---

## Reproduction Commands

```powershell
# 1. Build, validate, and package Golden Sector V2
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml build
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml validate --beamng "F:\SteamLibrary\steamapps\common\BeamNG.drive"
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml package

# 2. Optional Windows host bridge (when running from sandboxed shells / CI)
# Run once in desktop PowerShell (Medium IL):
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/beamng_host_bridge.py serve

# 3. Install & run repeatable static + live MCP benchmark via bridge (or directly at Medium IL)
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/beamng_host_bridge.py install --level ivanovo_golden_sector
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/benchmark_level.py --level ivanovo_golden_sector --run --vehicle etk800

# 4. Build & validate isolated P2 multi-TerrainBlock experiment
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts/experiment_dual_terrain.py
```

---

## Next Phase

1. **Corridor / Regional scaling without broken multi-`TerrainBlock` seams**: Use `8192×8192` single-`TerrainBlock` (`2–4 m`) or a **hybrid architecture** (single anchor `TerrainBlock` + streamed RTIN Collada ground chunks + `.forest4.json` undergrowth), per [`docs/p2_multiblock_experiment.md`](p2_multiblock_experiment.md).
2. **Ivanovo → Shuya (`configs/ivanovo_shuya.yaml`) V2 uplift**: Propagate V2 materials, hydrology clamping, `GroundCover`, roadside props, and regional buildings to the full intercity corridor.
3. **1440p verification**: Re-run `scripts/benchmark_level.py --level ivanovo_golden_sector --run` on a native `2560×1440` display to upgrade `2560×1440` from `NOT_TESTED`.
