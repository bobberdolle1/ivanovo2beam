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

import contextlib
import json
import math
import shutil
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
    write_terrain_base_textures,
)
from ivanovo2beam.beamng.preview import generate_level_preview_png
from ivanovo2beam.beamng.ter import write_ter
from ivanovo2beam.buildings.export import export_buildings
from ivanovo2beam.buildings.footprints import extract_buildings
from ivanovo2beam.osm.merge import merge_osm_data, osm_bbox_tiles
from ivanovo2beam.provenance import build_attribution_manifest, write_attribution_files
from ivanovo2beam.roads.bridges import (
    build_bridge_geometry,
    build_bridge_scene_object,
    compute_bridge_deck_profile,
    emit_bridges_materials,
    export_bridge_dae,
)
from ivanovo2beam.roads.build import (
    add_network_to_scene,
    roads_to_corridors,
    sample_surface_z,
)
from ivanovo2beam.roads.materials import write_road_materials
from ivanovo2beam.roads.network import extract_roads, road_network_stats
from ivanovo2beam.roads.railways import (
    export_railways,
    extract_railways,
    railways_to_corridors,
)
from ivanovo2beam.terrain.builder import build_terrain
from ivanovo2beam.vegetation.extract import extract_vegetation
from ivanovo2beam.vegetation.forest import export_vegetation
from ivanovo2beam.water.export import export_water_to_scene
from ivanovo2beam.water.extract import extract_water

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
        if "water" in stats and stats["water"].get("rivers"):
            w = stats["water"]
            lines.append(
                f"  water       {w.get('rivers', 0)} rivers ({w.get('river_length_km', 0.0):.1f} km), "
                f"{w.get('water_bodies', 0)} bodies ({w.get('water_area_km2', 0.0):.2f} km²)"
            )
        if "bridges" in stats and stats["bridges"].get("bridges"):
            br = stats["bridges"]
            lines.append(
                f"  bridges     {br['bridges']} 3D structures ({br.get('total_length_m', 0.0):.0f} m deck)"
            )
        if "railways" in stats and stats["railways"].get("railways"):
            rw = stats["railways"]
            lines.append(
                f"  railways    {rw['railways']} 1520mm tracks, {rw.get('length_km', 0.0):.1f} km"
            )
        if stats.get("buildings", {}).get("buildings"):
            b = stats["buildings"]
            lines.append(
                f"  buildings   {b['buildings']} placed "
                f"({b.get('archetypes', 0)} archetypes, {b.get('unique_meshes', 0)} unique)"
            )
        if stats.get("vegetation", {}).get("trees"):
            v = stats["vegetation"]
            lines.append(
                f"  vegetation  {v['trees']} trees across {v.get('areas', 0)} areas"
            )
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


