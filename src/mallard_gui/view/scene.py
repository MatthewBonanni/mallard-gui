"""The case view: the blocks `mallard check --view` writes (mesh, zones, regions, probes), rendered with
VTK on the server, with navigation presets, the section tool and picking.

Clip planes and boxes are clipping planes on the mappers (no new geometry), x-ray is per-actor
opacity with depth peeling, isolate and hide toggle actors, and picking is hardware selection,
which honors clipping planes, so a click hits the first visible surface, interior ones included.
When the browser draws the scene (vtk.js), it picks there and sends the world position, which finds the
face here.

Lines are one pixel: a surface's feature edges always, toned toward its color, and every face's edges only
with `mesh_edges` (the "show mesh" toggle)."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

# Rendering backends and the interactor style register themselves on import.
import vtkmodules.vtkInteractionStyle
from vtkmodules.vtkCommonCore import reference, vtkCommand
from vtkmodules.vtkCommonDataModel import (
    vtkDataObject,
    vtkDataSet,
    vtkPlane,
    vtkPolyData,
    vtkSelectionNode,
    vtkStaticCellLocator,
)
from vtkmodules.vtkFiltersCore import vtkAppendPolyData, vtkCleanPolyData, vtkCutter, vtkFeatureEdges
from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter
from vtkmodules.vtkFiltersSources import vtkCubeSource, vtkLineSource, vtkOutlineSource
from vtkmodules.vtkInteractionWidgets import vtkImplicitPlaneRepresentation, vtkImplicitPlaneWidget2
from vtkmodules.vtkIOXML import vtkXMLGenericDataObjectReader
from vtkmodules.vtkRenderingCore import (
    vtkActor,
    vtkCellPicker,
    vtkHardwareSelector,
    vtkMapper,
    vtkPolyDataMapper,
    vtkRenderer,
    vtkRenderWindow,
    vtkRenderWindowInteractor,
)

from ..macos import hide_from_dock
from ..tokens import GUI_COLORS
from .scene_ids import reference_id

import vtkmodules.vtkRenderingOpenGL2  # noqa: F401  isort: skip
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera

OKABE_ITO = (
    (0.337, 0.706, 0.914),
    (0.902, 0.624, 0.0),
    (0.0, 0.620, 0.451),
    (0.800, 0.475, 0.655),
    (0.941, 0.894, 0.259),
    (0.0, 0.447, 0.698),
    (0.835, 0.369, 0.0),
)


def _rgb(hex_color: str) -> tuple[float, float, float]:
    return (int(hex_color[1:3], 16) / 255, int(hex_color[3:5], 16) / 255, int(hex_color[5:7], 16) / 255)


# The views' backgrounds are the canvas token, so a view sits in its pane as the design draws it.
THEMES = {
    "dark": {
        "background": _rgb(GUI_COLORS["dark"]["canvas"]),
        "mesh": (0.26, 0.31, 0.37),
        "accent": (0.310, 0.769, 0.714),
        "outline": (0.42, 0.49, 0.56),
    },
    "light": {
        "background": _rgb(GUI_COLORS["light"]["canvas"]),
        "mesh": (0.70, 0.74, 0.78),
        "accent": (0.043, 0.420, 0.388),
        "outline": (0.45, 0.50, 0.56),
    },
}
# What a line or point must reach against the view's background (WCAG's 3:1 for graphics); the mesh is
# deliberately quieter, but still drawn.
LINE_CONTRAST = 3.0
MESH_CONTRAST = 1.6


def luminance(color: Sequence[float]) -> float:
    """Relative luminance (WCAG) of an RGB color in 0..1."""
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in color[:3]]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: Sequence[float], b: Sequence[float]) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def themed(color: Sequence[float], theme: str, at_least: float = LINE_CONTRAST) -> tuple[float, float, float]:
    """`color` as drawn on `theme`'s background: darkened (light theme) or lightened (dark) just enough to stand
    out, so the Okabe-Ito yellow reads on white; colors that already do are unchanged."""
    bg = THEMES[theme]["background"]
    out = (float(color[0]), float(color[1]), float(color[2]))
    toward = 0.0 if luminance(bg) > 0.5 else 1.0
    for _ in range(40):
        if contrast(out, bg) >= at_least:
            break
        out = (out[0] + (toward - out[0]) * 0.06, out[1] + (toward - out[1]) * 0.06, out[2] + (toward - out[2]) * 0.06)
    return out


PRESETS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    # view direction (camera to focal point), view up
    "+X": ((1, 0, 0), (0, 1, 0)),
    "-X": ((-1, 0, 0), (0, 1, 0)),
    "+Y": ((0, 1, 0), (0, 0, 1)),
    "-Y": ((0, -1, 0), (0, 0, 1)),
    "+Z": ((0, 0, 1), (0, 1, 0)),
    "-Z": ((0, 0, -1), (0, 1, 0)),
    "iso": ((-1, -1, -1), (0, 1, 0)),
}
PLANE_PRESETS = ("+Z", "-Z")  # a 2D case's own plane, seen from either side
_SELECTOR = re.compile(r'^(?P<array>[a-z_]+)\[(?P<key>[a-z_]+) = "(?P<value>[^"]*)"\](?P<rest>.*)$')


@dataclass
class Block:
    name: str
    label: str
    kind: str  # mesh | zone | region | probe
    color: tuple[float, float, float]
    input_path: str | None
    dimension: int
    cells: int
    actor: vtkActor
    extra: list[vtkActor] = field(default_factory=list)
    visible: bool = True
    drawn: tuple[float, float, float] | None = None  # the color on the current theme's background

    @property
    def actors(self) -> list[vtkActor]:
        return [self.actor, *self.extra]

    def info(self) -> dict[str, Any]:
        r, g, b = (round(c * 255) for c in (self.drawn or self.color))
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "color": f"rgb({r},{g},{b})",
            "input_path": self.input_path,
            "dimension": self.dimension,
            "cells": self.cells,
            "visible": self.visible,
        }


def label_of(name: str) -> str:
    """A block's short name: `write_data[prefix = "./solut/near_wake"].region` -> `near_wake`."""
    m = _SELECTOR.match(name)
    if not m:
        return name
    return m["value"].rstrip("/").rsplit("/", 1)[-1]


