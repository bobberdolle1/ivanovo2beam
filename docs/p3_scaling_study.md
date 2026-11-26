# P3 Scaling Study — Continuous 33 km Corridor Architecture (`ivanovo_shuya_v2`)

**Date:** 2026-11-26  
**Scope:** Scaling `ivanovo2beam` from the 4.095 × 4.095 km Golden Sector V2 (`ivanovo_golden_sector`) to the continuous ~33 km Ivanovo–Kokhma–Shuya Р152 corridor (`32.764 × 32.764 km`, `1,073.5 km²`) on BeamNG.drive 0.40.6.0 (D3D12).

---

## 1. The Scaling Constraint

BeamNG.drive's `.ter` v9 heightmap format enforces a hard per-`TerrainBlock` resolution ceiling of **`8192 × 8192`** vertices (`"Height map must be between 128 and 8192 in size!"`), yielding `extent_m = (grid_size - 1) × square_size_m`:

| Grid | Spacing (`square_size_m`) | World Extent | `.ter` Size | Trade-off |
|---|---|---|---|---|
| `4096 × 4096` | `1.0 m` | `4.095 × 4.095 km` | `48.00 MiB` | Golden Sector V2 reference; covers single city/crossing |
| `4096 × 4096` | `8.0 m` | `32.760 × 32.760 km` | `48.00 MiB` | Legacy `ivanovo_shuya.yaml`; 8 m vertex spacing blurs narrow road shoulders |
| `8192 × 8192` | `2.0 m` | `16.382 × 16.382 km` | `192.00 MiB` | P0 (`ivanovo_single_8192_2m`); spans half-corridor at 2 m |
| `8192 × 8192` | `4.0 m` | `32.764 × 32.764 km` | `192.00 MiB` | **P2 Production (`ivanovo_shuya_v2`)**: 2× legacy resolution across full 33 km corridor |

To decide how to scale past `4 × 4 km @ 1.0 m`, we evaluated four architectures empirically inside live BeamNG.drive 0.40.6.0 D3D12.

---

## 2. Candidate Architectures Evaluated

### 2.1 Adjacent Multi-`TerrainBlock` (`ivanovo_dual_terrain_exp`) — Rejected
Tested in [`docs/p2_multiblock_experiment.md`](p2_multiblock_experiment.md) using two `2048 × 2048 @ 1.0 m` blocks sliced from one continuous surface (`max_dz = 0.000 m`, `layer_mismatches = 0`). Live BeamNG 0.40 probing proved four hard engine blockers:
1. `core_terrain.getTerrainHeight` and physics `castRayStatic` bind only to the first `TerrainBlock` (raycast misses completely across `x >= 8350.0`).
2. `DecalRoad` (`overObjects=false`) spline projection & AI `navgraph` fail at the seam (`z = 0` nodes; vehicle stops at `12.4 km/h`).
3. `GroundCover` renders **0 elements** on secondary `TerrainBlock`s.
4. Clamped per-block finite-difference normals leave a visible dark crease along the seam, while dual-block initialization inflates load time to `147.21 s`.

### 2.2 MapNG Reference Analysis (`.tmp/mapng_ref`)
Inspecting `nikkiluzader/mapng` (`groundMesh.js`, `chunkedExport.js`, `chunkMerge.js`, `meshRoads.js`, `chunkSeamRoads.js`, `exportBeamNGLevel.js`) confirmed MapNG does **not** use multiple `TerrainBlock`s either:
- **Ground as Collada meshes (`groundAsModels: true`):** Tiles ground into `GROUND_TILE = 256` RTIN meshes (`GROUND_MAX_ERROR_M = 0.05 m`, forced tile borders + material boundaries + `triangleOff()` long-edge refinement, central-difference full-grid normals, `EDGE_SKIRT_M = 15 m` visual-only outer skirts).
- **Anchor `TerrainBlock`:** Keeps one `256 × 256 @ 0.1 m` flat placeholder `100 m` under spawn (`GROUND_ANCHOR_DEPTH_M = 100`) solely because BeamNG refuses to draw any `DecalRoad` when zero `TerrainBlock`s exist.
- **`Colmesh-1` & `terrain_b.color.png`:** Uses `.color.png` so `TSStatic` materials read sRGB rather than linear, and avoids trailing digits on mesh names (`ground_a`) because BeamNG parses trailing numbers as LOD detail sizes.
- **Known MapNG costs:** Disables native `GroundCover` ("grows on TerrainBlock layers only") and 5-channel `TerrainMaterial` blending, and pushes `mapng_golden_sector` to `47.27 FPS` avg / `8,893 MiB` VRAM.

