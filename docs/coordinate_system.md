# Coordinate system

This document defines the one thing the whole project rests on: **how a point on
the Earth becomes a point in the game world, and why one metre in the game is
exactly one metre in reality.**

## The short version

```
WGS84 degrees (EPSG:4326)
        │  pyproj Transformer, always_xy
        ▼
Custom Transverse Mercator centred on Ivanovo Oblast  (metres)
        │  subtract the projected coordinate of Ivanovo city centre
        ▼
World plane:  x = metres east of Ivanovo,  y = metres north of Ivanovo
        │  BeamNG 0.39 already uses (x, y, z) = (east, north, up)
        ▼
BeamNG world coordinates (metres)
```

Implemented in [`src/ivanovo2beam/gis/projection.py`](../src/ivanovo2beam/gis/projection.py):

```python
x, y = projection.geo_to_world(lon, lat)  # metres
lon, lat = projection.world_to_geo(x, y)  # exact inverse
```

## Why degrees will not do

A degree of latitude is about 111.1 km everywhere, but a degree of longitude is
only about 60.6 km at Ivanovo's latitude. Feeding raw latitudes and longitudes
into a game engine therefore produces a map stretched by a factor of ~1.83 in
the east-west direction — the oblast would be nearly twice as wide as it is.

## Which projection, and why this one

Ivanovo Oblast spans roughly **39.0–43.2 E** and **56.1–57.8 N**: about 220 km
wide and 145 km tall. That extent straddles the official UTM zone boundary —
zone 37N covers 36–42 E, zone 38N covers 42–48 E — and the oblast's centre sits
at 41.1 E, a degree and a half *inside* zone 38N and three degrees *outside*
zone 37N.

The obvious choice, UTM 37N, would put the eastern third of the region almost
4° from its central meridian. Transverse Mercator's point scale factor grows
with distance from the central meridian, so UTM 37N would stretch the east of
the region by about **+0.032 % — roughly 32 m of error per 100 km driven**.

Instead the project uses a **custom Transverse Mercator centred on the oblast
itself**:

```
+proj=tmerc +lat_0=56.99987 +lon_0=41.1 +k=0.999895527
  +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs +type=crs
```

The central meridian `lon_0` is the region's own centre. The scale factor `k0`
is not unity: along the central meridian a Transverse Mercator is compressed by
`k0`, and at a distance it is stretched by approximately `k0·(1 + W²/2)`, where
`W` is the region's angular half-width measured at its **lowest** latitude (the
worst case, because `cos φ` is largest there). Choosing

```
k0 = 1 − W²/4        →     0.9998955 for this bounding box
```

balances the two: distances are compressed by `W²/4` in the middle of the
region and stretched by `W²/4` at its edges. This is computed at load time by
`optimal_k0()` and can be overridden in the config.

Transverse Mercator was preferred over an equal-area or conic projection because
it is **conformal** (preserves local shape and angles, so road geometry keeps
its real curvature) and keeps **north up with straight meridians**, which makes
the sector grid and the build deterministic and cheap to reason about.

### Measured accuracy

| Quantity | Value |
|---|---|
| Worst-case scale error over the bounding box | **0.0106 %** |
| Equivalent distance error | **10.65 m per 100 km** |
| Ivanovo → Shuya, projected | 29.138 km |
| Ivanovo → Shuya, WGS84 geodesic | 29.141 km |
| Error over that route | **3 m in 29 km** |
| Vertical quantisation (BeamNG `.ter` heights) | 0.9 mm at a 58 m range |

These are asserted by `tests/test_projection.py`, which compares against
`pyproj.Geod` geodesics rather than against the projection's own arithmetic, so
a self-consistent but wrong PROJ string cannot pass.

For scale: a car is 4.5 m long. Driving Ivanovo to Shuya, the game world is
about two-thirds of a car length short of reality.

## The world origin

The world origin is **Ivanovo city centre** (40.97393 E, 56.99987 N), so the
coordinates the player occupies are small: a few kilometres rather than a few
hundred. That matters because BeamNG stores world positions as 32-bit floats.

