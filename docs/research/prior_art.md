# Prior art review — GIS → BeamNG.drive pipelines

**Project:** `ivanovo2beam` — an open-source, reproducible GIS → BeamNG.drive pipeline turning open geodata
(OpenStreetMap + Copernicus DEM) into a drivable 1:1-scale BeamNG map of Ivanovo Oblast, Russia (~220 × 145 km, ≈ 21 400 km²).
**Target:** Python 3.12+, BeamNG.drive 0.39.x. **Intended project licence:** MIT or Apache-2.0.

**Research date:** 2026-10-07. All activity dates are the GitHub `pushed_at` field read from `https://api.github.com/repos/<owner>/<repo>`
unless another source is cited. All licence SPDX identifiers are the `license.spdx_id` field from the same endpoint,
cross-checked against the repository `LICENSE` file where the API returned `NOASSERTION` or `null`.

**Method note.** Two limits applied to this review and are flagged where relevant:

* `curl.exe` and `Invoke-WebRequest` are blocked in the research sandbox; all HTTP was done with the `web_search` / `web_fetch` tools
  and with Python `urllib` against the GitHub REST API and `raw.githubusercontent.com`.
* GitHub's unauthenticated API is rate-limited (60 core requests/hour, 10 search requests/minute). Several bulk searches were
  cut off mid-run by `HTTP 403: rate limit exceeded`. Categories where that happened are marked **possibly incomplete**
  rather than "nothing found".

**Honesty statement.** Categories 5 (terrain/heightmap importers for BeamNG) and 6 (OSM road meshing) each contain exactly one
or two projects that are genuinely reusable; the rest are reference material. Several of the most directly relevant
tools are **unlicensed**, which makes them legally unusable regardless of technical merit. This is stated plainly rather than
papered over. Where a licence or an activity date could not be established from a primary source, the entry says **unverified**
— nothing below is guessed.

---

## Summary

