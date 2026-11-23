"""A stand-in for `mallard session` speaking its protocol (JSON-RPC 2.0 lines on stdin and stdout), for the
GUI's tests: initialize, case.open, mesh.cut (VTK's cutter on the case's mesh, written to the named
directory, decimated to a target when asked), mesh.pick, $/cancel and shutdown. `python fake_session.py MESH`."""

import json
import sys
from pathlib import Path

from vtkmodules.vtkCommonDataModel import vtkPlane, vtkStaticCellLocator
from vtkmodules.vtkFiltersCore import vtkCutter, vtkQuadricDecimation, vtkTriangleFilter
from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter, vtkXMLUnstructuredGridReader

mesh_path = sys.argv[1]
mesh = None
count = 0


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    req = json.loads(line)
    method, params, rid = req.get("method"), req.get("params") or {}, req.get("id")
    if method == "$/cancel":
        continue
    if method == "initialize":
        send(
            {
                "id": rid,
                "result": {
                    "schema": "mallard-capabilities",
                    "version": 1,
                    "methods": ["case.open", "mesh.cut", "mesh.pick"],
                },
            }
        )
    elif method == "case.open":
        r = vtkXMLUnstructuredGridReader()
        r.SetFileName(mesh_path)
        r.Update()
        mesh = r.GetOutput()
        send({"id": rid, "result": {"schema": "mallard-case", "version": 1, "input": params["input"], "dimension": 3}})
    elif method in ("mesh.cut", "mesh.pick") and mesh is None:
        send({"id": rid, "error": {"code": -32001, "message": "no case"}})
    elif method == "mesh.cut":
        send({"jsonrpc": "2.0", "method": "$/progress", "params": {"id": rid, "phase": "cut", "done": 0.5}})
        plane = vtkPlane()
        plane.SetOrigin(*params["plane"]["origin"])
        plane.SetNormal(*params["plane"]["normal"])
        cut = vtkCutter()
        cut.SetCutFunction(plane)
        cut.SetInputData(mesh)
        tri = vtkTriangleFilter()
        tri.SetInputConnection(cut.GetOutputPort())
        tri.Update()
        out = tri.GetOutput()
        if params.get("target_triangles") and out.GetNumberOfPolys() > params["target_triangles"]:
            dec = vtkQuadricDecimation()
            dec.SetInputData(out)
            dec.SetTargetReduction(1 - params["target_triangles"] / out.GetNumberOfPolys())
            dec.Update()
            out = dec.GetOutput()
        count += 1
        path = Path(params["dir"]) / f"cut_{count}.vtp"
        path.parent.mkdir(parents=True, exist_ok=True)
        w = vtkXMLPolyDataWriter()
        w.SetFileName(str(path))
        w.SetInputData(out)
        w.Write()
        send(
            {
                "id": rid,
                "result": {
                    "schema": "mallard-mesh-cut",
                    "version": 1,
                    "file": str(path),
                    "triangles": out.GetNumberOfPolys(),
                    "cells": out.GetNumberOfPolys(),
                },
            }
        )
    elif method == "mesh.pick":
        loc = vtkStaticCellLocator()
        loc.SetDataSet(mesh)
        loc.BuildLocator()
        cid = loc.FindCell(params["point"] + [0.0] * (3 - len(params["point"])))
        if cid < 0:
            send({"id": rid, "error": {"code": -32020, "message": "outside the mesh"}})
            continue
        b = mesh.GetCell(cid).GetBounds()
        send(
            {
                "id": rid,
                "result": {
                    "schema": "mallard-mesh-pick",
                    "version": 1,
                    "id": cid,
                    "zone": "domain",
                    "center": [(b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2],
                    "volume": (b[1] - b[0]) * (b[3] - b[2]) * (b[5] - b[4]),
                    "rank": 0,
                },
            }
        )
    elif method == "shutdown":
        send({"id": rid, "result": None})
        break
    else:
        send({"id": rid, "error": {"code": -32601, "message": f"no method {method}"}})
