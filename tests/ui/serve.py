"""Serve the app on a captured case, without a mallard build, for the browser tests:
`python -m tests.ui.serve PROJECT PORT`."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

MARK = (1.0, 0.0, 1.0)


def address(obj: Any) -> str:
    """Where a VTK object lives."""
    return str(obj.__this__)[1:17]


def reuse_address(app: Any) -> dict[str, Any]:
    """Draw marks at `old`, then in one update replace them by one at `new` whose actor is built where one of
    their properties was, until the heap gives such an address (`reused`): what it does, now and then, to the
    previews rebuilt after a release, as actors, properties and lookup tables share a size class."""
    from vtkmodules.vtkFiltersSources import vtkSphereSource
    from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkProperty

    scene = app.scene
    b = scene.bounds
    old_at = [b[0] + 0.3 * (b[1] - b[0]), b[2] + 0.7 * (b[3] - b[2]), 0.0]
    new_at = [b[0] + 0.7 * (b[1] - b[0]), b[2] + 0.7 * (b[3] - b[2]), 0.0]

    def mark(actor: vtkActor, at: list[float]) -> vtkActor:
        sphere = vtkSphereSource()
        sphere.SetCenter(*at)
        sphere.SetRadius(0.02 * (b[1] - b[0]))
        sphere.Update()
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(sphere.GetOutput())
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*MARK)
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        return actor

    for _ in range(60):
        marks = [mark(vtkActor(), old_at) for _ in range(8)]
        scene.set_overlay("marks", marks)
        app._view.update_geometry()
        freed = {address(a.GetProperty()) for a in marks}
        for a in marks:
            a.SetProperty(vtkProperty())  # the old one is freed alone: its block joins no neighbor's
        # A freed block is handed out again at once, on the processor core it was allocated on.
        spare = [vtkActor() for _ in range(16)]
        reused = [a for a in spare if address(a) in freed]
        scene.set_overlay("marks", [mark(reused[0] if reused else spare[0], new_at)])
        del marks
        app._update_view()
        if reused:
            break
    return {"reused": bool(reused), "old": old_at, "new": new_at}


def mark_at(app: Any, at: list[float], radius: float) -> None:
    """Draw a ball in the mark's color at a world point of the case view."""
    from vtkmodules.vtkFiltersSources import vtkSphereSource
    from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

    sphere = vtkSphereSource()
    sphere.SetCenter(*at)
    sphere.SetRadius(radius)
    sphere.SetThetaResolution(24)
    sphere.SetPhiResolution(24)
    sphere.Update()
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(sphere.GetOutput())
    actor = vtkActor()
    actor.SetMapper(mapper)
    prop = actor.GetProperty()
    prop.SetColor(*MARK)
    prop.SetAmbient(1.0)
    prop.SetDiffuse(0.0)
    app.scene.set_overlay("marks", [actor])
    app._update_view()


def datasets(app: Any) -> list[tuple[int, int, int]]:
    """The datasets of the case view's scene now, as (points, polygons, lines), those drawing nothing left out."""
    out = []
    actors = app.scene.renderer.GetActors()
    for i in range(actors.GetNumberOfItems()):
        actor = actors.GetItemAsObject(i)
        mapper = actor.GetMapper()
        data = mapper.GetInput() if mapper is not None and actor.GetVisibility() else None
        if data is not None and hasattr(data, "GetPolys") and data.GetNumberOfPoints():
            out.append((int(data.GetNumberOfPoints()), int(data.GetNumberOfPolys()), int(data.GetNumberOfLines())))
    return sorted(out)


def record_datasets(app: Any) -> set[tuple[int, int, int]]:
    """Every dataset the scene has held when it was sent to the browser: what the browser may draw. A dataset
    of the browser's with counts never sent is two updates mixed."""
    sent: set[tuple[int, int, int]] = set()
    send = app._view.update_geometry

    def update_geometry(*args: Any, **kwargs: Any) -> Any:
        sent.update(datasets(app))
        return send(*args, **kwargs)

    app._view.update_geometry = update_geometry
    return sent


def case_image(app: Any, width: int, height: int) -> str:
    """The case view as this process renders it, from the camera it has, `width` x `height`: a PNG, base64."""
    import base64
    import tempfile
    from pathlib import Path as P

    with tempfile.TemporaryDirectory() as tmp:
        app.scene.render_png(P(tmp) / "case.png", (int(width), int(height)))
        return base64.b64encode((P(tmp) / "case.png").read_bytes()).decode("ascii")


def main() -> None:
    project, port = Path(sys.argv[1]), int(sys.argv[2])
    sys.argv = sys.argv[:1]
    from trame.app import get_server

    from mallard_gui.session import Session
    from mallard_gui.ui.app import App
    from tests.conftest import FakeMallard, RequestsMallard

    server = get_server(client_type="vue3")
    check = os.environ.get("MALLARD_GUI_TEST_CHECK") or "cylinder_check.json"
    kind = RequestsMallard if os.environ.get("MALLARD_GUI_TEST_REQUESTS") else FakeMallard
    fake = kind(check=check, view=not os.environ.get("MALLARD_GUI_TEST_NO_VIEW"))
    if os.environ.get("MALLARD_GUI_TEST_KEYS"):  # catalog entries in place of the captured ones, as JSON
        fake.key_entries = json.loads(os.environ["MALLARD_GUI_TEST_KEYS"])
    if os.environ.get("FAKE_MALLARD_EXE"):  # post jobs run this executable
        fake.executable = Path(os.environ["FAKE_MALLARD_EXE"])
    fake.restart_check = os.environ.get("MALLARD_GUI_TEST_RESTART_CHECK", "")
    session = Session(project, fake)  # type: ignore[arg-type]
    app = App(session, server)
    server.controller.trigger("test_reuse_address")(lambda: reuse_address(app))

    def use_check(name: str) -> None:
        """Answer the next checks with another captured report (and its case view), and check now."""
        fake.check_fixture = name
        app._schedule_check()

    def images(on: bool) -> None:
        """The case view as the server's images (as for a case over the browser's triangle budget), or back."""
        from mallard_gui.ui import app as app_module

        app_module.GEOMETRY_TRIANGLES = 0 if on else 2_000_000
        app._update_view()

    server.controller.trigger("test_images")(images)
    server.controller.trigger("test_case_image")(lambda w, h: case_image(app, w, h))
    server.controller.trigger("test_use_check")(use_check)
    server.controller.trigger("test_initial_states")(lambda: getattr(fake, "initials", 0))
    sent = record_datasets(app)
    server.controller.trigger("test_datasets_sent")(lambda: sorted(sent))
    server.controller.trigger("test_datasets_now")(lambda: datasets(app))
    server.controller.trigger("test_mark")(lambda at, radius: mark_at(app, at, radius))
    # The results viewer's own camera, for the views it renders as images.
    server.controller.trigger("test_results_camera")(lambda: getattr(app.results_page.viewer, "call")("get_camera"))  # noqa: B009
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.start(host="127.0.0.1", port=port, open_browser=False, show_connection_info=False, timeout=0)
    finally:
        session.close()


if __name__ == "__main__":
    main()