### 2.3 P0 Single `TerrainBlock` (`8192 × 8192 @ 2.0 m`) vs P1 Hybrid (`4096 @ 4.0 m` + `1.0 m` RTIN `.dae`)
Built and benchmarked apples-to-apples over the exact same `16.38 × 16.38 km` Ivanovo–Kokhma–Shuya territory (`center = [41.16958, 56.92439]`, `spawn = [41.10000, 56.92700]`):

- **P0 (`configs/experiment_single_8192_2m.yaml`):** Single `8192 × 8192 @ 2.0 m` `TerrainBlock` (`192.00 MiB` `.ter`) with all Golden Sector V2 features enabled (`18,541` objects, `45,000` trees, `8` `GroundCover` layers).
- **P1 (`configs/experiment_hybrid_corridor.yaml`, `src/ivanovo2beam/sectors/hybrid.py`):** Single `4096 × 4096 @ 4.0 m` base `TerrainBlock` (`48.00 MiB` `.ter`) depressed `-0.55 m` (`6.0 → 12.0 m` cosine ramp) under `134` RTIN-simplified `512 m @ 1.0 m` Collada corridor chunks (`0.08 m` max error, `1,645,086` tris = `36.2%` of unsimplified, `134.85 MB` DAE, `collisionType="Visible Mesh Final"`, `decalType="Visible Mesh"`), with `2,480` corridor `DecalRoad`s lifted onto `overObjects=true`.

---

## 3. Live D3D12 Benchmark Comparison (`1920×1080 Borderless, Ultra, DLSS`)

Measured by [`scripts/qa_scaling_experiment.py`](../scripts/qa_scaling_experiment.py) on **AMD Ryzen 5 7500F / NVIDIA GeForce RTX 3080 10 GB / 31.6 GB RAM / BeamNG.drive 0.40.6.0 D3D12** (anonymized JSONs in [`docs/benchmarks/`](benchmarks/)):

