# BeamNG.drive engine limits relevant to a 1:1 Ivanovo Oblast map

**Purpose.** This document is the evidence base for choosing an architecture for `ivanovo2beam`, which must
produce a **1:1 (1 game metre = 1 real metre)** map of Ivanovo Oblast, Russia — approximately
**220 km × 145 km ≈ 31,900 km²** — from OpenStreetMap + Copernicus DEM.

**Environment examined (all local claims):**

| Item | Value |
| --- | --- |
| BeamNG.drive version | **0.39.4.0.20972** |
| Install | `F:\SteamLibrary\steamapps\common\BeamNG.drive` |
| User data | `C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current` |
| Engine binary inspected | `Bin64\BeamNG.drive.x64.exe` (46,154,872 bytes, PE, x64) |
| Shipped level zips | `content\levels\*.zip` (21 archives) |
| Third-party map mods surveyed | 21 map mods in `mods\repo\*.zip` **+** `mods\Nordschleife tg_m0dsbeamng.zip` (22 total) |

**Method.** Evidence is (a) verbatim bytes read in place from level `.zip` archives via Python `zipfile`
(no archives were extracted to disk), (b) verbatim Lua source under `lua\`, and (c) verbatim strings and
symbol names read out of the shipped engine executable with a byte-offset scan. Every read of the executable
is reported with its byte offset so it can be re-verified. Web sources are cited with URLs.

**A critical correction to the brief.** The task assumed levels are authored as `main.mis` TorqueScript files.
**That is no longer true in 0.39.x.** A recursive scan of the entire `content\` tree found **zero `.mis` files**.
Modern levels are stored as a JSON scene tree under `levels/<name>/main/**/items.level.json` plus a `map.json`.
Legacy `.mis` files are still *loadable* (the level loader explicitly falls back to them), and one third-party
map mod in the local repo still ships `.mis`; that file is quoted in §A1 because it contains the only
`TerrainBlock` block on this machine that uses the old `squareSize`/`MaxHeight` TorqueScript syntax.

Source: local file `F:\SteamLibrary\steamapps\common\BeamNG.drive\lua\ge\extensions\core\levels.lua` lines 39–55 (BeamNG 0.39.4.0.20972)
Source: local file `C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\mods\repo\mammoth12.zip` → `levels/mammoth/mammoth.mis` (BeamNG 0.39.4.0.20972)

---

## Summary table of claims

Confidence key: **verified-local** = read directly from the 0.39.4.0.20972 install or from data shipped with it;
**verified-web** = stated by official BeamNG documentation; **unverified** = not confirmed, do not build on it.

| # | Claim | Value | Confidence | Source |
| --- | --- | --- | --- | --- |
| 1 | Maximum heightmap / terrain grid resolution | **8192** (square, power of two) | verified-local | Engine string `"Height map must be between 128 and 8192 in size!"` at exe offset 32979392 |
| 2 | Minimum heightmap / terrain grid resolution | **128** | verified-local | Same string, exe offset 32979392 |
| 3 | Must be square and power of two | yes | verified-local | Engine string `"Height map must be square and power of two in size!"` at exe offset 32979336 |
| 4 | Real-world extent of one TerrainBlock | `terrainSize × squareSize` metres (max 8192 m at squareSize 1) | verified-local + verified-web | `.ter` header + `TerrainBlock.squareSize`; docs "world size = size x squareSize" |
| 5 | Observed `squareSize` values | 0.3, 0.5, 0.75, 1.0 (default); editor default for a *new* TerrainBlock is `8` | verified-local | `items.level.json` of derby / automation_test_track / hirochi_raceway+johnson_valley; `createObjectTool.lua:852` |
| 6 | Hard engine min/max for `squareSize` | **not found** — no validation string exists in the binary | verified-local (absence) | Byte scan of the executable; only a `GroundPlane` `squareSize` clamp exists |
| 7 | Height storage | `u16[size×size]`; `heightMeters = stored × (maxHeight / 65536)` | verified-local + verified-web | `.ter` layout byte-accounting; docs "Height scale" |
| 8 | `.ter` current version / payload | version **9**; `u8 ver, u32 size, u16[size²], u8[size²], u32 matCount, strings` = 3 bytes/cell + 5 + trailer | verified-local + verified-web | Exact byte accounting on 7 shipped `.ter` files; engine `binaryFormat` string |
| 9 | `layerTextureMap` array in the v9 payload | **does not exist** (only mentioned in the descriptive `binaryFormat` string) | verified-local + verified-web | payload == exactly `3 × size²` for every file tested |
| 10 | Terrain material count limit | **254** (255 reserved for "hole"/empty) | verified-local + verified-web | Engine string `"Terrain have to many materials, material will be ignored after the 254th"` at 32976144; docs "Material count limit" |
| 11 | Largest TerrainBlock in BeamNG's own shipped content | `terrainSize` **4096** (italy, johnson_valley, automation_test_track) | verified-local | `theTerrain.terrain.json` in `italy.zip`, `johnson_valley.zip` |
| 12 | Largest TerrainBlock in real community mods | `terrainSize` **8192** (NEFR, river_highway, sanok, americanroad) | verified-local | `terrain2.ter` header bytes in `NEFR_0391.zip`, `river_highway.zip`, `sanok_repo.zip`, `americanroad.zip` |
| 13 | Largest real-world extent of any map observed on this machine | **8192 m × 8192 m** (NEFR, river_highway) | verified-local | 8192 grid × squareSize 1 |
| 14 | TerrainBlock count per level | 1 in every shipped level and every map mod surveyed (one mod has 4 *duplicate* blocks at one position — author error) | verified-local | scan of all `items.level.json` |
| 15 | `roadWidth`, `fadeStartBit`, `fadeEndBit` fields on DecalRoad | **do not exist in 0.39** — zero occurrences in the engine binary and zero in all level data | verified-local (absence) | byte scan; field histogram over all shipped DecalRoads |
| 16 | DecalRoad node encoding | `nodes: [[x, y, z, width], …]` in JSON; `Node = "x y z width";` in legacy `.mis` | verified-local + verified-web | Utah `roads/items.level.json`; `mammoth.mis`; official docs |
| 17 | Largest DecalRoad dataset observed | west_coast_usa: **12,752 DecalRoads / 59,557 nodes** | verified-local | full parse of `west_coast_usa.zip` |
| 18 | Largest object count observed in a level file | west_coast_usa: **41,513 objects** (+2,198 more inside `.prefab` files) | verified-local | full parse of `west_coast_usa.zip` |
| 19 | Largest Forest item count observed | johnson_valley: **663,057 items** across 76 `.forest4.json` files | verified-local | full parse of `johnson_valley.zip` |
| 20 | Hard limit on total object count / draw calls | **none found** — no `maxObjects` / "too many objects" string in the binary | verified-local (absence) | byte scan |
| 21 | MeshRoad | class is registered in the engine; **zero instances in all 21 shipped levels and all 21 map mods surveyed** | verified-local | `fnMeshRoad_create` / `TypeMeshRoad` in exe; full data scan |
| 22 | Runtime level-object streaming | **no `LevelObjectStreaming` API exists**; scene objects can be created at runtime via `createObject`/`PrefabV2`/`spawnPrefab`, and `DynamicProps` implements view-range pooling | verified-local | Lua source; byte scan found no such string |
| 23 | Floating origin / world rebasing | **no evidence of a floating-origin system**; world positions behave as `f32` | verified-local (absence) + verified-local | byte scan for `floatingOrigin`/`rebase`/`worldOrigin`; `lua\console\unittests.lua` float precision assertion |
| 24 | `mod_info/<id>/info.json` required for a map mod | **no** — the largest map mod on this machine has none | verified-local | 21 map mods surveyed; `levels.lua:25` skips `/levels/mod_info` |
| 25 | Recommended map-mod zip root | `levels/<name>/…` at the archive root | verified-web + verified-local | docs section 10.2; every map mod surveyed |
| 26 | Max level extent enforced anywhere as a single number | **not found** — there is no world-extent cap, only the 8192 grid cap | verified-local (absence) | byte scan |
| 27 | DecalRoad vertex/batch limits exist | yes, with runtime-formatted numbers I could not recover | verified-local + unverified value | exe 32663600 and 32663775 |

---

## A. TerrainBlock

### A1. Verbatim TerrainBlock blocks

#### A1.1 The modern JSON form — `template.zip` (the canonical starting point)

File: `levels/template/main/MissionGroup/level_objects/terrain/items.level.json` — two lines, quoted in full:

```
{"class":"TSStatic","__parent":"terrain","position":[0,0,115.727997],"collisionType":"Visible Mesh Final","decalType":"None","isRenderEnabled":false,"shapeName":"/levels/template/art/shapes/misc/s_template_backdrop.dae","useInstanceRenderData":true}
{"name":"theTerrain","class":"TerrainBlock","persistentId":"a500d9a8-fb60-4a81-b438-f9f9593e8f7c","__parent":"terrain","position":[-512,-512,100],"materialTextureSet":"templateTerrainMaterialTextureSet","maxHeight":120,"terrainFile":"/levels/template/theTerrain.ter"}
```

Source: local file `F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\template.zip` → `levels/template/main/MissionGroup/level_objects/terrain/items.level.json` (BeamNG 0.39.4.0.20972)

#### A1.2 Three real large levels, quoted verbatim

`east_coast_usa` (`levels/east_coast_usa/main/MissionGroup/Level_object/terrain/items.level.json`):

```
{"name":"theTerrain2","class":"TerrainBlock","persistentId":"6639f0ae-89f9-4134-8285-c305155e33aa","__parent":"terrain","position":[-1024,-1024,0],"baseTexSize":2048,"materialTextureSet":"east_coast_usaTerrainMaterialTextureSet","maxHeight":120,"minimapImage":"levels/east_coast_usa/east_coast_minimap.png","terrainFile":"/levels/east_coast_usa/theTerrain2.ter"}
```

`west_coast_usa` (`levels/west_coast_usa/main/MissionGroup/LevelObjects/items.level.json`):

```
{"name":"theTerrain","class":"TerrainBlock","persistentId":"1797e4d4-23c8-4bd8-b977-4414702b7879","__parent":"LevelObjects","position":[-1023.44916,-1023.5,0],"baseTexSize":2048,"materialTextureSet":"west_coast_usaTerrainMaterialTextureSet","maxHeight":384.640015,"minimapImage":"levels/west_coast_usa/westcoast_minimap.png","terrainFile":"/levels/west_coast_usa/west_coast_usa.ter"}
```

`italy` (`levels/italy/main/MissionGroup/Level_objects/items.level.json`):

```
{"name":"theTerrain","class":"TerrainBlock","persistentId":"157d5cca-2f5f-48ca-a705-d61be90f7067","__parent":"Level_objects","position":[-2048,-2048,100],"baseTexSize":4096,"materialTextureSet":"italyTerrainMaterialTextureSet","maxHeight":500,"minimapImage":"levels/italy/italy_minimap.png","terrainFile":"/levels/italy/theTerrain.ter"}
```

`johnson_valley` — the only shipped level that sets `squareSize` explicitly (`levels/johnson_valley/main/MissionGroup/level_object/terrain/items.level.json`):

```
{"name":"theTerrain","class":"TerrainBlock","persistentId":"a21a0738-5687-455e-b599-732c02dddb88","__parent":"terrain","position":[-1534.39136,-1533.79248,99.9394836],"baseTexSize":4096,"materialTextureSet":"johnson_valleyTerrainMaterialTextureSet","maxHeight":200,"squareSize":0.75,"terrainFile":"/levels/johnson_valley/theTerrain.ter"}
```

Source: local files in `F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\{east_coast_usa,west_coast_usa,italy,johnson_valley}.zip` (BeamNG 0.39.4.0.20972)

#### A1.3 The legacy `.mis` form (for reference only — not used by 0.39 content)

From `mammoth2.mis` inside a third-party map mod. This is the only TorqueScript `TerrainBlock` on this machine,
and it is the form in which `squareSize`/`MaxHeight` are spelled with TorqueScript capitalisation:

```
new TerrainBlock(mammoth2) {
         terrainFile = "levels/mammoth/mammoth2.ter";
         castShadows = "1";
         squareSize = "1";
         MaxHeight = "464";
         baseTexSize = "4096";
         lightMapSize = "256";
         screenError = "16";
         position = "-2048 -2048 38.6";
         rotation = "1 0 0 0";
         canSave = "1";
         canSaveDynamicFields = "1";
      };
