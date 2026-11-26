# `ivanovo2beam` Long-Term Development Roadmap

**Project:** Reproducible 1:1 GIS → BeamNG.drive (and future multi-sim) generator for Ivanovo Oblast & Central Russia  
**Current Production Baseline:** `ivanovo_shuya_v2` (`8192 × 8192 @ 4.0 m`, `32.764 × 32.764 km`, `1,073.5 km²`, Single `TerrainBlock`, shared Transverse Mercator origin `[40.97393, 56.99987]`)  
**Regression Baseline:** `ivanovo_golden_sector` (`4096 × 4096 @ 1.0 m`, `4.095 × 4.095 km`, Kokhma SE / Р152 Uvod crossing)

---

## Architectural Anchor (Proven Empirical Baseline)

Live D3D12 benchmarking across four terrain architectures ([`p3_scaling_study.md`](p3_scaling_study.md), [`p2_multiblock_experiment.md`](p2_multiblock_experiment.md)) established the foundation that all subsequent phases build upon:

1. **Metric 1:1 Projection (`gis/projection.py`):** Transverse Mercator centered on `central_meridian = 41.10`, `latitude_of_origin = 57.00`, `origin = [40.97393, 56.99987]` (`~10.65 m / 100 km` worst-case geodesic error across `220 × 145 km`), ensuring every city, corridor, and future Oblast sector aligns byte-for-byte.
2. **Single `TerrainBlock` Ceiling (`8192 × 8192`):** Torque3D binds `core_terrain`, `castRayStatic`, `GroundCover`, and `DecalRoad` projection exclusively to block 0, and `.ter` v9 caps grids at `8192 × 8192`. Within a single level, `8192 × 8192 @ 4.0 m` (`32.764 km`) delivers smooth 5-channel `TerrainMaterial` blending, 8 native `GroundCover` layers, zero-copy `.link` forests, and superior FPS/VRAM/collision efficiency over baked Collada ground meshes.
3. **One-Way Pipeline Boundaries (`src/ivanovo2beam/`):** `gis` / `osm` / `terrain` / `water` / `roads` / `buildings` / `vegetation` emit engine-neutral spatial primitives assembled by `build.py` and serialized by `beamng/`. Future subsystems (architecture, full PDD, urban environment, traffic, multi-sector federation, Assetto Corsa) plug into this same layered boundary.

---

## 8-Phase Long-Term Roadmap

| Phase | Name | Primary Scope | Key Output |
|---|---|---|---|
| **1** | Corridor Stabilization & Optimization | Fix `DecalRoad` packing errors, cut bloat/hitches, verify unbroken round-trip driveability, clean level picker | Production-stable `ivanovo_shuya_v2` |
| **2** | Procedural Realistic Russian Architecture | Parametrized Russian houses (`наличники`), `хрущёвки`/`брежневки`, brick factories, fences, courtyards, landmark hooks | `buildings/` grammar + asset atlas + landmark registry |
| **3** | Full Russian Road System (PDD & GOST) | Complete GOST markings, Cyrillic signs, overhead gantries, traffic lights, turn lanes, intersection priorities | `roads/pdd.py`, dynamic sign atlas, signal phases |
| **4** | Urban & Highway Infrastructure + Urban Environment | Sidewalks, physical & dropped curbs (`бордюры`), parking pockets, embankments, bus bays, lighting, power/catenary lines | Dedicated `urban/` subsystem + night lighting |
| **5** | Street Names, Settlement Labels & POI Markers | Settlement entry boards, river plates, street plaques, in-game POI / BigMap / QuickTravel markers | Bilingual/Cyrillic POI & navigation overlay |
| **6** | Realistic AI Traffic | Lane-level PDD `navgraph`, speed/yield/signal rules, Russian traffic fleet hooks, scheduled transit buses | Realistic urban + Р152 highway traffic |
| **7** | Full 220 × 145 km Ivanovo Oblast 1:1 Scaling | Multi-sector `8192 × 8192` level federation with seamless border handoff across all Oblast cities | Seamless full-Oblast road network |
| **8** | Assetto Corsa Export & Russian World Generator | Decoupled `WorldIR`, Assetto Corsa `.kn5`/CSP exporter, generalized multi-region Russian World Generator | Cross-sim `world_ir` + regional presets |

---

### Phase 1 — Corridor Stabilization & Performance Optimization *(Current Session)*

