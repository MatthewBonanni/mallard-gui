"""Surface levels of detail as spatial tiles, and their progressive refinement in the view.

The format (`mallard-surface-lod`, version 1) is a directory: `index.json` and one binary file per tile.
Each level clusters the surface's vertices on a uniform grid whose cell size makes about that level's
triangle target (vertex clustering, as ParaView's levels of detail do): a cluster's point is the mean of its
vertices, a triangle whose corners fall in fewer than three clusters is dropped. Clustering on one global
grid needs no locked borders: pieces made apart (ranks, or snapshot pieces) agree on every cluster, and
their sums merge. Level L's tiles split the bounds into 2^L per axis, so a tile has eight children one level
finer; a triangle belongs to the tile holding its centroid. A tile file is little-endian: uint32 point count
and triangle count, float32 xyz per point, uint32 corners per triangle, then float32 per point per field
(the index lists fields with their components).

The view starts from level 0 and, on each still frame, refines a tile into its children where its cluster
size covers more than `max_error_px` pixels on screen and the triangle budget allows, coarsening tiles
out of view, so zooming on a body loads its fine tiles and not the far field's.
"""

from __future__ import annotations

import json
import math
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkFiltersCore import vtkAppendPolyData, vtkCellDataToPointData, vtkTriangleFilter
from vtkmodules.vtkFiltersGeometry import vtkDataSetSurfaceFilter

SCHEMA = "mallard-surface-lod"
VERSION = 1
DEFAULT_TARGETS = (200_000, 2_000_000, 20_000_000)

Array = np.ndarray[Any, Any]


# Building


@dataclass
class Accumulator:
    """One level's clusters and triangles over the pieces added so far, keyed by grid cell."""

    origin: Array
    h: float
    dims: Array
    keys: list[Array]
    sums: list[Array]
    tris: list[Array]

    def merged(self) -> tuple[Array, Array, Array]:
        """Every cluster's key and summed row (sorted by key), and the distinct triangles in cluster keys."""
        if not self.keys:
            return np.zeros(0, np.int64), np.zeros((0, 4)), np.zeros((0, 3), np.int64)
        keys = np.concatenate(self.keys)
        uniq, inverse = np.unique(keys, return_inverse=True)
        sums = np.zeros((len(uniq), self.sums[0].shape[1]))
        np.add.at(sums, inverse, np.concatenate(self.sums))
        tris = np.unique(np.concatenate(self.tris), axis=0) if self.tris else np.zeros((0, 3), np.int64)
        self.keys, self.sums, self.tris = [uniq], [sums], [tris]
        return uniq, sums, tris


def triangulated(data: Any) -> vtkPolyData:
    """A dataset's surface as triangles with its fields as point data."""
    surface = vtkDataSetSurfaceFilter()
    surface.SetInputData(data)
    to_points = vtkCellDataToPointData()
    to_points.SetInputConnection(surface.GetOutputPort())
    tri = vtkTriangleFilter()
    tri.SetInputConnection(to_points.GetOutputPort())
    tri.PassLinesOff()
    tri.PassVertsOff()
    tri.Update()
    out = vtkPolyData()
    out.ShallowCopy(tri.GetOutput())
    return out


def _arrays(poly: vtkPolyData, fields: Sequence[tuple[str, int]]) -> tuple[Array, Array, Array]:
    pts = vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
    conn = vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1, 3)
    cols = []
    for name, comps in fields:
        arr = poly.GetPointData().GetArray(name)
        a = vtk_to_numpy(arr).astype(np.float64).reshape(-1, comps) if arr is not None else np.zeros((len(pts), comps))
        cols.append(a)
    values = np.hstack(cols) if cols else np.zeros((len(pts), 0))
    return pts, conn, values


def _keys(acc: Accumulator, pts: Array) -> Array:
    ijk = np.floor((pts - acc.origin) / acc.h).astype(np.int64)
    ijk = np.clip(ijk, 0, acc.dims - 1)
    out: Array = (ijk[:, 0] * acc.dims[1] + ijk[:, 1]) * acc.dims[2] + ijk[:, 2]
    return out