```

Source: local file `C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\mods\repo\mammoth12.zip` → `levels/mammoth/mammoth2.mis` (BeamNG 0.39.4.0.20972)

#### A1.4 Complete observed field set of TerrainBlock

Union over all 21 shipped levels + all 22 local map mods:

| Field | Type | Notes |
| --- | --- | --- |
| `name` | string | Usually `theTerrain` |
| `class` | string | `"TerrainBlock"` |
| `persistentId` | uuid string | Optional; several community maps omit it |
| `__parent` | string | Parent `SimGroup` name |
| `position` | `[x, y, z]` | Terrain **corner** in world space. For a centred map this is `-(terrainSize × squareSize) / 2` on X and Y |
| `terrainFile` | path string | Points at the `.ter` |
| `squareSize` | number | **Absent ⇒ 1.0**. Only written when ≠ 1 |
| `maxHeight` | number | Vertical range in metres |
| `baseTexSize` | int | 2048 or 4096 in shipped content; legacy blend-texture resolution |
| `lightMapSize` | int | e.g. `256` (mammoth), `512` (sanok). "Legacy terrain lightmap size" |
| `screenError` | int | e.g. `32` (jungle_rock_island), `128` (sanok); engine doc string says "Not yet implemented." |
| `castShadows` | bool | `"Allows the terrain to cast shadows onto itself and other objects."` |
| `materialTextureSet` | string | Name of a `TerrainMaterialTextureSet` asset (the v1.5 PBR path) |
| `minimapImage` | path string | `"The mini map image for this terrain block"` |
| `annotation` | string | e.g. `"MUD"` on gridmap_v2 |
| `locked` | bool | e.g. `"locked":true` on NEFR and ogc_map |

Engine field-doc strings, verbatim, from the executable (offsets are byte offsets in
`Bin64\BeamNG.drive.x64.exe`):

```
32973136 'The source terrain data file.'            -> 32973168 'terrainFile'
32973184 'The mini map image for this terrain block'-> 32973232 'minimapImage'
32973248 'Template material used for rendeing'      -> 32973288 'materialTextureSet'
32973312 'Allows the terrain to cast shadows onto itself and other objects.'  (field name reuses 'castShadows')
32973392 'Indicates the spacing between points on the XY plane on the terrain.' -> 'squareSize'
32973464 'Maximum height in meters'                 -> 'maxHeight'
32973496 'Size of base texture size per meter.'     -> 32973536 'baseTexSize'
32973552 'Light map dimensions in pixels.'          -> 32973584 'lightMapSize'
32973600 'Not yet implemented.'                     -> 32973624 'screenError'
32973640 '$pref::Terrain::lodScale'
32973672 '$pref::Terrain::detailScale'
32916960 'TerrainBlock objects are not valid within a Prefab'
30929832 '$TerrainBlock::cellsRendered'
30929864 '$TerrainBlock::overrideCells'
30929896 '$TerrainBlock::drawCalls'
```

Source: local file `F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe` string table (BeamNG 0.39.4.0.20972)
Source (corroboration): <https://docs.beamng.com/modding/levels/level_classes/terrainblock/>

**Note on `TerrainBlock` inside prefabs:** the engine refuses it — `"TerrainBlock objects are not valid within a Prefab"` (exe offset 32916960).

### A2. `terrainSize` — allowed/observed values, and the engine-side check

Terminology: the field is **not** called `terrainSize` anywhere in 0.39. The string `terrainSize` has
**zero occurrences** in the engine executable. The value is read from the `.ter` header as `size` (a `u32`),
and is mirrored in `<terrain>.terrain.json` as `"size"`.

**The engine-side check is in the executable, not in Lua.** Verbatim strings around the import command:

```
32978879 'TerrainBlock.create( BString terrainName, i32 resolution, BString materialName, bool genNoise )'
32979008 '( BString terrainName, BString heightMap, f32 metersPerPixel, f32 heightScale, BString materials, BString opacityLayers[, bool flipYAxis=true, BString holesMap] )'
32979172 'import'
32979184 "TerrainBlock::createNew - error creating '{}'"
32979232 'engine::cm_TerrainBlock_createNew'
32979304 'engine::cm_TerrainBlock_import'
32979336 'Height map must be square and power of two in size!'
32979392 'Height map must be between 128 and 8192 in size!'
32979528 "The opacity map '{}' doesn't match height map size!"
32979736 'engine::TerrainBlock::importB'
```

Source: local file `F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe`, offsets 32978879–32979736 (BeamNG 0.39.4.0.20972)

There is **no `maxTerrainSize` symbol or string**. The 128…8192 window and the square/power-of-two rule are the
only terrain-resolution limits present. `TerrainBlock.createNew` rounds the requested resolution **up** to the
next power of two.

Corroboration: the official docs state, verbatim:

> - Heightmap must be square
> - Heightmap resolution must be power of two
> - Heightmap size must be between `128` and `8192`
> - Opacity/layer maps must match the heightmap size
> - Number of material names must match number of opacity layers

Source: <https://docs.beamng.com/modding/levels/level_formats/terrain/> (section "Importing terrain")

**Observed `terrainSize` values (read from the `.ter` headers):**

| Level | `size` in `.ter` | `.ter` version | World extent (size × squareSize) |
| --- | ---: | ---: | --- |
| `Industrial` | 256 | 8 | 256 m |
| `template` | 1024 | 9 | 1024 m |
| `small_island` | 1024 | 9 | 1024 m |
| `driver_training` | 1024 | 9 | 1024 m |
| `autotest` | 1024 | legacy | 1024 m |
| `gridmap_v2`, `east_coast_usa`, `Utah`, `west_coast_usa`, `Cliff`, `jungle_rock_island` | 2048 | 9 | 2048 m |
| `derby` | 2048 | 9 | 2048 × 0.3 = **614.4 m** |
| `hirochi_raceway` | 2048 | 9 | 2048 × 0.75 = **1536 m** |
| `italy` | **4096** | 9 | 4096 m |
| `johnson_valley` | **4096** | 9 | 4096 × 0.75 = **3072 m** |
| `automation_test_track` | **4096** | 9 | 4096 × 0.5 = **2048 m** |
| mod `cwmysgawen`, `ogc_map`, `tx_map`, `peppervalley`, `ogc_two`, `mammoth2` | 4096 | 9 (7 for mammoth) | 4096 m |
| mod **`NEFR`** | **8192** | 9 | **8192 m** |
| mod **`river_highway`** | **8192** | 9 | **8192 m** |
| mod **`sanok`** | **8192** | 9 | 8192 × 0.5 = **4096 m** |
| mod **`americanroad/mymap`** | **8192** | 8 | (version-8 layout, larger per-cell record) |

Sources: local files under `F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\*.zip` and `C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\mods\repo\*.zip` (BeamNG 0.39.4.0.20972)

**8192 is used in the wild.** `NEFR_0391.zip`, `river_highway.zip`, `sanok_repo.zip` and `americanroad.zip`
each contain a `.ter` whose 5th byte onwards decodes to `size = 8192`, i.e. the maximum value the import check
allows. This is the strongest local evidence that 8192 works in practice, not just in the validator.

### A3. `squareSize` — range, observed values, per-level extents

* Default when absent: **1.0** (every shipped level except three omits the field entirely).
* Editor default for a *newly created* `TerrainBlock`: **8** —
  `lua\ge\extensions\editor\createObjectTool.lua:852`: `obj:setField("squareSize", 0, "8")`.
* Engine documentation string: `"Indicates the spacing between points on the XY plane on the terrain."`
* Terrain import UI step/precision: `lua\ge\extensions\editor\terrainEditor.lua:1889`:
  `im.InputFloat('##MetersPerPixel', terrainImpExp.metersPerPixel, 0.1, 1, "%.1f")` — default `1`
  (`terrainImpExp.metersPerPixel = im.FloatPtr(1)`, line 95).
* **No minimum or maximum check for `squareSize` exists in the binary.** A byte scan of the executable finds
  only 3 occurrences of the literal `squareSize`, all belonging to `engine::GroundPlane` and to the
  `TerrainBlock` documentation example. `GroundPlane` has a guard
  (`32839552 'GroundPlane - squareSize below threshold; re-setting to {:0.2f}'`) but `TerrainBlock` has none.
  The only extreme real use found is `lua\ge\extensions\tech\impactgen\crashOutput.lua:574`:
  `ground:setField('squareSize', 0, 1024)` — i.e. 1024 m per quad for a crash-test ground plane.
* Other engine-exposed accessors seen in Lua: `tb:getSquareSize()`, `terrain:getWorldBlockSize()`.

Sources: local files `F:\SteamLibrary\steamapps\common\BeamNG.drive\lua\ge\extensions\editor\createObjectTool.lua`,
`...\lua\ge\extensions\editor\terrainEditor.lua`, `...\lua\ge\extensions\editor\levelValidator.lua:510`,
`...\lua\ge\extensions\tech\impactgen\crashOutput.lua`, and the string scan of
`...\Bin64\BeamNG.drive.x64.exe` (BeamNG 0.39.4.0.20972)

**Real-world extent per shipped level (terrainSize × squareSize):**

| Level | terrainSize | squareSize | Extent (m) | Extent (km²) | `info.json` `size` |
| --- | ---: | ---: | ---: | ---: | --- |
| `Industrial` | 256 | 1 | 256 × 256 | 0.066 | `[1024,1024]` (mismatch — see note) |
| `template` | 1024 | 1 (implied) | 1024 × 1024 | 1.05 | `[2048,2048]` (mismatch) |
| `small_island` | 1024 | 1 | 1024 × 1024 | 1.05 | `[1024,1024]` ✔ |
| `gridmap_v2` | 2048 | 1 | 2048 × 2048 | 4.19 | `[2048,2048]` ✔ |
| `east_coast_usa` | 2048 | 1 | 2048 × 2048 | 4.19 | `[2048,2048]` ✔ |
| `Utah` | 2048 | 1 | 2048 × 2048 | 4.19 | `[2048,2048]` ✔ |
| `west_coast_usa` | 2048 | 1 | 2048 × 2048 | 4.19 | `[2048,2048]` ✔ |
| `Cliff` | 2048 | 1 | 2048 × 2048 | 4.19 | `[2048,2048]` ✔ |
| `jungle_rock_island` | 2048 | 1 | 2048 × 2048 | 4.19 | `[2048,2048]` ✔ |
| `derby` | 2048 | 0.3 | 614.4 × 614.4 | 0.38 | `[1024,1024]` (mismatch) |
| `hirochi_raceway` | 2048 | 0.75 | 1536 × 1536 | 2.36 | — |
| `italy` | 4096 | 1 | **4096 × 4096** | 16.78 | `[4096,4096]` ✔ |
| `johnson_valley` | 4096 | 0.75 | 3072 × 3072 | 9.44 | `[3072,3072]` ✔ |
| `automation_test_track` | 4096 | 0.5 | 2048 × 2048 | 4.19 | `[2048,2048]` ✔ |
| mod `NEFR` | 8192 | 1 | **8192 × 8192** | 67.1 | `[8192,8192]` ✔ |
| mod `river_highway` | 8192 | 1 | **8192 × 8192** | 67.1 | `[8192,8192]` ✔ |
| mod `sanok` | 8192 | 0.5 | 4096 × 4096 | 16.78 | `[4096,4096]` ✔ |
| mod `americanroad/mymap` | 8192 | 1 | 8192 × 8192 | 67.1 | `[11000,11000]` (larger than the terrain) |

**Important:** `info.json`'s `size` is an **authored, non-authoritative** metadata field used by the minimap and
big-map UI. It agrees with `terrainSize × squareSize` in most levels but not all (`template` says 2048 while the
terrain is 1024 m; `derby` says 1024 while the terrain is 614 m; `americanroad` says 11000 while the terrain is
8192 m). It is **not** an engine limit and must not be used as such.

Sources: local files in the level and mod zips listed above; `lua\ge\extensions\core\levels.lua` (BeamNG 0.39.4.0.20972)

### A4. `heightScale` / `maxHeight` / vertical resolution

Heights are stored as **unsigned 16-bit** integers. The engine's own `TerrainBlock` interface exposes both a
"user" scale and the raw scale: `terrainImpExp.heightScale = im.FloatPtr(50) -- maxHeight`
(`terrainEditor.lua:96`), `tb:getHeightScaleUser()` (`terrainEditor.lua:1446`).

Conversion (official docs, verbatim):

```
heightScale = maxHeight / 65536
heightMeters = storedHeight x heightScale
```

So vertical resolution is `maxHeight / 65536`:

| Level | `maxHeight` | Vertical quantum |
| --- | ---: | ---: |
| `Industrial` | 100 | 1.53 mm |
| `template` | 120 | 1.83 mm |
| `east_coast_usa` | 120 | 1.83 mm |
| `Utah` | 200 | 3.05 mm |
| `johnson_valley` | 200 | 3.05 mm |
| `west_coast_usa` | 384.64 | 5.87 mm |
| `italy` | 500 | 7.63 mm |
| `Cliff` | 2000 | 30.5 mm |
| mod `NEFR` | 419 | 6.39 mm |
| mod `river_highway` | 512 | 7.81 mm |
| mod `sanok` | 640 | 9.77 mm |

The TerrainBlock's `position.z` is the base elevation; heights are added on top of it
(`terrainBlock:getHeight(...) - terrainBlock:getPosition().z`, `terrainEditor.lua:3063`).

Sources: local level/mod zips; `lua\ge\extensions\editor\terrainEditor.lua`; corroborated verbatim by
<https://docs.beamng.com/modding/levels/level_formats/terrain/> (section "Height scale")

### A5. `terrainFile` format (`terrain.ter`)

**It is not a PNG.** It is a raw little-endian binary file. The heightmap *source* PNG referenced by
`.terrain.json` (`heightmapImage`, e.g. `/levels/template/theTerrain.terrainheightmap.png`) is **not present**
in most shipped level zips — only a `<name>.ter.depth.png` companion is shipped, and the engine's exporter
writes `name.terrainheightmap.png`. `west_coast_usa` is the exception and does ship
`westcoast_heightmap.png` (8,404,564 bytes).

**Version 9 layout — verified by exact byte accounting on 7 shipped files:**

```
u8    version            = 9
u32   size               = terrainSize (power of two, 128..8192)
u16   heightMap[size*size]
u8    layerMap[size*size]              (index into the material name list; 255 = hole/empty)
u32   materialCount
cstr  materialNames[materialCount]     (u8 length prefix + ASCII bytes, no NUL)
```

Byte-accounting proof (`len == 5 + 3*size² + trailer` exactly, for every file):

| File | version | size | file bytes | trailer bytes | payload | payload / size² |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `template/theTerrain.ter` | 9 | 1024 | 3,145,775 | 42 | 3,145,728 | **3.000000** |
| `small_island/small_island.ter` | 9 | 1024 | 3,145,849 | 116 | 3,145,728 | **3.000000** |
| `gridmap_v2/theTerrain.ter` | 9 | 2048 | 12,582,975 | 58 | 12,582,912 | **3.000000** |
| `east_coast_usa/theTerrain2.ter` | 9 | 2048 | 12,583,053 | 136 | 12,582,912 | **3.000000** |
| `Utah/theTerrain.ter` | 9 | 2048 | 12,583,057 | 140 | 12,582,912 | **3.000000** |
| `west_coast_usa/west_coast_usa.ter` | 9 | 2048 | 12,583,020 | 103 | 12,582,912 | **3.000000** |
| `italy/theTerrain.ter` | 9 | 4096 | 50,331,819 | 166 | 50,331,648 | **3.000000** |
| mod `NEFR/terrain2.ter` | 9 | 8192 | 201,326,733 | 141 | 201,326,592 | **3.000000** |

Raw header bytes of `levels/template/theTerrain.ter`, first 5 bytes:
`09 00 04 00 00` → version `0x09` = 9, size `0x00000400` = 1024. ✔
Raw trailer of the same file:
`01 01 … 01 04 00 00 00 05 "Grass" 09 "BeachSand" 10 "dirt_rocky_large" 04 "rock"` → materialCount 4,
material names as expected. ✔

**`layerTextureMap` does not exist in the version-9 payload.** The `binaryFormat` string in `.terrain.json`
still advertises it, but the arithmetic above (`payload == 3 × size²`, never `4 × size²`) proves it is absent.
This is confirmed verbatim by the official docs: *"The engine currently writes `layerTextureMap` into the
`binaryFormat` description even though the version 9 payload written by `TerrainFile::save()` does not contain
that array."*

Sources: byte-level parse of the `.ter` entries inside `F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\*.zip`
and `C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\mods\repo\*.zip`; engine `binaryFormat` string at exe
offset 32975632; <https://docs.beamng.com/modding/levels/level_formats/terrain/> (BeamNG 0.39.4.0.20972)

**File-size formula and the practical consequence:**

```
bytes(.ter) = 5 + 3 × terrainSize² + 4 + Σ(1 + len(materialName))
```

| terrainSize | raw `.ter` size |
| ---: | ---: |
| 1024 | ~3.0 MiB |
| 2048 | ~12.0 MiB |
| 4096 | ~48.0 MiB |
| **8192** | **~192.0 MiB** |

**`baseTexSize`, `lightMapSize`, `terrainMaterialCache` (question A5, continued):**

* `baseTexSize` — legacy generated terrain blend/base texture resolution in pixels. Observed `2048` and `4096`;
  the engine string describes it as `"Size of base texture size per meter."` (a Torque-era doc string that does
  not match its actual use as a pixel size). Absent on `template`, `small_island`, `Industrial`, `autotest`.
* `lightMapSize` — `"Light map dimensions in pixels."` Observed `256` (mammoth2) and `512` (sanok); absent
  everywhere else. Effectively legacy.
* `terrainMaterialCache` — a *generated* cache, not an authored asset. Two paths appear:
  * engine string `art/terrainMaterialCache/%s.png` (exe offset 32967904, inside
    `engine::TerrainCellMaterial::_updateDescriptorSet`);
  * official docs: *"Generated cache files can be stored under paths like `/temp/art/terrainMaterialCache/<hash>.dds`.
    The hash is based on the source texture paths…"*
  Related engine strings:
  ```
  32967768 'Too many terrain textures. Only {} are supported'
  32968168 'Texture added for packing, because cache not found: {}'
  32968224 'Texture added for packing, because cache outdated: {}'
  32968280 'Texture added for packing, because size differs from cache: {}'
  32968376 'TerrainCellMaterial::getUniqueTextures::Texture'
  32968480 'Terrain material [{}] use texture [{}] that dont match size of [{},{}] declared in TerrainMaterialTextureSet named [{}]'
  ```
* Terrain GPU memory: the `.ter` is *not* what is resident on the GPU. The `TerrainMaterialTextureSet` packs
  every terrain material into shared texture arrays sized `baseTexSize × macroTexSize × detailTexSize`. For
  `template` that is `baseTexSize=[512,512]`, `detailTexSize=[1024,1024]`, `macroTexSize=[1024,1024]`
  (see §A6).

### A6. Terrain material / blend-map system

**How layers are stored.** The version-9 `.ter` stores exactly **one** material index per terrain cell
(`u8 layerMap[size²]`). There is no per-cell blend weight array in the file. Material *names* are stored in the
file trailer, and `TerrainMaterial` assets are looked up by internal name at load.

**How many layers.** Observed counts in shipped content: **4** (`template`), **5** (`cwmysgawen`),
**8** (`gridmap_v2`), **11** (`west_coast_usa`, `small_island`), **13** (`Utah`, `jungle_rock_island`),
**14** (`johnson_valley`), **15** (`east_coast_usa`, `italy`), up to **22** (`ogc_map` mod).
Engine limit, verbatim:

```
32976144 'Terrain have to many materials, material will be ignored after the 254th'
32976272 'art/terrains'
32976288 'Terrain have to many materials, material will be ignored after the 31th'
```

Index `255` is reserved (`255 = empty terrain / hole`). So the hard ceiling is **254 usable materials**, with a
legacy path limited to 31.

**How they are authored.** In the World Editor's *Terrain Material Library* / Terrain Editor. Terrain import
accepts an opacity/layer-map set (one texture channel per material — R/G/B/A) and, per sample, *the material
layer with the highest opacity wins* (winner-takes-all, no blending weights). Optional hole map:
`holeMap value 0xFF ⇒ layerMap = 255` (empty). Lua entry points:
`terrBlock:importMaps(heightMapTexturePath, metersPerPixel, heightScale, holeMap, materials, textureMaps, flipYAxis)`
(`terrainEditor.lua:1266`) and `terrBlock:addMaterial(internalName, -1)` /
`terrBlock:updateMaterial(index, name)` / `terrBlock:removeMaterial(index)` / `terrBlock:reorderMaterial(from,to)`.

**The `TerrainMaterial` asset format** (v1.5 PBR). Stored as a name-keyed JSON object in
`levels/<level>/art/terrains/main.materials.json`. Verbatim head of the `template` file:

```json
{
 "Asphalt-e72d9e60-60c5-40e7-a447-273955b221c6": {
  "internalName": "Asphalt",
  "class": "TerrainMaterial",
  "persistentId": "e72d9e60-60c5-40e7-a447-273955b221c6",
  "annotation": "ASPHALT",
  "aoBaseTex": "/levels/template/art/terrains/t_terrain_base_asphalt_ao.png",
  "aoBaseTexSize": 150,
  "aoDetailStrength": [1, 0],
  "aoDetailTex": "/levels/template/art/terrains/t_asphalt_02_ao.png",
  "aoMacroStrength": [1, 0.5],
  "aoMacroTex": "/levels/template/art/terrains/t_macro_asphalt_ao.png",
  "aoMacroTexSize": 80,
  "baseColorBaseTex": "/levels/template/art/terrains/t_terrain_base_asphalt_b.png",
  "baseColorBaseTexSize": 150,
  "baseColorDetailStrength": [0.3, 0],
  "baseColorDetailTex": "/levels/template/art/terrains/t_asphalt_02_b.png",
  "baseColorMacroStrength": [0.2, 0.1],
  "baseColorMacroTex": "/levels/template/art/terrains/t_macro_asphalt_b.png",
  "baseColorMacroTexSize": 80,
  "detailDistances": [0, 0, 30, 50],
  "groundmodelName": "ASPHALT",
  "heightBaseTex": "/levels/template/art/terrains/t_terrain_base_asphalt_h.png",
  "heightBaseTexSize": 150,
  "heightDetailTex": "/levels/template/art/terrains/t_asphalt_02_h.png",
  "heightMacroTex": "/levels/template/art/terrains/t_macro_asphalt_h.png",
  "heightMacroTexSize": 80,
  "macroDistances": [0, 10, 100, 3000],
  "normalBaseTex": "/levels/template/art/terrains/t_terrain_base_asphalt_nm.png",
  "normalBaseTexSize": 150,
  "normalDetailStrength": [0.6, 0.2],
  "normalDetailTex": "/levels/template/art/terrains/t_asphalt_02_nm.png",
  "normalMacroStrength": [0.6, 0.6],
  "normalMacroTex": "/levels/template/art/terrains/t_macro_asphalt_nm.png",
  "normalMacroTexSize": 80,
  "roughnessBaseTex": "/levels/template/art/terrains/t_terrain_base_asphalt_r.png",
  "roughnessBaseTexSize": 150,
  "roughnessDetailStrength": [1, 0],
  "roughnessDetailTex": "/levels/template/art/terrains/t_asphalt_02_r.png",
  "roughnessMacroStrength": [0.7, 1],
  "roughnessMacroTex": "/levels/template/art/terrains/t_macro_asphalt_r.png",
  "roughnessMacroTexSize": 80
 },
 …
}
```

The `TerrainMaterialTextureSet` that the TerrainBlock references, verbatim from the same file:

```json
{
 "name": "templateTerrainMaterialTextureSet",
 "class": "TerrainMaterialTextureSet",
 "baseTexSize": [512, 512],
 "detailTexSize": [1024, 1024],
 "macroTexSize": [1024, 1024]
}
```

Five texture groups (`baseColor`, `normal`, `roughness`, `ao`, `height`) × three scales (`Base`, `Macro`,
`Detail`) = 15 texture slots minimum per material, **all of which must be assigned or the material editor
refuses to save**. Per-slot `<group><Scale>TexSize` is a **world mapping size in metres**, not pixels
(typical: base = whole-terrain, macro 30–80 m, detail 2–8 m). Hard renderer caps, per the docs: detail sampling
stops at **250 m**, macro at **1000 m**. Blending between overlapping materials is **heightmap-driven**
("higher layer shows through"), not a linear cross-fade.

Sources: local file `F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\template.zip` →
`levels/template/art/terrains/main.materials.json`; `lua\ge\extensions\editor\terrainEditor.lua`;
executable strings at 32967768–32968752; <https://docs.beamng.com/modding/levels/level_formats/terrain/>
(BeamNG 0.39.4.0.20972 / docs "Terrain Files")

### A7. Is there a hard limit on total level size (world extent)?

**No world-extent cap was found.** The only hard geometric limit is per-TerrainBlock:

```
maximum world extent of one TerrainBlock = terrainSize × squareSize
                                        = 8192 × squareSize  (metres)