| Metric | Golden Sector V2 (`4096@1m`, `16.8 km²`) | **P0 Single (`8192@2m`, `268.4 km²`)** | **P1 Hybrid (`4096@4m + 1m DAE`, `268.4 km²`)** | **P2 Corridor V2 (`8192@4m`, `1,073.5 km²`)** |
|---|---|---|---|---|
| Benchmark file | [`golden_sector_v2_regression.json`](benchmarks/golden_sector_v2_regression.json) | [`p0_single_8192_2m.json`](benchmarks/p0_single_8192_2m.json) | [`p1_hybrid_corridor.json`](benchmarks/p1_hybrid_corridor.json) | [`p2_ivanovo_shuya_v2.json`](benchmarks/p2_ivanovo_shuya_v2.json) |
| Load time (`world_ready_s`) | `24.30 s` warm | **`27.72 s`** warm (`37.08 s` cold) | `33.05 s` warm (`52.98 s` cold) | **`78.35 s`** cold (`78.49 s` wall) |
| `TSStatic` load stage | `0.69 s` (`642`) | `2.54 s` (`4,468`) | `3.35 s` warm / `14.47 s` cold (`4,586`) | `21.18 s` cold (`24,743`) |
| `.ter` / `.dae` footprint | `48.00 MiB` / `3.55 MiB` (`45`) | `192.00 MiB` / `30.76 MiB` (`404`) | `48.00 MiB` / `155.10 MiB` (`522`) | `192.00 MiB` / `93.49 MiB` (`1,399`) |
| **Avg FPS** | `143.17 FPS` | **`105.50 FPS`** | `97.82 FPS` | **`80.32 FPS`** |
| **1% Low / Min FPS** | `111.07 / 104.97 FPS` | **`89.64 / 88.27 FPS`** | `78.66 / 68.96 FPS` | **`48.68 / 36.48 FPS`** |
| CPU / GPU frametime avg | `5.96 / 6.65 ms` | `9.41 / 5.45 ms` | `10.26 / 5.77 ms` | `16.18 / 26.54 ms` |
| Peak VRAM (`nvidia-smi`) | `4,575.0 MiB` | **`4,930.0 MiB`** | `5,021.0 MiB` warm / `5,162` cold | **`6,333.0 MiB`** (`6,236.6` avg / 10 GB) |
| Process RAM (`WorkingSet` / `Private`) | `3,957 / 7,077 MiB` | `5,563 / 9,209 MiB` | `4,889 / 8,410 MiB` | `8,093 / 12,738 MiB` |
| Static / Physics collision | `63.56 / 53.32 MB` (`1.55M` tris) | **`160.54 / 134.66 MB`** (`3.92M` tris) | `224.53 / 189.13 MB` (`5.51M` tris) | `735.64 / 618.46 MB` (`18.01M` tris) |
| `Forest` items / `GroundCover` | `45,000` (`30` spp) / `8` layers | `45,000` (`30` spp) / `8` layers | `45,000` (`30` spp) / `8` layers (off `.dae`) | **`120,000`** (`32` spp) / `8` layers |
| `DecalRoad`s / `WaterBlock`s / `River`s | `1,733` / `407` / `9` | `11,629` / `2,361` / `55` | `11,627` (`2,527` `overObjects`) / `2,361` / `55` | `69,000` / `9,764` / `157` |
| AI `etk800` drive (`~80–105 km/h`) | `2.22 km`, `damageSum=0` | **`6.37 km`**, **`damageSum=0`** | `4.56 km`, `damage=6,416` (Leg 1) / `0` (Leg 2) | `8.69 km` across 3 legs (`damageSum=0` on Ivanovo→Kokhma & Shuya legs) |
| `2560×1440` display | `NOT_TESTED` (1080p monitor) | `NOT_TESTED` | `NOT_TESTED` | `NOT_TESTED` |

### Key Empirical Findings from P0 vs P1
1. **`DecalRoad overObjects: true` works on `Visible Mesh Final`:** Unlike MapNG's older `Colmesh-1` finding, BeamNG 0.40 rendered all `2,527` `overObjects: true` roads, markings, and `edge_grass` decals cleanly onto `collisionType: "Visible Mesh Final"` + `decalType: "Visible Mesh"` corridor `.dae`s with `0` missing materials.
2. **Double-collision suppression works:** Depressing the base `TerrainBlock` by `-0.55 m` (`6 → 12 m` ramp) yielded clean `0.541–0.553 m` separation in `castRayStatic` probes with `0` `z=0` `navgraph` nodes across seams.
3. **Why Single `8192 × 8192` won decisively:**
   - **Visual & GroundCover fidelity:** P1 `.dae` corridor meshes bake discrete base-terrain material zones into static meshes, producing visible `4 m` checkerboard grass/sand/gravel squares and sawtooth feather triangles along road shoulders while stripping 3D `GroundCover` from the corridor. P0/P2 Single `TerrainBlock` keeps smooth 5-channel `TerrainMaterial` blending and all 8 native `GroundCover` layers seamlessly up to road edges.
   - **Performance & memory efficiency:** On identical `268.4 km²` territory, P0 achieved **+7.9% avg FPS (`105.50` vs `97.82`)**, **+14.0% 1% low FPS (`89.64` vs `78.66`)**, **-40% physics/static collision triangles (`3.92M` vs `5.51M`)**, faster warm (`27.72 s` vs `33.05 s`) and cold (`37.08 s` vs `52.98 s`) loads, and `damageSum = 0` across `6.37 km`.
   - **33 km scalability:** Extrapolating P1's `1 m` `.dae` mesh strategy to the full `32.76 km` corridor (`1,073.5 km²`) would inflate `.dae` count, cold `TSStatic` load time, and static collision well past P2's `735 MB` budget, whereas a single `8192 × 8192 @ 4.0 m` `.ter` stays fixed at `192.00 MiB`.

---

## 4. Production P2 Corridor Implementation (`ivanovo_shuya_v2`)

