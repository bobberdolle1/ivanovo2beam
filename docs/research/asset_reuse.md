# External Asset Reuse Audit & Universal Geo-Pinned Replacement Architecture

**Author:** `asset-auditor` (Asset & Pipeline Research Lead)  
**Date:** 2026-11-27  
**Scope:** Read-only technical & legal audit of two local asset/pipeline repositories (`F:\Projects\ccd2beam` + City Car Driving install, and `E:\SteamLibrary\steamapps\common\X-Plane 12\Custom Scenery\UUBI_Ivanovo`) and design specification for a **Universal External Asset Import & Geo-Pinned Procedural Replacement System** for `ivanovo2beam` (BeamNG.drive + future Assetto Corsa export).

> [!IMPORTANT]
> **Legal & Repository Hygiene Invariant:** Neither City Car Driving (`F:\SteamLibrary\steamapps\common\City Car Driving`) nor X-Plane `UUBI_Ivanovo` assets are redistributable. **Zero third-party geometry, textures, `.nvx2`, `.dds`, `.obj`, or `.png` files may ever be committed to `ivanovo2beam` or bundled into open-source releases.** All external reuse must operate strictly via a local, gitignored `assets_external/` workspace + manifest registry.

---

## 1. Audit A: `F:\Projects\ccd2beam` & Local City Car Driving Installation

### 1.1 Repository & Codebase Overview

`F:\Projects\ccd2beam` is a Python 3.11+ (`pyproject.toml`) content-translation pipeline converting **City Car Driving (CCD)** (`engine_build: 1.0.60236.24810`) into a **BeamNG.drive** mod (`levels/<name>/` unpacked or `.zip`), preserving BeamNG vehicle physics while translating CCD meshes, scene graphs, materials, navigation, and static collision.

- **Dependencies (`pyproject.toml`):** `Pillow>=10`, `numpy>=1.24`, `ispc-texcomp==1.0.1`, `psutil>=5.9`; optional `dev` (`pytest>=7`) and `oracle` (`pycollada>=0.7` for independent COLLADA round-trip auditing).
- **Module Layout (`src/ccd2beam/`):**
  - `discovery/` (`paths.py`, `beamng.py`): locates CCD install (`home:`, `textures:` virtual paths) and active BeamNG user folder via runtime VFS checks.
  - `formats/` (`nvx2.py`, `ntcl.py`, `location_binary.py`, `navigation_map.py`, `source_surfaces.py`, `audit.py`): binary & script decoders for Nebula2 `.nvx2` meshes, `.n2` Tcl scene graphs, `.g`/`.p` placement tables, `.pstatic.sg`/`.mopp` Havok static collision envelopes, `.bmap` road/navigation graphs, and `.fbin` foliage streams.
  - `scene/` (`graph.py`, `level.py`, `world.py`, `dependencies.py`, `foliage.py`, `navigation_graph.py`, `traffic_*.py`, `vehicle_assembly.py`, `runtime_state.py`): hierarchy IR, `.g` selection/locale resolution, reference closure, and VirtualCity 8-region world assembly.
  - `beamng/` (88 modules): `transform.py`, `collada.py`, `materials.py`, `material_export.py`, `texture_cooking.py`, `texture_composition.py`, `collision.py`, `collision_proxy.py`, `collision_pole.py`, `collision_breakaway.py`, `forest.py`, `source_tree_lod.py`, `fuel_stations.py`, `parking_sites.py`, `signal_generator.py`, `navigation.py`, `level.py`, `deploy.py`, `verify.py`, `asset_cache.py`, `performance.py`.
  - `tests/`: 92 deterministic test modules (`test_nvx2.py`, `test_collada.py`, `test_collision.py`, `test_collision_proxy.py`, `test_texture_cooking.py`, `test_transform.py`, etc.).

### 1.2 Local CCD Russian Asset Catalog (`F:\SteamLibrary\steamapps\common\City Car Driving\export`)

Inspected directly on disk (`meshes/`: **3,845 `.nvx2`**, `gfxlib/`: **3,731 `.n2`**, `texturesdds/`: **8,555 `.dds`**; note `docs/CCD_FORMATS.md` records 20,487 total files under `export/`):