**Goal:** Bring `ivanovo_shuya_v2` (`32.764 × 32.764 km`) to rock-solid, stutter-free daily-driver quality with zero engine errors and clean level management.

- **Zero `DecalRoad` Batch Errors & Reduced Bloat:** Eliminate all `SceneStaticDecalRoadManager` `2048 m / inf` packing overflow errors; drastically trim redundant edge-blend decals on narrow/minor streets while preserving visual shoulders.
- **Hydrology & Terrain Polish:** Clip and coalesce out-of-bounds `WaterBlock` tiling (`9,764` → ~`1,000–2,000` tiles), eliminate `4.0 m` grid grass-through-asphalt bleed in towns, and soften `Field`/`Meadow`/`ForestFloor` transitions.
- **Draw-Call, Collision & Camera-Spin Freeze Elimination:** Thin/cull redundant wooden utility poles on minor dead-ends, swap `Visible Mesh Final` high-poly prop collisions for lightweight `Colmesh`/box proxies or LOD culling, fix bridge guardrail placement, and eliminate fast 360° camera-spin frame spikes.
- **Unbroken Round-Trip Driveability:** Diagnose and resolve the AI Leg 2 crash, verify all 82 bridges and rural/urban surfaces, and pass a continuous unbroken **Ivanovo → Kokhma → Shuya → Kokhma → Ivanovo** drive at realistic speeds with `damageSum = 0`.
- **Level Picker Cleanup & Previews:** Human-friendly titles/descriptions, real in-game `preview.png` thumbnails (`src/ivanovo2beam/beamng/level.py`), and `scripts/cleanup_beamng_levels.py` to remove obsolete experimental builds while preserving `ivanovo_golden_sector` and `ivanovo_shuya_v2`.

---

### Phase 2 — Procedural Realistic Russian Architecture

**Goal:** Replace untextured prism extrusions with recognizable Central Russian urban, suburban, and village architecture that scales across every settlement while supporting custom hero landmarks.

- **Private Houses (`частный сектор` / `избы`):**
  - Timber log/clapboard and brick single-story / attic dwellings (`1–2` floors) parameterized by OSM footprint aspect ratio, `building=house/detached`, and `building:material`.
  - Procedural **carved window casings (`резные наличники`)** with regional Ivanovo/Shuya motif variants, multi-pane wooden/PVC windows with sills and shutters, hipped/gabled corrugated-metal (`профнастил`) and slate (`шифер`) roofs, brick chimneys, and covered porches (`крыльцо`).
  - Plot-level perimeter **fences (`заборы`)** (painted picket, corrugated steel sheet, concrete slabs), sheds/garages, and backyard garden textures.
- **Panel & Brick Apartment Blocks (`хрущёвки`, `брежневки`, modern infill):**
  - Parametric facade grammar keyed on `building:levels` and footprint dimensions: 5-story brick/panel `хрущёвки` (1-447 / 1-464 series), 9-story `брежневки` (111-90 / 1-515), recessed & glazed **balconies (`балконы`/`лоджии`)** with randomized glazing/awning variation, stairwell window columns, entrance canopies (`козырьки подъездов`), concrete entry steps, and rooftop elevator/ventilation penthouses.
  - Ground-floor commercial plinths (`первые этажи`) along major streets (`shop=*`, `amenity=*`) with glazed storefronts, fascia signboards, and service doors.
- **Industrial & Civic Heritage:**
  - Red-brick **Ivanovo textile mills (`краснокирпичные мануфактуры`)** with tall arched/segmented industrial glazing, brick pilasters, clerestory/sawtooth roofs, and concrete `ПО-2` perimeter fences.
  - Garage cooperatives (`ГСК` — rows of sheet-metal/brick garage boxes), warehouses, and Soviet public buildings (schools, clinics, houses of culture).
- **Cross-Settlement Reusability & Dedicated Landmark Hooks:**
  - Shared PBR **Architectural Material & Trim Atlas** (`buildings/atlas.py`) instanced across all settlements to minimize VRAM and draw calls, still batched into spatial chunks (`250–500 m`).
  - **Landmark Override & External Asset Hooks (`src/ivanovo2beam/assets/`, `asset_manifest.yaml`):** matches OSM `wikidata`, `name`, `aeroway`, or way/relation ID to substitute bespoke `.dae`/external hero models + LODs (e.g., Ivanovo-South Airport `UUBI` terminal/tower, Ivanovo & Shuya railway stations, Resurrection Cathedral belltower, historic river bridges) cleanly into the scene tree. See [`research/asset_reuse.md`](research/asset_reuse.md).