Float32 has a 24-bit mantissa, so the representable step at a distance `d` from
the origin is about `2^ceil(log2 d) / 2^24`:

| Distance from origin | Float32 step (ULP) |
|---|---|
| 1 km | 0.06 mm |
| 10 km | 0.98 mm |
| 35 km (Shuya) | 3.9 mm |
| 100 km (Kineshma) | 7.8 mm |
| 250 km | 15.6 mm |

BeamNG 0.39 has **no floating origin** — a byte-level scan of the executable
found no `floatingOrigin`, `worldOrigin` or `setWorldOrigin` symbol — so keeping
the origin inside the region is the only lever available. See
[`docs/research/beamng_engine_limits.md`](research/beamng_engine_limits.md) and
the floating-point experiment milestone for the measured consequences.

## Axis convention

BeamNG 0.39 uses **(x, y, z) = (east, north, up)** in metres. This was
established from the shipped game, two independent ways:

1. `core_terrain.getTerrainHeight` is called as
   `newPos.z = core_terrain.getTerrainHeight(newPos)` — the returned height is
   written to `z`.
2. Utah's DecalRoads carry `z` values around 293 m, inside that level's terrain
   height range of 100–300 m, while their `y` values are ground-plane
   coordinates around −37.

The project's internal world frame is also (east, north, up), so positions map
across **unchanged** — there is no axis swap anywhere in the exporter. This is a
common source of mirrored maps, so it is stated explicitly and tested.

### Heightmap row order

Inside a `.ter` file, the grid is indexed `[row, col]` and

```
x = origin_x + col * squareSize        (east)
y = origin_y + row * squareSize        (north)
```

so **row 0 is the southern edge** and no vertical flip is applied. This was
confirmed against the shipped `template` level: its spawn sphere sits at
y = 70.16 m, and sampling the terrain at `row ≈ 583` (not `row ≈ 441`, the
mirrored position) gives 135.255 m — 0.44 m below the sphere's z of 135.699 m,
exactly as a spawn marker should sit.

## Terrain grid arithmetic

A BeamNG `TerrainBlock` is a **vertex** grid, so an `n`-by-`n` grid at
`squareSize` metres spans

```
extent = (n − 1) × squareSize
```

not `n × squareSize`. This off-by-one was verified against shipped levels:

| Level | `terrainSize` | `squareSize` | `position` | `(n−1)·s / 2` |
|---|---|---|---|---|
| automation_test_track | 4096 | 0.5 | −1023.75 | 1023.75 |
| hirochi_raceway | 2048 | 0.75 | −767.625 | 767.625 |
| gridmap_v2 | 2048 | 1.0 | −1023.5 | 1023.5 |

The shipped `info.json` `size` field is *not* the terrain extent — the template
level declares `size: [2048, 2048]` with a 1023 m terrain — so it is treated as
nominal UI metadata only.

## Vertical encoding

BeamNG stores terrain heights as unsigned 16-bit values mapped onto
`[0, maxHeight]` above the block's `position.z`:

```
elevation_m = position.z + stored × (maxHeight / 65536)
```

Note the divisor is **65536, not 65535**. The vertical range is chosen from the
DEM with a margin (`TerrainFile.fit_vertical_range`), so real elevations are
never clipped. For the Ivanovo MVP — 105.0 to 153.4 m — the range is 58.4 m and
the quantisation step is **0.89 mm**, far below the ~1 m vertical accuracy of
Copernicus DEM itself.

## Reproducibility

The projection is fully specified by the PROJ string in the region config, which
`ivanovo2beam info` prints. No projection parameter is hard-coded in a module, so
mapping a different region is a config change rather than a code change —
see `configs/ivanovo_oblast.yaml`.

## Tests

`tests/test_projection.py` (37 tests) covers:

* `geo → world → geo` and `world → geo → world` round trips to 1e-9 degrees
* projected distance versus independent WGS84 geodesics for six real routes
* the worst-case scale error over the whole bounding box
* that the chosen `k0` beats both `k0 = 1` and UTM 37N
* azimuth independence of the scale factor (conformality)
* rejection of geographic CRSs and non-metric units
* vectorised transforms agreeing with scalar ones
