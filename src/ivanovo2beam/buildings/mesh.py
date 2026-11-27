"""Generate building geometry and write it as COLLADA.

BeamNG 0.39 loads plain ``.dae`` files directly. This module builds clean,
watertight building meshes with multi-material support, slope skirts for hillsides,
courtyard hole cutouts, and texture coordinates (UVs) suitable for native PBR materials.

Geometry:
- MeshBuilder manages vertices, normals, and texture coordinates with spatial indexing;
- Extrusion supports exterior rings, inner courtyard holes, and multi-height parts;
- Downhill slope skirts extend foundation walls downwards (min -3.0m) to prevent
  buildings from floating on slopes (such as Plyos bluffs or Uvod riverbanks);
- Texture UVs are mapped in real-world metres for seamless tileable brick, concrete,
  and wood textures;
- COLLADA 1.4.1 export supports multiple materials per mesh geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

import numpy as np
from shapely import constrained_delaunay_triangles
from shapely.geometry import Point, Polygon, box

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from numpy.typing import NDArray

#: Fixed timestamp written into every generated DAE, so output is reproducible.
_FIXED_TIMESTAMP = "2026-01-01T00:00:00"

#: Roof pitch as a fraction of half the building depth.
_ROOF_PITCH = 0.30

#: Bounds on generated roof height, in metres.
_MIN_ROOF_M = 0.8
_MAX_ROOF_M = 4.5

_ROOF_ALIASES: dict[str, str] = {
    "flat": "flat",
    "flat_roof": "flat",
    "hip": "hip",
    "hipped": "hip",
    "pyramidal": "hip",
    "half-hipped": "hip",
    "skillion": "skillion",
    "lean_to": "skillion",
    "gable": "gable",
    "gabled": "gable",
    "pitched": "gable",
    "gambrel": "gable",
    "mansard": "gable",
    "hipped-and-gabled": "gable",
    "saltbox": "gable",
    "round": "gable",
}


def normalize_roof_shape(roof: str | None, fallback: str = "gable") -> str:
    """Normalise an OSM ``roof:shape`` tag into ``flat``, ``hip``, ``skillion``, or ``gable``."""
    key = (roof or fallback).strip().lower()
    return _ROOF_ALIASES.get(key, _ROOF_ALIASES.get(fallback.strip().lower(), "gable"))


@dataclass(slots=True)
class Mesh:
    """A flat-shaded triangle mesh with per-corner normals and UVs.

    Attributes:
        positions: Unique vertex positions.
        normals: Unique normal vectors.
        triangles: Triangles with ``(position_index, normal_index)`` pairs.
        uvs: Unique UV texture coordinates.
        triangle_uvs: Per-triangle UV indices ``(uv0, uv1, uv2)``.
        triangle_materials: Per-triangle material identifiers.
    """

    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    triangles: list[tuple[tuple[int, int], tuple[int, int], tuple[int, int]]] = field(
        default_factory=list
    )
    uvs: list[tuple[float, float]] = field(default_factory=list)
    triangle_uvs: list[tuple[int, int, int]] = field(default_factory=list)
    triangle_materials: list[str] = field(default_factory=list)

    _position_index: dict[tuple[float, float, float], int] = field(
        default_factory=dict, repr=False, compare=False
    )
    _normal_index: dict[tuple[float, float, float], int] = field(
        default_factory=dict, repr=False, compare=False
    )
    _uv_index: dict[tuple[float, float], int] = field(
        default_factory=dict, repr=False, compare=False
    )

    def add_triangle(
        self,
        a: tuple[float, float, float],
        b: tuple[float, float, float],
        c: tuple[float, float, float],
        normal: tuple[float, float, float] | None = None,
        uv0: tuple[float, float] = (0.0, 0.0),
        uv1: tuple[float, float] = (1.0, 0.0),
        uv2: tuple[float, float] = (0.0, 1.0),
        material: str = "default",
    ) -> None:
        """Append a triangle, computing its normal if not provided."""
        rounded = tuple(_round3(p) for p in (a, b, c))
        if len(set(rounded)) < 3:
            return
        fn = normal if normal is not None else _face_normal(*rounded)
        if fn is None:
            return
        vi0 = self._vertex(rounded[0])
        vi1 = self._vertex(rounded[1])
        vi2 = self._vertex(rounded[2])
        ni = self._normal(fn)
        uvi0 = self._uv(uv0)
        uvi1 = self._uv(uv1)
        uvi2 = self._uv(uv2)

        self.triangles.append(((vi0, ni), (vi1, ni), (vi2, ni)))
        self.triangle_uvs.append((uvi0, uvi1, uvi2))
        self.triangle_materials.append(material)

    def _vertex(self, position: tuple[float, float, float]) -> int:
        key = (round(position[0], 4), round(position[1], 4), round(position[2], 4))
        index = self._position_index.get(key)
        if index is None:
            index = len(self.positions)
            self._position_index[key] = index
            self.positions.append(key)
        return index

    def _normal(self, normal: tuple[float, float, float]) -> int:
        key = (round(normal[0], 5), round(normal[1], 5), round(normal[2], 5))
        index = self._normal_index.get(key)
        if index is None:
            index = len(self.normals)
            self._normal_index[key] = index
            self.normals.append(key)
        return index

    def _uv(self, uv: tuple[float, float]) -> int:
        key = (round(uv[0], 5), round(uv[1], 5))
        index = self._uv_index.get(key)
        if index is None:
            index = len(self.uvs)
            self._uv_index[key] = index
            self.uvs.append(key)
        return index

    @property
    def triangle_count(self) -> int:
        """Number of triangles in the mesh."""
        return len(self.triangles)

    def bounds_z(self) -> tuple[float, float]:
        """Return ``(min_z, max_z)`` over all vertices."""
        if not self.positions:
            return (0.0, 0.0)
        zs = [p[2] for p in self.positions]
        return (min(zs), max(zs))

    def unique_materials(self) -> list[str]:
        """Return distinct material keys in insertion order."""
        seen: set[str] = set()
        out: list[str] = []
        for mat in self.triangle_materials:
            if mat not in seen:
                seen.add(mat)
                out.append(mat)
        return out or ["default"]


def _round3(p: tuple[float, float, float]) -> tuple[float, float, float]:
    """Round a position the way the mesh stores it."""
    return (round(p[0], 4), round(p[1], 4), round(p[2], 4))


def _centre(values: NDArray[np.float64]) -> float:
    """Return the midpoint of a coordinate range."""
    return float((values.min() + values.max()) / 2.0)


def _face_normal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float] | None:
    """Return the unit normal of a triangle, or None if degenerate."""
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-12:
        return None
    return (nx / length, ny / length, nz / length)


def roof_height_for(ring: NDArray[np.float64], klass_roof: str) -> float:
    """Return a plausible roof height for a footprint."""
    roof = normalize_roof_shape(klass_roof)
    if roof == "flat":
        return 0.0
    xs = ring[:, 0]
    ys = ring[:, 1]
    depth = min(float(xs.max() - xs.min()), float(ys.max() - ys.min()))
    height = float(np.clip(depth * _ROOF_PITCH, _MIN_ROOF_M, _MAX_ROOF_M))
    return min(height, 2.2) if roof == "skillion" else height


class MeshBuilder:
    """Clean, robust mesh generator incorporating BeamNG.WorldForge principles.

    Manages spatial vertex deduplication, outward normal computation,
    wrapped wall UVs, top-down roof UVs, and multi-material groups.
    """

    def __init__(self) -> None:
        self.mesh = Mesh()

    def add_quad(
        self,
        p0: tuple[float, float, float],
        p1: tuple[float, float, float],
        p2: tuple[float, float, float],
        p3: tuple[float, float, float],
        *,
        normal: tuple[float, float, float] | None = None,
        uvs: tuple[
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
            tuple[float, float],
        ] | None = None,
        material: str = "default",
    ) -> None:
        """Add a quad made of two triangles (p0, p1, p2) and (p0, p2, p3)."""
        if uvs is None:
            uv0, uv1, uv2, uv3 = (0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)
        else:
            uv0, uv1, uv2, uv3 = uvs

        self.mesh.add_triangle(p0, p1, p2, normal=normal, uv0=uv0, uv1=uv1, uv2=uv2, material=material)
        self.mesh.add_triangle(p0, p2, p3, normal=normal, uv0=uv0, uv1=uv2, uv2=uv3, material=material)

    def add_wall_strip(
        self,
        ring: NDArray[np.float64],
        base_zs: Sequence[float],
        top_zs: Sequence[float],
        *,
        material: str = "wall",
        is_inner: bool = False,
        tile_width_m: float = 3.2,
        tile_height_m: float = 3.0,
    ) -> None:
        """Add vertical wall quads around a closed ring.

        Args:
            ring: Closed ``(n, 2)`` ring.
            base_zs: Base Z for each vertex.
            top_zs: Top Z for each vertex.
            material: Material key.
            is_inner: True if this is an inner courtyard ring.
            tile_width_m: Horizontal texture repeat size in metres.
            tile_height_m: Vertical texture repeat size in metres.
        """
        n = ring.shape[0] - 1
        s = 0.0
        min_base = min(base_zs)
        for i in range(n):
            x0, y0 = float(ring[i, 0]), float(ring[i, 1])
            x1, y1 = float(ring[i + 1, 0]), float(ring[i + 1, 1])
            edge_len = math.hypot(x1 - x0, y1 - y0)
            if edge_len < 1e-6:
                continue

            z_bot0 = float(base_zs[i])
            z_bot1 = float(base_zs[i + 1])
            z_top0 = float(top_zs[i])
            z_top1 = float(top_zs[i + 1])

            s0 = s
            s1 = s + edge_len
            s = s1

            u0 = s0 / tile_width_m
            u1 = s1 / tile_width_m
            v_bot0 = (z_bot0 - min_base) / tile_height_m
            v_bot1 = (z_bot1 - min_base) / tile_height_m
            v_top0 = (z_top0 - min_base) / tile_height_m
            v_top1 = (z_top1 - min_base) / tile_height_m

            p_bot0 = (x0, y0, z_bot0)
            p_bot1 = (x1, y1, z_bot1)
            p_top0 = (x0, y0, z_top0)
            p_top1 = (x1, y1, z_top1)

            if not is_inner:
                # CCW winding: outward wall normal
                self.add_quad(
                    p_bot0,
                    p_bot1,
                    p_top1,
                    p_top0,
                    uvs=((u0, v_bot0), (u1, v_bot1), (u1, v_top1), (u0, v_top0)),
                    material=material,
                )
            else:
                # CW winding for courtyard: inward facing wall normal
                self.add_quad(
                    p_bot1,
                    p_bot0,
                    p_top0,
                    p_top1,
                    uvs=((u1, v_bot1), (u0, v_bot0), (u0, v_top0), (u1, v_top1)),
                    material=material,
                )

    def add_roof_triangles(
        self,
        polygon: Polygon,
        lift_fn: Callable[[float, float], float],
        *,
        material: str = "roof",
        up: bool = True,
        tile_size_m: float = 4.0,
    ) -> None:
        """Triangulate a polygon (supporting holes) and emit oriented roof triangles."""
        if polygon.is_empty or polygon.area <= 0:
            return
        collection = constrained_delaunay_triangles(polygon)
        sub_polys = _iter_polygons(collection)
        for tri in sub_polys:
            # Only keep triangles inside the polygon (excluding courtyard holes!)
            if not polygon.contains(tri.representative_point()):
                continue
            coords = list(tri.exterior.coords)[:3]
            pts = [(float(p[0]), float(p[1])) for p in coords]
            area2 = (
                (pts[1][0] - pts[0][0]) * (pts[2][1] - pts[0][1])
                - (pts[2][0] - pts[0][0]) * (pts[1][1] - pts[0][1])
            )
            if abs(area2) < 1e-9:
                continue
            order = pts if (area2 > 0) == up else pts[::-1]
            p0 = (order[0][0], order[0][1], lift_fn(*order[0]))
            p1 = (order[1][0], order[1][1], lift_fn(*order[1]))
            p2 = (order[2][0], order[2][1], lift_fn(*order[2]))
            uv0 = (order[0][0] / tile_size_m, order[0][1] / tile_size_m)
            uv1 = (order[1][0] / tile_size_m, order[1][1] / tile_size_m)
            uv2 = (order[2][0] / tile_size_m, order[2][1] / tile_size_m)
            self.mesh.add_triangle(p0, p1, p2, uv0=uv0, uv1=uv1, uv2=uv2, material=material)

    def build(self) -> Mesh:
        """Return the completed mesh."""
        return self.mesh


def _iter_polygons(geom: object) -> list[Polygon]:
    """Extract valid non-empty Polygon parts from any Shapely geometry."""
    if isinstance(geom, Polygon):
        return [geom] if not geom.is_empty and geom.area > 1e-9 else []
    geoms = getattr(geom, "geoms", None)
    if geoms is None:
        return []
    result: list[Polygon] = []
    for sub in geoms:
        result.extend(_iter_polygons(sub))
    return result


def _principal_angle(ring: NDArray[np.float64]) -> float:
    """Return the orientation of the ring's longest edge, in radians."""
    deltas = np.diff(ring, axis=0)
    if deltas.size == 0:
        return 0.0
    lengths = np.hypot(deltas[:, 0], deltas[:, 1])
    longest = int(np.argmax(lengths))
    return math.atan2(float(deltas[longest, 1]), float(deltas[longest, 0]))