| Category Path (`export/meshes/`) | `.nvx2` Count | Russian Assets & Relevance to `ivanovo2beam` | LOD / Notes |
| --- | ---: | --- | --- |
| `universal/PDD_objects` | 1,446 | Complete Russian **GOST R 52290-2004** traffic signs (`sign_2_4_*`, `sign_4_1_2_*`, `Sign_D4-*`, speed, priority, direction) | Native `_lod1`, `_lod2` variants |
| `universal/stolb` | 81 | 33 base meshes: streetlights (`fonar_stolb_final`), bridge/overhead gantries (`opora_most_00..09`), railway poles (`railway_stolb_1/2`), `stolb_single/double/quadro`, `stolb_cosmos_big`, `parcing_stolb_*` | `_lod1.._lod3` on major poles |
| `universal/barrier` | 36 | Concrete construction fences (`barrier_concrete*`), metal barriers (`barrier_metal*`), `barrier_iris*`, brick/builder/twisted fences | Candidate roadside/construction barriers |
| `universal/industrial` | 80 | Russian gas stations (`AZS_1_blue/yellow`, `AZS_old_1/2`, `AZS_board_1_*`), `Builder_house`, `Gate_*`, `LEP_200_4` (power pylons), `Truba` (industrial chimney), `MTZ1221_*` tractor, `Komatsu-PC1250` | `_lod1.._lod2` on `AZS_old_2` |
| `universal/produce` | 21 | Russian bus shelters (`bus_station_blue`, `bus_station_glas_02_final`), kiosks (`Ice_cream_shop`, `flower_shop_01_final`, `ProduceFood`, `shop_1..5`, `tent`, `trailer`) | Direct replacement candidates for OSM `highway=bus_stop` & `shop=kiosk` |
| `universal/bilboard` | 150 | `bilboard_(6x3)*`, `bilboard_MKAD` Russian roadside 6×3 m billboards | `_lod1.._lod3` variants |
| `universal/pointers` | 44 | Overhead & roadside directional pointers (`Pointer_01`, `Pointer_03_*`) | Russian road signage |
| `universal/Monuments` | 9 | Soviet/space pedestals (`pedestal_01..05`, `baykonur`, `lunohod`, `sputnik`, `voyager`) | Landmark replacements |
| `universal/other` | 46 | `Lenin.nvx2`, traffic cones (`konus*`), `asphalt_patch`, `bridge_column/crossbar`, `concrete_plate`, `floodlight` | Urban props & monuments |
| `universal/tree` | 102 | 33 base meshes: `beeroza_big/med/small` (birch), `elka*` (spruce), `klen_big`, `lipa_big`, `WillowTopol_border`, `BushSimple*`, `clumba_*`, `grass_foliage*` | `_lod1.._lod3` + crossed-quad leaf expansion |
| `locations/Modern_buildings` | 23 | Modern Russian apartment/commercial blocks (`modern_buildings_01..09`) | `_lod1.._lod3` |
| `locations/Country_buildings` | 22 | Russian village houses (`village_hous_01..04`, `village_hous_low_01..05`, `Com_center*`) | Rural/suburban replacement archetypes |
| `locations/typical` | 20 | Moscow-style Russian urban landmarks (`LeninkyProspekt_12`, `TverskoiBulvar_16s2`, `Ostojenka3a`, `fireStation`) | `_lod1.._lod3` on major facades |
| `locations/VirtualCity*` & `Wise_City*` & `_location_*` | 665+ | City terrain tiles, bridges (`Bridge_1/2_Old_district`), tunnels, railways, Moscow district buildings (`_location_CAO/SAO/VAO/ZAO/Cosmos/Sadovoe`) | Reference + region-specific buildings |

### 1.3 Reusable Converters, Transforms, Collision & Texture Pipeline in `ccd2beam`

Several engineering patterns in `ccd2beam` are directly validated against BeamNG 0.40 and solve exact edge cases `ivanovo2beam` faces:

1. **Single-Boundary Coordinate Transform (`beamng/transform.py`):**
   - CCD uses right-handed **Y-up** metres; BeamNG uses right-handed **Z-up** metres (`+X` east, `+Y` north, `+Z` up).
   - Permutation `P = [[1,0,0],[0,0,-1],[0,1,0]]` (`det = +1`, preserves triangle winding without reversing indices): `(x, y, z)_CCD -> (x, -z, y)_BeamNG`, rotation matrix conjugated via `R_beamng = P @ R_ccd @ P^-1`, quaternion vector part `(x, -z, y, w)`.
   - **Critical Serialization Rule:** BeamNG's `items.level.json` `rotationMatrix` is **column-major** (`MatrixF` columns as verified via `getRotation():getColumn(i)`), whereas internal IR matrices are row-major — transpose strictly at serialization.
   - **Pivot Recentering (`beamng/level.py`):** Vertices are recentered on their local geometry centroid, and the world offset is placed on `TSStatic.position` to avoid float32 precision jitter.
2. **NVX2 & Scene Decoding (`formats/nvx2.py`, `formats/ntcl.py`):**
   - `nvx2.py` decodes `2XVN` headers, group tables (`vertex_first`, `vertex_count`, `triangle_first`, `triangle_count`), both 16-bit and 32-bit index blocks, and 15 vertex component attributes (`position`, `normal`, `uv0..uv7`, `color`, `tangent`, `binormal`, `weights`, `joints`).
   - `.setlocalbox` in `.n2` is `(center, half_extent)`, not `(min, max)` — though mesh vertex bounds remain authoritative due to exporter quirks.
