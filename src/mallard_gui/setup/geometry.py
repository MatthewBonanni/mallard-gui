"""Geometry-only previews of regions and probes, straight from their tables in the input: drawn at once over
the case view's surfaces, before `mallard check` places them on the mesh (the design's region previews:
"analytic regions ... are drawn as their geometry over the surfaces at once").

Only geometry is computed here, never cells, fractions or measures: those come from `mallard check`. A table
this module cannot draw (an expression, a region defined by the solution) gets a note instead, and the check's
own block draws it.

TODO(#1256): the keys of each region and probe type are those of Mallard's geometry guide; the catalog's
variants of tables will name them.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vtkmodules.vtkCommonComputationalGeometry import vtkParametricTorus
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkCommonTransforms import vtkTransform
from vtkmodules.vtkFiltersCore import vtkAppendPolyData
from vtkmodules.vtkFiltersGeneral import vtkTransformFilter
from vtkmodules.vtkFiltersSources import vtkCubeSource, vtkParametricFunctionSource, vtkSphereSource

Vec = tuple[float, float, float]

LENGTH_UNITS = {"m": 1.0, "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "km": 1e3, "in": 0.0254, "ft": 0.3048}
_QUANTITY = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z]+)?\s*$")
SEGMENTS = 96  # of a full circle
PROBE_TYPES = ("point", "points", "line", "polyline", "circle", "arc", "curve", "cut_plane", "surface")
BOOLEANS = ("union", "intersection", "difference")
TRANSFORM_KEYS = ("scale", "rotate", "pivot", "translate", "repeat")
MAX_DEPTH = 16


def length(value: Any) -> float | None:
    """A length in meters: a number, or a quantity with a length unit (`"2 mm"`); None for an expression."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _QUANTITY.match(value)
        if m and (m[2] is None or m[2] in LENGTH_UNITS):
            return float(m[1]) * LENGTH_UNITS.get(m[2] or "m", 1.0)
    return None


def number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def vector(value: Any, dim: int = 3) -> Vec | None:
    """A point or direction of 2 or 3 lengths, as 3 components (z = 0 in 2D)."""
    if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 3:
        return None
    out = [length(v) for v in value]
    if any(v is None for v in out):
        return None
    xs = [float(v) for v in out if v is not None]
    while len(xs) < 3:
        xs.append(0.0)
    return xs[0], xs[1], xs[2]


def _sub(a: Vec, b: Vec) -> Vec:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _add(a: Vec, b: Vec) -> Vec:
    return a[0] + b[0], a[1] + b[1], a[2] + b[2]


def _mul(a: Vec, s: float) -> Vec:
    return a[0] * s, a[1] * s, a[2] * s


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]


def _norm(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def unit(a: Vec) -> Vec:
    n = _norm(a)
    return _mul(a, 1.0 / n) if n > 0 else (0.0, 0.0, 1.0)


def plane_axes(normal: Vec, reference: Vec | None = None) -> tuple[Vec, Vec]:
    """The in-plane axes of a flat shape: `reference` projected (default: the coordinate axis least aligned
    with the normal, as Mallard's geometry guide says), and normal x that."""
    n = unit(normal)
    if reference is None:
        k = min(range(3), key=lambda i: abs(n[i]))
        reference = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))[k]
    r = _sub(reference, _mul(n, _dot(reference, n)))
    e1 = unit(r)
    return e1, _cross(n, e1)


@dataclass
class Handle:
    """A point of a shape that can be dragged in the view, and the key it sets.

    role: `point` sets the key to the position; `move` too, and shifts the table's `bounds` with it (a bounded
    cut plane's piece moves whole); `radius` to the distance from `anchor`; `size` to twice the distance from
    `anchor` per component; `vertex` sets item `index` of the key's list of points."""

    key: str
    role: str
    position: Vec
    anchor: Vec | None = None
    index: int | None = None


