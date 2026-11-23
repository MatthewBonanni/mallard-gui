"""What case setup draws over the case view's blocks: geometry previews of regions and probes with their drag
handles, inflow velocity vectors, and markers (a boundary's origin and toward, where its coordinates start).

Its actors are the scene's overlays, so the section tool clips them as it clips the zones. Labels and drag
handles are not actors but pins (`pins()`): the page places them over the view every frame, whoever renders
it, and drags handles itself, sending positions to `move_handle` and `release_handle`. No VTK widget takes
part, so a release that rebuilds every preview and handle frees nothing a VTK callback is still running in.
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
from vtkmodules.vtkCommonCore import vtkDoubleArray, vtkPoints
from vtkmodules.vtkCommonDataModel import vtkPolyData
from vtkmodules.vtkFiltersCore import vtkGlyph3D
from vtkmodules.vtkFiltersSources import vtkArrowSource, vtkLineSource, vtkSphereSource
from vtkmodules.vtkRenderingCore import vtkActor, vtkBillboardTextActor3D, vtkPolyDataMapper

from ..setup.geometry import Handle, Preview, Vec
from ..setup.inflow import Inflow
from ..viewer.colormaps import color_at
from .scene import OKABE_ITO, THEMES, CaseScene, flat, themed

REGION_COLOR = OKABE_ITO[3]
PROBE_COLOR = OKABE_ITO[4]
UNKNOWN_ARROW = (0.6, 0.62, 0.66)


@dataclass
class Item:
    """One preview drawn: its id (a tree node), what it is, and its actors."""

    id: str
    kind: str  # region | probe
    preview: Preview
    actors: list[vtkActor] = field(default_factory=list)


class SetupOverlay:
    LAYER = "setup"

    def __init__(self, scene: CaseScene) -> None:
        self.scene = scene
        self.items: dict[str, Item] = {}
        self.selected: str | None = None
        self.handles: list[tuple[Handle, list[float]]] = []
        self.arrows: list[vtkActor] = []
        self.markers: list[Any] = []
        self.arrow_length = 0.0
        self.on_drag: Callable[[Handle, Vec], None] | None = None
        self.on_release: Callable[[Handle, Vec], None] | None = None
        self.last_drag = 0.0
        self._markers: list[tuple[str, Vec]] = []
        self.located: list[tuple[str, Vec]] = []  # points the check report names, drawn by the page alone

    # Previews

    @property
    def size(self) -> float:
        b = self.scene.bounds
        return math.dist(b[0::2], b[1::2]) or 1.0

    def set_previews(self, previews: Sequence[tuple[str, str, Preview]], selected: str | None) -> None:
        """Draw these previews (id, kind, preview), the selected one in the accent color with its handles."""
        self._clear_items()
        self.selected = selected
        for pid, kind, p in previews:
            item = Item(pid, kind, p)
            item.actors = self._actors(item, pid == selected)
            self.items[pid] = item
        self._sync()
        self._set_handles(self.items[selected].preview.handles if selected in self.items else [])

    def update_preview(self, pid: str, p: Preview) -> None:
        """Redraw one preview in place (while a handle moves); its handles follow."""
        item = self.items.get(pid)
        if item is None:
            return
        for a in item.actors:
            self.scene.renderer.RemoveActor(a)
        item.preview = p
        item.actors = self._actors(item, pid == self.selected)
        self._sync()
        if pid == self.selected:
            self._set_handles(p.handles)

    def _actors(self, item: Item, selected: bool) -> list[vtkActor]:
        p = item.preview
        if p.poly.GetNumberOfPoints() == 0 and not p.samples:
            return []
        accent = THEMES[self.scene.theme]["accent"]
        color = accent if selected else themed(PROBE_COLOR if item.kind == "probe" else REGION_COLOR, self.scene.theme)
        out = []
        body = _actor(p.poly, color)
        prop = body.GetProperty()
        prop.SetLineWidth(3.0 if selected else 2.0)
        prop.SetPointSize(9.0 if selected else 7.0)
        prop.RenderPointsAsSpheresOn()
        if p.dimension == self.scene.dim:
            prop.SetOpacity(0.28 if selected else 0.16)
            edge = _actor(_lines_only(p.poly) if self.scene.dim == 2 else p.poly, color)
            if self.scene.dim == 3:
                edge.GetProperty().SetRepresentationToWireframe()
            edge.GetProperty().SetLineWidth(2.5 if selected else 1.5)
            edge.GetProperty().SetOpacity(0.9 if self.scene.dim == 2 else 0.25)
            edge.PickableOff()
            out.append(edge)
        elif p.dimension == 2 and self.scene.dim == 3:
            prop.SetOpacity(0.35 if selected else 0.22)
        body.PickableOff()
        out.insert(0, body)
        if p.samples and p.dimension > 0:
            pts = _points_poly(p.samples)
            samples = _actor(pts, color)
            samples.GetProperty().SetPointSize(6.0 if selected else 4.0)
            samples.GetProperty().RenderPointsAsSpheresOn()
            samples.PickableOff()
            out.append(samples)
        for a in out:
            self.scene.renderer.AddActor(a)
        return out

    def _clear_items(self) -> None:
        for item in self.items.values():
            for a in item.actors:
                self.scene.renderer.RemoveActor(a)
        self.items.clear()
        self._set_handles([])

    # Handles

    def pins(self) -> list[dict[str, Any]]:
        """What the page draws over the view: each marker's label and each handle of the selection."""
        out: list[dict[str, Any]] = [
            {"kind": "label", "text": label, "p": [float(c) for c in p], "i": i}
            for i, (label, p) in enumerate(self._markers)
        ]
        out += [{"kind": "handle", "text": h.key, "p": list(p), "i": i} for i, (h, p) in enumerate(self.handles)]
        out += [{"kind": "located", "text": label, "p": list(p), "i": i} for i, (label, p) in enumerate(self.located)]
        return out

    def body(self, limit: int = 600) -> dict[str, Any] | None:
        """The selected preview's body for the page to hit-test, so a drag on it moves the shape: its points
        (at most `limit`), as an area whose screen hull is hit or as line segments, and the handle that moves
        it: its one handle that sets a point (a center). None when no single handle moves it whole (a line's
        two ends, a polygon's vertices)."""
        item = self.items.get(self.selected or "")
        movers = [i for i, (h, _) in enumerate(self.handles) if h.role in ("point", "move")]
        if item is None or len(movers) != 1:
            return None
        mover = movers[0]
        poly = item.preview.poly
        n = poly.GetNumberOfPoints()
        if n == 0:
            return None
        step = max(1, math.ceil(n / limit))
        keep = range(0, n, step)
        index = {k: j for j, k in enumerate(keep)}
        points = [[round(c, 6) for c in poly.GetPoint(k)] for k in keep]
        segments: list[list[int]] = []
        if item.preview.dimension <= 1:
            lines = poly.GetLines()
            ids = lines.GetConnectivityArray() if lines.GetNumberOfCells() else None
            offsets = lines.GetOffsetsArray() if ids is not None else None
            for c in range(lines.GetNumberOfCells() if ids is not None and offsets is not None else 0):
                assert ids is not None and offsets is not None
                cell = [int(ids.GetValue(k)) for k in range(int(offsets.GetValue(c)), int(offsets.GetValue(c + 1)))]
                kept = [index[k - k % step] for k in cell]
                segments += [[a, b] for a, b in itertools.pairwise(kept) if a != b]
        return {"handle": mover, "points": points, "area": item.preview.dimension >= 2, "segments": segments}

    def move_handle(self, index: int, position: Sequence[float]) -> None:
        """A handle dragged in the page, still held."""
        found = self._place(index, position)
        if found:
            self.last_drag = time.monotonic()
            if self.on_drag:
                self.on_drag(*found)

    def release_handle(self, index: int, position: Sequence[float]) -> None:
        """A handle let go in the page; `on_release` may replace every preview and handle."""
        found = self._place(index, position)
        if found:
            self.last_drag = time.monotonic()
            if self.on_release:
                self.on_release(*found)

    def drag(self, index: int, position: Sequence[float]) -> None:
        """Move handle `index` to `position` and release it, as a drag in the page does."""
        self.move_handle(index, position)
        self.release_handle(index, position)

    def _set_handles(self, handles: Sequence[Handle]) -> None:
        self.handles = [(h, [float(c) for c in h.position]) for h in handles]

    def _place(self, index: int, position: Sequence[float]) -> tuple[Handle, Vec] | None:
        if not 0 <= index < len(self.handles):
            return None
        handle = self.handles[index][0]
        x, y, z = (float(c) for c in list(position)[:3])
        if self.scene.dim == 2:
            z = 0.0
        self.handles[index] = (handle, [x, y, z])
        return handle, (x, y, z)

    # Inflow vectors

    def set_inflow(self, flows: Sequence[Inflow], scale: float = 1.0) -> float:
        """Arrows of the inflows, colored by speed (cividis), the longest `0.08 * scale` of the domain's size;
        returns the length of the longest arrow (the legend's scale)."""
        for a in self.arrows:
            self.scene.renderer.RemoveActor(a)
        self.arrows = []
        top = max((f.max_speed for f in flows if f.known), default=0.0)
        self.arrow_length = 0.08 * scale * self.size
        for f in flows:
            if not f.points:
                continue
            pts = vtkPoints()
            vec = vtkDoubleArray()
            vec.SetName("velocity")
            vec.SetNumberOfComponents(3)
            mag = vtkDoubleArray()
            mag.SetName("speed")
            for p, v in zip(f.points, f.vectors, strict=True):
                pts.InsertNextPoint(*p)
                vec.InsertNextTuple3(*v)
                mag.InsertNextValue(math.sqrt(sum(c * c for c in v)))
            poly = vtkPolyData()
            poly.SetPoints(pts)
            poly.GetPointData().AddArray(vec)
            poly.GetPointData().SetVectors(vec)
            poly.GetPointData().AddArray(mag)
            poly.GetPointData().SetScalars(mag)
            arrow = vtkArrowSource()
            arrow.SetTipResolution(12)
            arrow.SetShaftResolution(8)
            glyph = vtkGlyph3D()
            glyph.SetInputData(poly)
            glyph.SetSourceConnection(arrow.GetOutputPort())
            glyph.SetVectorModeToUseVector()
            glyph.OrientOn()
            if f.known and top > 0:
                glyph.SetScaleModeToScaleByVector()
                glyph.SetScaleFactor(self.arrow_length / top)
            else:
                glyph.SetScaleModeToDataScalingOff()
                glyph.SetScaleFactor(0.6 * self.arrow_length)
            glyph.SetColorModeToColorByScalar()
            glyph.Update()
            arrows = glyph.GetOutput()
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(arrows)
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.PickableOff()
            if f.known and top > 0:
                # Colors as RGB, not a lookup table, and unlit: the browser draws the legend's colors exactly.
                speed = vtk_to_numpy(arrows.GetPointData().GetScalars())
                table = speed_colors(self.scene.theme)
                rgb = table[np.clip(np.rint(speed / top * 255), 0, 255).astype(np.intp)]
                colors = numpy_to_vtk(rgb, deep=True)
                colors.SetName("speed_rgb")
                arrows.GetPointData().SetScalars(colors)
                mapper.SetColorModeToDirectScalars()
                flat(actor)
                mapper.ScalarVisibilityOn()
            else:
                mapper.ScalarVisibilityOff()
                actor.GetProperty().SetColor(*themed(UNKNOWN_ARROW, self.scene.theme))
            self.arrows.append(actor)
            self.scene.renderer.AddActor(actor)
        self._sync()
        return self.arrow_length

    # Markers

    def set_markers(self, markers: Sequence[tuple[str, Vec]]) -> None:
        """Labeled points: a boundary's `origin` and `toward`, where its coordinates start."""
        for a in self.markers:
            self.scene.renderer.RemoveActor(a)
        self._markers = list(markers)
        accent = THEMES[self.scene.theme]["accent"]
        self.markers = marker_actors(markers, accent, self.size, labels=False)
        for a in self.markers:
            self.scene.renderer.AddActor(a)
        self._sync()

    def _sync(self) -> None:
        """Register every actor as an overlay of the scene, so the section tool clips it."""
        actors = [a for i in self.items.values() for a in i.actors] + self.arrows
        actors += [a for a in self.markers if isinstance(a, vtkActor)]
        self.scene.set_overlay(self.LAYER, actors)