3. **Strict BeamNG COLLADA 1.4.1 Serialization (`beamng/collada.py`):**
   - `Z_UP`, `<unit meter="1">`, `CHANNEL1` texcoord binding (`<bind_vertex_input semantic="CHANNEL1" input_semantic="TEXCOORD" input_set="0"/>`).
   - Top-origin UVs (CCD / DirectX / DDS) are V-flipped at serialization (`(u, 1.0 - v)`).
   - `<accessor>` must NOT emit a `size` attribute (BeamNG Torque DOM logs errors on unknown `size`).
   - `<p>` emits one index per `<input>` per corner (`i i i` when sharing vertex/normal/UV indices, not expanded `vertex*stride`).
   - Float64 precision is retained in `.dae` `<float_array>` until native import to prevent thin-triangle distortion.
4. **Material Translation & Native BC7 Texture Cooking (`beamng/materials.py`, `beamng/texture_cooking.py`, `beamng/asset_cache.py`):**
   - Shader semantics are decoded from `dx11/4.0/shaders.fxo` DXBC (`Building`, `Solid`, `RoadWithRoughnesses`, `TreeLeaf`, `SolidLuminescent`, `TrafficlightLamp`).
   - `texture_cooking.py` uses `ispc-texcomp==1.0.1` (`alpha_basic` profile) to encode **BC7_UNORM (`DXGI_FORMAT = 98`)** DDS files with full mip chains (`DDS_ALPHA_MODE_STRAIGHT`), using strip-bounded partition-box filtering and **unit-vector re-normalization on normal map mips**.
   - `ArtifactCache` (`asset_cache.py`) provides SHA-256 content-addressed staging in `.cache` (hashing source files, parameters, NumPy arrays, and converter version) for deterministic resumable builds.
5. **Collision Proxy & Native Hierarchy (`beamng/collision.py`, `collision_proxy.py`, `collision_pole.py`):**
   - BeamNG native collision-only hierarchy (verified against `driver_training.zip`):
     `base00` → `start01` → `Colmesh_<name>-1` (mesh geometry) + `base00` → `collision-1` (empty marker node).
   - Physical-only shapes set `collisionType: "Collision Mesh"`, `useInstanceRenderData: false`, keeping collision meshes out of `SceneStaticManager` render batching.
   - `collision_proxy.py` demonstrates building **float32-safe quantized convex hull enclosures** (`0.1 mm` quantization, max deviation `< 0.25 mm`, capped face counts) so vehicles do not snag on thin/non-manifold visual triangles (documented in `docs/M2_PHYSICAL_COLLISION.md` & `docs/M2_PANEL_COLLISION.md`).

### 1.4 CCD License & Risk Assessment

- `F:\Projects\ccd2beam` has **no OSS `LICENSE` file** in its root (private repo); its `AGENTS.md` and `README.md` enforce strict clean-room hygiene: **no CCD geometry, `.dds` textures, or generated assets are committed** (`generated/` is gitignored).
- CCD game assets belong to **Forward Development** (proprietary EULA). Any `ivanovo2beam` integration MUST treat CCD as an **optional local-install converter source** (`--ccd-root` / `assets_external/`), never committing code or assets from CCD without explicit ownership/permission, and never redistributing converted CCD meshes/textures in public `ivanovo2beam` releases.

---

## 2. Audit B: `E:\SteamLibrary\steamapps\common\X-Plane 12\Custom Scenery\UUBI_Ivanovo`

### 2.1 Directory Census & Geographic Scope

Total disk footprint: **219.58 MB** across the following verified structure:

| Directory / File | Contents & File Counts | Key Observations |
| --- | --- | --- |
| Root | `earth.wed.xml` (529 KB), `earth.wed.bak.xml` (611 KB), `GroundTraffic.txt` (377 B) | WED XML holds airport pavements/runways/ramps; 3D objects live in DSF! |
| `Earth nav data/` | `apt.dat` (1,037 lines), `atc.dat`, `nav.dat`, `Russia.xorg`, `+50+040/+56+040.dsf` (13.4 KB), `+56+040.txt` (1,243 lines, 51.7 KB), `mod_+56+040.dsf`/`.txt`, `+57+040.dsf` (1.2 KB), `.bak`/`.backup`/`.orig`/`.xmc` | `atc.dat` & `nav.dat` header: `# Converted by FS2XPlane 3.26`; `+56+040.txt` authored with `OverlayEditor 2.48` |
| `objects/` | **151 `.obj`**, **68 `.dds`**, **12 `.png`**, **2 `.lin`** + `taxylines/` (`Lines_TW.png`, `line_woc.png`, 2 `.lin`) | 6 named `.obj` + 145 FS2XPlane MD5-hashed `.obj`; 49 unique base texture names (+ `_LIT` night variants) |
| `facades/` | **149 `.fac`**, **16 `.png`**, `osm2xpFacadeSetDescriptor.xml` (1,898 lines) | OSM2XP v2.0.2 facade library (`buildingGeneric1..16`, `houseGeneric1..16`, `slopedHouse1..16`, `buildingComplexShape1..8`, `buildingGlass1..8`, `houseText1..8`) |
| `orthophoto/` | **15 `.png`** (4096×4096), **15 `.pol`** (`uubi_1..16` excluding `6`) | `DRAPED_POLYGON` tiles (`SCALE 75 75`, `LAYER_GROUP terrain +1`), ~2.3 MB PNG each |
| `plugins/GroundTraffic/` | `win.xpl`, `lin.xpl`, `mac.xpl` (32/64-bit), `ReadMe.txt` | Marginal's GroundTraffic v1.50 (**LGPL v2.1**), drives `ruscenery/tech/gazel-1.obj` along apron route |

