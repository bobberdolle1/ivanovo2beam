"""3D procedural bridges subsystem for BeamNG.drive.

For OSM ways tagged with ``bridge=yes`` (or viaducts / boardwalks), this module
generates:
1. Procedural Collada 1.4.1 meshes (``.dae``) containing:
   - Closed manifold deck slab (width = road width + 1.2m, thickness = 0.65m)
     with UV mapping for ``ivanovo_bridge_deck``;
   - Continuous guardrails / parapets (0.85m high x 0.35m wide safety barriers
     along both deck edges);
   - Substructure abutments and concrete piers spaced every 25m down to bed.
2. BeamNG ``TSStatic`` scene object with:
   - ``collisionType: "Visible Mesh Final"``
   - ``decalType: "Visible Mesh"``
3. Materials definition in ``levels/<id>/art/shapes/bridges/main.materials.json``
   referencing native BeamNG concrete textures.
4. Bridgehead approach ramp corridors pinned to deck endpoints with 0cm step lip.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

import numpy as np

from ivanovo2beam.beamng.level import stable_uuid

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from ivanovo2beam.config import RoadSettings
    from ivanovo2beam.roads.network import Road
    from ivanovo2beam.terrain.builder import RoadCorridor

#: Fixed timestamp for byte-identical deterministic COLLADA outputs.
_FIXED_TIMESTAMP = "2026-01-01T00:00:00Z"

#: Material name bound in Collada and defined in main.materials.json
BRIDGE_DECAL_MATERIAL = "ivanovo_bridge_deck"

#: Base-game precast concrete textures referenced by path
CONCRETE_PRECAST_TEXTURES: dict[str, str] = {
    "ao": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_ao.data.dds",
    "b": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_b.color.dds",
    "nm": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_nm.normal.dds",
    "r": "/assets/materials/tileable/concrete/concrete_precast1/t_concrete_precast1_r.data.dds",
}

#: Bridge slab parameters (in metres)
SLAB_EXTRA_WIDTH_M = 1.2
SLAB_THICKNESS_M = 0.65
GUARDRAIL_HEIGHT_M = 0.85
GUARDRAIL_WIDTH_M = 0.35
PIER_SPACING_M = 25.0
PIER_THICKNESS_M = 1.6
ABUTMENT_LENGTH_M = 3.5


def _quad_face_normal(
    v0: tuple[float, float, float],
    v1: tuple[float, float, float],
    v2: tuple[float, float, float],
    fallback: tuple[float, float, float] = (0.0, 0.0, 1.0),
) -> tuple[float, float, float]:
    """Compute the unit outward normal of a CCW quad ``(v0, v1, v2, v3)``."""
    ax, ay, az = v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]
    bx, by, bz = v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]
    nx = ay * bz - az * by
    ny = az * bx - ax * bz
    nz = ax * by - ay * bx
    length = float(np.sqrt(nx * nx + ny * ny + nz * nz))
    if length < 1e-9:
        return fallback
    return (nx / length, ny / length, nz / length)


@dataclass(slots=True)
class BridgeGeometry:
    """Procedural 3D mesh representation for a bridge."""

    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    # Each triangle is a tuple of 3 indices: ((v0, n0, uv0), (v1, n1, uv1), (v2, n2, uv2))
    triangles: list[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]] = field(
        default_factory=list
    )
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def center(self) -> tuple[float, float, float]:
        """Return world-space bounding center ``(cx, cy, min_z)`` for localization."""
        if not self.positions:
            return self.origin
        xs = [p[0] for p in self.positions]
        ys = [p[1] for p in self.positions]
        zs = [p[2] for p in self.positions]
        return (
            round(self.origin[0] + 0.5 * (min(xs) + max(xs)), 4),
            round(self.origin[1] + 0.5 * (min(ys) + max(ys)), 4),
            round(self.origin[2] + min(zs), 4),
        )

    def add_triangle(
        self,
        v0: tuple[float, float, float],
        v1: tuple[float, float, float],
        v2: tuple[float, float, float],
        n: tuple[float, float, float],
        uv0: tuple[float, float],
        uv1: tuple[float, float],
        uv2: tuple[float, float],
    ) -> None:
        """Add a single triangle with specified normal and UVs."""
        idx_base = len(self.positions)
        self.positions.extend([v0, v1, v2])
        self.normals.extend([n, n, n])
        self.uvs.extend([uv0, uv1, uv2])
        self.triangles.append(
            ((idx_base, idx_base, idx_base), (idx_base + 1, idx_base + 1, idx_base + 1), (idx_base + 2, idx_base + 2, idx_base + 2))
        )

    def add_quad(
        self,
        v0: tuple[float, float, float],
        v1: tuple[float, float, float],
        v2: tuple[float, float, float],
        v3: tuple[float, float, float],
        n: tuple[float, float, float],
        uv0: tuple[float, float] = (0.0, 0.0),
        uv1: tuple[float, float] = (1.0, 0.0),
        uv2: tuple[float, float] = (1.0, 1.0),
        uv3: tuple[float, float] = (0.0, 1.0),
    ) -> None:
        """Add a quad (split into 2 triangles) with CCW winding: (v0, v1, v2) and (v0, v2, v3)."""
        self.add_triangle(v0, v1, v2, n, uv0, uv1, uv2)
        self.add_triangle(v0, v2, v3, n, uv0, uv2, uv3)

    def add_box(
        self,
        c000: tuple[float, float, float],
        c100: tuple[float, float, float],
        c110: tuple[float, float, float],
        c010: tuple[float, float, float],
        c001: tuple[float, float, float],
        c101: tuple[float, float, float],
        c111: tuple[float, float, float],
        c011: tuple[float, float, float],
    ) -> None:
        """Add a closed 6-sided box defined by 8 corner vertices (right-handed)."""
        faces = (
            (c001, c101, c111, c011, (0.0, 0.0, 1.0)),   # Top (+Z)
            (c000, c010, c110, c100, (0.0, 0.0, -1.0)),  # Bottom (-Z)
            (c000, c100, c101, c001, (0.0, -1.0, 0.0)),  # Front (-N)
            (c110, c010, c011, c111, (0.0, 1.0, 0.0)),   # Back (+N)
            (c010, c000, c001, c011, (-1.0, 0.0, 0.0)),  # Left (-T)
            (c100, c110, c111, c101, (1.0, 0.0, 0.0)),   # Right (+T)
        )
        for v0, v1, v2, v3, fallback in faces:
            self.add_quad(v0, v1, v2, v3, _quad_face_normal(v0, v1, v2, fallback))


def compute_bridge_deck_profile(
    road: Road,
    ground_z: np.ndarray | None = None,
    ground_sampler: Callable[[float, float], float] | None = None,
    clearance_m: float = 4.0,
) -> np.ndarray:
    """Compute the longitudinal 3D deck elevation profile for a bridge.

    Pins endpoints to terrain/ground elevation, and ensures carriageway spans
    at least clearance_m above the bed/valley below without introducing step
    kinks near the bridgeheads.
    """
    n = road.points.shape[0]
    if ground_z is not None and len(ground_z) == n:
        raw_z = np.asarray(ground_z, dtype=np.float64).copy()
    elif ground_sampler is not None:
        raw_z = np.array([ground_sampler(float(p[0]), float(p[1])) for p in road.points], dtype=np.float64)
    else:
        raw_z = np.zeros(n, dtype=np.float64)

    if n < 2:
        return raw_z

    deltas = np.diff(road.points, axis=0)
    seg_lens = np.hypot(deltas[:, 0], deltas[:, 1])
    cum_lens = np.concatenate(([0.0], np.cumsum(seg_lens)))
    total_len = float(cum_lens[-1])
    if total_len <= 1e-3:
        return raw_z

    z0 = float(raw_z[0])
    z1 = float(raw_z[-1])

    # A smooth linear chord between bridgehead endpoints, with tapered clearance
    # over interior spans so bank vertices near the ends do not cause a step kink.
    taper_len = max(total_len * 0.25, 1.0)
    deck_z = np.zeros(n, dtype=np.float64)
    for i in range(n):
        t = float(cum_lens[i] / total_len)
        chord_z = z0 + t * (z1 - z0)
        if i == 0:
            deck_z[i] = z0
        elif i == n - 1:
            deck_z[i] = z1
        else:
            d_nearest = float(min(cum_lens[i], total_len - cum_lens[i]))
            s = min(1.0, d_nearest / taper_len)
            smooth_s = s * s * (3.0 - 2.0 * s)
            target_z = max(chord_z, float(raw_z[i]) + clearance_m * smooth_s)
            deck_z[i] = min(target_z, chord_z + 0.08 * d_nearest)

    return deck_z


def build_bridge_geometry(
    road: Road,
    deck_z: np.ndarray,
    ground_sampler: Callable[[float, float], float] | None = None,
    *,
    localize: bool = False,
) -> BridgeGeometry:
    """Generate complete 3D procedural bridge geometry.

    Includes deck slab manifold, guardrails, abutments, and piers every 25m.

    Args:
        road: The bridge road.
        deck_z: Longitudinal elevations along road.points.
        ground_sampler: Optional function returning ground elevation at (x, y).
        localize: When True, shift vertex positions relative to ``geom.center``
            and store that world-space offset in ``geom.origin``.

    Returns:
        BridgeGeometry container ready for COLLADA export.
    """
    geom = BridgeGeometry()
    pts = np.asarray(road.points, dtype=np.float64)
    z_arr = np.asarray(deck_z, dtype=np.float64)
    if pts.shape[0] < 2:
        return geom

    # Deduplicate consecutive zero-length points while preserving endpoints
    keep = np.ones(pts.shape[0], dtype=bool)
    last_x, last_y = float(pts[0, 0]), float(pts[0, 1])
    for i in range(1, pts.shape[0]):
        if math.hypot(float(pts[i, 0]) - last_x, float(pts[i, 1]) - last_y) < 1e-6:
            keep[i] = False
        else:
            last_x, last_y = float(pts[i, 0]), float(pts[i, 1])
    pts = pts[keep]
    z_arr = z_arr[keep]
    n = pts.shape[0]
    if n < 2:
        return geom
    deck_z = z_arr

    # Calculate 2D unit tangents and left-hand normals without mutating raw lengths
    diffs = np.diff(pts, axis=0)
    raw_seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])
    safe_seg_lens = np.where(raw_seg_lens < 1e-6, 1.0, raw_seg_lens)
    seg_tangents = diffs / safe_seg_lens[:, None]

    normals = np.zeros((n, 2), dtype=np.float64)
    normals[0] = [-seg_tangents[0, 1], seg_tangents[0, 0]]
    normals[-1] = [-seg_tangents[-1, 1], seg_tangents[-1, 0]]
    if n > 2:
        mid_tangents = (seg_tangents[:-1] + seg_tangents[1:]) * 0.5
        mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
        degenerate = mid_lens < 1e-6
        if np.any(degenerate):
            mid_tangents[degenerate] = seg_tangents[:-1][degenerate]
            mid_lens = np.hypot(mid_tangents[:, 0], mid_tangents[:, 1])
            mid_lens = np.where(mid_lens < 1e-6, 1.0, mid_lens)
        mid_tangents /= mid_lens[:, None]
        normals[1:-1] = np.column_stack((-mid_tangents[:, 1], mid_tangents[:, 0]))

    deck_width = float(road.width_m + SLAB_EXTRA_WIDTH_M)
    deck_half = deck_width * 0.5
    thickness = SLAB_THICKNESS_M

    # 1. Deck slab closed manifold
    # Precompute cross-section vertices: TL (-N), TR (+N), BR (+N), BL (-N)
    tl: list[tuple[float, float, float]] = []
    tr: list[tuple[float, float, float]] = []
    br: list[tuple[float, float, float]] = []
    bl: list[tuple[float, float, float]] = []

    for i in range(n):
        px, py = float(pts[i, 0]), float(pts[i, 1])
        nx, ny = float(normals[i, 0]), float(normals[i, 1])
        top_z = float(deck_z[i])
        bot_z = top_z - thickness
        tl.append((px - deck_half * nx, py - deck_half * ny, top_z))
        tr.append((px + deck_half * nx, py + deck_half * ny, top_z))
        br.append((px + deck_half * nx, py + deck_half * ny, bot_z))
        bl.append((px - deck_half * nx, py - deck_half * ny, bot_z))

    # Extrude deck slab segments (CCW outward winding)
    cum_s = 0.0
    for i in range(n - 1):
        ds = float(raw_seg_lens[i])
        s0, s1 = cum_s * 0.25, (cum_s + ds) * 0.25
        cum_s += ds

        # Top face (+Z)
        geom.add_quad(
            tl[i], tl[i + 1], tr[i + 1], tr[i],
            _quad_face_normal(tl[i], tl[i + 1], tr[i + 1], (0.0, 0.0, 1.0)),
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )
        # Bottom face (-Z)
        geom.add_quad(
            bl[i], br[i], br[i + 1], bl[i + 1],
            _quad_face_normal(bl[i], br[i], br[i + 1], (0.0, 0.0, -1.0)),
            (0.0, s0), (1.0, s0), (1.0, s1), (0.0, s1),
        )
        # -N side (right of tangent)
        geom.add_quad(
            bl[i], bl[i + 1], tl[i + 1], tl[i],
            _quad_face_normal(bl[i], bl[i + 1], tl[i + 1], (-float(normals[i, 0]), -float(normals[i, 1]), 0.0)),
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )
        # +N side (left of tangent)
        geom.add_quad(
            tr[i], tr[i + 1], br[i + 1], br[i],
            _quad_face_normal(tr[i], tr[i + 1], br[i + 1], (float(normals[i, 0]), float(normals[i, 1]), 0.0)),
            (0.0, s0), (0.0, s1), (1.0, s1), (1.0, s0),
        )

    # Deck slab end caps
    t0 = seg_tangents[0]
    geom.add_quad(
        tl[0], tr[0], br[0], bl[0],
        (-float(t0[0]), -float(t0[1]), 0.0),
    )
    t_end = seg_tangents[-1]
    geom.add_quad(
        tl[-1], bl[-1], br[-1], tr[-1],
        (float(t_end[0]), float(t_end[1]), 0.0),
    )

    # 2. Guardrails / Parapets (0.85m high x 0.35m wide safety barriers)
    gh = GUARDRAIL_HEIGHT_M
    gw = GUARDRAIL_WIDTH_M

    for i in range(n - 1):
        nx0, ny0 = float(normals[i, 0]), float(normals[i, 1])
        nx1, ny1 = float(normals[i + 1, 0]), float(normals[i + 1, 1])
        px0, py0, z0 = float(pts[i, 0]), float(pts[i, 1]), float(deck_z[i])
        px1, py1, z1 = float(pts[i + 1, 0]), float(pts[i + 1, 1]), float(deck_z[i + 1])

        # -N Guardrail: outer edge at -deck_half, inner edge at -(deck_half - gw)
        l_out_bot0 = (px0 - deck_half * nx0, py0 - deck_half * ny0, z0)
        l_out_top0 = (px0 - deck_half * nx0, py0 - deck_half * ny0, z0 + gh)
        l_in_top0 = (px0 - (deck_half - gw) * nx0, py0 - (deck_half - gw) * ny0, z0 + gh)
        l_in_bot0 = (px0 - (deck_half - gw) * nx0, py0 - (deck_half - gw) * ny0, z0)

        l_out_bot1 = (px1 - deck_half * nx1, py1 - deck_half * ny1, z1)
        l_out_top1 = (px1 - deck_half * nx1, py1 - deck_half * ny1, z1 + gh)
        l_in_top1 = (px1 - (deck_half - gw) * nx1, py1 - (deck_half - gw) * ny1, z1 + gh)
        l_in_bot1 = (px1 - (deck_half - gw) * nx1, py1 - (deck_half - gw) * ny1, z1)

        # -N guardrail top face (+Z)
        geom.add_quad(
            l_out_top0, l_out_top1, l_in_top1, l_in_top0,
            _quad_face_normal(l_out_top0, l_out_top1, l_in_top1, (0.0, 0.0, 1.0)),
        )
        # -N guardrail inner face (facing road, normal = +N)
        geom.add_quad(
            l_in_top0, l_in_top1, l_in_bot1, l_in_bot0,
            _quad_face_normal(l_in_top0, l_in_top1, l_in_bot1, (nx0, ny0, 0.0)),
        )
        # -N guardrail outer face (facing outward, normal = -N)
        geom.add_quad(
            l_out_bot0, l_out_bot1, l_out_top1, l_out_top0,
            _quad_face_normal(l_out_bot0, l_out_bot1, l_out_top1, (-nx0, -ny0, 0.0)),
        )

        # +N Guardrail: inner edge at +(deck_half - gw), outer edge at +deck_half
        r_in_bot0 = (px0 + (deck_half - gw) * nx0, py0 + (deck_half - gw) * ny0, z0)
        r_in_top0 = (px0 + (deck_half - gw) * nx0, py0 + (deck_half - gw) * ny0, z0 + gh)
        r_out_top0 = (px0 + deck_half * nx0, py0 + deck_half * ny0, z0 + gh)
        r_out_bot0 = (px0 + deck_half * nx0, py0 + deck_half * ny0, z0)

        r_in_bot1 = (px1 + (deck_half - gw) * nx1, py1 + (deck_half - gw) * ny1, z1)
        r_in_top1 = (px1 + (deck_half - gw) * nx1, py1 + (deck_half - gw) * ny1, z1 + gh)
        r_out_top1 = (px1 + deck_half * nx1, py1 + deck_half * ny1, z1 + gh)
        r_out_bot1 = (px1 + deck_half * nx1, py1 + deck_half * ny1, z1)

        # +N guardrail top face (+Z)
        geom.add_quad(
            r_in_top0, r_in_top1, r_out_top1, r_out_top0,
            _quad_face_normal(r_in_top0, r_in_top1, r_out_top1, (0.0, 0.0, 1.0)),
        )
        # +N guardrail inner face (facing road, normal = -N)
        geom.add_quad(
            r_in_bot0, r_in_bot1, r_in_top1, r_in_top0,
            _quad_face_normal(r_in_bot0, r_in_bot1, r_in_top1, (-nx0, -ny0, 0.0)),
        )
        # +N guardrail outer face (facing outward, normal = +N)
        geom.add_quad(
            r_out_top0, r_out_top1, r_out_bot1, r_out_bot0,
            _quad_face_normal(r_out_top0, r_out_top1, r_out_bot1, (nx0, ny0, 0.0)),
        )

    # Guardrail end caps
    nx0, ny0 = float(normals[0, 0]), float(normals[0, 1])
    px0, py0, z0 = float(pts[0, 0]), float(pts[0, 1]), float(deck_z[0])
    t0 = seg_tangents[0]
    # Start end caps (-T)
    geom.add_quad(
        (px0 - deck_half * nx0, py0 - deck_half * ny0, z0 + gh),
        (px0 - (deck_half - gw) * nx0, py0 - (deck_half - gw) * ny0, z0 + gh),
        (px0 - (deck_half - gw) * nx0, py0 - (deck_half - gw) * ny0, z0),
        (px0 - deck_half * nx0, py0 - deck_half * ny0, z0),
        (-float(t0[0]), -float(t0[1]), 0.0),
    )
    geom.add_quad(
        (px0 + (deck_half - gw) * nx0, py0 + (deck_half - gw) * ny0, z0 + gh),
        (px0 + deck_half * nx0, py0 + deck_half * ny0, z0 + gh),
        (px0 + deck_half * nx0, py0 + deck_half * ny0, z0),
        (px0 + (deck_half - gw) * nx0, py0 + (deck_half - gw) * ny0, z0),
        (-float(t0[0]), -float(t0[1]), 0.0),
    )

    nx1, ny1 = float(normals[-1, 0]), float(normals[-1, 1])
    px1, py1, z1 = float(pts[-1, 0]), float(pts[-1, 1]), float(deck_z[-1])
    t_end = seg_tangents[-1]
    # End end caps (+T)
    geom.add_quad(
        (px1 - deck_half * nx1, py1 - deck_half * ny1, z1 + gh),
        (px1 - deck_half * nx1, py1 - deck_half * ny1, z1),
        (px1 - (deck_half - gw) * nx1, py1 - (deck_half - gw) * ny1, z1),
        (px1 - (deck_half - gw) * nx1, py1 - (deck_half - gw) * ny1, z1 + gh),
        (float(t_end[0]), float(t_end[1]), 0.0),
    )
    geom.add_quad(
        (px1 + (deck_half - gw) * nx1, py1 + (deck_half - gw) * ny1, z1 + gh),
        (px1 + (deck_half - gw) * nx1, py1 + (deck_half - gw) * ny1, z1),
        (px1 + deck_half * nx1, py1 + deck_half * ny1, z1),
        (px1 + deck_half * nx1, py1 + deck_half * ny1, z1 + gh),
        (float(t_end[0]), float(t_end[1]), 0.0),
    )

    # 3. Abutments at start and end
    def add_abutment(pt: tuple[float, float], z_deck: float, tangent: np.ndarray, normal: np.ndarray) -> None:
        px, py = pt
        tx, ty = float(tangent[0]), float(tangent[1])
        nx, ny = float(normal[0]), float(normal[1])
        top_z = z_deck - thickness
        ground = ground_sampler(px, py) if ground_sampler is not None else (top_z - 4.0)
        bot_z = min(top_z - 1.0, ground - 1.5)  # embed securely into ground

        # Rectangular box beneath deck
        w_half = deck_half
        l_len = ABUTMENT_LENGTH_M

        c000 = (px - l_len * tx - w_half * nx, py - l_len * ty - w_half * ny, bot_z)
        c100 = (px - w_half * nx, py - w_half * ny, bot_z)
        c110 = (px + w_half * nx, py + w_half * ny, bot_z)
        c010 = (px - l_len * tx + w_half * nx, py - l_len * ty + w_half * ny, bot_z)

        c001 = (px - l_len * tx - w_half * nx, py - l_len * ty - w_half * ny, top_z)
        c101 = (px - w_half * nx, py - w_half * ny, top_z)
        c111 = (px + w_half * nx, py + w_half * ny, top_z)
        c011 = (px - l_len * tx + w_half * nx, py - l_len * ty + w_half * ny, top_z)

        geom.add_box(c000, c100, c110, c010, c001, c101, c111, c011)

    add_abutment((float(pts[0, 0]), float(pts[0, 1])), float(deck_z[0]), seg_tangents[0], normals[0])
    # Negate both tangent and normal to preserve right-handedness (-T) x (-N) = +Z
    add_abutment((float(pts[-1, 0]), float(pts[-1, 1])), float(deck_z[-1]), -seg_tangents[-1], -normals[-1])

    # 4. Concrete piers every 25m down to bed
    cum_lens = np.concatenate(([0.0], np.cumsum(raw_seg_lens)))
    total_len = float(cum_lens[-1])
    pier_dist = PIER_SPACING_M

    while pier_dist < total_len - 12.0:
        # Interpolate location at pier_dist
        idx = int(np.searchsorted(cum_lens, pier_dist, side="right") - 1)
        idx = max(0, min(idx, len(raw_seg_lens) - 1))
        d0 = float(cum_lens[idx])
        slen = float(raw_seg_lens[idx])
        t = 0.0 if slen <= 1e-6 else (pier_dist - d0) / slen

        px = float(pts[idx, 0] + t * (pts[idx + 1, 0] - pts[idx, 0]))
        py = float(pts[idx, 1] + t * (pts[idx + 1, 1] - pts[idx, 1]))
        pz = float(deck_z[idx] + t * (deck_z[idx + 1] - deck_z[idx]))

        tx, ty = float(seg_tangents[idx, 0]), float(seg_tangents[idx, 1])
        nx, ny = float(normals[idx, 0]), float(normals[idx, 1])

        top_z = pz - thickness
        bed_z = ground_sampler(px, py) if ground_sampler is not None else (top_z - 6.0)
        bot_z = min(top_z - 1.0, bed_z - 0.5)

        p_width = max(2.0, deck_width - 1.6)
        pw_half = p_width * 0.5
        pl_half = PIER_THICKNESS_M * 0.5

        # 8 box corners for pier column
        c000 = (px - pl_half * tx - pw_half * nx, py - pl_half * ty - pw_half * ny, bot_z)
        c100 = (px + pl_half * tx - pw_half * nx, py + pl_half * ty - pw_half * ny, bot_z)
        c110 = (px + pl_half * tx + pw_half * nx, py + pl_half * ty + pw_half * ny, bot_z)
        c010 = (px - pl_half * tx + pw_half * nx, py - pl_half * ty + pw_half * ny, bot_z)

        c001 = (px - pl_half * tx - pw_half * nx, py - pl_half * ty - pw_half * ny, top_z)
        c101 = (px + pl_half * tx - pw_half * nx, py + pl_half * ty - pw_half * ny, top_z)
        c111 = (px + pl_half * tx + pw_half * nx, py + pl_half * ty + pw_half * ny, top_z)
        c011 = (px - pl_half * tx + pw_half * nx, py - pl_half * ty + pw_half * ny, top_z)

        geom.add_box(c000, c100, c110, c010, c001, c101, c111, c011)

        pier_dist += PIER_SPACING_M

    if localize and geom.positions:
        ox, oy, oz = geom.center
        geom.positions = [(px - ox, py - oy, pz - oz) for px, py, pz in geom.positions]
        geom.origin = (ox, oy, oz)

    return geom


def localize_bridge_geometry(
    geom: BridgeGeometry,
    origin: tuple[float, float, float] | None = None,
) -> BridgeGeometry:
    """Return a copy of ``geom`` with positions localized around ``origin``.

    If ``origin`` is omitted, ``geom.center`` is used. Safe to call on an
    already-localized ``BridgeGeometry`` (computes delta relative to ``geom.origin``).
    """
    target = geom.center if origin is None else (float(origin[0]), float(origin[1]), float(origin[2]))
    dx = target[0] - geom.origin[0]
    dy = target[1] - geom.origin[1]
    dz = target[2] - geom.origin[2]
    return BridgeGeometry(
        positions=[(px - dx, py - dy, pz - dz) for px, py, pz in geom.positions],
        normals=list(geom.normals),
        uvs=list(geom.uvs),
        triangles=list(geom.triangles),
        origin=target,
    )


def _format_floats(values: Sequence[float]) -> str:
    """Format a sequence of floats compactly."""
    return " ".join(f"{v:.4f}".rstrip("0").rstrip(".") or "0" for v in values)


def export_bridge_dae(
    path: Path,
    geom: BridgeGeometry,
    bridge_name: str,
    *,
    origin: tuple[float, float, float] | None = None,
) -> int:
    """Write bridge geometry to a BeamNG-compatible COLLADA 1.4.1 file.

    Args:
        path: Output file path.
        geom: Geometry container.
        bridge_name: Shape name (e.g. ``bridge_way12345``).
        origin: Optional world-space origin ``(ox, oy, oz)`` to subtract from
            ``geom.positions`` so the exported mesh stays near local ``(0, 0, 0)``.
            Accounts for ``geom.origin`` so localized meshes are never shifted twice.

    Returns:
        Number of bytes written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if origin is None:
        dx = dy = dz = 0.0
    else:
        dx = float(origin[0]) - geom.origin[0]
        dy = float(origin[1]) - geom.origin[1]
        dz = float(origin[2]) - geom.origin[2]

    if dx == 0.0 and dy == 0.0 and dz == 0.0:
        pos_flat = [coord for p in geom.positions for coord in p]
    else:
        pos_flat = [coord for p in geom.positions for coord in (p[0] - dx, p[1] - dy, p[2] - dz)]
    norm_flat = [coord for n in geom.normals for coord in n]
    uv_flat = [coord for uv in geom.uvs for coord in uv]

    pos_str = _format_floats(pos_flat)
    norm_str = _format_floats(norm_flat)
    uv_str = _format_floats(uv_flat)

    p_entries: list[str] = []
    for tri in geom.triangles:
        for vi, ni, uvi in tri:
            p_entries.append(f"{vi} {ni} {uvi}")
    p_str = " ".join(p_entries)

    mat_name = BRIDGE_DECAL_MATERIAL
    safe_name = escape(bridge_name)

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor>
      <author>ivanovo2beam</author>
      <authoring_tool>ivanovo2beam 3D bridge generator</authoring_tool>
    </contributor>
    <created>{_FIXED_TIMESTAMP}</created>
    <modified>{_FIXED_TIMESTAMP}</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="{safe_name}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse>
              <color>0.7 0.7 0.7 1</color>
            </diffuse>
          </lambert>
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
          <float_array id="{safe_name}-mesh-positions-array" count="{len(pos_flat)}">{pos_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-positions-array" count="{len(geom.positions)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{safe_name}-mesh-normals">
          <float_array id="{safe_name}-mesh-normals-array" count="{len(norm_flat)}">{norm_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-normals-array" count="{len(geom.normals)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{safe_name}-mesh-map-0">
          <float_array id="{safe_name}-mesh-map-0-array" count="{len(uv_flat)}">{uv_str}</float_array>
          <technique_common>
            <accessor source="#{safe_name}-mesh-map-0-array" count="{len(geom.uvs)}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{safe_name}-mesh-vertices">
          <input semantic="POSITION" source="#{safe_name}-mesh-positions"/>
        </vertices>
        <triangles material="{mat_name}" count="{len(geom.triangles)}">
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
          <bind_material>
            <technique_common>
              <instance_material symbol="{mat_name}" target="#{mat_name}"/>
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#Scene"/>
  </scene>
</COLLADA>
"""
    data = xml.encode("utf-8")
    path.write_bytes(data)
    return len(data)


def emit_bridges_materials(level_dir: Path, level_id: str) -> Path:
    """Emit ``levels/<id>/art/shapes/bridges/main.materials.json``.

    Maps ``ivanovo_bridge_deck`` to BeamNG native precast concrete PBR textures.
    """
    materials_path = level_dir / "art" / "shapes" / "bridges" / "main.materials.json"
    materials_path.parent.mkdir(parents=True, exist_ok=True)

    materials_doc = {
        BRIDGE_DECAL_MATERIAL: {
            "name": BRIDGE_DECAL_MATERIAL,
            "mapTo": BRIDGE_DECAL_MATERIAL,
            "class": "Material",
            "persistentId": stable_uuid(level_id, "Material", BRIDGE_DECAL_MATERIAL),
            "Stages": [
                {
                    "ambientOcclusionMap": CONCRETE_PRECAST_TEXTURES["ao"],
                    "baseColorMap": CONCRETE_PRECAST_TEXTURES["b"],
                    "normalMap": CONCRETE_PRECAST_TEXTURES["nm"],
                    "roughnessMap": CONCRETE_PRECAST_TEXTURES["r"],
                },
                {},
                {},
                {},
            ],
            "version": 1.5,
        }
    }

    materials_path.write_text(json.dumps(materials_doc, indent=2), encoding="utf-8")
    return materials_path


def build_bridge_scene_object(
    road: Road,
    level_id: str,
    *,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    shape_path: str | None = None,
    parent: str = "bridges",
) -> dict[str, Any]:
    """Place a bridge mesh as a BeamNG ``TSStatic`` scene object.

    Uses ``collisionType: "Visible Mesh Final"`` and ``decalType: "Visible Mesh"``
    so player vehicles collide with the bridge deck and DecalRoads project
    directly onto the surface.
    """
    key = f"bridge_way{road.osm_id}"
    if shape_path is None:
        shape_path = f"/levels/{level_id}/art/shapes/bridges/{key}.dae"

    return {
        "class": "TSStatic",
        "persistentId": stable_uuid(level_id, "TSStatic", key),
        "__parent": parent,
        "position": [float(position[0]), float(position[1]), float(position[2])],
        "shapeName": shape_path,
        "scale": [1.0, 1.0, 1.0],
        "collisionType": "Visible Mesh Final",
        "decalType": "Visible Mesh",
    }


def create_bridgehead_ramps(
    road: Road,
    settings: RoadSettings,
    deck_z: np.ndarray,
    ground_sampler: Callable[[float, float], float] | None = None,
    ramp_length_m: float = 25.0,
) -> list[RoadCorridor]:
    """Generate approach ramp corridors pinned to deck endpoints with 0cm step lip.

    Grading ramps smoothly connect existing terrain to the bridge deck level,
    eliminating any vertical barrier or collision step at bridge entrances.
    """
    from ivanovo2beam.roads.build import corridor_half_width_for
    from ivanovo2beam.terrain.builder import RoadCorridor

    pts = road.points
    if pts.shape[0] < 2:
        return []

    corridors: list[RoadCorridor] = []
    half_w = road.half_width_m
    corridor_half = corridor_half_width_for(road, settings)
    shoulder_material_key = (
        "Grass"
        if road.material_key in {"Asphalt", "Asphalt_worn", "Concrete"}
        else ("Dirt" if road.material_key == "Dirt" else "Gravel")
    )

    # 1. Start bridgehead ramp
    v_start = pts[0] - pts[1]
    v_start_len = float(np.hypot(v_start[0], v_start[1]))
    if v_start_len > 1e-4:
        u_start = v_start / v_start_len
        p_far = pts[0] + u_start * ramp_length_m
        z_pin = float(deck_z[0])
        z_far = ground_sampler(float(p_far[0]), float(p_far[1])) if ground_sampler is not None else z_pin
        ramp_pts = np.array([p_far, pts[0]], dtype=np.float64)
        ramp_z = np.array([z_far, z_pin], dtype=np.float64)
        corridors.append(
            RoadCorridor(
                points=ramp_pts,
                half_width_m=half_w,
                corridor_half_width_m=corridor_half,
                priority=road.priority + 2,
                grade_terrain=True,
                material_key=road.material_key,
                shoulder_material_key=shoulder_material_key,
                key=f"ramp_start_way{road.osm_id}",
                surface_z=ramp_z,
            )
        )

    # 2. End bridgehead ramp
    v_end = pts[-1] - pts[-2]
    v_end_len = float(np.hypot(v_end[0], v_end[1]))
    if v_end_len > 1e-4:
        u_end = v_end / v_end_len
        p_far = pts[-1] + u_end * ramp_length_m
        z_pin = float(deck_z[-1])
        z_far = ground_sampler(float(p_far[0]), float(p_far[1])) if ground_sampler is not None else z_pin
        ramp_pts = np.array([pts[-1], p_far], dtype=np.float64)
        ramp_z = np.array([z_pin, z_far], dtype=np.float64)
        corridors.append(
            RoadCorridor(
                points=ramp_pts,
                half_width_m=half_w,
                corridor_half_width_m=corridor_half,
                priority=road.priority + 2,
                grade_terrain=True,
                material_key=road.material_key,
                shoulder_material_key=shoulder_material_key,
                key=f"ramp_end_way{road.osm_id}",
                surface_z=ramp_z,
            )
        )

    return corridors