```

At the default `squareSize = 1` that is **8.192 km × 8.192 km**. `squareSize` is unvalidated (see §A3), so a
single terrain could in principle span `8192 × 8 = 65.536 km` at the editor's default new-TerrainBlock
`squareSize = 8` — but at 8 m per height sample, which is far coarser than road widths and would not be
drivable. **This trade-off (extent vs. horizontal resolution) is the single most important architectural fact
in this document.**

Because `TerrainBlock` is not valid inside a `Prefab` (exe offset 32916960) and there is no tiling/stitching
mechanism in the shipped tooling, a world larger than 8192 m at 1 m/px must be composed of **multiple sibling
TerrainBlock scene objects**, one per tile. No shipped level and no local mod does this for real (the one level
with 4 TerrainBlocks has 4 *identical duplicates at the same position*). The engine does support multiple
terrain blocks in principle (`getTerrainUnderWorldPoint()` returns "the ID of the requested terrain block";
`terrain.lua` iterates `scenetree.findClassObjects("TerrainBlock")`), but **no supported multi-tile workflow,
seam handling or editor tooling for it was found** — see "Open questions".

Additional intra-level coupling limits that will bite on a multi-block design:

```
33115912 'Terrain block size and mask file size mismatch'
33116816 'Terrain block size ({}x{}) and mask size ({}x{}) does not match!'
```
(GroundCover masks must match the terrain block size.)

Sources: executable string scan; `lua\ge\extensions\core\terrain.lua`; `lua\ge\extensions\util\terrainGenerator.lua:107`;
level/mod scans (BeamNG 0.39.4.0.20972)

---

## B. Roads

### B8. `DecalRoad` — full field list and a real example

The task's assumed field list contains three fields that **do not exist in BeamNG 0.39**:
`roadWidth`, `fadeStartBit`, `fadeEndBit`. A byte scan of `BeamNG.drive.x64.exe` finds **zero occurrences** of
any of the three, and a full field-frequency histogram over all DecalRoads in all shipped levels finds none
either. These are Torque 3D-era field names that BeamNG removed. `roadWidth` in particular has been replaced
by the per-node width (4th component of each node). Do not use them.

**Real example, quoted verbatim** (`levels/italy/main/MissionGroup/decalroads/dirt/items.level.json`-style
entry from `Utah`, pretty-printed here for readability — the file is one JSON object per line):

```json
{
 "class": "DecalRoad",
 "persistentId": "92cf4804-5357-4d86-bc22-4baceef0781c",
 "__parent": "roads",
 "position": [835.18512, -268.018433, 143.609955],
 "breakAngle": 1,
 "distanceFade": [200, 100],
 "improvedSpline": true,
 "material": "DirtRoad_variation_01",
 "nodes": [
   [835.18512, -268.018433, 143.609955, 4],
   [830.987793, -260.761627, 143.66449, 4],
   [821.31189, -262.353394, 143.640045, 4]
 ],
 "renderPriority": 9,
 "startEndFade": [5, 5],
 "textureLength": 50
}
```

**Real legacy `.mis` example** showing the `Node` sub-block syntax (one `Node` line per node, in the `.mis`
dialect; `Material` capitalised):

```
new DecalRoad() {
      drivability = "-1";
      Material = "AsphaltRoad_lanes";
      textureLength = "10";
      breakAngle = "1";
      renderPriority = "10";
      zBias = "0";
      decalBias = "0.001";
      distanceFade = "0 0";
      startEndFade = "1 1";
      position = "1541.36 -723.757 269.191";
      rotation = "1 0 0 0";
      scale = "1 1 1";
      canSave = "1";
      canSaveDynamicFields = "1";

      Node = "1541.364990 -723.757385 269.191406 9.000000";
      Node = "1544.802368 -693.613770 268.838379 9.000000";
      Node = "1548.327148 -668.722778 268.616699 9.000000";
      Node = "1557.970337 -596.590881 268.808594 9.000000";
      Node = "1570.993530 -499.984131 269.148438 9.000000";
      Node = "1579.279053 -441.223053 269.351074 9.000000";
      Node = "1587.301392 -378.055695 269.495117 9.000000";
      Node = "1598.559570 -289.977692 269.233887 9.000000";
      Node = "1600.330444 -263.017090 269.197266 9.000000";
   };