**Geographic Overlap with `ivanovo2beam`:**
- `apt.dat` defines `UUBI Yuzhny` (elevation `413 ft` / `125.9 m`), runways `17/35`, `11/29`, `16/34`, helipad `H1`, 24 `WED_Taxiway` polygons, 30 `WED_RampPosition` stands.
- `earth.wed.xml` spans `lat 56.936173°..56.966847°N, lon 40.914296°..41.076329°E`.
- `+56+040.txt` DSF `OBJECT` placements span `lat 56.930917°..56.999681°N, lon 40.901768°..40.976774°E` (**350** objects inside the airport bbox `[56.93..56.96, 40.91..40.97]`, **41** objects extending north into Ivanovo city up to `56.99968°N, 40.97677°E`).
- `configs/ivanovo_shuya_v2.yaml` covers `32.764 × 32.764 km` centered at `[41.16958°E, 56.92439°N]` (approx. `±16.38 km` E/W/N/S). UUBI (`40.941°E, 56.942°N`) sits ~13.85 km west and ~1.95 km north of corridor center — **completely inside `ivanovo_shuya_v2`**!

### 2.2 UUBI Asset Catalog & OBJ8 / FAC Technical Structure

#### Local Airport & Ivanovo City Objects (`objects/`)
- **Named Hero Assets (6 `.obj`):**
  - `terminal.obj` (6.26 MB, 68,434 lines): **62,145 vertices**, 51 `TRIS` draw calls, bounds `X [-0.01, 107.07]`, `Y [-0.06, 12.27]`, `Z [-50.04, 8.33]` m (~107 m × 58 m × 12.3 m). Uses `uubiport.png` (**4096×4096**, 18.17 MB) + `uubiport_LIT.png`. Contains `ATTR_hard`, `ATTR_cull`, `ATTR_draw_disable`, and **no `ATTR_LOD`**! *(Critical performance hazard if imported raw into BeamNG without decimation/LOD + simplified `Colmesh-1`.)*
  - `tower.obj` (84 KB, 1,281 lines): **1,040 vertices**, 2,262 indices, bounds `X [-7.45, 10.78]`, `Y [0.00, 10.01]`, `Z [-8.31, 10.81]` m, using `uubitower.png` (**1024×1024**) + `uubitower_LIT.png`.
  - `Start.obj` (101 KB): **1,166 vertices**, 2,340 indices, `START.png` + `LIGHT_NAMED`.
  - `UWKB_Transf_10x5.obj` (6.1 KB): 72 vertices, 132 indices, `UWKB_Transf_10x5.png` (placed in `+56+040.txt` at `40.944615, 56.943473`).
  - `UWKB_Mobile_Fence.obj` (6.1 KB): 72 vertices, 114 indices, `ATTR_LOD 0..30000`.
  - `xudn_OB0-fence1.obj`: perimeter fencing.
  *(Note: `terminal.obj` and `tower.obj` sit on disk in `objects/` while `+56+040.txt` currently places `opensceneryx/.../terminals/concrete/1.obj`, `ruscenery/buildings/tower-u2.obj`, and hashed `objects/d1490fc56208b54599ebd31b71a18efe-tower.obj`; geo-pinned manifests can explicitly target either `objects/terminal.obj` or the DSF-placed hashed landmarks!)*
- **FS2XPlane Hashed City & Industrial Objects (145 `.obj`):**
  - Soviet residential series (`tipovuha00a/00b/02/03/05/07/09/11`, **39 `.obj`**, DXT1 `.dds` 256×256 + `_LIT`) — classic Khrushchevka/Brezhnevka panel blocks.
  - Ivanovo city blocks (`ivcity`, **4 `.obj`**, `ivcity.dds` 1024×1024 DXT1 + `_LIT`), including `ab42f275dd8d664aa44e49c7e7a55ee9-ivcity_0085.obj` (246 verts) and DSF-placed `0a49fb9342db7842bf87f37ea64af35e-ivcity.obj` (at `56.99968°N, 40.97677°E` in Ivanovo center).
  - Industrial chimneys (`chim2/2a/3a/4n/5n`, **6 `.obj`**, with `VLIGHT` aviation obstruction beacons up to `Y=131.39 m`), factories/warehouses (`factold`, `ind2`, `ind3`, `aba1`, `aba2`, `wha1`, `build_02`), `tower`, `textl29`.
  - Static Soviet aircraft (`an2`, `an2m`, `y52`, `y52m`, `y18t`, `c172`).

