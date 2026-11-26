"""Localhost-only BeamNG.drive host bridge for autonomous dev/QA loops.

Workaround for Windows Antigravity ``run_command`` running under an isolated
``WinSta0\\exebox-*`` desktop (see google-antigravity/antigravity-cli#1575),
where spawning GUI executables fails with ``0xC0000142`` and BeamNG file locks
block level overwrites until the game process exits cleanly.

Security model:
- Binds strictly to ``127.0.0.1`` (loopback only).
- Requires an auto-generated per-session ``X-Bridge-Token`` written to
  ``.tmp/beamng_bridge.json`` for mutating ``POST`` endpoints.
- Exposes only a fixed allowlist of BeamNG operations (no arbitrary shell
  execution, no arbitrary path reads/writes, no privilege elevation).

Usage (run once in a normal desktop PowerShell outside Antigravity)::

    .\\.venv\\Scripts\\python.exe scripts\\beamng_host_bridge.py serve

Client commands (usable autonomously from Antigravity ``run_command``)::

    python scripts/beamng_host_bridge.py status
    python scripts/beamng_host_bridge.py restart --level ivanovo_golden_sector --install --clear-cache --wait-ready
    python scripts/beamng_host_bridge.py logs --tail 80
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from benchmark_level import (  # noqa: E402
    DEFAULT_BEAMNG_EXE,
    DEFAULT_USER_DIR,
    BeamNgMcpClient,
    parse_beamng_log,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 29300
STATE_FILE = REPO_ROOT / ".tmp" / "beamng_bridge.json"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_safe_id(value: str, label: str = "identifier") -> str:
    """Validate that ``value`` is a single safe identifier (no path traversal)."""
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r} (allowed: [A-Za-z0-9_-], len 1..80)")
    return value


def find_beamng_pids() -> list[int]:
    """Return PIDs of running ``BeamNG.drive.x64.exe`` processes on Windows."""
    if sys.platform != "win32":
        return []
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process -Name 'BeamNG.drive.x64' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    pids: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def find_active_mcp(base_port: int = 29292, span: int = 8) -> tuple[str | None, BeamNgMcpClient | None]:
    """Probe ``http://127.0.0.1:<port>/mcp`` and return the active URL & client."""
    for port in range(base_port, base_port + span):
        url = f"http://127.0.0.1:{port}/mcp"
        client = BeamNgMcpClient(url=url, timeout=4.0)
        if client.initialize():
            return url, client
    return None, None


