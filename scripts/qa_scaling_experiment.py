"""Repeatable live BeamNG.drive 0.40 D3D12 QA & scaling benchmark harness.

Supports Golden Sector V2 regression (``--mode regression``), P0 single-terrain
8192x8192@2m (``--mode p0``), P1 hybrid corridor (``--mode p1``), and P2 ~33 km
continuous corridor (``--mode p2``).

Controls BeamNG.drive exclusively via the localhost Host Bridge
(``scripts/beamng_host_bridge.py``) and BeamNG MCP (``http://127.0.0.1:29292/mcp``).
Never logs, copies, or stores any Host Bridge token, and anonymizes all
machine/user filesystem paths in output JSON reports.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from beamng_host_bridge import BeamNgBridgeClient  # noqa: E402
from benchmark_level import (  # noqa: E402
    _LUA_PERF_SNAPSHOT,
    DEFAULT_BEAMNG_EXE,
    DEFAULT_MCP_URL,
    DEFAULT_USER_DIR,
    BeamNgMcpClient,
    inspect_settings,
    inspect_static_budget,
    parse_beamng_log,
    query_nvidia_smi,
)

DEFAULT_SCREENSHOT_DIR = (
    Path.home() / ".gemini" / "antigravity" / "brain" / "315c13fd-de24-4b6e-a54e-5a8d8f975b09" / "screenshots"
)
VIEWS_JSON = REPO_ROOT / ".tmp" / "qa_camera_views.json"

_STOCK_ERROR_PATTERNS = (
    "OnlineServiceProvider| Could not initialize online service provider",
    "GELua.core_levels.| No entry point for level found:",
    "GELua.| Failed to set unload mode",
    "== OUTDATED MOD CODE ==",
    'registerCoreModule("partInfo"',
    "GELua.jsonDecode|",
    "GELua.core_vehicles.vehicles| invalid JSON file, ignoring: /vehicles/",
    "GELua.core_vehiclePaints.vehicles|",
    "GELua.jbeam.loadJBeamFile| Duplicate part found:",
    "GELua.jbeam.getAvailableSlotNameMap|",
    "CEF.VehicleTex-",
    "Navigator: Invalid center point detected.",
    "d3d12ma",
    "ActionMap",
)

_LEVEL_SIGNAL_PATTERNS = (
    "WarningMaterial",
    "NO MATERIAL",
    "GroundCover",
    "ForestData",
    "TerrainBlock",
    "SceneStaticDecalRoadManager",
    "SceneContainer::checkBins",
    "WaterBlock",
    "River",
    "Collada",
    "TSStatic",
    "failed to load",
    "Failed to find",
)


def anonymize_text(text: str, user_dir: Path, beamng_exe: Path) -> str:
    """Redact local user/machine paths and usernames from strings."""
    out = text
    replacements = [
        (str(REPO_ROOT), "<REPO_ROOT>"),
        (str(REPO_ROOT).replace("\\", "/"), "<REPO_ROOT>"),
        (str(user_dir), "<BEAMNG_USER_DIR>"),
        (str(user_dir).replace("\\", "/"), "<BEAMNG_USER_DIR>"),
        (str(beamng_exe.parent.parent), "<BEAMNG_INSTALL_DIR>"),
        (str(beamng_exe.parent.parent).replace("\\", "/"), "<BEAMNG_INSTALL_DIR>"),
        (str(Path.home()), "<USER_HOME>"),
        (str(Path.home()).replace("\\", "/"), "<USER_HOME>"),
    ]
    for src, dst in replacements:
        if src:
            out = out.replace(src, dst)
            out = out.replace(src.lower(), dst)
    for user_name in {getpass.getuser(), os.environ.get("USERNAME", "")}:
        if user_name and len(user_name) >= 3:
            out = re.sub(rf"(?i)([\\/]){re.escape(user_name)}([\\/])", r"\1<USER>\2", out)
    return out


def anonymize_obj(obj: Any, user_dir: Path, beamng_exe: Path) -> Any:
    """Recursively anonymize strings inside dicts/lists."""
    if isinstance(obj, str):
        return anonymize_text(obj, user_dir, beamng_exe)
    if isinstance(obj, list):
        return [anonymize_obj(v, user_dir, beamng_exe) for v in obj]
    if isinstance(obj, dict):
        return {k: anonymize_obj(v, user_dir, beamng_exe) for k, v in obj.items()}
    return obj


def get_beamng_memory_stats() -> dict[str, Any] | None:
    """Query all ``BeamNG.drive*`` processes and return main + total RAM in MiB."""
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$ps = Get-Process -Name 'BeamNG.drive*' -ErrorAction SilentlyContinue; "
                "if ($ps) { $ps | ForEach-Object { \"$($_.Id),$($_.WorkingSet64),$($_.PeakWorkingSet64),$($_.PrivateMemorySize64)\" } }",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        rows: list[tuple[int, float, float, float]] = []
        mib = 1024.0 * 1024.0
        for line in proc.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                rows.append(
                    (
                        int(parts[0]),
                        float(parts[1]) / mib,
                        float(parts[2]) / mib,
                        float(parts[3]) / mib,
                    )
                )
        if not rows:
            return None
        main = max(rows, key=lambda r: r[1])
        return {
            "process_count": len(rows),
            "main_pid": main[0],
            "working_set_mib": round(main[1], 1),
            "peak_working_set_mib": round(main[2], 1),
            "private_bytes_mib": round(main[3], 1),
            "total_working_set_mib": round(sum(r[1] for r in rows), 1),
            "total_private_mib": round(sum(r[3] for r in rows), 1),
        }
    except Exception:
        return None


def classify_beamng_log(log_path: Path, user_dir: Path, beamng_exe: Path) -> dict[str, Any]:
    """Parse ``beamng.log`` and separate stock game noise from level errors."""
    lm = parse_beamng_log(log_path)
    stock_errors = 0
    level_errors: list[str] = []
    level_warnings: list[str] = []
    signal_hits: dict[str, int] = {pat: 0 for pat in _LEVEL_SIGNAL_PATTERNS}

    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            is_err = "|E|" in line
            is_warn = "|W|" in line
            if not (is_err or is_warn):
                continue
            is_stock = any(pat in line for pat in _STOCK_ERROR_PATTERNS)
            if is_err:
                if is_stock:
                    stock_errors += 1
                else:
                    clean = anonymize_text(line.strip()[:260], user_dir, beamng_exe)
                    if clean not in level_errors and len(level_errors) < 50:
                        level_errors.append(clean)
            if is_warn and not is_stock:
                for pat in _LEVEL_SIGNAL_PATTERNS:
                    if pat in line:
                        signal_hits[pat] += 1
                        clean = anonymize_text(line.strip()[:260], user_dir, beamng_exe)
                        if clean not in level_warnings and len(level_warnings) < 50:
                            level_warnings.append(clean)
                        break
            if is_err and not is_stock:
                for pat in _LEVEL_SIGNAL_PATTERNS:
                    if pat in line:
                        signal_hits[pat] += 1

    return {
        "world_ready_s": lm.world_ready_s,
        "load_stages_s": lm.load_stages_s,
        "forest_items_loaded": lm.forest_items_loaded,
        "forest_types_loaded": lm.forest_types_loaded,
        "static_collision_verts": lm.static_collision_verts,
        "static_collision_tris": lm.static_collision_tris,
        "static_collision_mb": lm.static_collision_mb,
        "physics_instances": lm.physics_instances,
        "physics_memory_mb": lm.physics_memory_mb,
        "free_ram_at_spawn_gb": lm.free_ram_at_spawn_gb,
        "total_error_lines": lm.error_lines,
        "total_warning_lines": lm.warning_lines,
        "stock_error_lines": stock_errors,
        "level_error_lines": lm.error_lines - stock_errors,
        "decal_road_batch_errors": signal_hits.get("SceneStaticDecalRoadManager", 0),
        "level_error_samples": level_errors,
        "level_warning_samples": level_warnings,
        "signal_hits": {k: v for k, v in signal_hits.items() if v > 0},
    }


def summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate live 1 Hz telemetry samples into summary metrics."""
    if not samples:
        return {}

    def col(key: str) -> list[float]:
        return [
            float(s[key])
            for s in samples
            if s.get(key) is not None and isinstance(s.get(key), (int, float)) and float(s[key]) >= 0
        ]

    fps_vals = col("fps_inst")
    p99_vals = col("fps_p99")
    cpu_vals = col("cpu_ms")
    gpu_vals = col("gpu_ms")
    dc_vals = col("draw_calls")
    poly_vals = col("poly_count")
    tdc_vals = col("terrain_draw_calls")
    render_vals = col("perf_render_ms")
    shadows_vals = col("perf_shadows_ms")
    refl_vals = col("perf_reflections_ms")
    post_vals = col("perf_post_render_ms")
    lua_vals = col("perf_lua_ms")
    phys_vals = col("perf_physics_nodes_ms")
    forest_vals = col("forest_items_rendered")
    vram_vals = col("nv_vram_used_mib")
    ram_vals = col("proc_working_set_mib")
    priv_vals = col("proc_private_mib")
    tot_ram_vals = col("proc_total_working_set_mib")

    sorted_fps = sorted(fps_vals)
    p1_idx = max(0, int(len(sorted_fps) * 0.01) - 1) if sorted_fps else 0
    sampled_1pct_fps = sorted_fps[p1_idx] if sorted_fps else None
    engine_p99_fps = statistics.median(p99_vals) if p99_vals else None
    effective_1pct_fps = engine_p99_fps if engine_p99_fps is not None else sampled_1pct_fps
    avg_fps = statistics.mean(fps_vals) if fps_vals else None

    return {
        "sample_count": len(samples),
        "fps_avg": round(avg_fps, 2) if avg_fps is not None else None,
        "fps_1pct_low": round(effective_1pct_fps, 2) if effective_1pct_fps is not None else None,
        "fps_min_sampled": round(min(fps_vals), 2) if fps_vals else None,
        "frametime_avg_ms": round(1000.0 / avg_fps, 2) if avg_fps and avg_fps > 0 else None,
        "frametime_1pct_ms": (
            round(1000.0 / effective_1pct_fps, 2) if effective_1pct_fps and effective_1pct_fps > 0 else None
        ),
        "cpu_ms_avg": round(statistics.mean(cpu_vals), 2) if cpu_vals else None,
        "gpu_ms_avg": round(statistics.mean(gpu_vals), 2) if gpu_vals else None,
        "perf_render_ms_avg": round(statistics.mean(render_vals), 2) if render_vals else None,
        "perf_shadows_ms_avg": round(statistics.mean(shadows_vals), 2) if shadows_vals else None,
        "perf_reflections_ms_avg": round(statistics.mean(refl_vals), 2) if refl_vals else None,
        "perf_post_render_ms_avg": round(statistics.mean(post_vals), 2) if post_vals else None,
        "perf_lua_ms_avg": round(statistics.mean(lua_vals), 2) if lua_vals else None,
        "perf_physics_nodes_ms_avg": round(statistics.mean(phys_vals), 2) if phys_vals else None,
        "terrain_draw_calls_avg": round(statistics.mean(tdc_vals), 1) if tdc_vals else None,
        "terrain_draw_calls_max": int(max(tdc_vals)) if tdc_vals else None,
        "forest_items_rendered_avg": round(statistics.mean(forest_vals), 1) if forest_vals else None,
        "forest_items_rendered_max": int(max(forest_vals)) if forest_vals else None,
        "draw_calls_avg": round(statistics.mean(dc_vals), 1) if dc_vals else None,
        "draw_calls_max": int(max(dc_vals)) if dc_vals else None,
        "polys_avg": int(statistics.mean(poly_vals)) if poly_vals else None,
        "polys_max": int(max(poly_vals)) if poly_vals else None,
        "vram_peak_mib": round(max(vram_vals), 1) if vram_vals else None,
        "vram_avg_mib": round(statistics.mean(vram_vals), 1) if vram_vals else None,
        "proc_ram_peak_mib": round(max(ram_vals), 1) if ram_vals else None,
        "proc_private_peak_mib": round(max(priv_vals), 1) if priv_vals else None,
        "proc_total_ram_peak_mib": round(max(tot_ram_vals), 1) if tot_ram_vals else None,
        "pass_60fps_target": bool(avg_fps is not None and avg_fps >= 60.0),
    }


