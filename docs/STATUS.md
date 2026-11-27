# Project Status — Golden Sector V2 & Stabilized 33 km Corridor (`ivanovo_shuya_v2`)

**Date:** 2026-11-27  
**Baseline:** `ivanovo_golden_sector` (`configs/golden_sector.yaml` — Kokhma SE / Р152 `24К-260` Uvod River Crossing, `4096×4096 @ 1.0 m`)  
**Corridor V2:** `ivanovo_shuya_v2` (`configs/ivanovo_shuya_v2.yaml` — Ivanovo–Kokhma–Shuya Р152, `8192×8192 @ 4.0 m = 32.764 × 32.764 km`)  
**Scaling Experiments:** `ivanovo_dual_terrain_exp` (P2 Dual-Block), `ivanovo_single_8192_2m` (P0), `ivanovo_hybrid_corridor` (P1)  
**Status:** **Stabilized & Verified on `1920×1080 Borderless, Overall: Ultra` (D3D12 + DLSS)** (`2560×1440`: **`NOT_TESTED`** due to `1920×1080` physical monitor cap)

---

## Verification & Quality Gates

| Check | Result | Notes |
|---|---|---|
| Offline test suite (`pytest tests/ -m "not network"`) | **All passed (exit 0)** | Covers projection, `.ter`, roads, bridges, props (`Colmesh-1`), buildings, water, terrain/vegetation, level metadata/previews, cleanup, and offline build |
| Golden Sector V2 (`ivanovo_golden_sector`, `7985014`) | **Verified (`0` level errors)** | `142.88 FPS` avg / `105.77 FPS` 1% low (`4,700 MiB` VRAM, `35.81 s` wall / `29.28 s` `world_ready_s`), `0` `Bounds` / `DecalRoad` errors, `1.986 km` @ `95.5 km/h` `damageSum = 0` |
| P0 Single Terrain (`ivanovo_single_8192_2m`, `8192@2m`) | **9/9 passed, live verified** | `105.50 FPS` avg / `89.64 FPS` 1% low (`4,930 MiB` VRAM), `6.37 km` `etk800` drive with `damageSum = 0` |
| P1 Hybrid (`ivanovo_hybrid_corridor`, `4096@4m + 1m DAE`) | **14/14 passed, live verified** | `97.82 FPS` avg / `78.66 FPS` 1% low (`5,021–5,162 MiB` VRAM); `overObjects=true` works on `Visible Mesh`, but loses `GroundCover` and shows baked checkerboard/sawtooth seams |
| P2 Dual-`TerrainBlock` (`ivanovo_dual_terrain_exp`) | **Empirically engine-limited** | `0.000 m` seam ΔZ offline; live BeamNG 0.40 binds `castRayStatic`, `core_terrain`, `GroundCover`, and `DecalRoad`s to block 0 ([`p2_multiblock_experiment.md`](p2_multiblock_experiment.md)) |
| **Stabilized P2 33 km Corridor (`ivanovo_shuya_v2`, `7985014`)** | **Verified (`5/5` continuous round-trip)** | **`99.72–111.16 FPS`** city / **`106.53 FPS`** rural / **`160.0–166.4 FPS`** 720° spin (`0` hitches `> 33 ms`), **`59.646 km`** unbroken Ivanovo–Kokhma–Kitovo–Shuya–Ivanovo drive (`5/5` `reached_target = true`, `113.19 FPS` avg), `-77.3%` static collision (`166.66 MB`), `0` `Bounds` errors |

---

## Corridor Stabilization Before (`9f0fc11`) → Final After (`7985014`)

Measured on **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM**, **BeamNG.drive 0.40.6.0 D3D12**, `1920×1080 Borderless, Overall: Ultra`, DLSS (cold cache):