def _cached_overlapping_osm_files(
    cache_dir: Path,
    bbox: Any,
    read_cache_meta: Any,
) -> tuple[list[Path], float, float]:
    """Return cached ``.osm`` files whose sidecar bbox overlaps ``bbox``, plus span coverage."""
    if not cache_dir.is_dir():
        return [], 0.0, 0.0
    matched: list[Path] = []
    u_west, u_south, u_east, u_north = math.inf, math.inf, -math.inf, -math.inf
    for cp in sorted(cache_dir.glob("*.osm")):
        meta = read_cache_meta(cp)
        if not isinstance(meta, dict):
            continue
        mb = meta.get("bbox") or meta.get("request", {}).get("bbox")
        if not isinstance(mb, dict):
            continue
        mw = mb.get("west")
        ms = mb.get("south")
        me = mb.get("east")
        mn = mb.get("north")
        if None in (mw, ms, me, mn):
            continue
        if me >= bbox.west and mw <= bbox.east and mn >= bbox.south and ms <= bbox.north:
            matched.append(cp)
            u_west = min(u_west, float(mw))
            u_south = min(u_south, float(ms))
            u_east = max(u_east, float(me))
            u_north = max(u_north, float(mn))
    if not matched:
        return [], 0.0, 0.0
    bbox_w = max(1e-9, float(bbox.east - bbox.west))
    bbox_h = max(1e-9, float(bbox.north - bbox.south))
    lon_cov = max(0.0, min(float(bbox.east), u_east) - max(float(bbox.west), u_west)) / bbox_w
    lat_cov = max(0.0, min(float(bbox.north), u_north) - max(float(bbox.south), u_south)) / bbox_h
    return matched, lon_cov, lat_cov


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
    from ivanovo2beam.gis.projection import BBox
    from ivanovo2beam.osm import cache_paths, fetch_overpass, parse_osm, read_cache_meta
    from ivanovo2beam.osm.pbf import parse_osm_pbf

    tiles = osm_bbox_tiles(bbox.west, bbox.south, bbox.east, bbox.north, tile_km=tile_km)
    cache_dir = config.cache_path("osm")
    extracts = []
    failures: list[str] = []
    skipped: list[str] = []
    started = time.perf_counter()
    timed_out = False

    # Prefer a local regional .osm.pbf extract when present (complete coverage,
    # zero Overpass rate-limiting or 502 timeouts).
    if not force and cache_dir.is_dir():
        pbf_files = sorted(cache_dir.glob("*.osm.pbf"))
        if pbf_files:
            pbf_path = pbf_files[0]
            if progress is not None:
                progress(0.05, f"loading OSM PBF {pbf_path.name}")
            osm_data = parse_osm_pbf(pbf_path, bbox=bbox)
            elapsed = round(time.perf_counter() - started, 1)
            return osm_data, {
                "source": str(pbf_path),
                "load_seconds": elapsed,
                "tiles_requested": 1,
                "tiles_succeeded": 1,
                "tiles_failed": 0,
                "tiles_skipped": 0,
                "budget_exhausted": False,
                "fetch_seconds": elapsed,
                "merge": {},
                "merge_summary": f"loaded {pbf_path.name} ({len(osm_data.nodes)} nodes, {len(osm_data.ways)} ways)",
            }

    # Reuse a cached multi-tile extract whose sidecar bboxes cover this level,
    # avoiding 240s Overpass hangs or loading unrelated regions' cache files.
    if not force:
        cached_overlap, lon_cov, lat_cov = _cached_overlapping_osm_files(cache_dir, bbox, read_cache_meta)
        if len(cached_overlap) >= 10 and lon_cov >= 0.5 and lat_cov >= 0.5:
            for i, cp in enumerate(cached_overlap):
                if progress is not None:
                    progress(0.05 + 0.25 * (i / len(cached_overlap)), f"cached OSM {i + 1}/{len(cached_overlap)}")
                with contextlib.suppress(Exception):
                    extracts.append(parse_osm(cp))
            if extracts:
                merged, report = merge_osm_data(extracts)
                return merged, {
                    "tiles_requested": len(cached_overlap),
                    "tiles_succeeded": len(extracts),
                    "tiles_failed": 0,
                    "tiles_skipped": 0,
                    "budget_exhausted": False,
                    "fetch_seconds": round(time.perf_counter() - started, 1),
                    "merge": report.as_dict(),
                    "merge_summary": f"loaded {len(extracts)} cached extracts",
                }

    for i, (west, south, east, north) in enumerate(tiles):
        if progress is not None:
            progress(0.05 + 0.25 * (i / max(1, len(tiles))), f"OSM tile {i + 1}/{len(tiles)}")
        if timed_out:
            skipped.append(f"tile {i + 1}")
            continue
        tile_bbox = BBox(west=west, south=south, east=east, north=north)
        try:
            path = fetch_overpass(tile_bbox, cache_dir, force=force)
            extracts.append(parse_osm(path))
        except Exception as exc:
            failures.append(f"tile {i + 1}/{len(tiles)} ({west:.4f},{south:.4f}): {exc}")
        if time.perf_counter() - started > budget_s and i + 1 < len(tiles):
            timed_out = True

    if len(extracts) < len(tiles) and cache_dir.is_dir():
        if not extracts:
            for west, south, east, north in tiles:
                tile_bbox = BBox(west=west, south=south, east=east, north=north)
                osm_path, _ = cache_paths(cache_dir, tile_bbox)
                if osm_path.is_file():
                    with contextlib.suppress(Exception):
                        extracts.append(parse_osm(osm_path))
        cached_overlap, _, _ = _cached_overlapping_osm_files(cache_dir, bbox, read_cache_meta)
        if len(cached_overlap) > len(extracts):
            fallback_extracts = []
            for cp in cached_overlap:
                with contextlib.suppress(Exception):
                    fallback_extracts.append(parse_osm(cp))
            if len(fallback_extracts) > len(extracts):
                extracts = fallback_extracts
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
            with archive.open(info, "w") as dst, path.open("rb") as src:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
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
    max_buildings: int | None = None,
    building_simplify_m: float = 1.0,
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
        max_buildings: Optional cap on exported buildings, largest first.
            Useful for keeping a first look at a large level tractable.
        building_simplify_m: Footprint simplification tolerance in metres.
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
    report(0.30, "projecting and classifying roads")
    margin = ROAD_RENDER_MARGIN_M
    clip_bounds = (
        origin_x - margin,
        origin_y - margin,
        origin_x + extent + margin,
        origin_y + extent + margin,
    )
    roads = extract_roads(osm_data, config.projection, config.roads, clip_bounds=clip_bounds)
    road_stats = road_network_stats(roads)
    durations["roads"] = time.perf_counter() - stage
    if not roads:
        warnings.append("no drivable roads were found in the OSM extract")

    # -- water (hydrology) -------------------------------------------------
    stage = time.perf_counter()
    water_network = None
    water_stats: dict[str, Any] = {"enabled": config.water_enabled}
    if config.water_enabled:
        report(0.33, "extracting hydrology and water bodies")
        try:
            water_clip = (origin_x, origin_y, origin_x + extent, origin_y + extent)
            water_network = extract_water(osm_data, config.projection, clip_bounds=water_clip)
            water_stats.update({
                "rivers": len(water_network.rivers),
                "river_length_km": round(water_network.total_river_length_m / 1000.0, 2),
                "water_bodies": len(water_network.water_bodies),
                "water_area_km2": round(water_network.total_water_area_m2 / 1e6, 3),
            })
        except Exception as exc:
            warnings.append(f"hydrology extraction failed: {exc}")
    durations["water"] = time.perf_counter() - stage

    # -- railways ----------------------------------------------------------
    stage = time.perf_counter()
    railways: list[Any] = []
    railway_stats: dict[str, Any] = {"enabled": config.railways_enabled}
    if config.railways_enabled:
        report(0.35, "extracting railways and tram lines")
        try:
            railways = extract_railways(osm_data, config.projection, clip_bounds=clip_bounds)
            railway_stats["railways"] = len(railways)
            railway_stats["length_km"] = round(sum(rw.length_m for rw in railways) / 1000.0, 2)
        except Exception as exc:
            warnings.append(f"railway extraction failed: {exc}")
    durations["railways"] = time.perf_counter() - stage

    # -- building footprints -----------------------------------------------
    stage = time.perf_counter()
    buildings: list[Any] = []
    footprint_stats: dict[str, Any] = {}
    if config.buildings_enabled:
        report(0.38, "extracting building footprints")
        buildings, footprints = extract_buildings(
            osm_data,
            config.projection,
            clip_bounds=clip_bounds,
            simplify_tolerance_m=building_simplify_m,
            max_buildings=max_buildings,
        )
        footprint_stats = footprints.as_dict()
        if footprints.skipped_relations:
            warnings.append(
                f"{footprints.skipped_relations} multipolygon buildings were skipped; "
                "only closed ways are assembled into footprints"
            )
    durations["buildings_extract"] = time.perf_counter() - stage

    # -- vegetation extraction ---------------------------------------------
    stage = time.perf_counter()
    vegetation_areas: list[Any] = []
    veg_stats: dict[str, Any] = {"enabled": config.vegetation_enabled}
    if config.vegetation_enabled:
        report(0.40, "extracting vegetation and forest areas")
        try:
            vegetation_areas, v_stats = extract_vegetation(
                osm_data,
                config.projection,
                clip_bounds=clip_bounds,
            )
            veg_stats.update(v_stats.as_dict())
        except Exception as exc:
            warnings.append(f"vegetation extraction failed: {exc}")
    durations["vegetation_extract"] = time.perf_counter() - stage

    # -- corridors & terrain -----------------------------------------------
    stage = time.perf_counter()
    report(0.42, "building terrain corridors and grading terrain")
    with DemReader(dem_path) as dem:
        corridors = roads_to_corridors(roads, config.roads)
        if config.railways_enabled and railways:
            corridors.extend(railways_to_corridors(railways))

        landcover_polys: dict[str, list[Any]] = {}
        field_rings = [v.ring for v in vegetation_areas if v.kind == "scrub"]
        farmland_rings = []
        grass_rings = [v.ring for v in vegetation_areas if v.kind == "meadow"]
        industrial_rings = []
        residential_rings = []
        clip_min_x, clip_min_y, clip_max_x, clip_max_y = clip_bounds
        if osm_data is not None and getattr(osm_data, "ways", None):
            for way in osm_data.ways:
                if not way.is_closed or len(way.nodes) < 4:
                    continue
                lu = way.tags.get("landuse", "")
                if not lu:
                    continue
                coords = osm_data.way_coords(way)
                if len(coords) < 3:
                    continue
                lons = np.asarray([c[0] for c in coords], dtype=np.float64)
                lats = np.asarray([c[1] for c in coords], dtype=np.float64)
                xs, ys = config.projection.geo_to_world_array(lons, lats)
                if (
                    float(xs.max()) < clip_min_x
                    or float(xs.min()) > clip_max_x
                    or float(ys.max()) < clip_min_y
                    or float(ys.min()) > clip_max_y
                ):
                    continue
                ring = np.column_stack((xs, ys))
                if lu in ("farmland", "farmyard") or (lu in ("allotments", "orchard") and not vegetation_areas):
                    farmland_rings.append(ring)
                elif lu in ("greenfield", "recreation_ground") or (
                    lu in ("meadow", "grass", "village_green") and not vegetation_areas
                ):
                    grass_rings.append(ring)
                elif lu in ("industrial", "commercial", "retail", "construction"):
                    industrial_rings.append(ring)
                elif lu == "residential":
                    residential_rings.append(ring)

        if field_rings or farmland_rings:
            landcover_polys["Field"] = field_rings + farmland_rings

        forest_rings = [v.ring for v in vegetation_areas if v.kind == "forest"]
        if forest_rings:
            landcover_polys["ForestFloor"] = forest_rings

        # Paint meadows/residential yards as lush summer grass after farmland/fields
        # so village areas stay green instead of worn asphalt or dry stubble.
        if grass_rings or residential_rings:
            landcover_polys["Grass"] = grass_rings + residential_rings

        building_rings = [b.ring for b in buildings] if buildings else []
        if industrial_rings or building_rings:
            landcover_polys["Concrete"] = industrial_rings + building_rings

        forest_polys = forest_rings if forest_rings else None
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
            water_network=water_network,
            forest_polygons=forest_polys,
            landcover_polygons=landcover_polys or None,
            progress=lambda f, m: report(0.42 + 0.30 * f, m),
        )
    durations["terrain"] = time.perf_counter() - stage
    if build.graded_cells == 0 and roads:
        warnings.append("no road corridors were graded into the terrain")

    # -- scene -------------------------------------------------------------
    stage = time.perf_counter()
    report(0.74, "assembling the level scene")
    tree = SceneTree(level_id=level.id)
    level_dir = config.build_path("levels", level.id)
    if level_dir.exists():
        shutil.rmtree(level_dir)
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
        base_tex_size=512,
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
        time_of_day=level.time_of_day,
        axis_tilt=level.axis_tilt,
        visible_distance_m=min(float(max(4000.0, extent * 2.5)), 12000.0),
        fog_density=0.00018,
    )

    export_stats = add_network_to_scene(
        tree,
        roads,
        build,
        group_path=("level_objects", "road_network"),
        max_node_spacing_m=max_decal_node_spacing_m,
    )

    # -- water scene objects -----------------------------------------------
    if water_network is not None:
        report(0.76, "exporting water scene objects")
        water_objects_count = export_water_to_scene(tree, water_network)
        water_stats["scene_objects"] = water_objects_count

    # -- bridges 3D meshes -------------------------------------------------
    bridge_stats: dict[str, Any] = {"enabled": config.bridges_enabled}
    bridges_dir = level_dir / "art" / "shapes" / "bridges"
    if bridges_dir.exists():
        shutil.rmtree(bridges_dir)
    if config.bridges_enabled:
        bridge_roads = [r for r in roads if r.bridge and len(r.points) >= 2]
        if bridge_roads:
            report(0.78, f"generating {len(bridge_roads)} 3D bridge structures")
            bridges_dir.mkdir(parents=True, exist_ok=True)
            exported_bridges = 0
            total_bridge_len = 0.0
            for r in bridge_roads:
                try:
                    gz = sample_surface_z(r, build)
                    deck_z = compute_bridge_deck_profile(r, ground_z=gz)
                    geom = build_bridge_geometry(r, deck_z, localize=True)
                    dae_path = bridges_dir / f"bridge_way{r.osm_id}.dae"
                    export_bridge_dae(dae_path, geom, f"bridge_way{r.osm_id}")
                    obj = build_bridge_scene_object(r, level.id, position=geom.origin)
                    tree.add_object(("level_objects", "bridges"), obj)
                    exported_bridges += 1
                    total_bridge_len += r.length_m
                except Exception as exc:
                    warnings.append(f"bridge way {r.osm_id} export failed: {exc}")
            if exported_bridges > 0:
                emit_bridges_materials(level_dir, level.id)
            elif bridges_dir.exists():
                shutil.rmtree(bridges_dir)
            bridge_stats["bridges"] = exported_bridges
            bridge_stats["total_length_m"] = round(total_bridge_len, 1)
    durations["bridges"] = time.perf_counter() - stage

    # -- railways 3D meshes ------------------------------------------------
    stage = time.perf_counter()
    railways_dir = level_dir / "art" / "shapes" / "railways"
    if railways_dir.exists():
        shutil.rmtree(railways_dir)
    if config.railways_enabled and railways:
        report(0.80, f"generating {len(railways)} 1520mm railway tracks")
        try:
            rw_objects, rw_export = export_railways(
                railways,
                level.id,
                level_dir,
                roads=roads,
                chunk_size_m=float(config.sector_size_m),
                batch_chunks=True,
                parent="railways",
            )
            for obj in rw_objects:
                tree.add_object(("level_objects", "railways"), obj)
            if rw_export.get("exported", 0) == 0 and railways_dir.exists():
                shutil.rmtree(railways_dir)
            railway_stats.update(rw_export)
        except Exception as exc:
            warnings.append(f"railway export failed: {exc}")
    durations["railways_export"] = time.perf_counter() - stage

    # -- buildings ---------------------------------------------------------
    stage = time.perf_counter()
    building_stats: dict[str, Any] = {"enabled": config.buildings_enabled}
    if config.buildings_enabled and buildings:
        report(0.82, f"generating {len(buildings)} buildings")
        building_objects, building_export = export_buildings(
            buildings,
            build,
            level_id=level.id,
            level_dir=level_dir,
            max_instances=max_buildings,
            batch_chunks=True,
        )
        for obj in building_objects:
            tree.add_object(("level_objects", "buildings"), obj)
        building_stats.update(building_export.as_dict())
        if building_export.placements_skipped:
            warnings.append(
                f"{building_export.placements_skipped} buildings could not be placed"
            )
    elif config.buildings_enabled:
        stale_shapes = level_dir / "art" / "shapes" / "buildings"
        if stale_shapes.exists():
            shutil.rmtree(stale_shapes)
        warnings.append("buildings are enabled but the OSM extract contained no footprints")
    else:
        stale_shapes = level_dir / "art" / "shapes" / "buildings"
        if stale_shapes.exists():
            shutil.rmtree(stale_shapes)
    durations["buildings"] = time.perf_counter() - stage

    # -- vegetation export -------------------------------------------------
    stage = time.perf_counter()
    veg_export_stats: dict[str, Any] = dict(veg_stats)
    if config.vegetation_enabled and vegetation_areas:
        report(0.85, f"generating forest and vegetation across {len(vegetation_areas)} areas")
        water_items = []
        if water_network:
            water_items.extend(water_network.rivers)
            water_items.extend(water_network.water_bodies)
        try:
            veg_objects, v_exp = export_vegetation(
                vegetation_areas,
                build,
                level_id=level.id,
                level_dir=level_dir,
                roads=roads,
                buildings=buildings,
                water=water_items,
                railways=railways,
            )
            for obj in veg_objects:
                tree.add_object(("level_objects", "vegetation"), obj)
            veg_export_stats.update(v_exp.as_dict())
        except Exception as exc:
            warnings.append(f"vegetation export failed: {exc}")
    durations["vegetation"] = time.perf_counter() - stage

    # -- spawn points ------------------------------------------------------
    if level.id == "ivanovo_shuya":
        spawn_presets = [
            {"key": "player_spawn", "name": "spawn_default", "title": "Ivanovo-Shuya Highway (Кохомское шоссе / Выезд на Шую)", "geo": (41.0370, 56.9530)},
            {"key": "spawn_kitovo", "name": "spawn_kitovo", "title": "Kitovo Highway Straightaway (Скоростная прямая у Китово)", "geo": (41.3000, 56.8650)},
            {"key": "spawn_shuya", "name": "spawn_shuya", "title": "Entrance to Shuya (Въезд в г. Шуя, Р152)", "geo": (41.3500, 56.8500)},
            {"key": "spawn_forest", "name": "spawn_forest", "title": "Forest Highway Section (Лесной участок трассы)", "geo": (41.1700, 56.9150)},
            {"key": "spawn_ivanovo", "name": "spawn_ivanovo", "title": "Ivanovo City Origin (Площадь Революции)", "geo": (40.97393, 56.99987)},
        ]
    else:
        spawn_presets = [
            {"key": "player_spawn", "name": "spawn_default", "title": "City Centre (Площадь Революции)", "geo": (40.97393, 56.99987)},
            {"key": "spawn_pushkin", "name": "spawn_pushkin", "title": "Pushkin Square & Uvod (Площадь Пушкина)", "geo": (40.9740, 56.9935)},
            {"key": "spawn_bim", "name": "spawn_bim", "title": "BIM Textile Factory (БИМ / Река Уводь)", "geo": (40.9765, 57.0035)},
            {"key": "spawn_sheremetev", "name": "spawn_sheremetev", "title": "Sheremetevsky Prospekt (Шереметевский проспект)", "geo": (40.9850, 57.0050)},
            {"key": "spawn_embankment", "name": "spawn_embankment", "title": "Uvod Embankment (Набережная Уводи / Серебряный Город)", "geo": (40.9690, 57.0010)},
        ]

    spawn_points_info: list[dict[str, Any]] = []
    stats_spawn_anchor = [0.0, 0.0]
    default_spawn: tuple[float, float, float, float, float] | None = None
    for sp in spawn_presets:
        lon, lat = sp["geo"]
        anchor_x, anchor_y = config.projection.geo_to_world(lon, lat)
        spawn = _choose_spawn(roads, build, anchor=(anchor_x, anchor_y))
        add_spawn_point(
            tree,
            ("PlayerDropPoints",),
            key=sp["key"],
            name=sp["name"],
            x=spawn[0],
            y=spawn[1],
            z=spawn[2] + 0.35,
            radius=5.0,
            heading_rad=spawn[4] if len(spawn) > 4 else 0.0,
        )
        spawn_points_info.append({
            "objectname": sp["name"],
            "translationId": sp["title"],
            "preview": f"{level.id}_preview.png",
        })
        if sp["name"] == "spawn_default":
            stats_spawn_anchor = [round(anchor_x, 1), round(anchor_y, 1)]
            default_spawn = spawn
        elif default_spawn is None:
            default_spawn = spawn

    # -- files -------------------------------------------------------------
    stage = time.perf_counter()
    report(0.88, "writing level files")
    files = tree.render()
    files["info.json"] = (
        json.dumps(
            build_info_json(
                level.id,
                level.title,
                f"1:1 scale map of {config.region_title} generated from OpenStreetMap and "
                f"Copernicus DEM. Build extent {extent / 1000:.2f} km, vertical range "
                f"{build.max_height:.1f} m.",
                extent_m=extent,
                default_spawn_point_name="spawn_default",
                spawn_points=spawn_points_info,
            ),
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    files["map.json"] = json.dumps({"segments": {}}, indent=2) + "\n"
    files["main.decals.json"] = (
        json.dumps(
            {
                "header": {
                    "name": "DecalData File",
                    "comments": "// Instances format: rectIdx, size, renderPriority, position.x, position.y, position.z, normal.x, normal.y, normal.z, tangent.x, tangent.y, tangent.z, uid",
                    "version": 2,
                },
                "instances": {},
            },
            indent=2,
        )
        + "\n"
    )
    write_level_files(level_dir, files)

    write_json(
        level_dir / "theTerrain.terrain.json",
        build_terrain_json(level.id, "theTerrain", size=build.grid_size, materials=build.materials),
    )
    write_terrain_base_textures(level_dir, DEFAULT_MATERIAL_ORDER, size=terrain_placement.base_tex_size)
    write_json(
        level_dir / "art" / "terrains" / "main.materials.json",
        build_materials_json(level.id, DEFAULT_MATERIAL_ORDER, base_tex_size=terrain_placement.base_tex_size),
    )
    write_road_materials(level_dir, level.id)
    terrain_bytes = write_ter(level_dir / "theTerrain.ter", build.to_terrain_file())

    # Generate preview PNG for level selection
    generate_level_preview_png(level_dir, level.id)

    # Provenance attribution
    try:
        manifest = build_attribution_manifest(
            level.id,
            dem_sources=[dem_path],
        )
        write_attribution_files(manifest, level_dir=level_dir, build_dir=config.build_dir)
    except Exception as exc:
        warnings.append(f"attribution manifest generation failed: {exc}")

    durations["scene"] = time.perf_counter() - stage

    # -- package -----------------------------------------------------------
    stage = time.perf_counter()
    mod_zip: Path | None = None
    if make_zip:
        report(0.94, "packaging the mod archive")
        mod_zip = package_level(config.build_dir, level.id)
    durations["package"] = time.perf_counter() - stage

    spawn_for_stats = default_spawn if default_spawn is not None else (0.0, 0.0, ground_z, 0.0, 0.0)
    stats: dict[str, Any] = {
        "level_id": level.id,
        "terrain": build.stats,
        "roads": road_stats,
        "water": water_stats,
        "railways": railway_stats,
        "bridges": bridge_stats,
        "osm": osm_stats,
        "export": export_stats,
        "footprints": footprint_stats,
        "buildings": building_stats,
        "vegetation": veg_export_stats,
        "spawn": {
            "x": spawn_for_stats[0],
            "y": spawn_for_stats[1],
            "z": spawn_for_stats[2],
            "anchor": stats_spawn_anchor,
            "distance_from_anchor_m": round(spawn_for_stats[3], 1),
        },
        "files": sum(1 for p in level_dir.rglob("*") if p.is_file()),
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


_MAJOR_SPAWN_HIGHWAYS = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
    }
)


def _choose_spawn(
    roads: Sequence[Any],
    build: Any,
    *,
    anchor: tuple[float, float],
) -> tuple[float, float, float, float, float]:
    """Choose a spawn point on the drivable road nearest an anchor.

    Spawning on a road matters: a drop point in a field gives a poor first
    impression and, worse, makes it hard to tell whether the road network
    actually works. Placing it near a configured anchor rather than the
    geometric centre matters just as much for a corridor level, whose centre
    would otherwise be farmland halfway between two cities.

    Args:
        roads: Extracted roads.
        build: The built terrain.
        anchor: ``(x, y)`` world point the spawn should be as close to as
            possible, normally the region's world origin (Ivanovo centre).

    Returns:
        ``(x, y, z, distance_from_anchor_m, heading_rad)``.
    """
    elevations = build.elevations
    origin_x, origin_y = build.origin_x, build.origin_y
    cell = build.cell_size_m
    n = elevations.shape[0]
    max_x = origin_x + (n - 1) * cell
    max_y = origin_y + (n - 1) * cell
    anchor_x, anchor_y = anchor

    def sample(x: float, y: float) -> float:
        col = int(np.clip(round((x - origin_x) / cell), 0, n - 1))
        row = int(np.clip(round((y - origin_y) / cell), 0, n - 1))
        return float(elevations[row, col])

    def make_candidate(pts: Any, index: int, distance: float) -> tuple[float, float, float, float, float]:
        # Avoid placing the vehicle on a terminal dead-end DecalRoad endpoint
        # where startEndFade tapers to invisible and tangent faces off-road.
        if pts.shape[0] == 2 and index == 1:
            px = 0.5 * float(pts[0, 0] + pts[1, 0])
            py = 0.5 * float(pts[0, 1] + pts[1, 1])
            dx = float(pts[1, 0] - pts[0, 0])
            dy = float(pts[1, 1] - pts[0, 1])
            dist = math.hypot(px - anchor_x, py - anchor_y)
            return (px, py, sample(px, py), dist, math.atan2(dy, dx))
        point = pts[index]
        if index < pts.shape[0] - 1:
            dx = float(pts[index + 1, 0] - pts[index, 0])
            dy = float(pts[index + 1, 1] - pts[index, 1])
        elif index > 0:
            dx = float(pts[index, 0] - pts[index - 1, 0])
            dy = float(pts[index, 1] - pts[index - 1, 1])
        else:
            dx, dy = 1.0, 0.0
        heading = math.atan2(dy, dx)
        return (float(point[0]), float(point[1]), sample(float(point[0]), float(point[1])), distance, heading)

    best_major: tuple[float, float, float, float, float] | None = None
    best_any: tuple[float, float, float, float, float] | None = None
    best_fallback: tuple[float, float, float, float, float] | None = None
    for road in roads:
        if not road.drivable or road.bridge or road.tunnel:
            continue
        pts = road.points
        if pts.shape[0] < 2:
            continue
        distances = np.hypot(pts[:, 0] - anchor_x, pts[:, 1] - anchor_y)
        in_bounds = (
            (pts[:, 0] >= origin_x)
            & (pts[:, 0] <= max_x)
            & (pts[:, 1] >= origin_y)
            & (pts[:, 1] <= max_y)
        )
        if in_bounds.any():
            candidate_mask = in_bounds
            if pts.shape[0] >= 3:
                interior_mask = in_bounds.copy()
                interior_mask[0] = False
                interior_mask[-1] = False
                if interior_mask.any():
                    candidate_mask = interior_mask
            masked = np.where(candidate_mask, distances, np.inf)
            index = int(np.argmin(masked))
            distance = float(distances[index])
            cand = make_candidate(pts, index, distance)
            if best_any is None or cand[3] < best_any[3]:
                best_any = cand
            hw = getattr(road, "highway", "")
            if hw in _MAJOR_SPAWN_HIGHWAYS and (best_major is None or cand[3] < best_major[3]):
                best_major = cand
        elif best_any is None:
            index = int(np.argmin(distances))
            distance = float(distances[index])
            if best_fallback is None or distance < best_fallback[3]:
                best_fallback = make_candidate(pts, index, distance)

    if best_major is not None and best_major[3] <= 2500.0:
        return best_major
    if best_any is not None:
        return best_any
    if best_major is not None:
        return best_major
    if best_fallback is not None:
        return best_fallback
    return (anchor_x, anchor_y, sample(anchor_x, anchor_y), 0.0, 0.0)
