"""Procedural & stock Russian roadside infrastructure props for BeamNG.drive.

Generates and places:
1. Vienna-convention / Russian GOST road signs (speed limits, priority/give-way,
   crosswalks, bridge & sharp-bend warnings) using native BeamNG assets.
2. Galvanized cantilever streetlights and wooden utility poles along major roads.
3. Bus stop shelters and pole signs from OSM ``highway=bus_stop`` nodes (or
   suburban highway pullouts).
4. Procedural continuous W-beam steel guardrails (``отбойники``) along bridge
   approaches and steep curves/embankments, exported as localized Collada 1.4.1
   meshes referencing native ``guardrail_painted`` PBR DDS textures.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import numpy as np

from ivanovo2beam.beamng.level import stable_uuid
from ivanovo2beam.roads.build import CarriagewayIndex, build_carriageway_index, sample_points_z

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ivanovo2beam.gis.projection import RegionProjection
    from ivanovo2beam.osm.types import OsmData
    from ivanovo2beam.roads.network import Road

_FIXED_TIMESTAMP = "2026-01-01T00:00:00Z"

ROAD_PROPS_GROUP: tuple[str, ...] = ("level_objects", "road_props")

GUARDRAIL_MATERIAL = "ivanovo_guardrail_wbeam"
GUARDRAIL_TEXTURES: dict[str, str] = {
    "ao": "/assets/materials/baked_props/guardrail_painted/t_guardrail_ao.data.dds",
    "b": "/assets/materials/baked_props/guardrail_painted/t_guardrail_b.color.dds",
    "m": "/assets/materials/baked_props/guardrail_painted/t_guardrail_m.data.dds",
    "nm": "/assets/materials/baked_props/guardrail_painted/t_guardrail_nm.normal.dds",
    "r": "/assets/materials/baked_props/guardrail_painted/t_guardrail_r.data.dds",
}

_CLUTTER = "/art/shapes/garage_and_dealership/Clutter"
SIGN_SHAPES: dict[str, str] = {
    "speed_30": f"{_CLUTTER}/italy_traf_sign_speed_30.dae",
    "speed_50": f"{_CLUTTER}/italy_traf_sign_speed_50.dae",
    "speed_60": f"{_CLUTTER}/italy_traf_sign_speed_60.dae",
    "speed_80": f"{_CLUTTER}/italy_traf_sign_speed_80.dae",
    "speed_90": f"{_CLUTTER}/italy_traf_sign_speed_90.dae",
    "speed_100": f"{_CLUTTER}/italy_traf_sign_speed_100.dae",
    "priority_road": f"{_CLUTTER}/italy_traf_sign_priorityroad.dae",
    "give_way": f"{_CLUTTER}/italy_traf_sign_give_way.dae",
    "stop": f"{_CLUTTER}/italy_traf_sign_stop.dae",
    "zebra": f"{_CLUTTER}/italy_traf_sign_zebra.dae",
    "warn_zebra": f"{_CLUTTER}/italy_traf_sign_warn_zebra_warning.dae",
    "bridge": f"{_CLUTTER}/italy_traf_sign_bridge_not_motorway.dae",
    "crossroad": f"{_CLUTTER}/italy_traf_sign_crossroad.dae",
    "warn_bend_l": f"{_CLUTTER}/italy_traf_sign_warn_bend_l.dae",
    "warn_bend_r": f"{_CLUTTER}/italy_traf_sign_warn_bend_r.dae",
    "one_way": f"{_CLUTTER}/italy_traf_sign_one_way.dae",
    "roundabout": f"{_CLUTTER}/italy_traf_sign_roundabout.dae",
}

_SPEED_BUCKETS: tuple[int, ...] = (30, 50, 60, 80, 90, 100)

POLE_LIGHT_SINGLE = "/art/shapes/objects/pole_light_single.dae"
POLE_WOOD_OLD = "/art/shapes/common/power_lines_procedural/electric_pole_wood_old_01.dae"
BUS_STOP_SHELTER = "/art/shapes/objects/s_busstop_it.dae"
BUS_STOP_SIGN = "/art/shapes/objects/s_sign_busstop.dae"

_LIT_HIGHWAYS = frozenset({"motorway", "trunk", "primary", "primary_link", "secondary"})
_UTILITY_POLE_HIGHWAYS = frozenset({"tertiary", "residential"})
_GUARDRAIL_HIGHWAYS = frozenset({"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary"})
_SIGNED_HIGHWAYS = frozenset(
    {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link", "tertiary"}
)


def yaw_to_rot_matrix(yaw_rad: float) -> list[float]:
    """Convert a CCW world-XY yaw angle (radians) to a BeamNG 3x3 rotation matrix."""
    c = round(math.cos(yaw_rad), 6)
    s = round(math.sin(yaw_rad), 6)
    return [c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]


def _sign_yaw_for_travel(tx: float, ty: float) -> float:
    """Return yaw so a sign mesh facing local ``-Y`` points back at oncoming traffic ``(tx, ty)``."""
    return math.atan2(-tx, ty)


def _inward_x_pos_yaw(ix: float, iy: float) -> float:
    """Return yaw so local ``+X`` points along ``(ix, iy)`` (e.g. ``pole_light_single`` arm)."""
    return math.atan2(iy, ix)


def _inward_x_neg_yaw(ix: float, iy: float) -> float:
    """Return yaw so local ``-X`` points along ``(ix, iy)`` (e.g. ``s_busstop_it`` opening)."""
    return math.atan2(-iy, -ix)


def _speed_sign_key(maxspeed_kph: float) -> str:
    best = min(_SPEED_BUCKETS, key=lambda v: abs(v - maxspeed_kph))
    return f"speed_{best}"


class _SpatialDedupe:
    """Category-aware 2D spatial grid preventing crowded or overlapping props."""

    def __init__(self, cell_m: float = 20.0) -> None:
        self.cell_m = cell_m
        self.items: dict[str, dict[tuple[int, int], list[tuple[float, float]]]] = {}

    def allow(self, category: str, x: float, y: float, min_dist_m: float) -> bool:
        bucket = self.items.setdefault(category, {})
        gx = math.floor(x / self.cell_m)
        gy = math.floor(y / self.cell_m)
        span = max(1, math.ceil(min_dist_m / self.cell_m))
        r2 = min_dist_m * min_dist_m
        for dx in range(-span, span + 1):
            for dy in range(-span, span + 1):
                for ox, oy in bucket.get((gx + dx, gy + dy), ()):
                    if (ox - x) * (ox - x) + (oy - y) * (oy - y) < r2:
                        return False
        bucket.setdefault((gx, gy), []).append((x, y))
        return True


def _in_terrain_bounds(x: float, y: float, build: object | None) -> bool:
    if build is None or not hasattr(build, "elevations"):
        return True
    elev = build.elevations  # type: ignore[attr-defined]
    ox = float(build.origin_x)  # type: ignore[attr-defined]
    oy = float(build.origin_y)  # type: ignore[attr-defined]
    cs = float(build.cell_size_m)  # type: ignore[attr-defined]
    rows, cols = elev.shape
    max_x = ox + (cols - 1) * cs
    max_y = oy + (rows - 1) * cs
    return (ox + 1.0) <= x <= (max_x - 1.0) and (oy + 1.0) <= y <= (max_y - 1.0)


def _sample_z(x: float, y: float, build: object | None, fallback: float = 0.0) -> float:
    if build is None or not hasattr(build, "elevations"):
        return fallback
    arr = sample_points_z(np.array([[x, y]], dtype=np.float64), build)
    val = float(arr[0])
    return val if math.isfinite(val) else fallback


def _is_shoulder_clear(
    x: float,
    y: float,
    owner_id: int,
    layer: int,
    carriageway_index: CarriagewayIndex,
    *,
    margin_m: float = 1.0,
) -> bool:
    return not carriageway_index.is_blocked(
        x,
        y,
        margin_m=margin_m,
        tangent=(0.0, 0.0),
        owner_id=owner_id,
        layer=layer,
        cut_all_angles=True,
    )


def _polyline_cum_len(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    deltas = np.diff(pts[:, :2], axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    cum = np.concatenate(([0.0], np.cumsum(seg_lens)))
    return seg_lens, cum


def _sample_polyline_at(pts: np.ndarray, cum: np.ndarray, dist_m: float) -> tuple[float, float, float, float]:
    """Return ``(x, y, tx, ty)`` at arc-length ``dist_m`` along ``pts``."""
    d = max(0.0, min(float(cum[-1]), dist_m))
    idx = int(np.searchsorted(cum, d, side="right") - 1)
    idx = max(0, min(idx, pts.shape[0] - 2))
    d0 = float(cum[idx])
    d1 = float(cum[idx + 1])
    span = d1 - d0
    t = 0.0 if span <= 1e-6 else (d - d0) / span
    ax, ay = float(pts[idx, 0]), float(pts[idx, 1])
    bx, by = float(pts[idx + 1, 0]), float(pts[idx + 1, 1])
    dx, dy = bx - ax, by - ay
    seg_len = math.hypot(dx, dy)
    if seg_len <= 1e-6:
        return (ax, ay, 1.0, 0.0)
    return (ax + t * dx, ay + t * dy, dx / seg_len, dy / seg_len)


def _make_tsstatic(
    level_id: str,
    *,
    key: str,
    shape_name: str,
    x: float,
    y: float,
    z: float,
    yaw_rad: float,
    parent: str,
    collision_type: str = "Visible Mesh Final",
    decal_type: str = "None",
) -> dict[str, Any]:
    return {
        "name": key,
        "class": "TSStatic",
        "persistentId": stable_uuid(level_id, "TSStatic", key),
        "__parent": parent,
        "position": [round(x, 4), round(y, 4), round(z, 4)],
        "rotationMatrix": yaw_to_rot_matrix(yaw_rad),
        "shapeName": shape_name,
        "scale": [1.0, 1.0, 1.0],
        "collisionType": collision_type,
        "decalType": decal_type,
    }


# ---------------------------------------------------------------------------
# Procedural continuous W-beam guardrail (.dae + material)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GuardrailMesh:
    """Procedural W-beam steel guardrail mesh container."""

    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    triangles: list[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=list
    )
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def add_quad(
        self,
        v0: tuple[float, float, float],
        v1: tuple[float, float, float],
        v2: tuple[float, float, float],
        v3: tuple[float, float, float],
        uv0: tuple[float, float],
        uv1: tuple[float, float],
        uv2: tuple[float, float],
        uv3: tuple[float, float],
    ) -> None:
        ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
        bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
        nx = ay * bz - az * by
        ny = az * bx - ax * bz
        nz = ax * by - ay * bx
        nlen = math.sqrt(nx * nx + ny * ny + nz * nz)
        n = (nx / nlen, ny / nlen, nz / nlen) if nlen > 1e-9 else (0.0, 0.0, 1.0)
        base = len(self.positions)
        self.positions.extend([v0, v1, v2, v3])
        self.normals.extend([n, n, n, n])
        self.uvs.extend([uv0, uv1, uv2, uv3])
        self.triangles.append(((base, base, base), (base + 1, base + 1, base + 1), (base + 2, base + 2, base + 2)))
        self.triangles.append(((base, base, base), (base + 2, base + 2, base + 2), (base + 3, base + 3, base + 3)))


def _add_wbeam_post(
    mesh: GuardrailMesh,
    px: float,
    py: float,
    pz: float,
    tx: float,
    ty: float,
    inward_x: float,
    inward_y: float,
) -> None:
    """Add one galvanized C-post (``0.12 x 0.08 m``) sunk ``0.25 m`` into ground up to ``0.77 m``."""
    out_x, out_y = -inward_x, -inward_y
    cx = px + out_x * 0.06
    cy = py + out_y * 0.06
    hl = 0.055
    hw = 0.045
    zb = pz - 0.25
    zt = pz + 0.77
    c000 = (cx - hl * tx - hw * out_x, cy - hl * ty - hw * out_y, zb)
    c100 = (cx + hl * tx - hw * out_x, cy + hl * ty - hw * out_y, zb)
    c110 = (cx + hl * tx + hw * out_x, cy + hl * ty + hw * out_y, zb)
    c010 = (cx - hl * tx + hw * out_x, cy - hl * ty + hw * out_y, zb)
    c001 = (c000[0], c000[1], zt)
    c101 = (c100[0], c100[1], zt)
    c111 = (c110[0], c110[1], zt)
    c011 = (c010[0], c010[1], zt)
    for v0, v1, v2, v3 in (
        (c001, c101, c111, c011),
        (c000, c100, c101, c001),
        (c110, c010, c011, c111),
        (c010, c000, c001, c011),
        (c100, c110, c111, c101),
    ):
        mesh.add_quad(v0, v1, v2, v3, (0.0, 0.0), (0.2, 0.0), (0.2, 0.8), (0.0, 0.8))


def build_wbeam_guardrail_mesh(
    points_3d: np.ndarray,
    *,
    inward_side: float = 1.0,
    post_spacing_m: float = 2.0,
    localize: bool = True,
) -> GuardrailMesh:
    """Build a continuous corrugated W-beam steel guardrail along ``points_3d``.

    ``inward_side`` (`+1.0` or `-1.0`) indicates which polyline left-normal faces
    the carriageway; end terminals taper slightly away from traffic and drop down
    cleanly.
    """
    pts = np.asarray(points_3d, dtype=np.float64)
    mesh = GuardrailMesh()
    n = pts.shape[0]
    if n < 2:
        return mesh

    diffs = np.diff(pts[:, :2], axis=0)
    lens = np.hypot(diffs[:, 0], diffs[:, 1])
    safe_lens = np.where(lens < 1e-6, 1.0, lens)
    tans = diffs / safe_lens[:, None]
    cum = np.concatenate(([0.0], np.cumsum(lens)))
    total_len = float(cum[-1])
    if total_len < 1.0:
        return mesh

    normals = np.zeros((n, 2), dtype=np.float64)
    normals[0] = [-tans[0, 1], tans[0, 0]]
    normals[-1] = [-tans[-1, 1], tans[-1, 0]]
    if n > 2:
        mids = (tans[:-1] + tans[1:]) * 0.5
        mlens = np.hypot(mids[:, 0], mids[:, 1])
        bad = mlens < 1e-6
        if np.any(bad):
            mids[bad] = tans[:-1][bad]
            mlens = np.where(np.hypot(mids[:, 0], mids[:, 1]) < 1e-6, 1.0, np.hypot(mids[:, 0], mids[:, 1]))
        mids /= mlens[:, None]
        normals[1:-1] = np.column_stack((-mids[:, 1], mids[:, 0]))
    inward_n = normals * inward_side

    # W-beam cross-section profile offsets: (inward_offset_m, height_above_ground_m, v_tex)
    profile: tuple[tuple[float, float, float], ...] = (
        (-0.02, 0.44, 0.00),
        (0.06, 0.49, 0.16),
        (0.06, 0.56, 0.34),
        (-0.01, 0.60, 0.50),
        (0.06, 0.64, 0.66),
        (0.06, 0.71, 0.84),
        (-0.02, 0.76, 1.00),
    )

    def _station_ring(i: int) -> list[tuple[float, float, float]]:
        d = float(cum[i])
        flare = 0.0
        z_drop = 0.0
        if d < 2.0 and total_len >= 8.0:
            f = 1.0 - d / 2.0
            flare = 0.28 * f
            z_drop = 0.14 * f
        elif total_len - d < 2.0 and total_len >= 8.0:
            f = 1.0 - (total_len - d) / 2.0
            flare = 0.28 * f
            z_drop = 0.14 * f
        px, py, pz = float(pts[i, 0]), float(pts[i, 1]), float(pts[i, 2]) - z_drop
        ix, iy = float(inward_n[i, 0]), float(inward_n[i, 1])
        return [(px + (off - flare) * ix, py + (off - flare) * iy, pz + hz) for off, hz, _ in profile]

    rings = [_station_ring(i) for i in range(n)]
    for i in range(n - 1):
        u0 = float(cum[i]) * 0.5
        u1 = float(cum[i + 1]) * 0.5
        r0 = rings[i]
        r1 = rings[i + 1]
        for k in range(len(profile) - 1):
            v0_t = profile[k][2]
            v1_t = profile[k + 1][2]
            mesh.add_quad(r0[k], r1[k], r1[k + 1], r0[k + 1], (u0, v0_t), (u1, v0_t), (u1, v1_t), (u0, v1_t))
        # Back face so W-beam looks solid from outside the road too
        mesh.add_quad(r0[-1], r1[-1], r1[0], r0[0], (u0, 1.0), (u1, 1.0), (u1, 0.0), (u0, 0.0))

    # Support posts every post_spacing_m
    n_posts = max(2, math.ceil(total_len / post_spacing_m) + 1)
    for pd in np.linspace(0.2, max(0.2, total_len - 0.2), n_posts):
        pidx = max(0, min(n - 2, int(np.searchsorted(cum, float(pd), side="right") - 1)))
        slen = float(lens[pidx])
        t = 0.0 if slen <= 1e-6 else (float(pd) - float(cum[pidx])) / slen
        px = float(pts[pidx, 0] + t * (pts[pidx + 1, 0] - pts[pidx, 0]))
        py = float(pts[pidx, 1] + t * (pts[pidx + 1, 1] - pts[pidx, 1]))
        pz = float(pts[pidx, 2] + t * (pts[pidx + 1, 2] - pts[pidx, 2]))
        ix, iy = float(inward_n[pidx, 0]), float(inward_n[pidx, 1])
        tx, ty = float(tans[pidx, 0]), float(tans[pidx, 1])
        _add_wbeam_post(mesh, px, py, pz, tx, ty, ix, iy)

    if localize and mesh.positions:
        xs = [p[0] for p in mesh.positions]
        ys = [p[1] for p in mesh.positions]
        zs = [p[2] for p in mesh.positions]
        ox = round(0.5 * (min(xs) + max(xs)), 4)
        oy = round(0.5 * (min(ys) + max(ys)), 4)
        oz = round(min(zs), 4)
        mesh.positions = [(px - ox, py - oy, pz - oz) for px, py, pz in mesh.positions]
        mesh.origin = (ox, oy, oz)

    return mesh


def export_guardrail_dae(path: Path, mesh: GuardrailMesh, name: str) -> int:
    """Write a localized W-beam guardrail COLLADA 1.4.1 mesh to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pos_str = " ".join(f"{v:.4f}".rstrip("0").rstrip(".") or "0" for p in mesh.positions for v in p)
    norm_str = " ".join(f"{v:.4f}".rstrip("0").rstrip(".") or "0" for n in mesh.normals for v in n)
    uv_str = " ".join(f"{v:.4f}".rstrip("0").rstrip(".") or "0" for uv in mesh.uvs for v in uv)
    p_str = " ".join(f"{vi} {ni} {uvi}" for tri in mesh.triangles for vi, ni, uvi in tri)
    safe_name = escape(name)
    mat_name = GUARDRAIL_MATERIAL

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><author>ivanovo2beam</author></contributor>
    <created>{_FIXED_TIMESTAMP}</created>
    <modified>{_FIXED_TIMESTAMP}</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="{safe_name}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert><diffuse><color>0.65 0.67 0.69 1</color></diffuse></lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{mat_name}" name="{mat_name}">
      <instance_effect url="#{safe_name}-effect"/>
    </material>
  </library_materials>
  <library_geometries>
    <geometry id="{safe_name}-mesh" name="{safe_name}">
      <mesh>
        <source id="{safe_name}-mesh-positions">
          <float_array id="{safe_name}-mesh-positions-array" count="{len(mesh.positions) * 3}">{pos_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-positions-array" count="{len(mesh.positions)}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{safe_name}-mesh-normals">
          <float_array id="{safe_name}-mesh-normals-array" count="{len(mesh.normals) * 3}">{norm_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-normals-array" count="{len(mesh.normals)}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{safe_name}-mesh-map-0">
          <float_array id="{safe_name}-mesh-map-0-array" count="{len(mesh.uvs) * 2}">{uv_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-map-0-array" count="{len(mesh.uvs)}" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{safe_name}-mesh-vertices">
          <input semantic="POSITION" source="#{safe_name}-mesh-positions"/>
        </vertices>
        <triangles material="{mat_name}" count="{len(mesh.triangles)}">
          <input semantic="VERTEX" source="#{safe_name}-mesh-vertices" offset="0"/>
          <input semantic="NORMAL" source="#{safe_name}-mesh-normals" offset="1"/>
          <input semantic="TEXCOORD" source="#{safe_name}-mesh-map-0" offset="2" set="0"/>
          <p>{p_str}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="{safe_name}" name="{safe_name}" type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <instance_geometry url="#{safe_name}-mesh" name="{safe_name}">
          <bind_material><technique_common>
            <instance_material symbol="{mat_name}" target="#{mat_name}"/>
          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
"""
    data = xml.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def emit_road_props_materials(level_dir: Path, level_id: str) -> Path:
    """Emit ``levels/<id>/art/shapes/road_props/main.materials.json`` for guardrails."""
    mat_path = level_dir / "art" / "shapes" / "road_props" / "main.materials.json"
    mat_path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        GUARDRAIL_MATERIAL: {
            "name": GUARDRAIL_MATERIAL,
            "mapTo": GUARDRAIL_MATERIAL,
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", GUARDRAIL_MATERIAL),
            "Stages": [
                {
                    "ambientOcclusionMap": GUARDRAIL_TEXTURES["ao"],
                    "baseColorFactor": [0.78, 0.80, 0.82, 1.0],
                    "baseColorMap": GUARDRAIL_TEXTURES["b"],
                    "metallicFactor": 0.72,
                    "metallicMap": GUARDRAIL_TEXTURES["m"],
                    "normalMap": GUARDRAIL_TEXTURES["nm"],
                    "roughnessFactor": 0.45,
                    "roughnessMap": GUARDRAIL_TEXTURES["r"],
                },
                {},
                {},
                {},
            ],
            "version": 1.5,
        }
    }
    mat_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return mat_path


# ---------------------------------------------------------------------------
# Placement orchestrator
# ---------------------------------------------------------------------------


def add_roadside_props_to_scene(
    tree: Any,
    roads: Sequence[Road],
    build: object | None = None,
    *,
    level_dir: Path | None = None,
    osm_data: OsmData | None = None,
    projection: RegionProjection | None = None,
    group_path: Sequence[str] = ROAD_PROPS_GROUP,
    carriageway_index: CarriagewayIndex | None = None,
) -> dict[str, int]:
    """Place Russian road signs, poles, bus stops, and W-beam guardrails.

    Global stock assets (signs, poles, bus stops) work without ``level_dir``.
    Procedural localized W-beam guardrails (``отбойники``) are emitted on bridge
    approaches and steep embankments whenever ``level_dir`` is provided.
    """
    parent_name = group_path[-1] if group_path else "road_props"
    idx = carriageway_index if carriageway_index is not None else build_carriageway_index(roads)
    dedupe = _SpatialDedupe()
    counts = {"signs": 0, "poles": 0, "bus_stops": 0, "guardrails": 0}
    prop_seq = 0

    def _emit_stock(
        category: str,
        key_tag: str,
        shape: str,
        x: float,
        y: float,
        z: float,
        yaw: float,
        min_sep: float,
        any_sep: float = 2.2,
    ) -> bool:
        nonlocal prop_seq
        if not _in_terrain_bounds(x, y, build):
            return False
        if not dedupe.allow(category, x, y, min_sep):
            return False
        if not dedupe.allow("_any", x, y, any_sep):
            return False
        prop_seq += 1
        obj = _make_tsstatic(
            tree.level_id,
            key=f"prop_{key_tag}_{prop_seq}",
            shape_name=shape,
            x=x,
            y=y,
            z=z,
            yaw_rad=yaw,
            parent=parent_name,
        )
        tree.add_object(group_path, obj)
        return True

    drivable_roads = [r for r in roads if r.drivable and not r.tunnel and r.points.shape[0] >= 2]

    # Build endpoint junction graph for priority / give-way / bridge signs
    junc_arms: dict[tuple[int, int], list[tuple[Road, bool, float, float]]] = {}
    for road in drivable_roads:
        pts = road.points
        for at_end in (False, True):
            p_end = pts[-1] if at_end else pts[0]
            p_in = pts[-2] if at_end else pts[1]
            dx = float(p_in[0] - p_end[0])
            dy = float(p_in[1] - p_end[1])
            dlen = math.hypot(dx, dy)
            if dlen > 1e-6:
                jkey = (round(float(p_end[0]) * 2.0), round(float(p_end[1]) * 2.0))
                junc_arms.setdefault(jkey, []).append((road, at_end, dx / dlen, dy / dlen))

    # 1. Signs (speed limit, junction priority/give-way, sharp bends, bridges)
    for road in drivable_roads:
        if road.bridge or road.highway not in _SIGNED_HIGHWAYS:
            continue
        pts = road.points
        _, cum = _polyline_cum_len(pts)
        total_len = float(cum[-1])
        if total_len < 30.0:
            continue
        shoulder_off = road.half_width_m + 1.25

        # Speed limit sign (3.24) near start (facing forward travel)
        sx, sy, tx, ty = _sample_polyline_at(pts, cum, min(18.0, total_len * 0.25))
        rx, ry = sx + ty * shoulder_off, sy - tx * shoulder_off
        if _is_shoulder_clear(rx, ry, road.osm_id, road.layer, idx, margin_m=0.8):
            gz = _sample_z(rx, ry, build) - 0.05
            if _emit_stock("speed", f"spd_{road.osm_id}", SIGN_SHAPES[_speed_sign_key(road.maxspeed_kph)], rx, ry, gz, _sign_yaw_for_travel(tx, ty), 55.0):
                counts["signs"] += 1

        # Reverse-direction speed sign on long two-way highways
        if not road.oneway and total_len >= 160.0 and road.highway in _LIT_HIGHWAYS:
            sx2, sy2, tx2, ty2 = _sample_polyline_at(pts, cum, total_len - 18.0)
            lx2, ly2 = sx2 - ty2 * shoulder_off, sy2 + tx2 * shoulder_off
            if _is_shoulder_clear(lx2, ly2, road.osm_id, road.layer, idx, margin_m=0.8):
                gz = _sample_z(lx2, ly2, build) - 0.05
                if _emit_stock("speed", f"spdr_{road.osm_id}", SIGN_SHAPES[_speed_sign_key(road.maxspeed_kph)], lx2, ly2, gz, _sign_yaw_for_travel(-tx2, -ty2), 55.0):
                    counts["signs"] += 1

        # Sharp bend warning signs on curves >= 35 deg
        if pts.shape[0] >= 3 and road.highway in _LIT_HIGHWAYS:
            for vi in range(1, pts.shape[0] - 1):
                v0 = pts[vi - 1]
                v1 = pts[vi]
                v2 = pts[vi + 1]
                ax, ay = float(v1[0] - v0[0]), float(v1[1] - v0[1])
                bx, by = float(v2[0] - v1[0]), float(v2[1] - v1[1])
                la, lb = math.hypot(ax, ay), math.hypot(bx, by)
                if la < 8.0 or lb < 8.0:
                    continue
                cross = (ax * by - ay * bx) / (la * lb)
                dot = (ax * bx + ay * by) / (la * lb)
                if dot > math.cos(math.radians(38.0)):
                    continue
                warn_d = max(5.0, float(cum[vi]) - 32.0)
                wx, wy, wtx, wty = _sample_polyline_at(pts, cum, warn_d)
                prx, pry = wx + wty * shoulder_off, wy - wtx * shoulder_off
                if _is_shoulder_clear(prx, pry, road.osm_id, road.layer, idx, margin_m=0.8):
                    sign_k = "warn_bend_l" if cross > 0 else "warn_bend_r"
                    gz = _sample_z(prx, pry, build) - 0.05
                    if _emit_stock("warn_bend", f"bend_{road.osm_id}", SIGN_SHAPES[sign_k], prx, pry, gz, _sign_yaw_for_travel(wtx, wty), 90.0):
                        counts["signs"] += 1

    # Priority / Give-Way signs at junctions & Bridge warning signs
    # Map osm_id -> Road priority for interior T-junction checks
    prio_by_id = {r.osm_id: r.priority for r in drivable_roads}

    def _max_intersecting_priority(x: float, y: float, owner_id: int, layer: int) -> int:
        best_p = 0
        cell = (math.floor(x / idx.cell_size_m), math.floor(y / idx.cell_size_m))
        for si in idx.grid.get(cell, ()):
            seg = idx.segments[si]
            if seg.owner_id == owner_id or seg.layer != layer:
                continue
            dx, dy = seg.bx - seg.ax, seg.by - seg.ay
            len2 = dx * dx + dy * dy
            t = max(0.0, min(1.0, ((x - seg.ax) * dx + (y - seg.ay) * dy) / len2)) if len2 > 0.0 else 0.0
            dist = math.hypot(seg.ax + t * dx - x, seg.ay + t * dy - y)
            if dist <= seg.half_width + 1.5:
                best_p = max(best_p, prio_by_id.get(seg.owner_id, 0))
        return best_p

    for arms in junc_arms.values():
        max_prio = max(r.priority for r, _, _, _ in arms)
        for road, at_end, dx_in, dy_in in arms:
            if road.bridge:
                continue
            pts = road.points
            _, cum = _polyline_cum_len(pts)
            total_len = float(cum[-1])
            if total_len < 20.0:
                continue
            p_end = pts[-1] if at_end else pts[0]
            eff_max_prio = max(max_prio, _max_intersecting_priority(float(p_end[0]), float(p_end[1]), road.osm_id, road.layer))
            # Outbound travel toward junction is (-dx_in, -dy_in)
            tx, ty = -dx_in, -dy_in
            dist_from_end = min(11.0, total_len * 0.4)
            s_pos = (total_len - dist_from_end) if at_end else dist_from_end
            sx, sy, _, _ = _sample_polyline_at(pts, cum, s_pos)
            shoulder_off = road.half_width_m + 1.15
            rx, ry = sx + ty * shoulder_off, sy - tx * shoulder_off
            if not _is_shoulder_clear(rx, ry, road.osm_id, road.layer, idx, margin_m=0.8):
                continue

            # Check if opposite arm is a bridge -> emit bridge sign 28m before portal
            partner_bridge = any(r.bridge for r, _, _, _ in arms if r.osm_id != road.osm_id)
            if partner_bridge and total_len >= 32.0:
                b_pos = (total_len - min(32.0, total_len * 0.7)) if at_end else min(32.0, total_len * 0.7)
                bx, by, _, _ = _sample_polyline_at(pts, cum, b_pos)
                brx, bry = bx + ty * shoulder_off, by - tx * shoulder_off
                if _is_shoulder_clear(brx, bry, road.osm_id, road.layer, idx, margin_m=0.8):
                    gz = _sample_z(brx, bry, build) - 0.05
                    if _emit_stock("bridge_sign", f"brs_{road.osm_id}", SIGN_SHAPES["bridge"], brx, bry, gz, _sign_yaw_for_travel(tx, ty), 35.0):
                        counts["signs"] += 1

            is_t_or_multi = len(arms) >= 3 or eff_max_prio > road.priority
            if not is_t_or_multi:
                continue
            gz = _sample_z(rx, ry, build) - 0.05
            if road.priority <= eff_max_prio - 15 and road.highway in _SIGNED_HIGHWAYS | {"residential", "unclassified"}:
                sk = "stop" if road.width_m < 6.0 else "give_way"
                if _emit_stock("priority", f"gw_{road.osm_id}", SIGN_SHAPES[sk], rx, ry, gz, _sign_yaw_for_travel(tx, ty), 18.0):
                    counts["signs"] += 1
            elif len(arms) >= 3 and road.priority == eff_max_prio and road.highway in _LIT_HIGHWAYS:
                if _emit_stock("priority_main", f"pr_{road.osm_id}", SIGN_SHAPES["priority_road"], rx, ry, gz, _sign_yaw_for_travel(tx, ty), 75.0):
                    counts["signs"] += 1

    # 2. OSM Crosswalk Signs & Bus Stops (placed before periodic poles so stops are never displaced)
    if osm_data is not None and projection is not None:
        paved_candidates = [r for r in drivable_roads if not r.bridge and r.highway in _SIGNED_HIGHWAYS]
        cand_cell = 32.0
        cand_grid: dict[tuple[int, int], list[tuple[Road, float, float, float, float]]] = {}
        for r in paved_candidates:
            pts = r.points
            for i in range(pts.shape[0] - 1):
                ax, ay = float(pts[i, 0]), float(pts[i, 1])
                bx, by = float(pts[i + 1, 0]), float(pts[i + 1, 1])
                seg = (r, ax, ay, bx, by)
                gx0 = math.floor(min(ax, bx) / cand_cell)
                gx1 = math.floor(max(ax, bx) / cand_cell)
                gy0 = math.floor(min(ay, by) / cand_cell)
                gy1 = math.floor(max(ay, by) / cand_cell)
                for gx in range(gx0, gx1 + 1):
                    for gy in range(gy0, gy1 + 1):
                        cand_grid.setdefault((gx, gy), []).append(seg)

        for nid, tags in osm_data.node_tags.items():
            hw_tag = tags.get("highway", "")
            pt_tag = tags.get("public_transport", "")
            is_bus = hw_tag == "bus_stop" or (pt_tag == "platform" and tags.get("bus") == "yes")
            is_cross = hw_tag == "crossing"
            if not is_bus and not is_cross:
                continue
            lon_lat = osm_data.nodes.get(nid)
            if lon_lat is None:
                continue
            wx, wy = projection.geo_to_world(lon_lat[0], lon_lat[1])
            cx_cell = math.floor(wx / cand_cell)
            cy_cell = math.floor(wy / cand_cell)
            best: tuple[float, Road, float, float, float, float] | None = None
            for gx in range(cx_cell - 1, cx_cell + 2):
                for gy in range(cy_cell - 1, cy_cell + 2):
                    for r, ax, ay, bx, by in cand_grid.get((gx, gy), ()):
                        dx, dy = bx - ax, by - ay
                        len2 = dx * dx + dy * dy
                        t = max(0.0, min(1.0, ((wx - ax) * dx + (wy - ay) * dy) / len2)) if len2 > 0.0 else 0.0
                        cx, cy = ax + t * dx, ay + t * dy
                        d = math.hypot(cx - wx, cy - wy)
                        if d < 28.0 and (best is None or d < best[0]):
                            slen = math.sqrt(len2) if len2 > 1e-9 else 1.0
                            best = (d, r, cx, cy, dx / slen, dy / slen)
            if best is None:
                continue
            _, road, cx, cy, tx, ty = best
            # Pick whichever side of road the OSM stop node sits on
            cross_side = (wx - cx) * (-ty) + (wy - cy) * tx
            left_normal = (-ty, tx)
            out_dir = left_normal if cross_side >= 0 else (ty, -tx)
            in_dir = (-out_dir[0], -out_dir[1])
            travel_tx, travel_ty = (-tx, -ty) if cross_side >= 0 else (tx, ty)

            if is_bus:
                bx = cx + out_dir[0] * (road.half_width_m + 2.4)
                by = cy + out_dir[1] * (road.half_width_m + 2.4)
                if _is_shoulder_clear(bx, by, road.osm_id, road.layer, idx, margin_m=1.2):
                    gz = _sample_z(bx, by, build) - 0.02
                    if _emit_stock("bus_stop", f"bus_{nid}", BUS_STOP_SHELTER, bx, by, gz, _inward_x_neg_yaw(in_dir[0], in_dir[1]), 60.0):
                        counts["bus_stops"] += 1
                        sx = bx - travel_tx * 3.0 + in_dir[0] * 0.9
                        sy = by - travel_ty * 3.0 + in_dir[1] * 0.9
                        if _is_shoulder_clear(sx, sy, road.osm_id, road.layer, idx, margin_m=0.8):
                            _emit_stock("bus_sign", f"bussign_{nid}", BUS_STOP_SIGN, sx, sy, _sample_z(sx, sy, build) - 0.05, _sign_yaw_for_travel(travel_tx, travel_ty), 15.0)
            elif is_cross:
                zx = cx + out_dir[0] * (road.half_width_m + 1.2)
                zy = cy + out_dir[1] * (road.half_width_m + 1.2)
                if _is_shoulder_clear(zx, zy, road.osm_id, road.layer, idx, margin_m=0.8):
                    gz = _sample_z(zx, zy, build) - 0.05
                    if _emit_stock("zebra", f"zeb_{nid}", SIGN_SHAPES["zebra"], zx, zy, gz, _sign_yaw_for_travel(travel_tx, travel_ty), 35.0):
                        counts["signs"] += 1

    # 3. Streetlights & Utility Poles (step clear of signs & bus stops via any_sep=5.0)
    for road in drivable_roads:
        if road.bridge:
            continue
        is_lit = road.highway in _LIT_HIGHWAYS
        is_util = road.highway in _UTILITY_POLE_HIGHWAYS
        if not is_lit and not is_util:
            continue
        pts = road.points
        _, cum = _polyline_cum_len(pts)
        total_len = float(cum[-1])
        spacing = 50.0 if is_lit else 65.0
        if total_len < spacing:
            continue
        pole_shape = POLE_LIGHT_SINGLE if is_lit else POLE_WOOD_OLD
        pole_off = road.half_width_m + (1.65 if is_lit else 2.1)
        z_embed = 0.15 if is_lit else 0.05
        d = 22.0
        while d <= total_len - 22.0:
            px, py, tx, ty = _sample_polyline_at(pts, cum, d)
            # Right shoulder: inward normal toward road center is left normal (-ty, tx)
            sx = px + ty * pole_off
            sy = py - tx * pole_off
            if _is_shoulder_clear(sx, sy, road.osm_id, road.layer, idx, margin_m=1.2):
                gz = _sample_z(sx, sy, build) - z_embed
                yaw = _inward_x_pos_yaw(-ty, tx)
                if _emit_stock("pole", f"pole_{road.osm_id}", pole_shape, sx, sy, gz, yaw, 38.0, any_sep=5.0):
                    counts["poles"] += 1
            d += spacing

    # 4. Procedural continuous W-beam guardrails (отбойники) on bridge approaches & steep curves
    if level_dir is not None:
        props_dir = level_dir / "art" / "shapes" / "road_props"
        mat_written = False
        gr_seq = 0

        def _emit_guardrail_run(run_xy: np.ndarray, road: Road, inward_side: float, tag: str) -> None:
            nonlocal mat_written, gr_seq
            if run_xy.shape[0] < 2:
                return
            pts_3d = np.column_stack(
                (
                    run_xy[:, 0],
                    run_xy[:, 1],
                    np.array([_sample_z(float(p[0]), float(p[1]), build) for p in run_xy], dtype=np.float64),
                )
            )
            mesh = build_wbeam_guardrail_mesh(pts_3d, inward_side=inward_side, localize=True)
            if not mesh.triangles:
                return
            if not mat_written:
                emit_road_props_materials(level_dir, tree.level_id)
                mat_written = True
            gr_seq += 1
            mesh_name = f"guardrail_{tag}_{gr_seq}"
            dae_path = props_dir / f"{mesh_name}.dae"
            export_guardrail_dae(dae_path, mesh, mesh_name)
            obj = _make_tsstatic(
                tree.level_id,
                key=mesh_name,
                shape_name=f"/levels/{tree.level_id}/art/shapes/road_props/{mesh_name}.dae",
                x=mesh.origin[0],
                y=mesh.origin[1],
                z=mesh.origin[2],
                yaw_rad=0.0,
                parent=parent_name,
            )
            tree.add_object(group_path, obj)
            counts["guardrails"] += 1

        # 4a) Bridgehead guardrails (~28m approach leading into both portals of every bridge)
        for road in drivable_roads:
            if not road.bridge:
                continue
            pts = road.points
            off = road.half_width_m + 0.35
            for at_end in (False, True):
                p0 = pts[-1] if at_end else pts[0]
                p_in = pts[-2] if at_end else pts[1]
                vx, vy = float(p0[0] - p_in[0]), float(p0[1] - p_in[1])
                vlen = math.hypot(vx, vy)
                if vlen <= 1e-6:
                    continue
                ux, uy = vx / vlen, vy / vlen  # pointing away from bridge along approach
                nx, ny = -uy, ux
                for side_sign in (-1.0, 1.0):
                    # Walk from 28m back toward portal (p0) so polyline order runs toward bridge
                    samples: list[tuple[float, float]] = []
                    for ds in np.linspace(28.0, 1.5, 10):
                        cx = float(p0[0]) + ux * float(ds) + side_sign * off * nx
                        cy = float(p0[1]) + uy * float(ds) + side_sign * off * ny
                        if not _in_terrain_bounds(cx, cy, build):
                            continue
                        if carriageway_index_clear(cx, cy, road, idx):
                            samples.append((cx, cy))
                        elif len(samples) >= 3:
                            break
                        else:
                            samples.clear()
                    if len(samples) >= 3:
                        # Polyline goes 28m -> 1.5m; tangent = (-ux,-uy), left_normal = (uy,-ux) = (-nx,-ny)
                        # So inward direction toward road center (-side_sign * (nx,ny)) corresponds to side_sign * left_normal
                        _emit_guardrail_run(np.asarray(samples, dtype=np.float64), road, inward_side=side_sign, tag=f"br{road.osm_id}")

        # 4b) Outer curve & steep embankment guardrails along major highways
        for road in drivable_roads:
            if road.bridge or road.highway not in _GUARDRAIL_HIGHWAYS:
                continue
            pts = road.points
            _, cum = _polyline_cum_len(pts)
            total_len = float(cum[-1])
            if total_len < 40.0 or build is None or not hasattr(build, "elevations"):
                continue
            step = 4.0
            n_s = max(2, int(total_len / step) + 1)
            ds_arr = np.linspace(0.0, total_len, n_s)
            off = road.half_width_m + 0.55
            for side_sign in (-1.0, 1.0):
                run: list[tuple[float, float]] = []
                for k, d in enumerate(ds_arr):
                    cx, cy, tx, ty = _sample_polyline_at(pts, cum, float(d))
                    nx, ny = -ty, tx
                    gx = cx + side_sign * off * nx
                    gy = cy + side_sign * off * ny
                    out_x = cx + side_sign * (off + 4.0) * nx
                    out_y = cy + side_sign * (off + 4.0) * ny
                    if not (_in_terrain_bounds(gx, gy, build) and _in_terrain_bounds(out_x, out_y, build)):
                        if len(run) >= 5:
                            _emit_guardrail_run(np.asarray(run, dtype=np.float64), road, inward_side=-side_sign, tag=f"emb{road.osm_id}")
                        run = []
                        continue
                    z_road = _sample_z(cx, cy, build)
                    z_out = _sample_z(out_x, out_y, build)
                    steep_drop = (z_road - z_out) > 2.2
                    sharp_outer = False
                    if 0 < k < len(ds_arr) - 1:
                        _, _, txp, typ = _sample_polyline_at(pts, cum, float(ds_arr[k - 1]))
                        _, _, txn, tyn = _sample_polyline_at(pts, cum, float(ds_arr[k + 1]))
                        turn_cross = txp * tyn - typ * txn
                        # Positive cross = left turn -> outer side is right (-1.0)
                        if (turn_cross > 0.08 and side_sign < 0.0) or (turn_cross < -0.08 and side_sign > 0.0):
                            sharp_outer = True
                    if (steep_drop or sharp_outer) and carriageway_index_clear(gx, gy, road, idx):
                        run.append((gx, gy))
                    else:
                        if len(run) >= 5:
                            _emit_guardrail_run(np.asarray(run, dtype=np.float64), road, inward_side=-side_sign, tag=f"emb{road.osm_id}")
                        run = []
                if len(run) >= 5:
                    _emit_guardrail_run(np.asarray(run, dtype=np.float64), road, inward_side=-side_sign, tag=f"emb{road.osm_id}")

    return counts


def carriageway_index_clear(x: float, y: float, owner_road: Road, idx: CarriagewayIndex) -> bool:
    """Return True when ``(x, y)`` is clear of all OTHER road carriageways and self-centerline."""
    return not idx.is_blocked(
        x,
        y,
        margin_m=0.8,
        tangent=(0.0, 0.0),
        owner_id=owner_road.osm_id,
        layer=owner_road.layer,
        cut_all_angles=True,
    )
