"""Tests for the localhost-only BeamNG host bridge."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from scripts.beamng_host_bridge import (
    BeamNgBridgeClient,
    BeamNgHostController,
    _make_handler,
    _ThreadingHTTPServer,
    validate_safe_id,
)


def test_validate_safe_id_rejects_traversal() -> None:
    assert validate_safe_id("ivanovo_golden_sector") == "ivanovo_golden_sector"
    assert validate_safe_id("etk800-v2") == "etk800-v2"
    for bad in ("", "../etc", "a/b", "a\\b", "foo bar", "x" * 100):
        with pytest.raises(ValueError):
            validate_safe_id(bad)


def test_controller_install_clear_cache_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    built = repo / "build" / "levels" / "demo_lvl"
    built.mkdir(parents=True)
    (built / "info.json").write_text('{"title":"demo"}', encoding="utf-8")

    user_dir = tmp_path / "user"
    (user_dir / "temp" / "levels" / "demo_lvl").mkdir(parents=True)
    (user_dir / "temp" / "levels" / "demo_lvl" / "stale.txt").write_text("x", encoding="utf-8")
    (user_dir / "temp" / "art" / "terrainMaterialCache").mkdir(parents=True)
    (user_dir / "mods").mkdir(parents=True)
    (user_dir / "mods" / "demo_lvl.zip").write_text("oldzip", encoding="utf-8")
    (user_dir / "beamng.log").write_text(
        "  1.000|I|init| start\n  2.500|E|test| sample error\n  3.000|D|GELua.serverConnection| setting worldReadyState to 1\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("scripts.beamng_host_bridge.find_beamng_pids", lambda: [])
    ctrl = BeamNgHostController(repo_root=repo, beamng_exe=tmp_path / "BeamNG.drive.x64.exe", user_dir=user_dir)

    res = ctrl.install_level("demo_lvl", clear_cache=True, remove_mod_zip=True)
    assert res["installed"] is True
    assert (user_dir / "levels" / "demo_lvl" / "info.json").is_file()
    assert not (user_dir / "mods" / "demo_lvl.zip").exists()
    assert not (user_dir / "temp" / "levels" / "demo_lvl").exists()

    logs = ctrl.get_logs(tail=10, only_errors=True)
    assert logs["exists"] is True
    assert logs["metrics"]["error_lines"] == 1
    assert len(logs["lines"]) == 1
    assert "sample error" in logs["lines"][0]


def test_bridge_http_token_auth_and_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    built = repo / "build" / "levels" / "demo_lvl"
    built.mkdir(parents=True)
    (built / "info.json").write_text('{"title":"demo"}', encoding="utf-8")
    user_dir = tmp_path / "user"
    user_dir.mkdir(parents=True)

    monkeypatch.setattr("scripts.beamng_host_bridge.find_beamng_pids", lambda: [])
    ctrl = BeamNgHostController(repo_root=repo, beamng_exe=tmp_path / "BeamNG.drive.x64.exe", user_dir=user_dir)
    token = "test_secret_token_123"
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(ctrl, token))
    port = server.server_address[1]
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        url = f"http://127.0.0.1:{port}"
        bad_client = BeamNgBridgeClient(url=url, token="wrong")
        with pytest.raises(RuntimeError, match="403|Invalid"):
            bad_client.post("/install", {"level_id": "demo_lvl"})

        client = BeamNgBridgeClient(url=url, token=token)
        health = client.get("/health")
        assert health["ok"] is True

        inst = client.post("/install", {"level_id": "demo_lvl"})
        assert inst["ok"] is True
        assert (user_dir / "levels" / "demo_lvl" / "info.json").is_file()

        status = client.get("/status")
        assert status["installed_levels"] == ["demo_lvl"]
    finally:
        server.shutdown()
        server.server_close()
        time.sleep(0.05)
