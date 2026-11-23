"""The analysis overlays of the case view, drawn as layers of the scene (so the section tool clips them) from
files Mallard writes: the case view's zone blocks (with their normals and walls' `y_plus`), the initial state and
the mesh fields (per-cell quality). They color and select what Mallard computed; they compute no metric."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vtkmodules.vtkCommonCore import VTK_COLOR_MODE_MAP_SCALARS, vtkPoints
from vtkmodules.vtkCommonDataModel import (
    vtkDataObject,
    vtkDataSet,
    vtkPlane,
    vtkPolyData,
)
from vtkmodules.vtkFiltersCore import vtkCellCenters, vtkCutter, vtkGlyph3D, vtkThreshold
from vtkmodules.vtkFiltersGeometry import vtkDataSetSurfaceFilter, vtkGeometryFilter
from vtkmodules.vtkFiltersSources import vtkArrowSource, vtkSphereSource
from vtkmodules.vtkIOHDF import vtkHDFReader
from vtkmodules.vtkIOXML import vtkXMLGenericDataObjectReader
from vtkmodules.vtkRenderingCore import vtkActor, vtkColorTransferFunction, vtkPolyDataMapper

from ..view.scene import THEMES, CaseScene, flat, read_vtm
from ..viewer.colormaps import DEFAULT, DIVERGING, canonical, symmetric, transfer_function
from .report import Histogram

DANGER = (0.89, 0.29, 0.24)
WARNING = (0.95, 0.65, 0.15)
# Per-cell files larger than this are not read: their histograms come from the report alone
CELL_BUDGET = 2_000_000


def read_dataset(path: Path) -> vtkDataSet:
    """A snapshot Mallard wrote: VTKHDF, or a VTU file."""
    if path.suffix == ".vtkhdf":
        reader: Any = vtkHDFReader()
    else:
        reader = vtkXMLGenericDataObjectReader()
    reader.SetFileName(str(path))
    reader.Update()
    out = reader.GetOutputDataObject(0)
    if not isinstance(out, vtkDataSet):
        raise ValueError(f"{path.name}: not a dataset")
    return out


# The case view's per-face arrays, uppercase like field names (Mallard #1468); older builds wrote them lowercase.
VIEW_ARRAYS = ("NORMAL", "TANGENT", "AREA", "LENGTH", "COUNT", "FRACTION", "SIDE", "Y_PLUS")


def uppercase_arrays(data: vtkDataSet) -> vtkDataSet:
    """`data` with an older Mallard's lowercase case-view arrays named as the current one names them."""
    for fields in (data.GetCellData(), data.GetPointData()):
        for name in VIEW_ARRAYS:
            old = fields.GetArray(name.lower())
            if old is not None and fields.GetArray(name) is None:
                old.SetName(name)
    return data


def zone_blocks(view: Path) -> dict[str, vtkDataSet]:
    """The case view's blocks by name, the domain's cells left out."""
    return {name: uppercase_arrays(read_dataset(path)) for name, path in read_vtm(view) if name != "domain"}


def lookup_table(colormap: str, lo: float, hi: float) -> vtkColorTransferFunction:
    if canonical(colormap) in DIVERGING:
        lo, hi = symmetric(lo, hi)
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0) * 1e-6
    lut: vtkColorTransferFunction = transfer_function(colormap, lo, hi)
    return lut


def _surface(data: vtkDataSet) -> vtkPolyData:
    f = vtkGeometryFilter()
    f.SetInputData(data)
    f.Update()
    out = vtkPolyData()
    out.ShallowCopy(f.GetOutput())
    return out


def _actor(poly: vtkPolyData, color: Sequence[float] | None = None, on_top: bool = True) -> vtkActor:
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(poly)
    if on_top:
        # Drawn over the zones they coincide with
        mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-2, -2)
        mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-2, -2)
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.PickableOff()
    if color is not None:
        mapper.ScalarVisibilityOff()
        actor.GetProperty().SetColor(*color)
    return actor