```

**Complete `DecalRoad` field list, from real data (field-frequency histogram across all shipped levels,
with occurrence counts for the largest levels):**

| Field | Type | east_coast (of 3115) | west_coast (of 12752) | italy (of 7900) | Utah (of 3554) | Meaning |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `class` | string | 3115 | 12752 | 7900 | 3554 | always `"DecalRoad"` |
| `persistentId` | uuid | 3115 | 12752 | 7900 | 3554 | engine persistent id |
| `__parent` | string | 3115 | 12752 | 7900 | 3554 | parent group |
| `position` | `[x,y,z]` | 3115 | 12752 | 7900 | 3554 | object origin |
| `nodes` | `[[x,y,z,width], …]` | 3115 | 12752 | 7900 | 3554 | road spline control points |
| `material` | string | 3115 | 12752 | 7900 | 3554 | material datablock name |
| `textureLength` | number | 3007 | 10409 | 7194 | 3346 | metres per texture repeat |
| `renderPriority` | int | 2320 | 10947 | 5097 | 3300 | descending draw order |
| `startEndFade` | `[a,b]` | 2563 | 4731 | 5943 | 3310 | fade length at first/last node |
| `distanceFade` | `[d,range]` | 545 | 11136 | 6201 | 1001 | default per docs `[300, 50]` |
| `breakAngle` | number | 2688 | 3386 | 4189 | 1004 | legacy-spline subdivision threshold (deg) |
| `improvedSpline` | bool | 2983 | 10700 | 7025 | 3446 | Catmull-Rom spline mode |
| `detail` | number | 57 | 10722 | 340 | 5 | improved-spline sampling step (0.1 = 10 %) |
| `smoothness` | number | 0 | 347 | 10 | 6 | improved-spline tension |
| `looped` | bool | 0 | 24 | 4 | 1 | closed loop |
| `startTangent` / `endTangent` | bool | 0 / 2 | 531 / 526 | 0 / 0 | 0 / 0 | first/last node as tangent marker |
| `overObjects` | bool | 58 | 5177 | 152 | 210 | clip onto static meshes, not just terrain |
| `zBias` | number | 0 | 1744 | 0 | 1 | depth/render bias |
| `decalBias` | number | 0 | 1314 | 7545 | 1 | height bias above clipped surface |
| `useSubdivisions` | bool | 23 | 266 | 0 | 0 | high-detail AI road data |
| `drivability` | number | 407 | 1467 | 482 | 193 | `-1` = AI ignores, `1` = optimal |
| `gatedRoad` | bool | 26 | 703 | 144 | 24 | private/gated |
| `oneWay` | bool | 18 | 682 | 144 | 21 | one-way |
| `flipDirection` | bool | 3 | 53 | 21 | 2 | reverse logical direction |
| `autoLanes` | bool | 0 | 30 | 13 | 0 | auto lane assignment |
| `autoJunction` | bool | 0 | 232 | 1 | 0 | auto-connect intersecting AI paths |
| `lanes` | number | 0 | 28 | 0 | 0 | — |
| `lanesLeft` / `lanesRight` | int | 2 / 2 | 420 / 303 | 45 / 41 | 18 / 4 | manual lane counts |
| `lanesInvert` | bool | 0 | 93 | 0 | 0 | — |
| `speedLimit` | number | 0 | 83 | 0 | 0 | — |
| `hiddenInNavi` | bool | 0 | 0 | 0 | 0 | hide in navigation app (gridmap_v2 uses it 94×) |
| `hidden` | bool | 0 | 1 | 0 | 0 | — |
| `useTemplate` | bool | 0 | 854 | 208 | 614 | — |
| `internalName` | string | 0 | 141 | 0 | 0 | — |
| `cloneOrigin` | uuid | 1 | 0 | 0 | 0 | — |
| `annotation` | string | 0 | 0 | 429 | 0 | editor/debug tag |
| `name` | string | 131 | 58 | 19 | 9 | optional; most roads are unnamed |

Engine field-doc strings for DecalRoad (verbatim, byte offsets in the executable):

```
32574280 'overObjects'            <- 'Enables raycast down on static meshes'
32574296 'Material used for rendering'                                          -> 'material'
32574328 'The length in meters of textures mapped to the DecalRoad'             -> 'textureLength'
32574416 'Angle in degrees - DecalRoad will subdivided the spline if its curve is greater than this threshold.' -> 'breakAngle'
32574536 'DecalRoads are rendered in descending renderPriority order.'          -> 'renderPriority'
32574648 'the hovering height of the decal over the terrain'                    -> 'decalBias'
32574720 'distance at which the decalroad will start to fade out, and the size of the fade…' -> 'distanceFade'
32574864 'size of the fade at the start and end of the decalroad'               -> 'startEndFade'
32574952 'Whether this road is hidden in the navigation app'                    -> 'hiddenInNavi'
32575040 'Do not modify, for internal use.'                                     -> 'node'
32573040 'if the AI should use this road or not: -1 = no use, 1 = optimal, 0.5 = dirt road' -> 'drivability'
32573968 'use new spline algorithm for smoother paths'                          -> 'improvedSpline'
32574120 '0 = linear, 0.5 = half smooth, 1 = fully smooth'                      -> 'smoothness'
32574184 '0.1 = every 10%, 0.2 = every 20%, 0.5 = at 50% only'                  -> 'detail'
32573408 'autoLanes' / 32573464 'Amount of lanes on the left side' / 32573464 'Amount of lanes on the right side'
32573632 'When enabled, AI path will automatically connect to each other when they intersect…' -> 'autoJunction'
32573888 'Generate high detail AI road'                                          -> 'useSubdivisions'
32575208 '$DecalRoad::discardAll' / 32575184 '$DecalRoad::showBatches' / 32575232 '$DecalRoad::showSpline' / 32575160 '$DecalRoad::wireframe'
32575304 'Node = "%f %f %f %f";'
32575328 "Decal '{}' doesnt not use `overObjects` property and the map does not contain a terrain. Remove this decal or enable `overObjects` else it will crash"
```

Sources: local files in `F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\{east_coast_usa,west_coast_usa,italy,Utah}.zip`;
`C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\mods\repo\mammoth12.zip`;
`F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe` strings;
<https://docs.beamng.com/modding/levels/level_classes/decalorad/> (BeamNG 0.39.4.0.20972)

### B9. `MeshRoad` and other road object classes

**`MeshRoad` exists as an engine class** and is documented, but appears **nowhere in any shipped level or any
local mod**:

```
32582616 'fnMeshRoad_create'
32582640 'MeshRoad* create()'
32583024 'TypeMeshRoadRef'
32583040 'TypeMeshRoad'
32580216 'fnMeshRoad_regenerate'
32580552 'fnMeshRoad_getNodeCount'
32580792 'getNodeNormal'
32581016 'virtual f32 getNodeDepth(u32 idx)'
32581184 'fnMeshRoad_collideRay'
32581328 'fnMeshRoad_insertNode'   -> '(Point3F pos, f32 width, f32 depth, Point3F normal, u32 idx)'
```

Result of a full data scan for `"class":"MeshRoad"` across all 21 shipped level zips and all 21 map mods:
**0 instances.** The `MeshRoad` class is therefore `verified-local` as a class but `unverified` as a
production-ready asset type — no shipped reference implementation exists to copy.

**Complete list of road/road-like classes actually present in shipped level data:**

| Class | Role | Count in west_coast_usa |
| --- | --- | ---: |
| `DecalRoad` | projected road decal + AI nav source | 12,752 |
| `River` | spline water body derived from `WaterObject` (has `nodes`) | 2 |
| `WaterBlock` | box water volume | 3 |
| `WaterPlane` | infinite ocean plane | 1 |
| `AIWaypointGroup` / `BeamNGWaypoint` | AI waypoints | 838 `BeamNGWaypoint` |
| `Track_meshes` group | `TSStatic` road meshes | (inside `TSStatic`) |

Sources: full data scan of the level/mod zips; executable strings above (BeamNG 0.39.4.0.20972)

### B10. DecalRoad / node budgets in the large shipped levels

Counted by parsing every `items.level.json` in each archive:

| Level | Terrain area | DecalRoads | Total nodes | Mean nodes/road | Max nodes in one road | Roads / km² | Nodes / km² |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **west_coast_usa** | 4.19 km² | **12,752** | **59,557** | 4.67 | 165 | 3,040 | 14,199 |
| **italy** | 16.78 km² | **7,900** | **64,137** | 8.12 | 429 | 471 | 3,823 |
| **johnson_valley** | 9.44 km² | 4,345 | 23,822 | 5.48 | 115 | 460 | 2,524 |
| **Utah** | 4.19 km² | 3,554 | 18,218 | 5.13 | 156 | 847 | 4,344 |
| **east_coast_usa** | 4.19 km² | 3,115 | 19,994 | 6.42 | 73 | 743 | 4,767 |
| gridmap_v2 | 4.19 km² | 1,407 | 5,088 | 3.62 | 48 | 336 | 1,214 |
| small_island | 1.05 km² | 551 | 3,800 | 6.90 | 58 | 525 | 3,619 |

**Extrapolation to 220 km × 145 km = 31,900 km²** (naive area scaling — treat as an order-of-magnitude
indicator only, because Ivanovo Oblast has far lower road density than a US west-coast city):

| Basis | DecalRoads | Nodes | Objects | Forest items |
| --- | ---: | ---: | ---: | ---: |
| west_coast_usa density (dense city map) | ~97 M | ~453 M | ~332 M | ~1.0 B |
| italy density (mixed rural/urban) | ~15 M | ~122 M | ~44 M | ~392 M |
| johnson_valley density (rural) | ~15 M | ~81 M | ~69 M | ~2.2 B |

Node widths (the 4th node component) observed in shipped data range from **0.15 m** (thin painted lines) to
**10 m** (wide highways); the modal values are 2 m (east_coast), 0.2 m (west_coast, lane markings),
2 m (italy), 4 m (Utah), 8 m (johnson_valley).

Sources: full parse of `F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\*.zip` (BeamNG 0.39.4.0.20972)

**Known DecalRoad scaling limits in the engine** (these are the ones that will hurt at scale). Verbatim strings:

```
32663600 "DecalRoad [{}] amount of vertices '{}' is greater the maximum allowed '{}'"
          engine::SceneStaticDecalRoadManager::_rebuildNodeBatchesThread