---

### Phase 3 — Full Russian Road System (PDD & GOST Fidelity)

**Goal:** Bring road geometry, markings, signage, and intersection control up to authentic Russian **ПДД** and **ГОСТ Р 52289 / 51256 / 52290** standards.

- **Complete GOST Markings:**
  - Full taxonomy: `1.1` solid, `1.2` edge, `1.3` double solid, `1.5`/`1.6` dashed lane/approach lines, `1.11` pass-one-side, `1.12` stop line, `1.13` yield triangles, `1.14.1`/`1.14.2` zebra crosswalks (white + high-visibility yellow background), `1.16` channelizing islands, `1.18` lane-turn arrows, `1.20` yield-ahead markings, `1.23.1` bus lane `А`, and railway crossing boxes.
- **Full GOST R 52290 Road Signs & Overhead Gantries:**
  - Priority (`2.1 Главная дорога`, `2.4 Уступите дорогу`, `2.5 Движение без остановки запрещено`), prohibition (`3.24` speed limits, `3.20` no overtaking), mandatory (`4.1.x`), special (`5.19` crosswalks, `5.16` bus stops, `5.5/5.7` one-way), and warning (`1.x` curves, rail crossings, pedestrian zones).
  - **Dynamic Cyrillic Destination Plates & Overhead Gantries (`рамные опоры`):** procedural texture-atlas / UV-mapped blue (`6.10.1` intercity: `Шуя`, `Иваново`, `Владимир`, `Кинешма`) and white (`6.10.2` urban streets) signs mounted on arterial truss/cantilever gantries at Р152 interchanges.
- **Traffic Lights (`светофоры`) & Intersection Priorities:**
  - Russian 3-aspect vehicle heads (`Т.1`), additional turn-arrow sections, and pedestrian countdown heads (`П.1`) placed from OSM `highway=traffic_signals` and major arterial junctions.
  - Explicit intersection topology graph exporting both visual priority rules and BeamNG signal/yield state for AI traffic.
- **Lane Geometry:**
  - Left/right turn pockets, acceleration/deceleration tapers at Р152 interchanges, splitter islands, and multi-lane roundabout geometry (`кольцевое движение`).

---

### Phase 4 — Infrastructure & Urban Beautification

**Goal:** Transform bare road ribbons and building boxes into complete, lived-in Russian urban and highway corridors.

- **Nighttime & Street Lighting:** Emissive luminaire materials + distance-culled BeamNG dynamic `SpotLight`/`PointLight` pools along Р152, bridges, crosswalks, and city avenues, synchronized with `TimeOfDay`.
- **Overhead Power Lines & Catenary Networks:** Catenary-sagging wires (`ЛЭП` 0.4/10 kV along rural corridors and Ivanovo trolleybus contact wires with span poles and cross-arms) rendered as ultra-light LOD line/ribbon meshes without collision.
- **Bus Stops, Parking & Highway Service:** Detailed Russian bus-stop shelters (`остановочные павильоны`), roadside rest bays, fuel stations, crash cushions, and bridge/embankment W-beam guardrails.
- **Urban Beautification:** Street trees in sidewalk pits, park benches, trash receptacles, pedestrian guardrails (`пешеходные ограждения` near crosswalks per GOST), billboards, and courtyard play/utility zones.

---

### Dedicated Architectural Section: Procedural Urban Environment Subsystem (`src/ivanovo2beam/urban/`)

Because `TerrainBlock` at `2.0–4.0 m` grid spacing cannot resolve `15 cm` vertical curbs, `1.5–3.0 m` sidewalks, or crisp courtyard cutouts, urban hardscape must be generated by a dedicated **Procedural Urban Environment Subsystem** rather than forced into heightmap cells or `DecalRoad` hacks.

