"""End-to-end level build: geodata in, installable BeamNG level out.

This module is the spine of the pipeline. Each stage is cached on disk, so a
re-run after a failure or a tuning change only redoes the work that actually
changed::

    DEM  ──fetch_dem──► .cache/dem/<key>.tif
    OSM  ──fetch_overpass (tiled)──► .cache/osm/<key>.osm
         ──parse + merge + project──► roads in world metres
    DEM + roads ──build_terrain──► heightmap with graded road corridors
         ──scene assembly──► levels/<id>/{info.json, main/**, *.ter, art/**}
         ──package──► build/<id>.zip

Everything written is deterministic: regenerating an unchanged level produces
byte-identical files, because every object UUID is derived from stable keys and
no stage uses an unseeded random number generator.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from ivanovo2beam.beamng.level import (
    SceneTree,
    TerrainPlacement,
    add_spawn_point,
    build_info_json,
    build_sun_and_sky,
    write_level_files,
)
from ivanovo2beam.beamng.materials import (
    DEFAULT_MATERIAL_ORDER,
    build_materials_json,
    referenced_asset_paths,
)
from ivanovo2beam.beamng.ter import write_ter
from ivanovo2beam.osm.merge import merge_osm_data, osm_bbox_tiles
from ivanovo2beam.roads.build import add_network_to_scene, roads_to_corridors
from ivanovo2beam.roads.network import extract_roads, road_network_stats
from ivanovo2beam.terrain.builder import build_terrain

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from ivanovo2beam.config import ProjectConfig

#: ``binaryFormat`` string the game writes into ``<terrain>.terrain.json``.
#: Reproduced verbatim for compatibility. It is descriptive legacy text: the
#: ``layerTextureMap`` it advertises does not exist in the v9 payload, which was
#: proven by byte-accounting seven shipped terrains.
_TERRAIN_BINARY_FORMAT = (
    "version(char), size(unsigned int), heightMap(heightMapSize * heightMapItemSize), "
    "layerMap(layerMapSize * layerMapItemSize), "
    "layerTextureMap(layerMapSize * layerMapItemSize), materialNames"
)

#: How far outside the terrain a road is still rendered, in metres. Roads
#: leaving the mapped area should visibly continue rather than stop dead at the
#: boundary, but must not drag corridors to the horizon.
ROAD_RENDER_MARGIN_M = 250.0

ProgressFn = "Callable[[float, str], None]"


@dataclass(slots=True)
class BuildResult:
    """What a build produced.

    Attributes:
        level_id: BeamNG level identifier.
        level_dir: The built level directory, i.e. ``build/levels/<id>``.
        mod_zip: Packaged mod archive, when packaging was requested.
        stats: Stage-by-stage diagnostics.
        durations: Wall-clock seconds per stage.
        warnings: Non-fatal problems worth surfacing to the user.
    """

    level_id: str
    level_dir: Path
    mod_zip: Path | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Return a multi-line human-readable build report."""
        lines = [f"level {self.level_id}", f"  output: {self.level_dir}"]
        if self.mod_zip:
            lines.append(f"  mod zip: {self.mod_zip} ({self.mod_zip.stat().st_size / 1e6:.1f} MB)")
        for stage, seconds in self.durations.items():
            lines.append(f"  {stage:<12} {seconds:6.1f}s")
        stats = self.stats
        if "terrain" in stats:
            t = stats["terrain"]
            lines.append(
                f"  terrain     {t['grid_size']}x{t['grid_size']} @ {t['cell_size_m']} m "
                f"= {t['extent_m'] / 1000:.2f} km, elevations {t['dem_min_m']:.1f}..{t['dem_max_m']:.1f} m"
            )
        if "roads" in stats:
            r = stats["roads"]
            lines.append(
                f"  roads       {r['roads']} ways, {r['total_length_km']} km "
                f"({r.get('bridges', 0)} bridges, {r.get('tunnels', 0)} tunnels)"
            )
        if "export" in stats:
            e = stats["export"]
            lines.append(f"  decals      {e['exported']} DecalRoads, {e['nodes']} nodes")
        for warning in self.warnings:
            lines.append(f"  WARNING: {warning}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Small file writers
# ---------------------------------------------------------------------------


def build_terrain_json(
    level_id: str,
    terrain_name: str,
    *,
    size: int,
    materials: Sequence[str],
) -> dict[str, Any]:
    """Build the ``<terrain>.terrain.json`` terrain descriptor.

    Args:
        level_id: Level identifier.
        terrain_name: Terrain object/file stem, e.g. ``"theTerrain"``.
        size: Vertices per side.
        materials: Material names, in layer-map index order.

    Returns:
        The descriptor contents.
    """
    base = f"/levels/{level_id}/{terrain_name}"
    cells = size * size
    return {
        "binaryFormat": _TERRAIN_BINARY_FORMAT,
        "datafile": f"{base}.ter",
        "heightMapItemSize": 2,
        "heightMapSize": cells,
        "heightmapImage": f"{base}.terrainheightmap.png",
        "layerMapItemSize": 1,
        "layerMapSize": cells,
        "materials": list(materials),
        "size": size,
        "version": 9,
    }


def write_json(path: Path, payload: Any) -> Path:
    """Write JSON with stable formatting.

    Args:
        path: Destination; parents are created.
        payload: JSON-serialisable content.

    Returns:
        The path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Geodata stages
# ---------------------------------------------------------------------------


def _level_geographic_bbox(
    config: ProjectConfig,
    margin_deg: float = 0.0,
) -> tuple[Any, tuple[float, float], float]:
    """Return the level's geographic bbox, terrain origin and extent.

    Args:
        config: Project configuration with a ``level`` block.
        margin_deg: Optional padding in degrees.

    Returns:
        ``(bbox, (origin_x, origin_y), extent_m)``.

    Raises:
        ValueError: If no level is configured.
    """
    from ivanovo2beam.gis.projection import BBox

    level = config.level
    if level is None:
        raise ValueError("config has no 'level' block; nothing to build")
    origin_x, origin_y = config.level_terrain_origin(level)
    extent = level.grid.extent_m
    corners_x = [origin_x, origin_x + extent]
    corners_y = [origin_y, origin_y + extent]
    lons, lats = config.projection.world_to_geo_array(
        [corners_x[0], corners_x[1], corners_x[0], corners_x[1]],
        [corners_y[0], corners_y[0], corners_y[1], corners_y[1]],
    )
    bbox = BBox(
        west=float(lons.min()) - margin_deg,
        south=float(lats.min()) - margin_deg,
        east=float(lons.max()) + margin_deg,
        north=float(lats.max()) + margin_deg,
    )
    return bbox, (origin_x, origin_y), extent


def fetch_level_osm(
    config: ProjectConfig,
    bbox: Any,
    *,
    tile_km: float = 1.5,
    budget_s: float = 240.0,
    progress: Callable[[float, str], None] | None = None,
    force: bool = False,
) -> tuple[Any, dict[str, Any]]:
    """Fetch and merge OSM data for a level, tile by tile.

    Overpass cannot serve a full-feature extract for a 5 x 5 km city area in one
    request (it times out), but a 1.5 km tile returns in well under a second. The
    area is therefore requested as a grid of tiles and merged.

    A **time budget** is enforced. Overpass is a donated service that degrades
    under load, and its mirror failover uses exponential backoff of up to a
    minute per attempt; without a budget a run against a struggling mirror can
    hang for an hour. When the budget expires the build continues with the tiles
    that succeeded and reports the gap, which is far more useful than blocking.

    Args:
        config: Project configuration.
        bbox: Geographic bounding box to fetch.
        tile_km: Tile edge length in kilometres.
        budget_s: Wall-clock seconds to spend fetching before giving up on the
            remaining tiles. Cached tiles cost nothing and do not count.
        progress: Optional progress callback.
        force: Ignore the cache and re-download.

    Returns:
        ``(osm_data, stats)``.
    """
    from ivanovo2beam.osm import fetch_overpass, parse_osm

    tiles = osm_bbox_tiles(bbox.west, bbox.south, bbox.east, bbox.north, tile_km=tile_km)
    cache_dir = config.cache_path("osm")
    extracts = []
    failures: list[str] = []
    skipped: list[str] = []
    started = time.perf_counter()
    timed_out = False

    for i, (west, south, east, north) in enumerate(tiles):
        if progress is not None:
            progress(0.05 + 0.25 * (i / max(1, len(tiles))), f"OSM tile {i + 1}/{len(tiles)}")
        if timed_out:
            skipped.append(f"tile {i + 1}")
            continue
        from ivanovo2beam.gis.projection import BBox

        tile_bbox = BBox(west=west, south=south, east=east, north=north)
        try:
            path = fetch_overpass(tile_bbox, cache_dir, force=force)
            extracts.append(parse_osm(path))
        except Exception as exc:  # noqa: BLE001 - a failed tile must not kill the build
            failures.append(f"tile {i + 1}/{len(tiles)} ({west:.4f},{south:.4f}): {exc}")
        if time.perf_counter() - started > budget_s and i + 1 < len(tiles):
            timed_out = True

    if not extracts:
        raise RuntimeError(
            "every OSM tile failed; the Overpass API may be rate limiting. "
            f"First failure: {failures[0] if failures else 'unknown'}"
        )

    merged, report = merge_osm_data(extracts)
    stats = {
        "tiles_requested": len(tiles),
        "tiles_succeeded": len(extracts),
        "tiles_failed": len(failures),
        "tiles_skipped": len(skipped),
        "budget_exhausted": timed_out,
        "fetch_seconds": round(time.perf_counter() - started, 1),
        "merge": report.as_dict(),
        "merge_summary": report.describe(),
    }
    return merged, stats


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def package_level(build_dir: Path, level_id: str, output_zip: Path | None = None) -> Path:
    """Package a built level into a BeamNG mod zip.

    A BeamNG map archive only needs ``levels/<id>/...`` at its root. Entries are
    written in sorted order with a fixed timestamp so two builds of the same
    level produce byte-identical archives.

    Args:
        build_dir: Directory containing ``levels/<id>/``.
        level_id: Level identifier.
        output_zip: Destination archive. Defaults to ``build_dir/<id>.zip``.

    Returns:
        The archive path.

    Raises:
        FileNotFoundError: If the level directory does not exist.
    """
    level_root = build_dir / "levels" / level_id
    if not level_root.is_dir():
        raise FileNotFoundError(f"no built level at {level_root}")
    target = output_zip or (build_dir / f"{level_id}.zip")
    target.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in level_root.rglob("*") if p.is_file())
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            relative = path.relative_to(level_root).as_posix()
            info = zipfile.ZipInfo(filename=f"levels/{level_id}/{relative}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return target


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def build_level(
    config: ProjectConfig,
    *,
    progress: Callable[[float, str], None] | None = None,
    make_zip: bool = True,
    osm_tile_km: float = 1.5,
    osm_budget_s: float = 240.0,
    max_decal_node_spacing_m: float = 40.0,
    force_download: bool = False,
) -> BuildResult:
    """Build a complete BeamNG level from open geodata.

    Args:
        config: Project configuration including a ``level`` block.
        progress: Optional ``progress(fraction, message)`` callback.
        make_zip: Whether to package the result as a mod zip.
        osm_tile_km: OSM fetch tile size in kilometres.
        osm_budget_s: Seconds to spend on OSM acquisition before continuing
            with whatever tiles succeeded.
        max_decal_node_spacing_m: Maximum DecalRoad node spacing. Larger values
            mean far fewer nodes; the default keeps roads glued to the terrain
            on the gentle gradients of the Russian plain.
        force_download: Ignore caches and re-download geodata.

    Returns:
        A :class:`BuildResult` describing what was produced.

    Raises:
        ValueError: If the configuration has no level to build.
        RuntimeError: If a required data source is unavailable.
    """
    from ivanovo2beam.terrain.dem import DemReader, fetch_dem

    start = time.perf_counter()
    level = config.level
    if level is None:
        raise ValueError("config has no 'level' block; nothing to build")

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(fraction, message)

    durations: dict[str, float] = {}
    warnings: list[str] = []

    bbox, (origin_x, origin_y), extent = _level_geographic_bbox(config, margin_deg=0.004)
    report(0.01, f"level {level.id}: {extent / 1000:.2f} km terrain at {origin_x:.0f},{origin_y:.0f}")

    # -- DEM ---------------------------------------------------------------
    stage = time.perf_counter()
    report(0.02, "acquiring DEM")
    dem_path = fetch_dem(
        bbox,
        config.cache_path("dem"),
        source=config.terrain_source,
        resolution_m=config.terrain_resolution_m,
        force=force_download,
    )
    durations["dem"] = time.perf_counter() - stage

    # -- OSM ---------------------------------------------------------------
    stage = time.perf_counter()
    report(0.05, "acquiring OSM")
    osm_data, osm_stats = fetch_level_osm(
        config,
        bbox,
        tile_km=osm_tile_km,
        budget_s=osm_budget_s,
        progress=progress,
        force=force_download,
    )
    durations["osm"] = time.perf_counter() - stage
    if osm_stats["tiles_failed"]:
        warnings.append(
            f"{osm_stats['tiles_failed']} of {osm_stats['tiles_requested']} OSM tiles failed; "
            "the road network has gaps there"
        )
    if osm_stats.get("budget_exhausted"):
        warnings.append(
            f"OSM fetch hit its {osm_budget_s:.0f} s budget after "
            f"{osm_stats['tiles_succeeded']} tiles; {osm_stats['tiles_skipped']} tiles were "
            "skipped, so the road network is incomplete. Re-run to pick up the cached tiles "
            "and fetch more."
        )

    # -- roads -------------------------------------------------------------
    stage = time.perf_counter()
    report(0.33, "projecting and classifying roads")
    margin = ROAD_RENDER_MARGIN_M
    clip_bounds = (origin_x - margin, origin_y - margin, origin_x + extent + margin, origin_y + extent + margin)
    roads = extract_roads(osm_data, config.projection, config.roads, clip_bounds=clip_bounds)
    road_stats = road_network_stats(roads)
    durations["roads"] = time.perf_counter() - stage
    if not roads:
        warnings.append("no drivable roads were found in the OSM extract")

    # -- terrain -----------------------------------------------------------
    stage = time.perf_counter()
    report(0.38, "building terrain and grading road corridors")
    with DemReader(dem_path) as dem:
        corridors = roads_to_corridors(roads, config.roads)
        build = build_terrain(
            dem,
            config.projection,
            origin_x=origin_x,
            origin_y=origin_y,
            grid_size=level.grid.grid_size,
            cell_size_m=level.grid.square_size_m,
            corridors=corridors,
            material_order=DEFAULT_MATERIAL_ORDER,
            ground_margin_m=level.ground_margin_m,
            max_height_m=level.max_height_m,
            max_grade=config.roads.max_grade,
            progress=lambda f, m: report(0.38 + 0.34 * f, m),
        )
    durations["terrain"] = time.perf_counter() - stage
    if build.graded_cells == 0 and roads:
        warnings.append("no road corridors were graded into the terrain")

    # -- scene -------------------------------------------------------------
    stage = time.perf_counter()
    report(0.74, "assembling the level scene")
    tree = SceneTree(level_id=level.id)
    ground_z = float(np.median(build.elevations))
    terrain_placement = TerrainPlacement(
        name="theTerrain",
        origin_x=origin_x,
        origin_y=origin_y,
        base_z=build.base_z,
        max_height=build.max_height,
        square_size=level.grid.square_size_m,
        grid_size=build.grid_size,
        terrain_file=f"/levels/{level.id}/theTerrain.ter",
        base_tex_size=min(4096, level.grid.grid_size),
        material_texture_set=f"{level.id}TerrainMaterialTextureSet",
    )
    tree.add_group(("level_objects", "terrain"))
    tree.add_object(("level_objects", "terrain"), terrain_placement.to_object(level.id))

    build_sun_and_sky(
        tree,
        ("level_objects", "sky_and_sun"),
        latitude=level.center_lat,
        longitude=level.center_lon,
        elevation_m=ground_z,
        visible_distance_m=float(max(4000.0, extent * 2.5)),
    )

    export_stats = add_network_to_scene(
        tree,
        roads,
        build,
        group_path=("level_objects", "road_network"),
        max_node_spacing_m=max_decal_node_spacing_m,
    )

    spawn = _choose_spawn(roads, build, fallback=(origin_x + extent / 2, origin_y + extent / 2))
    add_spawn_point(
        tree,
        ("PlayerDropPoints",),
        key="ivanovo_centre",
        x=spawn[0],
        y=spawn[1],
        z=spawn[2] + 1.5,
    )
    if spawn[3]:
        warnings.append(f"spawn point placed {spawn[3]:.0f} m from the level centre (nearest road)")

    # -- files -------------------------------------------------------------
    report(0.86, "writing level files")
    level_dir = config.build_path("levels", level.id)
    files = tree.render()
    files["info.json"] = json.dumps(
        build_info_json(
            level.id,
            level.title,
            f"1:1 scale map of {config.region_title} generated from OpenStreetMap and "
            f"Copernicus DEM. Build extent {extent / 1000:.2f} km, vertical range "
            f"{build.max_height:.1f} m.",
            extent_m=extent,
        ),
        indent=2,
        ensure_ascii=False,
    ) + "\n"
    files["map.json"] = json.dumps({"segments": {}}, indent=2) + "\n"
    files["main.decals.json"] = json.dumps(
        {
            "header": {
                "name": "DecalData File",
                "comments": "// Instances format: rectIdx, size, renderPriority, position.x, position.y, position.z, normal.x, normal.y, normal.z, tangent.x, tangent.y, tangent.z, uid",
                "version": 2,
            },
            "instances": {},
        },
        indent=2,
    ) + "\n"
    written = write_level_files(level_dir, files)

    write_json(
        level_dir / "theTerrain.terrain.json",
        build_terrain_json(
            level.id, "theTerrain", size=build.grid_size, materials=build.materials
        ),
    )
    write_json(
        level_dir / "art" / "terrains" / "main.materials.json",
        build_materials_json(level.id, DEFAULT_MATERIAL_ORDER, base_tex_size=terrain_placement.base_tex_size),
    )
    terrain_bytes = write_ter(level_dir / "theTerrain.ter", build.to_terrain_file())
    durations["scene"] = time.perf_counter() - stage

    # -- package -----------------------------------------------------------
    stage = time.perf_counter()
    mod_zip: Path | None = None
    if make_zip:
        report(0.94, "packaging the mod archive")
        mod_zip = package_level(config.build_dir, level.id)
    durations["package"] = time.perf_counter() - stage

    stats: dict[str, Any] = {
        "level_id": level.id,
        "terrain": build.stats,
        "roads": road_stats,
        "osm": osm_stats,
        "export": export_stats,
        "spawn": {"x": spawn[0], "y": spawn[1], "z": spawn[2]},
        "files": len(written) + 3,
        "terrain_bytes": terrain_bytes,
        "asset_references": len(referenced_asset_paths(DEFAULT_MATERIAL_ORDER)),
        "total_seconds": time.perf_counter() - start,
    }
    report(1.0, "build complete")
    return BuildResult(
        level_id=level.id,
        level_dir=level_dir,
        mod_zip=mod_zip,
        stats=stats,
        durations=durations,
        warnings=warnings,
    )


def _choose_spawn(
    roads: Sequence[Any],
    build: Any,
    *,
    fallback: tuple[float, float],
) -> tuple[float, float, float, float]:
    """Choose a spawn point on the best road near the level centre.

    Spawning on a road matters: a drop point in a field gives a poor first
    impression and, worse, makes it hard to tell whether the road network
    actually works.

    Args:
        roads: Extracted roads.
        build: The built terrain.
        fallback: ``(x, y)`` to use when no road is available.

    Returns:
        ``(x, y, z, distance_from_centre_m)``. Distance is ``0`` when the
        fallback was used.
    """
    elevations = build.elevations
    origin_x, origin_y = build.origin_x, build.origin_y
    cell = build.cell_size_m
    n = elevations.shape[0]

    def sample(x: float, y: float) -> float:
        col = int(np.clip(round((x - origin_x) / cell), 0, n - 1))
        row = int(np.clip(round((y - origin_y) / cell), 0, n - 1))
        return float(elevations[row, col])

    best: tuple[float, float, float, float] | None = None
    for road in roads:
        if not road.drivable or road.bridge or road.tunnel:
            continue
        midpoint = road.points[road.points.shape[0] // 2]
        distance = float(np.hypot(midpoint[0], midpoint[1]))
        if best is None or distance < best[3]:
            best = (float(midpoint[0]), float(midpoint[1]), sample(*midpoint), distance)

    if best is None:
        x, y = fallback
        return (x, y, sample(x, y), 0.0)
    return best