#### X-Plane OBJ8 (`I 800 OBJ`) & DSF Placement Schema
- **OBJ8 Geometry (`I\n800\nOBJ`):**
  - Vertex line: `VT x y z nx ny nz u v` (right-handed **Y-up**, metres: `+X` east/right, `+Y` up, `+Z` south/back; bottom-origin V in OpenGL convention).
  - Indices: `IDX10` / `IDX` referencing 0-based `VT` rows, rendered via `TRIS <offset> <count>` (note `ATTR_draw_disable` disables rendering for collision-only sub-blocks; `ATTR_hard` marks solid surfaces).
  - Textures: `TEXTURE <file>` (albedo), `TEXTURE_LIT <file>` (night emissive), `TEXTURE_NORMAL <file>` (optional).
  - LODs: `ATTR_LOD <near_m> <far_m>` partitions draw distance ranges.
- **DSF Placement (`+56+040.txt`):**
  - `OBJECT_DEF <resource_path>` (78 definitions) paired with `OBJECT <def_idx> <lon_deg> <lat_deg> <heading_deg>` (**391** placements).
  - Heading `hdg` in X-Plane DSF is **clockwise degrees from true north** (`0° = +North`, `90° = +East`).

#### Third-Party Library Dependencies in `+56+040.txt`

| Library Prefix | `OBJECT` Placements | `OBJECT_DEF` / `POLYGON_DEF` | Examples Used at UUBI | Local Availability |
| --- | ---: | ---: | --- | --- |
| `ruscenery/` | **335** (85.7%) | 37 OBJ + 1 FAC | `bush/2/3`, `house-d1-01..14`, `hothouse-1..4`, `Eq-Hangar`, `tower-u2`, `BPRM-*`, `Yak-42-9`, `An-2-l`, `Mi-8-4`, `zil_krm`, `uaz469`, `paz`, `ikarus250`, `gazel-2`, `belarus4` | **External library** (not bundled inside `UUBI_Ivanovo/`) |
| `opensceneryx/` | **35** (9.0%) | 17 OBJ | `terminals/concrete/1`, `cargo_terminals/concrete/1`, `hangars/metal/22`, `fuel/1`, `water_towers/cylindrical/2`, `wooden/2..18`, `chainlink/2.5m` | **External library** (not bundled inside `UUBI_Ivanovo/`) |
| `lib/` (X-Plane built-in) | **11** (2.8%) | 2 OBJ + 5 POL/FOR + `roads.net` | `lib/cars/car_static.obj`, `lib/airport/landscape/apron_light.obj`, `wet_cool_bor_flat*.for` | **X-Plane 12 built-in** |
| `objects/` (local + 1 broken) | **10** (2.6%) | 15 OBJ (`1` missing) | `UWKB_Transf_10x5`, `0a49...-ivcity`, `d149...-tower`, `2d78...-ivcity`, `f9e4...-appt`; note `objects/supermarkets/RET_95.00x117.00_UK.obj` is missing on disk | **Self-contained** (except `RET_95.00x117.00_UK.obj`) |

### 2.3 UUBI License & Attribution Risk Matrix

| Asset / Component | Origin / Evidence | Observed License | Risk Level | Redistribution Policy for `ivanovo2beam` |
| --- | --- | --- | --- | --- |
| `UUBI_Ivanovo` scenery (`objects/terminal.obj`, `tower.obj`, FS2XPlane `.obj`/`.dds`, `orthophoto/`) | `atc.dat`/`nav.dat` (`FS2XPlane 3.26`); `UWKB_*` borrowed files; no scenery `LICENSE` | **All rights reserved / unstated** (freeware community scenery + MSFS conversion + ortho imagery) | **HIGH** | **Local-only.** Never commit to git; never include in distributed level `.zip` without explicit author permission |
| `ruscenery/` & `opensceneryx/` references | DSF `OBJECT_DEF` external library paths | `ruscenery`: freeware non-commercial; `OpenSceneryX`: CC BY-NC / mixed | **HIGH** | **Reference-only / external user library.** Resolve only if user provides local X-Plane `Custom Scenery/` path |
| `facades/` (`osm2xp` v2.0.2) | `osm2xpFacadeSetDescriptor.xml` (`author: ben`) | Unstated inside package (OSM2XP assets) | **MEDIUM-HIGH** | **Ignore for `ivanovo2beam`** (we already have superior footprint-exact procedural buildings in `buildings/export.py`) |
| `plugins/GroundTraffic/` | `ReadMe.txt` (Jonathan Harris, 2013–2014) | **LGPL v2.1** | **LOW** (unused) | **Do not import** (X-Plane native `.xpl` binary; irrelevant to BeamNG/AC) |

---

## 3. Universal External Asset Import & Geo-Pinned Procedural Replacement System

To enrich `ivanovo2beam` with real landmarks (UUBI Ivanovo-South terminal/tower/hangars, Ivanovo brick factories, churches, Soviet panel series, CCD GOST signs/bus stops/AZS stations, or custom Blender models) **without** polluting git or violating third-party licenses, we design a clean, config-driven **External Asset Import & Geo-Pinned Replacement** subsystem (`src/ivanovo2beam/assets/`).

### 3.1 Isolated Local Registry & Git Hygiene