def input_path_of(name: str, kind: str, assigned: set[str]) -> str | None:
    """The input element a block draws: its name is the element's key path.

    TODO(#1313): block metadata in the case view; until then names are parsed."""
    if kind == "zone":
        return f'boundaries[name = "{name}"]' if name in assigned else None
    if _SELECTOR.match(name):
        return name
    return f"regions.{name}" if kind == "region" else None


def _boundary_edges(data: vtkPolyData) -> vtkPolyData:
    """The outline of a set of cells: the edges only one of them has, once their shared points are merged."""
    merged = vtkCleanPolyData()
    merged.SetInputData(data)
    edges = vtkFeatureEdges()
    edges.SetInputConnection(merged.GetOutputPort())
    edges.BoundaryEdgesOn()
    edges.FeatureEdgesOff()
    edges.ManifoldEdgesOff()
    edges.NonManifoldEdgesOff()
    edges.Update()
    out = vtkPolyData()
    out.ShallowCopy(edges.GetOutput())
    return out


def snap_normal(normal: Sequence[float], degrees: float = 15.0) -> tuple[float, float, float]:
    """The nearest axis, if the normal is within `degrees` of it; else the normal, normalized."""
    n = math.sqrt(sum(c * c for c in normal)) or 1.0
    unit = tuple(c / n for c in normal)
    axis = max(range(3), key=lambda i: abs(unit[i]))
    if abs(unit[axis]) >= math.cos(math.radians(degrees)):
        out = [0.0, 0.0, 0.0]
        out[axis] = math.copysign(1.0, unit[axis])
        return out[0], out[1], out[2]
    return unit[0], unit[1], unit[2]