class BeamNgHostController:
    """Allowlisted controller managing BeamNG builds, process, logs, and MCP."""

    def __init__(
        self,
        *,
        repo_root: Path = REPO_ROOT,
        beamng_exe: Path = DEFAULT_BEAMNG_EXE,
        user_dir: Path = DEFAULT_USER_DIR,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.beamng_exe = beamng_exe
        self.user_dir = user_dir
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._last_proc: subprocess.Popen[Any] | None = None
        self._last_exit_code: int | None = None
        self._last_error: str | None = None

    def _poll_proc(self) -> int | None:
        if self._last_proc is not None:
            rc = self._last_proc.poll()
            if rc is not None:
                self._last_exit_code = rc
        return self._last_exit_code

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started_at, 1),
            "repo_root": str(self.repo_root),
            "beamng_exe": str(self.beamng_exe),
            "beamng_exe_exists": self.beamng_exe.is_file(),
            "user_dir": str(self.user_dir),
            "last_exit_code": self._poll_proc(),
            "last_error": self._last_error,
        }

    def status(self, *, probe_mcp: bool = True) -> dict[str, Any]:
        pids = find_beamng_pids()
        mcp_url = None
        world_ready_state = None
        current_level = None
        if probe_mcp and pids:
            mcp_url, client = find_active_mcp()
            if client is not None:
                with contextlib.suppress(Exception):
                    raw = client.call_tool(
                        "run_lua",
                        {
                            "code": (
                                "return jsonEncode({"
                                "wrs = worldReadyState or 0, "
                                "level = getCurrentLevelIdentifier and getCurrentLevelIdentifier() or nil"
                                "})"
                            )
                        },
                    )
                    parsed = json.loads(raw)
                    world_ready_state = parsed.get("wrs")
                    current_level = parsed.get("level")

        levels_dir = self.user_dir / "levels"
        installed_levels = sorted(p.name for p in levels_dir.iterdir() if p.is_dir()) if levels_dir.is_dir() else []

        return {
            "running": bool(pids),
            "pids": pids,
            "last_exit_code": self._poll_proc(),
            "last_error": self._last_error,
            "mcp_ready": mcp_url is not None,
            "mcp_url": mcp_url,
            "world_ready_state": world_ready_state,
            "current_level": current_level,
            "installed_levels": installed_levels,
        }

    def stop_beamng(self, *, timeout_s: float = 12.0) -> dict[str, Any]:
        with self._lock:
            pids = find_beamng_pids()
            if not pids:
                return {"stopped": True, "already_stopped": True, "exit_code": self._poll_proc()}

            # 1. Try graceful MCP Lua quit() first if MCP is up.
            _, client = find_active_mcp()
            if client is not None:
                with contextlib.suppress(Exception):
                    client.call_tool("run_lua", {"code": "quit()"})

            deadline = time.perf_counter() + min(4.0, timeout_s)
            while time.perf_counter() < deadline:
                if not find_beamng_pids():
                    return {"stopped": True, "method": "mcp_quit", "exit_code": self._poll_proc()}
                time.sleep(0.3)

            # 2. Ask GUI windows to close cleanly, then force-stop if still hung.
            if sys.platform == "win32":
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "Get-Process -Name 'BeamNG.drive*' -ErrorAction SilentlyContinue | ForEach-Object { [void]$_.CloseMainWindow() }",
                    ],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                deadline2 = time.perf_counter() + max(2.0, timeout_s - 4.0)
                while time.perf_counter() < deadline2:
                    if not find_beamng_pids():
                        return {"stopped": True, "method": "wm_close", "exit_code": self._poll_proc()}
                    time.sleep(0.3)

                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "Stop-Process -Name 'BeamNG.drive*' -Force -ErrorAction SilentlyContinue",
                    ],
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
                time.sleep(1.0)

            rem = find_beamng_pids()
            return {"stopped": not rem, "remaining_pids": rem, "method": "force_stop", "exit_code": self._poll_proc()}

    def clear_cache(self, *, level_id: str | None = None, clear_materials: bool = True, clear_foliage: bool = False) -> dict[str, Any]:
        removed: list[str] = []
        if level_id is not None:
            safe_id = validate_safe_id(level_id, "level_id")
            lvl_temp = self.user_dir / "temp" / "levels" / safe_id
            if lvl_temp.exists():
                shutil.rmtree(lvl_temp, ignore_errors=True)
                removed.append(str(lvl_temp))
        if clear_materials:
            mat_cache = self.user_dir / "temp" / "art" / "terrainMaterialCache"
            if mat_cache.exists():
                shutil.rmtree(mat_cache, ignore_errors=True)
                removed.append(str(mat_cache))
        if clear_foliage:
            fol_cache = self.user_dir / "temp" / "assets" / "meshes" / "foliage"
            if fol_cache.exists():
                shutil.rmtree(fol_cache, ignore_errors=True)
                removed.append(str(fol_cache))
        return {"cleared": removed}

    def install_level(
        self,
        level_id: str,
        *,
        include_zip: bool = False,
        remove_mod_zip: bool = True,
        clear_cache: bool = True,
        stop_if_running: bool = True,
    ) -> dict[str, Any]:
        safe_id = validate_safe_id(level_id, "level_id")
        src_dir = self.repo_root / "build" / "levels" / safe_id
        if not src_dir.is_dir():
            raise FileNotFoundError(f"Built level directory not found: {src_dir}")

        if stop_if_running and find_beamng_pids():
            self.stop_beamng()
            # Brief pause so Windows releases NTFS delete/directory handles cleanly.
            time.sleep(1.0)

        with self._lock:
            dst_dir = self.user_dir / "levels" / safe_id
            mod_zip_dst = self.user_dir / "mods" / f"{safe_id}.zip"
            if remove_mod_zip and mod_zip_dst.is_file():
                mod_zip_dst.unlink()

            if dst_dir.exists():
                shutil.rmtree(dst_dir)
                # Retry briefly if Windows Explorer / Defender still holds a delete-pending handle.
                for _ in range(10):
                    if not dst_dir.exists():
                        break
                    time.sleep(0.25)

            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)

            copied_zip = None
            src_zip = self.repo_root / "build" / f"{safe_id}.zip"
            if include_zip and src_zip.is_file():
                mod_zip_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_zip, mod_zip_dst)
                copied_zip = str(mod_zip_dst)

            cache_res = (
                self.clear_cache(level_id=safe_id, clear_materials=True, clear_foliage=False)
                if clear_cache
                else {"cleared": []}
            )
            file_count = sum(1 for p in dst_dir.rglob("*") if p.is_file())
            total_bytes = sum(p.stat().st_size for p in dst_dir.rglob("*") if p.is_file())
            return {
                "installed": True,
                "level_id": safe_id,
                "source": str(src_dir),
                "target": str(dst_dir),
                "mod_zip": copied_zip,
                "files": file_count,
                "bytes": total_bytes,
                **cache_res,
            }

    def start_beamng(
        self,
        *,
        level_id: str | None = None,
        vehicle: str = "etk800",
        wait_ready: bool = True,
        timeout_s: float = 150.0,
        restart_if_running: bool = False,
    ) -> dict[str, Any]:
        if not self.beamng_exe.is_file():
            raise FileNotFoundError(f"BeamNG executable not found: {self.beamng_exe}")

        safe_level = validate_safe_id(level_id, "level_id") if level_id else None
        safe_vehicle = validate_safe_id(vehicle, "vehicle") if vehicle else None

        if find_beamng_pids():
            if not restart_if_running:
                return {"already_running": True, **self.status(probe_mcp=True)}
            self.stop_beamng()

        cmd = [str(self.beamng_exe), "-enablemcp"]
        if safe_level:
            cmd.extend(["-level", safe_level])
        if safe_vehicle:
            cmd.extend(["-vehicle", safe_vehicle])

        game_cwd = self.beamng_exe.parent.parent
        t0 = time.perf_counter()
        with self._lock:
            self._last_error = None
            self._last_exit_code = None
            self._last_proc = subprocess.Popen(cmd, cwd=str(game_cwd) if game_cwd.is_dir() else None)
            launched_pid = self._last_proc.pid

        if not wait_ready:
            return {"launched": True, "pid": launched_pid, "cmd": cmd}

        deadline = t0 + float(timeout_s)
        world_ready_s: float | None = None
        mcp_url: str | None = None
        client: BeamNgMcpClient | None = None

        while time.perf_counter() < deadline:
            if self._last_proc is not None:
                rc = self._last_proc.poll()
                if rc is not None:
                    self._last_exit_code = rc
                    self._last_error = f"BeamNG exited prematurely with code {rc}"
                    raise RuntimeError(self._last_error)

            if client is None:
                mcp_url, client = find_active_mcp()
            if client is not None:
                with contextlib.suppress(Exception):
                    wrs_raw = client.call_tool("run_lua", {"code": "return tostring(worldReadyState or 0)"})
                    wrs = int(float(wrs_raw.strip()))
                    if wrs >= 1:
                        if world_ready_s is None:
                            world_ready_s = round(time.perf_counter() - t0, 2)
                        if wrs >= 2 or (time.perf_counter() - t0 - world_ready_s) >= 4.0:
                            return {
                                "launched": True,
                                "pid": launched_pid,
                                "mcp_url": mcp_url,
                                "world_ready_state": wrs,
                                "world_ready_s": world_ready_s,
                                "wall_ready_s": round(time.perf_counter() - t0, 2),
                            }
            time.sleep(0.5)

        raise TimeoutError(f"Timed out after {timeout_s:.1f}s waiting for BeamNG MCP / worldReadyState")

    def get_logs(self, *, tail: int = 120, only_errors: bool = False) -> dict[str, Any]:
        log_path = self.user_dir / "beamng.log"
        if not log_path.is_file():
            return {"exists": False, "log_path": str(log_path), "lines": []}

        text = log_path.read_text(encoding="utf-8", errors="ignore")
        all_lines = text.splitlines()
        if only_errors:
            filtered = [ln for ln in all_lines if "|E|" in ln or "|W|" in ln]
        else:
            filtered = all_lines
        n = max(1, min(2000, int(tail)))
        metrics = parse_beamng_log(log_path)
        return {
            "exists": True,
            "log_path": str(log_path),
            "total_lines": len(all_lines),
            "metrics": {
                "world_ready_s": metrics.world_ready_s,
                "error_lines": metrics.error_lines,
                "warning_lines": metrics.warning_lines,
                "forest_items_loaded": metrics.forest_items_loaded,
                "static_collision_mb": metrics.static_collision_mb,
                "physics_memory_mb": metrics.physics_memory_mb,
            },
            "lines": filtered[-n:],
        }

    def mcp_call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        validate_safe_id(tool_name, "mcp_tool")
        url, client = find_active_mcp()
        if client is None:
            raise RuntimeError("No active BeamNG MCP server on 127.0.0.1:29292..29299")
        res = client.call_tool(tool_name, arguments or {})
        return {"mcp_url": url, "tool": tool_name, "result": res}