| Metric | Before (`9f0fc11`, `stabilization_before.json`) | Final After (`7985014`, `stabilization_after.json`) | Delta |
|---|---|---|---|
| Cold load (`wall` / `world_ready_s`) | `91.38 s` / `91.33 s` | **`64.30 s` / `64.18 s`** | **`-29.6%`** |
| **Urban Ivanovo** (avg / 1% low / min) | `45.65 / 36.61 / 15.47 FPS` | **`99.72 / 80.04 / 59.71 FPS`** | **`+118.4%`** avg |
| **Urban Shuya** (avg / 1% low / min) | `47.73 / 38.82 / 5.50 FPS` | **`111.16 / 88.18 / 33.46 FPS`** | **`+132.9%`** avg |
| **Rural Р152** (avg / 1% low / min) | `50.99 / 39.13 / 34.22 FPS` | **`106.53 / 87.62 / 68.70 FPS`** | **`+108.9%`** avg |
| **720° Camera-Spin (Urban Ivanovo)** | `69.59 FPS` avg / `47.16` 1% low (`21.35 ms` max) | **`166.40 FPS`** avg / **`115.65`** 1% low (`8.73 ms` max, `0` hitches `> 33 ms`) | **`+139.1%`** |
| **720° Camera-Spin (Forest Corridor)** | `63.51 FPS` avg / `41.53` 1% low (`24.27 ms` max) | **`159.99 FPS`** avg / **`119.14`** 1% low (`8.52 ms` max, `0` hitches `> 33 ms`) | **`+151.9%`** |
| Peak VRAM (`nvidia-smi`, regimes) | `6,293.0 MiB` (`rural_p152`) | **`5,694.0 MiB`** (`6,128.0 MiB` over 45 min round-trip) | **`-9.5%`** |
| Process RAM (`WorkingSet` / `Private`) | `7,924.0 / 12,497.7 MiB` | **`6,661.1–6,818.8 / 10,485.2–10,995.6 MiB`** | **`-14%` to `-16%`** |
| Static collision (`MB` / tris) | `735.64 MB` / `18.01M` tris | **`166.66 MB`** / **`4.10M` tris** | **`-77.3%`** |
| Physics memory / instances | `618.46 MB` / `210,096` | **`140.78 MB`** / `142,607` | **`-77.2%`** |
| Scene counts (`DecalRoad` / `WaterBlock` / `TSStatic`) | `69,000` / `9,764` / `24,743` | **`40,601`** / **`1,859`** / **`12,464`** | `-41.2%` / `-81.0%` / `-49.6%` |
| `beamng.log` (`Bounds` / `DecalRoad` errors) | `0` / `38` | **`0`** / **`19`** (zero marking/line/gravel errors) | `-50.0%` |
| **Unbroken Round-Trip (`rt_1..rt_5`)** | Leg 2 crashed at Kokhma guardrail | **`59.646 km`** (`5/5` `reached_target = true`, `113.19 FPS` avg / `91.67` 1% low) | **Complete** |

> [!NOTE]
> During the `59.646 km` unbroken round-trip (`rt_1`..`rt_5`), legs `rt_2` (`14.219 km`), `rt_3` (`4.029 km`), and `rt_4` (`18.275 km`) recorded `0` new damage and both twin-bridge portals `(4052, -3815)` / `(3994, -3836)` crossed with `0` portal collisions. A minor front-bumper sag scrape (`damageSum` on `rt_1` and `rt_5`) occurred at `(3910.96, -3875.09)` on a `~10.8%` vertical sag curve (`way493492348`) with zero scene obstacles.

---

## Live Benchmark Comparison (V2 Regression, P0, P1, Stabilized P2 Corridor vs MapNG)