def _split_ring_at_y(ring: NDArray[np.float64], cut_y: float) -> NDArray[np.float64]:
    """Insert vertices where any ring edge crosses ``y = cut_y``."""
    out: list[tuple[float, float]] = []
    n = ring.shape[0] - 1
    for i in range(n):
        x0, y0 = float(ring[i, 0]), float(ring[i, 1])
        x1, y1 = float(ring[i + 1, 0]), float(ring[i + 1, 1])
        out.append((x0, y0))
        if (y0 - cut_y) * (y1 - cut_y) < 0.0 and abs(y1 - y0) > 1e-12:
            t = (cut_y - y0) / (y1 - y0)
            out.append((x0 + t * (x1 - x0), cut_y))
    out.append(out[0])
    return np.asarray(out, dtype=np.float64)


def extrude_footprint(
    ring: NDArray[np.float64],
    *,
    height_m: float,
    roof: str = "gable",
    roof_height_m: float | None = None,
    holes: tuple[NDArray[np.float64], ...] = (),
    min_height_m: float = 0.0,
    skirt_depth_m: float = 0.0,
    ground_z_fn: Callable[[float, float], float] | None = None,
    wall_material: str = "wall",
    roof_material: str = "roof",
) -> Mesh:
    """Build a closed building mesh from a footprint.

    Centred on the footprint's centroid in X/Y with ground level at z = 0
    (or base offset min_height_m). Supports inner courtyard holes and downwards
    slope skirts.
    """
    if height_m <= 0:
        raise ValueError(f"height_m must be positive, got {height_m}")
    if ring.shape[0] < 4:
        raise ValueError(f"ring must be a closed polygon with >= 4 points, got {ring.shape[0]}")

    polygon = Polygon(ring, holes)
    if not polygon.is_valid or polygon.area <= 0:
        raise ValueError("ring does not describe a valid polygon")

    # Ensure exterior counter-clockwise winding
    signed_area = float(0.5 * np.sum(ring[:-1, 0] * ring[1:, 1] - ring[1:, 0] * ring[:-1, 1]))
    if signed_area < 0:
        ring = ring[::-1].copy()

    centre_x = _centre(ring[:, 0])
    centre_y = _centre(ring[:, 1])
    offset = np.array([centre_x, centre_y], dtype=np.float64)

    local_ring = ring - offset
    local_holes = [h - offset for h in holes]

    angle = _principal_angle(local_ring)
    cos_a, sin_a = math.cos(-angle), math.sin(-angle)
    rotation = np.array([[cos_a, -sin_a], [sin_a, cos_a]], dtype=np.float64)
    aligned_ring = local_ring @ rotation.T
    aligned_holes = [h @ rotation.T for h in local_holes]

    builder = MeshBuilder()
    _build_extruded_body(
        builder,
        aligned_ring,
        aligned_holes,
        height_m=height_m,
        roof=roof,
        roof_height_m=roof_height_m,
        min_height_m=min_height_m,
        skirt_depth_m=skirt_depth_m,
        wall_material=wall_material,
        roof_material=roof_material,
    )

    mesh = builder.build()

    # Rotate positions and normals back into local unrotated coordinates
    back = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    rotated_positions = []
    for x, y, z in mesh.positions:
        wx, wy = back @ np.array([x, y], dtype=np.float64)
        rotated_positions.append((round(float(wx), 4), round(float(wy), 4), z))
    mesh.positions = rotated_positions
    mesh._position_index = {p: i for i, p in enumerate(rotated_positions)}

    rotated_normals = []
    for nx, ny, nz in mesh.normals:
        wnx, wny = back @ np.array([nx, ny], dtype=np.float64)
        rotated_normals.append((round(float(wnx), 5), round(float(wny), 5), nz))
    mesh.normals = rotated_normals
    mesh._normal_index = {n: i for i, n in enumerate(rotated_normals)}

    return mesh