1. **Workspace Layout (strictly outside tracked source):**
   - `assets_external/` at repo root (added to `.gitignore` alongside `*.dae`, `*.dds`, `*.fbx`, `*.glb`, `*.obj` inside `assets_external/`).
   - **Manifest Registry (`assets_external/asset_manifest.yaml` or `--asset-manifest <path>`):** Tracked example schema (`configs/asset_manifest.example.yaml`) in git; actual local manifest + binary assets stay local or user-supplied.
   - **Library Mounts:** Manifest declares named external search roots (`uubi: "E:/SteamLibrary/.../UUBI_Ivanovo"`, `xplane_libs: "E:/SteamLibrary/.../Custom Scenery"`, `ccd: "F:/SteamLibrary/.../City Car Driving/export"`), avoiding copying raw archives into the repo.
2. **Provenance & Redistribution Gate:**
   - Every manifest entry carries `license` (`cc0`, `cc-by-4.0`, `mit`, `proprietary_local_only`), `author`, `source_uri`, and `sha256`.
   - `provenance.py` records external asset hashes in `build/levels/<id>/attribution.json`.
   - `cli.py package` / export gates check `allow_redistribute`: if any `proprietary_local_only` asset is included, the build is stamped `"redistributable": false` (local play only) and warns loudly if `--distributable` packaging is requested.

### 3.2 Manifest Schema (`asset_manifest.yaml`)

```yaml
version: 1
sources:
  uubi: "E:/SteamLibrary/steamapps/common/X-Plane 12/Custom Scenery/UUBI_Ivanovo"
  ccd: "F:/SteamLibrary/steamapps/common/City Car Driving/export"

assets:
  - id: uubi_terminal
    source: "${uubi}/objects/terminal.obj"
    format: xplane_obj8                  # dae | glb | fbx | xplane_obj8 | ccd_n2
    license: proprietary_local_only
    sha256: "..."
    category: building                   # building | aeroway | prop | landmark | vegetation
    placement:
      mode: geo_pinned                   # geo_pinned | dsf_import | osm_tag_rule
      lon: 40.94170
      lat: 56.94225
      yaw_deg: 212.0                     # compass deg CW from North, or "auto_osm_obb"
      anchor: terrain_max_corner         # terrain_center | terrain_max_corner | msl
      foundation_skirt_m: 3.0            # downward foundation plinth to prevent slope float
      scale_mode: none                   # none | uniform | fit_footprint_xy
    replaces:
      osm_ids: ["way/123456789"]         # explicit OSM way/node/relation IDs to suppress
      suppression_radius_m: 65.0         # fallback spatial suppression polygon/radius
    lod:
      enabled: true
      levels:                            # pixel/detail sizes in BeamNG COLLADA
        - { name: lod0, detail_px: 120, decimate_ratio: 1.0 }
        - { name: lod1, detail_px: 35,  decimate_ratio: 0.35 }
        - { name: lod2, detail_px: 8,   decimate_ratio: 0.08 }
      null_detail_px: 4
      mesh_culling: true
    collision:
      mode: proxy_hull                   # none | proxy_boxes | proxy_hull | colmesh_source
      max_triangles: 256
      ac_collider_prefix: "1WALL_"       # Assetto Corsa collider naming contract
    materials:
      cook_bc7_dds: true
      emissive_lit_suffix: "_LIT"
      ac_shader: "ksPerPixelNM"

  - id: ccd_bus_stop_glass
    source: "${ccd}/gfxlib/universal/produce/bus_station_glas_02_final.n2"
    format: ccd_n2
    license: proprietary_local_only
    category: prop
    placement:
      mode: osm_tag_rule                 # replaces procedural s_busstop_it.dae globally or in bbox
      osm_selector: { highway: "bus_stop" }
      offset_from_road_m: 1.2
      yaw_deg: "road_inward"
    lod:
      enabled: true
      levels:
        - { name: lod0, detail_px: 40, decimate_ratio: 1.0 }
      null_detail_px: 3
    collision:
      mode: proxy_boxes
      ac_collider_prefix: "1WALL_"
```

### 3.3 Pipeline Integration & Geo-Pinned Procedural Replacement

Integration hooks cleanly into `src/ivanovo2beam/build.py` **without altering `beamng/` core independence**:

1. **Suppression Pass (Before Chunking / Prop Placement):**
   - `buildings/export.py`: Accept `suppressed_osm_ids: set[int]` and `suppression_geoms: list[Polygon]` (buffered footprint or explicit `suppression_radius_m`). Any `BuildingFootprint` matching `osm_id` or intersecting a replacement footprint (`IoU > 0.25` or centroid inside) is **omitted before `_export_buildings_chunked` builds the 500 m `.dae` chunks**, guaranteeing zero Z-fighting or double-walls inside chunked static meshes.
   - `roads/props.py`: Accept `prop_replacements` keyed by `(osm_selector, osm_id / spatial zone)` so stock `s_busstop_it.dae`, `italy_traf_sign_*`, or `pole_light_single.dae` can be selectively swapped per-rule or per-coordinates while retaining `_SpatialDedupe` and `_is_shoulder_clear`.