@dataclass
class Preview:
    """A region or probe drawn from its table: its dimension (0 points, 1 curve, 2 surface, 3 solid), the
    polydata, its samples (probes), the handles that edit it, and a note on what it leaves to the check."""

    dimension: int
    poly: vtkPolyData
    samples: list[Vec] = field(default_factory=list)
    handles: list[Handle] = field(default_factory=list)
    note: str = ""
    mesh_region: str = ""  # a region of the mesh (a zone, `domain`): drawn by its own block


def _polydata(
    points: Sequence[Vec],
    lines: Sequence[Sequence[int]] = (),
    polys: Sequence[Sequence[int]] = (),
    verts: Sequence[int] = (),
) -> vtkPolyData:
    pts = vtkPoints()
    for p in points:
        pts.InsertNextPoint(*p)
    poly = vtkPolyData()
    poly.SetPoints(pts)
    for attr, cells in (("SetLines", lines), ("SetPolys", polys)):
        if cells:
            arr = vtkCellArray()
            for c in cells:
                arr.InsertNextCell(len(c))
                for i in c:
                    arr.InsertCellPoint(i)
            getattr(poly, attr)(arr)
    if verts:
        arr = vtkCellArray()
        for i in verts:
            arr.InsertNextCell(1)
            arr.InsertCellPoint(i)
        poly.SetVerts(arr)
    return poly


def _ring(
    center: Vec, e1: Vec, e2: Vec, a: float, b: float, start: float = 0.0, end: float = 360.0, n: int = SEGMENTS
) -> list[Vec]:
    closed = abs(end - start) >= 360.0
    count = n if closed else max(2, int(n * abs(end - start) / 360.0) + 1)
    out = []
    for k in range(count):
        t = math.radians(start + (end - start) * k / (count if closed else count - 1))
        out.append(_add(center, _add(_mul(e1, a * math.cos(t)), _mul(e2, b * math.sin(t)))))
    return out


def _closed_curve(points: list[Vec], filled: bool) -> vtkPolyData:
    idx = list(range(len(points)))
    return _polydata(points, lines=[[*idx, 0]], polys=[idx] if filled else ())


def _open_curve(points: list[Vec]) -> vtkPolyData:
    return _polydata(points, lines=[list(range(len(points)))])


def _revolved(base: Vec, axis: Vec, profile: Sequence[tuple[float, float]], caps: bool) -> vtkPolyData:
    """A surface of revolution about `axis` through `base`: rings of radius r at distance s along the axis."""
    a = unit(axis)
    e1, e2 = plane_axes(a)
    pts: list[Vec] = []
    for s, r in profile:
        pts += _ring(_add(base, _mul(a, s)), e1, e2, r, r)
    n = SEGMENTS
    polys: list[list[int]] = []
    for i in range(len(profile) - 1):
        for k in range(n):
            k2 = (k + 1) % n
            polys.append([i * n + k, i * n + k2, (i + 1) * n + k2, (i + 1) * n + k])
    if caps:
        polys.append(list(range(n))[::-1])
        last = (len(profile) - 1) * n
        polys.append(list(range(last, last + n)))
    return _polydata(pts, polys=polys)


def _source(src: Any) -> vtkPolyData:
    src.Update()
    out = vtkPolyData()
    out.DeepCopy(src.GetOutput())
    return out


def _transformed(poly: vtkPolyData, t: vtkTransform) -> vtkPolyData:
    f = vtkTransformFilter()
    f.SetInputData(poly)
    f.SetTransform(t)
    f.Update()
    out = vtkPolyData()
    out.DeepCopy(f.GetOutput())
    return out


def _append(polys: Sequence[vtkPolyData]) -> vtkPolyData:
    app = vtkAppendPolyData()
    for p in polys:
        app.AddInputData(p)
    if not polys:
        return vtkPolyData()
    return _source(app)