class CaseScene:
    # Kinds drawn translucent by x-ray, and kinds the clip plane leaves whole (a cut lies on the plane).
    XRAY_KINDS: tuple[str, ...] = ("zone", "region")
    UNCLIPPED_KINDS: tuple[str, ...] = ()

    def __init__(self, offscreen: bool = True, theme: str = "dark") -> None:
        if offscreen:
            hide_from_dock()
        # Lines on a surface (feature and mesh edges) drawn in front of it, not z-fighting it: here and in vtk.js.
        vtkMapper.SetResolveCoincidentTopologyToPolygonOffset()
        self.renderer = vtkRenderer()
        self.window = vtkRenderWindow()
        self.window.SetOffScreenRendering(1 if offscreen else 0)
        self.window.AddRenderer(self.renderer)
        self.window.SetAlphaBitPlanes(1)
        self.window.SetMultiSamples(0)
        self.window.SetSize(800, 600)
        self.renderer.SetUseDepthPeeling(1)
        self.renderer.SetMaximumNumberOfPeels(8)
        self.interactor = vtkRenderWindowInteractor()
        self.interactor.SetRenderWindow(self.window)
        self.style = vtkInteractorStyleTrackballCamera()
        self.interactor.SetInteractorStyle(self.style)
        self.style.AddObserver(vtkCommand.LeftButtonPressEvent, self._left_press, 1.0)
        self.style.AddObserver(vtkCommand.LeftButtonReleaseEvent, self._left_release, 1.0)
        self._panning = False
        # Until the user moves the camera, a resized view (the browser's first size) is framed again.
        self.framed = False
        self.window.AddObserver(vtkCommand.WindowResizeEvent, self._resized)
        self.style.AddObserver(vtkCommand.StartInteractionEvent, self._interacted)
        self.theme = theme
        self.blocks: dict[str, Block] = {}
        # Actors drawn over the blocks by named layers (case setup's previews, other tools), clipped with them
        self.overlay_layers: dict[str, list[vtkActor]] = {}
        self.skipped: list[str] = []
        self.dim = 3
        self.selected: str | None = None
        self.xray = False
        self.xray_opacity = 0.2
        self.mesh_edges = False
        self._locators: dict[str, vtkStaticCellLocator] = {}
        self.lock_2d = False
        self.aspect = 0.0  # the view's width / height where it is drawn, when that is not this window
        self.plane = vtkPlane()
        self.clip_enabled = False
        self.clip_origin = (0.0, 0.0, 0.0)
        self.clip_normal = (0.0, 0.0, 1.0)
        self.keep_flipped = False
        self.clipped: list[vtkActor] = []  # the actors the plane cuts now, for a view that clips them itself
        self.widget_shown = True  # the plane's drag widget, where this process renders the view
        self.section_mark = True  # the plane's outline and normal, drawn in the scene while it cuts
        self._mark: vtkActor | None = None
        self.on_clip_moved: Callable[[], None] | None = None
        self._widget: vtkImplicitPlaneWidget2 | None = None
        self._plane_rep = vtkImplicitPlaneRepresentation()
        self.bounds = (0.0, 1.0, 0.0, 1.0, 0.0, 1.0)
        self.set_theme(theme)

    # Loading

    def load(self, vtm: Path, report: dict[str, Any] | None = None, assigned: set[str] | None = None) -> None:
        """Load a case view; `report` is the check report (its zones and regions classify blocks)."""
        report = report or {}
        self.clear()
        self.dim = int(report.get("dim") or 3)
        if self.dim == 2 and not (self.clip_normal[0] or self.clip_normal[1]):
            self.clip_normal = (1.0, 0.0, 0.0)  # a plane that cuts a 2D case: the page's own cuts nothing
        zones = {b.get("zone") for b in report.get("boundaries", [])}
        zones |= {u.get("zone") for u in report.get("unassigned") or []}
        zones |= {
            r["name"]
            for r in report.get("regions", [])
            if r.get("of_mesh") and r["name"] != "domain" and r.get("dimension") == self.dim - 1
        }
        cell_zones = {
            r["name"]
            for r in report.get("regions", [])
            if r.get("of_mesh") and r["name"] != "domain" and r.get("dimension") == self.dim
        }
        assigned = assigned if assigned is not None else zones
        real = {r["name"].replace("/", "_"): r["name"] for r in report.get("regions", [])}
        colors = {"zone": 0, "region": 3, "probe": 4}
        self.skipped = []
        for name, path in read_vtm(vtm):
            # The case view writes '/' in block names as '_'; the report has the names as written.
            name = real.get(name, name)
            kind = (
                "mesh"
                if name == "domain"
                else "zone"
                if name in zones
                else "probe"
                if name.startswith("probes[")
                else "region"
            )
            if self.dim == 3 and (kind == "mesh" or name in cell_zones):
                # The volume is never loaded for viewing; the domain's outline comes from its zones.
                # TODO(#1313): surface-only case views would draw cell zones by their boundaries.
                self.skipped.append(name)
                continue
            block = _read_block(path)
            if block is None:
                continue
            color = THEMES[self.theme]["mesh"] if kind == "mesh" else OKABE_ITO[colors[kind] % len(OKABE_ITO)]
            if kind != "mesh":
                colors[kind] += 1
            dimension = _topological_dimension(block)
            actor, extra = self._actors(block, kind, dimension, color)
            b = Block(
                name,
                label_of(name),
                kind,
                color,
                input_path_of(name, kind, assigned),
                dimension,
                block.GetNumberOfCells(),
                actor,
                extra,
            )
            self.blocks[name] = b
            for a in b.actors:
                self.renderer.AddActor(a)
        if self.dim == 3 and self.blocks:
            bounds = self.renderer.ComputeVisiblePropBounds()
            outline = vtkOutlineSource()
            outline.SetBounds(*bounds)
            outline.Update()
            actor = _actor(outline.GetOutput(), THEMES[self.theme]["outline"])
            actor.PickableOff()
            self.blocks["domain"] = Block("domain", "domain", "mesh", THEMES[self.theme]["outline"], None, 3, 0, actor)
            self.renderer.AddActor(actor)
        self.bounds = self.renderer.ComputeVisiblePropBounds()
        self.lock_2d = self.dim == 2
        self.clip_origin = tuple((self.bounds[2 * i] + self.bounds[2 * i + 1]) / 2 for i in range(3))  # type: ignore[assignment]
        self._apply_clip()
        self._apply_styles()
        self.reset_camera()

    def clear(self) -> None:
        for b in self.blocks.values():
            for a in b.actors:
                self.renderer.RemoveActor(a)
        self.blocks.clear()
        self.selected = None
        self._locators = {}
        self._update_mark()

    def _actors(
        self, data: vtkDataSet, kind: str, dimension: int, color: tuple[float, float, float]
    ) -> tuple[vtkActor, list[vtkActor]]:
        extra: list[vtkActor] = []
        src = vtkGeometryFilter()
        src.SetInputData(data)
        src.Update()
        actor = _actor(src.GetOutput(), color)
        p = actor.GetProperty()
        if kind == "mesh":
            if self.dim == 2:
                p.SetRepresentationToWireframe()
                flat(actor)
            actor.PickableOff()
        elif kind == "zone" and dimension == 2:
            p.SetLineWidth(1.0)
            merged = vtkCleanPolyData()  # faces share their points, so only true boundaries are boundary edges
            merged.SetInputConnection(src.GetOutputPort())
            edges = vtkFeatureEdges()
            edges.SetInputConnection(merged.GetOutputPort())
            edges.BoundaryEdgesOn()
            edges.FeatureEdgesOn()
            edges.SetFeatureAngle(40.0)
            edges.NonManifoldEdgesOn()
            edges.ManifoldEdgesOff()
            edges.ColoringOff()
            edges.Update()
            outline = _actor(edges.GetOutput(), _toned(color, 0.45))
            outline.GetProperty().SetLineWidth(1.0)
            outline.PickableOff()
            extra.append(outline)
        elif kind == "zone":
            p.SetLineWidth(2.0)
        elif kind == "region":
            if dimension == self.dim:
                p.SetOpacity(0.22)
                outline = _actor(_boundary_edges(src.GetOutput()), color)
                outline.GetProperty().SetLineWidth(1.0)
                outline.PickableOff()
                extra.append(outline)
            p.SetLineWidth(2.0)
        elif kind == "probe":
            p.SetPointSize(7.0)
            p.RenderPointsAsSpheresOn()
            p.SetLineWidth(2.0)
            p.SetRepresentationToPoints() if dimension == 0 else None
        return actor, extra

    # Appearance

    def set_theme(self, theme: str) -> None:
        self.theme = theme
        t = THEMES[theme]
        self.renderer.SetBackground(*t["background"])
        for b in self.blocks.values():
            if b.kind == "mesh":
                b.color = t["outline"] if b.name == "domain" and self.dim == 3 else t["mesh"]
        self._apply_styles()
        self._update_mark()

    def set_block_data(self, name: str, data: vtkPolyData) -> None:
        """Draw block `name` from new cells, in place: its actors stay, so the browser's scene updates them."""
        b = self.blocks[name]
        cast(vtkPolyDataMapper, b.actor.GetMapper()).SetInputData(data)
        if b.kind == "region" and b.dimension == self.dim and b.extra:
            cast(vtkPolyDataMapper, b.extra[0].GetMapper()).SetInputData(_boundary_edges(data))
        b.cells = data.GetNumberOfCells()
        self._locators.pop(name, None)

    def select(self, name: str | None) -> None:
        self.selected = name if name in self.blocks else None
        self._apply_styles()

    def set_visible(self, name: str, visible: bool) -> None:
        self.blocks[name].visible = visible
        self._apply_styles()

    def isolate(self, names: Sequence[str]) -> None:
        keep = set(names)
        for b in self.blocks.values():
            if b.kind != "mesh":
                b.visible = b.name in keep
        self._apply_styles()

    def show_all(self) -> None:
        for b in self.blocks.values():
            b.visible = True
        self._apply_styles()

    def set_mesh_edges(self, on: bool) -> None:
        self.mesh_edges = on
        self._apply_styles()

    def triangles(self) -> int:
        """The polygons the visible blocks and the overlays draw: what a browser would hold to draw the scene."""
        shown = [a for b in self.blocks.values() if b.visible for a in b.actors] + self.overlays
        return sum(
            a.GetMapper().GetInput().GetNumberOfCells()
            for a in shown
            if a.GetMapper() is not None and a.GetMapper().GetInput() is not None
        )

    def set_xray(self, on: bool, opacity: float | None = None) -> None:
        self.xray = on
        if opacity is not None:
            self.xray_opacity = min(max(opacity, 0.02), 1.0)
        self._apply_styles()

    def _apply_styles(self) -> None:
        accent = THEMES[self.theme]["accent"]
        # In 3D the domain's box is drawn only while no zone surface is: their feature edges outline it.
        zones_shown = any(b.visible and b.kind == "zone" and b.dimension == 2 for b in self.blocks.values())
        for b in self.blocks.values():
            selected = b.name == self.selected
            translucent = self.xray and not selected and b.kind in self.XRAY_KINDS
            shown = b.visible and not (b.name == "domain" and self.dim == 3 and zones_shown)
            for a in b.actors:
                a.SetVisibility(shown)
            p = b.actor.GetProperty()
            b.drawn = themed(b.color, self.theme, MESH_CONTRAST if b.kind == "mesh" else LINE_CONTRAST)
            color = accent if selected else b.drawn
            p.SetColor(*color)
            base = 0.22 if b.kind == "region" and b.dimension == self.dim else 1.0
            p.SetOpacity(min(base, self.xray_opacity) if translucent else base)
            b.actor.SetPickable(b.kind != "mesh" and not translucent)
            if b.kind == "zone" and b.dimension == 2:
                p.SetEdgeVisibility(1 if self.mesh_edges else 0)
                p.SetEdgeColor(*_toned(color))  # over the surface: darker reads as structure on either theme
                for e in b.extra:  # feature edges: against the background at the silhouette too
                    e.GetProperty().SetColor(*themed(_toned(color, 0.45), self.theme))
                    e.GetProperty().SetOpacity(p.GetOpacity())
            elif b.kind == "region":
                for e in b.extra:
                    e.GetProperty().SetColor(*color)
            elif b.kind == "zone":
                p.SetLineWidth(3.0 if selected else 2.0)

    # Navigation

    def zoom_at(self, factor: float, nx: float, ny: float) -> None:
        """Zoom in by `factor` toward the focal-plane point under (nx, ny), 0..1 from the top left, which
        stays where it is on screen: the camera scaled by 1 / factor about that point."""
        if factor <= 0:
            return
        r = self.renderer
        cam = r.GetActiveCamera()
        w, h = self.window.GetSize()
        r.SetWorldPoint(*cam.GetFocalPoint(), 1.0)
        r.WorldToDisplay()
        depth = r.GetDisplayPoint()[2]
        r.SetDisplayPoint(nx * w, (1.0 - ny) * h, depth)
        r.DisplayToWorld()
        x, y, z, wh = r.GetWorldPoint()
        at = (x / wh, y / wh, z / wh)
        k = 1.0 - 1.0 / factor
        pos, foc = cam.GetPosition(), cam.GetFocalPoint()
        cam.SetPosition(*(p + (a - p) * k for p, a in zip(pos, at, strict=True)))
        cam.SetFocalPoint(*(f + (a - f) * k for f, a in zip(foc, at, strict=True)))
        if cam.GetParallelProjection():
            cam.SetParallelScale(cam.GetParallelScale() / factor)
        r.ResetCameraClippingRange()

    def reset_camera(self) -> None:
        """The view a scene opens in: the plane in 2D, else from the corner."""
        self.view_preset("-Z" if self.dim == 2 else "iso")

    def _resized(self, _obj: Any, _event: str) -> None:
        if self.framed and self.blocks:
            self.fit()

    def _interacted(self, _obj: Any, _event: str) -> None:
        self.framed = False

    def view_preset(self, name: str) -> None:
        """Look along the preset's axis. In 2D, ±Z face the plane from either side and keep the 2D lock; the
        others look at it from out of the plane, which the lock forbids, so they lift it."""
        direction, up = PRESETS[name]
        if self.dim == 2 and name not in PLANE_PRESETS:
            self.lock_2d = False
        self.look(direction, up)
        self.fit()

    def look(self, direction: Sequence[float], up: Sequence[float]) -> None:
        """Turn the camera about its focal point to look along `direction`, at the distance it has."""
        cam = self.renderer.GetActiveCamera()
        focal, distance = cam.GetFocalPoint(), cam.GetDistance()
        norm = math.hypot(*direction) or 1.0
        cam.SetPosition(*(f - distance * d / norm for f, d in zip(focal, direction, strict=True)))
        cam.SetViewUp(*up)
        self.renderer.ResetCameraClippingRange(*self.bounds)

    def fit(self, bounds: Sequence[float] | None = None, margin: float = 1.12, aspect: float = 0.0) -> None:
        """Frame the box `bounds` (default: the scene's) from where the camera looks now: the view direction
        and up stay, the camera only moves across and along its line of sight (and, in a parallel projection,
        scales) until the box's corners fill the view of width / height `aspect` (default: the window's) but
        for `margin`. The view angle stays too, so framing twice is framing once."""
        b = self.bounds if bounds is None else bounds
        self.framed = bounds is None
        if aspect > 0:
            self.aspect = aspect
        w, h = self.window.GetSize()
        aspect = self.aspect or w / max(h, 1)
        cam = self.renderer.GetActiveCamera()
        cam.OrthogonalizeViewUp()
        d, u = cam.GetDirectionOfProjection(), cam.GetViewUp()
        r = (d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0])
        corners = [(x, y, z) for x in b[0:2] for y in b[2:4] for z in b[4:6]]
        xs, ys, zs = ([sum(c * a for c, a in zip(p, axis, strict=True)) for p in corners] for axis in (r, u, d))
        depth = (min(zs) + max(zs)) / 2
        tan = math.tan(math.radians(cam.GetViewAngle()) / 2)
        if cam.GetParallelProjection():
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            scale = margin * max((max(ys) - min(ys)) / 2, (max(xs) - min(xs)) / (2 * aspect)) or 1.0
            cz = min(zs) - max(scale / tan, max(zs) - min(zs))
        else:
            # A corner (x, y, z) is in view from (cx, cy, cz) while |y - cy| <= t (z - cz), and so across.
            tv, th = tan / margin, tan * aspect / margin
            lo_y, hi_y = (
                max(y - tv * z for y, z in zip(ys, zs, strict=True)),
                min(y + tv * z for y, z in zip(ys, zs, strict=True)),
            )
            lo_x, hi_x = (
                max(x - th * z for x, z in zip(xs, zs, strict=True)),
                min(x + th * z for x, z in zip(xs, zs, strict=True)),
            )
            cz = min((hi_y - lo_y) / (2 * tv), (hi_x - lo_x) / (2 * th))
            if depth - cz <= 0:  # a point: one unit away
                cz = depth - 1.0
            cx = _centered(xs, zs, cz, lo_x + th * cz, hi_x - th * cz)
            cy = _centered(ys, zs, cz, lo_y + tv * cz, hi_y - tv * cz)
            scale = (depth - cz) * tan
        at = [cx * r[i] + cy * u[i] for i in range(3)]
        cam.SetPosition(*(at[i] + cz * d[i] for i in range(3)))
        cam.SetFocalPoint(*(at[i] + depth * d[i] for i in range(3)))
        cam.SetParallelScale(scale)
        self.renderer.ResetCameraClippingRange(*self.bounds)

    def focus(self, name: str, margin: float = 1.5) -> bool:
        """Frame one block, keeping the view direction: a small surface enclosed by large ones (a sphere
        in its far field) fills the view instead of a few pixels of it."""
        block = self.blocks.get(name)
        if block is None:
            return False
        self.frame_bounds(block.actor.GetBounds(), margin)
        return True

    def frame_bounds(self, b: Sequence[float], margin: float = 1.5) -> None:
        """Frame the cube about the box `b` (xmin, xmax, ymin, ...) enlarged by `margin`, keeping the view
        direction."""
        center = [(b[2 * i] + b[2 * i + 1]) / 2 for i in range(3)]
        half = max(margin * (b[2 * i + 1] - b[2 * i]) / 2 for i in range(3)) or 1.0
        self.fit([c + s * half for c in center for s in (-1, 1)], margin=1.0)

    def set_parallel(self, on: bool) -> None:
        """The other projection, at the size the focal plane is drawn now."""
        cam = self.renderer.GetActiveCamera()
        if bool(cam.GetParallelProjection()) == on:
            return
        tan = math.tan(math.radians(cam.GetViewAngle()) / 2)
        if on:
            cam.SetParallelScale(cam.GetDistance() * tan)
        else:
            d, focal = cam.GetDirectionOfProjection(), cam.GetFocalPoint()
            distance = cam.GetParallelScale() / tan
            cam.SetPosition(*(f - distance * c for f, c in zip(focal, d, strict=True)))
        cam.SetParallelProjection(1 if on else 0)
        self.renderer.ResetCameraClippingRange(*self.bounds)

    def set_lock_2d(self, on: bool) -> None:
        """Drags pan; a 2D case is turned back to its plane, where it is, at the size it is drawn."""
        self.lock_2d = on
        if on and self.dim == 2:
            self.look((0, 0, -1), (0, 1, 0))

    def _left_press(self, obj: Any, _event: str) -> None:
        # In 2D (rotation locked to the plane) and with shift, a drag pans.
        if self.lock_2d or self.interactor.GetShiftKey():
            self.style.FindPokedRenderer(*self.interactor.GetEventPosition())
            self.style.StartPan()
            self._panning = True
            obj.SetAbortFlag(1) if hasattr(obj, "SetAbortFlag") else None
            return
        self.style.OnLeftButtonDown()

    def _left_release(self, _obj: Any, _event: str) -> None:
        if self._panning:
            self.style.EndPan()
            self._panning = False
            return
        self.style.OnLeftButtonUp()

    # The section tool

    def set_clip(
        self,
        enabled: bool,
        origin: Sequence[float] | None = None,
        normal: Sequence[float] | None = None,
        flipped: bool | None = None,
        snap: bool = False,
    ) -> None:
        self.clip_enabled = enabled
        if origin is not None:
            self.clip_origin = (float(origin[0]), float(origin[1]), float(origin[2]))
        if normal is not None and self.dim == 2 and not (normal[0] or normal[1]):
            normal = None  # the plane of the page cuts no 2D case: the plane stays as it was
        if normal is not None:
            n = snap_normal(normal) if snap else snap_normal(normal, 0.0)
            self.clip_normal = n
        if flipped is not None:
            self.keep_flipped = flipped
        self._apply_clip()

    def _apply_clip(self) -> None:
        sign = -1.0 if self.keep_flipped else 1.0
        self.plane.SetOrigin(*self.clip_origin)
        self.plane.SetNormal(*(sign * c for c in self.clip_normal))
        self.clipped = []
        for b in self.blocks.values():
            for a in b.actors:
                mapper = a.GetMapper()
                mapper.RemoveAllClippingPlanes()
                if (
                    self.clip_enabled
                    and not (b.kind == "mesh" and self.dim == 3)
                    and b.kind not in self.UNCLIPPED_KINDS
                ):
                    mapper.AddClippingPlane(self.plane)
                    self.clipped.append(a)
        for a in self.overlays:
            a.GetMapper().RemoveAllClippingPlanes()
            if self.clip_enabled:
                a.GetMapper().AddClippingPlane(self.plane)
                self.clipped.append(a)
        if self._widget is not None:
            rep = self._plane_rep
            rep.SetOrigin(*self.clip_origin)
            rep.SetNormal(*self.clip_normal)
            self._widget.SetEnabled(1 if self.clip_enabled and self.widget_shown else 0)
        self._update_mark()

    def section_extent(self) -> tuple[float, float]:
        """How far the scene reaches along the plane's normal, as positions along it (for an axis, the
        coordinate): what an offset of the plane runs over."""
        b, n = self.bounds, self.clip_normal
        along = [x * n[0] + y * n[1] + z * n[2] for x in b[0:2] for y in b[2:4] for z in b[4:6]]
        return min(along), max(along)

    def section_offset(self) -> float:
        """Where the plane is along its normal."""
        return sum(o * c for o, c in zip(self.clip_origin, self.clip_normal, strict=True))

    def mark_poly(self, bounds: Sequence[float]) -> vtkPolyData:
        """The plane's mark on a box: its outline where it crosses the box (its line across a 2D one) and its
        normal from the origin, a short way toward the side kept."""
        b = list(bounds)
        size = math.dist(b[0::2], b[1::2]) or 1.0
        if self.dim == 2 or b[5] - b[4] < 1e-9 * size:
            b[4], b[5] = b[4] - 1e-3 * size, b[5] + 1e-3 * size  # a slab the plane crosses
        box = vtkCubeSource()
        box.SetBounds(*b)
        outline = vtkCutter()
        outline.SetCutFunction(self.plane)
        outline.SetInputConnection(box.GetOutputPort())
        arrow = vtkLineSource()
        arrow.SetPoint1(*self.clip_origin)
        arrow.SetPoint2(*(o + 0.12 * size * n for o, n in zip(self.clip_origin, self.plane.GetNormal(), strict=True)))
        both = vtkAppendPolyData()
        both.AddInputConnection(outline.GetOutputPort())
        both.AddInputConnection(arrow.GetOutputPort())
        both.Update()
        out = vtkPolyData()
        out.ShallowCopy(both.GetOutput())
        return out

    def _update_mark(self) -> None:
        """The plane's mark, in the scene while the plane cuts and the mark is wanted: the same actor, redrawn,
        so a view that draws the scene itself only gets new points."""
        shown = self.clip_enabled and self.section_mark and bool(self.blocks)
        if not shown:
            if self._mark is not None:
                self.renderer.RemoveActor(self._mark)
                self._mark = None
            return
        poly = self.mark_poly(self.bounds)
        if self._mark is None:
            self._mark = _actor(poly, THEMES[self.theme]["accent"])
            self._mark.GetMapper().ScalarVisibilityOff()
            self._mark.GetProperty().SetLineWidth(3.0)
            flat(self._mark)
            self._mark.PickableOff()
            self.renderer.AddActor(self._mark)
        else:
            mapper = self._mark.GetMapper()
            assert isinstance(mapper, vtkPolyDataMapper)
            mapper.SetInputData(poly)
        self._mark.GetProperty().SetColor(*THEMES[self.theme]["accent"])

    @property
    def overlays(self) -> list[vtkActor]:
        return [a for actors in self.overlay_layers.values() for a in actors]

    def set_overlay(self, layer: str, actors: Sequence[vtkActor]) -> None:
        """Replace a layer's overlay actors: added to the renderer, clipped by the section tool."""
        for a in self.overlay_layers.pop(layer, []):
            self.renderer.RemoveActor(a)
        if actors:
            self.overlay_layers[layer] = list(actors)
            for a in actors:
                self.renderer.AddActor(a)
        self._apply_clip()

    def enable_plane_widget(self) -> None:
        """The plane widget: drag the plane to move it, the normal's tip to tilt it."""
        if self._widget is not None:
            return
        rep = vtkImplicitPlaneRepresentation()
        rep.SetPlaceFactor(1.05)
        rep.PlaceWidget(list(self.bounds))
        rep.SetOrigin(*self.clip_origin)
        rep.SetNormal(*self.clip_normal)
        rep.DrawPlaneOff()
        rep.OutlineTranslationOff()
        rep.SetScaleEnabled(0)
        rep.GetNormalProperty().SetColor(*THEMES[self.theme]["accent"])
        widget = vtkImplicitPlaneWidget2()
        widget.SetInteractor(self.interactor)
        widget.SetRepresentation(rep)
        widget.AddObserver(vtkCommand.InteractionEvent, self._widget_moved)
        self._widget = widget
        self._plane_rep = rep
        widget.SetEnabled(1 if self.clip_enabled else 0)

    def _widget_moved(self, widget: vtkImplicitPlaneWidget2, _event: str) -> None:
        rep = self._plane_rep
        ox, oy, oz = rep.GetOrigin()
        nx, ny, nz = rep.GetNormal()
        self.clip_origin = (ox, oy, oz)
        self.clip_normal = (nx, ny, nz)
        self._apply_clip()
        if self.on_clip_moved:
            self.on_clip_moved()

    # Picking

    def pick(self, x: float, y: float) -> dict[str, Any] | None:
        """The block under display position (x, y), y up from the bottom: the first visible surface,
        ignoring clipped and x-rayed geometry."""
        self.window.Render()
        name = self._hardware_pick(int(x), int(y))
        if name is None and self.xray:
            # Nothing opaque under the cursor: the x-rayed zones are pickable too.
            translucent = [
                b.actor
                for b in self.blocks.values()
                if b.visible and b.kind in self.XRAY_KINDS and not b.actor.GetPickable()
            ]
            for a in translucent:
                a.PickableOn()
            try:
                name = self._hardware_pick(int(x), int(y))
            finally:
                for a in translucent:
                    a.PickableOff()
        picker = vtkCellPicker()
        picker.SetTolerance(0.005)
        if name is not None:
            picker.PickFromListOn()
            picker.AddPickList(self.blocks[name].actor)
        if not picker.Pick(x, y, 0, self.renderer):
            if name is None:
                return None
            return {"name": name, "cell": None, "point": None}
        if name is None:
            actor = picker.GetActor()
            name = next((b.name for b in self.blocks.values() if b.actor is actor), None)
            if name is None:
                return None
        return {"name": name, "cell": int(picker.GetCellId()), "point": [float(c) for c in picker.GetPickPosition()]}

    def pick_world(self, point: Sequence[float], ref: str = "") -> dict[str, Any] | None:
        """The block and face at a world position the browser picked (its actor's reference `ref`, if it
        sent one): the nearest visible cell, so it agrees with what was drawn."""
        candidates = [b for b in self.blocks.values() if b.visible and b.kind != "mesh"]
        if ref:
            candidates = [b for b in candidates if any(_ref(a) == ref for a in b.actors)] or candidates
        best: tuple[float, str, int] | None = None
        for b in candidates:
            loc = self._locators.get(b.name)
            if loc is None:
                data = b.actor.GetMapper().GetInput()
                if data is None or data.GetNumberOfCells() == 0:
                    continue
                loc = vtkStaticCellLocator()
                loc.SetDataSet(data)
                loc.BuildLocator()
                self._locators[b.name] = loc
            closest = [0.0, 0.0, 0.0]
            cell: Any = reference(0)
            dist2: Any = reference(0.0)
            loc.FindClosestPoint(list(point), closest, cell, reference(0), dist2)  # type: ignore[call-overload]
            d2, cid = float(dist2), int(cell)
            if best is None or d2 < best[0]:
                best = (d2, b.name, cid)
        if best is None:
            return None
        return {"name": best[1], "cell": best[2], "point": [float(c) for c in point]}

    def _hardware_pick(self, x: int, y: int) -> str | None:
        selector = vtkHardwareSelector()
        selector.SetRenderer(self.renderer)
        selector.SetFieldAssociation(vtkDataObject.FIELD_ASSOCIATION_CELLS)
        selector.SetArea(x, y, x, y)
        selection = selector.Select()
        if selection is None or selection.GetNumberOfNodes() == 0:
            return None
        prop = selection.GetNode(0).GetProperties().Get(vtkSelectionNode.PROP())
        return next((b.name for b in self.blocks.values() if b.actor is prop), None)

    def display_to_world(self, x: float, y: float) -> tuple[float, float, float]:
        """A display position on the focal plane, for the cursor readout."""
        self.renderer.SetDisplayPoint(x, y, 0)
        cam = self.renderer.GetActiveCamera()
        self.renderer.SetWorldPoint(*cam.GetFocalPoint(), 1.0)
        self.renderer.WorldToDisplay()
        z = self.renderer.GetDisplayPoint()[2]
        self.renderer.SetDisplayPoint(x, y, z)
        self.renderer.DisplayToWorld()
        w = self.renderer.GetWorldPoint()
        return (w[0] / w[3], w[1] / w[3], w[2] / w[3]) if w[3] else (w[0], w[1], w[2])

    def render_png(self, path: Path, size: tuple[int, int] | None = None) -> None:
        """The view as a PNG; given `size`, of exactly that many pixels, the window's own size put back after."""
        if size is None:
            path.write_bytes(self.render_image("png"))
            return
        was, framed = self.window.GetSize(), self.framed
        self.framed = False  # another size here is not the view resized
        try:
            self.window.SetSize(max(int(size[0]), 16), max(int(size[1]), 16))
            path.write_bytes(self.render_image("png"))
        finally:
            self.window.SetSize(*was)
            self.framed = framed

    def render_image(self, fmt: str = "png", quality: int = 90) -> bytes:
        """The window's current image, encoded in memory: `png`, or `jpeg` at `quality`."""
        from vtkmodules.vtkIOImage import vtkJPEGWriter, vtkPNGWriter
        from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter

        self.window.Render()
        grab = vtkWindowToImageFilter()
        grab.SetInput(self.window)
        grab.ReadFrontBufferOff()
        grab.Update()
        writer: vtkPNGWriter | vtkJPEGWriter
        if fmt == "jpeg":
            writer = vtkJPEGWriter()
            writer.SetQuality(quality)
        else:
            writer = vtkPNGWriter()
        writer.WriteToMemoryOn()
        writer.SetInputConnection(grab.GetOutputPort())
        writer.Write()
        return bytes(memoryview(writer.GetResult()))


