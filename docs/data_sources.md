# Data sources, licences and attribution

Generated output is only as publishable as the data behind it. This document
records every dataset the pipeline touches, the licence it carries, what that
licence requires of us, and what we may **not** use.

The project's own code is MIT. **That licence does not extend to the geodata it
processes**, and nothing here attempts to relicense it.

---

## Datasets in use

### OpenStreetMap — roads (and, later, buildings, water, land use)

| | |
|---|---|
| Source | <https://www.openstreetmap.org> |
| Access | Overpass API, in tiled requests; `Geofabrik` PBF for regional builds |
| Licence | **ODbL 1.0** (Open Database Licence) |
| Attribution | "© OpenStreetMap contributors" |
| Copyright | <https://www.openstreetmap.org/copyright> |

### Copernicus DEM GLO-30 — elevation (primary)

| | |
|---|---|
| Source | <https://dataspace.copernicus.eu/> |
| Access | Public AWS Open Data mirror, `s3://copernicus-dem-30m`, anonymous |
| Licence | **Copernicus DEM Instrument Licence** — free and open, commercial use permitted |
| Attribution | "© ESA / European Union, Copernicus Programme" |
| Resolution | 30 m (1 arc-second N–S, 1.5 arc-second E–W), 1°×1° COG tiles |

Verified working anonymously on 2026-10-07: HTTP 200, 32,103,651 bytes for the
`N56 E041` tile, from both `copernicus-dem-30m.s3.amazonaws.com` and the
`eu-central-1` endpoint. Tiles over open ocean genuinely return 404, which is
what triggers the fallback chain.

### AWS Terrain Tiles — elevation (fallback)

| | |
|---|---|
| Source | <https://registry.opendata.aws/terrain-tiles/> |
| Access | `https://s3.amazonaws.com/elevation-tiles-prod/geotiff/{z}/{x}/{y}.tif`, anonymous |
| Licence | mixed upstream (SRTM, GMTED, ETOPO1, national datasets); **attribution required** |
| Attribution | "Terrain Tiles, AWS Open Data" plus the upstream dataset credits |

### SRTM GL1 — elevation (fallback)

| | |
|---|---|
| Source | <https://lpdaac.usgs.gov/products/srtmgl1v003/> |
| Access | Skadi `.hgt.gz` mirrors, anonymous (verified: 4,341,833 bytes for `N57E041`) |
| Licence | **Public domain** (NASA/USGS) |
| Resolution | 30 m, land between 60 °N and 56 °S |

The three DEM sources agree over Ivanovo to within a few metres (means of 132.8,
128.4 and 128.4 m over the same bounding box), which is the cross-check that
makes the fallback chain trustworthy rather than merely functional.

### Not yet used

**ESA WorldCover 10 m** (CC BY 4.0) is the intended land-cover source for
milestone M3, to drive forest, field and built-up surfaces. Attribution:
"© ESA WorldCover project / contains modified Copernicus Sentinel data (2021)".

---

## Obligations we must satisfy

### ODbL and the "Produced Work" question

The relevant question is whether the shipped map is a *Derivative Database*
(share-alike applies) or a *Produced Work* (it does not).

ODbL 1.0 §4.5(b) states that producing a Produced Work from a Derivative
Database does not, in itself, create a Derivative Database. The OpenStreetMap
Foundation's board-endorsed guidance on Produced Works gives the operative test:
a result is a Produced Work rather than a database if it is **not intended for
the extraction of the original data**. A baked BeamNG level — a heightmap with
graded roads and, later, mesh geometry — clearly fails that test. There is no
practical way to recover street-level OSM data from it.

So the **shipped map artefact may be MIT-licensed**, and the project does not
attempt to impose share-alike on it.

**But §4.6 still bites.** Where we publish a Produced Work made from a Derivative
Database, we must offer recipients, free of charge, *either* the whole Derivative
Database *or* "the method of making the alterations to the Database (such as an
algorithm)". Our processed road graph, lane assignment and footprint tables are a
Derivative Database, so:

* **OSM-derived geodata stays ODbL.** Files under `build/debug/*.geojson`, any
  extracted road graph, and any sector geodata are ODbL and are never relicensed
  under MIT.