def transform_of(table: Mapping[str, Any], dim: int) -> vtkTransform | None:
    """The table's `scale`, `rotate` (about `pivot`) and `translate`, applied in that order; None without them."""
    if not any(k in table for k in ("scale", "rotate", "translate")):
        return None
    t = vtkTransform()
    t.PostMultiply()
    pivot = vector(table.get("pivot"), dim) or (0.0, 0.0, 0.0)
    t.Translate(*_mul(pivot, -1.0))
    scale = table.get("scale")
    if scale is not None:
        s = number(scale)
        if s is not None:
            t.Scale(s, s, s if dim == 3 else 1.0)
        elif isinstance(scale, list) and all(number(v) is not None for v in scale):
            f = [float(v) for v in scale] + [1.0] * (3 - len(scale))
            t.Scale(*f[:3])
    rotate = table.get("rotate")
    if number(rotate) is not None:
        t.RotateZ(float(number(rotate) or 0.0))
    elif isinstance(rotate, Mapping):
        if "axes" in rotate and isinstance(rotate["axes"], list) and len(rotate["axes"]) == 3:
            axes = [vector(a) for a in rotate["axes"]]
            if all(a is not None for a in axes):
                from vtkmodules.vtkCommonMath import vtkMatrix4x4

                m = vtkMatrix4x4()
                for j, a in enumerate(axes):
                    assert a is not None
                    for i in range(3):
                        m.SetElement(i, j, a[i])
                t.Concatenate(m)
        else:
            axis = vector(rotate.get("axis"))
            angle = number(rotate.get("angle"))
            if axis is not None and angle is not None:
                t.RotateWXYZ(angle, *axis)
    t.Translate(*pivot)
    translate = vector(table.get("translate"), dim)
    if translate is not None:
        t.Translate(*translate)
    return t


class _Unsupported(Exception):
    pass


def _need(table: Mapping[str, Any], key: str, kind: str = "length", dim: int = 3) -> Any:
    value = table.get(key)
    out: Any = vector(value, dim) if kind == "vector" else length(value) if kind == "length" else number(value)
    if out is None:
        what = "missing" if value is None else "an expression or not a number"
        raise _Unsupported(f"{key}: {what}")
    return out


def preview(
    table: Any,
    dim: int,
    named: Mapping[str, Any] | None = None,
    bounds: Sequence[float] | None = None,
    root: Path | None = None,
    _depth: int = 0,
) -> Preview:
    """The preview of a region or probe table (or a region's name), in a case of dimension `dim`.

    `named` is the input's `[regions]`; `bounds` (xmin, xmax, ymin, ...) sizes unbounded shapes (half-spaces,
    cut planes, infinite cylinders); `root` is the case directory, for CAD files."""
    named = named or {}
    bounds = tuple(bounds) if bounds else (-1.0, 1.0, -1.0, 1.0, -1.0 if dim == 3 else 0.0, 1.0 if dim == 3 else 0.0)
    empty = vtkPolyData()
    if _depth > MAX_DEPTH:
        return Preview(dim, empty, note="regions name each other too deeply")
    if isinstance(table, str):
        if table in named:
            return preview(named[table], dim, named, bounds, root, _depth + 1)
        return Preview(dim, empty, mesh_region=table, note="a region of the mesh: drawn by its zone")
    if not isinstance(table, Mapping):
        return Preview(dim, empty, note="not a table")
    try:
        p = _preview(table, dim, named, bounds, root, _depth)
    except _Unsupported as e:
        return Preview(dim, empty, note=f"drawn by mallard check ({e})")
    t = transform_of(table, dim)
    if t is not None:
        p.poly = _transformed(p.poly, t)
        p.samples = [t.TransformPoint(*s) for s in p.samples]
        p.handles = []
        p.note = p.note or "transformed: edit its keys (no handles)"
    if table.get("filled") is False and p.dimension == dim:
        p.dimension = dim - 1
    return p


