# Project Status — Golden Sector Sign-Off

**Date:** 2026-11-26
**Target:** `ivanovo_golden_sector` (`configs/golden_sector.yaml` — Kokhma SE / Р152 `24К-260` Uvod River Crossing)
**Status:** **Verified on `1920×1080 Borderless, Overall: Ultra`** (`2560×1440`: **`NOT_TESTED`** due to `1920×1080` physical monitor cap)

---

## Verification & Quality Gates

| Check | Result | Notes |
|---|---|---|
| Lint (`uvx ruff check src tests`) | **Pass** | Zero lint errors |
| Offline test suite (`pytest tests/ -m "not network"`) | **741 passed, 3 deselected** | Live-network Overpass/DEM tests excluded |
| Level validator (`ivanovo2beam validate --beamng`) | **9 passed, 0 failed, 0 warnings** | 1,821 objects, 4 local `.dae` meshes, 49 extra material entries, 274 textures, 0 missing |
| Live D3D12 `beamng.log` error scan | **0 level errors** | `0` `SceneStaticDecalRoadManager`, `MaterialList`, `TerrainCellMaterial`, `ForestData`, `WaterBlock`, or collision errors |
| Both-ways `way/274102985` bridge drive test | **Pass** | AI drove East & West across bridge and junction up to `54.2 km/h`; `damage_max = 0`, `bridge_vz_abs_max = 0.65 m/s` |

---

## Live Benchmark Comparison (`ivanovo_golden_sector` vs `mapng_golden_sector`)

Measured on **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM**,
**BeamNG.drive 0.40.6.0 D3D12**, `1920×1080 Borderless, Overall: Ultra`
(baseline `mapng_golden_sector` built from MapNG `a5ed648`, MIT License, Copyright (c) 2026 Nikki Luzader; MIT attributions preserved in all adapted modules under `src/ivanovo2beam/` and `docs/`):

| Metric | `ivanovo_golden_sector` (DLSS) | `ivanovo_golden_sector` (`render_scale=1.0`) | `mapng_golden_sector` (`a5ed648`) |
|---|---|---|---|
| Cold load (all 3 caches wiped) | `26.59 s` (`worldReadyState=1`, `27.1 s` MCP) | — | `75.6 s` / `122.2 s` |
| Warm load (`worldReadyState=1`) | `21.60 s` (`22.43 s` wall-clock) | `21.50 s` (`22.18 s` wall-clock) | `37.7 s` / `38.9 s` |
| Average FPS (`$fps::instantaneous`) | **`133.09 FPS`** | **`139.25 FPS`** | `47.27 FPS` |
| 1% low FPS (`$fps::p99`) | **`114.42 FPS`** | **`118.44 FPS`** | `35.58 FPS` |
| GPU VRAM peak (`nvidia-smi`) | **`3,965.0 MiB`** | **`3,999.0 MiB`** | `8,893 MiB` |
| Process RAM (`WorkingSet` / `Private`) | `3,809.3 MiB` / `6,658.6 MiB` | `3,825.4 MiB` / `6,670.7 MiB` | — |
| Mod ZIP size | **`30.86 MiB`** (`32,358,030 B`) | **`30.86 MiB`** | — |
| `2560×1440` physical display | **`NOT_TESTED`** (monitor cap `1920×1080`) | **`NOT_TESTED`** | — |

---

## Reproduction Commands

```bash
# 1. Build and package Golden Sector
python -m ivanovo2beam -c configs/golden_sector.yaml --json build

# 2. Validate structure, .ter/.terrain.json parity, and game asset paths
python -m ivanovo2beam -c configs/golden_sector.yaml validate --beamng "F:\SteamLibrary\steamapps\common\BeamNG.drive"

# 3. Install into BeamNG user data directory
python -m ivanovo2beam -c configs/golden_sector.yaml install --beamng "F:\Games\BeamNG.drive\current" --force

# 4. Run repeatable static + live MCP benchmark
python scripts/benchmark_level.py --level ivanovo_golden_sector --run
```

---

## Next Phase

1. **Corridor scaling (`configs/ivanovo_shuya.yaml`)**: Apply the Golden Sector
   visual, hydrology, coupled-bridge, and summer `.forest4.json` pipeline across
   the `4096 × 4096 @ 8.0 m` (`32.760 × 32.760 km`) Ivanovo → Shuya Р152
   corridor with bounded `LevelInfo.visibleDistance` (`10–12 km`), roadside-weighted
   forest allocation (`40–60k` stems), and `4 km` sector chunking for bridge/rail
   meshes.
2. **Whole-oblast tiling (`configs/ivanovo_oblast.yaml`)**: Extend multi-`TerrainBlock`
   sector streaming to cover the full `220 × 145 km` Ivanovo Oblast target
   (Milestones M5/M6).
3. **1440p verification**: Re-run `scripts/benchmark_level.py --level ivanovo_golden_sector --run`
   on a `2560×1440` display to upgrade `2560×1440` from `NOT_TESTED`.
