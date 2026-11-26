"""Repeatable runtime benchmark and static budget audit for BeamNG.drive levels.

Supports two modes:
1. Static + log analysis (no game launch): inspects level files, mod ZIP size,
   BeamNG graphics settings (1440p/DLSS/VSync check), and parses an existing
   ``beamng.log`` for load stages, static/physics collision memory, and errors.
2. Live D3D12 driving benchmark (``--run``): launches BeamNG.drive 0.40+,
   measures wall-clock load time to ``worldReadyState=1``, optionally engages AI
   or cruise driving, samples ``nvidia-smi`` VRAM, Windows process WorkingSet /
   PrivateUsage RAM, and BeamNG 0.40 MCP/Lua performance counters (FPS, 1% low
   ``$fps::p99``, CPU/GPU ms, draw calls, triangles, Terrain/Forest stats,
   DXGI VRAM), and outputs a structured JSON report.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BEAMNG_EXE = Path(r"F:\SteamLibrary\steamapps\common\BeamNG.drive\Bin64\BeamNG.drive.x64.exe")
DEFAULT_USER_DIR = Path(r"F:\Games\BeamNG.drive\current")
DEFAULT_MCP_URL = "http://127.0.0.1:29292/mcp"

_STAGE_RE = re.compile(
    r"^\s*(\d+\.\d+)\|D\|LoadingManager\|\s+Loaded\s+(\d+)x\s+([\w.:]+)\s+in\s+(\d+\.\d+)s"
)
_STATIC_COL_RE = re.compile(
    r"Loaded Static Collision in (\d+\.\d+)s,\s+Verts:\s+(\d+),\s+Tris:\s+(\d+),\s+Memory:\s+([\d.]+)MB"
)
_PHYS_COL_RE = re.compile(
    r"Physics collision reloaded in (\d+\.\d+)s\s+\((\d+) instances generated in (\d+\.\d+)s,\s+using\s+([\d.]+)mb\)"
)
_FOREST_READ_RE = re.compile(r"Read (\d+) forest items split into (\d+) different types")
_WORLD_READY_RE = re.compile(r"^\s*(\d+\.\d+)\|D\|GELua\.serverConnection\|.*setting worldReadyState to 1")
_SPAWN_FREE_MEM_RE = re.compile(r"available memory:\s+([\d.]+)\s+GB free")


@dataclass(slots=True)
class StaticBudget:
    """On-disk and scene complexity metrics for a built level."""

    level_id: str
    level_dir: str | None = None
    zip_path: str | None = None
    zip_bytes: int = 0
    zip_mib: float = 0.0
    ter_bytes: int = 0
    ter_mib: float = 0.0
    terrain_grid_size: int = 0
    terrain_square_size_m: float = 0.0
    decal_roads: int = 0
    decal_nodes: int = 0
    mesh_roads: int = 0
    forest_items: int = 0
    forest_types: int = 0
    water_blocks: int = 0
    rivers: int = 0
    ts_statics: int = 0
    dae_files: int = 0
    dae_bytes: int = 0


@dataclass(slots=True)
class LogMetrics:
    """Load timings and memory figures parsed from ``beamng.log``."""

    log_path: str
    world_ready_s: float | None = None
    load_stages_s: dict[str, dict[str, Any]] = field(default_factory=dict)
    forest_items_loaded: int | None = None
    forest_types_loaded: int | None = None
    static_collision_verts: int | None = None
    static_collision_tris: int | None = None
    static_collision_mb: float | None = None
    physics_instances: int | None = None
    physics_memory_mb: float | None = None
    free_ram_at_spawn_gb: float | None = None
    error_lines: int = 0
    warning_lines: int = 0


def inspect_static_budget(
    level_id: str,
    level_dir: Path | None,
    zip_path: Path | None,
) -> StaticBudget:
    """Measure mod ZIP size, ``.ter`` size, and scene object counts."""
    budget = StaticBudget(level_id=level_id)

    if zip_path is not None and zip_path.is_file():
        budget.zip_path = str(zip_path)
        budget.zip_bytes = zip_path.stat().st_size
        budget.zip_mib = round(budget.zip_bytes / (1024 * 1024), 2)

    if level_dir is None or not level_dir.is_dir():
        return budget

    budget.level_dir = str(level_dir)
    ter_path = level_dir / "theTerrain.ter"
    if ter_path.is_file():
        budget.ter_bytes = ter_path.stat().st_size
        budget.ter_mib = round(budget.ter_bytes / (1024 * 1024), 2)

    ter_json = level_dir / "theTerrain.terrain.json"
    if ter_json.is_file():
        try:
            tdata = json.loads(ter_json.read_text(encoding="utf-8"))
            budget.terrain_grid_size = int(tdata.get("size", 0))
        except Exception:
            pass

    ter_items = level_dir / "main" / "MissionGroup" / "level_objects" / "terrain" / "items.level.json"
    if ter_items.is_file():
        for line in ter_items.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("class") == "TerrainBlock":
                budget.terrain_square_size_m = float(obj.get("squareSize", 0.0))
                break

    forest_dir = level_dir / "forest"
    if forest_dir.is_dir():
        for fp in forest_dir.glob("*.forest4.json"):
            cnt = sum(1 for ln in fp.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip())
            if cnt > 0:
                budget.forest_items += cnt
                budget.forest_types += 1

    shapes_dir = level_dir / "art" / "shapes"
    if shapes_dir.is_dir():
        daes = list(shapes_dir.rglob("*.dae"))
        budget.dae_files = len(daes)
        budget.dae_bytes = sum(p.stat().st_size for p in daes)

    for item_file in (level_dir / "main").rglob("items.level.json"):
        for line in item_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            cls = obj.get("class")
            if cls == "DecalRoad":
                budget.decal_roads += 1
                budget.decal_nodes += len(obj.get("nodes") or ())
            elif cls == "MeshRoad":
                budget.mesh_roads += 1
            elif cls == "WaterBlock":
                budget.water_blocks += 1
            elif cls == "River":
                budget.rivers += 1
            elif cls == "TSStatic":
                budget.ts_statics += 1

    return budget


def inspect_settings(user_dir: Path) -> dict[str, Any]:
    """Read active BeamNG graphics/display settings relevant to 1440p benchmarking."""
    settings_path = user_dir / "settings" / "settings.json"
    game_settings_path = user_dir / "settings" / "game-settings.json"
    out: dict[str, Any] = {"settings_path": str(settings_path)}

    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            keys = (
                "GraphicDisplayResolutions",
                "GraphicDisplayModes",
                "GraphicDisplayRefreshRates",
                "GraphicAntialiasType",
                "GraphicAntialias",
                "GraphicOverallQuality",
                "GraphicTerrainQuality",
                "GraphicTextureQuality",
                "GraphicMeshQuality",
                "GraphicShadowsQuality",
                "GraphicLightingQuality",
                "GraphicDynReflectionEnabled",
                "GraphicDynReflectionDistance",
                "GraphicDynMirrorsDistance",
                "enableMcp",
                "vsync",
                "fpsLimitEnabled",
                "fpsLimit",
            )
            for k in keys:
                if k in data:
                    out[k] = data[k]
        except Exception as exc:
            out["settings_error"] = str(exc)

    if game_settings_path.is_file():
        try:
            gdata = json.loads(game_settings_path.read_text(encoding="utf-8"))
            pref = gdata.get("$pref", {})
            out["pref_Terrain"] = pref.get("Terrain")
            out["pref_TS"] = pref.get("TS")
            out["pref_Video_displayDevice"] = pref.get("Video", {}).get("displayDevice")
            out["pref_Video_gpu"] = pref.get("Video", {}).get("gpu")
        except Exception:
            pass

    res = str(out.get("GraphicDisplayResolutions", ""))
    if res and res != "2560 1440":
        out["resolution_warning"] = (
            f"Active resolution is '{res}', not '2560 1440' (1440p target). "
            "Set 2560x1440 in BeamNG Display settings before final benchmark."
        )
    return out


def parse_beamng_log(log_path: Path) -> LogMetrics:
    """Extract load times, stage counts, collision memory, and log errors."""
    metrics = LogMetrics(log_path=str(log_path))
    if not log_path.is_file():
        return metrics

    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "|E|" in line:
            metrics.error_lines += 1
        elif "|W|" in line:
            metrics.warning_lines += 1

        m_stage = _STAGE_RE.match(line)
        if m_stage:
            _, count, name, secs = m_stage.groups()
            metrics.load_stages_s[name] = {"count": int(count), "seconds": float(secs)}
            continue

        m_ready = _WORLD_READY_RE.match(line)
        if m_ready:
            metrics.world_ready_s = float(m_ready.group(1))
            continue

        m_forest = _FOREST_READ_RE.search(line)
        if m_forest:
            metrics.forest_items_loaded = int(m_forest.group(1))
            metrics.forest_types_loaded = int(m_forest.group(2))
            continue

        m_sc = _STATIC_COL_RE.search(line)
        if m_sc:
            metrics.static_collision_verts = int(m_sc.group(2))
            metrics.static_collision_tris = int(m_sc.group(3))
            metrics.static_collision_mb = float(m_sc.group(4))
            continue

        m_pc = _PHYS_COL_RE.search(line)
        if m_pc:
            metrics.physics_instances = int(m_pc.group(2))
            metrics.physics_memory_mb = float(m_pc.group(4))
            continue

        m_ram = _SPAWN_FREE_MEM_RE.search(line)
        if m_ram:
            metrics.free_ram_at_spawn_gb = float(m_ram.group(1))

    return metrics


def query_nvidia_smi() -> dict[str, Any] | None:
    """Query GPU VRAM and utilization via ``nvidia-smi``."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu,clocks.gr",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        parts = [p.strip() for p in proc.stdout.strip().splitlines()[0].split(",")]
        if len(parts) < 5:
            return None
        return {
            "gpu_name": parts[0],
            "vram_used_mib": float(parts[1]),
            "vram_total_mib": float(parts[2]),
            "gpu_util_pct": float(parts[3]),
            "gpu_clock_mhz": float(parts[4]),
        }
    except Exception:
        return None