def _preview(
    table: Mapping[str, Any],
    dim: int,
    named: Mapping[str, Any],
    bounds: Sequence[float],
    root: Path | None,
    depth: int,
) -> Preview:
    kind = str(table.get("type", ""))
    if not kind:
        kind = "point" if "point" in table else "line" if "start" in table and "end" in table else ""
    filled = table.get("filled", True) is not False
    size = math.dist(bounds[0::2], bounds[1::2]) or 1.0

    if kind in BOOLEANS:
        parts = list(table.get("regions") or [])
        if kind == "difference" and not parts:
            parts = [table.get("a"), table.get("b")]
        previews = [preview(p, dim, named, bounds, root, depth + 1) for p in parts if p is not None]
        drawn = [p for p in previews if p.poly.GetNumberOfPoints()]
        notes = [p.note for p in previews if p.note]
        return Preview(
            max((p.dimension for p in previews), default=dim),
            _append([p.poly for p in drawn]),
            note=f"{kind}: its parts drawn" + (f"; {notes[0]}" if notes else ""),
        )
    if kind in ("boundary", "interior"):
        inner = preview(table.get("of"), dim, named, bounds, root, depth + 1)
        inner.dimension += -1 if kind == "boundary" else 1
        inner.handles = []
        return inner

    if kind in ("sphere", "disk") and (dim == 2 or kind == "sphere"):
        c, r = _need(table, "center", "vector", dim), _need(table, "radius")
        handles = [Handle("center", "point", c), Handle("radius", "radius", _add(c, (r, 0.0, 0.0)), anchor=c)]
        if dim == 2:
            return Preview(
                2 if filled else 1, _closed_curve(_ring(c, (1, 0, 0), (0, 1, 0), r, r), filled), handles=handles
            )
        s = vtkSphereSource()
        s.SetCenter(*c)
        s.SetRadius(r)
        s.SetThetaResolution(48)
        s.SetPhiResolution(24)
        return Preview(3 if filled else 2, _source(s), handles=handles)
    if kind == "ellipsoid":
        c, a = _need(table, "center", "vector", dim), _need(table, "semi_axes", "vector", dim)
        handles = [Handle("center", "point", c), Handle("semi_axes", "size", _add(c, a), anchor=c)]
        if dim == 2:
            return Preview(2, _closed_curve(_ring(c, (1, 0, 0), (0, 1, 0), a[0], a[1]), filled), handles=handles)
        s = vtkSphereSource()
        s.SetThetaResolution(48)
        s.SetPhiResolution(24)
        t = vtkTransform()
        t.Translate(*c)
        t.Scale(*a)
        return Preview(3, _transformed(_source(s), t), handles=handles)
    if kind in ("rectangle", "box") and (dim == 2 or kind == "box"):
        c, sz = _need(table, "center", "vector", dim), _need(table, "size", "vector", dim)
        handles = [Handle("center", "point", c), Handle("size", "size", _add(c, _mul(sz, 0.5)), anchor=c)]
        if dim == 2:
            hx, hy = sz[0] / 2, sz[1] / 2
            pts = [
                (c[0] - hx, c[1] - hy, 0.0),
                (c[0] + hx, c[1] - hy, 0.0),
                (c[0] + hx, c[1] + hy, 0.0),
                (c[0] - hx, c[1] + hy, 0.0),
            ]
            return Preview(2, _closed_curve(pts, filled), handles=handles)
        cube = vtkCubeSource()
        cube.SetCenter(*c)
        cube.SetXLength(sz[0])
        cube.SetYLength(sz[1])
        cube.SetZLength(sz[2])
        return Preview(3, _source(cube), handles=handles)
    if kind == "rectangle":  # 3D: a flat surface
        c, sz, n = (
            _need(table, "center", "vector", dim),
            _need(table, "size", "vector", 2),
            _need(table, "normal", "vector"),
        )
        e1, e2 = plane_axes(n, vector(table.get("reference")))
        h1, h2 = _mul(e1, sz[0] / 2), _mul(e2, sz[1] / 2)
        pts = [_sub(_sub(c, h1), h2), _sub(_add(c, h1), h2), _add(_add(c, h1), h2), _add(_sub(c, h1), h2)]
        return Preview(2, _closed_curve(pts, filled), handles=[Handle("center", "point", c)])
    if kind == "annulus" or (kind == "disk" and dim == 3):
        c, r = _need(table, "center", "vector", dim), _need(table, "radius")
        ri = length(table.get("inner_radius")) or 0.0
        if dim == 2:
            e1, e2 = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
        else:
            e1, e2 = plane_axes(_need(table, "normal", "vector"), vector(table.get("reference")))
        outer = _ring(c, e1, e2, r, r)
        handles = [Handle("center", "point", c), Handle("radius", "radius", _add(c, _mul(e1, r)), anchor=c)]
        if ri <= 0:
            return Preview(dim if dim == 2 else 2, _closed_curve(outer, filled), handles=handles)
        hole = _ring(c, e1, e2, ri, ri)
        n = len(outer)
        polys = [[k, (k + 1) % n, n + (k + 1) % n, n + k] for k in range(n)] if filled else []
        lines = [[*range(n), 0], [*range(n, 2 * n), n]]
        handles.append(Handle("inner_radius", "radius", _add(c, _mul(e1, ri)), anchor=c))
        return Preview(dim if dim == 2 else 2, _polydata(outer + hole, lines=lines, polys=polys), handles=handles)
    if kind in ("circle", "ellipse") or (kind == "arc" and "angles" in table):
        c = _need(table, "center", "vector", dim)
        if kind == "ellipse":
            a = _need(table, "semi_axes", "vector", 2)
            ra, rb = a[0], a[1]
        else:
            ra = rb = _need(table, "radius")
        if dim == 2:
            e1, e2 = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
        else:
            e1, e2 = plane_axes(_need(table, "normal", "vector"), vector(table.get("reference")))
        handles = [Handle("center", "point", c)]
        if kind != "ellipse":
            handles.append(Handle("radius", "radius", _add(c, _mul(e1, ra)), anchor=c))
        if kind == "arc":
            angles = table.get("angles")
            if not (isinstance(angles, list) and len(angles) == 2 and all(number(v) is not None for v in angles)):
                raise _Unsupported("angles: expected [start, end] in degrees")
            a0, a1 = float(angles[0]), float(angles[1])
            pts = _ring(c, e1, e2, ra, rb, a0, a1)
            n = _n_points(table, 2)
            samples = (
                [
                    _add(c, _add(_mul(e1, ra * math.cos(t)), _mul(e2, rb * math.sin(t))))
                    for t in (math.radians(a0 + (a1 - a0) * k / (n - 1)) for k in range(n))
                ]
                if n >= 2
                else []
            )
            return Preview(1, _open_curve(pts), samples=samples, handles=handles)
        pts = _ring(c, e1, e2, ra, rb)
        n = _n_points(table, 0)
        samples = _ring(c, e1, e2, ra, rb, n=n) if n >= 3 else []
        return Preview(1, _closed_curve(pts, False), samples=samples, handles=handles)
    if kind == "cylinder":
        c, r = _need(table, "center", "vector", dim), _need(table, "radius")
        axis = vector(table.get("axis"), dim) or (0.0, 0.0, 1.0)
        a = unit(axis)
        lval = table.get("length")
        half = length(lval) if lval is not None else None
        if lval is not None and half is None:
            raise _Unsupported("length: an expression")
        half = half / 2 if half is not None else size
        ri = length(table.get("inner_radius")) or 0.0
        e1, _ = plane_axes(a)
        handles = [Handle("center", "point", c), Handle("radius", "radius", _add(c, _mul(e1, r)), anchor=c)]
        if dim == 2:
            raise _Unsupported("cylinder: a 3D region")
        surf = [_revolved(c, a, [(-half, r), (half, r)], caps=filled)]
        if ri > 0:
            surf.append(_revolved(c, a, [(-half, ri), (half, ri)], caps=False))
        return Preview(
            3 if filled else 2,
            _append(surf),
            handles=handles,
            note="" if lval is not None else "infinite: drawn across the domain",
        )
    if kind in ("cone", "frustum"):
        if dim == 2:
            raise _Unsupported("cone: a 3D region")
        base, ln = _need(table, "base", "vector", dim), _need(table, "length")
        r0, r1 = _need(table, "base_radius"), length(table.get("top_radius")) or 0.0
        axis = vector(table.get("axis")) or (0.0, 0.0, 1.0)
        return Preview(
            3 if filled else 2,
            _revolved(base, axis, [(0.0, r0), (ln, max(r1, 1e-12))], caps=filled),
            handles=[Handle("base", "point", base)],
        )
    if kind == "torus":
        if dim == 2:
            raise _Unsupported("torus: a 3D region")
        c, rmaj, rmin = _need(table, "center", "vector"), _need(table, "major_radius"), _need(table, "minor_radius")
        axis = unit(vector(table.get("axis")) or (0.0, 0.0, 1.0))
        tor = vtkParametricTorus()
        tor.SetRingRadius(rmaj)
        tor.SetCrossSectionRadius(rmin)
        src = vtkParametricFunctionSource()
        src.SetParametricFunction(tor)
        src.SetUResolution(64)
        src.SetVResolution(24)
        t = vtkTransform()
        t.Translate(*c)
        z = (0.0, 0.0, 1.0)
        rot = _cross(z, axis)
        if _norm(rot) > 1e-12:
            t.RotateWXYZ(math.degrees(math.acos(max(-1.0, min(1.0, _dot(z, axis))))), *rot)
        elif axis[2] < 0:
            t.RotateX(180)
        return Preview(3, _transformed(_source(src), t), handles=[Handle("center", "point", c)])
    if kind in ("half_space", "cut_plane"):
        pt, nrm = _need(table, "point", "vector", dim), unit(_need(table, "normal", "vector", dim))
        lo, hi = bounds[0::2], bounds[1::2]
        box = table.get("bounds") if kind == "cut_plane" else None
        handles = [Handle("point", "point", pt)]
        if isinstance(box, list) and len(box) == 2:
            blo, bhi = vector(box[0], dim), vector(box[1], dim)
            if blo and bhi:
                lo, hi = blo, bhi
                handles = [Handle("point", "move", pt, anchor=pt)]
        dimension = dim - 1 if kind == "cut_plane" else dim
        if dim == 2:
            d = (-nrm[1], nrm[0], 0.0)
            pts = _clip_line(pt, d, lo, hi)
            return Preview(dimension, _open_curve(pts) if pts else vtkPolyData(), handles=handles)
        e1, e2 = plane_axes(nrm)
        h = math.dist(lo, hi) / 2 or size / 2
        center = tuple(min(max(pt[i], lo[i]), hi[i]) for i in range(3))
        q = _add(pt, _mul(nrm, _dot(_sub(center, pt), nrm)))  # the plane's point nearest the box center
        quad = [_add(q, _add(_mul(e1, sx * h), _mul(e2, sy * h))) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        return Preview(dimension, _closed_curve(quad, True), handles=handles)
    if kind == "polygon":
        vs = _points(table.get("vertices"), dim, 3)
        if vs is None:
            raise _Unsupported("vertices: expected at least three points")
        handles = [Handle("vertices", "vertex", v, index=i) for i, v in enumerate(vs)]
        return Preview(dim if dim == 2 else 2, _closed_curve(vs, filled), handles=handles)
    if kind in ("stl", "obj"):
        return _cad(table, kind, root)
    if kind == "point":
        p = _need(table, "point", "vector", dim)
        return Preview(0, _polydata([p], verts=[0]), samples=[p], handles=[Handle("point", "point", p)])
    if kind == "points":
        raw = table.get("points")
        if raw is None and "points_file" in table:
            raise _Unsupported("points_file: read by mallard check")
        vs = _points(raw, dim, 1)
        if vs is None:
            raise _Unsupported("points: expected a list of points")
        return Preview(
            0,
            _polydata(vs, verts=list(range(len(vs)))),
            samples=vs,
            handles=[Handle("points", "vertex", v, index=i) for i, v in enumerate(vs)],
        )
    if kind == "line":
        a, b = _need(table, "start", "vector", dim), _need(table, "end", "vector", dim)
        n = _n_points(table, 2)
        samples = [_add(a, _mul(_sub(b, a), k / (n - 1))) for k in range(n)] if n >= 2 else []
        return Preview(
            1, _open_curve([a, b]), samples=samples, handles=[Handle("start", "point", a), Handle("end", "point", b)]
        )
    if kind == "polyline":
        vs = _points(table.get("vertices"), dim, 2)
        if vs is None:
            raise _Unsupported("vertices: expected at least two points")
        n = _n_points(table, 2)
        samples = []
        if n >= 2:
            for i in range(len(vs) - 1):
                seg = [_add(vs[i], _mul(_sub(vs[i + 1], vs[i]), k / (n - 1))) for k in range(n)]
                samples += seg if i == 0 else seg[1:]
        return Preview(
            1,
            _open_curve(vs),
            samples=samples,
            handles=[Handle("vertices", "vertex", v, index=i) for i, v in enumerate(vs)],
        )
    if kind in ("curve", "surface", "implicit"):
        raise _Unsupported(f"{kind}: expressions")
    raise _Unsupported(f"type {kind!r}" if kind else "no type")


def _points(raw: Any, dim: int, least: int) -> list[Vec] | None:
    pts = [vector(v, dim) for v in raw] if isinstance(raw, list) else []
    out = [v for v in pts if v is not None]
    return out if len(out) == len(pts) and len(out) >= least else None


def _n_points(table: Mapping[str, Any], default: int) -> int:
    n = table.get("n_points")
    return int(n) if isinstance(n, int) and not isinstance(n, bool) else default


def _clip_line(p: Vec, d: Vec, lo: Sequence[float], hi: Sequence[float]) -> list[Vec]:
    """The segment of the line p + s d inside the box [lo, hi] (x and y)."""
    s0, s1 = -math.inf, math.inf
    for i in range(2):
        if abs(d[i]) < 1e-15:
            if not lo[i] <= p[i] <= hi[i]:
                return []
            continue
        a, b = (lo[i] - p[i]) / d[i], (hi[i] - p[i]) / d[i]
        s0, s1 = max(s0, min(a, b)), min(s1, max(a, b))
    if s0 > s1:
        return []
    return [_add(p, _mul(d, s0)), _add(p, _mul(d, s1))]


def _cad(table: Mapping[str, Any], kind: str, root: Path | None) -> Preview:
    file, units = table.get("file"), table.get("units")
    if not isinstance(file, str) or units not in LENGTH_UNITS:
        raise _Unsupported("file and units required")
    path = Path(file) if root is None else root / file
    if not path.exists():
        raise _Unsupported(f"{file}: not found")
    if kind == "stl":
        from vtkmodules.vtkIOGeometry import vtkSTLReader

        reader: Any = vtkSTLReader()
    else:
        from vtkmodules.vtkIOGeometry import vtkOBJReader

        reader = vtkOBJReader()
    reader.SetFileName(str(path))
    t = vtkTransform()
    s = LENGTH_UNITS[str(units)]
    t.Scale(s, s, s)
    return Preview(3, _transformed(_source(reader), t))


def apply_handle(table: Mapping[str, Any], handle: Handle, position: Sequence[float], dim: int) -> dict[str, Any]:
    """The keys a handle dragged to `position` sets in its table, rounded to 6 significant digits."""
    p = tuple(float(c) for c in position[:dim])
    if handle.role == "point":
        return {handle.key: [_round(c) for c in p]}
    anchor = handle.anchor or (0.0, 0.0, 0.0)
    if handle.role == "move":
        shift = [p[i] - anchor[i] for i in range(dim)]
        box = table.get("bounds")
        out: dict[str, Any] = {handle.key: [_round(c) for c in p]}
        if isinstance(box, list) and len(box) == 2:
            out["bounds"] = [[_round(float(c) + d) for c, d in zip(corner, shift, strict=False)] for corner in box]
        return out
    if handle.role == "radius":
        return {handle.key: _round(math.dist(p, anchor[:dim]))}
    if handle.role == "size":
        return {handle.key: [_round(2 * abs(p[i] - anchor[i])) for i in range(dim)]}
    if handle.role == "vertex" and handle.index is not None:
        items = list(table.get(handle.key) or [])
        if handle.index < len(items):
            items[handle.index] = [_round(c) for c in p]
        return {handle.key: items}
    raise ValueError(f"handle {handle.key}: role {handle.role}")


def _round(x: float) -> float:
    if x == 0 or not math.isfinite(x):
        return 0.0
    return float(f"{x:.6g}")
