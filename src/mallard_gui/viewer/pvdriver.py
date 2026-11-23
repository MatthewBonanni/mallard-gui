"""The parallel viewer's driver (design: Viewer, Rendering): run by `pvpython` inside the viewer job, it
connects to a `pvserver` of N MPI ranks that reads the snapshot in parallel and composites each image with
IceT, and answers the viewer protocol on its standard input and output, so only images leave the job and the
GUI drives it like any viewer. Its methods are the volume's: whole-volume surfaces, colored by a field, and
the camera; the series come from the same catalog as the single viewer's.

It imports nothing of the GUI's but `catalog.py`, copied beside it, so ParaView's own VTK is never shadowed.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catalog

NAME = "mallard-viewer"
VERSION = 1
# The single viewer's backgrounds and view presets (view/scene.py), copied: the driver imports nothing of the GUI.
BACKGROUNDS = {"dark": (0.043, 0.059, 0.075), "light": (1.0, 1.0, 1.0)}
PRESETS = {
    "+X": ((1, 0, 0), (0, 1, 0)),
    "-X": ((-1, 0, 0), (0, 1, 0)),
    "+Y": ((0, 1, 0), (0, 0, 1)),
    "-Y": ((0, -1, 0), (0, 0, 1)),
    "+Z": ((0, 0, 1), (0, 1, 0)),
    "-Z": ((0, 0, -1), (0, 1, 0)),
    "iso": ((-1, -1, -1), (0, 1, 0)),
}
# The GUI's colormaps (viewer/colormaps.py: ParaView's presets by name, shipped beside the driver), set as points so
# any ParaView draws them as the GUI does.
COLORMAPS = {p["Name"]: p for p in json.loads(Path(__file__).with_name("colormaps.json").read_text(encoding="utf-8"))}
DEFAULT_COLORMAP = "Fast"


class Driver:
    def __init__(self, root: str, server: str, timeout: float) -> None:
        from paraview import simple

        self.pv = simple
        self.root = Path(root).resolve()
        host, _, port = server.rpartition(":")
        end = time.monotonic() + timeout
        while True:
            try:
                self.connection = simple.Connect(host or "localhost", int(port))
                if self.connection is not None:
                    break
            except RuntimeError:
                pass
            if time.monotonic() > end:
                raise RuntimeError(f"no pvserver at {server} after {timeout:.0f} s")
            time.sleep(1.0)
        from paraview import servermanager

        self.ranks = int(servermanager.ActiveConnection.GetNumberOfDataPartitions())
        self.view = simple.CreateRenderView()
        self.view.OrientationAxesVisibility = 0
        self.view.EnableRenderOnInteraction = 0
        self.run_dir: Path | None = None
        self.series: dict[str, catalog.Series] = {}
        self.source: Any = None
        self.display: Any = None
        self.primary, self.index = "", 0
        self.field, self.component, self.colormap = "", "magnitude", DEFAULT_COLORMAP
        self.range: list[float] | None = None
        self.dim = 3
        self.size = (800, 600)

    # The protocol's methods

    def open(self, run: str) -> dict[str, Any]:
        path = Path(run)
        path = (path if path.is_absolute() else self.root / path).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"{run}: outside the viewer's root {self.root}")
        self.run_dir = path
        self.series = {s.id: s for s in catalog.discover(path)}
        return {"dim": self.dim, "series": [s.to_dict() for s in self.series.values()]}

    def show(
        self, series: str, index: int, zones: list[str] | None = None, particles: list[str] | None = None
    ) -> dict[str, Any]:
        s = self.series[series]
        index = max(0, min(int(index), len(s.snapshots) - 1))
        assert self.run_dir is not None
        snap = s.snapshots[index]
        if self.source is not None:
            self.pv.Delete(self.source)
        self.source = self.pv.OpenDataFile(str(self.run_dir / snap.path))
        if self.source is None:
            raise ValueError(f"{snap.path}: ParaView cannot read it")
        if snap.index and hasattr(self.source, "TimestepValues") and self.source.TimestepValues:
            steps = list(self.source.TimestepValues)
            self.view.ViewTime = steps[min(snap.index, len(steps) - 1)]
        self.display = self.pv.Show(self.source, self.view)
        self.display.SetRepresentationType("Surface")
        first = self.primary != series
        self.primary, self.index = series, index
        names = self._field_names()
        if self.field not in names:
            self.field = names[0] if names else ""
        self._color()
        if first:
            self.pv.ResetCamera(self.view)
        return self.info()

    def set_field(
        self,
        field: str = "",
        component: str = "",
        colormap: str = "",
        range: list[float] | None = None,
        auto_range: bool = False,
        interpolation: str = "",  # the parallel viewer draws cell values only (its info says "cells")
    ) -> dict[str, Any]:
        self.field = field or self.field
        self.component = component or self.component
        self.colormap = colormap or self.colormap
        self.range = None if auto_range else ([float(range[0]), float(range[1])] if range else self.range)
        self._color()
        return self.info()

    def camera(
        self, op: str, dx: float = 0.0, dy: float = 0.0, factor: float = 1.0, preset: str = "", aspect: float = 0.0
    ) -> None:
        """The single viewer's moves: orbit and pan by (dx, dy) of the view's size, zoom by a factor. Fit keeps
        the view direction (ParaView's reset does); `aspect` is the single viewer's, images here have their own."""
        cam = self.view.GetActiveCamera()
        if op == "orbit":
            cam.Azimuth(-dx * 180.0)
            cam.Elevation(dy * 180.0)
            cam.OrthogonalizeViewUp()
        elif op == "pan":
            parallel = bool(cam.GetParallelProjection())
            angle = math.radians(cam.GetViewAngle() / 2)
            height = 2 * cam.GetParallelScale() if parallel else 2 * cam.GetDistance() * math.tan(angle)
            w, h = self.size
            up, normal = cam.GetViewUp(), cam.GetDirectionOfProjection()
            right = _cross(normal, up)
            sx, sy = -dx * height * (w / max(h, 1)), dy * height
            move = [sx * r + sy * u for r, u in zip(right, up, strict=True)]
            f, p = cam.GetFocalPoint(), cam.GetPosition()
            cam.SetFocalPoint(*(a + m for a, m in zip(f, move, strict=True)))
            cam.SetPosition(*(a + m for a, m in zip(p, move, strict=True)))
        elif op == "zoom":
            if cam.GetParallelProjection():
                cam.SetParallelScale(cam.GetParallelScale() / factor)
            else:
                cam.Dolly(float(factor))
        elif op == "preset":
            direction, up = PRESETS[preset]
            self.view.CameraFocalPoint = [0.0, 0.0, 0.0]
            self.view.CameraPosition = [-d for d in direction]
            self.view.CameraViewUp = list(up)
            self.view.ResetCamera(False)
        elif op in ("fit", "reset"):
            self.view.ResetCamera(False)
        elif op == "parallel":
            cam.SetParallelProjection(1 if factor else 0)
        elif op == "lock2d":
            pass  # the page pans instead of orbiting; volumes here are 3D
        else:
            raise ValueError(f"camera op {op!r}")

    def set_theme(self, theme: str) -> None:
        self.view.UseColorPaletteForBackground = 0
        self.view.Background = list(BACKGROUNDS.get(theme, BACKGROUNDS["dark"]))

    def get_camera(self) -> dict[str, Any]:
        cam = self.view.GetActiveCamera()
        return {
            "position": list(cam.GetPosition()),
            "focal_point": list(cam.GetFocalPoint()),
            "view_up": list(cam.GetViewUp()),
            "view_angle": cam.GetViewAngle(),
            "parallel": bool(cam.GetParallelProjection()),
            "parallel_scale": cam.GetParallelScale(),
        }

    def set_camera(
        self,
        position: list[float],
        focal_point: list[float],
        view_up: list[float],
        view_angle: float = 30.0,
        parallel: bool = False,
        parallel_scale: float = 1.0,
    ) -> dict[str, Any]:
        cam = self.view.GetActiveCamera()
        cam.SetPosition(*position)
        cam.SetFocalPoint(*focal_point)
        cam.SetViewUp(*view_up)
        cam.SetViewAngle(view_angle)
        cam.SetParallelProjection(1 if parallel else 0)
        cam.SetParallelScale(parallel_scale)
        return self.get_camera()

    def frame(self, width: int, height: int, interactive: bool = False) -> dict[str, Any]:
        width, height = max(int(width), 16), max(int(height), 16)
        self.size = (width, height)
        if interactive:
            width, height = max(width // 2, 16), max(height // 2, 16)
        start = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "frame.png")
            self.pv.SaveScreenshot(out, self.view, ImageResolution=[width, height])
            image = Path(out).read_bytes()
        return {
            "image": base64.b64encode(image).decode("ascii"),
            "mime": "image/png",
            "width": width,
            "height": height,
            "ms": round(1000 * (time.perf_counter() - start), 1),
            "camera": self.get_camera(),
            "triangles": 0,
        }

    def screenshot(self, width: int, height: int) -> dict[str, Any]:
        size = self.size
        try:
            return self.frame(width, height)
        finally:
            self.size = size

    def info(self) -> dict[str, Any]:
        s = self.series.get(self.primary)
        snap = s.snapshots[self.index] if s and s.snapshots else None
        data = self.source.GetDataInformation() if self.source is not None else None
        rng = self._data_range()
        return {
            "series": self.primary,
            "index": self.index,
            "count": len(s.snapshots) if s else 0,
            "step": snap.step if snap else None,
            "t": snap.t if snap else None,
            "zones": [],
            "field": self.field,
            "component": self.component,
            "colormap": self.colormap,
            "interpolation": "cells",
            "range": self.range or rng,
            "data_range": rng,
            "auto_range": self.range is None,
            "fields": self._fields(),
            "blocks": [],
            "cells": int(data.GetNumberOfCells()) if data is not None else 0,
            "triangles": 0,
            "levels": {},
            "cached_bytes": 0,
            "budget": {},
            "notice": f"parallel viewer: {self.ranks} ranks, composited with IceT",
            "lod": {},
            "section": {},
            "dim": self.dim,
            "ranks": self.ranks,
        }

    def render_info(self) -> dict[str, Any]:
        return {"window": "pvserver (IceT)", "ranks": self.ranks, "gpu": False, "vendor": "", "renderer": ""}

    # Helpers

    def _arrays(self) -> list[tuple[str, str, int]]:
        if self.source is None:
            return []
        out = []
        for assoc, infos in (("CELLS", self.source.CellData), ("POINTS", self.source.PointData)):
            for name in infos.keys():  # noqa: SIM118  iterating ParaView's field information yields arrays
                out.append((assoc, name, int(infos[name].GetNumberOfComponents())))
        return out

    def _field_names(self) -> list[str]:
        return [name for _, name, _ in self._arrays() if not name.startswith("vtk")]

    def _fields(self) -> list[dict[str, Any]]:
        return [{"name": n, "components": c, "units": "", "location": a.lower()} for a, n, c in self._arrays()]

    def _array(self) -> tuple[str, int] | None:
        return next(((a, c) for a, n, c in self._arrays() if n == self.field), None)

    def _data_range(self) -> list[float] | None:
        arr = self._array()
        if arr is None or self.source is None:
            return None
        infos = self.source.CellData if arr[0] == "CELLS" else self.source.PointData
        comp = -1 if arr[1] > 1 and self.component == "magnitude" else _component(self.component)
        lo, hi = infos[self.field].GetRange(comp)
        return [float(lo), float(hi)]

    def _color(self) -> None:
        arr = self._array()
        if self.display is None or arr is None:
            return
        assoc, comps = arr
        if comps > 1 and self.component != "magnitude":
            self.pv.ColorBy(self.display, (assoc, self.field, self.component.upper()))
        else:
            self.pv.ColorBy(self.display, (assoc, self.field, "Magnitude") if comps > 1 else (assoc, self.field))
        lut = self.pv.GetColorTransferFunction(self.field)
        preset = COLORMAPS.get(self.colormap) or COLORMAPS[DEFAULT_COLORMAP]
        pts = preset["RGBPoints"]
        x0, x1 = min(pts[0::4]), max(pts[0::4])
        lut.ColorSpace = preset.get("ColorSpace", "RGB")
        lut.RGBPoints = [(v - x0) / (x1 - x0) if i % 4 == 0 else v for i, v in enumerate(pts)]
        rng = self.range or self._data_range()
        if rng:
            lut.RescaleTransferFunction(rng[0], rng[1])
        self.display.SetScalarBarVisibility(self.view, False)


def _component(name: str) -> int:
    return {"x": 0, "y": 1, "z": 2}.get(name.lower(), 0)


def _cross(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


METHODS = (
    "open",
    "show",
    "set_field",
    "set_theme",
    "camera",
    "get_camera",
    "set_camera",
    "frame",
    "screenshot",
    "info",
    "render_info",
)


def serve(driver: Driver, idle: float) -> str:
    # The protocol owns the real stdout; pvpython's own stdout (a capture helper without .buffer) and anything
    # ParaView prints go to stderr instead.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    out.write(json.dumps({"hello": NAME, "version": VERSION, "pid": os.getpid(), "host": socket.gethostname(),
                          "engine": "paraview", "ranks": driver.ranks}).encode() + b"\n")  # fmt: skip
    out.flush()
    import selectors

    sel = selectors.DefaultSelector()
    sel.register(0, selectors.EVENT_READ)
    buffer = b""
    while True:
        while b"\n" not in buffer:
            if not sel.select(idle):
                return "idle"
            chunk = os.read(0, 1 << 16)
            if not chunk:
                return "closed"
            buffer += chunk
        line, _, buffer = buffer.partition(b"\n")
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method = request.get("id"), request.get("method")
        if method not in METHODS:
            reply: dict[str, Any] = {"id": rid, "error": f"the parallel viewer has no {method!r}"}
        else:
            try:
                reply = {"id": rid, "result": getattr(driver, method)(**(request.get("params") or {}))}
            except Exception as e:  # noqa: BLE001  every failure goes back to the client
                reply = {"id": rid, "error": f"{type(e).__name__}: {e}"}
        out.write(json.dumps(reply).encode() + b"\n")
        out.flush()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--server", required=True, help="HOST:PORT of pvserver")
    p.add_argument("--root", required=True)
    p.add_argument("--idle", type=float, default=1800.0)
    p.add_argument("--connect-timeout", type=float, default=120.0)
    a = p.parse_args()
    serve(Driver(a.root, a.server, a.connect_timeout), a.idle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