def inspect_extended_static(level_dir: Path | None) -> dict[str, Any]:
    """Collect extra static layout metrics (overObjects roads, ground meshes, blocks)."""
    if level_dir is None or not level_dir.is_dir():
        return {}
    terrain_blocks = 0
    over_objects_true = 0
    over_objects_false = 0
    ground_mesh_statics = 0
    ground_covers = 0
    for item_file in (level_dir / "main").rglob("items.level.json"):
        for line in item_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            cls = obj.get("class")
            if cls == "TerrainBlock":
                terrain_blocks += 1
            elif cls == "GroundCover":
                ground_covers += 1
            elif cls == "DecalRoad":
                if obj.get("overObjects"):
                    over_objects_true += 1
                else:
                    over_objects_false += 1
            elif cls == "TSStatic":
                shape = str(obj.get("shapeName", "")).lower()
                if "ground" in shape or "terrain" in shape or "corridor" in shape:
                    ground_mesh_statics += 1
    return {
        "terrain_blocks": terrain_blocks,
        "ground_covers": ground_covers,
        "decal_roads_over_objects_true": over_objects_true,
        "decal_roads_over_objects_false": over_objects_false,
        "ground_mesh_statics": ground_mesh_statics,
    }


def probe_scene_lua(mcp: BeamNgMcpClient) -> dict[str, Any]:
    """Query live SceneTree objects, TerrainBlocks, GroundCovers, DecalRoads, and materials."""
    lua = r"""
    local tbs = scenetree.findClassObjects("TerrainBlock") or {}
    local tb_info = {}
    for _, name in ipairs(tbs) do
        local o = scenetree.findObject(name)
        local p = o and o:getPosition()
        local wb = o and o:getWorldBox()
        table.insert(tb_info, {
            name = name,
            squareSize = o and o.squareSize or nil,
            pos = p and {p.x, p.y, p.z} or nil,
            min = wb and {wb.minExtents.x, wb.minExtents.y, wb.minExtents.z} or nil,
            max = wb and {wb.maxExtents.x, wb.maxExtents.y, wb.maxExtents.z} or nil
        })
    end
    local gcs = scenetree.findClassObjects("GroundCover") or {}
    local gc_info = {}
    for _, name in ipairs(gcs) do
        local o = scenetree.findObject(name)
        table.insert(gc_info, {
            name = name,
            hidden = o and o.hidden or nil,
            maxElements = o and o.maxElements or nil
        })
    end
    local drs = scenetree.findClassObjects("DecalRoad") or {}
    local over_true = 0
    local over_false = 0
    local missing_dr_mat = 0
    for _, name in ipairs(drs) do
        local o = scenetree.findObject(name)
        if o then
            if o.overObjects then
                over_true = over_true + 1
            else
                over_false = over_false + 1
            end
            local mat = o.material
            if not mat or mat == "" or string.find(tostring(mat), "WarningMaterial") then
                missing_dr_mat = missing_dr_mat + 1
            end
        end
    end
    local ts = scenetree.findClassObjects("TSStatic") or {}
    local ground_mesh_boxes = {}
    for _, name in ipairs(ts) do
        local o = scenetree.findObject(name)
        local sh = o and tostring(o.shapeName or "") or ""
        local sh_l = string.lower(sh)
        if string.find(sh_l, "/art/shapes/corridor") or string.find(sh_l, "/art/shapes/ground") or string.find(sh_l, "corridor_chunk") or string.find(sh_l, "ground_chunk") then
            if #ground_mesh_boxes < 24 then
                local wb = o:getWorldBox()
                table.insert(ground_mesh_boxes, {
                    name = name,
                    shape = sh,
                    min = wb and {wb.minExtents.x, wb.minExtents.y, wb.minExtents.z} or nil,
                    max = wb and {wb.maxExtents.x, wb.maxExtents.y, wb.maxExtents.z} or nil
                })
            end
        end
    end
    local wb = scenetree.findClassObjects("WaterBlock") or {}
    local rv = scenetree.findClassObjects("River") or {}
    local blds = scenetree.findObject("buildings")
    local props = scenetree.findObject("road_props")
    local veh = be and be:getPlayerVehicle(0)
    local vpos = veh and veh:getPosition()
    return jsonEncode({
        terrain_blocks = tb_info,
        groundcover_count = #gcs,
        groundcovers = gc_info,
        decal_roads_total = #drs,
        decal_roads_over_objects_true = over_true,
        decal_roads_over_objects_false = over_false,
        decal_roads_missing_material = missing_dr_mat,
        ts_static_count = #ts,
        ground_mesh_sample_boxes = ground_mesh_boxes,
        water_block_count = #wb,
        river_count = #rv,
        buildings_count = blds and blds:size() or 0,
        road_props_count = props and props:size() or 0,
        spawn_pos = vpos and {vpos.x, vpos.y, vpos.z} or nil
    })
    """
    return json.loads(mcp.call_tool("run_lua", {"code": lua}))


