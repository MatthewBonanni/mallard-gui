"""The cells a region holds while its handle is dragged, drawn live in place of Mallard's highlight: the
domain's cells the preview's outline overlaps (an area), or that its segments cross (a
curve). Mallard's own classification replaces them when the check after the release reloads the view.

Only where the domain's cells are loaded (2D; in 3D the volume never is): elsewhere the old highlight is
hidden while dragging, as it no longer shows where the region is."""

from __future__ import annotations

import itertools
import time

import numpy as np
from numpy.typing import NDArray
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
from vtkmodules.vtkCommonCore import vtkIdList
from vtkmodules.vtkCommonDataModel import vtkDataObject, vtkPolyData, vtkStaticCellLocator
from vtkmodules.vtkFiltersCore import vtkFeatureEdges, vtkThreshold
from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter

from ..setup.geometry import Preview
from .scene import CaseScene

MASK = "live cells"
SLOW_MS = 25.0
Edges = NDArray[np.float64]  # (n, 4): x1, y1, x2, y2


def _segments(poly: vtkPolyData) -> Edges:
    """The preview's line segments; an area drawn only as polygons gives its boundary edges."""
    source = poly
    if poly.GetLines().GetNumberOfCells() == 0 and poly.GetPolys().GetNumberOfCells():
        edges = vtkFeatureEdges()
        edges.SetInputData(poly)
        edges.BoundaryEdgesOn()
        edges.FeatureEdgesOff()
        edges.ManifoldEdgesOff()
        edges.NonManifoldEdgesOff()
        edges.Update()
        source = edges.GetOutput()
    lines = source.GetLines()
    if lines.GetNumberOfCells() == 0:
        return np.zeros((0, 4))
    pts = vtk_to_numpy(source.GetPoints().GetData())[:, :2].astype(np.float64)
    ids = vtk_to_numpy(lines.GetConnectivityArray())
    offsets = vtk_to_numpy(lines.GetOffsetsArray())
    pairs = [np.stack([ids[a : b - 1], ids[a + 1 : b]], axis=1) for a, b in itertools.pairwise(offsets)]
    both = np.concatenate(pairs) if pairs else np.zeros((0, 2), dtype=int)
    return np.concatenate([pts[both[:, 0]], pts[both[:, 1]]], axis=1)


def inside(
    points: NDArray[np.float64], edges: Edges, eps: float = 0.0, order: NDArray[np.intp] | None = None
) -> NDArray[np.bool_]:
    """Points (n, 2) strictly inside closed outlines, by the even-odd rule (an annulus's two rings hold its
    ring): those within `eps` of an outline are not. Each edge toggles only the points in its band of y left
    of it, found by bisection in the points sorted by y (`order`, if known), so the cost is the points near the
    outline."""
    out = np.zeros(len(points), dtype=bool)
    if len(edges) == 0 or len(points) == 0:
        return out
    order = np.argsort(points[:, 1], kind="stable") if order is None else order
    ys, xs = points[order, 1], points[order, 0]
    odd = np.zeros(len(points), dtype=bool)
    close = np.zeros(len(points), dtype=bool)
    for x1, y1, x2, y2 in edges:
        lo, hi = (y1, y2) if y1 <= y2 else (y2, y1)
        if hi > lo:  # points with y in [lo, hi): a horizontal edge is crossed by none
            a, b = np.searchsorted(ys, lo, side="right"), np.searchsorted(ys, hi, side="right")
            if b > a:
                at = x1 + (x2 - x1) * (ys[a:b] - y1) / (y2 - y1)
                odd[a:b] ^= xs[a:b] < at
        if eps > 0:
            a, b = np.searchsorted(ys, lo - eps, side="left"), np.searchsorted(ys, hi + eps, side="right")
            if b > a:
                dx, dy = x2 - x1, y2 - y1
                t = np.clip(((xs[a:b] - x1) * dx + (ys[a:b] - y1) * dy) / max(dx * dx + dy * dy, 1e-300), 0.0, 1.0)
                close[a:b] |= (x1 + t * dx - xs[a:b]) ** 2 + (y1 + t * dy - ys[a:b]) ** 2 <= eps * eps
    out[order] = odd & ~close
    return out