def _make_handler(controller: BeamNgHostController, auth_token: str) -> type[http.server.BaseHTTPRequestHandler]:
    class _BridgeHandler(http.server.BaseHTTPRequestHandler):
        server_version = "BeamNgHostBridge/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            sys.stderr.write(f"[bridge] {format % args}\n")

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_auth(self) -> bool:
            got = self.headers.get("X-Bridge-Token", "")
            if not secrets.compare_digest(got, auth_token):
                self._send_json(403, {"ok": False, "error": "Invalid or missing X-Bridge-Token"})
                return False
            return True

        def _read_json_body(self) -> dict[str, Any]:
            raw_len = int(self.headers.get("Content-Length", "0") or "0")
            if raw_len > 1_048_576:
                raise ValueError("Request body exceeds 1 MiB limit")
            if raw_len <= 0:
                return {}
            data = self.rfile.read(raw_len)
            parsed = json.loads(data.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def do_GET(self) -> None:  # noqa: N802
            path, _, query = self.path.partition("?")
            try:
                if path == "/health":
                    self._send_json(200, controller.health())
                elif path == "/status":
                    probe = "probe_mcp=0" not in query
                    self._send_json(200, {"ok": True, **controller.status(probe_mcp=probe)})
                elif path == "/logs":
                    params = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
                    tail = int(params.get("tail", "120"))
                    only_err = params.get("only_errors", "0") in ("1", "true", "True")
                    self._send_json(200, {"ok": True, **controller.get_logs(tail=tail, only_errors=only_err)})
                else:
                    self._send_json(404, {"ok": False, "error": f"Unknown GET endpoint: {path}"})
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_auth():
                return
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json_body()
                if path == "/install":
                    res = controller.install_level(
                        str(body["level_id"]),
                        include_zip=bool(body.get("include_zip", False)),
                        remove_mod_zip=bool(body.get("remove_mod_zip", True)),
                        clear_cache=bool(body.get("clear_cache", True)),
                        stop_if_running=bool(body.get("stop_if_running", True)),
                    )
                    self._send_json(200, {"ok": True, **res})
                elif path == "/clear-cache":
                    res = controller.clear_cache(
                        level_id=str(body["level_id"]) if body.get("level_id") else None,
                        clear_materials=bool(body.get("clear_materials", True)),
                        clear_foliage=bool(body.get("clear_foliage", False)),
                    )
                    self._send_json(200, {"ok": True, **res})
                elif path == "/beamng/start":
                    res = controller.start_beamng(
                        level_id=str(body["level_id"]) if body.get("level_id") else None,
                        vehicle=str(body.get("vehicle", "etk800")),
                        wait_ready=bool(body.get("wait_ready", True)),
                        timeout_s=float(body.get("timeout_s", 150.0)),
                        restart_if_running=bool(body.get("restart_if_running", False)),
                    )
                    self._send_json(200, {"ok": True, **res})
                elif path == "/beamng/stop":
                    res = controller.stop_beamng(timeout_s=float(body.get("timeout_s", 12.0)))
                    self._send_json(200, {"ok": True, **res})
                elif path == "/beamng/restart":
                    controller.stop_beamng(timeout_s=float(body.get("stop_timeout_s", 12.0)))
                    install_res = None
                    if body.get("install") and body.get("level_id"):
                        install_res = controller.install_level(
                            str(body["level_id"]),
                            include_zip=bool(body.get("include_zip", False)),
                            remove_mod_zip=bool(body.get("remove_mod_zip", True)),
                            clear_cache=bool(body.get("clear_cache", True)),
                            stop_if_running=False,
                        )
                    elif body.get("clear_cache"):
                        controller.clear_cache(
                            level_id=str(body["level_id"]) if body.get("level_id") else None,
                            clear_materials=True,
                            clear_foliage=bool(body.get("clear_foliage", False)),
                        )
                    start_res = controller.start_beamng(
                        level_id=str(body["level_id"]) if body.get("level_id") else None,
                        vehicle=str(body.get("vehicle", "etk800")),
                        wait_ready=bool(body.get("wait_ready", True)),
                        timeout_s=float(body.get("timeout_s", 150.0)),
                        restart_if_running=False,
                    )
                    self._send_json(200, {"ok": True, "install": install_res, "start": start_res})
                elif path == "/mcp/call":
                    res = controller.mcp_call(
                        str(body["tool"]),
                        body.get("arguments") if isinstance(body.get("arguments"), dict) else {},
                    )
                    self._send_json(200, {"ok": True, **res})
                else:
                    self._send_json(404, {"ok": False, "error": f"Unknown POST endpoint: {path}"})
            except (KeyError, ValueError, FileNotFoundError) as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                controller._last_error = str(exc)
                self._send_json(500, {"ok": False, "error": str(exc)})

    return _BridgeHandler


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class BeamNgBridgeClient:
    """Thin client for talking to a running ``BeamNgHostBridge`` server."""

    def __init__(self, url: str | None = None, token: str | None = None, *, state_file: Path = STATE_FILE) -> None:
        if (url is None or token is None) and state_file.is_file():
            info = json.loads(state_file.read_text(encoding="utf-8"))
            url = url or info.get("url")
            token = token or info.get("token")
        if not url:
            raise RuntimeError(f"Bridge is not running (missing {state_file})")
        self.url = url.rstrip("/")
        self.token = token or ""

    def get(self, path: str, *, timeout: float = 15.0) -> dict[str, Any]:
        req = urllib.request.Request(f"{self.url}{path}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode("utf-8", errors="replace")) from exc

    def post(self, path: str, payload: dict[str, Any] | None = None, *, timeout: float = 180.0) -> dict[str, Any]:
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode("utf-8", errors="replace")) from exc


def serve_bridge(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    beamng_exe: Path = DEFAULT_BEAMNG_EXE,
    user_dir: Path = DEFAULT_USER_DIR,
    state_file: Path = STATE_FILE,
) -> None:
    """Run the localhost HTTP bridge until interrupted."""
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(f"Refusing non-loopback bind host {host!r}; only localhost is allowed")

    if ".venv" in Path(sys.executable).parts:
        print(
            "WARNING: Running bridge from .venv\\Scripts\\python.exe may inherit Low Integrity Level\n"
            "         and fail to write F:\\Games\\BeamNG.drive or launch BeamNG.drive.x64.exe.\n"
            "         Prefer system Python: C:\\Users\\kiril\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
            file=sys.stderr,
            flush=True,
        )

    token = secrets.token_hex(24)
    controller = BeamNgHostController(beamng_exe=beamng_exe, user_dir=user_dir)
    handler = _make_handler(controller, token)
    server = _ThreadingHTTPServer((host, int(port)), handler)

    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_payload = {
        "url": f"http://{host}:{server.server_address[1]}",
        "token": token,
        "pid": os.getpid(),
        "beamng_exe": str(beamng_exe),
        "user_dir": str(user_dir),
    }
    state_file.write_text(json.dumps(state_payload, indent=2) + "\n", encoding="utf-8")
    print(f"BeamNG host bridge listening on {state_payload['url']} (state: {state_file})", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with contextlib.suppress(OSError):
            state_file.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Localhost BeamNG.drive host bridge & CLI client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_srv = sub.add_parser("serve", help="Run the localhost bridge server (outside Antigravity)")
    p_srv.add_argument("--host", default=DEFAULT_HOST)
    p_srv.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_srv.add_argument("--beamng-exe", type=Path, default=DEFAULT_BEAMNG_EXE)
    p_srv.add_argument("--user-dir", type=Path, default=DEFAULT_USER_DIR)

    sub.add_parser("status", help="Query BeamNG process, MCP, and installed levels")

    p_inst = sub.add_parser("install", help="Install built level into BeamNG user dir via bridge")
    p_inst.add_argument("--level", required=True)
    p_inst.add_argument("--include-zip", action="store_true")
    p_inst.add_argument("--no-clear-cache", action="store_true")

    p_start = sub.add_parser("start", help="Launch BeamNG.drive -enablemcp via bridge")
    p_start.add_argument("--level", default=None)
    p_start.add_argument("--vehicle", default="etk800")
    p_start.add_argument("--no-wait", action="store_true")
    p_start.add_argument("--restart", action="store_true")

    sub.add_parser("stop", help="Stop BeamNG.drive cleanly via bridge")

    p_rst = sub.add_parser("restart", help="Stop, optionally install+clear-cache, and start BeamNG")
    p_rst.add_argument("--level", default="ivanovo_golden_sector")
    p_rst.add_argument("--vehicle", default="etk800")
    p_rst.add_argument("--install", action="store_true")
    p_rst.add_argument("--clear-cache", action="store_true")
    p_rst.add_argument("--no-wait", action="store_true")

    p_logs = sub.add_parser("logs", help="Fetch tail and error summary from beamng.log")
    p_logs.add_argument("--tail", type=int, default=120)
    p_logs.add_argument("--only-errors", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve_bridge(host=args.host, port=args.port, beamng_exe=args.beamng_exe, user_dir=args.user_dir)
        return 0

    client = BeamNgBridgeClient()
    if args.cmd == "status":
        out = client.get("/status")
    elif args.cmd == "install":
        out = client.post(
            "/install",
            {"level_id": args.level, "include_zip": args.include_zip, "clear_cache": not args.no_clear_cache},
        )
    elif args.cmd == "start":
        out = client.post(
            "/beamng/start",
            {
                "level_id": args.level,
                "vehicle": args.vehicle,
                "wait_ready": not args.no_wait,
                "restart_if_running": args.restart,
            },
        )
    elif args.cmd == "stop":
        out = client.post("/beamng/stop")
    elif args.cmd == "restart":
        out = client.post(
            "/beamng/restart",
            {
                "level_id": args.level,
                "vehicle": args.vehicle,
                "install": args.install,
                "clear_cache": args.clear_cache,
                "wait_ready": not args.no_wait,
            },
        )
    elif args.cmd == "logs":
        err_flag = "1" if args.only_errors else "0"
        out = client.get(f"/logs?tail={int(args.tail)}&only_errors={err_flag}")
    else:
        raise ValueError(f"Unknown command: {args.cmd}")

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