def probe_surface_and_double_collision(
    mcp: BeamNgMcpClient,
    points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Probe ``castRayStatic`` primary hit, secondary hit (double-collision), and terrain height."""
    pts_lua = ", ".join(
        f'{{label="{p["label"]}", x={float(p["x"])}, y={float(p["y"])}}}' for p in points
    )
    lua = f"""
    local pts = {{ {pts_lua} }}
    local out = {{}}
    for _, p in ipairs(pts) do
        local th = core_terrain and core_terrain.getTerrainHeight and core_terrain.getTerrainHeight(vec3(p.x, p.y, 500)) or nil
        local d1 = castRayStatic(vec3(p.x, p.y, 500), vec3(0, 0, -1), 600)
        local z1 = (d1 and d1 > 0 and d1 < 599.9) and (500 - d1) or nil
        local z2 = nil
        local gap = nil
        if z1 ~= nil then
            local d2 = castRayStatic(vec3(p.x, p.y, z1 - 0.05), vec3(0, 0, -1), 100)
            if d2 and d2 > 0 and d2 < 99.9 then
                z2 = (z1 - 0.05) - d2
                gap = z1 - z2
            end
        end
        table.insert(out, {{
            label = p.label,
            x = p.x,
            y = p.y,
            terrain_z = th,
            ray1_hit_z = z1,
            ray2_hit_z = z2,
            double_layer_gap_m = gap,
            ray1_minus_terrain_m = (z1 and th) and (z1 - th) or nil
        }})
    end
    return jsonEncode(out)
    """
    rows = json.loads(mcp.call_tool("run_lua", {"code": lua}))
    for r in rows:
        for k in ("terrain_z", "ray1_hit_z", "ray2_hit_z", "double_layer_gap_m", "ray1_minus_terrain_m"):
            if isinstance(r.get(k), (int, float)):
                r[k] = round(float(r[k]), 4)
    return rows


def probe_navgraph_points(
    mcp: BeamNgMcpClient,
    anchors: list[tuple[str, float, float, float]],
    radius: float = 180.0,
) -> list[dict[str, Any]]:
    """Inspect BeamNG AI ``navgraph`` nodes around key points for continuity and Z=0 bugs."""
    results: list[dict[str, Any]] = []
    for label, x, y, z in anchors:
        try:
            raw = mcp.call_tool("get_navgraph", {"near": {"x": x, "y": y, "z": z}, "radius": radius})
            data = json.loads(raw)
            nodes = data if isinstance(data, list) else data.get("nodes") or []
            zs = [
                float(n["pos"][2] if isinstance(n.get("pos"), list) else n["pos"]["z"])
                for n in nodes
                if isinstance(n, dict) and n.get("pos") is not None
            ]
            zero_z = sum(1 for zv in zs if zv <= 5.0)
            results.append(
                {
                    "label": label,
                    "center": [round(x, 1), round(y, 1), round(z, 1)],
                    "node_count": len(nodes),
                    "zero_z_nodes": zero_z,
                    "min_z": round(min(zs), 2) if zs else None,
                    "max_z": round(max(zs), 2) if zs else None,
                    "raw_keys": list(data.keys()) if isinstance(data, dict) else ["list"],
                }
            )
        except Exception as exc:
            results.append({"label": label, "error": str(exc)})
    return results


def discover_p152_waypoints(
    mcp: BeamNgMcpClient,
    mode: str,
    scene_info: dict[str, Any],
) -> dict[str, Any]:
    """Select drive routes and probe points adapted to the active level bounds."""
    tbs = scene_info.get("terrain_blocks") or []
    tb = tbs[0] if tbs else {}
    min_xy = tb.get("min") or [6000.0, -10000.0, 0.0]
    max_xy = tb.get("max") or [10000.0, -6000.0, 500.0]
    spawn = scene_info.get("spawn_pos") or [7264.12, -8071.68, 107.0]

    # Query navgraph along P152 / highway nodes if available
    def _nav_nodes_near(x: float, y: float, z: float, r: float) -> list[dict[str, Any]]:
        with contextlib.suppress(Exception):
            raw = mcp.call_tool("get_navgraph", {"near": {"x": x, "y": y, "z": z}, "radius": r})
            data = json.loads(raw)
            return data if isinstance(data, list) else (data.get("nodes") or [])
        return []

    def _pick_p152_pt(x: float, y: float, r: float = 350.0) -> tuple[float, float, float] | None:
        nodes = _nav_nodes_near(x, y, 120.0, r)
        best: tuple[float, tuple[float, float, float]] | None = None
        for n in nodes:
            pos = n.get("pos") if isinstance(n, dict) else None
            if not pos:
                continue
            px, py, pz = (
                (float(pos[0]), float(pos[1]), float(pos[2]))
                if isinstance(pos, list)
                else (float(pos["x"]), float(pos["y"]), float(pos["z"]))
            )
            if pz <= 5.0:
                continue
            d = math.hypot(px - x, py - y)
            if best is None or d < best[0]:
                best = (d, (px, py, pz))
        return best[1] if best else None

    if mode == "regression":
        return {
            "legs": [
                {"name": "p152_eastbound", "start": (7264.12, -8071.68, 107.0), "target": (8335.0, -8456.8, 108.0), "speed_ms": 26.5, "max_time_s": 50.0},
                {"name": "p152_westbound", "start": (8335.0, -8456.8, 108.0), "target": (7264.12, -8071.68, 107.0), "speed_ms": 26.5, "max_time_s": 50.0},
            ],
            "probe_points": [
                {"label": "p152_west_approach", "x": 7600.0, "y": -8178.0},
                {"label": "uvod_bridge_deck", "x": 7944.0, "y": -8306.0},
                {"label": "kokhma_se_junc", "x": 8348.0, "y": -8464.4},
                {"label": "open_meadow", "x": 8250.0, "y": -8420.0},
            ],
            "navgraph_anchors": [
                ("spawn", spawn[0], spawn[1], spawn[2]),
                ("uvod_bridge", 7944.0, -8306.0, 109.0),
                ("kokhma_junc", 8348.0, -8464.4, 107.5),
            ],
        }

    # P0 / P1 (16.4x16.4 km centered on Ivanovo-Shuya midpoint) and P2 (~33 km corridor)
    # Dynamically find primary/trunk highway (road_asphalt_2lane) positions in Lua across X slices!
    highway_lua = r"""
    local slices = {4200, 6200, 7944, 8348, 11500, 15000, 18500, 21800, 23700}
    local best = {}
    for i, sx in ipairs(slices) do best[i] = {dist = 1e9, pt = nil} end
    local drs = scenetree.findClassObjects("DecalRoad") or {}
    for _, name in ipairs(drs) do
        local o = scenetree.findObject(name)
        if o and tostring(o.material or "") == "road_asphalt_2lane" then
            local p = o:getPosition()
            if p then
                for i, sx in ipairs(slices) do
                    local d = math.abs(p.x - sx)
                    if d < best[i].dist and d < 1800 then
                        best[i].dist = d
                        best[i].pt = {p.x, p.y, p.z}
                    end
                end
            end
        end
    end
    return jsonEncode(best)
    """
    slice_labels = [
        "ivanovo_exit",
        "kokhma_west",
        "uvod_bridge",
        "kokhma_junc",
        "corridor_11km",
        "corridor_15km",
        "corridor_18km",
        "shuya_approach",
        "shuya_city",
    ]
    in_bounds: list[tuple[str, float, float, float]] = []
    margin = 150.0
    lua_hits: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        lua_hits = json.loads(mcp.call_tool("run_lua", {"code": highway_lua}))

    for idx, lbl in enumerate(slice_labels):
        hit = lua_hits[idx] if idx < len(lua_hits) and isinstance(lua_hits[idx], dict) else {}
        cand = hit.get("pt")
        if not cand:
            continue
        cx, cy = float(cand[0]), float(cand[1])
        if (min_xy[0] + margin) <= cx <= (max_xy[0] - margin) and (min_xy[1] + margin) <= cy <= (max_xy[1] - margin):
            pt = _pick_p152_pt(cx, cy, r=600.0)
            if pt:
                in_bounds.append((lbl, pt[0], pt[1], pt[2]))

    # Always include spawn as first anchor if valid
    probe_points = [{"label": "spawn_road", "x": spawn[0], "y": spawn[1]}]
    for lbl, px, py, _ in in_bounds[:6]:
        probe_points.append({"label": lbl, "x": px, "y": py})
    # Add an off-road point ~120 m perpendicular from spawn/kokhma to test base terrain vs mesh
    probe_points.append({"label": "offroad_south", "x": spawn[0] - 80.0, "y": spawn[1] - 120.0})
    probe_points.append({"label": "offroad_far", "x": spawn[0] + 350.0, "y": spawn[1] + 350.0})

    legs: list[dict[str, Any]] = []
    if mode in ("p0", "p1"):
        # Multi-km corridor drive (> 5 km total) across P152 + return/seam crossing
        if len(in_bounds) >= 3:
            p_start = in_bounds[0]
            p_mid = in_bounds[len(in_bounds) // 2]
            p_end = in_bounds[-1]
            legs.append({"name": f"{p_start[0]}_to_{p_mid[0]}", "start": p_start[1:], "target": p_mid[1:], "speed_ms": 27.0, "max_time_s": 130.0})
            legs.append({"name": f"{p_mid[0]}_to_{p_end[0]}", "start": p_mid[1:], "target": p_end[1:], "speed_ms": 27.0, "max_time_s": 130.0})
        elif len(in_bounds) >= 2:
            legs.append({"name": f"{in_bounds[0][0]}_to_{in_bounds[-1][0]}", "start": in_bounds[0][1:], "target": in_bounds[-1][1:], "speed_ms": 27.0, "max_time_s": 160.0})
        else:
            # Fallback relative to spawn
            legs.append({"name": "spawn_corridor_leg", "start": tuple(spawn), "target": (spawn[0] + 2500.0, spawn[1] - 1250.0, spawn[2]), "speed_ms": 26.0, "max_time_s": 120.0})
    elif mode == "p2":
        # Continuous ~33 km corridor: test Ivanovo -> Kokhma/Uvod, Mid-corridor, and Shuya approach -> Shuya
        # plus full end-to-end map.getPath navgraph continuity probe!
        if len(in_bounds) >= 4:
            p0 = in_bounds[0]
            p_kokhma = next((p for p in in_bounds if p[0] == "kokhma_junc"), in_bounds[len(in_bounds) // 3])
            p_mid = next((p for p in in_bounds if p[0] == "corridor_15km"), in_bounds[len(in_bounds) // 2])
            p_shuya_app = in_bounds[-2]
            p_shuya = in_bounds[-1]
            legs.extend([
                {"name": f"{p0[0]}_to_{p_kokhma[0]}", "start": p0[1:], "target": p_kokhma[1:], "speed_ms": 28.0, "max_time_s": 140.0},
                {"name": f"{p_kokhma[0]}_to_{p_mid[0]}", "start": p_kokhma[1:], "target": p_mid[1:], "speed_ms": 29.0, "max_time_s": 140.0},
                {"name": f"{p_shuya_app[0]}_to_{p_shuya[0]}", "start": p_shuya_app[1:], "target": p_shuya[1:], "speed_ms": 28.0, "max_time_s": 140.0},
            ])
        elif len(in_bounds) >= 2:
            legs.append({"name": f"{in_bounds[0][0]}_to_{in_bounds[-1][0]}", "start": in_bounds[0][1:], "target": in_bounds[-1][1:], "speed_ms": 29.0, "max_time_s": 180.0})
        else:
            legs.append({"name": "spawn_to_east", "start": tuple(spawn), "target": (spawn[0] + 6000.0, spawn[1] - 3000.0, spawn[2]), "speed_ms": 28.0, "max_time_s": 240.0})

    nav_anchors = [("spawn", spawn[0], spawn[1], spawn[2])] + in_bounds

    # For hybrid mode (or whenever ground_mesh_sample_boxes are present), add explicit
    # mesh-interior / mesh-edge / base-terrain seam probe points + seam navgraph check.
    mesh_boxes = scene_info.get("ground_mesh_sample_boxes") or []
    seam_cam_target: tuple[float, float] | None = None
    if mesh_boxes:
        box = mesh_boxes[0]
        bmin = box.get("min") or [spawn[0] - 50.0, spawn[1] - 50.0]
        bmax = box.get("max") or [spawn[0] + 50.0, spawn[1] + 50.0]
        cx = 0.5 * (float(bmin[0]) + float(bmax[0]))
        cy = 0.5 * (float(bmin[1]) + float(bmax[1]))
        edge_y = float(bmax[1])
        seam_cam_target = (cx, edge_y)
        probe_points.extend([
            {"label": "hybrid_mesh_center", "x": cx, "y": cy},
            {"label": "hybrid_mesh_inside_5m", "x": cx, "y": edge_y - 5.0},
            {"label": "hybrid_mesh_seam_edge", "x": cx, "y": edge_y},
            {"label": "hybrid_base_terrain_outside_15m", "x": cx, "y": edge_y + 15.0},
        ])
        nav_anchors.append(("hybrid_seam_edge", cx, edge_y, float(spawn[2])))

    return {
        "legs": legs,
        "probe_points": probe_points,
        "navgraph_anchors": nav_anchors,
        "seam_cam_target": seam_cam_target,
        "far_corridor_points": [
            (lbl, px, py, pz) for lbl, px, py, pz in in_bounds if lbl in ("corridor_15km", "shuya_approach", "shuya_city")
        ],
    }


def probe_end_to_end_nav_path(
    mcp: BeamNgMcpClient,
    anchors: list[tuple[str, float, float, float]],
) -> dict[str, Any]:
    """Verify continuous ``map.getPath`` route across the full corridor."""
    if len(anchors) < 2:
        return {"checked": False}
    a = anchors[0]
    b = anchors[-1]
    lua = f"""
    local n1 = map and map.findClosestRoad and map.findClosestRoad(vec3({a[1]}, {a[2]}, {a[3]})) or nil
    local n2 = map and map.findClosestRoad and map.findClosestRoad(vec3({b[1]}, {b[2]}, {b[3]})) or nil
    local path = (n1 and n2 and map.getPath) and map.getPath(n1, n2) or nil
    local plen = path and #path or 0
    local total_m = 0
    if path and plen > 1 and map.getMap then
        local m = map.getMap()
        local nodes = m and m.nodes or nil
        if nodes then
            for i = 1, plen - 1 do
                local pa = nodes[path[i]] and nodes[path[i]].pos
                local pb = nodes[path[i + 1]] and nodes[path[i + 1]].pos
                if pa and pb then
                    total_m = total_m + (pa - pb):length()
                end
            end
        end
    end
    return jsonEncode({{
        start_label = "{a[0]}",
        end_label = "{b[0]}",
        start_node = n1,
        end_node = n2,
        connected = plen > 1,
        path_nodes = plen,
        path_length_km = total_m / 1000.0
    }})
    """
    try:
        res = json.loads(mcp.call_tool("run_lua", {"code": lua}))
        if isinstance(res.get("path_length_km"), (int, float)):
            res["path_length_km"] = round(float(res["path_length_km"]), 3)
        return res
    except Exception as exc:
        return {"checked": True, "error": str(exc)}


def snap_screenshot(mcp: BeamNgMcpClient, out_dir: Path, filename: str, summary: str) -> str:
    """Capture a lossless PNG screenshot via MCP and save to ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = mcp.call_tool("screenshot", {"jpg": False}).strip().strip('"')
    try:
        src = Path(json.loads(raw).get("path", raw))
    except Exception:
        src = Path(raw)
    for _ in range(35):
        if src.is_file() and src.stat().st_size > 0:
            break
        time.sleep(0.2)
    dst = out_dir / filename
    if src.is_file():
        shutil.copy2(src, dst)
        print(f"Screenshot: {dst} | {summary}", flush=True)
        return str(dst)
    print(f"WARN: Screenshot missing at {src}", flush=True)
    return ""


def look_at(mcp: BeamNgMcpClient, cam_pos: tuple[float, float, float], target_pos: tuple[float, float, float], fov: float = 60.0) -> None:
    dx = target_pos[0] - cam_pos[0]
    dy = target_pos[1] - cam_pos[1]
    dz = target_pos[2] - cam_pos[2]
    lua = f"local q = quatFromDir(vec3({dx},{dy},{dz}):normalized(), vec3(0,0,1)); return jsonEncode({{x=q.x,y=q.y,z=q.z,w=q.w}})"
    rot = json.loads(mcp.call_tool("run_lua", {"code": lua}))
    mcp.call_tool("set_free_camera", {"pos": {"x": cam_pos[0], "y": cam_pos[1], "z": cam_pos[2]}, "rot": rot, "fov": fov})
    time.sleep(0.9)


def run_drive_leg(
    mcp: BeamNgMcpClient,
    *,
    name: str,
    start: tuple[float, float, float],
    target: tuple[float, float, float],
    speed_ms: float,
    max_time_s: float,
    perf_samples: list[dict[str, Any]],
    t0: float,
    teleport_to_start: bool = True,
) -> dict[str, Any]:
    """Drive ``etk800`` via BeamNG AI from ``start`` to ``target``, logging physics & perf."""
    dx = target[0] - start[0]
    dy = target[1] - start[1]
    if teleport_to_start:
        # Place accurately on surface via castRayStatic so vehicle never spawns underground
        place_lua = f"""
        local d = castRayStatic(vec3({start[0]}, {start[1]}, 500), vec3(0, 0, -1), 600)
        local z = (d and d > 0) and (500 - d + 0.45) or {start[2] + 0.5}
        local v = be:getPlayerVehicle(0)
        local q = quatFromDir(vec3({dx}, {dy}, 0):normalized(), vec3(0,0,1))
        v:setPosRot({start[0]}, {start[1]}, z, q.x, q.y, q.z, q.w)
        v:resetBrokenFlexMesh()
        return tostring(z)
        """
        mcp.call_tool("run_lua", {"code": place_lua})
        time.sleep(1.0)

    mcp.call_tool(
        "drive_to",
        {"pos": {"x": target[0], "y": target[1], "z": target[2]}, "speed": speed_ms, "aggression": 0.45},
    )
    with contextlib.suppress(Exception):
        mcp.call_tool(
            "run_lua",
            {
                "code": (
                    f"local v=be:getPlayerVehicle(0); "
                    f"if v then v:queueLuaCommand(\"ai.setSpeedMode('limit'); ai.setSpeed({float(speed_ms)})\") end"
                )
            },
        )

    trace: list[dict[str, Any]] = []
    t_start = time.perf_counter()
    last_perf = 0.0
    distance_m = 0.0
    prev_xy: tuple[float, float] | None = None
    stuck_since: float | None = None
    prev_dmg = 0.0
    damage_events: list[dict[str, Any]] = []

    while time.perf_counter() - t_start < max_time_s:
        st = json.loads(
            mcp.call_tool(
                "run_lua",
                {
                    "code": """
                    local v = be:getPlayerVehicle(0)
                    local p = v:getPosition()
                    local vel = v:getVelocity()
                    return jsonEncode({x=p.x, y=p.y, z=p.z, vz=vel.z, speed_ms=vel:length()})
                    """
                },
            )
        )
        cur_xy = (float(st["x"]), float(st["y"]))
        if prev_xy is not None:
            distance_m += math.hypot(cur_xy[0] - prev_xy[0], cur_xy[1] - prev_xy[1])
        prev_xy = cur_xy

        spd_kmh = float(st["speed_ms"]) * 3.6
        st["speed_kmh"] = round(spd_kmh, 2)
        st["t_s"] = round(time.perf_counter() - t_start, 2)
        trace.append(st)

        if spd_kmh < 2.0 and st["t_s"] > 5.0:
            if stuck_since is None:
                stuck_since = time.perf_counter()
            elif time.perf_counter() - stuck_since > 12.0:
                break
        else:
            stuck_since = None

        now = time.perf_counter()
        if now - last_perf >= 1.0:
            last_perf = now
            with contextlib.suppress(Exception):
                dmg_cur = json.loads(mcp.call_tool("get_vehicle_damage"))
                dsum_cur = float(dmg_cur.get("damageSum", 0) or 0)
                if dsum_cur > prev_dmg + 1.0 and len(damage_events) < 20:
                    damage_events.append({
                        "t_s": st["t_s"],
                        "x": round(cur_xy[0], 2),
                        "y": round(cur_xy[1], 2),
                        "z": round(float(st["z"]), 2),
                        "kmh": st["speed_kmh"],
                        "damage_sum": dsum_cur,
                    })
                prev_dmg = dsum_cur
            row: dict[str, Any] = {"t_rel_s": round(now - t0, 2)}
            nv = query_nvidia_smi()
            if nv:
                row["nv_vram_used_mib"] = nv["vram_used_mib"]
                row["nv_gpu_util_pct"] = nv["gpu_util_pct"]
                row["nv_gpu_clock_mhz"] = nv["gpu_clock_mhz"]
            pm = get_beamng_memory_stats()
            if pm:
                row["proc_working_set_mib"] = pm["working_set_mib"]
                row["proc_private_mib"] = pm["private_bytes_mib"]
                row["proc_total_working_set_mib"] = pm["total_working_set_mib"]
            with contextlib.suppress(Exception):
                row.update(json.loads(mcp.call_tool("run_lua", {"code": _LUA_PERF_SNAPSHOT})))
            perf_samples.append(row)

        if math.hypot(cur_xy[0] - target[0], cur_xy[1] - target[1]) <= 45.0:
            break
        time.sleep(0.15)

    with contextlib.suppress(Exception):
        mcp.call_tool("set_ai", {"mode": "disabled"})

    dmg = json.loads(mcp.call_tool("get_vehicle_damage"))
    abs_vz = sorted(abs(float(s["vz"])) for s in trace)
    p99_vz = abs_vz[max(0, int(len(abs_vz) * 0.99) - 1)] if abs_vz else 0.0
    peak_vz_row = max(trace, key=lambda s: abs(float(s["vz"])), default=None)
    reached = bool(prev_xy and math.hypot(prev_xy[0] - target[0], prev_xy[1] - target[1]) <= 60.0)

    return {
        "leg": name,
        "reached_target": reached,
        "distance_km": round(distance_m / 1000.0, 3),
        "duration_s": round(time.perf_counter() - t_start, 2),
        "samples": len(trace),
        "max_speed_kmh": round(max((s["speed_kmh"] for s in trace), default=0.0), 2),
        "avg_speed_kmh": round(sum(s["speed_kmh"] for s in trace) / max(1, len(trace)), 2),
        "min_vz": round(min((s["vz"] for s in trace), default=0.0), 3),
        "max_vz": round(max((s["vz"] for s in trace), default=0.0), 3),
        "p99_abs_vz": round(p99_vz, 3),
        "peak_vz_pos": [round(peak_vz_row["x"], 1), round(peak_vz_row["y"], 1), round(peak_vz_row["z"], 1)] if peak_vz_row else None,
        "min_z_seen": round(min((s["z"] for s in trace), default=0.0), 2),
        "damage_sum": dmg.get("damageSum", dmg),
        "damage_events": damage_events,
    }


def probe_zfighting_and_materials(mcp: BeamNgMcpClient) -> dict[str, Any]:
    """Inspect DecalRoad ``decalBias`` stratification and material health."""
    lua = r"""
    local drs = scenetree.findClassObjects("DecalRoad") or {}
    local min_bias, max_bias = nil, nil
    local zero_bias = 0
    local over_true_biases = {}
    for _, name in ipairs(drs) do
        local o = scenetree.findObject(name)
        if o then
            local b = tonumber(o.decalBias) or 0
            if min_bias == nil or b < min_bias then min_bias = b end
            if max_bias == nil or b > max_bias then max_bias = b end
            if b <= 0 then zero_bias = zero_bias + 1 end
            if o.overObjects and #over_true_biases < 12 then
                table.insert(over_true_biases, {name=name, mat=tostring(o.material or ""), bias=b, pri=tonumber(o.renderPriority)})
            end
        end
    end
    return jsonEncode({
        decal_road_count = #drs,
        decal_bias_min = min_bias,
        decal_bias_max = max_bias,
        decal_bias_zero_count = zero_bias,
        over_objects_sample = over_true_biases
    })
    """
    return json.loads(mcp.call_tool("run_lua", {"code": lua}))


def capture_mode_screenshots(
    mcp: BeamNgMcpClient,
    mode: str,
    level_id: str,
    out_dir: Path,
    scene_info: dict[str, Any],
    seam_cam_target: tuple[float, float] | None = None,
    far_corridor_points: list[tuple[str, float, float, float]] | None = None,
) -> list[dict[str, str]]:
    """Capture representative multi-angle screenshots for the level and mode."""
    shots: list[dict[str, str]] = []
    with contextlib.suppress(Exception):
        mcp.call_tool("toggle_ui", {"show": False})
        time.sleep(0.3)

    prefix = f"{level_id}_{mode}"
    spawn = scene_info.get("spawn_pos") or [7264.12, -8071.68, 107.0]

    # 1. If Golden Sector / P0 / P1 / P2 covers Uvod bridge & Kokhma SE, capture baseline-matched angles
    if VIEWS_JSON.is_file():
        views = json.loads(VIEWS_JSON.read_text(encoding="utf-8"))
        tb = (scene_info.get("terrain_blocks") or [{}])[0]
        min_xy = tb.get("min") or [0.0, -20000.0]
        max_xy = tb.get("max") or [25000.0, 0.0]
        for key in ("view1_p152_kokhma_junc", "view2_uvod_bridge", "view4_tjunc_closeup"):
            v = views.get(key)
            if not v:
                continue
            px, py = float(v["pos"]["x"]), float(v["pos"]["y"])
            if min_xy[0] <= px <= max_xy[0] and min_xy[1] <= py <= max_xy[1]:
                mcp.call_tool("set_free_camera", {"pos": v["pos"], "rot": v["rot"], "fov": v.get("fov", 60)})
                time.sleep(1.0)
                p = snap_screenshot(mcp, out_dir, f"{prefix}_{key}.png", f"{level_id} ({mode}) {v['description']}")
                if p:
                    shots.append({"name": key, "path": p})

    # 2. Spawn area low + aerial overview
    z_spawn = float(
        mcp.call_tool(
            "run_lua",
            {"code": f"local d=castRayStatic(vec3({spawn[0]},{spawn[1]},500),vec3(0,0,-1),600); return tostring(d and (500-d) or {spawn[2]})"},
        ).strip()
    )
    look_at(mcp, (spawn[0] - 40.0, spawn[1] + 25.0, z_spawn + 8.0), (spawn[0] + 35.0, spawn[1] - 15.0, z_spawn + 1.0), fov=60.0)
    p = snap_screenshot(mcp, out_dir, f"{prefix}_spawn_road.png", f"{level_id} ({mode}) spawn corridor ground-level road & props")
    if p:
        shots.append({"name": "spawn_road", "path": p})

    look_at(mcp, (spawn[0] - 240.0, spawn[1] + 180.0, z_spawn + 150.0), (spawn[0] + 150.0, spawn[1] - 80.0, z_spawn), fov=62.0)
    p = snap_screenshot(mcp, out_dir, f"{prefix}_corridor_aerial.png", f"{level_id} ({mode}) elevated corridor overview")
    if p:
        shots.append({"name": "corridor_aerial", "path": p})

    # 3. Hybrid mesh-to-terrain seam close-up (when ground_mesh_sample_boxes are present)
    if seam_cam_target is not None:
        sx, sy = seam_cam_target
        z_seam = float(
            mcp.call_tool(
                "run_lua",
                {"code": f"local d=castRayStatic(vec3({sx},{sy},500),vec3(0,0,-1),600); return tostring(d and (500-d) or {z_spawn})"},
            ).strip()
        )
        look_at(mcp, (sx - 25.0, sy - 20.0, z_seam + 6.0), (sx + 10.0, sy + 12.0, z_seam), fov=58.0)
        p = snap_screenshot(mcp, out_dir, f"{prefix}_hybrid_seam_closeup.png", f"{level_id} ({mode}) hybrid corridor mesh-to-terrain seam close-up")
        if p:
            shots.append({"name": "hybrid_seam_closeup", "path": p})

    # 4. P2 mid-corridor and Shuya far-end screenshots
    for lbl, fx, fy, fz in far_corridor_points or []:
        z_f = float(
            mcp.call_tool(
                "run_lua",
                {"code": f"local d=castRayStatic(vec3({fx},{fy},500),vec3(0,0,-1),600); return tostring(d and (500-d) or {fz})"},
            ).strip()
        )
        look_at(mcp, (fx - 80.0, fy + 60.0, z_f + 35.0), (fx + 40.0, fy - 30.0, z_f), fov=60.0)
        p = snap_screenshot(mcp, out_dir, f"{prefix}_{lbl}.png", f"{level_id} ({mode}) {lbl} corridor view")
        if p:
            shots.append({"name": lbl, "path": p})

    with contextlib.suppress(Exception):
        mcp.call_tool("toggle_ui", {"show": True})
    mcp.call_tool("set_camera", {"name": "chase"})
    time.sleep(0.3)
    return shots


def sample_live_telemetry(mcp: BeamNgMcpClient, t0: float) -> dict[str, Any]:
    """Capture one 1 Hz sample combining nvidia-smi, process RAM, Lua counters, and engine metrics."""
    row: dict[str, Any] = {"t_rel_s": round(time.perf_counter() - t0, 2)}
    nv = query_nvidia_smi()
    if nv:
        row["nv_vram_used_mib"] = nv["vram_used_mib"]
        row["nv_gpu_util_pct"] = nv["gpu_util_pct"]
        row["nv_gpu_clock_mhz"] = nv["gpu_clock_mhz"]
    pm = get_beamng_memory_stats()
    if pm:
        row["proc_working_set_mib"] = pm["working_set_mib"]
        row["proc_private_mib"] = pm["private_bytes_mib"]
        row["proc_total_working_set_mib"] = pm["total_working_set_mib"]
    with contextlib.suppress(Exception):
        row.update(json.loads(mcp.call_tool("run_lua", {"code": _LUA_PERF_SNAPSHOT})))
    with contextlib.suppress(Exception):
        pm_eng = json.loads(mcp.call_tool("get_performance_metrics"))
        if isinstance(pm_eng, dict):
            row["perf_render_ms"] = pm_eng.get("Render")
            row["perf_shadows_ms"] = pm_eng.get("RenderShadows")
            row["perf_reflections_ms"] = pm_eng.get("RenderReflections")
            row["perf_post_render_ms"] = pm_eng.get("PostRender")
            row["perf_lua_ms"] = pm_eng.get("Lua")
            row["perf_physics_nodes_ms"] = pm_eng.get("PhysicsNodes")
    return row


def _start_frame_dt_recorder(mcp: BeamNgMcpClient) -> None:
    """Install/reset transient per-frame dtReal (ms) recorder via BeamNG ``extensions.hook``."""
    lua = r"""
    if not _G._qa_orig_ext_hook then
      _G._qa_orig_ext_hook = extensions.hook
      _G._qa_dts = {}
      _G._qa_prof_active = false
      extensions.hook = function(funcName, a, b, c, d)
        if _G._qa_prof_active and funcName == "onPreRender" and type(a) == "number" then
          table.insert(_G._qa_dts, a * 1000.0)
        end
        return _G._qa_orig_ext_hook(funcName, a, b, c, d)
      end
    end
    _G._qa_dts = {}
    _G._qa_prof_active = true
    return "ok"
    """
    mcp.call_tool("run_lua", {"code": lua})


def _stop_frame_dt_recorder(mcp: BeamNgMcpClient) -> dict[str, Any]:
    """Stop transient per-frame dtReal recorder and summarize frame-time percentiles/hitches."""
    raw = mcp.call_tool(
        "run_lua",
        {
            "code": (
                "_G._qa_prof_active = false; "
                "local d = _G._qa_dts or {}; "
                "_G._qa_dts = {}; "
                "return jsonEncode(d)"
            )
        },
    )
    dts = [float(x) for x in json.loads(raw) if isinstance(x, (int, float)) and float(x) > 0]
    if not dts:
        return {"frames_total": 0}
    s_dts = sorted(dts)
    n = len(s_dts)

    def _pct(p: float) -> float:
        idx = min(n - 1, max(0, int(math.ceil(n * p)) - 1))
        return s_dts[idx]

    avg_ms = statistics.mean(dts)
    p99_ms = _pct(0.99)
    p999_ms = _pct(0.999)
    return {
        "frames_total": n,
        "frametime_avg_ms": round(avg_ms, 2),
        "frametime_p50_ms": round(_pct(0.50), 2),
        "frametime_p95_ms": round(_pct(0.95), 2),
        "frametime_p99_ms": round(p99_ms, 2),
        "frametime_p999_ms": round(p999_ms, 2),
        "frametime_max_ms": round(max(dts), 2),
        "fps_avg_exact": round(1000.0 / avg_ms, 2) if avg_ms > 0 else None,
        "fps_1pct_low_exact": round(1000.0 / p99_ms, 2) if p99_ms > 0 else None,
        "fps_0_1pct_low_exact": round(1000.0 / p999_ms, 2) if p999_ms > 0 else None,
        "hitches_gt_33ms": sum(1 for x in dts if x > 33.33),
        "hitches_gt_50ms": sum(1 for x in dts if x > 50.0),
        "hitches_gt_100ms": sum(1 for x in dts if x > 100.0),
    }


def _place_vehicle(
    mcp: BeamNgMcpClient,
    pos: tuple[float, float, float],
    dir_xy: tuple[float, float] = (1.0, -0.5),
) -> None:
    """Place player vehicle safely on surface using ``castRayStatic``."""
    lua = f"""
    local d = castRayStatic(vec3({pos[0]}, {pos[1]}, 500), vec3(0, 0, -1), 600)
    local z = (d and d > 0) and (500 - d + 0.45) or {pos[2] + 0.5}
    local v = be:getPlayerVehicle(0)
    if v then
      local q = quatFromDir(vec3({dir_xy[0]}, {dir_xy[1]}, 0):normalized(), vec3(0,0,1))
      v:setPosRot({pos[0]}, {pos[1]}, z, q.x, q.y, q.z, q.w)
      v:resetBrokenFlexMesh()
    end
    return tostring(z)
    """
    mcp.call_tool("run_lua", {"code": lua})
    time.sleep(1.0)


def run_benchmark_window(
    mcp: BeamNgMcpClient,
    *,
    duration_s: float,
    t0: float,
) -> list[dict[str, Any]]:
    """Run a sustained ~1 Hz telemetry sampling window while AI drives on road."""
    samples: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        mcp.call_tool("set_ai", {"mode": "span", "speedMode": "limit", "routeSpeed": 25.0, "aggression": 0.45})
    time.sleep(3.0)
    end_t = time.perf_counter() + duration_s
    while time.perf_counter() < end_t:
        ts = time.perf_counter()
        samples.append(sample_live_telemetry(mcp, t0))
        elapsed = time.perf_counter() - ts
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
    with contextlib.suppress(Exception):
        mcp.call_tool("set_ai", {"mode": "disabled"})
    return samples


def run_regime_drive_profile(
    mcp: BeamNgMcpClient,
    *,
    name: str,
    start: tuple[float, float, float],
    target: tuple[float, float, float],
    speed_ms: float,
    duration_s: float,
    t0: float,
) -> dict[str, Any]:
    """Profile a fixed-duration driving regime with 1 Hz engine telemetry + per-frame dtReal."""
    _place_vehicle(mcp, start, (target[0] - start[0], target[1] - start[1]))
    with contextlib.suppress(Exception):
        mcp.call_tool("set_camera", {"name": "chase"})
        time.sleep(0.4)
    mcp.call_tool("drive_to", {"pos": {"x": target[0], "y": target[1], "z": target[2]}, "speed": speed_ms, "aggression": 0.45})
    with contextlib.suppress(Exception):
        mcp.call_tool(
            "run_lua",
            {"code": f"local v=be:getPlayerVehicle(0); if v then v:queueLuaCommand(\"ai.setSpeedMode('limit'); ai.setSpeed({float(speed_ms)})\") end"},
        )
    time.sleep(2.5)

    _start_frame_dt_recorder(mcp)
    samples: list[dict[str, Any]] = []
    end_t = time.perf_counter() + duration_s
    while time.perf_counter() < end_t:
        ts = time.perf_counter()
        samples.append(sample_live_telemetry(mcp, t0))
        elapsed = time.perf_counter() - ts
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
    frame_stats = _stop_frame_dt_recorder(mcp)
    with contextlib.suppress(Exception):
        mcp.call_tool("set_ai", {"mode": "disabled"})
    summary = summarize_samples(samples)
    summary["frame_dt_stats"] = frame_stats
    return {"regime": name, "summary": summary}


def run_camera_spin_stress(
    mcp: BeamNgMcpClient,
    *,
    name: str,
    pos: tuple[float, float, float],
    t0: float,
    step_deg: float = 15.0,
    total_deg: float = 720.0,
    step_delay_s: float = 0.12,
) -> dict[str, Any]:
    """Spin free camera rapidly in ``step_deg`` increments and measure frame-time hitches."""
    z_ground = float(
        mcp.call_tool(
            "run_lua",
            {"code": f"local d=castRayStatic(vec3({pos[0]},{pos[1]},500),vec3(0,0,-1),600); return tostring(d and (500-d) or {pos[2]})"},
        ).strip()
    )
    cam_z = z_ground + max(8.0, pos[2])
    # Prime camera at yaw=0 first so initial stream-in settles slightly, then spin 720 deg
    rot0 = json.loads(mcp.call_tool("run_lua", {"code": "local q=quatFromDir(vec3(1,0,0),vec3(0,0,1)); return jsonEncode({x=q.x,y=q.y,z=q.z,w=q.w})"}))
    mcp.call_tool("set_free_camera", {"pos": {"x": pos[0], "y": pos[1], "z": cam_z}, "rot": rot0, "fov": 65.0})
    time.sleep(1.0)

    steps = int(round(total_deg / step_deg))
    pre_snap = sample_live_telemetry(mcp, t0)
    _start_frame_dt_recorder(mcp)
    t_spin0 = time.perf_counter()
    for i in range(1, steps + 1):
        ang = math.radians(i * step_deg)
        dx, dy = math.cos(ang), math.sin(ang)
        rot = json.loads(
            mcp.call_tool(
                "run_lua",
                {"code": f"local q=quatFromDir(vec3({dx:.5f},{dy:.5f},-0.12):normalized(),vec3(0,0,1)); return jsonEncode({{x=q.x,y=q.y,z=q.z,w=q.w}})"},
            )
        )
        mcp.call_tool("set_free_camera", {"pos": {"x": pos[0], "y": pos[1], "z": cam_z}, "rot": rot, "fov": 65.0})
        time.sleep(step_delay_s)
    spin_dur = round(time.perf_counter() - t_spin0, 2)
    frame_stats = _stop_frame_dt_recorder(mcp)
    post_snap = sample_live_telemetry(mcp, t0)
    mcp.call_tool("set_camera", {"name": "chase"})
    return {
        "zone": name,
        "pos": [round(pos[0], 1), round(pos[1], 1), round(cam_z, 1)],
        "total_deg": total_deg,
        "step_deg": step_deg,
        "duration_s": spin_dur,
        "frame_dt_stats": frame_stats,
        "telemetry_pre": pre_snap,
        "telemetry_post": post_snap,
    }


def run_stabilization_suite(
    mcp: BeamNgMcpClient,
    *,
    level_id: str,
    t0: float,
    run_roundtrip: bool = False,
) -> dict[str, Any]:
    """Run 3-regime profiling (Urban, Rural, Fast Camera-Spin) + optional continuous round-trip."""
    # Canonical coordinates discovered from ivanovo_shuya_v2 SpawnSpheres & P152 navgraph:
    # - Ivanovo urban center: (-20.48, 18.81, 113.13) -> (-405.0, 2682.0, 128.0) or (2293.0, -3063.0, 127.0)
    # - Shuya urban entrance/center: (22718.39, -16745.86, 104.86) -> (23707.7, -14574.6, 106.8)
    # - Kokhma SE / Uvod crossing: (7605.06, -8258.87, 104.26)
    # - Forest highway: (11472.13, -10145.29, 113.73)
    # - Rural P152 straightaway (Kitovo -> Shuya): (19869.38, -15179.29, 109.28) -> (22718.39, -16745.86, 104.86)
    urban_iv = run_regime_drive_profile(
        mcp,
        name="urban_ivanovo",
        start=(-20.48, 18.81, 113.13),
        target=(2293.2, -3063.5, 127.5),
        speed_ms=13.9,
        duration_s=20.0,
        t0=t0,
    )
    urban_sh = run_regime_drive_profile(
        mcp,
        name="urban_shuya",
        start=(22718.39, -16745.86, 104.86),
        target=(23707.7, -14574.6, 106.8),
        speed_ms=13.9,
        duration_s=20.0,
        t0=t0,
    )
    rural_p152 = run_regime_drive_profile(
        mcp,
        name="rural_p152_corridor",
        start=(14970.97, -12289.27, 127.87),
        target=(19869.38, -15179.29, 109.28),
        speed_ms=27.8,
        duration_s=25.0,
        t0=t0,
    )
    spin_urban = run_camera_spin_stress(
        mcp,
        name="spin_urban_ivanovo",
        pos=(-20.48, 18.81, 18.0),
        t0=t0,
    )
    spin_forest = run_camera_spin_stress(
        mcp,
        name="spin_forest_corridor",
        pos=(11472.13, -10145.29, 22.0),
        t0=t0,
    )

    roundtrip_legs: list[dict[str, Any]] = []
    if run_roundtrip:
        # True unbroken continuous round-trip: Ivanovo -> Kokhma -> Shuya -> Kokhma -> Ivanovo
        # Starts at ivanovo_exit (4204.16, -804.94) so BOTH outbound and inbound cross the twin bridge (4052, -3815).
        # Zero teleportation between intermediate waypoints!
        rt_waypoints = [
            ("rt_1_ivanovo_to_kokhma", (4204.16, -804.94, 130.92), (7605.06, -8258.87, 104.26), 21.0, 900.0),
            ("rt_2_kokhma_to_kitovo", (7605.06, -8258.87, 104.26), (19869.38, -15179.29, 109.28), 26.5, 960.0),
            ("rt_3_kitovo_to_shuya", (19869.38, -15179.29, 109.28), (22718.39, -16745.86, 104.86), 22.0, 320.0),
            ("rt_4_shuya_to_kokhma", (22718.39, -16745.86, 104.86), (7605.06, -8258.87, 104.26), 26.5, 1150.0),
            ("rt_5_kokhma_to_ivanovo", (7605.06, -8258.87, 104.26), (4204.16, -804.94, 130.92), 21.0, 900.0),
        ]
        rt_perf: list[dict[str, Any]] = []
        for idx, (leg_name, s_pt, e_pt, spd, max_s) in enumerate(rt_waypoints):
            leg_res = run_drive_leg(
                mcp,
                name=leg_name,
                start=s_pt,
                target=e_pt,
                speed_ms=spd,
                max_time_s=max_s,
                perf_samples=rt_perf,
                t0=t0,
                teleport_to_start=(idx == 0),
            )
            roundtrip_legs.append(leg_res)

    return {
        "regimes": {
            "urban_ivanovo": urban_iv["summary"],
            "urban_shuya": urban_sh["summary"],
            "rural_p152": rural_p152["summary"],
        },
        "camera_spin_stress": [spin_urban, spin_forest],
        "continuous_roundtrip_legs": roundtrip_legs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live BeamNG.drive 0.40 D3D12 QA & scaling benchmark.")
    parser.add_argument("--level", required=True, help="BeamNG level identifier")
    parser.add_argument("--mode", choices=("regression", "p0", "p1", "p2", "stabilization"), required=True, help="Experiment QA mode")
    parser.add_argument("--out", type=Path, default=None, help="Output JSON path (default: docs/benchmarks/<level>_<mode>.json)")
    parser.add_argument("--user-dir", type=Path, default=DEFAULT_USER_DIR)
    parser.add_argument("--beamng-exe", type=Path, default=DEFAULT_BEAMNG_EXE)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--vehicle", default="etk800")
    parser.add_argument("--install", action="store_true", help="Install build/levels/<level> via bridge before launching")
    parser.add_argument("--cold", action="store_true", help="Clear terrain/level temp cache before launch")
    parser.add_argument("--no-restart", action="store_true", help="Reuse running BeamNG if already on target level")
    parser.add_argument("--duration", type=float, default=25.0, help="Sustained FPS benchmark window seconds")
    parser.add_argument("--load-timeout", type=float, default=240.0, help="Max seconds to wait for worldReadyState")
    parser.add_argument("--screenshot-dir", type=Path, default=DEFAULT_SCREENSHOT_DIR)
    parser.add_argument("--roundtrip", action="store_true", help="Run full unbroken round-trip drive in stabilization mode")
    parser.add_argument("--keep-running", action="store_true", help="Leave BeamNG running after finishing")
    args = parser.parse_args(argv)

    out_path = args.out or (REPO_ROOT / "docs" / "benchmarks" / f"{args.level}_{args.mode}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    bridge = BeamNgBridgeClient()
    health = bridge.get("/health", timeout=5.0)
    if not health.get("ok"):
        raise RuntimeError("BeamNG Host Bridge is not healthy")

    build_level_dir = REPO_ROOT / "build" / "levels" / args.level
    installed_level_dir = args.user_dir / "levels" / args.level
    zip_path = REPO_ROOT / "build" / f"{args.level}.zip"
    if not zip_path.is_file():
        zip_path = args.user_dir / "mods" / f"{args.level}.zip"

    static_budget = inspect_static_budget(
        args.level,
        build_level_dir if build_level_dir.is_dir() else installed_level_dir,
        zip_path if zip_path.is_file() else None,
    )
    extended_static = inspect_extended_static(installed_level_dir if installed_level_dir.is_dir() else build_level_dir)
    settings_info = inspect_settings(args.user_dir)
    nv_baseline = query_nvidia_smi()

    t0 = time.perf_counter()
    wall_ready_s: float | None = None
    mcp_url = args.mcp_url
    status = bridge.get("/status", timeout=10.0)

    if args.no_restart and status.get("running") and status.get("mcp_ready") and status.get("current_level") == args.level:
        mcp_url = str(status.get("mcp_url") or mcp_url)
        wall_ready_s = 0.0
    else:
        rst_res = bridge.post(
            "/beamng/restart",
            {
                "level_id": args.level,
                "vehicle": args.vehicle,
                "install": bool(args.install),
                "clear_cache": bool(args.cold or args.install),
                "wait_ready": True,
                "timeout_s": args.load_timeout,
            },
            timeout=args.load_timeout + 45.0,
        )
        start_res = rst_res.get("start") or {}
        wall_ready_s = float(start_res.get("world_ready_s") or start_res.get("wall_ready_s") or 0.0)
        if start_res.get("mcp_url"):
            mcp_url = str(start_res["mcp_url"])

    mcp = BeamNgMcpClient(url=mcp_url, timeout=15.0)
    if not mcp.initialize():
        raise RuntimeError(f"Failed to connect to BeamNG MCP at {mcp_url}")

    time.sleep(2.5)
    with contextlib.suppress(Exception):
        mcp.call_tool("set_ui_state", {"route": "play"})
        time.sleep(0.5)

    # Ensure etk800 passenger car is active
    vinfo = json.loads(
        mcp.call_tool("run_lua", {"code": "local v=be:getPlayerVehicle(0); return jsonEncode({has=v~=nil, jbeam=v and v.JBeam or nil})"})
    )
    if not vinfo.get("has") or vinfo.get("jbeam") != args.vehicle:
        with contextlib.suppress(Exception):
            mcp.call_tool("spawn", {"model": args.vehicle})
            time.sleep(2.5)

    limit_reason = None
    with contextlib.suppress(Exception):
        limit_reason = mcp.call_tool("get_fps_limit_reason")

    scene_info = probe_scene_lua(mcp)
    zfighting_audit = probe_zfighting_and_materials(mcp)
    plan_mode = "p2" if args.mode == "stabilization" else args.mode
    plan = discover_p152_waypoints(mcp, plan_mode, scene_info)
    surface_probes = probe_surface_and_double_collision(mcp, plan["probe_points"])
    navgraph_probes = probe_navgraph_points(mcp, plan["navgraph_anchors"])
    end_to_end_path = probe_end_to_end_nav_path(mcp, plan["navgraph_anchors"])

    stabilization_data: dict[str, Any] | None = None
    perf_samples: list[dict[str, Any]] = []
    drive_legs: list[dict[str, Any]] = []
    screenshots: list[dict[str, str]] = []

    if args.mode == "stabilization":
        stabilization_data = run_stabilization_suite(
            mcp,
            level_id=args.level,
            t0=t0,
            run_roundtrip=bool(args.roundtrip),
        )
        drive_legs = stabilization_data.get("continuous_roundtrip_legs") or []
        all_samples = []
    else:
        screenshots = capture_mode_screenshots(
            mcp,
            args.mode,
            args.level,
            args.screenshot_dir,
            scene_info,
            seam_cam_target=plan.get("seam_cam_target"),
            far_corridor_points=plan.get("far_corridor_points"),
        )

        for idx, leg in enumerate(plan["legs"]):
            leg_res = run_drive_leg(
                mcp,
                name=leg["name"],
                start=leg["start"],
                target=leg["target"],
                speed_ms=leg["speed_ms"],
                max_time_s=leg["max_time_s"],
                perf_samples=perf_samples,
                t0=t0,
                teleport_to_start=(idx == 0 or args.mode != "p2" or idx == len(plan["legs"]) - 1),
            )
            drive_legs.append(leg_res)

        # Reset vehicle to canonical spawn road location before sustained D3D12 benchmark
        spawn = scene_info.get("spawn_pos") or [7605.06, -8258.87, 104.1]
        with contextlib.suppress(Exception):
            mcp.call_tool(
                "run_lua",
                {
                    "code": (
                        f"local d=castRayStatic(vec3({spawn[0]},{spawn[1]},500),vec3(0,0,-1),600); "
                        f"local z=(d and d>0) and (500-d+0.45) or {spawn[2]+0.5}; "
                        f"local v=be:getPlayerVehicle(0); "
                        f"if v then local q=quatFromDir(vec3(1,-0.5,0):normalized(),vec3(0,0,1)); "
                        f"v:setPosRot({spawn[0]},{spawn[1]},z,q.x,q.y,q.z,q.w); v:resetBrokenFlexMesh() end"
                    )
                },
            )
            time.sleep(1.0)

        # Dedicated sustained D3D12 benchmark window
        bench_samples = run_benchmark_window(mcp, duration_s=args.duration, t0=t0)
        all_samples = bench_samples if bench_samples else perf_samples

    # Stop cleanly before parsing final beamng.log (unless --keep-running)
    if not args.keep_running:
        with contextlib.suppress(Exception):
            bridge.post("/beamng/stop", timeout=20.0)
        time.sleep(1.0)

    log_summary = classify_beamng_log(args.user_dir / "beamng.log", args.user_dir, args.beamng_exe)

    raw_report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "level_id": args.level,
        "mode": args.mode,
        "vehicle": args.vehicle,
        "cold_cache": bool(args.cold or args.install),
        "wall_load_to_ready_s": wall_ready_s,
        "fps_limit_reason": limit_reason,
        "settings": settings_info,
        "nvidia_smi_baseline": nv_baseline,
        "static_budget": {k: getattr(static_budget, k) for k in static_budget.__dataclass_fields__},
        "extended_static": extended_static,
        "scene_probe": scene_info,
        "zfighting_and_materials": zfighting_audit,
        "surface_collision_probes": surface_probes,
        "navgraph_probes": navgraph_probes,
        "end_to_end_nav_path": end_to_end_path,
        "drive_legs": drive_legs,
        "drive_totals": {
            "legs_count": len(drive_legs),
            "total_distance_km": round(sum(l["distance_km"] for l in drive_legs), 3),
            "max_speed_kmh": round(max((l["max_speed_kmh"] for l in drive_legs), default=0.0), 2),
            "max_damage_sum": max((int(l["damage_sum"]) for l in drive_legs if isinstance(l["damage_sum"], (int, float))), default=0),
            "max_abs_vz": round(max((max(abs(l["min_vz"]), abs(l["max_vz"])) for l in drive_legs), default=0.0), 3),
        },
        "perf_summary": summarize_samples(all_samples),
        "stabilization": stabilization_data,
        "log_metrics": log_summary,
        "screenshots_captured": [Path(s["path"]).name for s in screenshots],
    }

    anonymized = anonymize_obj(raw_report, args.user_dir, args.beamng_exe)
    out_path.write_text(json.dumps(anonymized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"REPORT_WRITTEN: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