def _build_extruded_body(
    builder: MeshBuilder,
    ring: NDArray[np.float64],
    holes: list[NDArray[np.float64]],
    *,
    height_m: float,
    roof: str,
    roof_height_m: float | None,
    min_height_m: float,
    skirt_depth_m: float,
    wall_material: str,
    roof_material: str,
) -> None:
    """Build walls, roof and ground cap in the ridge-aligned local frame."""
    roof = normalize_roof_shape(roof)
    n_ring = ring.shape[0]
    base_z = min_height_m - skirt_depth_m
    top_z = min_height_m + height_m

    polygon = Polygon(ring, holes)
    minx, miny, maxx, maxy = polygon.bounds

    if roof == "flat" or (roof_height_m is not None and roof_height_m <= 0):
        # Walls
        builder.add_wall_strip(
            ring, [base_z] * n_ring, [top_z] * n_ring, material=wall_material, is_inner=False
        )
        for h in holes:
            nh = h.shape[0]
            builder.add_wall_strip(
                h, [base_z] * nh, [top_z] * nh, material=wall_material, is_inner=True
            )
        # Flat roof cap & ground cap
        builder.add_roof_triangles(polygon, lambda _x, _y: top_z, material=roof_material, up=True)
        builder.add_roof_triangles(polygon, lambda _x, _y: base_z, material=wall_material, up=False)
        return

    roof_height = (
        roof_height_for(ring, roof) if roof_height_m is None else max(0.0, roof_height_m)
    )
    if roof_height <= 0:
        builder.add_wall_strip(
            ring, [base_z] * n_ring, [top_z] * n_ring, material=wall_material, is_inner=False
        )
        for h in holes:
            nh = h.shape[0]
            builder.add_wall_strip(
                h, [base_z] * nh, [top_z] * nh, material=wall_material, is_inner=True
            )
        builder.add_roof_triangles(polygon, lambda _x, _y: top_z, material=roof_material, up=True)
        builder.add_roof_triangles(polygon, lambda _x, _y: base_z, material=wall_material, up=False)
        return

    xs = ring[:, 0]
    ys = ring[:, 1]
    centre_x = _centre(xs)
    centre_y = _centre(ys)
    half_x = max(1e-6, float(np.abs(xs - centre_x).max()))
    half_y = max(1e-6, float(np.abs(ys - centre_y).max()))

    if roof == "hip":
        builder.add_wall_strip(
            ring, [base_z] * n_ring, [top_z] * n_ring, material=wall_material, is_inner=False
        )
        for h in holes:
            nh = h.shape[0]
            builder.add_wall_strip(
                h, [base_z] * nh, [top_z] * nh, material=wall_material, is_inner=True
            )
        builder.add_roof_triangles(polygon, lambda _x, _y: base_z, material=wall_material, up=False)

        ridge_half = max(0.0, half_x - half_y)
        candidates = (
            [(centre_x - ridge_half, centre_y), (centre_x, centre_y), (centre_x + ridge_half, centre_y)]
            if ridge_half > 1e-3
            else [(centre_x, centre_y)]
        )
        ridge_pts = [
            pt
            for pt in candidates
            if polygon.contains(Point(pt)) and polygon.exterior.distance(Point(pt)) > 1e-3
        ]
        if not ridge_pts:
            rep = polygon.representative_point()
            ridge_pts = [(float(rep.x), float(rep.y))]

        max_dist = max(1e-6, max(polygon.exterior.distance(Point(pt)) for pt in ridge_pts))

        cut_ys = sorted({miny - 1.0, *(float(pt[1]) for pt in ridge_pts), maxy + 1.0})
        cut_xs = sorted({minx - 1.0, *(float(pt[0]) for pt in ridge_pts), maxx + 1.0})
        cells: list[Polygon] = []
        for ix in range(len(cut_xs) - 1):
            for iy in range(len(cut_ys) - 1):
                clipped = polygon.intersection(box(cut_xs[ix], cut_ys[iy], cut_xs[ix + 1], cut_ys[iy + 1]))
                cells.extend(_iter_polygons(clipped))

        def lift_hip(x: float, y: float) -> float:
            d = polygon.exterior.distance(Point(x, y))
            if d < 1e-6:
                return top_z
            return top_z + roof_height * min(1.0, d / max_dist)

        for cell in cells:
            builder.add_roof_triangles(cell, lift_hip, material=roof_material, up=True)
        return

    if roof == "skillion":
        span_y = max(1e-6, maxy - miny)

        def lift_skillion(_x: float, y: float) -> float:
            t = (y - miny) / span_y
            return top_z + roof_height * min(1.0, max(0.0, t))

        wall_tops = [lift_skillion(ring[i, 0], ring[i, 1]) for i in range(n_ring)]
        builder.add_wall_strip(
            ring, [base_z] * n_ring, wall_tops, material=wall_material, is_inner=False
        )
        for h in holes:
            nh = h.shape[0]
            h_tops = [lift_skillion(h[i, 0], h[i, 1]) for i in range(nh)]
            builder.add_wall_strip(
                h, [base_z] * nh, h_tops, material=wall_material, is_inner=True
            )
        builder.add_roof_triangles(polygon, lambda _x, _y: base_z, material=wall_material, up=False)
        builder.add_roof_triangles(polygon, lift_skillion, material=roof_material, up=True)
        return

    # Gable roof
    def lift_gable(x: float, y: float) -> float:
        _ = x
        t = abs(y - centre_y) / half_y
        return top_z + roof_height * (1.0 - min(1.0, t))

    split_ring = _split_ring_at_y(ring, centre_y)
    n_split = split_ring.shape[0]
    wall_tops = [lift_gable(split_ring[i, 0], split_ring[i, 1]) for i in range(n_split)]
    builder.add_wall_strip(
        split_ring, [base_z] * n_split, wall_tops, material=wall_material, is_inner=False
    )
    for h in holes:
        nh = h.shape[0]
        h_split = _split_ring_at_y(h, centre_y)
        nh_split = h_split.shape[0]
        h_tops = [lift_gable(h_split[i, 0], h_split[i, 1]) for i in range(nh_split)]
        builder.add_wall_strip(h_split, [base_z] * nh_split, h_tops, material=wall_material, is_inner=True)

    builder.add_roof_triangles(polygon, lambda _x, _y: base_z, material=wall_material, up=False)

    north = polygon.intersection(box(minx - 1.0, centre_y, maxx + 1.0, maxy + 1.0))
    south = polygon.intersection(box(minx - 1.0, miny - 1.0, maxx + 1.0, centre_y))
    for half in (north, south):
        for p in _iter_polygons(half):
            builder.add_roof_triangles(p, lift_gable, material=roof_material, up=True)


