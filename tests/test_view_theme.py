"""Every line and point the case view draws stands out from the background in both themes, switched at run
time: blocks (mesh, zones, regions, probes) and case setup's overlays (previews, markers, inflow arrows)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from trame.app import get_server
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkCommonDataModel import vtkPolyData

from mallard_gui.session import Session
from mallard_gui.ui.app import App
from mallard_gui.view.scene import LINE_CONTRAST, MESH_CONTRAST, contrast

from .conftest import FakeMallard

_names = itertools.count()


@pytest.fixture
def app(cylinder_project: Path) -> Iterator[App]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    a = App(Session(cylinder_project, FakeMallard(view=True)), get_server(f"theme-{next(_names)}", client_type="vue3"))  # type: ignore[arg-type]
    a.ctrl.view_update = lambda: None
    loop.run_until_complete(a.run_check())
    yield a
    loop.close()
    asyncio.set_event_loop(None)


def lines_and_points(app: App) -> list[tuple[str, Any]]:
    """The visible, single-colored actors that draw only lines or points (or a wireframe): what a background
    swallows."""
    out: list[tuple[str, Any]] = []
    actors = app.scene.renderer.GetActors()
    actors.InitTraversal()
    for _ in range(actors.GetNumberOfItems()):
        a = actors.GetNextActor()
        poly = a.GetMapper().GetInput() if a.GetMapper() else None
        if not a.GetVisibility() or not isinstance(poly, vtkPolyData) or a.GetMapper().GetScalarVisibility():
            continue
        wireframe = a.GetProperty().GetRepresentation() == 1
        if poly.GetNumberOfCells() and (wireframe or not (poly.GetNumberOfPolys() or poly.GetNumberOfStrips())):
            out.append((next((b.name for b in app.scene.blocks.values() if a in b.actors), "overlay"), a))
    return out


@pytest.mark.parametrize("order", [("light", "dark"), ("dark", "light")])
def test_lines_and_points_read_on_either_background(app: App, order: tuple[str, str]) -> None:
    app.toggle_view("mesh")
    app.select("section:regions")  # region and probe previews
    app.setup.toggle_inflow()
    for theme in (*order, order[0]):  # and back: the switch at run time recolors everything
        app.set_theme(theme)
        bg = app.scene.renderer.GetBackground()
        drawn = lines_and_points(app)
        assert {"domain", "farfield", "overlay"} <= {name for name, _ in drawn}
        for name, a in drawn:
            p = a.GetProperty()
            need = MESH_CONTRAST if name == "domain" else LINE_CONTRAST
            assert contrast(p.GetColor(), bg) >= need - 1e-6, f"{theme}: {name} {p.GetColor()} on {bg}"
            assert not p.GetLighting(), f"{name}: lit lines are washed out in the browser"
        arrows = app.setup.overlay.arrows
        assert arrows
        for a in arrows:
            rgb = vtk_to_numpy(a.GetMapper().GetInput().GetPointData().GetScalars()) / 255
            assert min(contrast(c, bg) for c in rgb) >= LINE_CONTRAST - 0.05, f"{theme}: inflow arrows"
            assert not a.GetProperty().GetLighting()  # drawn in the legend's colors
        legend = {b["name"]: b["color"] for b in app.state.blocks}
        yellow = legend['probes[name = "wake_point"]']
        assert (yellow == "rgb(240,228,66)") == (theme == "dark")  # Okabe-Ito yellow, darkened on white