def add_piece(acc: Accumulator, pts: Array, conn: Array, values: Array) -> None:
    """Merge one piece: its vertices' sums per cluster (position, fields, count) and its surviving triangles."""
    if len(conn) == 0:
        return
    keys = _keys(acc, pts)
    data = np.hstack([pts, values, np.ones((len(pts), 1))])
    uniq, inverse = np.unique(keys, return_inverse=True)
    sums = np.zeros((len(uniq), data.shape[1]))
    np.add.at(sums, inverse, data)
    acc.keys.append(uniq)
    acc.sums.append(sums)
    tk = keys[conn]
    keep = (tk[:, 0] != tk[:, 1]) & (tk[:, 1] != tk[:, 2]) & (tk[:, 0] != tk[:, 2])
    tk = tk[keep]
    # One orientation-preserving canonical form: rotate so the smallest key leads.
    lead = np.argmin(tk, axis=1)
    rows = np.arange(len(tk))
    canon = np.stack([tk[rows, lead], tk[rows, (lead + 1) % 3], tk[rows, (lead + 2) % 3]], axis=1)
    acc.tris.append(canon)
    if len(acc.keys) >= 16:
        acc.merged()  # bound memory by the level's size, not by the pieces seen


def build(
    pieces: Iterable[vtkPolyData],
    out: Path,
    fields: Sequence[tuple[str, int]],
    bounds: Sequence[float],
    area: float,
    targets: Sequence[int] = DEFAULT_TARGETS,
    source: dict[str, Any] | None = None,
    units: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Levels of detail of a surface given as triangulated pieces (any split: each is merged as it comes, so
    memory holds the levels, not the surface). `bounds` and `area` are the whole surface's."""
    lo = np.array(bounds[0::2], dtype=np.float64)
    hi = np.array(bounds[1::2], dtype=np.float64)
    span = np.maximum(hi - lo, 1e-12 * max(float(np.max(hi - lo)), 1.0))
    accs = []
    for target in targets:
        h = math.sqrt(2.0 * area / max(target, 1)) if area > 0 else float(np.max(span)) / 64
        dims = np.maximum(np.ceil(span / h).astype(np.int64), 1)
        accs.append(Accumulator(lo, h, dims, [], [], []))
    for piece in pieces:
        pts, conn, values = _arrays(piece, fields)
        for acc in accs:
            add_piece(acc, pts, conn, values)
    out.mkdir(parents=True, exist_ok=True)
    ncomp = sum(c for _, c in fields)
    levels = []
    for level, acc in enumerate(accs):
        levels.append(_write_level(out, level, acc, lo, span, ncomp))
    index = {
        "schema": SCHEMA,
        "version": VERSION,
        "bounds": [float(v) for v in bounds],
        "fields": [{"name": n, "components": c, "units": (units or {}).get(n, "")} for n, c in fields],
        "source": source or {},
        "levels": levels,
    }
    tmp = out / "index.json.partial"
    tmp.write_text(json.dumps(index))
    tmp.replace(out / "index.json")
    return index


def _write_level(out: Path, level: int, acc: Accumulator, lo: Array, span: Array, ncomp: int) -> dict[str, Any]:
    keys, sums, tris = acc.merged()
    means = sums[:, :-1] / np.maximum(sums[:, -1:], 1)
    corners = np.searchsorted(keys, tris) if len(tris) else tris
    pos = means[:, :3]
    n_tiles = 2**level
    tiles = []
    if len(corners):
        centroid = pos[corners].mean(axis=1)
        t = np.clip(np.floor((centroid - lo) / span * n_tiles).astype(np.int64), 0, n_tiles - 1)
        tile_id = (t[:, 0] * n_tiles + t[:, 1]) * n_tiles + t[:, 2]
        order = np.argsort(tile_id, kind="stable")
        ids, starts = np.unique(tile_id[order], return_index=True)
        ends = [*starts[1:].tolist(), len(order)]
        (out / f"L{level}").mkdir(exist_ok=True)
        for tid, a, b in zip(ids.tolist(), starts.tolist(), ends, strict=True):
            sel = corners[order[a:b]]
            used, local = np.unique(sel, return_inverse=True)
            ijk = (tid // (n_tiles * n_tiles), (tid // n_tiles) % n_tiles, tid % n_tiles)
            name = f"L{level}/{ijk[0]}_{ijk[1]}_{ijk[2]}.bin"
            p = pos[used].astype("<f4")
            vals = means[used, 3:].astype("<f4")
            with (out / name).open("wb") as f:
                f.write(struct.pack("<II", len(used), len(sel)))
                f.write(p.tobytes())
                f.write(local.reshape(-1, 3).astype("<u4").tobytes())
                for c in range(vals.shape[1]):
                    f.write(np.ascontiguousarray(vals[:, c]).tobytes())
            tb = [float(v) for pair in zip(p.min(axis=0), p.max(axis=0), strict=True) for v in pair]
            tiles.append({"file": name, "ijk": list(ijk), "triangles": len(sel), "bounds": tb})
    return {"level": level, "cell_size": acc.h, "tiles_per_axis": n_tiles, "triangles": len(corners), "tiles": tiles}


def clustered(poly: vtkPolyData, target: int) -> vtkPolyData:
    """A triangulated surface coarsened to about `target` triangles by the same vertex clustering, in one pass:
    linear in its size whatever its shape (quadric decimation of a flat surface of millions took minutes), its
    point fields averaged per cluster."""
    pd = poly.GetPointData()
    fields = [
        (str(pd.GetArrayName(i)), pd.GetArray(i).GetNumberOfComponents())
        for i in range(pd.GetNumberOfArrays())
        if pd.GetArrayName(i)
    ]
    bounds = poly.GetBounds()
    lo = np.array(bounds[0::2], dtype=np.float64)
    span = np.array(bounds[1::2], dtype=np.float64) - lo
    span = np.maximum(span, 1e-12 * max(float(np.max(span)), 1.0))
    area = surface_area(poly)
    h = math.sqrt(2.0 * area / max(target, 1)) if area > 0 else float(np.max(span)) / 64
    acc = Accumulator(lo, h, np.maximum(np.ceil(span / h).astype(np.int64), 1), [], [], [])
    pts, conn, values = _arrays(poly, fields)
    keys = _keys(acc, pts)
    grid = int(np.prod(acc.dims))
    if grid <= 4 * len(keys) + 1_000_000:  # a dense grid: number its occupied cells without sorting
        used = np.bincount(keys, minlength=grid) > 0
        cluster = (np.cumsum(used) - 1)[keys]
    else:
        _, cluster = np.unique(keys, return_inverse=True)
    n = int(cluster.max()) + 1 if cluster.size else 0
    count = np.bincount(cluster, minlength=n)
    data = np.hstack([pts, values])
    means = np.stack([np.bincount(cluster, data[:, c], n) for c in range(data.shape[1])], axis=1)
    means /= np.maximum(count, 1)[:, None]
    tk = cluster[conn]
    kept = (tk[:, 0] != tk[:, 1]) & (tk[:, 1] != tk[:, 2]) & (tk[:, 0] != tk[:, 2])
    tk = tk[kept]
    lead = np.argmin(tk, axis=1)  # one orientation-preserving form per triangle: the smallest cluster first
    rows = np.arange(len(tk))
    tk = np.stack([tk[rows, lead], tk[rows, (lead + 1) % 3], tk[rows, (lead + 2) % 3]], axis=1).astype(np.int64)
    if n < 2**21:  # a triangle's three clusters as one int64: a 1D unique, several times faster
        code, merged = np.unique((tk[:, 0] * n + tk[:, 1]) * n + tk[:, 2], return_inverse=True)
        corners = np.stack([code // (n * n), (code // n) % n, code % n], axis=1)
    else:
        corners, merged = np.unique(tk, axis=0, return_inverse=True)
    merged = merged.ravel()
    out = vtkPolyData()
    points = vtkPoints()
    points.SetData(numpy_to_vtk(np.ascontiguousarray(means[:, :3]), deep=True))
    out.SetPoints(points)
    cells = vtkCellArray()
    offsets = np.arange(0, corners.size + 1, 3, dtype=np.int64)
    cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True), numpy_to_vtkIdTypeArray(corners.ravel(), deep=True))
    out.SetPolys(cells)
    col = 3
    for name, comps in fields:
        block = means[:, col : col + comps]
        arr = numpy_to_vtk(np.ascontiguousarray(block[:, 0] if comps == 1 else block), deep=True)
        arr.SetName(name)
        out.GetPointData().AddArray(arr)
        col += comps
    # Cell fields too: each coarse triangle the mean of the fine ones merged into it, for cell-value coloring
    cd = poly.GetCellData()
    if cd.GetNumberOfArrays() and poly.GetNumberOfCells() == len(conn):
        merges = np.maximum(np.bincount(merged, minlength=len(corners)), 1)
        for i in range(cd.GetNumberOfArrays()):
            name = cd.GetArrayName(i)
            if not name:
                continue
            fine = vtk_to_numpy(cd.GetArray(i)).astype(np.float64).reshape(len(conn), -1)[kept]
            block = np.stack([np.bincount(merged, fine[:, c], len(corners)) for c in range(fine.shape[1])], axis=1)
            block /= merges[:, None]
            arr = numpy_to_vtk(np.ascontiguousarray(block[:, 0] if fine.shape[1] == 1 else block), deep=True)
            arr.SetName(name)
            out.GetCellData().AddArray(arr)
    return out


# Reading and refining


def read_tile(path: Path, fields: Sequence[dict[str, Any]]) -> vtkPolyData:
    raw = path.read_bytes()
    n_pts, n_tri = struct.unpack_from("<II", raw)
    off = 8
    pts = np.frombuffer(raw, "<f4", n_pts * 3, off).reshape(-1, 3)
    off += 12 * n_pts
    tri = np.frombuffer(raw, "<u4", n_tri * 3, off).astype(np.int64)
    off += 12 * n_tri
    poly = vtkPolyData()
    points = vtkPoints()
    points.SetData(numpy_to_vtk(pts.astype(np.float32), deep=True))
    poly.SetPoints(points)
    cells = vtkCellArray()
    offsets = np.arange(0, tri.size + 1, 3, dtype=np.int64)
    cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True), numpy_to_vtkIdTypeArray(tri, deep=True))
    poly.SetPolys(cells)
    for f in fields:
        comps = int(f["components"])
        cols = []
        for _ in range(comps):
            cols.append(np.frombuffer(raw, "<f4", n_pts, off))
            off += 4 * n_pts
        arr = numpy_to_vtk(np.stack(cols, axis=1) if comps > 1 else cols[0].copy(), deep=True)
        arr.SetName(f["name"])
        poly.GetPointData().AddArray(arr)
    return poly