#### 1. Subsystem Scope & Generated Elements
| Element | Russian Term | Geometry & Placement Rules |
|---|---|---|
| **Sidewalks** | `тротуары` | Continuous paved ribbons (`1.5–4.0 m` wide) offset from urban carriageway edges and OSM `highway=footway/sidewalk`, paved with asphalt or concrete pavers (`плитка`). |
| **Physical Curbs** | `бордюры` | Extruded granite/concrete curb profiles (`height = 0.12–0.15 m`, `width = 0.15 m`) bounding carriageways, traffic islands, and green medians. |
| **Dropped Curbs** | `пониженные бордюры` | Smooth `1.0–1.5 m` sloped curb transitions (`0.00–0.02 m` lip) automatically spliced wherever pedestrian crosswalks (`crossing=*`), courtyard driveways, or parking aprons intersect a curbline, paired with yellow tactile paving plates. |
| **Urban Lawns** | `газоны` | Polygonal turf strips (`придорожные газоны` between curb and sidewalk + inner courtyard lawns) suppressed from wild forest/tall-weed layers and seeded with trimmed lawn `GroundCover`. |
| **Courtyard Driveways** | `дворовые проезды` | `3.5–5.5 m` intra-block asphalt lanes linking arterial streets to apartment entrances (`addr:*` / `service=driveway`) with dropped-curb aprons. |
| **Parking Pockets** | `парковочные карманы` | Recessed parallel or angled (`45°`/`90°`) bays carved beside carriageways and commercial blocks with painted stall lines and wheel stops. |
| **Pedestrian Paths** | `пешеходные дорожки` | Park, square, and courtyard footpaths connecting building entrances to bus stops and crosswalks. |
| **Riverfront Embankments** | `набережные` | Terraced stone/concrete retaining walls, upper promenade decks, stairs, and pedestrian railings along urban Uvod & Teza riverfronts. |
| **Bus-Stop Pads** | `посадочные площадки` | Widened pull-out pockets (`заездные карманы`) with raised `0.18 m` boarding platforms, tactile edge strips, and shelter foundations. |

#### 2. Integration Contracts (Upstream & Downstream)
```
  roads/network.py (carriageways, widths, junctions, radii) ──┐
  roads/pdd.py     (crosswalks, stop lines, bus bays)        ──┼──► urban/layout.py (planar 2D urban polygons & curblines)
  buildings/       (footprints, entrances, courtyards)       ──┤         │
  water/extract.py (urban riverbanks)                        ──┘         ▼
                                                                 urban/mesh.py (height-sampled 3D curbs, sidewalks, skirts)
                                                                         │
                                          ┌──────────────────────────────┴──────────────────────────────┐
                                          ▼                                                             ▼
                        urban/export.py (batched chunk .dae + LOD + Colmesh)         terrain/builder.py (urban mask: suppress wild GroundCover/trees)
```

- **With Road Graph:** Reads exact carriageway edge polylines (including intersection fillet radii) so sidewalks/curbs never overlap asphalt lanes.
- **With Russian PDD:** Queries crosswalk centerlines, bus-stop zones, and driveway junctions to carve dropped curbs (`пониженные бордюры`), align zebra aprons, and keep sign/traffic-light poles cleanly inside the sidewalk utility strip (`0.5 m` behind curb face).
- **With Buildings & Infrastructure:** Clips sidewalks and courtyard driveways against building footprints (`buildings/footprints.py`), connects entrance nodes (`entrance=*`), and provides exact Z-elevated anchor pads for shelters, streetlights, benches, and railings.

#### 3. LOD, Spatial Batching & Collision Optimization
To avoid repeating the `TSStatic` / `Visible Mesh Final` bottleneck seen in naive per-prop exports:
- **Spatial Chunk Batching:** All urban meshes inside a `250 m × 250 m` (or `500 m × 500 m` building-aligned) urban tile are merged into **at most 2–3 draw-call-batched `.dae` objects** per tile (e.g., `urban_hardscape`, `urban_curbs`, `urban_furniture_static`) sharing one PBR trim-sheet material library.
- **Explicit 3-Tier LOD:**
  - `LOD0` (`0–120 m`): full curb chamfer, tactile paving strips, retaining-wall coping, embankment railings.
  - `LOD1` (`120–400 m`): flat-sided curb ribbons, railings & small bollards culled.
  - `LOD2` (`> 400 m`): culled (`null` detail); underlying `TerrainMaterial` urban asphalt/lawn base keeps distant vistas seamless.