def get_process_memory_mib(pid: int) -> dict[str, float] | None:
    """Query process WorkingSet and PrivateMemory for ``pid`` in MiB."""
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$p = Get-Process -Id {int(pid)} -ErrorAction Stop; "
                "$p.WorkingSet64, $p.PeakWorkingSet64, $p.PrivateMemorySize64 -join ','",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        ws, pws, priv = (float(x) for x in proc.stdout.strip().split(",")[:3])
        mib = 1024.0 * 1024.0
        return {
            "working_set_mib": round(ws / mib, 1),
            "peak_working_set_mib": round(pws / mib, 1),
            "private_bytes_mib": round(priv / mib, 1),
        }
    except Exception:
        return None


class BeamNgMcpClient:
    """Minimal Streamable-HTTP JSON-RPC client for BeamNG 0.40 ``mcp_server``."""

    def __init__(self, url: str = DEFAULT_MCP_URL, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout
        self._req_id = 0
        self.session_id: str | None = None

    def _post(self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._req_id += 1
            payload["id"] = self._req_id
        if params is not None:
            payload["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            raw = resp.read().decode("utf-8", errors="replace").strip()
            if not raw:
                return {}
            if raw.startswith("data:") or "\ndata:" in raw:
                for line in raw.splitlines():
                    if line.startswith("data:"):
                        body = json.loads(line[5:].strip())
                        break
                else:
                    body = {}
            else:
                body = json.loads(raw)
        if "error" in body:
            raise RuntimeError(f"MCP error: {body['error']}")
        return body.get("result")

    def initialize(self) -> bool:
        try:
            self._post(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "ivanovo2beam-bench", "version": "1.0"},
                },
            )
            with contextlib.suppress(Exception):
                self._post("notifications/initialized", notify=True)
            return True
        except Exception:
            return False

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        res = self._post("tools/call", {"name": name, "arguments": arguments or {}})
        content = res.get("content", [])
        if res.get("isError"):
            msg = content[0].get("text", "") if content else "unknown error"
            raise RuntimeError(f"Tool {name} failed: {msg}")
        return content[0].get("text", "") if content else ""


_LUA_PERF_SNAPSHOT = r"""
local function num(k)
  local v = tonumber(VariableRegistry.get(k, -1)) or -1
  if v ~= v or v == math.huge or v == -math.huge then return -1 end
  return v
end
local R = Engine.Render
local dc = num("$GFXDeviceStatistics::drawCalls")
if dc <= 0 and R and R.getDrawCallCount then dc = tonumber(R.getDrawCallCount()) or -1 end
local pc = num("$GFXDeviceStatistics::polyCount")
if pc <= 0 and R and R.getTriangleCount then pc = tonumber(R.getTriangleCount()) or -1 end
local gpuMem = R and R.getMemoryInfo and R.getMemoryInfo() or {}
local vram = R and R.getVRAMUsage and R.getVRAMUsage() or nil
local sysMem = Engine.Platform and Engine.Platform.getMemoryInfo and Engine.Platform.getMemoryInfo() or {}
local veh = be and be:getPlayerVehicle(0)
local pos = veh and veh:getPosition()
local speed = veh and veh:getVelocity():length() or 0
return jsonEncode({
  fps_inst = num("$fps::instantaneous"),
  fps_avg = num("$fps::avg"),
  fps_min = num("$fps::min"),
  fps_p95 = num("$fps::p95"),
  fps_p99 = num("$fps::p99"),
  fps_uncap = num("$fps::instantaneousUncap"),
  cpu_ms = num("$fps::cpuTime"),
  gpu_ms = num("$fps::gpuTime"),
  gpu_wait_ms = num("$fps::waitForGPU"),
  draw_calls = dc,
  poly_count = pc,
  state_changes = num("$GFXDeviceStatistics::drawStateChanges"),
  terrain_cells = num("$TerrainBlock::cellsRendered"),
  terrain_draw_calls = num("$TerrainBlock::drawCalls"),
  forest_total_cells = num("$Forest::totalCells"),
  forest_cells_rendered = num("$Forest::cellsRendered"),
  forest_items_rendered = num("$Forest::cellItemsRendered"),
  forest_items_batched = num("$Forest::cellItemsBatched"),
  shadow_draw_calls = num("$ShadowStats::drawCalls"),
  shadow_polys = num("$ShadowStats::polyCount"),
  gpu_avail_bytes = gpuMem.valid and gpuMem.availableBytes or nil,
  sys_avail_bytes = sysMem.availableBytes or nil,
  vram_usage = vram,
  veh_speed_ms = speed,
  veh_pos = pos and {pos.x, pos.y, pos.z} or nil
})
"""


def _close_process_wm_close(pid: int) -> None:
    """Ask a Windows GUI process to exit cleanly via ``CloseMainWindow()``."""
    with contextlib.suppress(Exception):
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$p = Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue; "
                "if ($p) { [void]$p.CloseMainWindow() }",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )


