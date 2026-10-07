# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions are assigned by what actually works, not by schedule. A version number
that claims a capability the project does not have would be worse than no
release at all.

## [Unreleased]

### Added
- Buildings, vegetation, water and rail generation (milestone M3).
- Ivanovo → Shuya intercity corridor (milestone M4).
- Sectors used at runtime for streaming, once engine behaviour is measured.

## [0.1.0] — 2026-10-07

First working end-to-end pipeline. **Not yet verified inside BeamNG.drive** —
see `BEAMNG_TEST` in the README.

### Added

**Coordinate system**
- A custom Transverse Mercator centred on Ivanovo Oblast, with an
  automatically derived scale factor `k0 = 1 − W²/4` that balances distortion
  between the central meridian and the region edge.
- `geo_to_world` / `world_to_geo` with exact inverses and vectorised variants.
- Measured accuracy: 0.0106 % worst case; Ivanovo → Shuya projects to
  29.138 km against a 29.141 km WGS84 geodesic.

**BeamNG interchange**
- `.ter` terrain codec for format version 9, derived by byte-accounting real
  files and verified by re-encoding six terrains shipped with the game and
  requiring byte-identical output.
- Level scene writer producing `info.json`, `map.json` and the
  `main/**/items.level.json` scene tree with deterministic object UUIDs.
- Terrain material library covering 12 surfaces, referencing 40 base-game
  shared textures by path so that no game asset is copied.
- DecalRoad export with per-node widths.

**Geodata**
- Tiled Overpass acquisition with mirror failover, content-addressed caching and
  a wall-clock budget, merged by element id so a way reported by several tiles
  appears once with full geometry.
- Streaming OSM XML parser (13 MB peak versus 54 MB for a DOM parse on a
  60,000-node document).
- DEM acquisition from Copernicus GLO-30 with automatic fallback to AWS Terrain
  Tiles and SRTM GL1, each verified against live endpoints.
- Road extraction with real OSM tag precedence over class defaults.

**Terrain generation**
- DEM sampling onto the world grid, row by row, so a 4096² grid does not need
  16.7 million points in memory at once.
- Road corridor grading: centreline profiles are resampled, smoothed over
  ~120 m and clamped to a maximum grade, then blended into the heightmap so the
  carriageway is flat and the terrain returns smoothly across the shoulder.

**Tooling**
- `ivanovo2beam` CLI: `info`, `download`, `build`, `package`, `validate`,
  `install`.
- Structural level validator checking required files, scene-tree parent
  resolution, `.ter`/`.terrain.json` agreement and asset-path resolution.
- 550+ tests; CI runs lint, the offline suite on Python 3.12 and 3.13, a
  complete synthetic end-to-end build, and config sanity checks.

**First generated level**
- `ivanovo_mvp`: 5.118 km square of central Ivanovo. 4096² terrain at 1.25 m
  (elevations 105.0–153.4 m from real Copernicus DEM), 5,688 roads totalling
  638 km, 19 bridges, 37 tunnels, 34,599 DecalRoad nodes.

### Fixed

Bugs found and corrected during development, each caught by a test rather than
by inspection:

- Road corridor grading painted the carriageway as *shoulder* wherever a cell
  lay beyond the endpoints of the nearest segment, because painting happened
  per segment instead of from the accumulated influence field.
- The material-ownership test compared a running maximum against itself after
  updating it, so no surface was ever painted.
- `TerrainMaterialSpec.all_asset_paths` merged base and macro texture mappings
  by channel key, which silently discarded every base texture.
- The DEM gap filler was quadratic and attempted a 3.5 TiB allocation once
  unfilled cells exceeded a few thousand; replaced with a linear morphological
  fill.
- `SceneTree.render` never emitted the root group's own `items.level.json`,
  leaving every top-level group with a dangling parent — a level BeamNG would
  not have loaded correctly.
- `SectorId` parsing rejected negative indices because the sign consumed one
  character of the zero-padded field.

### Known issues

- **The level has not been loaded in BeamNG.drive.** The development
  environment cannot write to BeamNG's user data folder and Steam was not
  running. Status is reported as `BEAMNG_TEST=NOT_TESTED`.
- `tunnel=*` ways are graded as surface roads.
- Buildings, vegetation, water and rail are not implemented.