def marker_actors(
    markers: Sequence[tuple[str, Vec]],
    color: tuple[float, float, float],
    size: float,
    lines: Sequence[tuple[Vec, Vec]] = (),
    labels: bool = True,
) -> list[Any]:
    """A dot and a label per point (`labels`: else the page labels them), and segments between pairs of
    points; none of them pickable."""
    out: list[Any] = []
    for label, p in markers:
        s = vtkSphereSource()
        s.SetCenter(*p)
        s.SetRadius(0.008 * size)
        s.Update()
        dot = _actor(s.GetOutput(), color)
        dot.PickableOff()
        out.append(dot)
        if not labels:
            continue
        text = vtkBillboardTextActor3D()
        text.SetInput(label)
        text.SetPosition(*p)
        text.SetDisplayOffset(8, 8)
        tp = text.GetTextProperty()
        tp.SetFontSize(26)
        tp.SetColor(*color)
        tp.SetBold(True)
        text.PickableOff()
        out.append(text)
    for a, b in lines:
        seg = vtkLineSource()
        seg.SetPoint1(*a)
        seg.SetPoint2(*b)
        seg.Update()
        actor = _actor(seg.GetOutput(), color)
        actor.GetProperty().SetLineWidth(3)
        actor.PickableOff()
        out.append(actor)
    return out


