from __future__ import annotations

from typing import Any

import pytest

from mallard_gui.setup.geometry import Handle, Vec, preview
from mallard_gui.setup.inflow import Inflow
from mallard_gui.view.overlay import SetupOverlay
from mallard_gui.view.scene import CaseScene

from .conftest import FIXTURES, fixture_json

RECT = {"type": "rectangle", "center": [3.0, 0.0], "size": [6.0, 3.0]}


@pytest.fixture
def scene() -> CaseScene:
    s = CaseScene(offscreen=True)
    s.load(FIXTURES / "cylinder_view" / "case.vtm", fixture_json("cylinder_check.json"))
    s.window.SetSize(600, 400)
    s.focus("cylinder", margin=8.0)
    return s


def display_of(scene: CaseScene, p: Vec) -> tuple[int, int]:
    r = scene.renderer
    r.SetWorldPoint(p[0], p[1], p[2], 1.0)
    r.WorldToDisplay()
    x, y, _ = r.GetDisplayPoint()
    return round(x), round(y)


def mouse(scene: CaseScene, event: str, xy: tuple[int, int]) -> None:
    scene.interactor.SetEventInformation(xy[0], xy[1])
    scene.interactor.InvokeEvent(event)


def test_selected_preview_gets_its_handles_and_others_none(scene: CaseScene) -> None:
    ov = SetupOverlay(scene)
    line = preview({"type": "line", "start": [1, -1], "end": [1, 1], "n_points": 5}, 2)
    ov.set_previews([("region:a", "region", preview(RECT, 2)), ("el:probes[3]", "probe", line)], "region:a")
    assert [(p["kind"], p["text"]) for p in ov.pins() if p["kind"] == "handle"] == [
        ("handle", "center"),
        ("handle", "size"),
    ]
    assert set(scene.overlays) >= {a for i in ov.items.values() for a in i.actors}
    ov.set_previews([("region:a", "region", preview(RECT, 2))], None)
    assert ov.handles == []


def test_a_release_that_rebuilds_every_handle_is_safe_and_the_server_view_never_drags(scene: CaseScene) -> None:
    """The page drags handles; a release rebuilds the previews, as case setup does after writing the key.
    A VTK handle widget freed by that rebuild inside its own EndSelectAction crashed the server."""
    ov = SetupOverlay(scene)
    shapes = [("region:a", "region", preview(RECT, 2))]
    ov.set_previews(shapes, "region:a")
    released: list[tuple[Handle, Vec]] = []
    dragged: list[Vec] = []
    ov.on_drag = lambda h, p: dragged.append(p)

    def rebuild(h: Handle, p: Vec) -> None:
        released.append((h, p))
        ov.set_previews(shapes, "region:a")

    ov.on_release = rebuild
    scene.lock_2d = True
    scene.window.Render()
    start, end = display_of(scene, (3.0, 0.0, 0.0)), display_of(scene, (4.0, 1.0, 0.0))
    for event, xy in [("MouseMoveEvent", start), ("LeftButtonPressEvent", start), ("MouseMoveEvent", end)]:
        mouse(scene, event, xy)
    mouse(scene, "LeftButtonReleaseEvent", end)
    assert released == [] and dragged == []  # a press on a handle in a server image pans; it drags nothing
    ov.move_handle(0, (3.5, 0.5, 0.2))
    ov.release_handle(0, (4.0, 1.0, 0.3))
    ov.drag(0, (2.0, 0.0, 0.0))
    assert dragged == [(3.5, 0.5, 0.0), (2.0, 0.0, 0.0)]
    assert [(h.key, p) for h, p in released] == [("center", (4.0, 1.0, 0.0)), ("center", (2.0, 0.0, 0.0))]
    assert ov.pins()[0]["p"] == [3.0, 0.0, 0.0]  # rebuilt from the shape


def test_section_tool_clips_the_overlays(scene: CaseScene) -> None:
    ov = SetupOverlay(scene)
    ov.set_previews([("region:a", "region", preview(RECT, 2))], None)
    ov.set_inflow([Inflow("far", [(-25.0, 0.0, 0.0)], [(0.2, 0.0, 0.0)], "uniform")])
    scene.set_clip(True, (0, 0, 0), (1, 0, 0))
    actors: list[Any] = scene.overlays
    assert actors and all(a.GetMapper().GetNumberOfClippingPlanes() == 1 for a in actors)
    scene.set_clip(False)
    assert all(a.GetMapper().GetNumberOfClippingPlanes() == 0 for a in actors)


def test_inflow_arrows_scale_with_speed_and_unknown_ones_have_unit_length(scene: CaseScene) -> None:
    ov = SetupOverlay(scene)
    fast = Inflow("a", [(0.0, 0.0, 0.0)], [(2.0, 0.0, 0.0)], "uniform")
    slow = Inflow("b", [(0.0, 5.0, 0.0)], [(1.0, 0.0, 0.0)], "uniform")
    length = ov.set_inflow([fast, slow])
    widths = [a.GetBounds()[1] - a.GetBounds()[0] for a in ov.arrows]
    assert widths[0] == pytest.approx(length, rel=0.02)
    assert widths[1] == pytest.approx(length / 2, rel=0.02)
    ov.set_inflow([Inflow("c", [(0.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)], "profile", known=False)])
    assert ov.arrows[0].GetMapper().GetScalarVisibility() == 0


@pytest.mark.parametrize("parallel", [False, True])
def test_zoom_keeps_the_point_under_the_cursor_and_scales_continuously(scene: CaseScene, parallel: bool) -> None:
    cam = scene.renderer.GetActiveCamera()
    cam.SetParallelProjection(parallel)
    scene.window.Render()
    w, h = scene.window.GetSize()
    nx, ny = 0.8, 0.25
    r = scene.renderer
    r.SetWorldPoint(*cam.GetFocalPoint(), 1.0)
    r.WorldToDisplay()
    r.SetDisplayPoint(nx * w, (1 - ny) * h, r.GetDisplayPoint()[2])
    r.DisplayToWorld()
    x, y, z, wh = r.GetWorldPoint()
    under = (x / wh, y / wh, z / wh)
    span = cam.GetParallelScale() if parallel else cam.GetDistance()
    scene.zoom_at(1.08, nx, ny)
    after = display_of(scene, under)
    assert after == pytest.approx((nx * w, (1 - ny) * h), abs=1.0)
    assert (cam.GetParallelScale() if parallel else cam.GetDistance()) == pytest.approx(span / 1.08)


def test_the_selected_body_moves_by_its_point_handle(scene: CaseScene) -> None:
    ov = SetupOverlay(scene)
    circle = preview({"type": "circle", "center": [1, 0], "radius": 0.5, "n_points": 16}, 2)
    line = preview({"type": "line", "start": [1, -1], "end": [1, 1], "n_points": 5}, 2)
    shapes = [("region:a", "region", preview(RECT, 2)), ("el:p", "probe", circle), ("el:l", "probe", line)]
    ov.set_previews(shapes, "region:a")
    body = ov.body(limit=8)
    assert body is not None and body["area"] and body["handle"] == 0  # the center, not the size
    xs = [p[0] for p in body["points"]]
    assert len(body["points"]) <= 8 and min(xs) == pytest.approx(0.0) and max(xs) == pytest.approx(6.0)
    ov.set_previews(shapes, "el:p")
    body = ov.body()
    assert body is not None and not body["area"] and body["segments"]
    assert ov.handles[body["handle"]][0].role == "point"
    ov.set_previews(shapes, "el:l")
    assert ov.body() is None  # its two ends: no one handle moves it whole
    ov.set_previews(shapes, None)
    assert ov.body() is None