| Metric | Golden Sector V2 (`golden_sector_v2_regression.json`, `7985014`) | P0 Single `8192@2m` Warm (`p0_single_8192_2m.json`) | P1 Hybrid `4m+1m` Warm (`p1_hybrid_corridor.json`) | **Stabilized P2 Corridor `8192@4m` (`stabilization_after.json`, `7985014`)** | `mapng_golden_sector` (`a5ed648`) |
|---|---|---|---|---|---|
| World extent | `4.095 × 4.095 km` | `16.382 × 16.382 km` | `16.380 × 16.380 km` | **`32.764 × 32.764 km`** | `4.096 × 4.096 km` |
| Cold / Warm load | `35.81 s` wall (`29.28 s` `world_ready_s`) cold | `27.72 s` warm (`37.08 s` cold) | `33.05 s` warm (`52.98 s` cold) | **`64.30 s` wall (`64.18 s` `world_ready_s`) cold** | `37.7 s` warm / `75.6 s` cold |
| Average FPS | **`142.88 FPS`** | `105.50 FPS` | `97.82 FPS` | **`99.72–111.16` (urban) / `106.53` (rural) / `113.19` (round-trip)** | `47.27 FPS` |
| 1% low FPS (`$fps::p99`) | **`105.77 FPS`** | `89.64 FPS` | `78.66 FPS` | **`80.04–88.18` (urban) / `87.62` (rural) / `91.67` (round-trip)** | `35.58 FPS` |
| GPU VRAM peak (`nvidia-smi`) | `4,700.0 MiB` (`4,653.8` avg) | `4,930.0 MiB` | `5,021.0 MiB` (`5,162` cold) | `5,283–5,694 MiB` (`6,128` round-trip peak) | `8,893 MiB` |
| Process RAM (`WorkingSet` / `Private`) | `3,923.9 / 7,099.8 MiB` | `5,562.6 / 9,209.4 MiB` | `4,888.7 / 8,409.5 MiB` | `6,655.9–6,818.8 / 10,485.2–10,995.6 MiB` | — |
| Static / Physics collision | `49.90 MB` / `41.94 MB` | `160.54 MB` / `134.66 MB` | `224.53 MB` / `189.13 MB` | **`166.66 MB` / `140.78 MB`** | — |
| Mod ZIP / `.ter` size | `35.88 MiB` / `48.00 MiB` | — / `192.00 MiB` | — / `48.00 MiB` (+`155.1` DAE) | **`160.26 MiB` / `192.00 MiB`** | — |
| `2560×1440` physical display | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | **`NOT_TESTED`** | — |

---

## Reproduction & Level Cleanup Commands

```powershell
# 1. Build, validate, and package Golden Sector V2 (4 km regression baseline)
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml build
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml validate --beamng "F:\SteamLibrary\steamapps\common\BeamNG.drive"
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/golden_sector.yaml package

# 2. Build, validate, and package stabilized 33 km Corridor V2 (ivanovo_shuya_v2)
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/ivanovo_shuya_v2.yaml build
.\.venv\Scripts\python.exe -m ivanovo2beam -c configs/ivanovo_shuya_v2.yaml validate --beamng "F:\SteamLibrary\steamapps\common\BeamNG.drive"

# 3. Clean obsolete/duplicate builds from BeamNG user dir (preserves ivanovo_golden_sector & ivanovo_shuya_v2)
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/cleanup_beamng_levels.py --dry-run
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/cleanup_beamng_levels.py --stop-beamng

# 4. Live D3D12 stabilization & continuous round-trip QA (via desktop Host Bridge)
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/beamng_host_bridge.py install --level ivanovo_shuya_v2
C:\Users\kiril\AppData\Local\Programs\Python\Python313\python.exe scripts/qa_scaling_experiment.py --level ivanovo_shuya_v2 --mode stabilization --roundtrip --out docs/benchmarks/stabilization_after.json
```

---

## Roadmap & Research Links

- **Long-Term 8-Phase Roadmap & Urban Environment Spec:** [`docs/ROADMAP.md`](ROADMAP.md) — covers Phase 2 Russian procedural architecture (`наличники`, `хрущёвки`/`брежневки`, red-brick factories), Phase 3 full Russian PDD/GOST roads, Phase 4 infrastructure + dedicated **Procedural Urban Environment Subsystem**, Phase 5 street/settlement/POI markers, Phase 6 realistic AI traffic, Phase 7 seamless `220 × 145 km` Oblast scaling, and Phase 8 Assetto Corsa export & generalized Russian World Generator.
- **External Asset Reuse & Geo-Pinned Replacement Spec:** [`docs/research/asset_reuse.md`](research/asset_reuse.md) — license-isolated `assets_external/` + `asset_manifest.yaml` design covering local-only `ccd2beam` and `UUBI_Ivanovo` (`UUBI` airport) asset catalogs and multi-sim `ExternalAssetIR`.
- **Remaining Stabilization Polish Items:** (1) Ease vertical sag profile on `way493492348` (`~10.8%` sag at `(3910.96, -3875.09)`); (2) Inspect the remaining `19` `road_asphalt_2lane` `SceneStaticDecalRoadManager` batch warnings; (3) Re-test on native `2560×1440` hardware to resolve `NOT_TESTED`.