- **Optimized Collision (`Colmesh-1`):**
  - Sidewalks, driveways, parking pockets, and bus-stop pads emit simple planar quad collision strips (`Colmesh-1`) rather than `Visible Mesh Final`.
  - Curbs use simplified single-bevel collision boxes (`3` faces instead of high-poly rounded profiles), and dropped curbs use smooth ramp triangles so vehicle wheels mount driveways/crosswalks cleanly without suspension spikes.
  - Embankment railings/bollards either share coarse box colliders or use `collisionType="None"` for thin decorative bars when backed by a continuous parapet collider.
- **Anti-Bleed Skirts & Z-Fighting Discipline:** Sidewalk/curb outer edges include a `-0.30 m` downward skirt into the `4.0 m` terrain grid so grass/soil slopes never expose floating edges, and surfaces maintain deterministic `>= 2 cm` / `decalBias` separation from `DecalRoad` layers.

---

### Dedicated Architectural Section: Universal External Asset Import & Geo-Pinned Replacement (`src/ivanovo2beam/assets/`)

Alongside procedural architecture and urban hardscape, [`research/asset_reuse.md`](research/asset_reuse.md) specifies how externally authored Russian assets (e.g., local-only City Car Driving / `ccd2beam` prop/PDD libraries and X-Plane 12 `UUBI_Ivanovo` airport/city sceneries located inside `ivanovo_shuya_v2` bounds, or future open-licensed Blender/CC-BY packs) integrate without polluting git or licensing:

1. **Strict License Isolation & Local-Only Distribution Gate:**
   - All third-party/proprietary source assets live strictly under a gitignored `assets_external/` directory governed by `asset_manifest.yaml` (SHA-256 checksums + explicit `allow_redistribute` flag). Proprietary assets are never committed to git or bundled into public `.zip` releases; local developer builds resolve them cleanly via manifest lookup.
2. **Pre-Chunk Geo-Pinned Procedural Suppression:**
   - Before `buildings/export.py` batches `500 m` building chunks or `roads/props.py` places stock signs/shelters/poles, registered manifest entries suppress overlapping OSM way/relation IDs or footprint polygons so hero landmarks (`UUBI` terminal/control tower, historic stations, custom PDD gantries) replace generic extrusions with zero double-geometry overlap.
3. **Transform, Material & Collision Normalization:**
   - **Placement:** WGS84 → Transverse Mercator geo-pinning (`auto_osm_obb` yaw alignment) with `terrain_max_corner` elevation sampling and automatic `3.0 m` foundation skirts for sloped ground.
   - **Cooking & LOD:** `(x, -z, y)` `det=+1` axis alignment, BC7_UNORM DDS + `_LIT` emissive night maps, and Torque3D `detail_*` LOD tiers (`meshCulling`).
   - **Safe Collision (`Colmesh-1`):** High-poly imported meshes never default to `Visible Mesh Final`; instead the importer generates or binds `proxy_boxes` / `proxy_hull` `Colmesh-1` colliders (`<= 256` tris), preventing physics/camera-spin stalls.
4. **Shared `ExternalAssetIR` for Multi-Sim Export:** Normalizing imports into `ExternalAssetIR` allows the exact same geo-pinned catalog to target both BeamNG `.dae`/`items.level.json` and Phase 8 Assetto Corsa `FBX`/`KN5` (`1ROAD_*`/`1WALL_*`, `ksPerPixel*`).

---

### Phase 5 — Street Names, Settlement Labels, Landmarks & In-Game POI / Map Markers

**Goal:** Give the driver full spatial orientation and Russian geographic identity inside BeamNG.drive.

- **Settlement Entry/Exit Signs:** GOST `5.23.1`/`5.25` white-background urban boards (`Иваново`, `Кохма`, `Шуя`) and `5.23.2`/`5.24.2` blue-background village boards (`Китово`, `Егорий`, `Клочково`) placed from OSM `place=city/town/village` boundaries.
- **Bridge River Plates & Street Plaques:** River crossing signs (`р. Уводь`, `р. Теза`, `р. Талка`) at bridge portals; street name plates (`ул. Лежневская`, `Кохомское шоссе`, `ул. Свердлова`) on major intersections and corner facades.
- **In-Game POI & BigMap Navigation:** Curated `SpawnSphere` / `QuickTravel` network covering city centers, railway stations, industrial landmarks, and highway interchanges; road-name & district annotations exported into BeamNG `map.json` / `info.json` / mission POI hooks for BigMap navigation.

---

### Phase 6 — Realistic AI Traffic