def speed_gradient(theme: str) -> str:
    """The arrows' colors as a CSS gradient, for their legend."""
    table = speed_colors(theme)
    stops = [
        f"rgb({r},{g},{b}) {round(100 * i / 255)}%" for i, (r, g, b) in enumerate(table) if i % 32 == 0 or i == 255
    ]
    return f"linear-gradient(to right, {', '.join(stops)})"


def speed_colors(theme: str) -> np.ndarray:
    """Cividis in 256 RGB bytes for arrows over `theme`'s background: on white its pale yellow end is darkened
    until it reads."""
    rows = [themed(color_at("Cividis", i / 255), theme) for i in range(256)]
    return np.rint(np.array(rows) * 255).astype(np.uint8)


def _actor(poly: vtkPolyData, color: tuple[float, float, float]) -> vtkActor:
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(poly)
    mapper.ScalarVisibilityOff()
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*color)
    if poly.GetNumberOfPolys() == 0 and poly.GetNumberOfStrips() == 0:
        flat(actor)
    return actor


def _points_poly(points: Sequence[Vec]) -> vtkPolyData:
    from ..setup.geometry import _polydata

    return _polydata(points, verts=list(range(len(points))))


def _lines_only(poly: vtkPolyData) -> vtkPolyData:
    out = vtkPolyData()
    out.SetPoints(poly.GetPoints())
    out.SetLines(poly.GetLines())
    return out