def _centered(xs: Sequence[float], zs: Sequence[float], cz: float, lo: float, hi: float) -> float:
    """Where across (between `lo` and `hi`, from where every point is in view) a perspective camera at depth
    `cz` sees the points centered: as far left of it as right, on screen."""
    for _ in range(60):
        c = (lo + hi) / 2
        slopes = [(x - c) / (z - cz) for x, z in zip(xs, zs, strict=True) if z > cz]
        if not slopes or hi - lo < 1e-12 * max(1.0, abs(c)):
            break
        lo, hi = (c, hi) if max(slopes) + min(slopes) > 0 else (lo, c)
    return (lo + hi) / 2


def read_vtm(path: Path) -> list[tuple[str, Path]]:
    """The named blocks of a `.vtm` file, read one by one so volume blocks can be left out."""
    root = ET.parse(path).getroot()
    out = []
    for ds in root.iter("DataSet"):
        name, file = ds.get("name"), ds.get("file")
        if name and file:
            out.append((name, path.parent / file))
    return out


def _read_block(path: Path) -> vtkDataSet | None:
    reader = vtkXMLGenericDataObjectReader()
    reader.SetFileName(str(path))
    reader.Update()
    out = reader.GetOutput()
    return out if isinstance(out, vtkDataSet) else None


def _ref(obj: Any) -> str:
    """How trame's scene serializer names an object to the browser (its `remoteId`)."""
    return reference_id(obj)


def _toned(color: Sequence[float], amount: float = 0.22) -> tuple[float, float, float]:
    """A line color: the surface's, darkened by `amount`, so edges read as structure, not as noise."""
    return (color[0] * (1 - amount), color[1] * (1 - amount), color[2] * (1 - amount))


def _actor(poly: Any, color: tuple[float, float, float]) -> vtkActor:
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(poly)
    mapper.ScalarVisibilityOff()
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*color)
    if isinstance(poly, vtkPolyData) and poly.GetNumberOfPolys() == 0 and poly.GetNumberOfStrips() == 0:
        flat(actor)
    return actor


def flat(actor: vtkActor) -> None:
    """Lines and points in exactly their color: unlit, as the browser (vtk.js) lights normal-less lines as if
    facing the light, which washed them out (dark mesh lines drawn pale, orange zones yellow)."""
    actor.GetProperty().LightingOff()


def _topological_dimension(data: vtkDataSet) -> int:
    if data.GetNumberOfCells() == 0:
        return 0
    return max(data.GetCell(i).GetCellDimension() for i in range(min(data.GetNumberOfCells(), 64)))