**Goal:** Enable believable, rule-abiding Russian road traffic across both dense urban streets and the 33 km Р152 highway.

- **Lane-Level Topology & PDD Constraints:** Export directional `oneway`, lane count, turn-only constraints, settlement/sign speed limits (`60 km/h` urban, `90 km/h` rural Р152, `40 km/h` school/bridge zones), and intersection priority (`main` vs `yield` vs `stop` vs signalized) into BeamNG `navgraph` metadata.
- **Traffic Density & Zone Profiles:** Dynamic spawn zoning so multi-lane city arterials (`Кохомское шоссе`, `Шуя`) carry steady urban flow while narrow village tracks see sparse local traffic.
- **Vehicle Mix & Transit Routes:** Configurable vehicle-group hooks supporting period-appropriate / Russian & European car pools (sedans, Lada-class hatchbacks, commercial vans, trucks) plus scheduled bus loops stopping at `s_busstop` pads.

---

### Phase 7 — Full 220 × 145 km Ivanovo Oblast 1:1 Scaling (Seamless Travel)

**Goal:** Scale from the 33 km Ivanovo–Shuya corridor to the entire `220 × 145 km` Ivanovo Oblast (`~31,900 km²`) while preserving `1:1` scale and seamless road travel.

- **Multi-Sector `8192 × 8192` Level Federation:**
  - Respecting BeamNG's hard `8192 × 8192` `.ter` limit and single-`TerrainBlock` constraint, partition the Oblast into an overlapping grid of `8192 × 8192 @ 4.0 m` (`32.764 × 32.764 km`) sector levels anchored to the **exact same Oblast Transverse Mercator frame** (`origin = [40.97393, 56.99987]`).
  - Priority corridor sectors: (1) Ivanovo–Kokhma–Shuya (active), (2) Ivanovo–Furmanov–Privolzhsk–Plyos (Volga), (3) Shuya–Palekh–Yuzha, (4) Ivanovo–Vichuga–Kineshma–Navoloki, (5) Kineshma–Yuryevets, (6) Ivanovo–Teikovo–Gavrilov Posad.
- **Seamless Border Overlap & Lua State Handoff:**
  - Each sector carries a `2–4 km` overlapping road/terrain buffer beyond its logical border so horizons never end in a cliff or void.
  - Highway border triggers invoke a lightweight BeamNG Lua gateway module that captures vehicle state (velocity vector, yaw, steering, gear, fuel, damage, active route destination), loads the adjacent sector level at the exact metric border coordinate, and restores momentum seamlessly—giving uninterrupted Oblast-wide road trips (e.g., Ivanovo → Plyos or Ivanovo → Kineshma → Yuryevets).

---

### Phase 8 — Assetto Corsa Export & Generalized Russian World Generator

**Goal:** Decouple geodata ingestion and procedural world synthesis from BeamNG-specific formats so the generator supports multiple simulators and any Russian region.

- **Simulator-Agnostic `WorldIR` Layer:** Refactor `terrain/`, `roads/`, `urban/`, `buildings/`, `water/`, `vegetation/`, and `assets/` (`ExternalAssetIR`) to emit a clean intermediate representation (`WorldIR`: metric heightfield/road ribbons, PDD graph, procedural building/urban instances, geo-pinned external assets, biome masks, material IDs).
- **Assetto Corsa Exporter (`src/ivanovo2beam/exporters/assettocorsa/`):**
  - Export sector-rebased Y-up (`(x, z, -y)`) road/curb/terrain mesh strips with `1ROAD_*`, `1WALL_*`, and `GRASS`/`GRAVEL` collider naming plus `ksPerPixel*` (`txMaps`) / `ksTree` shaders into `.fbx`/`.kn5` sector packs.
  - Generate `fast_lane.ai` / `pit_lane.ai` / `surfaces.ini` splines directly from `roads/network.py`, CSP `GrassFX`, `RainFX`, `LightsFX`, and `ui_track.json` for long-distance freeroam / Russian highway driving (e.g., Kokhma–Shuya–Plyos).
- **Generalized Russian World Generator:**
  - Package regional biome presets (Central Plain, Volga Riverfront, Northern Taiga, Steppe), regional architectural trim packs, and GOST rule sets so generating any Russian city or Oblast requires only a YAML region config (`configs/regions/<name>.yaml`) and open OSM/DEM inputs.