[`configs/ivanovo_shuya_v2.yaml`](../configs/ivanovo_shuya_v2.yaml) implements the winning **Single `TerrainBlock` `8192 × 8192 @ 4.0 m`** (`32.764 × 32.764 km`) architecture:

- **1:1 Metric Alignment:** Preserves `central_meridian = 41.10`, `latitude_of_origin = 57.00`, `origin = [40.97393, 56.99987]`, `center = [41.16958, 56.92439]`, and `scale_accuracy = 10.65 m / 100 km`.
- **Calibrated Golden Sector V2 Parity:**
  - `corridor_half_width_m: 14.0` (ensuring ≥ 3.5 graded grid cells per side on `4.0 m` spacing for smooth `Р152` and `82` coupled bridges totaling `4,633 m`).
  - `vegetation.max_trees: 120000` (`2.67×` Golden Sector's `45,000` `.link` stems across `32` species, eliminating P0's `45k`-across-`268 km²` roadside sparsity) + `8` native `GroundCover` layers (`322k` max elements).
  - `buildings.max_buildings: 12000` (retaining the largest `12,000` of `41,587` OSM footprints — all industrial, commercial, civic, apartment, and main residential blocks across Ivanovo, Kokhma, and Shuya — placing `11,945` inside bounds).
- **Pipeline Scaling Optimizations:**
  - `src/ivanovo2beam/roads/props.py`: replaced the `O(N × M)` `bus_stop`/`crossing` linear road scan (`1,981` OSM nodes × all paved segments) with a `32 m` spatial hash grid, cutting prop matching from `> 20 min` down to sub-second execution.
  - `src/ivanovo2beam/build.py`: added `_clip_ground_decal_roads_to_bounds` (preserving `ROAD_RENDER_MARGIN_M = 250 m` for terrain grading and crossing cuts while trimming `overObjects=false` `DecalRoad`s inside the `TerrainBlock`, eliminating all `SceneContainer::checkBins` off-terrain octree warnings) and `6` corridor `SpawnSphere` presets (`player_spawn`, `spawn_kokhma_se`, `spawn_kitovo`, `spawn_shuya`, `spawn_forest`, `spawn_ivanovo`).

### Live 33 km D3D12 Verification Summary
- **End-to-end AI `navgraph`:** `map.getPath` from Ivanovo exit (`[4125.4, -5043.2]`) to Shuya city (`[23707.7, -14574.6]`) returned **`connected = true`**, **`486` nodes**, **`25.229 km`** unbroken route, and **`0` `z=0` nodes** across all `10` corridor anchors.
- **Raycast accuracy:** `castRayStatic` surface delta `0.0000–0.0051 m` across all `9` road and off-road probes (`4 km`–`15 km`).
- **Log cleanliness:** `0` `GroundCover`, `ForestData`, `TerrainBlock`, `WaterBlock`, `Collada`, `WarningMaterial`, or `SceneContainer::checkBins` errors.
- **Known remaining gaps for future M5/M6 polish:**
  1. `38` internal `SceneStaticDecalRoadManager` `2048 m` / `inf` batch packing overflow lines remain on long/curved internal road markings/segments.
  2. Minor `4.0 m` terrain-grid grass clipping occurs on narrow un-graded urban residential streets (`corridor_half_width_m` vs `4 m` cell edge), whereas `14 m`-graded `Р152` and bridges are clean.
  3. Leg 2 AI drive (`kokhma_junc -> corridor_15km`) collided off-road at ~`114 s` (`damageSum = 104,101,776`) while Leg 1 (`3.149 km`, `101.04 km/h`) and Leg 3 (`3.212 km`, `101.08 km/h`) completed with `damageSum = 0`.
  4. **Whole-oblast scaling (> 33 km, M5/M6):** Because `8192 × 8192 @ 4.0 m` already reaches the `.ter` resolution ceiling for `33 km`, scaling to the full `220 × 145 km` Ivanovo Oblast requires independent `8192 × 8192` sector levels linked by the shared Oblast Transverse Mercator projection (or selective P1-style RTIN corridors only if `TerrainMaterial` blending can be restored on static meshes).