32663775 "DecalRoad [{}] packed position exceeded supported local range of {} meters by more than tolerated
          overflow {} m on axis '{}' by {} m (local axis value {}). worst world pos=({}, {}, {}), leaf origin=({"
32663488 "DecalRoad [{}] has invalid distance fade parameters: {} {}"
32663104 "Material {} dont support instancing"
32662456 "*** Built static data in {:5.3f}s"
```

The numeric limits in the first two messages are *runtime-formatted arguments*, not literals, so the exact
metres/vertex counts **could not be recovered by string inspection — they are `unverified`.** The official docs
describe the same behaviour qualitatively: *"The decal road renderer packs vertex positions relative to block
origins. Large decal road batches are split into blocks so packed local positions remain within a supported
range… Avoid extremely long single roads with huge uninterrupted spans. Split very large roads into smaller
sections."*

Sources: `F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe` strings (BeamNG 0.39.4.0.20972);
<https://docs.beamng.com/modding/levels/level_classes/decalorad/> (sections "Packed vertex range", "Batching and rendering")

### B11. Road elevation relative to terrain

`DecalRoad` is **not road geometry and does not create elevation**. It is a decal projected/clipped onto
existing surfaces. Behaviour, verbatim from the engine and confirmed by the data:

* "When `overObjects` is disabled, node height is projected to terrain height during regeneration."
* "By default, `DecalRoad` projects onto terrain. It samples terrain height and adjusts generated edges to sit
  on the terrain surface. This means the node Z value is often less important for terrain roads, because the
  road is snapped to terrain during regeneration."
* With `overObjects: true` the road can clip onto static meshes (bridges, tunnels, overpasses) and the node Z
  becomes meaningful.
* There is **no `roadElevation` field** and no `z` offset field beyond `decalBias` (`"the hovering height of the
  decal over the terrain"`) and `zBias`.
* Spline smoothing exists in two modes: legacy Catmull-Rom with `breakAngle`, and `improvedSpline` with
  `detail`/`smoothness`/`startTangent`/`endTangent`/`looped`.
* **Physical road surface must come from elsewhere** — terrain heightmap, a `TSStatic` mesh, or a `MeshRoad`.
  The engine will refuse (and warn) if a level has no terrain and the decal is not `overObjects`.
* Regeneration is triggered by terrain edits, moving static objects, node edits, material changes, spline
  settings, and bias/fade changes.

Sources: <https://docs.beamng.com/modding/levels/level_classes/decalorad/> (sections "Terrain projection",
"overObjects", "zBias and decalBias", "Terrain and static object updates"); engine field doc strings listed in §B8
(BeamNG 0.39.4.0.20972)

---

## C. Objects and performance

### C12. Object types present in level files, with counts

Full class census for the four largest shipped levels (parsed from every `items.level.json`; prefab contents
counted separately):

| Class | east_coast_usa | west_coast_usa | italy | Utah |
| --- | ---: | ---: | ---: | ---: |
| **TOTAL objects in `items.level.json`** | **6,857** | **41,513** | **23,180** | **7,520** |
| `TSStatic` | 2,187 | **22,067** | **13,971** | 2,608 |
| `DecalRoad` | 3,115 | **12,752** | 7,900 | 3,554 |
| `Prefab` (instances) | 888 | 610 | 217 | 436 |
| `SpotLight` | 0 | 3,371 | 148 | 349 |
| `PointLight` | 1 | 682 | 0 | 34 |
| `BeamNGWaypoint` | 198 | 838 | 196 | 127 |
| `SFXSpace` | 135 | 372 | 382 | 122 |
| `SimGroup` | 64 | 356 | 86 | 77 |
| `ParticleEmitterNode` | 62 | 1 | 47 | 0 |
| `Portal` | 0 | 97 | 4 | 56 |
| `Zone` | 0 | 33 | 3 | 34 |
| `BeamNGTrigger` | 31 | 72 | 25 | 14 |
| `GroundCover` | 23 | 14 | 27 | 8 |
| `River` | 13 | 2 | 7 | 1 |
| `BeamNGPointOfInterest` | 10 | 59 | 33 | 4 |
| `BeamNGGameplayArea` | 10 | 59 | 33 | 16 |
| `OcclusionVolume` | 8 | 0 | 0 | 0 |
| `WaterBlock` | 6 | 3 | 6 | 2 |
| `SpawnSphere` | 5 | 15 | 11 | 7 |
| `Precipitation` | 2 | 1 | 1 | 0 |
| `WaterPlane` | 1 | 1 | 1 | 0 |
| `TerrainBlock` | 1 | 1 | 1 | 1 |
| `Forest` | 1 | 1 | 1 | 1 |
| `ForestWindEmitter` | 1 | 1 | 1 | 1 |
| `ScatterSky` | 1 | 1 | 1 | 1 |
| `TimeOfDay` | 1 | 1 | 1 | 1 |
| `LevelInfo` | 1 | 1 | 1 | 1 |
| `CloudLayer` | 1 | 1 | 1 | 1 |
| `CameraBookmark` | 90 | 28 | 65 | 45 |
| `SFXEmitter` | 1 | 73 | 10 | 19 |
| `ForestBrushElement` | 0 | 0 | 0 | 0 (9 in gridmap_v2) |
| — plus smaller groups: `Trigger`, `TrafficSignal`, `BeamNGBusRoute`, `DecalInstance` etc. | | | | |

**Additional objects hidden inside `.prefab` / `.prefab.json` files** (these are *not* counted above, but are
instantiated into the scene at level load):

| Level | prefab files | objects inside them | classes |
| --- | ---: | ---: | --- |
| west_coast_usa | 127 | **2,198** | TSStatic 1,999; DecalRoad 136; SimGroup 49; Prefab 13; SpotLight 1 |
| Utah | 69 | 143 | TSStatic 104; SimGroup 28; PointLight 10; SpotLight 1 |
| east_coast_usa | 49 | 103 | TSStatic 101; SimGroup 2 |
| italy | 111 | 3 | TSStatic 2; SimGroup 1 |

**Forest items are the dominant object population by a wide margin** (see §C14).

Sources: full parse of `F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\{east_coast_usa,west_coast_usa,italy,Utah}.zip` (BeamNG 0.39.4.0.20972)

### C13. Object count limits, draw calls, instancing, LOD

**No object-count limit, draw-call limit, or `maxObjects` symbol exists.** Verbatim results of the executable
byte scan: `maxObjects` → 0 hits; `too many objects` / `Too many objects` → 0 hits; `object limit` → 0 hits.
The engine does **not** impose a scene object cap that could be found; the binding constraints are CPU/GPU
budget, not a hard number.

**Instancing / static batching.** BeamNG 0.39 has a "scene static manager" that batches static geometry:

```
32660880 'fnSceneStaticManagerRender_create'
32660920 'SceneStaticManagerRender* create()'
32662416 'StaticRenderManager'
32662440 'DecalManager'
32662456 '*** Built static data in {:5.3f}s'
32663104 'Material {} dont support instancing'
32663144 'engine::SceneStaticManager::validateShape'
32663192 'rebuildStaticNodes'
32663216 'Shape with invalid detail levels: {}'
32663416 'engine::SceneStaticDecalRoadManager::addInstance'
30760926 'buffer budget exceeded! Additional instance will be missing'
```

Note `30760926`: there **is** a finite instancing buffer budget, and overflow silently drops instances
("Additional instance will be missing"). The numeric size is not a literal string → **unverified**.

`TSStatic` exposes instancing-related fields (verbatim doc strings):

```
32960696 '@brief The activate per instance render properties'  -> 32960752 'useInstanceRenderData'
32960776 'instanceColor'   32960824 'instanceColor1'   32960872 'instanceColor2'
32960416 'Enables detailed culling of meshes within the TSStatic. Should only be used with large complex shapes like buildings which contain many submeshes.' -> 32960568 'meshCulling'
32960592 'Enables translucent sorting of the TSStatic by its origin instead of the bounds.' -> 32960680 'originSort'
32960944 'collisionType'   32960960 'decalType'   32961128 'prebuildCollisionData'
32961296 'forceDetail'   "Forces rendering to a particular detail level."
32960232 'Disable render optimizations for static objects'
```

Real `TSStatic` example (from `template`), showing the fields actually written:

```
{"class":"TSStatic","__parent":"terrain","position":[0,0,115.727997],"collisionType":"Visible Mesh Final","decalType":"None","isRenderEnabled":false,"shapeName":"/levels/template/art/shapes/misc/s_template_backdrop.dae","useInstanceRenderData":true}
```

**LOD system.** There are three independent LOD mechanisms:

1. **Shape LODs** — `.dae`/`.cdae` shapes carry detail levels; `TSStatic.forceDetail` forces one.
2. **Forest LOD + imposters** — per-item screen-size LOD selection, plus per-cell imposter batching.
   Debug switches: `Forest::smForceImposters`, `Forest::smDisableImposters`
   Stats: `$Forest::totalCells`, `$Forest::cellsRendered`, `$Forest::cellItemsRendered`,
   `$Forest::cellsBatched`, `$Forest::cellItemsBatched`.
3. **Terrain LOD** — quadtree cells with **minimum cell size 64** (`docs: "minimum terrain cell size = 64"`),
   driven by `screenError` and the globals `$pref::Terrain::lodScale`, `$pref::Terrain::detailScale`.
   Debug counter: `$TerrainBlock::cellsRendered`, `$TerrainBlock::overrideCells`, `$TerrainBlock::drawCalls`.

**Prefab system.** Two generations coexist: `Prefab` (legacy, `filename` + `loadMode`) and `PrefabV2`
(JSON, `.prefab.json`). Runtime creation APIs (verbatim Lua):

```lua
-- lua\ge\ge_utils.lua:715
function loadPrefabv2Template(filename)
  local prefabTemplateName = prefabFilenameToTemplateName(filename)
  local prefab = PrefabV2()
  prefab:load(filename)
  prefab:registerObject(prefabTemplateName)
  …
end

-- lua\ge\ge_utils.lua:727
function addPrefab(objName, objFileName, objPos, objRotation, objScale, useGlobalTranslation)
  local obj = scenetree[objName]
  if not obj then
    local p = createObject('Prefab')
    p.filename = String(objFileName)
    p.loadMode = 1 --'Manual'
    p:setField('position', '', objPos)
    …
    p:registerObject(objName)
    return p
  …
end

-- lua\ge\ge_utils.lua:749
function spawnPrefab(objName, objFileName, objPos, objRotation, objScale, useGlobalTranslation)
```

Constraint: `'TerrainBlock objects are not valid within a Prefab'` (exe 32916960).

Sources: `F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe` string scan;
`F:\SteamLibrary\steamapps\common\BeamNG.drive\lua\ge\ge_utils.lua`;
<https://docs.beamng.com/modding/levels/level_classes/forest/>;
<https://docs.beamng.com/modding/levels/level_formats/terrain/> (BeamNG 0.39.4.0.20972)

### C14. The Forest system

**Scene object.** Exactly one `Forest` per level in shipped content. Real example verbatim
(`east_coast_usa`, `…/Level_object/vegetation/items.level.json`):

```
{"class":"ForestWindEmitter","persistentId":"35132e0a-10d8-47a6-ac54-3f6f8523d2b2","__parent":"vegetation","position":[-77.7248993,204.464996,104.431],"gustYawFrequency":0.100000001,"hasMount":"0","strength":0.150000006}
{"name":"theForest","class":"Forest","persistentId":"1acda816-9f86-4554-9013-5eaa77f4b451","__parent":"vegetation","lodReflectScalar":0.150000006}
```

**`Forest` field list** (exe doc strings + real data): `lodReflectScalar` ("Scalar applied to the farclip
distance when Forest renders into a reflection.") and the legacy `dataFile` ("The source forest data file.").
That is essentially it — placement lives in files, not in the object.

**Where trees are stored.** One file per item type under `levels/<level>/forest/<type>.forest4.json`, one JSON
object per line. Verbatim from `template.zip` → `levels/template/forest/pac_rock_large_1.forest4.json`:

```
{"ctxid":1,"pos":[200.229736,140.311386,181.151382],"rotationMatrix":[-0.365135998,-0.930954158,0,0.930954158,-0.365135998,0,0,0,0.999999881],"scale":2.34052372,"type":"pac_rock_large_1"}
```

Item type definitions live in `levels/<level>/art/forest/managedItemData.json`, keyed by internal name.
Verbatim entries:

```json
"Clover_closetop": {
  "name": "Clover_closetop", "internalName": "Clover_closetop", "class": "ForestItemData",
  "persistentId": "47a054a0-d13b-4421-bd12-5ca590c8759a",
  "radius": 0.300000012,
  "shapeFile": "levels/east_coast_usa/art/shapes/groundcover/Clover_closetop.dae",
  "snapRotationToTerrain": true
},
"container_01_a": {
  "name": "container_01_a", "internalName": "container_01_a", "class": "TSForestItemData",
  "persistentId": "d277728c-f970-4d0e-b364-d0a9fc321541",
  "dampingCoefficient": 0, "radius": 0.800000012,
  "shapeFile": "levels/east_coast_usa/art/shapes/misc/container_01_a.dae"
}
```

Both `ForestItemData` and `TSForestItemData` classes are in use.

**Density control / painting.** `ForestBrush` (a `SimGroup`-parented container) holds `ForestBrushElement`
children. Verbatim from `template.zip` → `levels/template/main.forestbrushes4.json`:

```
{"name":"ForestBrush_Rock_large","internalName":"Pac_Rock_large","class":"ForestBrush","persistentId":"f3042d8f-b51c-44f4-a02a-906f6112e03e","__parent":"ForestBrushGroup"}
{"internalName":"pac_rock_large_1","class":"ForestBrushElement","persistentId":"7ef77341-7d80-43e6-82cc-747f59e5e3cd","__parent":"ForestBrush_Rock_large","forestItemData":"pac_rock_large_1","scaleMax":1.20000005,"scaleMin":0.800000012,"sinkRadius":3}
{"internalName":"pac_rock_medium_1","class":"ForestBrushElement","persistentId":"115ce870-fc64-44b4-bd78-fc92109b1f19","__parent":"ForestBrush_Rock_medium","forestItemData":"pac_rock_medium_1","probability":0.300000012,"scaleMax":1.20000005,"scaleMin":0.800000012,"sinkMax":0.5}
{"internalName":"pac_rock_tiny_1","class":"ForestBrushElement","persistentId":"34246680-0bab-4833-94cd-2844ccb8f8ca","__parent":"ForestBrush_Pac_Rock_Tiny","forestItemData":"pac_rock_tiny_1","probability":4,"scaleMax":1.20000005,"sinkMax":0.0700000003}
```

`ForestBrushElement` fields: `forestItemData`, `probability`, `rotationRange`, `scaleMin`, `scaleMax`,
`scaleExponent`, `sinkMin`, `sinkMax`, `sinkRadius`, `slopeMin`, `slopeMax`, `elevationMin`, `elevationMax`.

**Does it scale to large areas?** Yes — with hard evidence. Forest items are organised into spatial
**cells/buckets** ("If the cell has too many items, it splits into subcells"), stored in a quadtree-like
hierarchy, culled per cell, batched per type, and rendered as imposters at distance. Observed item counts:

| Level | `.forest4.json` files | Total forest items | Distinct types | Items per km² |
| --- | ---: | ---: | ---: | ---: |
| **johnson_valley** | 76 | **663,057** | 76 | 70,260 |
| **italy** | 99 | **206,148** | 99 | 12,287 |
| **east_coast_usa** | 150 | **186,706** | 150 | 44,514 |
| **Utah** | 50 | **160,169** | 50 | 38,187 |
| **west_coast_usa** | 143 | **133,363** | 143 | 31,796 |
| template | 11 | 109,629 | 11 | 104,408 |
| small_island | 41 | 22,852 | 41 | 21,764 |

So **663,057 forest instances in one level is a proven, shipped-by-a-mod data point** in the same engine
version. Extrapolating johnson_valley's density to 31,900 km² gives ~2.2 billion items — clearly infeasible —
so forest density will have to be generated on demand rather than authored level-wide.

Legacy formats `*.forest` and `*.forest.json` are deprecated. `template.zip` still ships
`levels/template/template.forest.json` with the v2 array format:

```json
{ "header":{ "format":"JSON Forest Data v2",
             "format description":"posX, posY, posZ, rotX, rotY, rotZ, rotW, scale",
             "version":2},
  "instances":{ "tro_bush_small":[ [-62.3408,-37.7754,123.484,0,0,0.9938,-0.1109,0.9131], … ] } }
```

Engine warnings for legacy data (verbatim): `'You have old forest files in the level folder. Please resave and
delete them:'`, `' * {} - v2,v3: deprecated. Please remove after upgrade.'`,
`' === Old forest file format detected. Please resave the forest and delete the old files. ==='`,
`'forest data block: \'{}\' have an instance with a scale set below the minimum scale of 0.0001'`,
`'{} items were skipped because their datablocks were not found.'`

**There is no `[Forest]` section.** The `[Forest]` / `[Terrain]` INI-style sections belong to Torque 3D's
`levelinfo.cs`. BeamNG 0.39's `info.json` has **no `[Terrain]` and no `[Forest]` section** — see §C17.

Sources: full parse of the level zips; `F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe`
strings at offsets 30559652–30566080; <https://docs.beamng.com/modding/levels/level_classes/forest/>
(BeamNG 0.39.4.0.20972)

### C15. Water objects

Three water classes are used in shipped content: **`WaterPlane`** (infinite ocean), **`WaterBlock`** (box
volume) and **`River`** (spline body, `WaterObject` subclass with `nodes`). A fourth engine class,
`WaterMesh`, **does not exist**.

Real `WaterPlane`, verbatim (from `east_coast_usa`, `…/Level_object/sea/items.level.json`, trimmed at the end
by the print limit — the leading fields are exact):

```
{"name":"Ocean","class":"WaterPlane","persistentId":"3d686003-49c8-4dc4-a2e3-bcaf46197014","__parent":"sea","position":[0,0,8.92973042],"Foam":[{"foamDir":[0,1],"foamOpacity":null,"foamSpeed":0.00999999978,"foamTexScale":null},{"foamDir":[0,-1],"foamOpacity":5,"foamSpeed":0.00999999978,"foamTexScale":[4,4]}],"Ripples (texture animation)":[{"rippleDir":[0,-1],"rippleMagnitude":0.5,"rippleSpeed":0.00800000038,"rippleTexScale":[10,10]},{"rippleDir":[0.707000017,0.707000017],"rippleMagnitude":0.5,"rippleSpeed":0.0500000007,"rippleTexScale":[2,2]},{"rippleDir":[-0.5,0.860000014],"rippleMagnitude":0.349999994,"rippleSpeed":0.00300000003,"rippleTexScale":[120,120]}],"Waves (vertex undulation)":[{"waveDir":[0,1],"waveMagnitude":0.5,"waveSpeed":2},{"waveDir":[-0.707000017,-0.707000017],"waveMagnitude":0.104999997,"waveSpeed":1},{"waveDir":[0.5,0.860000014],"waveMagnitude":0.100000001,"waveSpeed":2}],"baseColor":[40,197,254,255],"clarity":0.25,"cubemap":"cubemap_ocean_reflection","depthGradientMax":5,"depthGradientTex":"levels/east_coast_usa/art/water/depthcolor_ramp.dds","distortEndDist":15,"distortFullDepth":5,"foamAmbientLerp":1,"foamMaxDepth":0.75,"foamRippleInfluence":0.00499999989,"foamTex":"core/art/water/foam.dds","fresnelBias":-0.5,"fresnelPower":0.200000003,"fullReflect":false,"gridElementSize":0.800000012,"gridSize":220,"overallFoamOpacity":3.5,"overallRippleMagnitude":0.200000003,"overallWaveMagnitude":0.100000001,"reflectDetailAdjust":0,"reflectMaxRateMs":1,"rippleTex":"le…
```

Real `WaterBlock` (from `italy`, `…/Level_objects/Water/items.level.json`, again exact up to the print limit):

```
{"name":"quarry_WaterBlock_11","class":"WaterBlock","__parent":"water","position":[450.713043,-1776.54565,246.616714],"Foam":[{},{}],"Ripples (texture animation)":[{"rippleDir":[0,1],"rippleMagnitude":0.800000012,"rippleSpeed":0.00100000005,"rippleTexScale":[12,12]},{"rippleDir":[0,1],"rippleMagnitude":null,"rippleSpeed":0.0199999996,"rippleTexScale":[6,6]},{"rippleDir":[0.699999988,-0.699999988],"rippleMagnitude":1,"rippleSpeed":0.0199999996,"rippleTexScale":[3,3]}],"Waves (vertex undulation)":[{"waveDir":[0,1],"waveMagnitude":0.200000003,"waveSpeed":1},{"waveDir":[0.707000017,0.707000017],"waveMagnitude":0.200000003,"waveSpeed":1},{"waveDir":[0.5,0.860000014],"waveMagnitude":0.200000003,"waveSpeed":1}],"baseColor":[189,253,255,255],"cubemap":"cubemap_italy_reflection","depthGradientMax":30,"depthGradientTex":"/assets/materials/tileable/water/depthcolor_ramp/depthcolor_ramp_italy_muddy_b.png","foamAmbientLerp":1.29999995,"foamMaxDepth":0.150000006,"foamRippleInfluence":0.0149999997,"foamTex":"/assets/materials/tileable/water/water_effects/foam2_b.color.dds","fresnelBias":0.200000003,"fresnelPower":20,"fullReflect":false,"gridElementSize":1,"gridSize":1,"overallRippleMagnitude":0.200000003,"overallWaveMagnitude":0,"reflectivity":0.800000012,"rippleTex":"/assets/materials/tileable/water/water_effects/ripple_nm.normal.dds","rotationMatrix":[0.715829432,0.698275268,0,-0.698275268,0.715829432,0,0,0,1],"scale":[36.2570419,36.2570419,3.6257019],"specularPower":200,"waterFogDensity"…
```

Real `River` — note the `nodes` array of 8-tuples `[x, y, z, width, depth, ?, ?, ?]`
(from `Utah`, `…/Level_objects/water/items.level.json`):

```
{"class":"River","persistentId":"42452b19-447f-4a99-b9e5-30546fc10748","__parent":"water","position":[759.218323,394.767273,133.829193],"Foam":[{"foamDir":[0,-1],"foamSpeed":0.075000003,"foamTexScale":[3,3]},{"foamDir":[0.400000006,0],"foamSpeed":0.075000003,"foamTexScale":[10,10]}],"Ripples (texture animation)":[{"rippleDir":[0,-1],"rippleMagnitude":0.699999988,"rippleSpeed":-1.20000005,"rippleTexScale":[0.5,0.5]},{"rippleDir":[0.5,1],"rippleMagnitude":0.5,"rippleSpeed":0.0700000003,"rippleTexScale":[7,7]},{"rippleDir":[-0.5,1],"rippleMagnitude":0.400000006,"rippleSpeed":0.0170000009,"rippleTexScale":[30,50]}],"Waves (vertex undulation)":[{"waveDir":[0,1],"waveMagnitude":0.200000003,"waveSpeed":3},{"waveDir":[0.707000017,0.707000017],"waveMagnitude":0.200000003,"waveSpeed":1.5},{"waveDir":[0.5,0.860000014],"waveMagnitude":0.200000003,"waveSpeed":1.5}],"baseColor":[248,208,169,255],"cubemap":"cubemap_utah_reflection","depthGradientMax":5,"depthGradientTex":"/levels/Utah/art/water/depthcolor_ramp.png","distortEndDist":1,"flowMagnitudePhysics":2.75,"foamMaxDepth":0.75,"foamRippleInfluence":0.100000001,"foamTex":"core/art/water/foam.dds","fresnelBias":0,"fresnelPower":3,"fullReflect":false,"lowLODDistance":80,"nodes":[[759.218323,394.767273,133.829193,55,5,0,0,1],[723.719299,257.834137,133.700256,55,5,0,0,1],[668.648804,197.923676,133.707703,55,5,0,0,1],[626.67041,176.635101,132.313721,55,5,0,0,1],[599.275513,143.702042,132.347549,55,5,0,0,1],[593.225403,97.7160263,132.398758,55…
```

Counts of water objects in shipped levels: `WaterPlane` 1 per level (template, east_coast_usa, west_coast_usa,
italy, small_island, johnson_valley); `WaterBlock` 6 (east_coast_usa), 6 (italy), 3 (west_coast_usa),
2 (Utah), 1 (gridmap_v2); `River` 13 (east_coast_usa), 7 (italy), 3 (gridmap_v2), 2 (west_coast_usa), 1 (Utah).

`River` Lua API (executable): `fnRiver_regenerate`, `setMetersPerSegment`, `setBatchSize`,
`setMaxDivisionSize`, `getNodeCount/Transform/Position/Normal/Width/Depth`, `insertNode`,
`insertNodeNoRegen(Point3F pos, f32 width, f32 depth, Point3F normal, u32 idx)`, `deleteNode`, `getEdgeCount`,
`getTopRightEdgePosition`, `collideRay`, plus fields `flowMagnitudePhysics`, `lowLODDistance`.

Sources: local files in the level zips; `F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe`
strings at offsets 32627090–32638568 (BeamNG 0.39.4.0.20972)

### C16. Runtime object spawning / streaming

**There is no `LevelObjectStreaming` API.** Verbatim scan results: `LevelObjectStreaming` → 0 hits in the
executable; 0 hits in all Lua. `spawnObject` exists but only as `BeamEngine:spawnObject(...)`, a **vehicle**
spawner used by console benchmarks (`lua\console\bananabench.lua`, `lua\console\simpleload.lua`).

What **does** exist for runtime scene manipulation:

| API | File | Purpose |
| --- | --- | --- |
| `createObject('Prefab')` | `lua\ge\ge_utils.lua:731` | create a scene object at runtime |
| `PrefabV2()` / `prefab:load(file)` / `prefab:registerObject(name)` | `lua\ge\ge_utils.lua:715-725` | load and register a prefab template |
| `spawnPrefab(objName, file, pos, rot, scale, useGlobalTranslation)` | `lua\ge\ge_utils.lua:749` | instantiate a prefab |
| `scenetree.findObject(name)` / `findClassObjects(class)` / `findObjectById(id)` | widespread | scene tree lookup |
| `SimGroup:addObject(obj)` | `terrainEditor.lua:1272` | reparent into the mission tree |
| `core_vehicles.spawnNewVehicle(model, opts)` | `lua\ge\extensions\core\dynamicProps.lua:104` | spawn a vehicle (also used as a physics prop) |
| `obj:spawnProps()` / `despawnProp` | `lua\ge\extensions\core\dynamicProps.lua:94-174` | see below |

**The only real "streaming" pattern in the engine is `DynamicProps`** — it replaces forest items with pooled
physics props inside a view range:

```lua
-- lua\ge\extensions\core\dynamicProps.lua
object.spawnInViewRange = data.spawnInViewRange or 100
…
local spawningOptions = sanitizeVehicleSpawnOptions(self.propName, options)
spawningOptions.autoEnterVehicle = false
local vehId = core_vehicles.spawnNewVehicle(spawningOptions.model, spawningOptions):getId()
…
forest = scenetree.findObject("theForest")
…
local spawnCount = math.min(self.poolSize, #locationsInSight)
```

This is a *pool of vehicles*, not a generic level-object streamer. **Honest conclusion:** BeamNG 0.39 has no
generic runtime level-object streaming subsystem; anything resembling it must be built in Lua on top of
`createObject`/`PrefabV2`/`spawnPrefab` and manual `SimGroup` management. Whether such a Lua-built streamer can
keep up with a 220 km world is **unverified**.

**Forest cells** provide the closest thing to built-in spatial streaming for scatter objects (cells subdivide,
cull, batch, and imposter by distance).

Also present: `lua\ge\extensions\util\nodeStream.lua` (a debug/dev utility, unrelated to level geometry), and
`lua\ge\extensions\ui\uiNavi.lua:359` which iterates terrain blocks to build navigator tiles.
The new **`TerrainSdf`** subsystem (engine strings: `TerrainSdf dig: touched={} created={} totalBricks={} …`,
`digSphereObject`, `digShapeObject`, `placeSphereObject`, `gyroid`) is a brick-based SDF terrain-editing
system introduced in this version; its suitability for procedural large-area generation is **unverified**.

Sources: `F:\SteamLibrary\steamapps\common\BeamNG.drive\lua\ge\ge_utils.lua`,
`...\lua\ge\extensions\core\dynamicProps.lua`, `...\lua\ge\extensions\core\terrain.lua`,
`...\Bin64\BeamNG.drive.x64.exe` strings (BeamNG 0.39.4.0.20972)

### C17. `info.json` — full field list

`info.json` lives at `levels/<name>/info.json`. If missing, the level still loads but UI metadata is
incomplete and a warning is logged.

**Complete field list** (union of the docs and every shipped/mod `info.json` inspected):

| Field | Type | Default / notes |
| --- | --- | --- |
| `title` | string | Translation ID (e.g. `levels.italy.info.title`) |
| `description` | string | Translation ID |
| `authors` | string | free text |
| `features`, `suitablefor`, `roads`, `biome` | string | Translation IDs or free text |
| `country` | string | e.g. `levels.common.country.italy` |
| `region` | string | e.g. `europe`, `northAmerica` |
| `size` | `[w, h]` | **Advisory only.** Used for minimap/big-map; NOT enforced, and demonstrably inconsistent with the real terrain in several levels |
| `previews` | `[string]` | relative to the level folder; first is the thumbnail |
| `spawnPoints` | `[{translationId, description, logbookEntry, objectname, preview, previews}]` | `objectname` must match a `SpawnSphere` in the scene |
| `defaultSpawnPointName` | string | must match a `spawnPoints[].objectname` |
| `minimap` | `[{file, size:[w,h], offset:[x,y]}]` | multiple tiles allowed |
| `roadRules` | `{rightHandDrive: bool, turnOnRed: bool}` | defaults `false`/`false` |
| `localUnits` | `{gasoline, diesel}` | e.g. `"gallonUS"` |
| `supportsTraffic` | bool | **default `true`** if missing |
| `supportsTimeOfDay` | bool | **default `true`** if missing |
| `defaultDate` | `{year, month, day}` | missing values default to 2026-06-20 |
| `isAuxiliary` | bool | replaces the deprecated `hidden` |
| `disableQuickraceTimeOfDay` | bool | seen on `glow_city` |
| `bigMapProperties` | `{mapBoundaries:{minExtents:[x,y], maxExtents:[x,y]}}` | seen on `gridmap_v2` |

**There is no `loadingScreen` field** — zero occurrences in every `info.json` inspected and in the loader.
There is **no `[Terrain]` and no `[Forest]` INI section**; those were Torque 3D `levelinfo.cs` constructs and
do not exist in the JSON schema.

Verbatim full `info.json` of `italy` (note the trailing comma after the last spawn point, which makes the file
**invalid strict JSON** — the game's parser tolerates it, `json.loads` does not, which is itself a useful fact):

```json
{
  "size":[ 4096, 4096 ],
  "suitablefor": "levels.italy.info.suitablefor",
  "authors":"BeamNG",
  "description": "levels.italy.info.description",
  "roads": "levels.italy.info.roads",
  "title": "levels.italy.info.title",
  "country": "levels.common.country.italy",
  "region": "europe",
  "biome": "levels.italy.info.biome",
  "features": "levels.italy.info.features",
  "previews":[ "italy_preview0.jpg", "italy_preview1.jpg" ],
  "defaultSpawnPointName":"spawn_crossroads",
  "spawnPoints":[
    { "translationId":"levels.italy.spawnpoints.castle_town",
      "objectname":"spawn_castle_town",
      "preview":"spawn_castle_town_preview.jpg" },
    … 9 more …
    { "translationId":"levels.italy.spawnpoints.quarry_parking",
      "objectname":"spawn_quarry_parking",
      "preview":"spawn_quarry_parking.jpg"
	}
  ],

}
```

The minimal `info.json` of `template`:

```json
{
  "defaultDate": {"year": 2026, "month": 6, "day": 20},
  "title": "levels.template.info.title",
  "description": "levels.template.info.description",
  "previews": ["template_preview.png"],
  "size": [2048, 2048],
  "biome": "levels.template.info.description",
  "roads": "levels.template.info.description",
  "suitablefor": "levels.template.info.description",
  "features": "levels.template.info.description",
  "isAuxiliary": true,
  "authors": "BeamNG",
  "supportsTraffic": false,
  "supportsTimeOfDay": false
}
```

Loader behaviour (verbatim Lua, `lua\ge\extensions\core\levels.lua`):
`l.infoPath = d .. '/info.json'`; `log('W', '', 'info.json missing: ' .. l.infoPath)`;
`if info.supportsTraffic == nil then info.supportsTraffic = true end`;
`log("W", "", "Found deprecated flag 'hidden' in level: "…" The flag has been renamed to 'isAuxiliary'")`.

Sources: `lua\ge\extensions\core\levels.lua`; all `info.json` files in the level and mod zips;
<https://docs.beamng.com/modding/levels/level_formats/info/> (BeamNG 0.39.4.0.20972)

### C18. Float precision / world origin / large-world handling

**No floating-origin or world-rebasing system exists.** Verbatim scan of the engine executable:

| Pattern | Hits |
| --- | ---: |
| `floatingOrigin` | **0** |
| `floating origin` | **0** |
| `worldOrigin` | **0** |
| `setWorldOrigin` | **0** |
| `world_origin` | **0** |
| `precisionOffset` | **0** |
| `cameraRelative` | **0** |
| `rebase` | 2 (unrelated: at offsets 36,786,673 / 36,809,116, in bundled library code) |
| `jitter` | 2 (unrelated: TAA/rendering at 35,670,357 / 35,685,938) |
| `camera relative` | 1 (in a `ScatterSky`/`Atmosphere` documentation block, offset 32,601,458) |
| `precision_offset` | 1 (`use_precision_offset`, a **screen-space-shadows depth-bias toggle** at 32,524,128 — *not* a floating origin) |

**Positive evidence that world coordinates are `f32`.** `lua\console\unittests.lua` contains an assertion that
documents actual rounding at ~133 km from the origin:

```lua
a = vec3(0,1337.13371337,-1)
assert(tostring(a) == "(0, 1337.134, -1)", "vec3 float point test 2 failed")
a = vec3(0,133007.133,-1)
assert(tostring(a) == "(0, 133007.141, -1)", "vec3 float point precision test failed")
```

At `y = 133007`, one `f32` ULP is `2^-23 × 131072 = 0.015625 m`, and indeed `133007.133` prints as
`133007.141`. This is a **direct, in-engine demonstration that world coordinates are `f32` and that positional
resolution is ~1.6 cm at 133 km from the origin.** Storage corroboration: positions in level JSON are written
with 7–8 significant figures (e.g. `-1023.44916`, `133.829193`), consistent with `f32`.

**A counter-signal that should be tested, not trusted.** `lua\ge\extensions\core\cameraUpdate.lua:146` carries
this comment:

```lua
camData.vehPos:set(veh:getRefNodeAbsPositionXYZ()) -- vehicle's actual position on a double precision grid
```

`getRefNodeAbsPositionXYZ` is a real engine method (present in the executable's Lua API name table at offset
30,952,376, alongside `getNodeAbsPositionXYZ` and `getRefNodeAbsPosition`), so BeamNG **does** maintain an
authoritative high-precision vehicle reference-node position. Whether that is genuinely `double` in the C++ and
whether it is used for physics collision at distance is **unverified** — a Lua comment is not proof of storage
type. What is verifiable is that the Lua-visible `vec3` type is `f32`-backed, per the unit test above.

**Corroborating weak signal from the shipped tools:** `lua\ge\extensions\ui\apps\minimap\topomap.lua:320`
computes with `gridWidth + 1) * (gridHeight + 1)` and `uiNavi.lua:379` sets `d.terrainSize = mainTile.size`,
while `ui\ui-vue\src\services\navigator-class.js:503` contains commented-out code referencing
`data.squareSize`. These show the UI layer is aware of terrain grid geometry but reveal nothing about precision.

**Practical reading.** At `f32` world coordinates, one ULP is:

| Distance from origin | `f32` ULP |
| ---: | ---: |
| 1 km | 0.06 mm |
| 10 km | 0.98 mm |
| 50 km | 3.8 mm |
| 100 km | 7.6 mm |
| 150 km | 15.3 mm |
| 220 km | 15.3 mm (2^17 range, ULP = 2^-23 × 2^17 = 0.0156 m) |

These are *representational* ULPs. The behaviour that actually matters for driving — suspension jitter,
collision detection, decal projection, and render vertex snapping at those distances — is a separate question
and **must be measured**, not inferred. See §E.

Sources: byte scan of `F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe`;
`F:\SteamLibrary\steamapps\common\BeamNG.drive\lua\console\unittests.lua`;
`...\lua\ge\extensions\core\cameraUpdate.lua`; `...\lua\ge\extensions\editor\rendererComponents.lua`
(BeamNG 0.39.4.0.20972)

### C19. Level loading performance

**What is measured locally.**

| Factor | Evidence |
| --- | --- |
| `.ter` read cost | linear in `3 × terrainSize²`. 8192 → 192 MiB read + 64 MiB `u16` heightmap + 64 MiB layer map in RAM per terrain |
| Static batching cost | engine logs `'*** Built static data in {:5.3f}s'` (offset 32662456) — a per-level static-build timing line |
| Terrain material packing | `TerrainCellMaterial` builds are threaded; cached on disk (`art/terrainMaterialCache/%s.png`, `/temp/art/terrainMaterialCache/<hash>.dds`). Cache invalidated by *"cache not found / outdated / size differs"* |
| Terrain render cells | `'minimum terrain cell size = 64'`; cells generate skirt geometry; cells uploaded to GPU buffers |
| `lightMapSize` | legacy; no measurable modern cost found. Not a loading bottleneck |
| `screenError` | `'Not yet implemented.'` in the engine's own field description, yet the forum evidence (below) is that it *does* change terrain tessellation/LOD and can multiply FPS. Treat the engine doc string as stale |
| Terrain LOD globals | `$pref::Terrain::lodScale`, `$pref::Terrain::detailScale` |
| Level streaming | information about level loading shown in the UI is driven by `core_gamestate.requestEnterLoadingScreen('levels')` / `requestExitLoadingScreen('levels')` (`levels.lua:488-512`). **There is no `loadingScreen` key in `info.json`** |

**Concrete community data point on terrain cost vs. quality (2018, pre-0.39 — treat as indicative only):**
a BeamNG team member reports that on an 8k terrain, cranking `screenError` to 32 (with heavy fog) gave
*"10x the FPS"*, and that a 10 km `DecalRoad` on a 2k map "takes one decal" but on an 8k map "only reaches
about 4 km before the decal just… stops placing on the terrain".
Source: <https://www.beamng.com/threads/turns-out-beamng-doesnt-like-16k-size-terrain-files.60140/>

**No absolute loading-time numbers exist for a level larger than italy (4096, 16.8 km²).** Any claim about
load time for a 220 km world would be invented. This is flagged in the open-questions section.

Sources: executable string scan; `lua\ge\extensions\core\levels.lua`; <https://docs.beamng.com/modding/levels/level_formats/terrain/>
(BeamNG 0.39.4.0.20972)

---

## D. Packaging and export

### D20. Map-mod zip structure for BeamNG 0.39.x

**Verified by inspecting 21 real map mods on this machine.** The required structure is minimal: put
`levels/<name>/…` at the **archive root**.

```
your_map.zip
└── levels/
    └── your_map/
        ├── info.json                  (recommended; level still loads without it, with a warning)
        ├── preview.jpg / preview.png  (referenced by info.json "previews")
        ├── main/                      ← the JSON scene tree (preferred entry point)
        │   ├── items.level.json
        │   └── MissionGroup/…/items.level.json
        ├── art/                       (materials, terrain textures, shapes)
        │   ├── terrains/main.materials.json
        │   └── forest/managedItemData.json
        ├── forest/*.forest4.json      (forest item placement)
        ├── main.decals.json
        ├── main.forestbrushes4.json
        ├── map.json                   (AI navigation graph)
        ├── theTerrain.ter
        └── theTerrain.terrain.json
```

**There is no `main.mis` requirement**, and a `main/` folder is not strictly required either: the loader tries,
in order, `levels/<name>/main/`, then the legacy `main.mis` path, then *any* `*.mis` in the level folder
(`lua\ge\extensions\core\levels.lua:39-55`).

**`mod_info` is optional and only needed for Repository metadata.** Evidence:

* The largest map mod installed here, `Nordschleife tg_m0dsbeamng.zip` (809 entries, 1.2 GB, level
  `levels/Gheo_Nordschleife_2026/`), contains **no `mod_info` folder at all**.
* Of the 21 map mods in `mods\repo\` plus the standalone Nordschleife mod, several (e.g. `mammoth12.zip`,
  `Nordschleife tg_m0dsbeamng.zip`, `etk_w_rev tg_m0dsbeamng.zip`, `agent_traffic_mod tg_m0dsbeamng.zip`) have
  no `mod_info` at all; the ones that do have it carry
  `mod_info/<TAGID>/info.json` + `icon.jpg` + `thumbs/` + `images/`.
* The loader matches it with a regex and treats it as pure presentation metadata
  (`lua\ge\extensions\core\modmanager.lua:126`):
  ```lua
  local modID = string.match(lower, '^/?mod_info/([0-9a-zA-Z]*)/info%.json')
  if modID then
    local mi = jsonDecode(jsonContent, …) or {}
    res.modInfo = { resource_id, current_version_id, resource_version_id,
                    version_string, last_update, resource_date, filename,
                    title, user_id, username, tagid = mi.tagid or modID, via }
  ```
* `levels.lua:25` explicitly skips a folder named `mod_info`: `d == "/levels/mod_info"`.

So a `mod_info/<id>/info.json` is **not required** for a map mod to load or appear. If you do ship one, the
fields the game actually reads are exactly the eleven listed above.

**Also note `levels/<name>/art/` is not the only allowed asset root** — 0.39 introduced a shared `assets/`
folder, documented at <https://docs.beamng.com/modding/levels/assets/>.

Sources: `C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\mods\*.zip` and `...\mods\repo\*.zip`;
`F:\SteamLibrary\steamapps\common\BeamNG.drive\lua\ge\extensions\core\{modmanager,levels}.lua`;
<https://docs.beamng.com/modding/levels/level_creation/section10/> (BeamNG 0.39.4.0.20972)

### D21. Installing a level for local development

Three mechanisms exist; all three are present on this machine.

| Method | Path | Evidence |
| --- | --- | --- |
| **Loose folder in userdata** (best for iteration) | `C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\levels\<name>\` | This machine already has `…\current\levels\Autodrom\` and `…\current\levels\mymap\` — both are partial WIP folders (only `art/` and `forest/` subfolders, **no `info.json`**, so they are not yet loadable levels) |
| **Zip in userdata mods** | `…\current\mods\<anything>.zip` containing `levels/<name>/…` | 6 such zips present, including the 1.2 GB Nordschleife map |
| **Unpacked mod folder** | `…\current\mods\unpacked\<name>\` | `…\current\mods\unpacked\ccd2beam\` exists. `modmanager.lua:189` strips the literal `unpacked/` from the mod name, confirming this is a supported mount point |

Practical recommendation for `ivanovo2beam`: develop as a loose folder under `…\current\levels\ivanovo\`, and
apply the docs' verification pass before shipping — *"Move the unpacked working folder out of the user
levels/mods path. Install only the packaged `.zip`. … If the packaged version works only while the working
folder is present, the package is missing files."*

Source: <https://docs.beamng.com/modding/levels/level_creation/section10/>; direct inspection of
`C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\{levels,mods}`;
`F:\SteamLibrary\steamapps\common\BeamNG.drive\lua\ge\extensions\core\modmanager.lua` (BeamNG 0.39.4.0.20972)

### D22. Official importers and authoring tools shipped with the game

| Tool | Location on disk | What it does |
| --- | --- | --- |
| **Terrain Editor / Terrain Importer** | `lua\ge\extensions\editor\terrainEditor.lua` (3,401 lines) | Full heightmap + opacity-layer + hole-map import. Engine entry point: `terrBlock:importMaps(heightMapTexturePath, metersPerPixel, heightScale, holeMap, materials, textureMaps, flipYAxis)` (`:1266`). Also exports: `exportHeightMap()`, `exportLayerMaps()`, `exportHoleMaps` (engine strings 32974448–32975160). Preset system persists to `levels/<level>/import/*terrainPreset.json` (see `template.zip` → `levels/template/import/import_presetterrainPreset.json`) |
| **Terrain Material Library** | `lua\ge\extensions\editor\terrainMaterialsEditor.lua` | Authors `TerrainMaterial` v1.5 assets, validates texture sizes/channels, offers "Upgrade Terrain Materials" (v1 → v1.5) and "Upgrade Terrain Material file format" (`art/terrains/materials.json` → `main.materials.json`) |
| **World Editor** | `lua\ge\extensions\editor\*` (many files); `ui\…` | The in-game editor: `sceneTree.lua`, `inspector.lua`, `createObjectTool.lua`, `terraform\terraform.lua`, `levelValidator.lua`, `materialEditor.lua`, `biomeTool.lua`, `dynamicDecals`, `resourceChecker\`. Docs: <https://docs.beamng.com/world_editor/> |
| **Terraform tool** | `lua\ge\extensions\editor\terraform\terraform.lua` | Sculpting; uses `tb:getSquareSize()` for cell-accurate work (`:539`) |
| **Master Spline / Auto Road Gen** | `lua\ge\extensions\editor\masterSpline\autoRoadGen.lua` | Generates roads along a master spline; uses `tb:getSquareSize()` (`:192`) |
| **Road Architect** | `roadArchitect\profiles\*.json` (21 profiles) + `roadArchitect\groups\*.json` (12 junction groups) | Road cross-section profile library: `Hwy_1W_1L`, `Hwy_2W_2L`, `Rural_1W_2L`, `Urban_2W_2L_Lamps`, `Tracks_Type1`…`Type6`, `Barrier_Only`, `Lamps_Only`; junction groups `Roundabout - Separator`, `Double_Roundabout`, `RIRO_Intersection`, `Staggered_Jct`, `Double_Y`, `Urban To Highway Split`, … |
| **Track Builder** | `lua\ge\extensions\trackbuilder\trackBuilder.lua` + `lua\ge\extensions\util\trackBuilder\{pieces,splineTrack}.lua` + `trackEditor\*.json` (22 tracks) | Piece-based track construction; `trackEditor\` holds authored track JSON. Docs list it under `/modding/levels/level_creation/` |
| **GroundCover / Biome tools** | `lua\ge\extensions\editor\biomeTool.lua`; `art\biomeTool\biomeTool.json` in levels | Large-scale scatter placement driven by terrain material / mask / lasso |
| **Riverbed tool** | `lua\ge\extensions\editor\toolUtilities\riverbed.lua` | Uses `tb:getSquareSize()` (`:201`) to carve riverbeds |
| **Level Validator** | `lua\ge\extensions\editor\levelValidator.lua` | Checks content against terrain extents (`terrain:getWorldBlockSize()`, `:510`) |
| **Mesh/asset pipeline** | `assimp.dll`, `draco.dll` in `Bin64\`; `.dae` / `.cdae` shapes | DAE import; docs at <https://docs.beamng.com/modding/levels/export_dae/> |
| **BeamNG.tech Terrain and Road Importer** | external BeamNG.tech tool | Documented at <https://docs.beamng.com/beamng_tech/tools/terrainandroadimporter/> — a separate product, not present in the consumer install |

**There is no shipped GIS/OSM/GeoTIFF importer.** All terrain import is via a PNG heightmap
(`R16` read directly as 16-bit; other formats converted with `storedHeight = pixelValue / 255 × 65535`) plus
opacity layer maps from RGBA channels, plus an optional hole map. Any OSM/DEM pipeline for `ivanovo2beam` must
be written outside the game and feed this interface.

Sources: `F:\SteamLibrary\steamapps\common\BeamNG.drive\lua\ge\extensions\{editor,util,trackbuilder}\…`,
`...\roadArchitect\…`, `...\trackEditor\…`, `...\content\levels\template.zip`;
<https://docs.beamng.com/modding/levels/level_formats/terrain/> (sections "Importing terrain", "Heightmap import",
"Opacity/layer import", "Hole map import") (BeamNG 0.39.4.0.20972)

---

## E. Precision experiment design (proposed — NOT run)

**Goal.** Measure whether BeamNG 0.39 can place and simulate a vehicle at 0 / 25 / 50 / 100 / 150 / 200 / 250 km
from the world origin at 1:1 scale with acceptable fidelity, and record the failure mode (visual snapping,
suspension jitter, collision errors, teleport rejection, decal misplacement).

**Design principle: isolate precision from content.** Do not build a real map. Use the smallest possible level
that can place a vehicle at an arbitrary `(x, y)` on flat ground, plus a single straight test road, so that any
observed jitter is attributable to coordinate magnitude and not to the world geometry.

### Files to author (exactly these, nothing else)

All under the loose dev folder `C:\Users\kiril\AppData\Local\BeamNG\BeamNG.drive\current\levels\prectest\`:

| # | Path | Content |
| --- | --- | --- |
| 1 | `info.json` | Minimal: `title`, `description`, `previews:["preview.png"]`, `size:[500000,500000]`, `spawnPoints:[{"translationId":"Origin","objectname":"spawn_origin","preview":"preview.png"}]`, `defaultSpawnPointName:"spawn_origin"`, `authors`, `supportsTraffic:false`, `supportsTimeOfDay:false`, `isAuxiliary:false` |
| 2 | `preview.png` | any small PNG (the loader warns but does not fail without it) |
| 3 | `main/MissionGroup/items.level.json` | `{"name":"MissionGroup","class":"SimGroup","enabled":"1"}` |
| 4 | `main/MissionGroup/Level_objects/items.level.json` | one `TerrainBlock` **and** one `LevelInfo`, both verbatim copies of the template forms with our values (see below) |
| 5 | `main/MissionGroup/Level_objects/Water/items.level.json` | one `WaterPlane` (copy the template's, `position:[0,0,0]`) — optional, only if a horizon reference is wanted |
| 6 | `main/MissionGroup/Level_objects/sky_and_sun/items.level.json` | one `ScatterSky` + one `TimeOfDay` copied from `template.zip`, with a fixed noon time so lighting is identical across runs |
| 7 | `main/MissionGroup/roads/items.level.json` | **one** `DecalRoad`, straight, along the +X axis, material `road_asphalt` style, `textureLength:8`, `renderPriority:10`, `improvedSpline:true`, `detail:0.1`, `drivability:1`, 3 nodes at x = 0, 1000, 2000, width 8, `overObjects:false` |
| 8 | `main/MissionGroup/spawnpoints/items.level.json` | N `SpawnSphere` objects named `spawn_origin`, `spawn_25km`, `spawn_50km`, `spawn_100km`, `spawn_150km`, `spawn_200km`, `spawn_250km`, each at `[D, 0, terrainBaseZ + 2]`, in **separate** runs/levels rather than all at once if `TerrainBlock` extent is a problem |
| 9 | `theTerrain.ter` + `theTerrain.terrain.json` | a **flat** `terrainSize = 256`, version-9 `.ter`: 1 header byte `09`, `u32 size = 256`, `u16[65536]` all set to the value that yields the desired base height, `u8[65536]` all zero, `u32 materialCount = 1`, `c1 "Grass"` |
| 10 | `art/terrains/main.materials.json` | a single `TerrainMaterial` named `Grass` (copy `template`'s `Grass` entry) + a `TerrainMaterialTextureSet` named `prectestTerrainMaterialTextureSet` |
| 11 | `main/MissionGroup/Level_objects/terrain/items.level.json` | the `TerrainBlock` itself, **`squareSize` swept as the independent variable** (see below) |
| 12 | (optional) `prectest_probe.lua` | a small dev script in `…\current\lua\ge\extensions\` that logs, twice per second: `veh:getRefNodeAbsPositionXYZ()`, `veh:getNodeAbsPositionXYZ(0)`, `veh:getVelocityXYZ()`, node 0's Z over 100 frames, and the standard deviation of that Z series |

### The one non-obvious trick that makes this experiment possible

A single `TerrainBlock` cannot exceed 8192 m, so 250 km cannot be laid out along one terrain. **Do not tile.**
Instead, keep the *origin* fixed and move the *spawn point and the road* by re-positioning or re-authoring the
single 256×256 terrain so that its centre sits at the target distance, sweeping the independent variable across
seven separate level loads. Two equivalent ways:

* **Preferred:** one level, seven spawn spheres, and slide the `TerrainBlock.position` so the terrain covers the
  whole 0…250 km span. This requires `squareSize ≈ 1000` (256 × 1000 = 256 km) — a flat terrain is the one case
  where a huge `squareSize` is harmless, because there is no relief to lose.
* **Fallback:** seven copies of the level folder (`prectest_0`, `prectest_25`, …) with the `TerrainBlock`
  centred on each distance. Slower to run, but immune to any large-`squareSize` behaviour.

### Procedure

1. Spawn at the origin. Record 60 s of baseline data: mean and standard deviation of the vehicle's rest height,
   node-0 Z, and suspension travel; screenshot the horizon; note the frame time.
2. For each distance `D ∈ {25, 50, 100, 150, 200, 250} km`: teleport (or spawn directly) to `(D, 0, baseZ+2)`,
   let the vehicle settle, then record the identical 60 s dataset.
3. At each distance, drive forward 200 m along the straight `DecalRoad` in a straight line and record:
   whether the decal renders, whether it visibly shifts relative to the wheels, and whether the vehicle tracks
   straight (any lateral drift indicates collision/projection error).
4. Record frame time and any log lines matching `exceed`, `precis`, `range`, `NaN`, or `inf`.
5. Additionally, at each distance, place a test object (a `TSStatic` cube) at a *known* 10.000 m offset from the
   spawn point and measure the in-game distance to the vehicle with the built-in measuring/waypoint tool. Any
   deviation from 10.000 m is the practical positional error at that distance.

### Metrics and the decision they drive

| Metric | Why it matters | Decision threshold (proposed) |
| --- | --- | --- |
| σ(node-0 Z) at rest vs. at origin | direct measure of physics jitter at magnitude | if σ grows super-linearly and exceeds ~1 cm at 100 km, a single-origin 220 km world is unsafe |
| Measured vs. true 10 m offset | end-to-end positional error | error > 5 cm at max distance ⇒ world origin must be rebased per region |
| DecalRoad visibility/tracking at distance | the packed-vertex-range limit (§B10) | a road that stops rendering mid-span ⇒ roads must be split per tile |
| Frame time at each distance | whether the renderer degrades with magnitude | 2× slowdown ⇒ tiling by render cost, not just by terrain size |
| Any `NaN`/`inf`/range log line | hard failures | any occurrence is disqualifying for that distance |

### What this experiment cannot tell you

It uses a flat, nearly empty level. It will **not** measure load time, memory, object counts, forest density,
AI graph cost, or traffic behaviour at scale. Those need a second experiment on a real tile. It also cannot
distinguish "`f32` world coordinates" from "`f64` physics + `f32` rendering" except indirectly, via the
magnitude of the measured error.

---

## Open questions / unverified

Everything below is **not confirmed** and must not be baked into an architecture decision without testing.

1. **Whether BeamNG 0.39 can host multiple TerrainBlocks that form one seamless world.** The engine supports
   multiple terrain blocks in principle (`getTerrainUnderWorldPoint()` returns a block ID; `terrain.lua` uses
   `scenetree.findClassObjects("TerrainBlock")`), but **no shipped level and no mod on this machine does real
   multi-tile composition** — the one level with four `TerrainBlock` objects has four identical duplicates at
   the same position. No seam-stitching, LOD-joining, or cross-tile tooling was found. **Unverified.**
2. **The exact vertex-count and packed-position limits for DecalRoad batches.** The engine has the messages
   (`"amount of vertices '{}' is greater the maximum allowed '{}'"` and
   `"packed position exceeded supported local range of {} meters…"`) but the numeric limits are runtime
   arguments I could not recover from the binary. **Value unverified.**
3. **The exact size of the instancing buffer budget** (`'buffer budget exceeded! Additional instance will be missing'`).
   **Value unverified.**
4. **Whether `getRefNodeAbsPositionXYZ` is genuinely double-precision in C++.** Only a Lua comment claims
   `"double precision grid"`. The Lua `vec3` type is demonstrably `f32` (unit test at 133 km). **Unverified.**
5. **Whether jitter is actually acceptable at 50–250 km.** No measurement exists. §E is designed to produce it.
   Do not assume `f32` ULPs are the whole story — physics solvers, collision broadphases and decal projection
   may fail at much shorter distances, or not at all.
6. **Whether there is any hidden hard world-extent limit enforced outside the string table** (e.g. in
   serialisation of `persistentId`, physics broadphase cell indices, or the `f32` AABB format). No such string
   was found, but **absence of a string is not absence of a limit**.
7. **Whether `squareSize` values above ~1 m are usable for terrain relief at driving scale.** `squareSize` is
   unvalidated in the binary, and the engine's own new-TerrainBlock default is 8, but no shipped level uses
   more than 1 and the two 8192-grid maps both use ≤ 1. **Unverified** whether `squareSize = 4` terrain is
   visually or physically acceptable for a driving map.
8. **Level load time and memory for anything larger than italy (4096 grid, 16.8 km²).** No measurement exists
   above that. Extrapolating would be invention.
9. **Whether the new `TerrainSdf` subsystem** (`digSphereObject`, `placeSphereObject`, brick/dirty-queue
   logging) **can generate or replace heightfield terrain at scale**, or is purely an editor sculpting aid.
   **Unverified.**
10. **Whether a Lua-driven object streamer** built on `createObject`/`PrefabV2`/`spawnPrefab` can sustain the
    patch rates a 220 km world would need. No such streamer exists in BeamNG or in any mod inspected.
    **Unverified.**
11. **Ground cover size coupling:** `'Terrain block size ({}x{}) and mask size ({}x{}) does not match!'` — the
    exact semantics (pixels vs. metres vs. cells) were not established. **Unverified.**
12. **AI/navigation scaling.** `map.json` and `signals.json` exist per level; the largest seen is
    west_coast_usa's `signals.json` (120 KB) and `slotTrafficRoadNodeSnapshot.json` (901 KB). Whether the
    navigation graph can be built for 15–97 M DecalRoads is unknown. **Unverified.**
13. **`MeshRoad` as a production asset type.** The class exists and is documented, but ships in **zero** levels
    on this machine, so there is no reference implementation. **Unverified.**
14. **The `.ter` version-8 layout.** `americanroad/mymap` ships a version-8 `.ter` at 8192 that is
    ~7 bytes/cell rather than 3. Its exact layout was not decoded. **Unverified.**
15. **`info.json` files in shipped content are frequently invalid strict JSON** (trailing commas in `italy`,
    `east_coast_usa`, gridmap_v2's `bigMapProperties`). The engine tolerates this. Any exporter we write must
    either emit strict JSON (safe) or exactly reproduce the game's leniency. **Engine leniency itself is
    verified-local by observation; the exact grammar accepted is unverified.**

---

## Appendix: reproducibility commands

All local evidence in this document was produced with plain CPython 3.13 (`python` on PATH) reading
`.zip` archives in place — **no multi-gigabyte level archive was ever extracted to disk** — plus a byte-offset
string scan of `Bin64\BeamNG.drive.x64.exe`. Nothing in this repository outside
`docs/research/beamng_engine_limits.md` was created or modified.

Sketch of the two probes used, for anyone re-verifying:

```python
import zipfile, struct

# (1) decode a .ter header + exact payload accounting
z = zipfile.ZipFile(r"F:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\italy.zip")
b = z.read("levels/italy/theTerrain.ter")
ver = b[0]
size = struct.unpack("<I", b[1:5])[0]
payload = len(b) - 5 - 166  # 166 = 4 + sum(1+len(name)) over the 15 italy materials
print(ver, size, payload, payload / (size * size))  # -> 9 4096 50331648 3.0

# (2) find an engine limit string and its neighbours
data = open(r"F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe", "rb").read()
i = data.find(b"Height map must be between 128 and 8192 in size!")
print(i, data[i - 160 : i + 120])
```