2. **Coordinate Projection, Orientation & Terrain/Foundation Snap:**
   - Geographic coordinates `(lon, lat)` project via `RegionProjection.geo_to_world(lon, lat)` (`gis/projection.py`, exact Transverse Mercator centred on Ivanovo `[40.97393, 56.99987]`) into world `(x, y)` metres.
   - **Source-to-World Axis Normalization (`ExternalAssetIR`):**
     - **X-Plane OBJ8** (`Y-up, +X=east, +Z=south`) → BeamNG Z-up: `(x, y, z)_OBJ8 -> (x, -z, y)_BeamNG` (exact same `det = +1` conjugation as `ccd2beam/beamng/transform.py`!).
     - **glTF / GLB / standard FBX** (`Y-up`) → `(x, -z, y)`; **COLLADA** inspects `<up_axis>` (`Y_UP` vs `Z_UP`) and `<unit meter="...">`.
     - **Heading Conversion:** X-Plane DSF compass heading `hdg_cw_deg` (`0° = North/+Y`, `90° = East/+X`) maps to BeamNG CCW Z-yaw `yaw_rad = math.radians(90.0 - hdg_cw_deg)` (or `math.atan2` along OSM footprint major OBB axis when `yaw_deg: auto_osm_obb`).
   - **Terrain & Foundation Snap:** Sample `build.elevations` across the rotated XY bounding box corners + center (`anchor_z = max(ground_z, max_corner - 0.25)`), matching `buildings/export.py:617-621`. If the imported mesh bottoms out at `z = 0`, generate a downward concrete/masonry foundation skirt (`-skirt_depth_m`, min `-3.0 m`) using `bld_concrete_panel` / `bld_plaster_civic` so airport terminals or hillside buildings never float on 4 m DEM slopes.
   - **Footprint Fitting:** When `scale_mode: fit_footprint_xy` is used against an OSM polygon, clamp aspect ratio distortion (`<= 1.35×`) to prevent extreme texture/normal stretching; prefer `scale_mode: none` + `yaw_deg: auto_osm_obb` for surveyed hero buildings like `terminal.obj`.

### 3.4 Material Conversion, Multi-Level LOD & Collision Proxies

