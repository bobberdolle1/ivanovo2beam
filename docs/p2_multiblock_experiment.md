# P2 Multi-TerrainBlock Scaling Experiment (`ivanovo_dual_terrain_exp`)

## 1. Findings from `nikkiluzader/mapng` (`.tmp/mapng_ref`)

Inspection of `.tmp/mapng_ref/services/{chunkLayout.js,chunkedExport.js,chunkMerge.js,chunkSeamRoads.js,exportBeamNGLevel.js,groundMesh.js}` shows how MapNG scales past a single `TerrainBlock`:

1. **MapNG does NOT ship multiple `TerrainBlock`s in its chunked pipeline.**
   - `chunkMerge.js` (`LEVEL_WIDE`) explicitly treats `TerrainBlock`, `GroundCover`, `Forest`, `LevelInfo`, etc. as single level-wide objects (`first` chunk only).
   - MapNG converts terrain into tiled Collada static ground meshes (`groundAsModels: true`, `groundMesh.js` RTIN simplification at `GROUND_TILE = 256`, `GROUND_MAX_ERROR_M = 0.05 m`) plus a tiny `256x256 @ 0.1 m` flat anchor `TerrainBlock` buried `100 m` under spawn (`exportBeamNGLevel.js:1032-1047`) because BeamNG refuses to draw any `DecalRoad` if zero `TerrainBlock`s exist in the scene.
   - To avoid seam height steps between independently solved chunks (`~0.8 m` measured divergence in Oslo), MapNG uses `OVERLAP_PX = 256` (`CHUNK_PX = 2048`, stride `1792`), `seamBlendFor()` (`SEAM_EASE_PX = 64`), `seamRoadPins()`, and `joinSeamRoads()` (`near = 0.3 m`, `dzMax = 0.5 m`).
2. **Known trade-offs of MapNG's `groundAsModels` workaround:**
   - **No `GroundCover`:** `exportBeamNGLevel.js:14417, 16994` disables `GroundCover` because BeamNG `GroundCover` only grows on `TerrainBlock` material layers.
   - **No 3-tier PBR terrain blending:** Static `.dae` ground loses `TerrainMaterial` 5-channel base/detail/macro blending and reads a single baked `baseColorMap` (`terrain_b.color.png`) with one tiled `detailNormalMap`.
   - **Heavy `Colmesh` collision cost:** `0.76M–1.31M` triangles per 1 km² (`groundMesh.js:25`).

## 2. Experimental Harness: Two True Adjacent `TerrainBlock`s

To prove empirically whether BeamNG.drive 0.40 D3D12 supports **multiple adjacent `TerrainBlock`s** directly at `1.0 m` resolution, `src/ivanovo2beam/sectors/multiblock.py` and `scripts/experiment_dual_terrain.py` build an isolated level `ivanovo_dual_terrain_exp` (`configs/experiment_dual_terrain.yaml`):

- **Layout:** `2 x 1` adjacent `2048x2048 @ 1.0 m` blocks (`4094 m x 2047 m` total):
  - `theTerrain_0_0` (West): `origin = (6303.0, -9800.0)`, `x in [6303.0, 8350.0]`
  - `theTerrain_1_0` (East): `origin = (8350.0, -9800.0)`, `x in [8350.0, 10397.0]`
- **Exact seam parity verified offline (`validate_multiblock_level`):**
  - Both blocks are sliced from one continuous `4096x4096 @ 1.0 m` graded `TerrainBuild` with identical `(base_z = 89.644 m, max_height)` and 14-material array.
  - Column `2047` of `theTerrain_0_0` and column `0` of `theTerrain_1_0` at `x = 8350.0` have **`max_dz = 0.000000 m`**, **`u16_diff = 0`**, and **`layer_mismatches = 0`**.
