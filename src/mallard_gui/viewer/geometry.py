"""Geometry mode: the surfaces a viewer shows, sent as decimated triangles with their colors already mapped,
for the browser to render with vtk.js. The viewer clips and colors next to the data, so the GUI holds only
what is drawn: points as float32, triangles and lines as int32 indices, and an RGB byte per point, each
base64-encoded. Within `Budget.geometry_triangles` of the whole scene, or the view stays in image mode."""

from __future__ import annotations

import base64
from typing import Any

import numpy as np
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
from vtkmodules.vtkFiltersCore import vtkClipPolyData, vtkTriangleFilter
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

SCHEMA = "mallard-viewer-geometry"
VERSION = 1


def _b64(a: np.ndarray[Any, Any]) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def _unb64(text: str, dtype: str, width: int) -> np.ndarray[Any, Any]:
    a = np.frombuffer(base64.b64decode(text), dtype=dtype)
    return a.reshape(-1, width) if width > 1 else a


def triangles(poly: vtkPolyData) -> vtkPolyData:
    tri = vtkTriangleFilter()
    tri.SetInputData(poly)
    tri.PassLinesOn()
    tri.PassVertsOff()
    tri.Update()
    out = vtkPolyData()
    out.ShallowCopy(tri.GetOutput())
    return out


def clipped(poly: vtkPolyData, plane: Any) -> vtkPolyData:
    clip = vtkClipPolyData()
    clip.SetInputData(poly)
    clip.SetClipFunction(plane)  # the side its normal points to stays, as a mapper's clipping plane keeps it
    clip.Update()
    out = vtkPolyData()
    out.ShallowCopy(clip.GetOutput())
    return out


def _connectivity(cells: vtkCellArray, per: int) -> np.ndarray[Any, Any]:
    """The cells of `per` points each, as rows of point indices (other sizes dropped)."""
    if cells.GetNumberOfCells() == 0:
        return np.zeros((0, per), dtype=np.int32)
    offsets = vtk_to_numpy(cells.GetOffsetsArray())
    conn = vtk_to_numpy(cells.GetConnectivityArray())
    sizes = np.diff(offsets)
    starts = offsets[:-1][sizes == per]
    idx = starts[:, None] + np.arange(per)[None, :]
    out: np.ndarray[Any, Any] = conn[idx].astype(np.int32)
    return out


def encode_block(
    name: str, poly: vtkPolyData, rgb: np.ndarray[Any, Any] | None, color: tuple[float, float, float], opacity: float
) -> dict[str, Any]:
    """One block: its triangles and line segments, and its colors (per point) or its one color."""
    pts = vtk_to_numpy(poly.GetPoints().GetData()).astype(np.float32) if poly.GetNumberOfPoints() else np.zeros((0, 3))
    tris = _connectivity(poly.GetPolys(), 3)
    lines = _connectivity(poly.GetLines(), 2)
    out: dict[str, Any] = {
        "name": name,
        "points": _b64(pts.astype(np.float32)),
        "triangles": _b64(tris),
        "lines": _b64(lines),
        "n_points": int(pts.shape[0]),
        "n_triangles": int(tris.shape[0]),
        "color": [round(c, 4) for c in color],
        "opacity": round(opacity, 3),
    }
    if rgb is not None:
        out["rgb"] = _b64(rgb.astype(np.uint8))
    return out


def decode_block(block: dict[str, Any]) -> vtkPolyData:
    pts = _unb64(block["points"], "float32", 3)
    poly = vtkPolyData()
    points = vtkPoints()
    points.SetData(numpy_to_vtk(pts.copy(), deep=True))
    poly.SetPoints(points)
    for key, per, setter in (("triangles", 3, poly.SetPolys), ("lines", 2, poly.SetLines)):
        conn = _unb64(block[key], "int32", per).astype(np.int64)
        if conn.size:
            cells = vtkCellArray()
            offsets = np.arange(0, conn.size + 1, per, dtype=np.int64)
            cells.SetData(numpy_to_vtkIdTypeArray(offsets, deep=True), numpy_to_vtkIdTypeArray(conn.ravel(), deep=True))
            setter(cells)
    if "rgb" in block:
        rgb = numpy_to_vtk(_unb64(block["rgb"], "uint8", 3).copy(), deep=True)
        rgb.SetName("rgb")
        poly.GetPointData().SetScalars(rgb)
    return poly


def actor_of(block: dict[str, Any]) -> vtkActor:
    """An actor drawing a decoded block with its mapped colors, as the viewer drew it."""
    poly = decode_block(block)
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(poly)
    if "rgb" in block:
        mapper.SetColorModeToDirectScalars()
        mapper.ScalarVisibilityOn()
    else:
        mapper.ScalarVisibilityOff()
    actor = vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*block["color"])
    actor.GetProperty().SetOpacity(block.get("opacity", 1.0))
    if block.get("n_triangles", 0) == 0:
        actor.GetProperty().SetLineWidth(3.0)
        actor.GetProperty().LightingOff()  # vtk.js lights normal-less lines as if facing the light: washed out
    return actor