1. **Material Conversion & BC7 Cooking (`main.materials.json` v1.5):**
   - Emit asset-local `levels/<id>/art/shapes/external/<asset_id>/main.materials.json` with `version: 1.5`, deterministic `uuid5` `persistentId`, `groundType`, and `Stages[0]` PBR slots (`baseColorMap`, `normalMap`, `roughnessMap`, optional `opacityMap` / `alphaRef` for chainlink fences).
   - Convert X-Plane `TEXTURE_LIT` / CCD `SolidLuminescent` textures into `Stages[0].emissiveMap` + `emissiveFactor` (or night-switched `glowMap`) rather than spawning thousands of runtime point lights.
   - Cook non-DDS `.png`/`.tga` textures (e.g. `uubiport.png` 18 MB uncompressed PNG) into **BC7_UNORM (`DXGI_FORMAT=98`) DDS** with complete mip chains using `ispc-texcomp==1.0.1` (`ccd2beam`'s `texture_cooking.py` pattern: strip-bounded box filter + normal unit-vector re-normalization), cutting VRAM ~4–6× and eliminating runtime stalls.
   - Cache compiled `.dae` + `.dds` products by `(source_sha256, params_hash)` using content-addressed staging (`ccd2beam`'s `ArtifactCache` pattern).
2. **Multi-Level LOD & Draw-Call Hygiene:**
   - Raw assets like `UUBI`'s `terminal.obj` (**62,145 vertices**, 51 `TRIS` groups, 0 LODs) must **never** be dropped directly into BeamNG.
   - Consolidate redundant `TRIS` runs sharing the same texture/render state into **1 `<triangles>` block per material** (51 → 1–3 draw calls).
   - Emit Torque3D LOD hierarchy in Collada 1.4.1 (`base00` → `start01`):
     - `detail_<id>_120` (LOD0, full or welded mesh)
     - `detail_<id>_35` (LOD1, 35% planar/quadric decimation)
     - `detail_<id>_8` (LOD2, low-poly silhouette box)
     - `nulldetail_4` (culls completely below 4 px screen size)
   - Set `meshCulling: true` and `useInstanceRenderData: true` on `TSStatic`.
3. **Simplified Collision Proxies (`Colmesh-1` vs `Visible Mesh Final`):**
   - **Never use `collisionType: "Visible Mesh Final"`** on imported high-poly assets (62k-vert `terminal.obj` or 500 m building chunks cause huge physics memory spikes and camera-spin/collision hitches).
   - Instead, generate a simplified collision node inside the `.dae` under `base00/start01/Colmesh_<id>-1` + `base00/collision-1` (following `driver_training.zip` and `ccd2beam/beamng/collision.py`), and set `TSStatic.collisionType = "Collision Mesh"`.
   - Proxy generation modes:
     - `proxy_boxes`: 1–6 axis-aligned / oriented bounding boxes capturing main building volumes/wings (`12` tris per box, zero concave traps).
     - `proxy_hull`: 2D footprint extrusion or float32-safe quantized convex hull (`0.1 mm` quantization, `<= 256` triangles, preventing BeamNG JBeam node snagging as proven in `ccd2beam` M2).
     - `none`: for distant clutter, roof details, or non-collidable foliage.

### 3.5 Future Assetto Corsa (`FBX` / `KN5`) Export Compatibility

Designing the `ExternalAssetIR` and scene graph cleanly now ensures `ivanovo2beam` can target **Assetto Corsa (`ksEditor` / `KN5`)** without re-architecting the pipeline:

| Dimension | BeamNG.drive Target (Current) | Assetto Corsa (`FBX` → `ksEditor` → `KN5`) Target | Shared `ivanovo2beam` IR Adapter Strategy |
| --- | --- | --- | --- |
| **Units & Axes** | Right-handed **Z-up**, metres (`+X=East, +Y=North, +Z=Up`) | Right-handed **Y-up**, metres (`+X=East, +Y=Up, +Z=South`) | Single boundary transform `(x, y, z)_world -> (x, z, -y)_AC` (exact inverse of `CcdToBeamNg`) |
| **Floating-Point Origin** | World origin `[40.97393, 56.99987]` (1–3.9 mm ULP within 35 km; no floating origin) | Float32 physics/rendering degrades steeply past ~2–4 km (`ksEditor` single-sector track scale) | Sector/stage rebase parameter `ac_origin_xy = (ox, oy)`: subtract from all vertices, splines, and spawns before FBX export |
| **Road & Surface Colliders** | `TerrainBlock` heightmap + `DecalRoad` (`renderPriority`) + `Colmesh-1` | Mesh-based physics! Road ribbon & terrain meshes named **`1ROAD_*`**, **`1GRASS_*`**, **`1SAND_*`**, **`1KERB_*`** | Export graded road corridor ribbon + shoulder + curb TINs as physical mesh groups with AC numeric collider prefixes (`1` = collidable) |
| **Building / Barrier Colliders** | `TSStatic` with `Colmesh_<id>-1` (`Collision Mesh`) | Visual mesh (`0` / unnumbered, `isRenderable=true`) + simplified proxy mesh named **`1WALL_*`** (`isRenderable=false` in `surfaces.ini` / `ksEditor`) | Same `ExternalAssetIR` proxy hull/box generator emits `Colmesh_<id>-1` for BeamNG DAE **and** `1WALL_<id>` mesh node for AC FBX! |
| **Materials & Shaders** | `main.materials.json` v1.5 (`baseColorMap`, `normalMap`, `roughnessMap`, BC7 DDS) | `ksPerPixel`, `ksPerPixelNM`, `ksPerPixelAT`, `ksPerPixelMultiMap` (`txDiffuse`, `txNormal`, `txMaps` packing Spec/Rough/Detail in RGB) | Keep separate PBR source maps (`color`, `normal`, `roughness`, `ao`) in `ExternalAssetIR`; pack `txMaps` + DXT1/DXT5 DDS only in the AC FBX/material exporter |
| **AI Splines & Spawns** | `DecalRoad` `road_invisible` + `SpawnSphere` | `fast_lane.ai` / `pit_lane.ai` (left/right track width + centerline), empties `AC_START_0..N`, `AC_PIT_0..N`, `AC_TIME_0_L/R` | Rebase `roads/network.py` metric centrelines + lane widths directly to `(x - ox, z, -(y - oy))` for AC AI spline writer |

---

## 4. Recommended Implementation Sequence in `ivanovo2beam`

1. **Phase A (Core Registry & Suppression):** Add `src/ivanovo2beam/assets/manifest.py` + `assets_external/` in `.gitignore` + `suppressed_osm_ids` / polygon suppression hook in `buildings/export.py` and `roads/props.py`.
2. **Phase B (Importers & Proxy/LOD Builder):** Implement pure-Python `xplane_obj8.py` + `collada_import.py` (and optional `glb`/`ccd_n2` adapters) producing `ExternalAssetIR` with automatic `Colmesh-1` box/hull proxies, `detail_*` LODs, foundation skirts, and `ispc-texcomp` BC7 DDS cooking.
3. **Phase C (Pilot Landmarks — UUBI & Ivanovo):** Create a local-only test manifest placing `UUBI` `terminal.obj` (decimated + LODs + `1`-box collider), `tower.obj`, and select Ivanovo city landmarks inside `ivanovo_shuya_v2` to validate terrain/foundation snapping, draw-call impact, and zero `WarningMaterial`.
4. **Phase D (AC Sector Export Prototype):** Add `sectors/ac_export.py` exporting a rebased 2×2 km sector around `golden_sector` or `UUBI` to Y-up FBX (`1ROAD_*`, `1WALL_*`, `AC_START_0`, `fast_lane.ai`).