class LiveCells:
    """The domain's cells and locator, kept while the domain is the same, and the last update's time."""

    def __init__(self, scene: CaseScene) -> None:
        self.scene = scene
        self._domain: vtkPolyData | None = None
        self._points: NDArray[np.float64] = np.zeros((0, 2))
        self._order: NDArray[np.intp] = np.zeros(0, dtype=np.intp)
        self._connectivity: NDArray[np.int64] = np.zeros(0, dtype=np.int64)
        self._offsets: NDArray[np.int64] = np.zeros(1, dtype=np.int64)
        self._locator: vtkStaticCellLocator | None = None
        self.ms = 0.0
        self._next = 0.0

    def _cells(self) -> vtkPolyData | None:
        b = self.scene.blocks.get("domain")
        if b is None or self.scene.dim != 2 or b.kind != "mesh":
            return None
        data = b.actor.GetMapper().GetInput()
        if not isinstance(data, vtkPolyData) or data.GetNumberOfCells() == 0:
            return None
        if data is not self._domain:
            if data.GetNumberOfCells() != data.GetPolys().GetNumberOfCells():
                return None  # a 2D domain is polygons
            self._points = vtk_to_numpy(data.GetPoints().GetData())[:, :2].astype(np.float64)
            self._order = np.argsort(self._points[:, 1], kind="stable")
            self._connectivity = vtk_to_numpy(data.GetPolys().GetConnectivityArray()).astype(np.int64)
            self._offsets = vtk_to_numpy(data.GetPolys().GetOffsetsArray()).astype(np.int64)
            self._locator = vtkStaticCellLocator()
            self._locator.SetDataSet(data)
            self._locator.BuildLocator()
            self._domain = data
        return data

    def select(self, p: Preview) -> NDArray[np.int64] | None:
        """The domain cells of the preview's region; None where the domain's cells are not loaded."""
        domain = self._cells()
        if domain is None:
            return None
        edges = _segments(p.poly)
        assert self._locator is not None
        found: set[int] = set()
        if p.dimension >= 2:
            # Mallard's cells are those the region overlaps (fraction > 0): a vertex strictly inside, or a
            # corner of the region inside the cell.
            b = domain.GetBounds()
            eps = 1e-9 * max(b[1] - b[0], b[3] - b[2], 1e-30)
            corner = inside(self._points, edges, eps, self._order)[self._connectivity]
            cells = np.logical_or.reduceat(corner, self._offsets[:-1]) if len(corner) else corner
            found.update(int(c) for c in np.flatnonzero(cells))
            for x, y in edges[:, :2]:
                c = self._locator.FindCell([float(x), float(y), 0.0])
                if c >= 0:
                    found.add(int(c))
            return np.array(sorted(found), dtype=np.int64)
        ids = vtkIdList()
        for x1, y1, x2, y2 in edges:
            self._locator.FindCellsAlongLine([x1, y1, 0.0], [x2, y2, 0.0], 1e-9, ids)
            found.update(ids.GetId(k) for k in range(ids.GetNumberOfIds()))
        return np.array(sorted(found), dtype=np.int64)

    def update(self, block: str, p: Preview, force: bool = False) -> float:
        """Draw `block` (a region's highlight) as the cells of preview `p`, or hide it where they can't be
        found here. On a mesh where that takes long, at most a fifth of the time, so the drag keeps up:
        returns 0 when drawn, else the seconds until the caller should draw the latest preview."""
        start = time.perf_counter()
        if not force and self.ms > SLOW_MS and start < self._next:
            return self._next - start
        b = self.scene.blocks.get(block)
        if b is None or b.kind != "region":
            return 0.0
        cells = self.select(p)
        if cells is None or self._domain is None:
            self.scene.set_visible(block, False)
        else:
            marked = vtkPolyData()
            marked.ShallowCopy(self._domain)
            mask = np.zeros(marked.GetNumberOfCells(), dtype=np.uint8)
            mask[cells] = 1
            array = numpy_to_vtk(mask, deep=True)
            array.SetName(MASK)
            marked.GetCellData().AddArray(array)
            keep = vtkThreshold()
            keep.SetInputData(marked)
            keep.SetInputArrayToProcess(0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, MASK)
            keep.SetLowerThreshold(0.5)
            keep.SetUpperThreshold(1.5)
            keep.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
            surface = vtkGeometryFilter()
            surface.SetInputConnection(keep.GetOutputPort())
            surface.Update()
            out = vtkPolyData()
            out.DeepCopy(surface.GetOutput())
            out.GetCellData().RemoveArray(MASK)
            self.scene.set_block_data(block, out)
        end = time.perf_counter()
        self.ms = 1000 * (end - start)
        self._next = end + 4 * (end - start)
        return 0.0