def _colored(poly: vtkPolyData, array: str, lut: vtkColorTransferFunction, component: int = -1) -> vtkActor:
    """Cells colored by a cell array through `lut`, mapped here to RGB so the browser (vtk.js) draws the same
    colors as this process, with no lookup table of its own."""
    if component >= 0:
        lut.SetVectorModeToComponent()
        lut.SetVectorComponent(component)
    else:
        lut.SetVectorModeToMagnitude()
    colored = vtkPolyData()
    colored.CopyStructure(poly)
    values = poly.GetCellData().GetArray(array)
    if values is not None:
        rgb = lut.MapScalars(values, VTK_COLOR_MODE_MAP_SCALARS, -1)
        rgb.SetName(array)
        colored.GetCellData().SetScalars(rgb)
    actor = _actor(colored)
    mapper = actor.GetMapper()
    mapper.SetScalarModeToUseCellData()
    mapper.SetColorModeToDirectScalars()
    mapper.ScalarVisibilityOn()
    flat(actor)  # the colormap's colors as they are: lit, a flat field was drawn washed out
    return actor


def _lifted(scene: CaseScene, actors: list[vtkActor]) -> list[vtkActor]:
    """In 2D, actors moved a hair toward the viewer (+z), so they cover the mesh lines they coincide with in
    both renderers: depth offsets reach vtk.js only in part."""
    if scene.dim == 2:
        b = scene.bounds
        lift = 1e-3 * math.dist(b[0::2], b[1::2])
        for a in actors:
            a.SetPosition(0.0, 0.0, lift)
    return actors


def face_size(data: vtkDataSet, dim: int) -> float:
    """The mean size of a zone's faces from the measures Mallard gives them (`LENGTH` in 2D, `AREA` in 3D)."""
    arr = data.GetCellData().GetArray("AREA" if dim == 3 else "LENGTH")
    n = data.GetNumberOfCells()
    if arr is None or n == 0:
        b = data.GetBounds()
        return max(b[1] - b[0], b[3] - b[2], b[5] - b[4]) / max(n, 1)
    total = sum(arr.GetValue(i) for i in range(n))
    return math.sqrt(total / n) if dim == 3 else total / n


def normal_glyphs(data: vtkDataSet, scale: float) -> vtkPolyData:
    """Arrows along each face's `NORMAL` (the case view's, pointing out of the domain) at its center."""
    centers = vtkCellCenters()
    centers.SetInputData(data)
    centers.VertexCellsOff()
    centers.Update()
    pts = centers.GetOutput()
    pts.GetPointData().SetActiveVectors("NORMAL")
    arrow = vtkArrowSource()
    arrow.SetTipLength(0.35)
    arrow.SetTipRadius(0.12)
    arrow.SetShaftRadius(0.04)
    glyph = vtkGlyph3D()
    glyph.SetInputData(pts)
    glyph.SetSourceConnection(arrow.GetOutputPort())
    glyph.SetVectorModeToUseVector()
    glyph.SetScaleModeToDataScalingOff()
    glyph.SetScaleFactor(scale)
    glyph.OrientOn()
    glyph.Update()
    out = vtkPolyData()
    out.ShallowCopy(glyph.GetOutput())
    return out