- **Real-world crossings across `x = 8350.0` (Kokhma SE / Р152):**
  - Primary highway Р152 (`way/183586213`) + 12 residential/service roads
  - Uvod river (`way/136383439`) and tributary (`way/274670197`)
  - Continuous forest (`.forest4.json`), experimental `GroundCover` (`exp_meadow_grass_cover`, `exp_short_grass_cover` + V2 layers), buildings (`34` chunks / `TSStatic`), and bridge `way/274102985`.

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts/experiment_dual_terrain.py
# Isolated build/validation/installation into F:\Games\BeamNG.drive\current\levels\ivanovo_dual_terrain_exp
```

## 3. Observed Live BeamNG.drive 0.40.6.0 D3D12 Results

Live MCP Lua probes, raycasts, AI driving tests, and screenshot inspections (`.tmp/p2_live_qa.py`, `.tmp/p2_live_qa_report.json`) established **four hard Torque3D / BeamNG 0.40 engine limitations** when two `TerrainBlock`s coexist in one scene:

| Property Across `x = 8350.0` | `theTerrain_0_0` (`x < 8350`) | Seam (`x = 8350`) & `theTerrain_1_0` (`x >= 8350`) | Root Engine Cause |
|---|---|---|---|
| `obj:getHeight(pos)` (direct method) | Works (`107.47 m` at `8349`) | Works (`114.11 m` on `t1` at `(8500, -8540)`) | Both `.ter` blocks exist in SceneTree |
| `core_terrain.getTerrainHeight(pos)` | `107.4688 m` at `(8349, -8464.4)` | **`89.6441 m` (`base_z`)** at `x = 8350, 8351, 8352, 8500` | Global helper binds only to the first `TerrainBlock` (`theTerrain_0_0`) |
| `castRayStatic(..., -Z)` ground collision | Hits cleanly (`107.4688 m` at `8349`) | **Returns `0` (complete raycast miss)** at `x >= 8350` | Physics/render terrain raycast fails across second `TerrainBlock` boundary |
| `DecalRoad` (`overObjects=false`) projection & `navgraph` | Drapes normally on most west ways; fails on some ways | Navgraph nodes on `x=8348.2, 8341.7` snap to **`z = 0`**; vehicle cannot cross seam (`max_speed = 12.4 km/h`, `crossed_seam = false`) | Cross-seam `DecalRoad` spline/navgraph lookup disagrees between active block handles |
| `GroundCover` vegetation | Renders lush 3D grass, clover, wildflowers (`8+2` layers) | **0 elements** beyond `x = 8350.0` (razor-sharp curtain cutoff in `p2_groundcover_block0_vs_block1.png` and `p2_river_seam.png`) | `GroundCover` binds to a single active `TerrainBlock` |
| Visual seam shading | Smooth lighting inside block | **Dark north-south normal/shadow crease** visible across asphalt, fields, and Uvod river in `p2_seam_ground_closeup.png` and `p2_seam_aerial.png` despite `0.000 m` height ΔZ | Torque3D clamps finite-difference terrain normal calculation at `[0, N-1]` per block |
| Runtime performance (`1920×1080 Ultra D3D12`) | — | `fps_avg = 175.96`, `fps_1pct_low = 140.82`, `vram_peak = 4,861 MiB`, `world_ready_s = 147.21 s` (62s–158s slow dual-block load) | Initializing two `TerrainBlock`s adds massive startup cost without functional multi-block physics/decals |

### Conclusion from P2

**Multiple native `TerrainBlock`s in one BeamNG.drive 0.40 level are NOT viable as a production scaling architecture.** Even with mathematically exact border vertices (`max_dz = 0.000 m`, `u16_diff = 0`, `layer_mismatches = 0`), BeamNG 0.40 binds `core_terrain`, physics `castRayStatic`, `GroundCover`, and `DecalRoad` projection to a single primary terrain instance, leaving adjacent `TerrainBlock`s un-drivable across the seam, stripping all `GroundCover` on secondary blocks, and creating a permanent dark normal crease along the block boundary.

## 4. Recommended Production Scaling Architecture (Instead of Dual `TerrainBlock`s)

To scale beyond `4 × 4 km @ 1.0 m` without falling back to a blurry `8.0 m` whole-corridor grid:

1. **Up to `8 × 8 km` (`1.0 m`) or `16 × 16 km` (`2.0 m`) sectors:** Use a **single `8192 × 8192` `TerrainBlock`** (the hard `.ter` v9 vertex cap) so native 5-channel `TerrainMaterial`, `GroundCover`, physics collision, and `DecalRoad` projection work everywhere with zero seams. Pair with independent playable sector levels along the corridor (linked cleanly via the shared Oblast Transverse Mercator projection).
2. **Continuous multi-chunk terrain beyond `8192 × 8192` (`> 16 km` at `1 m` corridor detail):** Adopt a **hybrid architecture**:
   - Keep **one single low-res `TerrainBlock`** (`2048`–`4096`) spanning the world strictly as the global fallback terrain + `DecalRoad` / spawn anchor (or depressed slightly below road corridors where detailed meshes take over).
   - Export **RTIN-simplified Collada terrain chunks** (`500 m`–`1000 m` sectors with `~0.05–0.10 m` vertical error tolerance, shared chunk border vertices for C0/C1 seamless normals, and `Visible Mesh Final` collision) along highway corridors (`P152`), using `overObjects: true` on `DecalRoad`s (or baked UV road strips) and replacing `GroundCover` with instanced 3D grass/shrub `.forest4.json` items (`TSForestItemData`) inside high-detail mesh zones.