| Project | Category | Licence | Alive | Decision | Why |
|---|---|---|---|---|---|
| [earth2road](https://github.com/Alezx311/earth2road) (formerly `terra-drive`) | GIS→BeamNG | MIT (code) + ODbL 1.0 (OSM data) | Yes — pushed 2026-10-02 | **FORK** | Only permissively-licensed, end-to-end OSM→BeamNG mod-ZIP generator that exists; closest architectural match to us. |
| [blender-ng](https://github.com/mate-h/blender-ng) | BeamNG terrain/road I/O | Unlicense (public domain) | Semi — pushed 2025-08-10 | **FORK** | Unlicense removes all legal friction; contains the best third-party `.ter` parser and EXR round-trip in existence. |
| [cherepokivan/BeamNG.drive-map-generator-v3](https://github.com/cherepokivan/BeamNG.drive-map-generator-v3) ("TerraForge") | GIS→BeamNG | Apache-2.0 | Semi — pushed 2026-03-03 | **FORK** | Apache-2.0, Rust, already does OSM + AWS Terrarium → `heightmap.raw` + mod ZIP + road nodes; same problem statement as ours. |
| [BeamNG_LevelCleanUp](https://github.com/alexkleinwaechter/BeamNG_LevelCleanUp) | GIS→BeamNG | **no licence — all rights reserved** | Yes — pushed 2026-08-02 | **TAKE-REFERENCE-ONLY** | The most capable GeoTIFF+OSM→BeamNG wizard in existence, but nothing may be copied from it. |
| [OSM2World](https://github.com/tordanik/OSM2World) | OSM→3D | MIT | Yes — pushed 2026-10-06 | **TAKE-ALGORITHM** | Mature Java OSM→mesh converter with a proven road/building/tree pipeline and a CC0 style file. |
| [osm2streets](https://github.com/a-b-street/osm2streets) | OSM→road graph | Apache-2.0 | Yes — pushed 2025-10-02 | **TAKE-ALGORITHM** | The lane-level street schema we need for Road Architect profiles; Apache-2.0 permits linking. |
| [osmnx](https://github.com/gboeing/osmnx) | OSM→graph | MIT | Yes — pushed 2026-07-31 | **TAKE-ALGORITHM** | Battle-tested Python OSM graph extraction, projection and simplification. |
| [SUMO / netconvert](https://github.com/eclipse-sumo/sumo) | OSM→lane network | EPL-2.0 | Yes — pushed 2026-10-07 | **TAKE-ALGORITHM** | External CLI that already solves OSM→lane-level network with junction logic. |
| [UE4HmapsGenerator](https://github.com/Rodrigo-NH/UE4HmapsGenerator) | DEM→heightmap | BSD-3-Clause | Dormant — 2021-03-03 | **TAKE-ALGORITHM** | Only public worked example of tiling SRTM into fixed-size engine heightmaps at true scale. |
| [tin-terrain](https://github.com/heremaps/tin-terrain) | DEM→mesh | MIT | Dormant — 2023-03-08 | **TAKE-ALGORITHM** | C++ GeoTIFF→TIN tiler; the right idea for roads-must-be-flat terrain decimation. |
| [GeoTiff2BeamNG](https://github.com/emlodnaor/GeoTiff2BeamNG) | DEM→`.ter` | **no licence — all rights reserved** | Dormant — pushed 2025-08-19 | **IGNORE** | Directly on-topic but unlicensed and tiny; not even safe as a reference to copy from. |
| [BeamNGTerrainGenerator](https://github.com/nikkiluzader/BeamNGTerrainGenerator) | DEM→BeamNG | MIT | Dormant — pushed 2025-04-05 | **TAKE-REFERENCE-ONLY** | MIT, but hardwired to Mapbox imagery + OpenTopography API keys — a licensing trap we must not inherit. |
| [BeamNG.WorldForge](https://github.com/bobberdolle1/BeamNG.WorldForge) | GIS→BeamNG | MIT (`LICENSE`), API reports NOASSERTION | Yes — pushed 2026-07-27 | **IGNORE** | Depends on Google Earth Engine (proprietary, licence-gated) and an LLM; wrong architecture for reproducibility. |
| [beamng-map-import-assistant](https://github.com/jonesyhunting/beamng-map-import-assistant) | OSM→BeamNG | **no licence — all rights reserved** | Yes — pushed 2026-04-15 | **IGNORE** | Right idea (native `MeshRoad` from OSM in-editor) but unlicensed and 0 stars/unproven. |
| [BeamNG Road Architect](https://documentation.beamng.com/world_editor/tools/road_architect/) (built-in) | BeamNG road import | Proprietary (game asset) | Yes — shipped in 0.39.x | **TAKE-REFERENCE-ONLY** | Its JSON session/profile schema is a target format, not code we can ship. |
| [BeamNG.tech Terrain And Road Importer](https://docs.beamng.com/beamng_tech/tools/terrainandroadimporter/) | DEM+roads→BeamNG | Proprietary, **BeamNG.tech only** | Yes — doc updated 2026-08-20 | **TAKE-REFERENCE-ONLY** | Defines the 16-bit PNG + `{node,width}` JSON contract, but is unavailable in consumer `BeamNG.drive`. |
| [Project Virginia](https://www.beamng.com/threads/project-virginia-209-870mi%C2%B2-337-750km%C2%B2-map.106603/) | Large 1:1 map | Proprietary mod | On hold (2025-11) | **TAKE-REFERENCE-ONLY** | Existence proof that a 337 750 km² 1:1 map runs in BeamNG — and a catalogue of its failure modes. |
| [BlenderGIS](https://github.com/domlysz/BlenderGIS) | Blender GIS | **GPL-3.0** | Yes — pushed 2025-12-20 | **TAKE-REFERENCE-ONLY** | Best-in-class, but GPL-3.0 code cannot enter an MIT/Apache-2.0 project. |
| [blosm](https://github.com/vvoovv/blosm) (ex Blender-OSM) | Blender GIS | GPL for source; paid Pro build | Yes — pushed 2026-09-08 | **IGNORE** | Source is GPL and the useful features are behind the paid Pro tier. |
| [osm2xp](https://github.com/32kda/osm2xp) | OSM→scenery | **GPL-3.0** | Yes — pushed 2026-10-07 | **TAKE-REFERENCE-ONLY** | Years of hard-won OSM→flight-sim geometry heuristics; licence blocks reuse. |
| [osm2city](https://wiki.flightgear.org/OSM2City) | OSM→city mesh | GPL-2.0 (**unverified** — not on GitHub at the expected path) | **Unverified** | **TAKE-REFERENCE-ONLY** | The canonical OSM building/city generator; same GPL barrier. |
| [MapGenerator](https://github.com/ProbableTrain/MapGenerator) | Procedural city | **LGPL-3.0** | Dormant — 2024-05-09 | **TAKE-REFERENCE-ONLY** | 2D road-network growth ideas only; copyleft. |
| [arnis](https://github.com/Mukller/arnis) | OSM→world (Minecraft) | MIT | Semi — pushed 2026-08-04 | **TAKE-ALGORITHM** | Small, readable 1:1 OSM rasterisation reference in Rust. |
| [GDAL](https://github.com/OSGeo/gdal) | Raster I/O | MIT/X (API reports NOASSERTION) | Yes — pushed 2026-10-07 | **TAKE-ALGORITHM** | The warp/resample/scale engine; use as a dependency, do not reimplement. |
| [rasterio](https://github.com/rasterio/rasterio) | Raster I/O (Python) | BSD-3-Clause (API reports NOASSERTION) | Yes — pushed 2026-10-02 | **TAKE-ALGORITHM** | Pythonic GDAL binding; the natural dependency for Python 3.12+. |
| [BeamNGpy](https://github.com/BeamNG/BeamNGpy) | BeamNG automation | MIT | Yes — pushed 2026-08-20 | **TAKE-ALGORITHM** | Official Python API — the only credible way to smoke-test a generated level. |
| [OSM2World-default-style](https://github.com/tordanik/OSM2World-default-style) | 3D style data | **CC0-1.0** | Yes — pushed 2026-08-27 | **TAKE-ALGORITHM** | CC0 style mapping is directly shippable, unlike the GPL-adjacent alternatives. |

**Categories with nothing usable found:**

* **Category 5 (heightmap importers for BeamNG)** — no maintained, licensed, scriptable tool exists. Four tools touch the problem
  (`GeoTiff2BeamNG`, `BeamNGTerrainGenerator`, `BeamNG_LevelCleanUp`, `blender-ng`); three are unusable or stale, and only
  `blender-ng` (Unlicense) is safely forkable. See [§5](#5-terrain-generators--heightmap-importers-for-beamng).
* **Category 9 (QGIS → BeamNG)** — no QGIS plugin targets BeamNG. The only QGIS→game-engine exporter found is a GPL-3.0 Unity
  exporter, which is not reusable. See [§9](#9-qgis--beamng-and-dem--beamng-workflows).
* **Category 4 (real-city BeamNG maps at 1:1)** — every real-world BeamNG map found is a closed-source mod. There is no open
  pipeline to fork; there is only evidence and a bug catalogue. See [§4](#4-beamng-large-map-and-real-world-11-map-experiments).

---

## 1. GIS → BeamNG.drive tools

### 1.1 earth2road (formerly terra-drive)

* **URL:** https://github.com/Alezx311/earth2road (the old `terra-drive` name now redirects here; the GitHub API returns `earth2road`
  as `full_name` for both)
* **Description:** Pick a place on a map; it downloads OSM + terrain, builds a drivable world (roads, junctions, markings, sidewalks,
  buildings, signs, traffic lights, traffic), and exports a BeamNG.drive mod ZIP.
* **Licence:** **MIT** — `LICENSE` fetched from `raw.githubusercontent.com` reads "MIT License, Copyright (c) 2026 Earth2Road contributors",
  with an explicit carve-out: *"This license covers the original source code and procedurally generated artwork of this repository only.
  It does NOT cover map data: OpenStreetMap derived data stays under ODbL 1.0."* GitHub API reports `NOASSERTION` because of that extra paragraph.
* **Alive:** Yes. Created 2026-09-28, pushed **2026-10-02**, 12 stars, Python. Public beta.
* **Stack:** Python 3.11+ package `akadem_maps`, CLI `earth2road`; optional Godot 4.6 runtime for the bundled game;
  `pyosmium`, `shapely`, `pyproj`, `Pillow`, `numpy`. BeamNG adapter is a first-class package:
  `akadem_maps/adapters/beamng/{beamng_terrain,beamng_geometry,beamng_corridors,beamng_curbs,beamng_pavement,beamng_placement,export_beamng,optimization}.py`.
* **What we could reuse:**
  * The entire BeamNG export adapter — terrain writing, road corridors, curbs, pavement, prop placement, and an
    "optimization" pass for multi-kilometre maps.
  * Their third-party-notices discipline (`THIRD_PARTY_NOTICES.md`, `docs/SOURCES.md`) is a working model for our own provenance manifest.
  * Their explicit statement that exported ZIPs reference stock BeamNG assets **by path** and contain no BeamNG files
    — the exact redistribution posture we need.
  * `config/osm_types.typ.xml` (OSM→render-type mapping) and `core/road_profiles.py`.
* **Architecture fit for 220 × 145 km:** **Poor as-is, good as a base.** The README states the supported area is 0.3–5 km and warns
  *"Large areas need a lot of RAM and video memory, especially in BeamNG."* Their validated region profile is Ukrainian; Russia is
  "experimental". Nothing in the architecture is intrinsically city-scale, but the optimisation pass is designed for "several kilometres",
  i.e. two orders of magnitude below our target.
* **Decision: FORK.** This is the only end-to-end OSM→BeamNG generator under a permissive licence, and it is the closest thing to our
  problem statement that exists in public. Its BeamNG adapter is exactly the layer we would otherwise spend months writing. The fork is
  justified despite the scale gap because (a) MIT lets us relicense our derivative work, (b) the code is already split along the seams we
  need (core extraction / core geometry / BeamNG adapter / optimisation), and (c) it demonstrates the correct legal posture
  (ODbL data kept under ODbL, code under MIT, no BeamNG assets shipped). We must fix its scale ceiling, not its licence or structure.

### 1.2 BeamNG_LevelCleanUp ("BeamNG Tools for Mapbuilders")

* **URL:** https://github.com/alexkleinwaechter/BeamNG_LevelCleanUp
* **Releases:** https://github.com/alexkleinwaechter/BeamNG_LevelCleanUp/releases/tag/1.5.1
* **Forum threads:** https://www.beamng.com/threads/building-real-life-maps-in-beamng-tool-is-finished.107781/ and
  https://www.beamng.com/threads/beamng-mapping-tools-updated-2026-08-01.89058/
* **Description:** A .NET map-builder toolkit. Beyond cleaning/renaming/shrinking levels, version 1.5.1 added a **Create Level Wizard**,
  **"Generate Terrain from GeoTiff with OSM Data"** and per-material elevation smoothing.
* **Licence:** **no licence — all rights reserved.** `LICENSE` at the repository root returns **HTTP 404**, and the GitHub API returns
  `license: null`. No licence file was found. Treat the code, and anything derived from reading it closely, as off-limits.
* **Alive:** Yes. Created 2022-05-24, pushed **2026-08-02**, 54 stars, C#.
* **Stack:** C# / .NET desktop, GeoTIFF ingestion, OSM ingestion, BeamNG level writing.
* **What we could reuse:** **Nothing copyable.** Value is purely informational:
  * Confirms that GeoTIFF → BeamNG level generation with OSM roads is a solved *workflow* in 2026, which de-risks our plan.
  * The author's own published limitation is a design lesson: *"Smoothing out junctions from roads of different materials is horrible
    because it is a kind of post processing step. I guess I have to rethink this idea."* — i.e. smooth **before** splitting by material,
    not after. This directly informs our pipeline ordering.
  * The project's existence tells us the market expects a GUI wizard; our reproducible-CLI approach is a differentiator, not a gap.
* **Architecture fit:** Unverified — the tool is a manual GUI wizard with no documented batch/CLI mode, so it does not fit a reproducible
  220 × 145 km pipeline even ignoring the licence.
* **Decision: TAKE-REFERENCE-ONLY.** It is the strongest existing GeoTIFF+OSM→BeamNG tool and therefore the most tempting to read for
  ideas, but with no licence file at all there is no permission to copy, adapt, or redistribute any part of it. We should read the
  README, the forum thread and the release notes for *behavioural* requirements only, and independently design the smoothing order
  (smooth globally, then attribute materials) that its author identified as necessary.

### 1.3 BeamNG.drive-map-generator-v3 ("TerraForge")

* **URL:** https://github.com/cherepokivan/BeamNG.drive-map-generator-v3 (predecessor: https://github.com/cherepokivan/BeamNG-Map-Generator)
* **Description:** Select an area on a Leaflet map → generate a BeamNG mod from OSM (forms, roads, objects) plus AWS Terrarium
  heightmaps, auto-generate textures, add road nodes, export a `.zip` to `Downloads`.
* **Licence:** **Apache-2.0** (GitHub API `license.spdx_id = "Apache-2.0"`, confirmed on both v3 and the older repo).
* **Alive:** Semi. Created 2026-02-16, pushed **2026-03-03**, 1 star, Rust. Low maturity but recent.
* **Stack:** Rust backend (`axum` + `tokio`), TypeScript/React/Leaflet UI built with Vite, GitHub Actions producing a portable `.exe`.
* **What we could reuse:**
  * `src/aws_terrain.rs` — decodes **AWS Terrarium PNG tiles** (`elevation-tiles-prod`) to metres and normalises them into `heightmap.raw`.
    This is precisely the DEM→heightmap step we need, under Apache-2.0.
  * Its Overpass multi-endpoint fallback strategy (several mirrors, retries) — directly reusable resilience logic.
  * Its local `.osm` file import path (`POST /api/generate-from-osm`) — matches our "pin an extract, build offline" reproducibility requirement.
  * Its generated output set (`main.materials.json` with terrain and building materials, `heightmap.raw`, road nodes) is a concrete
    functional spec for a minimal BeamNG level.
* **Architecture fit:** Same class of tool as earth2road, same class of limitation — a browser-driven single-area generator with no
  evidence of >10 km scale, no tiling, no streaming, and 1 star of adoption.
* **Decision: FORK (secondary) / TAKE-ALGORITHM.** Apache-2.0 is as clean as MIT for our purposes, and Rust is a fine stack, but the
  project is a month-old 1-star prototype — forking it as a *foundation* would mean inheriting unproven code. The right move is to treat
  `aws_terrain.rs` and the Overpass-fallback design as the reference implementation for one well-defined module (DEM tile acquisition →
  heightmap), written in Python in our own pipeline, with attribution in `NOTICE`.

### 1.4 BeamNGTerrainGenerator

* **URL:** https://github.com/nikkiluzader/BeamNGTerrainGenerator
* **Description:** C# desktop app generating BeamNG-compatible terrain levels from real-world map data, integrating online elevation
  and imagery sources.
* **Licence:** **MIT** (GitHub API `license.spdx_id = "MIT"`).
* **Alive:** Dormant. Created 2025-03-27, pushed **2025-04-05** (a nine-day project), 13 stars, C#.
* **Stack:** .NET 8, Visual Studio 2022; NuGet `GDAL`/`GDAL.Native`, `ProjNet`, `SixLabors.ImageSharp`, `Newtonsoft.Json`,
  `HelixToolkit.Core`, `Microsoft.Web.WebView2`; embedded **Mapbox** web map.
* **What we could reuse:** The README documents a concrete, correct-looking GDAL-based stack (GDAL for DEM reprojection and GeoTIFF
  parsing, ProjNet for CRS transforms, ImageSharp for image export) and a HelixToolkit 3D preview. That stack list is a useful sanity
  check on our own dependency choices.
* **Architecture fit:** **Fails on licensing, not just scale.** The README requires a **Mapbox access token for satellite imagery** and an
  **OpenTopography API key**. Mapbox imagery is proprietary and cannot be redistributed in an open map; OpenTopography is fine but key-gated,
  which breaks unattended reproducibility. The MIT licence covers the code but cannot cure the imagery.
* **Decision: TAKE-REFERENCE-ONLY.** MIT would permit a fork, but there is very little code worth forking (nine days of work, dormant for
  18 months) and the architecture bakes in a proprietary imagery dependency we must avoid. Read it for the GDAL/ProjNet/ImageSharp
  stack confirmation; do not adopt its data sources and do not build on the codebase.

### 1.5 BeamNG.WorldForge

* **URL:** https://github.com/bobberdolle1/BeamNG.WorldForge
* **Description:** "Auto-generate realistic BeamNG.drive maps from real satellite data. Uses AI (Ollama/Qwen) for terrain, roads, buildings."
* **Licence:** **MIT.** GitHub API reports `NOASSERTION`, but the `LICENSE` file fetched from `raw.githubusercontent.com/bobberdolle1/BeamNG.WorldForge/main/LICENSE`
  is verbatim MIT ("Copyright (c) 2025 BeamNG.WorldForge Team"). Its own third-party list flags a problem:
  *"Google Earth Engine (proprietary, requires license)"* and *"React Leaflet (Hippocratic License)"*.
* **Alive:** Yes. Created 2025-10-21, pushed **2026-07-27**, 5 stars, Python.
* **Stack:** FastAPI + NumPy + Pillow + OpenCV + GDAL + Rasterio backend; React + TypeScript + Vite + Three.js + React-Leaflet frontend; Ollama/Qwen LLM.
* **What we could reuse:** Very little. The `Rasterio` + `GDAL` + `Pillow` backend confirms the standard Python raster stack.
* **Architecture fit:** **No.** Adding a local LLM to a determinism-critical geometry pipeline is the opposite of reproducible. Google Earth
  Engine is a proprietary, licence-gated platform whose terms are not compatible with shipping derived terrain in an open map.
* **Decision: IGNORE.** Dependencies (proprietary GEE, an LLM in the geometry path) are disqualifying for a reproducible open pipeline, and
  the permissive licence does not rescue the data-provenance problem. Note it in the field survey and move on.

### 1.6 beamng-map-import-assistant

* **URL:** https://github.com/jonesyhunting/beamng-map-import-assistant
* **Description:** A BeamNG.drive **World Editor extension mod** that brings real-world map data in as native BeamNG `MeshRoad` objects,
  with MapNG GeoJSON first and raw OSM XML fallback, plus optional roadside placement of poles, signs, signals and barriers.
* **Licence:** **no licence — all rights reserved.** GitHub API returns `license: null`; no licence is referenced in the README.
* **Alive:** Yes. Created 2026-03-08, pushed **2026-04-15**, 0 stars, Lua.
* **Stack:** Lua (BeamNG World Editor extension), PowerShell installer that junctions the repo into `mods/unpacked`.
* **What we could reuse:** **Ideas only, and even those cautiously.** Two are worth noting:
  * It chose **`MeshRoad`** rather than `DecalRoad` for "driveable road output" — a concrete, testable claim about which primitive
    gives real collision, which our own design must settle.
  * Its install pattern (junction the repo into `mods/unpacked` for live editing) is a useful local-development technique.
* **Architecture fit:** Unverified. It is explicitly *"intentionally not a one-click full world generator"* and is designed for
  preview→filter→commit on small areas inside the editor. It is a manual in-editor tool, not a batch generator.
* **Decision: IGNORE.** Zero stars, three weeks of activity, no licence, and an explicitly interactive rather than scriptable workflow.
  The `MeshRoad`-vs-`DecalRoad` question is worth carrying forward as a question, not as borrowed code.

### 1.7 Import-OSM-custom — analysis BeamNG and Hexagon

* **URL:** https://github.com/gianlucafabris/Import-OSM-custom---analysis-BeamNG-and-Hexagon
* **Description:** OSM import analysis for BeamNG and Hexagon (Hexagon being a proprietary Hexagon AB geospatial product).
* **Licence:** **CC-BY-4.0** (GitHub API `license.spdx_id = "CC-BY-4.0"`). Note: a content licence applied to a code repository is unusual
  and its applicability to the source files is **unverified**.
* **Alive:** Yes — pushed **2026-07-28**, 2 stars, Python.
* **Stack:** Python.
* **What we could reuse:** Unverified — the repository was not read beyond its API metadata, and a CC-BY-4.0 code repository needs its
  per-file licensing checked before anything is copied.
* **Architecture fit:** Unverified.
* **Decision: IGNORE.** Two stars, an ambiguous content-licence-on-code situation, and a scope that mixes in a proprietary Hexagon product.
  Not worth the verification cost given stronger candidates exist.

### 1.8 Automated Generation of Driving Simulation Scenarios using OSM and BeamNG (Master's thesis prototype)

* **URL:** https://github.com/NiharikaPatil/Automated_Generation_of_Driving_Simulation_Scenarios_using_OSM_and_BeamNG-MasterArbeit
* **Description:** BeamNG.tech prototype that imports OpenStreetMap road networks around Hochschule Neu-Ulm into a driving simulation.
* **Licence:** **no licence — all rights reserved** (GitHub API `license: null`).
* **Alive:** Pushed **2026-03-11**, 0 stars, Python. Academic artefact; no evidence of maintenance.
* **Stack:** Python (BeamNG.tech + OSM).
* **What we could reuse:** Ideas only — it demonstrates that pairing OSM road geometry with **BeamNG.tech** (rather than consumer
  BeamNG.drive) is an accepted research pattern. Relevant because BeamNG.tech is where OpenDRIVE import lives (see §1.10).
* **Architecture fit:** No — a campus-scale scenario generator, not a region-scale map builder.
* **Decision: TAKE-REFERENCE-ONLY.** Zero stars, no licence, thesis-scoped. Read the approach for the BeamNG.tech angle; copy nothing.

### 1.9 BeamNGpy

* **URL:** https://github.com/BeamNG/BeamNGpy
* **Description:** Official Python API for controlling BeamNG.tech/BeamNG.drive programmatically.
* **Licence:** **MIT** (GitHub API `license.spdx_id = "MIT"`).
* **Alive:** Yes — pushed **2026-08-20**, 389 stars, Python.
* **Stack:** Python, network client to the game's Lua API.
* **What we could reuse:** **As a dependency, not as code to fork.** It is the only credible route to an automated acceptance test:
  load the generated level, spawn a vehicle, drive a fixed scripted path, assert the vehicle did not fall through the terrain.
  That converts "we think the map is valid" into CI evidence.
* **Architecture fit:** Not a map builder, so "fit" is not the right frame. It scales with whatever the game can load.
* **Decision: TAKE-ALGORITHM (use as a test-harness dependency).** MIT, official, maintained. Adopt it for the verification layer of
  `ivanovo2beam`, and keep it an *optional* extra so that the core pipeline has no game dependency.

### 1.10 BeamNG Road Architect (built-in World Editor tool)

* **URL (tool docs):** https://documentation.beamng.com/world_editor/tools/road_architect/
  — see also [Introduction](https://documentation.beamng.com/world_editor/tools/road_architect/introduction/),
  [Disk Options](https://documentation.beamng.com/world_editor/tools/road_architect/disk_options/),
  [Profile Editing](https://documentation.beamng.com/world_editor/tools/road_architect/profile_editing/)
* **Description:** BeamNG's built-in procedural road-network tool: roads, nodes, bridges, overlays, lateral **profiles**,
  groups and junctions, with an Edit Mode and a Render Mode that generates decals, meshes, a collision mesh and a navigation graph.
* **Licence:** **Proprietary** — part of the BeamNG.drive distribution. Covered by the
  [BeamNG EULA](https://beamng.com/game/support/policies/eula/). We may not ship any of it.
* **Alive:** Yes, shipped and current in 0.39.x (docs pages for the tool tree are live; patch notes reference 0.39.1.0).
* **Stack:** The World Editor is written in **Lua** with a small C++ core exposed as Lua tables; it lives in
  `/lua/ge/extensions/editor`, and tools are extensions under `/lua/ge/extensions/editor/extensions`
  (https://documentation.beamng.com/world_editor/api_reference/).
* **Answering the specific question — what consumes the `roadArchitect` JSON profiles, and is it scriptable?**
  * **What consumes them:** the Road Architect tool itself. Per the Disk Options page, a saved session is **two files** —
    a **user-readable `.json`** holding the road network, and a **`.png`** holding terrain state, which must be
    *"monochrome (1-channel), 16-bit, uncompressed .png"*. Separately, **template profiles, groups and junctions** each have
    dedicated save buttons and are *"all saved in the `.json` file format."* So the `.json` profiles are the tool's own
    profile/template serialisation, loaded through its Profiles List / Groups List / Junctions List.
  * **Is it scriptable?** Partly, and not through a documented Road Architect API. Two things are confirmed:
    (i) the World Editor is Lua and explicitly extensible — the docs describe creating editor tool extensions from a `test.lua`
    template, custom object-create buttons, custom user-window menu items and an extension event-hook system
    (https://documentation.beamng.com/world_editor/api_reference/); and (ii) the tool can **import and export ASAM OpenDRIVE
    (`.xodr`)** — but *"In the BeamNG.tech version"* only. **Unverified:** no public documentation was found for a
    `roadArchitect` *installation folder* or for a machine-readable schema of the profile JSON, so the on-disk folder layout and the
    profile schema must be derived empirically from an installed game. Likewise, whether the Road Architect's own internal Lua API
    is callable from a custom editor extension is **unverified**.
  * **Documented OpenDRIVE limitations** (from Disk Options) are directly relevant to a generated network: unsupported features include
    elevation shape definitions, elevation strips, excluding lanes from the lateral profile, lane linkage (predecessor/successor),
    lane borders, road markings, **most junction types**, objects and signals.
* **What we could reuse:** A **target format** and a design vocabulary. The session JSON + 16-bit PNG pair is a plausible intermediate
  representation, and the tool's road *classes* (Urban / Highway / Dirt) with their compatibility rules (roads connect within a class;
  cross-class connections need explicit transition blocks) are a ready-made classification scheme that our OSM→profile mapping should adopt.
* **Architecture fit:** **Unknown and risky at our scale.** Interaction is manual, junction finalisation is one-way, and the author of the
  largest known map using it called it *"an extremely annoying tool at times."* Nothing suggests it can ingest a 220 × 145 km network.
* **Decision: TAKE-REFERENCE-ONLY.** The schema and the road-class taxonomy are free to learn from (they are documented, not code we copy);
  driving the tool itself from a script is not a documented capability and its OpenDRIVE path requires BeamNG.tech. Our pipeline should
  therefore target the **level file formats directly** (`DecalRoad` / `MeshRoad` in `items.level.json`, plus `.ter` + `.terrain.json`),
  and treat Road Architect JSON as an optional hand-off for human polish.

### 1.11 BeamNG.tech "Terrain And Road Importer"

* **URL:** https://docs.beamng.com/beamng_tech/tools/terrainandroadimporter/
* **Description:** A World Editor tool that imports a terrain heightmap, imports roads, then **terraforms the terrain so roads lie flat**.
  Roads are rendered as Decal Roads.
* **Licence:** **Proprietary**, and materially it is a **BeamNG.tech** feature — the documentation lives under the BeamNG.tech section,
  not the BeamNG.drive modding section.
* **Alive:** Yes — page last modified **2026-08-20**.
* **Stack:** In-game Lua editor tool.
* **What we could reuse — the input contract, which is the clearest format spec in the whole ecosystem:**
  * **Terrain:** greyscale **16-bit `.png`, uncompressed**, one pixel = 1 m × 1 m, values `[0, 65535]` mapping linearly to `[0, zMax]`
    where `zMax` is a user-set "prominence" slider.
  * **Roads:** a *single* `.json` file; top level indexes each road; each road is an ordered list of nodes; each node is
    `[x, y, z, width]`. **The z-value is ignored** (elevation comes from the terrain) but must be present.
  * **Terraforming model:** two sliders — **Margin** (flat top around the road) and **DOI / domain of influence** (how far out the
    terrain may be modified, and therefore how steep the resulting embankment is).
  * **Recommended host level:** "Tech Ground", 2048 × 2048 at 1 m/px, and a supplied PNG *overwrites* only the covered region, leaving
    the far edges untouched.
  * The page itself suggests *"the user may wish to retrieve roads data from a source such as OpenStreetMap"* — i.e. our exact use case
    is anticipated by the vendor, but only as a manual workflow.
* **Architecture fit:** **Fails on two counts.** It is BeamNG.tech-only, and the recommended 2048 × 2048 @ 1 m surface is a 2 × 2 km box —
  0.02% of our area.
* **Decision: TAKE-REFERENCE-ONLY.** We cannot ship or require it, but the 16-bit PNG + `{node, width}` JSON contract and the
  Margin/DOI terraforming model are the single most useful published specification for the flat-road problem. Our generator should be
  able to *emit* exactly this JSON so that a BeamNG.tech user can round-trip our output, and our own terraforming implementation should
  reproduce the Margin/DOI semantics so results are comparable.

---

## 2. OSM → BeamNG.drive, OSM → Torque3D, GIS → Torque3D

**Result: no OSM → Torque3D or GIS → Torque3D tool was found.** That is a genuine negative finding, and it matters, because BeamNG's
engine lineage is Torque3D-derived and its terrain file (`.ter`) is a Torque-style heightmap container. A GitHub repository search for
`torque3d terrain` returned only three results, all effectively dead:

| Repo | Licence | Activity | Note |
|---|---|---|---|
| [emlodnaor/GeoTiff2BeamNG](https://github.com/emlodnaor/GeoTiff2BeamNG) | **no licence** (API `null`) | pushed 2025-08-19, 2 stars | Described as *"Generates BeamNG (Torque3d maybe) Terrain file from GeoTiff files."* — the "maybe" is the author's own uncertainty. |
| [Torque3D-Games-Demos/Torque3D_Terrains](https://github.com/Torque3D-Games-Demos/Torque3D_Terrains) | NOASSERTION | dormant since 2018-01-24, 1 star | Terrain assets, not a pipeline. |
| [ChrisCalef/terrainPager_game](https://github.com/ChrisCalef/terrainPager_game) | none | dormant since 2015-03-04, 0 stars | A game directory for a Torque3D `terrainPager` branch. |

The one substantive lesson is in the third row: Torque3D had a **terrain pager** concept. That is a hint about how large worlds were
contemplated upstream, but the 2015 repository is not usable as a source and the mechanism is not documented publicly for BeamNG.
See [§11 "What we must build ourselves"](#what-we-must-build-ourselves) for why paging is on our critical path.

For OSM → BeamNG.drive specifically, the only candidates are those already covered in §1 (`earth2road`, `BeamNGTerrainGenerator`,
`cherepokivan/…-v3`, `beamng-map-import-assistant`) plus the two research artefacts
([gianlucafabris](https://github.com/gianlucafabris/Import-OSM-custom---analysis-BeamNG-and-Hexagon),
[NiharikaPatil](https://github.com/NiharikaPatil/Automated_Generation_of_Driving_Simulation_Scenarios_using_OSM_and_BeamNG-MasterArbeit)).
A GitHub repository search for `beamng osm` returned **2 results total**. The category is essentially unexploited.

**Decision for the category: IGNORE the Torque3D branch; rely on the official `.ter` format documentation instead**
(https://documentation.beamng.com/modding/levels/level_formats/terrain/), which is authoritative and current, rather than reverse-engineering
a dead engine fork.

---

## 3. BeamNG road import tools / spline road generators

Beyond the built-in Road Architect and the BeamNG.tech importer, the ecosystem contains:

### 3.1 mate-h/blender-ng — BeamNG.drive Blender importer/exporter

* **URL:** https://github.com/mate-h/blender-ng
* **Description:** A Blender addon for importing and exporting BeamNG.drive level data, with full round-trip terrain workflows,
  16-bit EXR displacement layers and coordinate-system correction.
* **Licence:** **Unlicense** (GitHub API `license.spdx_id = "Unlicense"`) — a public-domain dedication. This is the most permissive
  licence encountered anywhere in this review; there is essentially no legal friction.
* **Alive:** Semi. Created 2025-06-07, pushed **2025-08-10**, 6 stars, Python. Dormant for ~14 months but self-consistent and complete-looking.
* **Stack:** Python Blender addon (Blender 4.0+, tested 4.3), NumPy, geometry nodes, OpenImageIO tooling.
* **What we could reuse — this is unusually rich:**
  * `BeamNG_TER_Format_Documentation.md` and `terrain_analysis/ter_parser.py` — a third-party, empirically-derived parser for the `.ter`
    binary format **with a stated byte offset (`0x05`) and endianness**. This is an independent cross-check on the official format page,
    which is exactly what a format implementation needs.
  * `terrain_analysis/{exr_to_ter.py, npy_to_exr.py, create_terrain_textures.py}` — a working heightmap ↔ `.ter` conversion path.
  * `beamng_blender_addon/parsers/decal_road_parser.py` and `utils/decal_road.py` — a `DecalRoad` reader, i.e. a working interpretation of the
    road JSON that our *writer* must produce.
  * `BeamNG_Road_Analysis.md` — a written analysis of the layered road model (**base terrain mesh with roads carved out**, terrain base PBR
    textures, `DecalRoad` objects with **no collision**, Road Architect for authoring) and a documented `DecalRoad` field list
    (`nodes` as `[x, y, z, width]`, `improvedSpline`, `breakAngle`, `textureLength`, `startEndFade`, `distanceFade`, `renderPriority`,
    `overObjects`, `material`), plus the discovery that roads live at
    `levels/{level}/main/MissionGroup/roads/items.level.json`.
  * `test/{hermite_g1_curve.py, clothoid_hermite_test.py, resample_curve.py, spline_analysis.py}` — road-spline mathematics.
    Road geometry from OSM nodes is fundamentally a spline-fitting problem, and this is a head start.
  * Coordinate-system handling: BeamNG uses a bottom-left origin with Y increasing south→north; Blender is top-left with Y downward;
    the addon applies a single Y-flip. Getting this wrong is the classic first bug in any BeamNG level generator.
* **Architecture fit:** Neutral. It is an authoring addon, not a generator; it has no notion of a region. But its parsers and spline maths
  are scale-independent, so they fit a 220 × 145 km pipeline as well as a 2 km one.
* **Decision: FORK.** The Unlicense makes this a zero-risk source, and it contains precisely the low-level format and spline knowledge
  that is expensive to re-derive and for which no official documentation exists at this level of detail (the `.ter` byte offsets, the
  `DecalRoad` field semantics, the coordinate-convention trap). We should fork or vendor the parsers and the spline utilities into our
  pipeline, and keep the Blender GUI functionality as an optional downstream polishing step rather than a core dependency. **Caveat:**
  its `beamng_map_mod/` sub-project and `map-browser/` Svelte app additionally depend on **OpenTopography** (`download-opentopography.ts`),
  which needs an API key — keep that out of the reproducible core.

### 3.2 `DecalRoad` / `MeshRoad` as the real import target

From the docs tree, BeamNG exposes several spline-based road/mesh primitives relevant to import:
[Decal Road Editor](https://documentation.beamng.com/world_editor/tools/decalroad_editor/),
[Meshroad Editor](https://documentation.beamng.com/world_editor/tools/meshroad_editor/),
[Road Spline Tool](https://documentation.beamng.com/world_editor/tools/road_spline_tool/),
[Mesh Spline Tool](https://documentation.beamng.com/world_editor/tools/mesh_spline_tool/),
[Master Spline Tool](https://documentation.beamng.com/world_editor/tools/master_spline_tool/), and
[Building Architect](https://documentation.beamng.com/world_editor/tools/building_architect/).
Level object files are documented at
[Level Object Files (items.level.json)](https://documentation.beamng.com/modding/levels/level_formats/items/) and the classes at
[Level Object Classes](https://documentation.beamng.com/modding/levels/level_classes/).

The important open question — raised independently by `beamng-map-import-assistant` (which chose `MeshRoad`) and by `blender-ng`
(which notes `DecalRoad` has **no collision**) — is **which primitive gives drivable collision**. `DecalRoad` is a visual decal on the
terrain; collision appears to come from the terrain mesh itself plus terraforming. **Unverified:** no single document was found that states
definitively which primitive a *generated* road network should use to be drivable without manual terraforming. This must be settled
empirically against 0.39.x and is listed as a build gap.

### 3.3 Master Spline load/save mod

The `BeamNG_LevelCleanUp` author also published an experimental mod for **loading and saving master splines**:
https://www.beamng.com/threads/experimental-load-and-save-mastersplines.107762/ (referenced from the thread in §1.2).
This is relevant because it shows the community working around the absence of a scripted import path for spline data.
**Licence: unverified** (a forum-distributed mod; no licence statement found).

**Decision for the category: TAKE-REFERENCE-ONLY**, with `blender-ng`'s parsers as the sole code-level exception. The category contains
no scriptable, licensed, region-scale road importer. The road-import problem is ours to solve.

---

## 4. BeamNG large-map and real-world 1:1 map experiments

There is **no open-source real-city BeamNG map pipeline**. What exists is a set of closed-source mods that establish the envelope of
what is possible. This category is therefore evidence, not code.

### 4.1 Project Virginia — 337 750 km² at 1:1

* **URL:** https://www.beamng.com/threads/project-virginia-209-870mi%C2%B2-337-750km%C2%B2-map.106603/
* **Description:** *"a 209,870 mi² / 337,750 km² mapping project … it is a 1:1 scale (road distance wise)"* — a recreation of Virginia, USA.
* **Licence:** **Proprietary mod**, distributed through the BeamNG forum/repository. No source, no licence to reuse.
* **Alive:** **On hold** — *"project on hold due to personal issues"*, thread last meaningfully updated 2025-11-05.
* **Stack (as reported by the author):** heightmap-based terrain, roads built with the built-in **Road Architect**, signs modelled in Blender
  and textured in paint.net, Lua triggers for gates and dynamic message signs. Heightmap credited to forum user `OwnTheBanks`.
* **What we could reuse — the lessons are worth more than the assets:**
  * **Scale is not hard-capped by the engine.** The author's answer: *"To my knowledge, beamng itself does not limit the size of maps
    (on purpose at least), you could literally make a map the size of the universe most likely but the devs do not support maps huge like
    this… the game engine is limited with huge sizes and many bugs do show once you get bigger."* This is the single most important
    precedent for us: **our 21 400 km² target is ~16× smaller than a map that actually shipped in some form.**
  * **Floating-point error is the real enemy at these extents** — the author explicitly warns of *"visual bugs and floating point errors"*.
  * **Quality collapses with area.** The author's own disclaimer: *"Due to the size of this map … the quality will be pretty low in
    comparison to most other maps."* Our project must decide consciously whether it is optimising for fidelity or extent.
  * **Road Architect does not scale operationally.** *"did you use the Road Architect tool to make the roads? — Yes, but its an extremely
    annoying tool at times."* Building a whole oblast by hand in Road Architect is not a plan.
  * The feature list (traffic that respects speed limits, working traffic signals, gas stations, licence plates, dynamic lights, reflectors)
    is a de-facto requirements document for what makes a large real-world map feel finished.
* **Architecture fit:** Directly comparable — larger than ours in area, 1:1 in road distance, and it ran. The differences are that it is
  hand-authored rather than generated, and incomplete.
* **Decision: TAKE-REFERENCE-ONLY.** Nothing is licensable. But it is the strongest available evidence that a 1:1-scale BeamNG map of a
  region is achievable, and its documented failure modes (float precision, quality-vs-area trade-off, manual tooling) should be adopted as
  our own risk register.

### 4.2 Other large / real-world BeamNG maps observed

| Map | URL | Licence | Note |
|---|---|---|---|
| LOS INJURUS CITY mega-map | https://steamcommunity.com/app/284160/discussions/0/6980058383076359857 | Proprietary mod | Cited as a large-map existence proof only; not inspected in depth. |
| Isezaki Ijincho, Yokohama, Japan | https://www.beamng.com/threads/isezaki-ijincho-yokohama-japan-yakuza-like-a-dragon.93719/ | Proprietary mod | A real-city recreation from a commercial game's fiction; relevance is limited to "real-city maps are made". |
| BeamNG Mapping Tools (AlexKidd71) | https://www.beamng.com/threads/beamng-mapping-tools-updated-2026-08-01.89058/ | See §1.2 (no licence) | The active community hub for map-builder tooling. |
| "How to make HUGE terrain area with square size 1?" | https://www.beamng.com/threads/how-to-make-huge-terrain-area-with-square-size-1.90627/ | Forum | Community grappling with exactly our terrain-resolution constraint. Not read in full. |
| "How to make the map bigger?" | https://www.beamng.com/threads/how-to-make-the-map-bigger.84837/ | Forum | Same. Not read in full. |
| "Imported Heightmap not importing properly" | https://www.beamng.com/threads/imported-heightmap-not-importing-properly.106820/ | Forum | Failure modes of heightmap import. Not read in full. |
| "Transfering L3DT maps to BeamNG" | https://beamng.com/threads/transfering-l3dt-maps-to-beamng.2773/ | Forum | L3DT, a commercial terrain tool, as an upstream. Not read in full. |

**Decision for the category: TAKE-REFERENCE-ONLY across the board.** No open-source real-world BeamNG map pipeline exists to fork. This is
not a disappointment — it is the core justification for `ivanovo2beam`.

### 4.3 The hard constraint we discovered in the official format documentation

**This is the single most consequential technical finding of this review.**

From https://documentation.beamng.com/modding/levels/level_formats/terrain/:

* Terrain is a **square grid**: `.ter` stores `size × size`.
* **"Heightmap must be square."**
* **"Heightmap resolution must be power of two."**
* **"Heightmap size must be between `128` and `8192`."**
* World size = `size × squareSize`; heights are `u16` scaled by `maxHeight / 65536`.
* **"Terrain object scaling is disabled. Use `squareSize` and `maxHeight` to control terrain dimensions instead of object scale."**
* The layer map is `u8`, `255` = empty/hole, max **254** terrain materials.
* At `2048 × 2048` a `.ter` is ~12 MiB; the file grows as `size²`.

Consequence for a 220 × 145 km region:

* A single terrain square of **8192 × 8192** samples covering the 220 km width needs `squareSize ≈ 26.9 m`. At that resolution a
  motorway (≈ 20–30 m wide) is **about one sample wide** and lane markings are sub-sample. The map would be undrivable in any meaningful sense.
* Reducing `squareSize` to a useful value (say 2 m) at 8192 samples covers only **16.4 km** — you would need roughly **13 × 9 = 117 terrain
  tiles** to cover the oblast, and 1 m/px would need ~450 tiles.
* **Unverified:** whether BeamNG.drive supports multiple `TerrainBlock` objects in one level with correct collision, seams, LOD and
  streaming. No documentation found either way. The 8 MiB heightmap + 4 MiB layer map per 2048² tile also means memory pressure is real:
  117 tiles at 2048² is ≈ 1.4 GiB of raw heightmap alone.

Also note the **non-square** problem: our region's 220 : 145 aspect ratio (≈ 1.52 : 1) cannot be represented by a square terrain without
padding, so any tiling scheme wastes area or must clip.

**Decision: this is a build gap, not a fork target** — see [§11](#what-we-must-build-ourselves).

---

## 5. Terrain generators / heightmap importers for BeamNG

**Honest assessment: no maintained, licensed, scriptable heightmap importer for BeamNG exists.** Every candidate is either unlicensed,
dormant, GUI-only, or already covered above. Details:

### 5.1 GeoTiff2BeamNG

* **URL:** https://github.com/emlodnaor/GeoTiff2BeamNG
* **Description:** *"Generates BeamNG (Torque3d maybe) Terrain file from GeoTiff files."*
* **Licence:** **no licence — all rights reserved.** GitHub API returns `license: null`; fetching `README.md` from `main` returned
  **HTTP 404**, so the default branch differs, but no licence file was located.
* **Alive:** Dormant. Created 2023-09-04, pushed **2025-08-19**, 2 stars, C#.
* **Stack:** C#.
* **What we could reuse:** Nothing copyable. The *description* alone confirms GeoTIFF → `.ter` is tractable in a small program.
* **Architecture fit:** Unverified — no documentation, no README, 2 stars, 2 years of near-inactivity.
* **Decision: IGNORE.** Unlicensed *and* undocumented. Even as a reference it offers less than the official format documentation and
  `blender-ng`'s parser, both of which we may lawfully use.

### 5.2 KingStu90/LiDAR-to-Heightmap-Beginner-Tutorial

* **URL:** https://github.com/KingStu90/LiDAR-to-Heightmap-Beginner-Tutorial
* **Description:** A beginner-friendly, reproducible workflow for turning LiDAR data into heightmaps, basemaps and related outputs.
* **Licence:** **MIT** (GitHub API `license.spdx_id = "MIT"`).
* **Alive:** Yes — pushed **2026-09-26**, 0 stars, Shell.
* **Stack:** Shell scripts (likely GDAL-based).
* **What we could reuse:** A reproducibility pattern — scripted, documented, re-runnable acquisition/processing. Possibly GDAL invocations.
* **Architecture fit:** Not applicable (a tutorial, no engine target). Also note **LiDAR coverage of Ivanovo Oblast is not established** —
  **unverified** — so this is unlikely to be directly applicable data-wise.
* **Decision: TAKE-REFERENCE-ONLY.** MIT and current, so reading it is safe, but it is a tutorial with no BeamNG target and no relevant
  data coverage. Worth a look for the GDAL command lines, nothing more.

### 5.3 The `.ter` writing problem, and why the ecosystem is thin

The reason this category is empty is structural: `.ter` is a proprietary binary format owned by BeamNG. Until the official
[Terrain Files (.ter and .terrain.json)](https://documentation.beamng.com/modding/levels/level_formats/terrain/) page existed, every tool
had to reverse-engineer it. That history explains the shape of the ecosystem:

* **Unofficial, unlicensed reverse-engineered tools** (`GeoTiff2BeamNG`, and the unlicensed parts of `BeamNG_LevelCleanUp`).
* **One Unlicense reverse-engineered tool** (`blender-ng`) — the only one we can lawfully build on.
* **Official documentation** now specifying the format precisely (version 9, little-endian, `u8 version`, `u32 size`,
  `u16[size²] heightMap`, `u8[size²] layerMap`, `u32 materialCount`, then material name strings).

**Decision for the category: TAKE-REFERENCE-ONLY for the tools; implement the writer ourselves from the official format page plus
`blender-ng`'s parser as a cross-check.** This is a small, well-specified piece of work and there is no reason to inherit anyone's
reverse-engineering debt. Related official pages we should implement against:
[Level Creation](https://docs.beamng.com/modding/levels/level_creation/section2/) (terrain, heightmaps, materials, water),
[TerrainBlock](https://docs.beamng.com/modding/levels/level_classes/terrainblock/),
[Terrain Editor](https://documentation.beamng.com/world_editor/tools/terrain_editor/),
[Groundmodels](https://documentation.beamng.com/modding/levels/level_formats/groundmodels/).

---

## 6. OpenStreetMap road-meshing and street-network projects

### 6.1 OSM2World

* **URL:** https://github.com/tordanik/OSM2World
* **Description:** *"converter that creates three-dimensional models of the world from OpenStreetMap data."*
* **Licence:** **MIT** (GitHub API `license.spdx_id = "MIT"`).
* **Alive:** **Yes — pushed 2026-10-06** (one day before this review), 783 stars, Java.
* **Stack:** Java; outputs OBJ, glTF, POV-Ray and more; separate CC0 style repo; a WebGL demo frontend (MIT-0).
* **What we could reuse:**
  * The **OSM→geometry interpretation rules**: which tags imply which surfaces, how `lanes`, `width`, `sidewalk`, `bridge`, `tunnel`,
    `layer`, `surface` are resolved into actual geometry. This is years of accumulated correctness.
  * Its **style system** — and its default style is published separately under **CC0-1.0**
    (https://github.com/tordanik/OSM2World-default-style), which means the style *data* is shippable in our project with no obligations at all.
  * Related MIT-0 helpers: [OSM2World-object-viewer](https://github.com/tordanik/OSM2World-object-viewer),
    [OSM2World-webgl-demo](https://github.com/tordanik/OSM2World-webgl-demo).
* **Architecture fit:** **Good on algorithm, poor on runtime.** The conversion logic is scale-independent; but OSM2World is a whole-world
  in-memory converter with a Java toolchain — a second language and a second build system in a Python 3.12+ project. Its own scale
  behaviour on a 21 400 km² extract is **unverified**.
* **Decision: TAKE-ALGORITHM.** MIT makes it fork-legal, but reimplementing its interpolation rules in Python is more sensible than
  maintaining a JVM sidecar. Port the tag→geometry rules we need and vendor the **CC0 style data** directly. Do not fork the Java codebase.
  Note also the third-party fork [spinachpasta/OSM2Worldtemp](https://github.com/spinachpasta/OSM2Worldtemp) is **LGPL-3.0** — avoid it; take
  upstream from `tordanik`.

### 6.2 osm2streets

* **URL:** https://github.com/a-b-street/osm2streets
* **Description:** A simplified street-network schema plus a library transforming OSM into it: roads between intersections as thickened
  linestrings, intersections as polygon areas with perpendicular road approaches, and **a list of lanes from left to right with type,
  direction and width**.
* **Licence:** **Apache-2.0** (GitHub API `license.spdx_id = "Apache-2.0"`).
* **Alive:** Yes — pushed **2025-10-02**, 158 stars, Rust (with Jupyter notebooks for analysis).
* **Stack:** Rust library with WebAssembly bindings; live demo at https://a-b-street.github.io/osm2streets/; `StreetExplorer` for
  importing any area of the world.
* **What we could reuse:** **This is the schema we want.** The hard part of OSM→BeamNG is not the mesh; it is turning raw OSM ways into
  *roads with a lane list and resolved intersections*. `osm2streets` already produces exactly the intermediate representation that maps
  onto Road Architect **profiles** (lanes left-to-right, each with type and width) and onto junction geometry. It also explicitly handles
  the nasty cases our region will be full of: dual carriageways, separated cycletracks and footways, and complex intersections.
  Apache-2.0 means we may link it or port from it with a `NOTICE` entry.
  * **Documented gap to note:** the schema lists turning movements and crosswalks as *planned*, and bike boxes / stop lines / crossing
    islands / modal filters as *planned*. We would need to supply turn restrictions ourselves.
* **Architecture fit:** Best-in-class. A Rust library with WASM bindings processes whole cities in the live demo; the schema is compact and
  explicitly designed for rendering, routing and analysis on top of it. No per-area limit is documented.
* **Decision: TAKE-ALGORITHM (strong candidate for a real dependency).** Apache-2.0, active, and its output is precisely the shape our
  profile generator needs. Either bind to it or faithfully port its road/intersection schema into Python — and adopt its schema as our
  internal IR regardless, because adopting a proven schema is cheaper than inventing one.

### 6.3 osmnx

* **URL:** https://github.com/gboeing/osmnx
* **Description:** Python for street networks: retrieve, model, analyse and visualise OSM street networks as NetworkX graphs.
* **Licence:** **MIT** (GitHub API `license.spdx_id = "MIT"`).
* **Alive:** **Yes — pushed 2026-07-31**, 5 862 stars, Python.
* **Stack:** Python; NetworkX, GeoPandas, Shapely under the hood.
* **What we could reuse:** Network topology extraction, graph simplification, projection to a metric CRS, and the `osmnx` conventions for
  handling OSM edge attributes. It is the standard, well-documented front door to OSM road graphs in Python and will save us writing
  Overpass queries and graph assembly.
* **Architecture fit:** **Untested at our scale and likely a bottleneck.** `osmnx` builds an in-memory NetworkX graph; a 21 400 km² oblast
  includes a very large number of ways, and NetworkX is not designed for graphs of that size. Expect to need **spatially partitioned
  processing** (per-tile graphs, with boundary reconciliation) rather than one global graph. This constraint is **unverified** as a hard
  limit — it should be measured early.
* **Decision: TAKE-ALGORITHM.** MIT, hugely adopted, actively maintained. Use it, but treat its in-memory model as a per-tile tool rather
  than a whole-region one, and design our pipeline so the graph library is replaceable.

### 6.4 SUMO and `netconvert`

* **URL:** https://github.com/eclipse-sumo/sumo · OSM import docs: https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html
* **Description:** An open traffic simulation suite whose `netconvert` tool imports OSM directly and produces a lane-level network with
  junction logic and connections.
* **Licence:** **EPL-2.0** (GitHub API `license.spdx_id = "EPL-2.0"`; the project also offers `GPL-2.0-or-later` components —
  `earth2road`'s notices list `eclipse-sumo, sumolib, libsumo, traci` as `EPL-2.0 OR GPL-2.0-or-later`).
  **EPL-2.0 is a weak copyleft** — it does not infect our MIT/Apache code as long as we do not copy SUMO source into our project.
* **Alive:** Yes — pushed **2026-10-07**, 4 202 stars.
* **Stack:** C++ core with Python tooling.
* **What we could reuse:** **Run it as an external CLI process**, not as a library. `netconvert --osm-files x.osm` already solves
  OSM → lane-level network → junction connections, and can emit OpenDRIVE, which is the format BeamNG.tech's Road Architect can import.
  That creates one viable end-to-end path today: **OSM → netconvert → OpenDRIVE → (BeamNG.tech) Road Architect**, and a second:
  **netconvert's network → our own profile generator**.
  * Note `earth2road` independently arrived at the same conclusion: SUMO is in its stack for traffic, installed separately and not committed.
* **Architecture fit:** Good. SUMO is designed for city- and region-scale networks.
* **Decision: TAKE-ALGORITHM (external tool).** Do not link it, do not vendor it; invoke it as a subprocess in an optional pipeline stage and
  document EPL-2.0 as a third-party tool. If the BeamNG.tech path is acceptable to users, this may be the fastest route to a lane-correct
  OpenDRIVE network for Ivanovo Oblast.

### 6.5 osm2xp

* **URL:** https://github.com/32kda/osm2xp
* **Description:** OSM → X-Plane scenery generator (with related tooling for other simulators).
* **Licence:** **GPL-3.0** (GitHub API `license.spdx_id = "GPL-3.0"`).
* **Alive:** Yes — pushed **2026-10-07**, 31 stars, Java.
* **Stack:** Java.
* **What we could reuse:** Ideas only — how to decide which OSM features become 3D objects in a vehicle/aircraft simulator, and how to
  prune a whole-planet dataset to a region. The licence forbids copying code into our MIT/Apache project.
* **Architecture fit:** Conceptually comparable (whole-region scenery generation) but not importable.
* **Decision: TAKE-REFERENCE-ONLY.** GPL-3.0 is explicitly excluded by our project constraints.

### 6.6 osm2city

* **URL:** https://wiki.flightgear.org/OSM2City (the project is not on GitHub at `osm2city/osm2city` — API returned **HTTP 404**)
* **Description:** The canonical OSM→3D city generator for FlightGear: buildings, roads, and landcover from OSM.
* **Licence:** reported as **GPL-2.0** on the FlightGear wiki. **Unverified** — no `LICENSE` file was read, because the canonical repository
  was not located at the expected path during this review.
* **Alive:** **Unverified.** The wiki page and Read the Docs build exist and appear maintained, but no commit date was established.
* **Stack:** Python.
* **What we could reuse:** Ideas only. It has solved OSM→building extrusion, roof shapes and LOD for large areas — conceptually the same
  problem as our footprint stage.
* **Architecture fit:** Conceptually good (it is built for whole cities/regions), legally unusable.
* **Decision: TAKE-REFERENCE-ONLY.** GPL-2.0 plus an unverified licence and activity record. Cite it as prior art, copy nothing.

### 6.7 Smaller OSM→3D projects

| Project | URL | Licence | Alive | Decision |
|---|---|---|---|---|
| arnis | https://github.com/Mukller/arnis | **MIT** | pushed 2026-08-04, 0 stars, Rust | **TAKE-ALGORITHM** — a small, readable OSM→Minecraft generator that rasterises roads, buildings and water at **1:1 scale**; a useful sanity model for 1:1 mindset, though its target is a voxel grid, not a mesh. |
| OSM2Mesh | https://github.com/happysmaran/OSM2Mesh | **no licence** (API `null`) | pushed 2026-07-03, 1 star | **IGNORE** — unlicensed. |
| osm2threejs | https://github.com/YusufEminoglu/osm2threejs | **MIT** | pushed 2026-10-05, 0 stars | **TAKE-REFERENCE-ONLY** — headless OSM→glTF/OBJ/GLB; a possible output-format reference. |
| Osm3DBuildingGenerator | https://github.com/whztt07/Osm3DBuildingGenerator | **no licence** (API `null`) | dormant since 2015-07-31 | **IGNORE**. |
| osm2pgsql | https://github.com/osm2pgsql-dev/osm2pgsql | **GPL-2.0** | pushed 2026-09-26, 1 686 stars | **TAKE-REFERENCE-ONLY** — the standard OSM→PostGIS importer; run it as a tool if needed, never link it. |
| openaddresses | https://github.com/openaddresses/openaddresses | **BSD-3-Clause** | pushed 2026-10-05, 3 269 stars | **TAKE-REFERENCE-ONLY** — address coverage for Ivanovo Oblast is **unverified** and likely negligible. |
| `osm2pov` | — | — | — | **Nothing found.** Both `mapillary/osm2pov` and `plepe/osm2pov` returned **HTTP 404**. The project appears not to exist under those names; treat as **unverified / not located**. |

**Decision for the category: TAKE-ALGORITHM from `osm2streets` (Apache-2.0) and `OSM2World` (MIT); TAKE-REFERENCE-ONLY from the GPL
projects.** The OSM→3D problem is well solved in the abstract and badly solved *for BeamNG specifically*, which is the gap we fill.

---

## 7. Procedural city / building-footprint generators

### 7.1 ProbableTrain/MapGenerator

* **URL:** https://github.com/ProbableTrain/MapGenerator
* **Description:** A browser-based procedural city map generator that grows a road network and fills it with buildings.
* **Licence:** **LGPL-3.0** (GitHub API `license.spdx_id = "LGPL-3.0"`).
* **Alive:** Dormant — pushed **2024-05-09**, 1 452 stars, TypeScript.
* **Stack:** TypeScript, browser.
* **What we could reuse:** The *idea* of growing a network by agent-based extension and then subdividing blocks — the natural fallback for
  filling parts of Ivanovo Oblast where OSM building coverage is thin.
* **Architecture fit:** Poor for our purpose. It is 2D and generates *invented* cities; we need *real* footprints derived from OSM, with
  synthesis used only as a clearly-labelled fallback.
* **Decision: TAKE-REFERENCE-ONLY.** LGPL-3.0 is copyleft, and the tool solves a different problem (invent a city vs. reproduce a real one).

### 7.2 Esri CityEngine and CityEngine SDK

* **URL:** https://github.com/Esri/cityengine-sdk
* **Description:** The SDK for Esri's CityEngine, the industry-standard procedural city generator (CGA rule language).
* **Licence:** The **SDK** repository is **Apache-2.0** (GitHub API `license.spdx_id = "Apache-2.0"`). **CityEngine itself is proprietary,
  commercial software** and its procedural-rule authoring is not redistributable.
* **Alive:** SDK pushed 2026-07-01, 223 stars.
* **What we could reuse:** The **CGA concept** — separate *geometry* (parcel/footprint) from *rules* (how to extrude and texture it).
  That separation is good architecture for us: OSM gives us footprints, and a versioned rule file should give us massing and materials.
* **Architecture fit:** CityEngine is genuinely region-scale, so the architecture is right — but it is proprietary and paid, which is
  incompatible with "open-source, reproducible" and with shipping generated assets.
* **Decision: TAKE-REFERENCE-ONLY (concept only).** Adopt the footprint/rule separation in our own design; do not depend on CityEngine or
  its SDK, because a build that requires a commercial licence is not reproducible.

### 7.3 blosm (formerly Blender-OSM)

* **URL:** https://github.com/vvoovv/blosm
* **Description:** A Blender addon for downloading and importing OpenStreetMap, "Google 3D cities", and real-world terrain.
* **Licence:** The README states *"All customers will get the addon source code under GPL license!"* — i.e. **GPL** for the source, with the
  full-featured **Pro** version sold commercially via Gumroad and the base version free.
* **Alive:** Yes — pushed **2026-09-08**, 2 129 stars. GitHub API reports `license: null` because the source is distributed to customers
  rather than published in-repo.
* **What we could reuse:** Nothing copyable. Also note two dependency hazards visible in the README: **"Google 3D cities"** (a Google
  product — see [§10.5](#105-google-maps--google-earth--bing--yandex--confirmed-not-redistributable)) and satellite imagery web maps.
* **Architecture fit:** Blender-based and interactive; not a batch region generator.
* **Decision: IGNORE.** GPL source plus proprietary data sources plus a paid tier that gates the features we would actually want.
  Strictly worse than `BlenderGIS` for reference purposes and worse than `blender-ng` for code purposes.

### 7.4 Building-footprint datasets (relevant to this category)

| Dataset | URL | Licence | Note for Ivanovo Oblast |
|---|---|---|---|
| Overture Maps buildings | https://docs.overturemaps.org/ | **ODbL 1.0** (per earth2road's `docs/SOURCES.md`) | Likely the best global fallback; coverage over Russia is **unverified**. ODbL means it carries the same obligations as OSM data. |
| Microsoft Building Footprints | https://www.microsoft.com/en-us/maps/building-footprints | **CDLA-Permissive-2.0** (per earth2road's `docs/SOURCES.md`; GitHub API reports NOASSERTION for the repo) | Permissive and attractive on licence; **Russia coverage is unverified and historically weak** — must be measured before relying on it. |
| Google Open Buildings | — | — | Repository `googlecreativelab/open-buildings` returned **HTTP 404**; the dataset itself is a Google product and subject to Google's terms. Coverage is focused on the Global South. **Treat as not applicable.** |
| ESA WorldCover 10 m | https://esa-worldcover.org/en | **CC-BY 4.0** — confirmed via Zenodo record https://zenodo.org/api/records/7254221 (`license.id = "cc-by-4.0"`, DOI 10.5281/zenodo.7254221) and the AWS registry `esa-worldcover-vito.yaml` (`License: "CC-BY 4.0"`) | **Land cover, not footprints** — use it for surface attribution (forest/grass/cropland/built-up), which is essential for terrain materials. Global coverage. |

**Decision for the category: TAKE-REFERENCE-ONLY from the generators; TAKE-ALGORITHM from the datasets.** No procedural city generator
found is both permissively licensed and appropriate for reproducing a real region. The correct architecture is: **real footprints from OSM
(and Overture as fallback) for the massing, procedural rules for the detail, and explicit labelling of anything synthesised.**

---

## 8. BlenderGIS and Blender-based GIS → 3D pipelines

### 8.1 BlenderGIS

* **URL:** https://github.com/domlysz/BlenderGIS
* **Description:** A Blender addon to import georeferenced rasters and vectors, fetch basemaps, and build 3D terrain and city models from
  shapefiles/GeoTIFF/OSM.
* **Licence:** **GPL-3.0** (GitHub API `license.spdx_id = "GPL-3.0"`).
* **Alive:** Yes — pushed **2025-12-20**, **9 449 stars**, Python. The most-adopted project in this entire review.
* **Stack:** Python Blender addon; GDAL/PyShapely under the hood.
* **What we could reuse:** **Knowledge, not code.** It encodes an enormous amount of practical GIS-in-Blender lore: CRS handling inside
  Blender, georeferenced import, terrain meshing from DEM, and shapefile→mesh conversion. Notably it is the de-facto answer to "how do I get
  a DEM into a 3D tool", and reading its approach is legitimate.
* **Architecture fit:** **Fails on two counts.** GPL-3.0 is explicitly excluded by our licence constraint, and it is an interactive
  Blender addon, not a headless batch tool.
* **Decision: TAKE-REFERENCE-ONLY.** GPL-3.0 forbids incorporation into an MIT/Apache-2.0 project. This is a hard boundary and worth stating
  explicitly, because BlenderGIS is the most obvious thing a newcomer would reach for.

### 8.2 Blender itself, and headless batching

* **URL:** Blender — https://www.blender.org/ ; licence https://www.blender.org/about/license/ ; headless CLI documented in the manual at
  https://docs.blender.org/manual/en/latest/advanced/command_line/arguments.html
* **Description:** The 3D suite, driven non-interactively with `blender --background --python script.py`.
* **Licence:** **GPL-2.0-or-later** for Blender itself. Critically, this does **not** infect our project, because invoking a GPL program as a
  **separate process** over a CLI is not linking — the same reasoning that lets us call GDAL, SUMO, `netconvert` or QGIS.
  **This distinction is important to record**: GPL *tools* are usable; GPL *libraries and addons we import into our own code* are not.
* **Alive:** Yes — Blender's own repository was pushed 2026-10-07.
* **What we could reuse:** An optional, fully scriptable geometry stage: import our generated meshes, apply operators, export `.dae`/`.obj`/`.gltf`
  for BeamNG, all inside CI via the headless flag.
* **Architecture fit:** Good. Headless Blender is used for exactly this kind of batch asset work at scale. The risk is that it adds a large
  external binary to the pipeline; it should therefore be **optional**, with the core pipeline able to emit level files without it.
* **Decision: TAKE-ALGORITHM (external tool, optional stage).** Use it as a subprocess, never as an imported module, and keep it out of the
  critical path so that a fresh checkout can build a map without Blender installed.

### 8.3 Blender-based pipelines found

| Project | URL | Licence | Alive | Decision |
|---|---|---|---|---|
| blender-ng | https://github.com/mate-h/blender-ng | **Unlicense** | pushed 2025-08-10, 6 stars | **FORK** — see [§3.1](#31-mate-hblender-ng--beamngdrive-blender-importerexporter). The one truly reusable Blender-adjacent asset. |
| geo2blender | https://github.com/EnriqueFuster/geo2blender | **MIT** (per search-result metadata; API returned rate-limited `403` before confirmation — treat SPDX as **unverified**) | pushed 2025-11-07, 1 star | **TAKE-REFERENCE-ONLY** — *"preprocessing large DSM/DTM and satellite raster datasets into Blender-ready"* outputs; conceptually close to our DEM stage but very small. |
| BlenderGIS | https://github.com/domlysz/BlenderGIS | **GPL-3.0** | pushed 2025-12-20, 9 449 stars | **TAKE-REFERENCE-ONLY**. |
| blosm | https://github.com/vvoovv/blosm | GPL / paid Pro | pushed 2026-09-08 | **IGNORE**. |

**Decision for the category: FORK `blender-ng`; use headless Blender as an optional subprocess; take BlenderGIS as reference only.**

---

## 9. QGIS → BeamNG and DEM → BeamNG workflows

**Honest finding: there is no QGIS → BeamNG workflow, and no QGIS plugin targeting BeamNG.** A GitHub search for `beamng gis` returned
**one** result, and it was not a QGIS plugin: https://github.com/KingStu90/LiDAR-to-Heightmap-Beginner-Tutorial (MIT; see [§5.2](#52-kingstu90lidar-to-heightmap-beginner-tutorial)).

### 9.1 The nearest thing that exists: a QGIS → Unity exporter

* **URL:** https://github.com/liviaruegger/unity-terrain-exporter
* **Description:** *"QGIS plugin to process and export heightmaps (GeoTIFF) to Unity's terrain (.raw) format."*
* **Licence:** **GPL-3.0** (GitHub API `license.spdx_id = "GPL-3.0"`).
* **Alive:** Pushed **2025-11-24**, 12 stars, Python.
* **What we could reuse:** The **pattern** — a QGIS Processing plugin that takes a GeoTIFF and emits an engine-specific heightmap container
  (Unity `.raw`). That is structurally the same as our GeoTIFF → BeamNG `.ter` step, and it proves the shape of the solution. Writing a
  QGIS plugin is therefore a plausible future front-end for `ivanovo2beam`, but not a component we can copy.
* **Architecture fit:** A QGIS plugin is inherently interactive and single-area; it cannot drive a 117-tile region build.
* **Decision: TAKE-REFERENCE-ONLY.** GPL-3.0 blocks reuse, and a QGIS plugin is the wrong layer for a reproducible pipeline. Record it as
  evidence that the GeoTIFF→engine-heightmap pattern is standard, and as a possible optional GUI later.

### 9.2 QGIS itself

* **URL:** https://qgis.org/ · licence: https://qgis.org/en/site/about/features.html (GPL-2.0-or-later)
* **Assessment:** QGIS is **GPL-2.0-or-later**, but it is a **desktop application invoked as a separate process** (including headless via
  `qgis_process`). As with Blender and GDAL, using it as a tool does not make our project GPL. It is, however, an interactive application
  whose scripting surface is oriented toward single-dataset operations — a poor fit for a 117-tile reproducible build.
* **Decision: TAKE-REFERENCE-ONLY / optional.** Document QGIS as a *manual inspection and QA aid* for humans working on the pipeline
  (the BeamNG.tech importer docs themselves suggest QGIS for preparing real-world terrain), but do not make it a build dependency.
  **Unverified:** whether `qgis_process` headless is a practical CI dependency at our data volumes; not tested.

### 9.3 DEM → engine heightmap reference implementations

| Project | URL | Licence | Note | Decision |
|---|---|---|---|---|
| UE4HmapsGenerator | https://github.com/Rodrigo-NH/UE4HmapsGenerator | **BSD-3-Clause** | *"prepare and tile from SRTM (and others) DEM to UE heightmaps keeping"* true scale. 40 stars, dormant since 2021-03-03. | **TAKE-ALGORITHM** — the only public worked example of correctly tiling a global DEM into fixed-size engine heightmaps at true scale, which is exactly our tiling problem. BSD-3-Clause is permissive. |
| DEM2UE | https://github.com/Babamal/DEM2UE | **MIT** (search metadata; not API-confirmed) | Python GUI converting GeoTIFF DEMs to UE5 heightmaps and volume meshes. pushed 2026-06-26. | **TAKE-REFERENCE-ONLY** — small; useful as a second opinion on scaling/encoding. |
| GeoTIFF2Heightmap | https://github.com/Kmschr/GeoTIFF2Heightmap | **MIT** | Converts GeoTIFF rasters to RGBA-encoded PNG heightmaps. Dormant (2023-10-03). | **TAKE-ALGORITHM** — the 16-bit-in-RGBA encoding trick is directly relevant to BeamNG's `R16`/16-bit PNG heightmap requirement. |
| dem2hm | https://github.com/maloquacious/dem2hm | **MIT** | GeoTIFF DEM → compact heightmaps with a canonical reader. pushed 2026-09-30. | **TAKE-REFERENCE-ONLY**. |
| phstl | https://github.com/anoved/phstl | **MIT** | GDAL rasters (GeoTIFF heightmaps) → 3D STL surfaces, closed 2024-01-19. | **TAKE-REFERENCE-ONLY** — a clean minimal example of GDAL raster → mesh. |
| dem-renderer | https://github.com/JustCreature/dem-renderer | **MIT** | Rust real-time DEM raymarcher. pushed 2026-06-19. | **IGNORE** — visualisation only, no geometry output. |
| heightmap-slicer | https://github.com/jojolebarjos/heightmap-slicer | **MIT** | Topographic raster → smooth contour layers. 2023-12-25. | **IGNORE** — wrong output. |
| HeightMapTo3dTerrain | https://github.com/shanto462/HeightMapTo3dTerrain | **MIT** (search metadata) | Heightmap → mesh with adaptive Delaunay. pushed 2026-10-06, 23 stars. | **TAKE-ALGORITHM** — adaptive triangulation is relevant to decimating terrain while keeping road corridors flat. |
| contour | https://github.com/FelixMiddelhoff/contour | **no licence** (API `null`) | 16-bit PNG / `.r16` georeferenced heightmap export from a map selection. | **IGNORE** — unlicensed. |
| dem2mesh | https://github.com/blaze33/dem2mesh | **Apache-2.0** | WASM lib to process DEM heightmaps for three.js. Dormant 2020. | **TAKE-REFERENCE-ONLY**. |

**Decision for the category: the QGIS→BeamNG category is genuinely empty; the DEM→engine-heightmap category yields one strong algorithm
(`UE4HmapsGenerator`, BSD-3-Clause) and one encoding trick (`GeoTIFF2Heightmap`, MIT).**

---

## 10. Licensing analysis for `ivanovo2beam`

This section states the obligations we must satisfy. Every claim is sourced. Where a primary source did not settle the question, it says so.

### 10.1 OpenStreetMap — ODbL 1.0

**Licence.** Open Database License v1.0. Full text: https://opendatacommons.org/licenses/odbl/1-0/ ·
Data copyright page: https://www.openstreetmap.org/copyright

**Is a generated game map a "Derivative Database" or a "Produced Work"?** This is the crux, and ODbL answers it in three places:

* **§1 Definitions — "Produced Work":** *"a work (such as an image, audiovisual material, text, or sounds) resulting from using the whole or a
  Substantial part of the Contents (via a search or other query) from this Database…"* A baked game map is an image/audiovisual work.
* **ODbL §4.5(b):** *"Using this Database, a Derivative Database, or this Database as part of a Collective Database to create a Produced Work
  **does not create a Derivative Database for purposes of Section 4.4**"* — i.e. **creating a Produced Work does not trigger share-alike.**
* **ODbL §4.4(c):** *"A Derivative Database is Publicly Used and so must comply with Section 4.4 **if a Produced Work created from the
  Derivative Database is Publicly Used**."* — i.e. share-alike binds the *database*, and publishing the map merely makes the underlying
  database's public use effective.
* **ODbL §4.6 (Access to Derivative Databases):** *"If You Publicly Use a Derivative Database or a Produced Work from a Derivative Database,
  You must also offer to recipients … a copy in a machine readable form of: (a) The entire Derivative Database; or (b) A file containing all
  of the alterations made to the Database or **the method of making the alterations to the Database (such as an algorithm)**…"* — and it
  *"must be … free of charge if distributed over the internet."*

**OSMF's own guidance.** The Produced Work guideline (endorsed by the OSMF board 2014-06-06) states the test plainly:
*"If the published result of your project is intended for the extraction of the original data, then it is a database and not a Produced Work.
Otherwise it is a Produced Work. However, if you publish a produced work, **the underlying database has to be published as well** …
according to section 4.6 of ODbL."* — https://osmfoundation.org/wiki/Licence/Community_Guidelines/Produced_Work_-_Guideline

**What this means for us, concretely.**
1. **Our shipped game map is a Produced Work.** It is a baked, human-viewable artefact; a player cannot extract the OSM database from it
   (this is the "intended for the extraction of the original data" test). So we may license our generated map **however we like** — MIT/Apache-2.0
   is permissible for the *map artefact*. Cite ODbL §4.5(b) and the Produced Work guideline.
2. **Our intermediate geodata is a Derivative Database**, and we almost certainly will produce one: the pinned OSM extract plus our processed
   road centrelines, lane assignments, junction topology and building footprints *is* a database derived from OSM. Publishing the map makes
   its public use effective (§4.4(c)).
3. **Therefore §4.6 applies and we must offer, free of charge, either the entire Derivative Database or the algorithm/method by which it was
   made.** For a *reproducible open-source pipeline with pinned inputs*, option (b) is the natural and cheap choice: our **source code plus
   the pinned OSM extract and its checksum** is the method of making the alterations, published free on the internet. This is exactly the
   posture `earth2road` adopted — it keeps the OSM snapshot and its SHA-256 in `inputs/` and states that *"worlds and exported maps derived
   from OSM are Produced Works / Derivative Databases under ODbL"*
   (https://github.com/Alezx311/earth2road/blob/main/THIRD_PARTY_NOTICES.md).
4. **Attribution is mandatory** (§4.3, and the OSMF guidelines). **Exact required text and placement:**
   * Required attribution must be **to "OpenStreetMap"** and must make clear the data is under the **Open Database License**. The historical
     form **"© OpenStreetMap contributors"** is explicitly acceptable, and *"Map data from OpenStreetMap."* is offered as an example for
     re-rendered data.
   * **Safe harbour for computer games** (verbatim from https://osmfoundation.org/wiki/Licence/Attribution_Guidelines): *"For video or
     computer games, attribution can be provided either by a splash screen on application startup, in the game view, during gameplay, on the
     credits page, in the menu, or in another suitable location. Detailed information must be provided in a suitable location if the initial
     attribution does not provide detailed information."* A splash screen must be *"easily legible and visible such that the typical viewer
     has time to comprehend the attribution."*
   * Because our map is **offline-capable**, the link must still resolve to readable licence information — so we must ship a local
     in-game/menu "Data licences" page, not only a URL.
   * **Scope the credit.** OSMF asks that we *"only attribute OpenStreetMap for data which comes from our database and not for any other
     geodata"* — so our notices must separately credit OSM, Copernicus DEM and ESA WorldCover.
   * **Where to put it:** in-game credits/menu **and** a `NOTICE`/`ATTRIBUTION` file in the repository and in the shipped mod archive.
5. **Do not impose extra restrictions.** ODbL §4.7(a) forbids terms or technological measures on the database that restrict the licence's
   rights; §4.8 forbids sublicensing the database and forbids further restrictions. Practically: our OSM-derived **data** artefacts must be
   redistributed under ODbL, and we may not add DRM or restrictive EULAs on top of them.

**The single biggest constraint:** *we must publish our OSM-derived intermediate data (or the reproducible method plus pinned inputs) for free,
and we must attribute OSM visibly in-game.* We may **not** treat the OSM-derived geodata as MIT/Apache — only our **code** and the **baked
map artefact** can be.

### 10.2 Copernicus DEM GLO-30

**Licence status: free, with a mandatory and very specific attribution notice.**

* The AWS Open Data registry entry states: *"GLO-30 Public and GLO-90 are available on a free basis for the general public under the terms
  and conditions of the Licence found"* at the Copernicus Data Space collection page — https://github.com/awslabs/open-data-registry/blob/main/datasets/copernicus-dem.yaml
* **Mandatory notice** (verbatim from https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM):
  * Unmodified redistribution: **`© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved`**
  * **Adapted/modified data** — which is what we produce: **`produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved`**
* **Citation DOI (requested for research use):** https://doi.org/10.5270/ESA-c5d3d65
* The same page states the data were *"made available for use in 2019 and will be maintained until 2026"*, and that GLO-30 is a **DSM**
  (includes buildings, infrastructure and vegetation) derived from an edited **WorldDEM™** — so our terrain will contain vegetation and
  building height, not bare earth. Vertical accuracy is stated as **< 4 m absolute (90% LE)** and **< 2 m relative for slopes ≤ 20%**;
  horizontal **< 6 m (90% CE)**.
* **The IPR chain matters:** the ESA Mission Specific Annex notes that *"The Intellectual Property Rights (IPRs) and related rights attached to
  the Copernicus WorldDEM™-90 does not belong to Copernicus…"* — https://spacedata.copernicus.eu/documents/20123/121286/CSCDA_ESA_Mission-specific+Annex_31_Oct_22.pdf
  This is why the notice names DLR and Airbus. **Unverified:** whether the ESA User Licence imposes any *additional* restriction on
  redistributing **derived** products; the User Licence PDF itself
  (https://dataspace.copernicus.eu/sites/default/files/media/files/2025-06/copernicus_contributing_mission_data_access_v2_cop_dem_licenses.pdf)
  was not read in this review. **This should be read before shipping**, because it is the one open question in our data licensing.

**⚠️ Access situation — this is a reproducibility risk, not a licensing one.** Copernicus announced on **2026-07-17** that from
**28 July 2026** the **COP-DEM 30 m view service is restricted to authorised user categories**, requiring users to register as
**Copernicus Contributing Missions (CCM)** users by selecting *"I am also interested in accessing Copernicus Contributing Missions data"*;
*"Users who do not complete these steps will no longer be granted access to the COP DEM 30m View Service."*
— https://dataspace.copernicus.eu/news/2026-7-17-copernicus-dem-30m-view-service-license-acceptance
A community thread also reports `Cannot download CopDEM COG from s3 anymore`:
https://forum.dataspace.copernicus.eu/t/cannot-download-copdem-cog-from-s3-anymore/5520
Per the collection page, **general public** users can *view* all instances and *download* GLO-90 and GLO-30, and full/bulk download is via
**S3-compatible object storage and OData API using temporary credentials**.
**Unverified:** whether `s3://copernicus-dem-30m` (eu-central-1) remains open and anonymous-read as of 2026-10-07; no authoritative
shut-off notice was located. **Action for us:** treat Copernicus DEM acquisition as a *swappable, credential-aware source* with a pinned
checksum manifest, and keep a documented fallback (see §10.4) so the build never depends on one gated endpoint.

### 10.3 ESA WorldCover 10 m

* **Licence: CC-BY 4.0.** Confirmed from two independent primary sources:
  * AWS Open Data registry: `License: "CC-BY 4.0"` — https://github.com/awslabs/open-data-registry/blob/main/datasets/esa-worldcover-vito.yaml
  * Zenodo DOI record **10.5281/zenodo.7254221** ("ESA WorldCover 10 m 2021 v200"), API-reported `license.id = "cc-by-4.0"` —
    https://zenodo.org/records/7254221
* **Obligation:** attribution is required (CC-BY 4.0). There is **no share-alike**, so WorldCover-derived raster or material assignments do
  not infect our licence. Commercial use and redistribution are permitted.
* **Suggested attribution text:** `Contains modified ESA WorldCover 10 m 2021 v200 data, © ESA WorldCover project 2021, licensed under CC BY 4.0 (DOI: 10.5281/zenodo.7254221)`.
  **Unverified:** ESA WorldCover does not publish a single prescribed verbatim attribution string in its own materials; the above follows
  CC-BY 4.0's requirements (credit, licence, link, indication of changes). Verbatim wording should be confirmed at https://esa-worldcover.org/en.
* **Access:** `arn:aws:s3:::esa-worldcover` (eu-central-1), STAC endpoint https://services.terrascope.be/stac/, per the registry entry.
  Note the older `esa-worldcover` registry listing is marked **deprecated** in favour of `esa-worldcover-vito`.

### 10.4 SRTM and AWS Terrain Tiles

**SRTM (NASA/USGS Shuttle Radar Topography Mission).**
* SRTM data are a product of the U.S. Government and are distributed as **public domain**; USGS requests but does not require attribution.
  Primary references: USGS SRTM documentation
  (https://lpdaac.usgs.gov/sites/default/files/public/measures/docs/SRTM%20Quick%20Guide.pdf) and the USGS Land Remote Sensing fact sheet
  (https://pubs.usgs.gov/fs/2005/3068/report.pdf). **Unverified:** a single canonical sentence stating "public domain" from a USGS page was
  not isolated during this review; the conclusion rests on the general U.S. Government works position plus USGS's data-courtesy practice.
  Treat SRTM as safe to use, and credit USGS as a courtesy.

**AWS "Terrain Tiles" (Mapzen/Tilezen, `elevation-tiles-prod`).**
* **Registry entry:** https://registry.opendata.aws/terrain-tiles/ ·
  YAML: https://github.com/awslabs/open-data-registry/blob/main/datasets/terrain-tiles.yaml ·
  Buckets: `arn:aws:s3:::elevation-tiles-prod` (us-east-1) and a replication `arn:aws:s3:::elevation-tiles-prod-eu` (eu-central-1).
* **Licence:** the registry does **not** state a single licence; it defers to a **per-source attribution list**:
  `License: https://github.com/tilezen/joerd/blob/master/docs/attribution.md`. That document is explicit:
  *"Attribution is required for many terrain tile data providers… **you are responsible for researching each project to follow their license
  terms.**"*
* **Relevant verbatim lines from that required-attribution block:**
  * `* Europe terrain data produced using Copernicus data and information funded by the European Union - EU-DEM layers;`
  * `* United States 3DEP (formerly NED) and global GMTED2010 and SRTM terrain data courtesy of the U.S. Geological Survey.`
  * `* Global ETOPO1 terrain data U.S. National Oceanic and Atmospheric Administration`
  * (an additional block adds `* Mapzen` when using Mapzen's hosted service; the dataset is now managed by *"Mapzen, a Linux Foundation project"*
    per the YAML.)
* **Which source applies to Ivanovo Oblast?** The list includes **EU-DEM layers** for Europe. Whether Ivanovo Oblast (Russia) falls under
  the EU-DEM coverage or under the global GMTED2010/SRTM layer is **unverified** and must be determined per-tile before relying on the
  attribution text. The safe approach is to credit **both** applicable lines.
* **Do not aggregate blindly.** Because the dataset mixes sources with *different* terms — including **"(All rights reserved)"** entries such as
  New Zealand LINZ and the UK Environment Agency — a naive "download terrain tiles for the region" step could pull data whose terms forbid
  redistribution. Our pipeline must **attribute per tile and refuse to ship tiles whose source terms are unknown or non-redistributable.**

**Practical recommendation.** AWS Terrain Tiles is the weakest link in our data chain **from a licensing-cleanliness standpoint**: it is a
convenience aggregate over many sources, some of which are "all rights reserved". Prefer **Copernicus DEM GLO-30 as primary** (one clear
licence, one clear notice) and use Terrain Tiles only as a **documented fallback**, with per-source attribution resolved and with a
provenance record per tile.

### 10.5 Google Maps / Google Earth / Bing / Yandex — **confirmed NOT redistributable**

All four prohibit the use we would need. These are hard exclusions.

**Google Maps — prohibited.**
Google Maps End User Additional Terms of Service, §2 *Prohibited Conduct* (https://maps.google.com/help/terms_maps/, last modified 2026-01-27):
* §2.1 — you may not *"redistribute or sell any part of Google Maps or **create a new product or service based on Google Maps**"*;
* §2.2 — you may not *"copy the content"* except as permitted by the Geo Guidelines or applicable law;
* §2.3 — you may not *"mass download or create bulk feeds of the content"*;
* §2.4 — you may not *"use Google Maps to create or augment any other mapping-related dataset … for use in a service that is a substitute
  for, or a substantially similar service to, Google Maps."*
A drivable game map of Ivanovo Oblast is comfortably inside §2.1 and §2.4. The licence granted is only to *"view and annotate maps"*,
*"create KML files"* and *"publicly display content with proper attribution online, in video, and in print"* — none of which covers baking
imagery or geometry into a redistributable game asset. Google Earth has separate additional terms
(https://maps.google.com/help/terms_maps-earth/, effective 2025-06-05), and Google's own permissions page
(https://www.google.com/permissions/geoguidelines/) governs the narrow cases where Google content may be reused.
**Decision: do not use Google Maps or Google Earth data or imagery in any form, at any pipeline stage, including as a visual QA reference
committed to the repository.**

**Bing Maps — prohibited.**
Microsoft Bing Maps and Embedded Maps Service Terms of Use (https://www.bingmapsportal.com/terms/EndUserTerms, last updated 2021-11-01):
* §3(b) — *"You may not modify, copy, distribute, transmit, display, perform, reproduce, publish, license, **create derivative works from**,
  sublicense, transfer, assign, rent, sell, or otherwise convey any information, software, products, or services obtained from Bing Maps
  Services **without the prior written approval from Microsoft**."*
* §2(b) — *"You agree not to … **copy, cache, or retain** any maps, routes, images, or other data provided to you by Microsoft as part of
  the Services."*
* §4(g) — *"You agree that you will not use Bird's Eye Imagery or Traffic data."*
* §4(m) — no local non-cache copies. §4(o) — no automated process (BOT/spider/periodic caching) to access the service.
**Decision: do not use Bing imagery or data. Note that §2(b) and §4(o) together also forbid building a cached tile pipeline against Bing
even for internal use.**

**Yandex Maps — prohibited.**
Yandex's Maps API community has explicit, Yandex-authored threads banning game projects. See
*"Запрет на игровые проекты"* ("Ban on game projects") — https://www.yandex.ru/blog/mapsapi/44160 — and the related discussion
*"Вопрос про использование карт для 'игровых проектов'"* ("Question about using maps for 'game projects'") —
https://www.yandex.ru/blog/mapsapi/56722, with the API terms at https://yandex.com/legal/maps_api/.
**Decision: do not use Yandex Maps tiles or data.** This is especially relevant because Ivanovo Oblast is in Russia and Yandex has the best
local coverage — which makes it the most tempting shortcut and therefore the most important exclusion to record in writing.
**Unverified:** the exact clause numbers in the current Yandex Maps API legal terms were not read; the ban is established from Yandex's own
community/API-club posts, which are Yandex-authored but not the licence text itself.

**Consequence for the project.** We have **no** satellite or aerial imagery we may bake into the shipped map. Every surface texture must
therefore be either **procedurally generated**, or sourced from **CC0** asset libraries (as `earth2road` does with ambientCG and Poly Haven,
both CC0 1.0 per its `THIRD_PARTY_NOTICES.md`), or derived from **CC-BY** land cover (ESA WorldCover). This is a design constraint on the
look of the map, not just a legal footnote.

### 10.6 BeamNG.drive's own constraints on redistributing assets

**EULA** — https://beamng.com/game/support/policies/eula/ (German law, courts of Hamburg, §14). Key clauses:

* **§1 Ownership** — *"All right, title, interest and ownership rights at / in the Software and any copyright … (including but not limited to
  any titles, computer code, themes, objects, characters … locations, concepts, artwork, animations, sounds …) are owned by, belong to and
  vest in BeamNG or its licensors. All rights are asserted and reserved."* **The map locations and art are BeamNG's.**
* **§3.3** — the licence *"does not grant the right to modify the source code of the Software, not to **integrate parts of the Software** or
  the Software as a whole **into another work** except if permitted in our modding guidelines."*
* **§4.1** — you may not *"sell, distribute or otherwise transfer copies or reproductions of the Software to other parties in any way"*.
* **§4.2** — you may not *"separate component parts for use"*; *"The Software is licensed to you as a single product. Some source files might
  be licensed differently."*
* **§11.1–11.2 (UGC)** — *"User Generated content shall be defined as any kind of content that is generated by the user within or for the
  Software including, but not limited to replays, skins, textures, mods…"*; the creator **still owns** the UGC but grants BeamNG a worldwide,
  royalty-free, perpetual, irrevocable, sub-licensable licence **for content uploaded to beamng.com or BeamNG's services**.
* **§11.4** — *"The user should not setup special usage terms or license fees if the User Generated Content is distributed or advertised
  within the beamng.com website and services of BeamNG."* **This is a real constraint on us:** if we publish on the BeamNG repository, a
  permissive open-source licence is a good-faith reading of "no special usage terms", but we should not attach fees or restrictive terms to
  the mod as distributed there.
* **§11.5** — by publishing UGC on the internet you acknowledge other users may use, copy, archive and **create derivative works**.
* **§11.10** — the uploader warrants the UGC does not infringe third-party IP and indemnifies BeamNG. **Our ODbL and Copernicus obligations
  are therefore our problem, not BeamNG's** — which is exactly why this document exists.

**Modding Guidelines** — https://www.beamng.com/game/support/policies/modding-guidelines/:
* *"You are **not allowed to use copyrighted content and content that belongs to other users** without permission in your mods."*
* *"You **must not overwrite any default game data**. This is also valid for parts within jbeam files, mesh names within .dae files or objects
  within your materials.json files."*
* *"Do not include unused files in your mod."*
* Mods *"must work without the need of any additional step, just by dragging and dropping the zip into the mods folder"*, must respect the
  correct folder structure, and use **unique** file/part names.
* Every uploaded mod is **manually reviewed** before publication.

**Answering the specific question: may a level REFERENCE base-game assets rather than copy them?**
**Yes — and that is the intended, documented workflow.** Evidence:
* The Modding Guidelines' prohibition is on *overwriting* default game data and on using others' copyrighted content — not on referencing
  base-game materials, meshes and prefabs by path.
* BeamNG's own official content does exactly this: the terrain documentation shows `TerrainBlock` referencing engine paths such as
  `/levels/example/art/terrains/t_terrain_base_grass_b.png`, and the terrain-material system expects a shared
  `TerrainMaterialTextureSet` per level.
* `earth2road`, an MIT-licensed project distributing BeamNG mod ZIPs, states it plainly:
  *"The BeamNG export references stock BeamNG.drive meshes and textures by path. No BeamNG file is copied into the generated ZIP.
  BeamNG.drive is required to play the exported maps and is subject to its own EULA."*
  (https://github.com/Alezx311/earth2road/blob/main/THIRD_PARTY_NOTICES.md) — and its README adds:
  *"The ZIP contains no BeamNG game files: stock trees, props and textures are referenced from your copy of the game."*
* For material *paths and internal names* we need to resolve against an installed game, the community reference is the official
  [Materials](https://documentation.beamng.com/modding/materials/) documentation and the
  [Resources Checker](https://documentation.beamng.com/world_editor/tools/resources_checker/) /
  [Asset Deduplicator](https://documentation.beamng.com/world_editor/tools/asset_deduplicator/) editor tools.

**Prohibited for us:**
* Shipping **any** copy of a BeamNG file — meshes, textures, DDS, prefabs, `.dae`, materials, levels, vehicles. Not even one.
* Overwriting default game data or reusing base-game mesh/object/material *names* for our own content.
* Separating and redistributing component parts of the game (§4.2).

**Required for us:**
* Reference base-game assets **by engine path only**, and document that BeamNG.drive is a prerequisite.
* Use **unique names** for our own materials/meshes/objects to avoid collisions with base game and other mods.
* Ship a mod ZIP with correct folder structure that works on drag-and-drop, containing no unused files.
* Accept that a beamng.com publication is manually reviewed and must not carry fees or special usage terms (§11.4).

**`roadArchitect` profiles and the game install.** The profile JSONs are read by the built-in Road Architect tool
(https://documentation.beamng.com/world_editor/tools/road_architect/disk_options/). Those JSON files are **BeamNG's content**; we may read
them to learn the schema on a user's own installation, but a copy of them must not be committed to our repository or shipped in our mod.
Our generator should emit **our own** profile definitions using our own names. **Unverified:** the on-disk location and schema of the shipped
profile files; no public documentation was found, so this must be established empirically from an installed 0.39.x.

**BeamNG's own open-source posture (useful precedent).** BeamNG GmbH publishes permissively-licensed tooling: **BeamNGpy is MIT**
(https://github.com/BeamNG/BeamNGpy). So an MIT-licensed project *around* BeamNG is an accepted pattern — our licence choice is consistent
with the vendor's own practice.

### 10.7 Licence obligations checklist for `ivanovo2beam`

**What we may ship:**

| Artefact | May we ship it? | Under what terms |
|---|---|---|
| Our pipeline source code | Yes | MIT or Apache-2.0 (our choice) |
| Baked game-map geometry (`.ter`, level JSON, meshes, materials) | Yes | Our choice of licence — it is a Produced Work (ODbL §4.5(b)) |
| Procedurally generated textures | Yes | Our choice |
| CC0 textures (ambientCG, Poly Haven, Kenney) | Yes | CC0 1.0 — no obligations |
| ESA WorldCover-derived land-cover rasters in material assignment | Yes | CC-BY 4.0 + attribution |
| Copernicus DEM-derived heightmap | Yes | Free licence + the **verbatim** WorldDEM notice |
| OSM-derived **geodata** (extracts, road graphs, footprints) | Yes | **ODbL 1.0 — must stay ODbL, must not be relicensed MIT/Apache** |
| Overture Maps building footprints (if used) | Yes | **ODbL 1.0** — same obligations as OSM |
| Microsoft Building Footprints (if used) | Yes | **CDLA-Permissive-2.0** per earth2road's notice; **coverage for Russia unverified** |
| Copies of BeamNG base-game files | **No** | EULA §4.1/§4.2 |
| Google / Bing / Yandex imagery or tiles | **No** | ToS prohibitions in §10.5 |
| Mapbox / other proprietary imagery | **No** | Proprietary |
| Any code from an unlicensed repository | **No** | All rights reserved |
| Any GPL/AGPL code in our MIT/Apache project | **No** | Copyleft incompatible with our stated licence policy |

**Attribution text we must ship** (in-game credits/menu + `NOTICE` file in repo and mod archive), following the OSMF "computer games"
safe harbour and each provider's own requirement:

```
Map data © OpenStreetMap contributors — available under the Open Database License (ODbL 1.0).
  https://www.openstreetmap.org/copyright

Terrain produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and
  © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS
  by the European Union and ESA; all rights reserved.
  DOI: https://doi.org/10.5270/ESA-c5d3d65

Land cover: Contains modified ESA WorldCover 10 m 2021 v200 data,
  © ESA WorldCover project 2021, licensed under CC BY 4.0
  (DOI: 10.5281/zenodo.7254221).

Surface textures: ambientCG (CC0 1.0), Poly Haven (CC0 1.0).
  <list any others actually used>

This map requires BeamNG.drive. No BeamNG game files are included;
stock assets are referenced by path from your installation.
```

**Placement rules we must satisfy:**
* OSM: visible in-game (splash, menu, credits or map view) **and** a machine-readable/offline-readable "Data licences" page in the mod,
  because the map works offline. Attribution must not require interaction to see.
* Copernicus: the **exact** prescribed sentence, naming DLR and Airbus — do not paraphrase.
* WorldCover: credit + licence + link + indication of modification (CC-BY 4.0).
* Terrain Tiles (if used as fallback): per-source list from
  https://github.com/tilezen/joerd/blob/master/docs/attribution.md, with the applicable EU-DEM and/or USGS lines, **and a per-tile
  provenance record** so we can prove which source produced which tile.
* BeamNG: state that the game is required and no game files are included.

**Reproducibility obligations we are imposing on ourselves (and why they help legally):**
* Pin every input with a **URL + SHA-256 + retrieval date** in a machine-readable manifest.
* Publish that manifest, and the processing code, as the §4.6(b) "method of making the alterations" — this simultaneously satisfies ODbL
  §4.6 and our own reproducibility goal. **These are the same artefact.**
* Do not commit large source datasets to the repository; commit the manifest and a fetch script instead (mirroring `earth2road`'s approach
  of keeping snapshots out of git and recording checksums in the build audit).

---

## 11. What we must build ourselves

Everything below is a gap no existing project fills. Ordered roughly by criticality.

**1. A terrain tiling and paging strategy for a non-square 220 × 145 km region — the hardest problem.**
BeamNG terrain is a **square, power-of-two grid of 128–8192 samples**, and object scaling is disabled
(https://documentation.beamng.com/modding/levels/level_formats/terrain/). At 8192 samples a *single* terrain spanning our 220 km width
forces `squareSize ≈ 26.9 m` — coarser than a road. Our 220 : 145 aspect ratio (≈ 1.52 : 1) cannot be represented by a square grid without
padding or clipping. Nothing in the ecosystem solves this: every tool found is single-tile and city-scale. We must (a) determine
**empirically** whether BeamNG.drive 0.39.x supports multiple `TerrainBlock` objects per level with correct collision, seams, LOD and
streaming — **currently unverified, no official documentation either way**; (b) design a tile grid (roughly 13 × 9 tiles at 2 m/px, far more
at 1 m/px) with a seam strategy, since terrain cells already emit automatic skirt geometry that we must not double up; and (c) budget memory,
because a 2048² tile is ~12 MiB of `.ter` and a 117-tile region is ≈ 1.4 GiB of heightmap alone before layer maps, materials and textures.
*BeamNG's own docs recommend 128–8192 and "keep terrain resolution power-of-two" but give no guidance for multi-tile worlds.*

**2. A road-collision model that survives tiles and junctions.** The community split on this — `beamng-map-import-assistant` chose
`MeshRoad` for "driveable road output", while `blender-ng`'s analysis states `DecalRoad` has **no collision**
(https://github.com/mate-h/blender-ng/blob/main/BeamNG_Road_Analysis.md). The official docs describe the Road Architect as generating decals,
meshes, *and* a collision mesh, and the BeamNG.tech importer as importing terrain, importing roads as Decal Roads, **then terraforming the
terrain so roads lie flat**. So the drivable surface is likely the terraformed terrain, not the road decal. **Unverified:** the definitive
answer. We must establish, against 0.39.x, which generated construct a vehicle can actually drive on, and then automate the terraforming step
(Margin + domain-of-influence semantics per the BeamNG.tech importer docs) ourselves — including across tile boundaries and at junctions where
roads of different classes meet.

**3. OSM → lane-level profile assignment with automatic junction resolution at region scale.** `osm2streets` (Apache-2.0) gives us the target
schema and SOLVES the dual-carriageway / separated-footway / complex-intersection cases, but explicitly leaves turn movements and crosswalks as
*planned*. BeamNG's Road Architect consumes **profiles** (lanes left-to-right with widths and types) and has hard **road-class compatibility
rules** (Urban / Highway / Dirt; cross-class joins need transition blocks)
(https://documentation.beamng.com/world_editor/tools/road_architect/introduction/). Mapping 21 400 km² of Russian OSM tagging onto those
profiles — and generating junction geometry — is ours. `earth2road` does a version of this at ≤ 5 km; SUMO's `netconvert` could supply the
topology, but nothing exists that emits BeamNG profile JSON at regional scale.

**4. A Russian Federation regional profile.** `earth2road` is explicit: *"The Ukrainian region profile is the verified one; other countries
build as experimental (no posted road signs, generic licence plates)."* There is **no** Russian profile anywhere — not for road classes and
signs, not for licence plates, not for typical tagging practice (`highway=track`, unpaved surfaces, `maxspeed` conventions, roadside furniture).
Ivanovo Oblast is also a region where OSM completeness varies sharply between the oblast capital and rural districts; profile design must
degrade gracefully.

**5. A buildable bridge / tunnel / embankment generator.** OSM carries `bridge`, `tunnel`, `layer`, `embankment` and `cutting`, and BeamNG's
Road Architect has a first-class **bridge** primitive (two nodes, solid mesh, collision-mesh recomputation) plus tunnel support. No existing tool
converts OSM bridge/tunnel semantics into BeamNG bridges or terrain cuts. This is essential for a region with the Volga and its tributaries
crossed by the M7 and other routes — a map where roads cross rivers without bridges is unusable.

**6. Water: rivers, lakes and DEM water flattening.** Copernicus DEM GLO-30 is a **DSM with water bodies already flattened and rivers made to
flow consistently** (per the collection page), but the *geometry* of the water — river channels and lake polygons — must come from OSM and be
cut into the terrain. BeamNG has a [River Editor](https://documentation.beamng.com/world_editor/tools/river_editor/) and
[Level Creation](https://docs.beamng.com/modding/levels/level_creation/section2/) covers water. No tool exports OSM water to BeamNG rivers
or carves terrain around them.

**7. A provenance and attribution generator.** No tool in the ecosystem emits a machine-readable per-asset provenance manifest, and none
generates the in-game attribution page that ODbL §4.3/§4.6 and CC-BY 4.0 require. Because our inputs are heterogeneous (OSM ODbL, Copernicus
with a verbatim notice, WorldCover CC-BY, SRTM public domain, terrain tiles with per-source terms), we need first-class provenance tracking:
every tile, mesh and material records which source produced it. This is both a legal requirement and the mechanism that lets us swap DEM sources
when access changes (§10.2).

**8. A `.ter` writer and level-file writer, implemented from the specification.** The official format page documents version 9 precisely
(`u8 version`, `u32 size`, `u16[size²] heightMap`, `u8[size²] layerMap`, `u32 materialCount`, material name strings; little-endian;
`heightMeters = storedHeight × maxHeight / 65536`; layer `255` = hole; ≤ 254 materials). Nobody has published a clean, licensed, maintained
writer. We should write it ourselves and use `blender-ng`'s `ter_parser.py` (Unlicense) as an independent cross-check. Same for
`items.level.json`, `.terrain.json`, `main.materials.json`, `groundmodels.json`, `signals.json` and the navigation `map.json`.

**9. Headless validation and CI.** There is no way to know whether a generated 220 × 145 km level is *valid* without launching the game — and
launching it for a region build is slow and fragile. We need (a) a fast static validator for `.ter`/JSON structure, power-of-two and range
constraints, orphaned material references and naming collisions against base-game names; and (b) an optional `BeamNGpy` (MIT) smoke test that
loads the level, spawns a vehicle on a known road and asserts it does not fall through. Nothing like this exists for BeamNG today.

**10. DEM acquisition that survives access changes.** Copernicus DEM GLO-30 access was tightened on **28 July 2026** (CCM registration
required for the view service), and the AWS bucket's status on 2026-10-07 is **unverified**. Our fetcher must be credential-aware, must not
assume `s3://copernicus-dem-30m` is anonymous, and must support a documented fallback chain (Copernicus GLO-30 → OpenTopography →
AWS Terrain Tiles with per-source attribution and refusal of non-redistributable source tiles → SRTM), with checksum pinning at every hop so a
build can be reproduced later even if an endpoint disappears.

**11. Building massing for a region where footprint data is thin.** Microsoft Building Footprints and Google Open Buildings have
**unverified and probably poor** coverage of Ivanovo Oblast; Overture (ODbL) is the best hope but its Russian coverage is **unverified**.
`earth2road` gates exactly this behind a `building_enrichment.enabled` flag and ships neither by default. We need a measured decision: which
footprint source, what coverage percentage, and a **clearly-labelled procedural fallback** for gaps — with the labelling being a licensing and
honesty requirement, not a nicety (we must not imply OSM data where there is none; OSMF asks that we *"only attribute OpenStreetMap for data
which comes from our database"*).

**12. A world-origin and floating-point strategy.** BeamNG uses single-precision floats and a fixed terrain origin. Project Virginia's author
warns of *"visual bugs and floating point errors"* at 337 750 km²; we are at 21 400 km², roughly a 16× reduction, but 220 km of extent is still
far beyond typical modulus. We must choose a world origin, tile-local coordinate frames, and a conversion layer, and verify jitter empirically
in-game rather than assuming it away.

---

## Appendix A — Category-by-category verdict

| # | Category | Verdict |
|---|---|---|
| 1 | GIS → BeamNG.drive tools | **Productive.** 8 projects; 3 permissively licensed (`earth2road` MIT, `…-v3` Apache-2.0, `BeamNGTerrainGenerator` MIT), 3 unlicensed, 1 CC-BY-4.0, 1 proprietary (BeamNG.tech importer). Two forks justified. |
| 2 | OSM → BeamNG / → Torque3D / GIS → Torque3D | **Empty for Torque3D — nothing usable found.** Three dead repositories. OSM→BeamNG is essentially unexploited: a GitHub search for `beamng osm` returns **2** repositories in total. |
| 3 | Road import / spline road generators | **One reusable asset** (`blender-ng`, Unlicense). The built-in Road Architect's JSON schema and road-class taxonomy are reference material; its scriptability is undocumented and its OpenDRIVE path is BeamNG.tech-only. The `MeshRoad`-vs-`DecalRoad` collision question is open. |
| 4 | Large-map and real-world 1:1 BeamNG projects | **No open pipeline exists — only closed mods.** Project Virginia (337 750 km², 1:1) is the key existence proof and the key source of failure modes. Its own numbers suggest our 21 400 km² target is tractable but unsupported. |
| 5 | Terrain generators / heightmap importers for BeamNG | **Essentially empty.** Four projects touch it; three are unusable (unlicensed ×2, MIT-but-proprietary-imagery ×1) and one is Unlicense (`blender-ng`). The authoritative format documentation is now the better source than any tool. |
| 6 | OSM road meshing | **Productive.** `OSM2World` (MIT), `osm2streets` (Apache-2.0), `osmnx` (MIT), SUMO (EPL-2.0), `arnis` (MIT), plus CC0 style data. Also three GPL projects usable only as reference (`osm2xp`, `osm2city`, `osm2pgsql`). |
| 7 | Procedural city / building generators | **Nothing directly usable.** `MapGenerator` LGPL-3.0, CityEngine proprietary, `blosm` GPL + paid tier. Value is conceptual (footprint/rule separation) plus the ODbL/CDLA footprint datasets. |
| 8 | BlenderGIS and Blender GIS → 3D pipelines | **One strong fork** (`blender-ng`, Unlicense). `BlenderGIS` is GPL-3.0 → reference only. Blender itself is usable **as a subprocess only**, which is the key GPL boundary to remember. |
| 9 | QGIS → BeamNG / DEM → BeamNG | **QGIS → BeamNG is empty.** No plugin exists. The nearest analogue is a GPL-3.0 QGIS→Unity heightmap exporter. DEM → engine-heightmap yields one strong algorithm (`UE4HmapsGenerator`, BSD-3-Clause) and one encoding trick (`GeoTIFF2Heightmap`, MIT). |
| 10 | GDAL / rasterio heightmap pipelines | **Productive and low-risk.** GDAL (MIT/X) and `rasterio` (BSD-3-Clause) are the right dependencies; `tin-terrain` (MIT) and `HeightMapTo3dTerrain` (MIT) supply decimation algorithms. |

## Appendix B — GPL/AGPL exclusion list (do not copy code from these)

| Project | Licence | URL |
|---|---|---|
| BlenderGIS | GPL-3.0 | https://github.com/domlysz/BlenderGIS |
| blosm (source) | GPL | https://github.com/vvoovv/blosm |
| osm2xp | GPL-3.0 | https://github.com/32kda/osm2xp |
| osm2city | GPL-2.0 (**unverified**) | https://wiki.flightgear.org/OSM2City |
| osm2pgsql | GPL-2.0 | https://github.com/osm2pgsql-dev/osm2pgsql |
| open-elevation | GPL-2.0 | https://github.com/Jorl17/open-elevation |
| unity-terrain-exporter (QGIS) | GPL-3.0 | https://github.com/liviaruegger/unity-terrain-exporter |
| MapGenerator | LGPL-3.0 | https://github.com/ProbableTrain/MapGenerator |
| OSM2Worldtemp (fork — use upstream `tordanik/OSM2World` instead) | LGPL-3.0 | https://github.com/spinachpasta/OSM2Worldtemp |
| SUMO (`netconvert`) | EPL-2.0 **or** GPL-2.0-or-later | https://github.com/eclipse-sumo/sumo — **EPL-2.0 path is fine as an external tool; do not copy source** |

## Appendix C — Unlicensed repositories (all rights reserved, do not use)

`alexkleinwaechter/BeamNG_LevelCleanUp` · `emlodnaor/GeoTiff2BeamNG` · `jonesyhunting/beamng-map-import-assistant` ·
`NiharikaPatil/Automated_Generation_of_Driving_Simulation_Scenarios_using_OSM_and_BeamNG-MasterArbeit` ·
`happysmaran/OSM2Mesh` · `whztt07/Osm3DBuildingGenerator` · `FelixMiddelhoff/contour` ·
`vvoovv/blosm` (source distributed only to customers) · `bobberdolle1/BeamNG.WorldForge`'s **runtime data dependencies**
(the code itself is MIT, but Google Earth Engine is proprietary and licence-gated).

## Appendix D — Items marked unverified in this review

Recorded honestly so that no downstream reader mistakes them for settled facts.

1. Whether `s3://copernicus-dem-30m` remains open/anonymous-read as of 2026-10-07. No authoritative shut-off notice located.
2. Whether the ESA Copernicus DEM User Licence imposes additional restrictions on redistributing **derived** products (the User Licence PDF was not read).
3. Whether BeamNG.drive 0.39.x supports multiple `TerrainBlock` objects per level with correct collision, seams and streaming.
4. Whether `DecalRoad` or `MeshRoad` is the correct primitive for a drivable *generated* road network.
5. The on-disk location and machine-readable schema of the shipped `roadArchitect` profile JSONs, and whether the Road Architect exposes a callable Lua API to custom editor extensions.
6. Whether the Road Architect session/profile JSON can be written and consumed without opening the editor (i.e. a true headless import path).
7. Licences of several small repositories where the GitHub API was rate-limited before confirmation: `EnriqueFuster/geo2blender`, `shanto462/HeightMapTo3dTerrain`, `Babamal/DEM2UE`, `Mukller/arnis`, `heremaps/tin-terrain`, `Rodrigo-NH/UE4HmapsGenerator`, `KingStu90/Lidar-to-Heightmap-Beginner-Tutorial`, `nikkiluzader/BeamNGTerrainGenerator`. For the ones where a SPDX id *was* returned it is reported above as confirmed; the remainder are marked in-line.
8. `osm2city`'s licence and last-commit date (not on GitHub at the expected path; GPL-2.0 is per the FlightGear wiki only).
9. `osm2pov` — not located; both `mapillary/osm2pov` and `plepe/osm2pov` return HTTP 404.
10. Which AWS Terrain Tiles source DEM (EU-DEM vs GMTED2010/SRTM) covers Ivanovo Oblast.
11. Coverage of Ivanovo Oblast in Microsoft Building Footprints, Overture Maps buildings, and Google Open Buildings.
12. Availability of LiDAR coverage for Ivanovo Oblast.
13. Exact clause numbers in the current Yandex Maps API legal terms (the game-project ban is established from Yandex's own API-club posts, not the licence text).
14. ESA WorldCover's prescribed verbatim attribution string (CC-BY 4.0 requirements were applied; no ESA-published wording was found).
15. Whether the unauthenticated GitHub search results for categories 6–10 are complete — several bulk searches were terminated by `HTTP 403: rate limit exceeded`.

## Appendix E — Search queries that produced nothing

Recorded so the negative result is auditable.

* GitHub repository searches returning **0 results**: `beamng terrain heightmap`, `beamng gis` (1 result, not a QGIS plugin).
* GitHub repository searches returning only dead or irrelevant repositories: `torque3d terrain` (3 results, all dormant ≥ 2015), `beamng osm` (2 results).
* `osm2pov` — no repository found under either plausible owner.
* **No QGIS plugin targeting BeamNG.drive was found.** The only QGIS → game-engine heightmap exporter located is GPL-3.0 and targets Unity.
* **No OSM → Torque3D or GIS → Torque3D tool was found at all.**
* **No open-source, licensed, scriptable heightmap importer for BeamNG.drive was found.**