class BoundaryOverlay:
    """Zones without a condition, face normals and walls colored by Mallard's first-cell y+."""

    LAYER = "analysis.boundaries"

    def __init__(self, scene: CaseScene) -> None:
        self.scene = scene
        self.blocks: dict[str, vtkDataSet] = {}

    def load(self, view: Path | None) -> None:
        self.blocks = zone_blocks(view) if view and view.exists() else {}

    def y_plus_range(self) -> tuple[float, float] | None:
        values = [
            arr.GetRange()
            for data in self.blocks.values()
            if (arr := data.GetCellData().GetArray("Y_PLUS")) is not None and data.GetNumberOfCells()
        ]
        if not values:
            return None
        return min(v[0] for v in values), max(v[1] for v in values)

    def walls(self) -> list[str]:
        return [n for n, d in self.blocks.items() if d.GetCellData().GetArray("Y_PLUS") is not None]

    def show(
        self,
        zones: Sequence[str],
        unassigned: Sequence[str],
        normals: bool,
        y_plus: bool,
        colormap: str = DEFAULT,
        y_range: tuple[float, float] | None = None,
    ) -> None:
        actors: list[vtkActor] = []
        dim = self.scene.dim
        width = 6.0
        for name in unassigned:
            data = self.blocks.get(name)
            if data is None:
                continue
            a = _actor(_surface(data), DANGER)
            a.GetProperty().SetLineWidth(width)
            if dim == 3:
                a.GetProperty().EdgeVisibilityOn()
                a.GetProperty().SetEdgeColor(1.0, 1.0, 1.0)
            actors.append(a)
        if y_plus:
            rng = y_range or self.y_plus_range()
            if rng is not None:
                for name in self.walls():
                    a = _colored(_surface(self.blocks[name]), "Y_PLUS", lookup_table(colormap, *rng))
                    a.GetProperty().SetLineWidth(width)
                    actors.append(a)
        if normals:
            accent = THEMES[self.scene.theme]["accent"]
            for name in [*zones, *unassigned]:
                data = self.blocks.get(name)
                if data is None or data.GetCellData().GetArray("NORMAL") is None:
                    continue
                glyphs = normal_glyphs(data, 1.2 * face_size(data, dim))
                actors.append(_actor(glyphs, DANGER if name in unassigned else accent, on_top=False))
        self.scene.set_overlay(self.LAYER, _lifted(self.scene, actors))

    def clear(self) -> None:
        self.scene.set_overlay(self.LAYER, [])


class InitialOverlay:
    """The initial state on the mesh: cells colored by a field (3D: the domain's surface, and a slice on the
    section plane while it clips)."""

    LAYER = "analysis.initial"

    def __init__(self, scene: CaseScene) -> None:
        self.scene = scene
        self.data: vtkDataSet | None = None
        self._surface: vtkPolyData | None = None

    def load(self, path: Path | None) -> None:
        self.data = read_dataset(path) if path and path.exists() else None
        self._surface = None
        if self.data is not None:
            f = vtkDataSetSurfaceFilter()
            f.SetInputData(self.data)
            f.Update()
            self._surface = vtkPolyData()
            self._surface.ShallowCopy(f.GetOutput())

    def fields(self) -> list[dict[str, Any]]:
        if self.data is None:
            return []
        cd = self.data.GetCellData()
        out = []
        for i in range(cd.GetNumberOfArrays()):
            arr = cd.GetArray(i)
            out.append({"name": arr.GetName(), "components": arr.GetNumberOfComponents()})
        return out

    def range(self, field: str, component: int = -1) -> tuple[float, float] | None:
        if self.data is None:
            return None
        arr = self.data.GetCellData().GetArray(field)
        if arr is None:
            return None
        lo, hi = arr.GetRange(component)
        return float(lo), float(hi)

    def show(self, field: str, colormap: str, rng: tuple[float, float], component: int = -1) -> None:
        if self.data is None or self._surface is None:
            self.clear()
            return
        lut = lookup_table(colormap, *rng)
        actors = [_colored(self._surface, field, lut, component)]
        actors[0].GetMapper().SetResolveCoincidentTopologyToOff()
        if self.scene.dim == 3 and self.scene.clip_enabled:
            actors.append(_colored(self._slice(), field, lut, component))
        self.scene.set_overlay(self.LAYER, _lifted(self.scene, actors))

    def _slice(self) -> vtkPolyData:
        """The cells cut by the section plane, moved a hair into the half kept so the plane does not clip it."""
        assert self.data is not None
        plane = self.scene.plane
        b = self.scene.bounds
        eps = 1e-4 * math.sqrt(sum((b[2 * i + 1] - b[2 * i]) ** 2 for i in range(3)))
        cut = vtkPlane()
        n = plane.GetNormal()
        cut.SetNormal(*n)
        cut.SetOrigin(*(o + eps * c for o, c in zip(plane.GetOrigin(), n, strict=True)))
        cutter = vtkCutter()
        cutter.SetCutFunction(cut)
        cutter.SetInputData(self.data)
        cutter.Update()
        out = vtkPolyData()
        out.ShallowCopy(cutter.GetOutput())
        return out

    def clear(self) -> None:
        self.scene.set_overlay(self.LAYER, [])


