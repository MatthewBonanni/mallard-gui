"""The scene geometry mode hands to vtk.js: the decimated, already colored surfaces a viewer sent, held in a
render window that is never drawn here (trame's local view only serializes it to the browser)."""

from __future__ import annotations

from typing import Any

from vtkmodules.vtkRenderingCore import vtkRenderer, vtkRenderWindow

from ..macos import hide_from_dock
from ..viewer.geometry import actor_of

import vtkmodules.vtkRenderingOpenGL2  # noqa: F401  isort: skip

# Above this round trip, interaction is smoother in the browser than as images (the design's ~100 ms).
AUTO_RTT_MS = 100.0


class GeometryScene:
    def __init__(self) -> None:
        hide_from_dock()
        self.renderer = vtkRenderer()
        self.window = vtkRenderWindow()
        self.window.SetOffScreenRendering(1)
        self.window.AddRenderer(self.renderer)
        self.triangles = 0

    def load(self, payload: dict[str, Any]) -> None:
        self.renderer.RemoveAllViewProps()
        for block in payload["blocks"]:
            self.renderer.AddActor(actor_of(block))
        self.triangles = int(payload.get("triangles", 0))
        if payload.get("background"):
            self.renderer.SetBackground(*payload["background"])
        self.set_camera(payload.get("camera") or {})

    def set_camera(self, cam: dict[str, Any]) -> None:
        if not cam:
            return
        c = self.renderer.GetActiveCamera()
        c.SetPosition(*cam["position"])
        c.SetFocalPoint(*cam["focal_point"])
        c.SetViewUp(*cam["view_up"])
        c.SetViewAngle(cam.get("view_angle", 30.0))
        c.SetParallelProjection(1 if cam.get("parallel") else 0)
        c.SetParallelScale(cam.get("parallel_scale", 1.0))
        self.renderer.ResetCameraClippingRange()


def want_geometry(mode: str, rtt_ms: float, triangles: int, budget: int, local: bool = False) -> bool:
    """Whether to draw in the browser: chosen, or (auto) whenever the surfaces fit the budget for a viewer
    on this machine, and for a remote one when its round trip is long."""
    if mode == "geometry":
        return True
    if mode == "images":
        return False
    return 0 < triangles <= budget and (local or rtt_ms > AUTO_RTT_MS)