def add_prism_collider(
    builder: MeshBuilder,
    ring: NDArray[np.float64],
    holes: Sequence[NDArray[np.float64]] = (),
    *,
    base_z: float,
    top_z: float,
    simplify_m: float = 1.5,
) -> None:
    """Append a simplified flat-top prism to ``builder`` for ``Colmesh-1`` physics."""
    col_ring = ring
    if ring.shape[0] > 7 and not holes:
        poly = Polygon(ring).simplify(simplify_m, preserve_topology=True)
        if isinstance(poly, Polygon) and poly.is_valid and poly.area > 1.0:
            coords = np.asarray(poly.exterior.coords, dtype=np.float64)
            if coords.shape[0] >= 4:
                col_ring = coords
    signed_area = float(0.5 * np.sum(col_ring[:-1, 0] * col_ring[1:, 1] - col_ring[1:, 0] * col_ring[:-1, 1]))
    if signed_area < 0:
        col_ring = col_ring[::-1].copy()

    n_ring = col_ring.shape[0]
    builder.add_wall_strip(
        col_ring, [base_z] * n_ring, [top_z] * n_ring, material="colmesh", is_inner=False
    )
    for h in holes:
        nh = h.shape[0]
        h_area = float(0.5 * np.sum(h[:-1, 0] * h[1:, 1] - h[1:, 0] * h[:-1, 1]))
        h_ccw = h[::-1].copy() if h_area < 0 else h
        builder.add_wall_strip(
            h_ccw, [base_z] * nh, [top_z] * nh, material="colmesh", is_inner=True
        )
    poly_col = Polygon(col_ring, holes)
    builder.add_roof_triangles(poly_col, lambda _x, _y: top_z, material="colmesh", up=True)


