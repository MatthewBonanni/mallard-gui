"""A small run directory written the way Mallard writes one: a 3D volume series of a box (P = t + x, U = (x, y,
t)), a zone surface series of its x = 0 face, a run log whose output records name their writers, and the
run's input."""

from __future__ import annotations

import json
from pathlib import Path

from vtkmodules.vtkCommonCore import vtkDoubleArray
from vtkmodules.vtkCommonDataModel import vtkDataSet, vtkImageData
from vtkmodules.vtkFiltersCore import vtkAppendFilter
from vtkmodules.vtkFiltersGeometry import vtkDataSetSurfaceFilter
from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter, vtkXMLUnstructuredGridWriter

INPUT = """\
[[boundaries]]
name = "inlet"
type = "farfield"

[[write_data]]
prefix = "./solut/box"
fields = ["P", "U"]

[[write_data]]
prefix = "./solut/inlet"
region = "inlet"
fields = ["P"]
"""


def _fields(data: vtkDataSet, t: float) -> None:
    p, u = vtkDoubleArray(), vtkDoubleArray()
    p.SetName("P")
    u.SetName("U")
    u.SetNumberOfComponents(3)
    for i in range(data.GetNumberOfCells()):
        b = data.GetCell(i).GetBounds()
        x, y = (b[0] + b[1]) / 2, (b[2] + b[3]) / 2
        p.InsertNextValue(t + x)
        u.InsertNextTuple3(x, y, t)
    data.GetCellData().AddArray(p)
    data.GetCellData().AddArray(u)


def _header(path: Path, step: int, t: float, fields: list[tuple[str, str]]) -> None:
    meta = {
        "schema": "mallard-snapshot",
        "version": 1,
        "step": step,
        "time": t,
        "fields": [{"name": n, "units": u, "long_name": n.lower()} for n, u in fields],
        "units": {"time": "s"},
    }
    text = path.read_text()
    first, _, rest = text.partition("\n")
    path.write_text(f"{first}\n<!-- mallard-snapshot {json.dumps(meta)} -->\n{rest}")


def make_run(run: Path, steps: int = 4, n: int = 6, dim: int = 3) -> Path:
    """`steps` snapshots at t = 0, 1, ...: a box of n³ cells on [0, 1]³ and its x = 0 face; with `dim` 2, a
    run of a 2D build, its volume the n² cells of [0, 1]² (and no zone surface)."""
    (run / "solut").mkdir(parents=True, exist_ok=True)
    (run / "input.toml").write_text(INPUT)
    records: list[dict[str, object]] = [
        {"type": "start", "run_id": "r1", "provenance": {"build": {"dim": dim}}, "units": {"t": "s"}},
    ]
    for k in range(steps):
        t, step = float(k), 10 * k
        img = vtkImageData()
        img.SetDimensions(n + 1, n + 1, n + 1 if dim == 3 else 1)
        img.SetSpacing(1 / n, 1 / n, 1 / n)
        grid = vtkAppendFilter()
        grid.SetInputData(img)
        grid.Update()
        volume = grid.GetOutput()
        _fields(volume, t)
        vol = run / "solut" / f"box_{step:06d}.vtu"
        w = vtkXMLUnstructuredGridWriter()
        w.SetFileName(str(vol))
        w.SetInputData(volume)
        w.SetDataModeToAscii()
        w.Write()
        _header(vol, step, t, [("P", "Pa"), ("U", "m s-1")])
        surface = vtkDataSetSurfaceFilter()
        surface.SetInputData(volume)
        surface.Update()
        face = surface.GetOutput()
        keep = [i for i in range(face.GetNumberOfCells()) if face.GetCell(i).GetBounds()[1] < 1e-9]
        face.BuildLinks()
        for i in range(face.GetNumberOfCells()):
            if i not in keep:
                face.DeleteCell(i)
        face.RemoveDeletedCells()
        surf = run / "solut" / f"inlet_{step:06d}.vtp"
        pw = vtkXMLPolyDataWriter()
        pw.SetFileName(str(surf))
        pw.SetInputData(face)
        pw.SetDataModeToAscii()
        pw.Write()
        _header(surf, step, t, [("P", "Pa")])
        if dim == 2:
            surf.unlink()
        for path, writer, kind in ((vol, "box.pvd", "vtu"), (surf, "inlet.pvd", "vtp"))[: dim - 1]:
            records.append(
                {
                    "type": "output",
                    "step": step,
                    "t": t,
                    "kind": kind,
                    "path": f"./solut/{path.name}",
                    "writer": f"./solut/{writer}",
                    "files": [{"path": f"./solut/{path.name}", "bytes": path.stat().st_size}],
                }
            )
    records.append({"type": "end", "outcome": "finished"})
    (run / "mallard.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return run