def _children(ijk: Sequence[int]) -> list[tuple[int, int, int]]:
    i, j, k = ijk
    return [(2 * i + a, 2 * j + b, 2 * k + c) for a in (0, 1) for b in (0, 1) for c in (0, 1)]


class LodSurface:
    """A tiled surface in the view: which tiles are drawn, refined against the camera within a budget."""

    def __init__(self, root: Path, max_error_px: float = 2.0) -> None:
        self.root = root
        self.index = json.loads((root / "index.json").read_text())
        if self.index.get("schema") != SCHEMA or int(self.index.get("version", 0)) > VERSION:
            raise ValueError(f"{root}: not a {SCHEMA} version {VERSION}")
        self.levels: list[dict[str, Any]] = self.index["levels"]
        self.by_ijk = [{tuple(t["ijk"]): t for t in lv["tiles"]} for lv in self.levels]
        self.max_error_px = max_error_px
        self.shown: dict[tuple[int, tuple[int, int, int]], vtkPolyData] = {}
        self.loads = 0
        self._cache: dict[tuple[int, tuple[int, int, int]], vtkPolyData] = {}

    def _tile(self, level: int, ijk: tuple[int, int, int]) -> vtkPolyData:
        key = (level, ijk)
        if key not in self._cache:
            self._cache[key] = read_tile(self.root / self.by_ijk[level][ijk]["file"], self.index["fields"])
            self.loads += 1
        return self._cache[key]

    def coarsest(self) -> vtkPolyData:
        self.shown = {(0, ijk): self._tile(0, ijk) for ijk in self.by_ijk[0]}
        return self.polydata()

    def refine(self, camera: Any, viewport_height: int, budget: int, aspect: float = 1.0) -> bool:
        """Choose the tiles for this camera: from level 0, split a visible tile while its cluster size spans
        more than `max_error_px` on screen and its children fit the budget. Returns whether the set changed."""
        planes = [0.0] * 24
        camera.GetFrustumPlanes(aspect, planes)
        pos = np.array(camera.GetPosition())
        tan = math.tan(math.radians(camera.GetViewAngle()) / 2)
        chosen: dict[tuple[int, tuple[int, int, int]], dict[str, Any]] = {(0, k): t for k, t in self.by_ijk[0].items()}
        total = sum(t["triangles"] for t in chosen.values())

        def error_px(level: int, tile: dict[str, Any]) -> float:
            b = tile["bounds"]
            center = np.array([(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2])
            radius = 0.5 * math.dist(b[0::2], b[1::2])
            d = max(float(np.linalg.norm(center - pos)) - radius, 1e-9)
            if camera.GetParallelProjection():
                return float(self.levels[level]["cell_size"] * viewport_height / (2 * camera.GetParallelScale()))
            return float(self.levels[level]["cell_size"] * viewport_height / (2 * d * tan))

        def visible(tile: dict[str, Any]) -> bool:
            b = tile["bounds"]
            for p in range(6):
                a, bb, c, dd = planes[4 * p : 4 * p + 4]
                x = b[1] if a >= 0 else b[0]
                y = b[3] if bb >= 0 else b[2]
                z = b[5] if c >= 0 else b[4]
                if a * x + bb * y + c * z + dd < 0:
                    return False
            return True

        frontier = sorted(chosen, key=lambda key: -error_px(key[0], chosen[key]))
        while frontier:
            key = frontier.pop(0)
            level, ijk = key
            tile = chosen[key]
            if level + 1 >= len(self.levels) or not visible(tile) or error_px(level, tile) <= self.max_error_px:
                continue
            kids = {c: self.by_ijk[level + 1][c] for c in _children(ijk) if c in self.by_ijk[level + 1]}
            extra = sum(t["triangles"] for t in kids.values()) - tile["triangles"]
            if total + extra > budget:
                continue
            del chosen[key]
            total += extra
            for c, t in kids.items():
                chosen[(level + 1, c)] = t
                frontier.append((level + 1, c))
            frontier.sort(key=lambda k: -error_px(k[0], chosen[k]))
        if set(chosen) == set(self.shown):
            return False
        self.shown = {k: self._tile(*k) for k in chosen}
        for k in [k for k in self._cache if k not in self.shown]:
            del self._cache[k]  # tiles out of the view leave memory
        return True

    def polydata(self) -> vtkPolyData:
        append = vtkAppendPolyData()
        for poly in self.shown.values():
            append.AddInputData(poly)
        if not self.shown:
            return vtkPolyData()
        append.Update()
        out = vtkPolyData()
        out.ShallowCopy(append.GetOutput())
        return out

    @property
    def triangles(self) -> int:
        return sum(p.GetNumberOfPolys() for p in self.shown.values())

    def levels_shown(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for level, _ in self.shown:
            out[level] = out.get(level, 0) + 1
        return out


def surface_area(poly: vtkPolyData) -> float:
    pts = vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float64)
    conn = vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1, 3)
    a, b, c = pts[conn[:, 0]], pts[conn[:, 1]], pts[conn[:, 2]]
    return float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())