class QualityOverlay:
    """Cells of a quality metric's bin and the worst cells, from the mesh fields and the report."""

    LAYER = "analysis.quality"

    def __init__(self, scene: CaseScene) -> None:
        self.scene = scene
        self.data: vtkDataSet | None = None
        self.too_large = False

    def load(self, path: Path | None, cells: int) -> None:
        self.too_large = cells > CELL_BUDGET
        self.data = read_dataset(path) if path and path.exists() and not self.too_large else None

    def cells_between(self, metric: str, lo: float, hi: float, last: bool) -> vtkDataSet | None:
        """The cells with lo <= metric < hi (<= hi for the last bin), as Mallard's histogram counts them."""
        if self.data is None or self.data.GetCellData().GetArray(metric) is None:
            return None
        t = vtkThreshold()
        t.SetInputData(self.data)
        t.SetInputArrayToProcess(0, 0, 0, vtkDataObject.FIELD_ASSOCIATION_CELLS, metric)
        t.SetThresholdFunction(vtkThreshold.THRESHOLD_BETWEEN)
        width = hi - lo
        t.SetLowerThreshold(lo)
        t.SetUpperThreshold(hi if last else hi - 1e-9 * (width or 1.0))
        t.Update()
        out: vtkDataSet = t.GetOutput()
        return out

    def show(self, hist: Histogram | None, selected: int | None, worst: bool) -> int:
        """Draw the selected bin's cells and the worst cells; the number of cells selected."""
        actors: list[vtkActor] = []
        n = 0
        if hist is not None and selected is not None and 0 <= selected < len(hist.counts):
            lo, hi = hist.bin_range(selected)
            cells = self.cells_between(hist.metric, lo, hi, selected == len(hist.counts) - 1)
            if cells is not None:
                n = cells.GetNumberOfCells()
                a = _actor(_surface(cells), THEMES[self.scene.theme]["accent"])
                a.GetProperty().EdgeVisibilityOn()
                a.GetProperty().SetEdgeColor(*(0.5 * c for c in THEMES[self.scene.theme]["accent"]))
                actors.append(a)
        if hist is not None and worst and hist.worst:
            if self.data is not None:
                last = min(w["value"] for w in hist.worst)
                cells = self.cells_between(hist.metric, last, hist.edges[-1], True)
                if cells is not None and cells.GetNumberOfCells():
                    a = _actor(_surface(cells), DANGER)
                    a.GetProperty().EdgeVisibilityOn()
                    a.GetProperty().SetEdgeColor(1.0, 1.0, 1.0)
                    actors.append(a)
            actors.append(self._markers([w["x"] for w in hist.worst]))
        self.scene.set_overlay(self.LAYER, _lifted(self.scene, actors))
        return n

    def _markers(self, points: Sequence[Sequence[float]]) -> vtkActor:
        """A sphere at each worst cell's centroid, from the report, so a cell smaller than a pixel shows."""
        pts = vtkPoints()
        for x in points:
            pts.InsertNextPoint(x[0], x[1], x[2] if len(x) > 2 else 0.0)
        poly = vtkPolyData()
        poly.SetPoints(pts)
        b = self.scene.bounds
        size = 0.012 * max(b[1] - b[0], b[3] - b[2], b[5] - b[4])
        sphere = vtkSphereSource()
        sphere.SetRadius(size)
        sphere.SetThetaResolution(16)
        sphere.SetPhiResolution(12)
        glyph = vtkGlyph3D()
        glyph.SetInputData(poly)
        glyph.SetSourceConnection(sphere.GetOutputPort())
        glyph.SetScaleModeToDataScalingOff()
        glyph.Update()
        out = vtkPolyData()
        out.ShallowCopy(glyph.GetOutput())
        a = _actor(out, WARNING, on_top=False)
        a.GetProperty().SetOpacity(0.85)
        return a

    def clear(self) -> None:
        self.scene.set_overlay(self.LAYER, [])