* **The repository ships the method.** The pipeline itself — pinned configs,
  download URLs, checksums and the deterministic generator — is the "method of
  making the alterations", which satisfies §4.6 without us having to redistribute
  a multi-gigabyte database. This is why reproducibility is a licensing
  requirement here and not just an engineering preference.

### Attribution in the product

ODbL §4.3 requires attribution. The OSMF guidance explicitly covers "Computer
games and simulations": a splash screen, an in-game view, a credits page or a
menu entry all qualify. Because a BeamNG map works offline, a URL alone is not
enough — the text has to travel with the map. The build therefore emits an
attribution manifest (`build/attribution.json`) and the level's `info.json`
carries the credit line, so the attribution ships inside the mod.

### BeamNG's own terms

BeamNG's EULA is stricter than modding communities often assume:

* It asserts ownership of the game's "locations, concepts, artwork" (§1).
* Integrating parts of the Software into another work is permitted only as the
  Modding Guidelines allow (§3.3); distributing copies is prohibited (§4.1) and
  so is separating component parts (§4.2).

Referencing base-game assets **by path** is fine, and is in fact the intended
workflow. Accordingly:

* **Zero BeamNG files are copied** into this repository or into any generated
  archive. `tests/test_build_offline.py::test_zip_contains_no_game_assets`
  asserts that the produced zip contains only `.json`, `.ter` and text.
* Generated terrain materials reference 40 of the game's shared terrain textures
  by path, and roads reference the shared decal materials by name.
* The `roadArchitect` cross-section profiles that ship with the game were read on
  a local installation **to learn the schema only**. They are game content and
  are neither committed nor shipped.
* The generated mod does not overwrite any default game data.

### Determinism as a licensing property

Because §4.6 can be satisfied by publishing the *method*, the method has to
actually reproduce the result. So:

* no unseeded randomness — procedural choices derive from the OSM element id;
* every object UUID is a deterministic function of stable keys;
* caches are content-addressed;
* `src/ivanovo2beam/beamng/materials.py` pins each material's `persistentId` as a
  UUIDv5 of its key.

`tests/test_build_offline.py::test_build_is_deterministic` asserts byte-identical
output across two independent builds.

---

## Prohibited sources

These may **not** appear anywhere in the pipeline, not even as a development
convenience that leaks into output. All three prohibit building derived mapping
datasets from their imagery:

| Source | Why it is prohibited |
|---|---|
| **Google Maps / Earth** | Additional ToS §2.1/§2.4 forbid creating a product based on Google Maps content or augmenting another mapping dataset with it |
| **Bing Maps** | End User Terms §3(b) forbid derivative works; §2(b) forbids caching; §4(o) forbids automated access |
| **Yandex Maps** | Terms and API-club guidance prohibit use in game projects |

This is a genuine loss for a Russian-region map, because Yandex coverage of
Ivanovo Oblast is excellent. It is also a hard line: using it would make the
project undistributable.

Where imagery is wanted for local visual comparison during development, it stays
in a gitignored `reference/` directory, is never read by the pipeline, and never
enters a build. There is currently no such directory.

**Textures** must be procedural or CC0. The project currently generates none of
its own: it references the game's shared textures by path, so there is nothing to
license.

---

## Attribution text to ship

```
Map data © OpenStreetMap contributors, ODbL 1.0 — https://www.openstreetmap.org/copyright
Elevation: © ESA / European Union, Copernicus Programme — Copernicus DEM GLO-30
Generated by ivanovo2beam — https://github.com/bobberdolle1/ivanovo2beam
```

The fallback sources add their own credit lines when used; the build records
which source actually delivered each dataset in the DEM sidecar and in
`build/attribution.json`, so attribution reflects what was really used rather
than what was requested.

---

## Per-asset provenance

Every generated artefact records the inputs it came from: source key, URLs
attempted, fetch timestamp, bounding box, byte size, SHA-256 of the payload, and
the licence and attribution strings. This is written as a sidecar next to each
cached download and summarised in `build/attribution.json`.

That single manifest serves three purposes at once: it satisfies ODbL §4.6, it
makes builds reproducible, and it lets a user answer "where did this number come
from?" without reading any code.