# ---------------------------------------------------------------------------
# COLLADA 1.4.1 Export
# ---------------------------------------------------------------------------


def _format_floats(values: list[float]) -> str:
    """Format a float array compactly and deterministically."""
    return " ".join(f"{v:.5f}".rstrip("0").rstrip(".") or "0" for v in values)


def mesh_to_dae_xml(
    mesh: Mesh,
    *,
    name: str,
    material_name: str | None = None,
    materials: Sequence[str] | None = None,
    colour: tuple[float, float, float] = (0.7, 0.7, 0.7),
    collision_mesh: Mesh | None = None,
    nulldetail_px: int | None = None,
) -> str:
    """Serialise a mesh as a BeamNG-compatible COLLADA 1.4.1 document.

    Supports multiple materials, texture coordinates (map1), optional simplified
    ``Colmesh-1`` physics geometry, and Torque3D ``nulldetail`` LOD culling.
    """
    if materials is None:
        mat_list = [material_name] if material_name else mesh.unique_materials()
    else:
        mat_list = list(materials)

    positions = _format_floats([c for p in mesh.positions for c in p])
    normals = _format_floats([c for n in mesh.normals for c in n])

    has_uvs = bool(mesh.uvs)
    if not has_uvs:
        # Default single UV coordinate if none provided
        uvs_str = "0 0"
        uv_count = 1
    else:
        uvs_str = _format_floats([c for uv in mesh.uvs for c in uv])
        uv_count = len(mesh.uvs)

    r, g, b = colour

    # Build effects and materials
    effects_xml = []
    materials_xml = []
    for mat in mat_list:
        effects_xml.append(
            f"""    <effect id="{escape(mat)}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <emission>
              <color sid="emission">0 0 0 1</color>
            </emission>
            <diffuse>
              <color sid="diffuse">{r:.4f} {g:.4f} {b:.4f} 1</color>
            </diffuse>
            <index_of_refraction>
              <float sid="ior">1</float>
            </index_of_refraction>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>"""
        )
        materials_xml.append(
            f"""    <material id="{escape(mat)}" name="{escape(mat)}">
      <instance_effect url="#{escape(mat)}-effect"/>
    </material>"""
        )

    # Build triangles elements grouped by material
    triangles_by_mat: dict[str, list[int]] = {}
    default_mat = mat_list[0]
    for idx, _tri in enumerate(mesh.triangles):
        m = mesh.triangle_materials[idx] if idx < len(mesh.triangle_materials) else default_mat
        if m not in mat_list:
            m = default_mat
        triangles_by_mat.setdefault(m, []).append(idx)

    triangles_xml = []
    for mat in mat_list:
        indices = triangles_by_mat.get(mat, [])
        if not indices:
            continue
        p_entries = []
        for tri_idx in indices:
            tri = mesh.triangles[tri_idx]
            uv_indices = (
                mesh.triangle_uvs[tri_idx]
                if tri_idx < len(mesh.triangle_uvs)
                else (0, 0, 0)
            )
            for corner_idx in range(3):
                vi, ni = tri[corner_idx]
                uvi = uv_indices[corner_idx] if has_uvs else 0
                p_entries.append(f"{vi} {ni} {uvi}")
        p_text = " ".join(p_entries)
        triangles_xml.append(
            f"""        <triangles material="{escape(mat)}" count="{len(indices)}">
          <input semantic="VERTEX" source="#{escape(name)}-mesh-vertices" offset="0"/>
          <input semantic="NORMAL" source="#{escape(name)}-mesh-normals" offset="1"/>
          <input semantic="TEXCOORD" source="#{escape(name)}-mesh-map1" offset="2" set="0"/>
          <p>{p_text}</p>
        </triangles>"""
        )

    instance_materials_xml = "\n".join(
        f'              <instance_material symbol="{escape(mat)}" target="#{escape(mat)}"/>'
        for mat in mat_list
    )

    effects_str = "\n".join(effects_xml)
    materials_str = "\n".join(materials_xml)
    triangles_str = "\n".join(triangles_xml)

    col_geom_xml = ""
    col_node_xml = ""
    if collision_mesh is not None and collision_mesh.triangles and collision_mesh.positions:
        c_pos = _format_floats([c for p in collision_mesh.positions for c in p])
        c_norm = _format_floats([c for n in collision_mesh.normals for c in n])
        c_p = " ".join(
            f"{tri[corner_i][0]} {tri[corner_i][1]}"
            for tri in collision_mesh.triangles
            for corner_i in range(3)
        )
        col_geom_xml = f"""
    <geometry id="{escape(name)}-colmesh" name="{escape(name)}-colmesh">
      <mesh>
        <source id="{escape(name)}-colmesh-positions">
          <float_array id="{escape(name)}-colmesh-positions-array" count="{len(collision_mesh.positions) * 3}">{c_pos}</float_array>
          <technique_common>
            <accessor source="#{escape(name)}-colmesh-positions-array" count="{len(collision_mesh.positions)}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{escape(name)}-colmesh-normals">
          <float_array id="{escape(name)}-colmesh-normals-array" count="{len(collision_mesh.normals) * 3}">{c_norm}</float_array>
          <technique_common>
            <accessor source="#{escape(name)}-colmesh-normals-array" count="{len(collision_mesh.normals)}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{escape(name)}-colmesh-vertices">
          <input semantic="POSITION" source="#{escape(name)}-colmesh-positions"/>
        </vertices>
        <triangles count="{len(collision_mesh.triangles)}">
          <input semantic="VERTEX" source="#{escape(name)}-colmesh-vertices" offset="0"/>
          <input semantic="NORMAL" source="#{escape(name)}-colmesh-normals" offset="1"/>
          <p>{c_p}</p>
        </triangles>
      </mesh>
    </geometry>"""
        col_node_xml = f"""
          <node id="Colmesh-1" name="Colmesh-1" type="NODE">
            <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
            <instance_geometry url="#{escape(name)}-colmesh" name="Colmesh-1"/>
          </node>"""

    if col_node_xml or nulldetail_px is not None:
        null_xml = (
            f"""
        <node id="nulldetail{nulldetail_px}" name="nulldetail{nulldetail_px}" type="NODE">
          <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        </node>"""
            if nulldetail_px is not None
            else ""
        )
        vis_node_name = f"{escape(name)}_a12"
        visual_scene_xml = f"""      <node id="base00" name="base00" type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <node id="start01" name="start01" type="NODE">
          <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
          <node id="{vis_node_name}" name="{vis_node_name}" type="NODE">
            <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
            <instance_geometry url="#{escape(name)}-mesh" name="{vis_node_name}">
              <bind_material>
                <technique_common>
{instance_materials_xml}
                </technique_common>
              </bind_material>
            </instance_geometry>
          </node>{col_node_xml}
        </node>{null_xml}
      </node>"""
    else:
        visual_scene_xml = f"""      <node id="{escape(name)}" name="{escape(name)}" type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <instance_geometry url="#{escape(name)}-mesh" name="{escape(name)}">
          <bind_material>
            <technique_common>
{instance_materials_xml}
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>"""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <asset>
    <contributor>
      <author>ivanovo2beam</author>
      <authoring_tool>ivanovo2beam building generator</authoring_tool>
    </contributor>
    <created>{_FIXED_TIMESTAMP}</created>
    <modified>{_FIXED_TIMESTAMP}</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
{effects_str}
  </library_effects>
  <library_images/>
  <library_materials>
{materials_str}
  </library_materials>
  <library_geometries>
    <geometry id="{escape(name)}-mesh" name="{escape(name)}">
      <mesh>
        <source id="{escape(name)}-mesh-positions">
          <float_array id="{escape(name)}-mesh-positions-array" count="{len(mesh.positions) * 3}">{positions}</float_array>
          <technique_common>
            <accessor source="#{escape(name)}-mesh-positions-array" count="{len(mesh.positions)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{escape(name)}-mesh-normals">
          <float_array id="{escape(name)}-mesh-normals-array" count="{len(mesh.normals) * 3}">{normals}</float_array>
          <technique_common>
            <accessor source="#{escape(name)}-mesh-normals-array" count="{len(mesh.normals)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{escape(name)}-mesh-map1">
          <float_array id="{escape(name)}-mesh-map1-array" count="{uv_count * 2}">{uvs_str}</float_array>
          <technique_common>
            <accessor source="#{escape(name)}-mesh-map1-array" count="{uv_count}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{escape(name)}-mesh-vertices">
          <input semantic="POSITION" source="#{escape(name)}-mesh-positions"/>
        </vertices>
{triangles_str}
      </mesh>
    </geometry>{col_geom_xml}
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
{visual_scene_xml}
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#Scene"/>
  </scene>
</COLLADA>
"""


def write_dae(
    path: Path,
    mesh: Mesh,
    *,
    name: str,
    material_name: str | None = None,
    materials: Sequence[str] | None = None,
    colour: tuple[float, float, float] = (0.7, 0.7, 0.7),
    collision_mesh: Mesh | None = None,
    nulldetail_px: int | None = None,
) -> int:
    """Write a mesh as a COLLADA file."""
    if not mesh.triangles:
        raise ValueError(f"refusing to write an empty mesh for {name!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = mesh_to_dae_xml(
        mesh,
        name=name,
        material_name=material_name,
        materials=materials,
        colour=colour,
        collision_mesh=collision_mesh,
        nulldetail_px=nulldetail_px,
    )
    path.write_text(xml, encoding="utf-8")
    return len(xml)