def _summarize_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate time-series samples into benchmark summary statistics."""
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
    vram_vals = col("nv_vram_used_mib")
    ram_vals = col("proc_working_set_mib")
    priv_vals = col("proc_private_mib")

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
        "draw_calls_avg": round(statistics.mean(dc_vals), 1) if dc_vals else None,
        "draw_calls_max": int(max(dc_vals)) if dc_vals else None,
        "polys_avg": int(statistics.mean(poly_vals)) if poly_vals else None,
        "polys_max": int(max(poly_vals)) if poly_vals else None,
        "vram_peak_mib": round(max(vram_vals), 1) if vram_vals else None,
        "vram_avg_mib": round(statistics.mean(vram_vals), 1) if vram_vals else None,
        "proc_ram_peak_mib": round(max(ram_vals), 1) if ram_vals else None,
        "proc_private_peak_mib": round(max(priv_vals), 1) if priv_vals else None,
        "pass_60fps_target": bool(avg_fps is not None and avg_fps >= 60.0),
    }


def run_live_benchmark(
    *,
    beamng_exe: Path,
    user_dir: Path,
    level_id: str,
    vehicle: str,
    warmup_s: float,
    measure_s: float,
    drive_mode: str,
    camera: str,
    mcp_url: str,
    load_timeout_s: float,
    render_scale: float | None = None,
) -> dict[str, Any]:
    """Launch BeamNG, drive deterministically, collect telemetry, and exit cleanly."""
    if not beamng_exe.is_file():
        raise FileNotFoundError(f"BeamNG executable not found: {beamng_exe}")

    log_path = user_dir / "beamng.log"
    old_log_head = log_path.read_text(encoding="utf-8", errors="ignore")[:256] if log_path.is_file() else ""
    cmd = [str(beamng_exe), "-enablemcp", "-level", level_id, "-vehicle", vehicle]
    game_cwd = beamng_exe.parent.parent
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd=str(game_cwd) if game_cwd.is_dir() else None)
    samples: list[dict[str, Any]] = []
    limit_reason: str | None = None
    world_ready_wall_s: float | None = None
    mcp = BeamNgMcpClient(url=mcp_url)
    mcp_ready = False

    try:
        # 1. Wait for MCP endpoint, then poll worldReadyState >= 1 (beamng.log is stdio-buffered)
        deadline = t0 + load_timeout_s
        base_port_match = re.search(r":(\d+)", mcp_url)
        base_port = int(base_port_match.group(1)) if base_port_match else 29292

        while time.perf_counter() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"BeamNG exited prematurely with code {proc.returncode}")

            if not mcp_ready:
                for port in range(base_port, base_port + 8):
                    candidate = BeamNgMcpClient(url=f"http://127.0.0.1:{port}/mcp")
                    if candidate.initialize():
                        mcp = candidate
                        mcp_ready = True
                        break

            if mcp_ready:
                with contextlib.suppress(Exception):
                    wrs_raw = mcp.call_tool("run_lua", {"code": "return tostring(worldReadyState or 0)"})
                    wrs = int(float(wrs_raw.strip()))
                    if wrs >= 1:
                        if world_ready_wall_s is None:
                            world_ready_wall_s = round(time.perf_counter() - t0, 2)
                        if wrs >= 2 or (time.perf_counter() - t0 - world_ready_wall_s) >= 5.0:
                            break
            elif log_path.is_file():
                text = log_path.read_text(encoding="utf-8", errors="ignore")
                if text and text[:256] != old_log_head and "setting worldReadyState to 1" in text:
                    world_ready_wall_s = round(time.perf_counter() - t0, 2)
                    break

            time.sleep(0.5)

        if world_ready_wall_s is None:
            raise TimeoutError(f"Timed out after {load_timeout_s}s waiting for level load")

        # Give vehicle cameras/scripts 3s to finish registering
        time.sleep(3.0)

        if mcp_ready:
            if render_scale is not None:
                with contextlib.suppress(Exception):
                    mcp.call_tool(
                        "run_lua",
                        {
                            "code": (
                                f"local rv=RenderViewManager and RenderViewManager.getView('main') "
                                f"if rv then rv.upscaleRenderScale={float(render_scale)} end"
                            )
                        },
                    )
            with contextlib.suppress(Exception):
                limit_reason = mcp.call_tool("get_fps_limit_reason")
            with contextlib.suppress(Exception):
                mcp.call_tool("set_camera", {"name": camera})
            if drive_mode == "ai":
                with contextlib.suppress(Exception):
                    mcp.call_tool(
                        "set_ai",
                        {"mode": "span", "speedMode": "limit", "routeSpeed": 25.0, "aggression": 0.4},
                    )
            elif drive_mode == "cruise":
                with contextlib.suppress(Exception):
                    mcp.call_tool(
                        "run_lua",
                        {
                            "code": (
                                "local v=be:getPlayerVehicle(0) "
                                "if v then v:queueLuaCommand('input.event(\"throttle\", 0.35, 1)') end"
                            )
                        },
                    )

        time.sleep(max(0.0, warmup_s))

        # 2. Sample window at ~1 Hz
        end_measure = time.perf_counter() + measure_s
        while time.perf_counter() < end_measure:
            t_sample = time.perf_counter()
            row: dict[str, Any] = {"t_rel_s": round(t_sample - t0, 2)}

            nv = query_nvidia_smi()
            if nv:
                row["nv_vram_used_mib"] = nv["vram_used_mib"]
                row["nv_gpu_util_pct"] = nv["gpu_util_pct"]
                row["nv_gpu_clock_mhz"] = nv["gpu_clock_mhz"]

            pmem = get_process_memory_mib(proc.pid)
            if pmem:
                row["proc_working_set_mib"] = pmem["working_set_mib"]
                row["proc_private_mib"] = pmem["private_bytes_mib"]

            if mcp_ready:
                try:
                    raw = mcp.call_tool("run_lua", {"code": _LUA_PERF_SNAPSHOT})
                    lua_data = json.loads(raw)
                    row.update(lua_data)
                except Exception:
                    pass

            samples.append(row)
            elapsed = time.perf_counter() - t_sample
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)

    finally:
        if mcp_ready and drive_mode == "cruise":
            with contextlib.suppress(Exception):
                mcp.call_tool(
                    "run_lua",
                    {
                        "code": (
                            "local v=be:getPlayerVehicle(0) "
                            "if v then v:queueLuaCommand('input.event(\"throttle\", 0.0, 1)') end"
                        )
                    },
                )
        _close_process_wm_close(proc.pid)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()

    return {
        "wall_load_to_ready_s": world_ready_wall_s,
        "mcp_connected": mcp_ready,
        "fps_limit_reason": limit_reason,
        "summary": _summarize_samples(samples),
        "samples": samples,
    }


def _resolve_level_paths(
    level_id: str,
    build_dir: Path,
    user_dir: Path,
) -> tuple[Path | None, Path | None]:
    """Locate built/deployed level directory and mod ZIP for ``level_id``."""
    candidates_dir = [
        build_dir / "levels" / level_id,
        user_dir / "levels" / level_id,
    ]
    level_dir = next((p for p in candidates_dir if p.is_dir()), None)

    candidates_zip = [
        build_dir / f"{level_id}.zip",
        user_dir / "mods" / f"{level_id}.zip",
    ]
    zip_path = next((p for p in candidates_zip if p.is_file()), None)
    return level_dir, zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark and audit BeamNG.drive level performance.")
    parser.add_argument("--level", default="ivanovo_golden_sector", help="BeamNG level id (default: ivanovo_golden_sector)")
    parser.add_argument("--build-dir", type=Path, default=Path("build"), help="Repo build directory")
    parser.add_argument("--user-dir", type=Path, default=DEFAULT_USER_DIR, help="BeamNG user folder")
    parser.add_argument("--beamng-exe", type=Path, default=DEFAULT_BEAMNG_EXE, help="BeamNG.drive.x64.exe path")
    parser.add_argument("--log", type=Path, default=None, help="beamng.log path (defaults to <user-dir>/beamng.log)")
    parser.add_argument("--run", action="store_true", help="Launch BeamNG and collect live driving metrics")
    parser.add_argument("--vehicle", default="pickup", help="Vehicle to spawn in --run mode")
    parser.add_argument("--camera", default="driver", help="Camera mode during --run (driver, chase, orbit)")
    parser.add_argument("--drive-mode", choices=("idle", "ai", "cruise"), default="ai", help="Driving mode during --run")
    parser.add_argument("--warmup", type=float, default=5.0, help="Warmup seconds after spawn before sampling")
    parser.add_argument("--duration", type=float, default=30.0, help="Measurement window duration in seconds")
    parser.add_argument("--load-timeout", type=float, default=180.0, help="Max seconds to wait for worldReadyState=1")
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL, help="BeamNG MCP HTTP URL")
    parser.add_argument("--render-scale", type=float, default=None, help="Optional ephemeral main RenderView upscaleRenderScale (e.g. 1.0)")
    parser.add_argument("--out", type=Path, default=None, help="Output JSON report path")
    args = parser.parse_args(argv)

    level_dir, zip_path = _resolve_level_paths(args.level, args.build_dir, args.user_dir)
    static_budget = inspect_static_budget(args.level, level_dir, zip_path)
    settings_info = inspect_settings(args.user_dir)
    nv_baseline = query_nvidia_smi()

    live_result: dict[str, Any] | None = None
    if args.run:
        live_result = run_live_benchmark(
            beamng_exe=args.beamng_exe,
            user_dir=args.user_dir,
            level_id=args.level,
            vehicle=args.vehicle,
            warmup_s=args.warmup,
            measure_s=args.duration,
            drive_mode=args.drive_mode,
            camera=args.camera,
            mcp_url=args.mcp_url,
            load_timeout_s=args.load_timeout,
            render_scale=args.render_scale,
        )

    log_path = args.log or (args.user_dir / "beamng.log")
    log_metrics = parse_beamng_log(log_path)

    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "level_id": args.level,
        "static_budget": asdict(static_budget),
        "settings": settings_info,
        "nvidia_smi_baseline": nv_baseline,
        "log_metrics": asdict(log_metrics),
        "live_benchmark": live_result,
    }

    out_path = args.out or (
        args.build_dir / "benchmarks" / f"{args.level}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Benchmark report written to {out_path}")
    print(json.dumps({
        "static_budget": report["static_budget"],
        "log_summary": {
            "world_ready_s": log_metrics.world_ready_s,
            "static_collision_mb": log_metrics.static_collision_mb,
            "physics_memory_mb": log_metrics.physics_memory_mb,
            "error_lines": log_metrics.error_lines,
        },
        "live_summary": live_result["summary"] if live_result else None,
        "resolution_warning": settings_info.get("resolution_warning"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
