"""Surface levels of detail: built piece by piece on one global clustering grid (pieces made apart agree),
level sizes near their targets, nested tiles, and refinement on zoom within the budget, with tiles out of
view leaving memory. The engine draws a snapshot's levels instead of reading it."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from vtkmodules.vtkCommonCore import vtkDoubleArray
from vtkmodules.vtkCommonDataModel import vtkPolyData
from vtkmodules.vtkFiltersSources import vtkSphereSource
from vtkmodules.vtkRenderingCore import vtkCamera

from mallard_gui.viewer import lod
from mallard_gui.viewer.engine import Budget, ResultScene

from .runs import make_run


def sphere(res: int = 200, center: tuple[float, float, float] = (0, 0, 0)) -> vtkPolyData:
    src = vtkSphereSource()
    src.SetThetaResolution(res)
    src.SetPhiResolution(res)
    src.SetCenter(*center)
    src.Update()
    poly = lod.triangulated(src.GetOutput())
    p = vtkDoubleArray()
    p.SetName("P")
    for i in range(poly.GetNumberOfPoints()):
        p.InsertNextValue(poly.GetPoint(i)[2])
    poly.GetPointData().AddArray(p)
    return poly


def halves(poly: vtkPolyData) -> list[vtkPolyData]:
    from vtkmodules.vtkCommonDataModel import vtkPlane
    from vtkmodules.vtkFiltersCore import vtkClipPolyData

    out = []
    for inside in (0, 1):
        plane = vtkPlane()
        plane.SetNormal(1, 0, 0)
        clip = vtkClipPolyData()
        clip.SetInputData(poly)
        clip.SetClipFunction(plane)
        clip.SetInsideOut(inside)
        clip.Update()
        out.append(lod.triangulated(clip.GetOutput()))
    return out


def test_levels_near_their_targets_with_fields_and_nested_tiles(tmp_path: Path) -> None:
    s = sphere()
    index = lod.build([s], tmp_path / "lod", [("P", 1)], s.GetBounds(), lod.surface_area(s), targets=(500, 5000, 30000))
    counts = [lv["triangles"] for lv in index["levels"]]
    assert counts[0] < counts[1] < counts[2] <= s.GetNumberOfPolys()
    assert 250 < counts[0] < 1500 and 2500 < counts[1] < 12000
    assert [lv["tiles_per_axis"] for lv in index["levels"]] == [1, 2, 4]
    assert sum(t["triangles"] for t in index["levels"][2]["tiles"]) == counts[2]
    tile = lod.read_tile(tmp_path / "lod" / index["levels"][1]["tiles"][0]["file"], index["fields"])
    p = tile.GetPointData().GetArray("P")
    z = np.array([tile.GetPoint(i)[2] for i in range(tile.GetNumberOfPoints())])
    assert np.allclose([p.GetValue(i) for i in range(len(z))], z, atol=1e-5)  # means of the same vertices


def test_pieces_made_apart_merge_into_the_same_levels(tmp_path: Path) -> None:
    s = sphere(120)
    args = ([("P", 1)], s.GetBounds(), lod.surface_area(s), (400, 4000))
    whole = lod.build([s], tmp_path / "a", *args)
    split = lod.build(halves(s), tmp_path / "b", *args)
    for a, b in zip(whole["levels"], split["levels"], strict=True):
        assert abs(a["triangles"] - b["triangles"]) <= 0.02 * a["triangles"]  # only the cut's slivers differ


def test_refinement_follows_the_camera_within_the_budget(tmp_path: Path) -> None:
    near = sphere(160)
    far = sphere(160, center=(20, 0, 0))
    from vtkmodules.vtkFiltersCore import vtkAppendPolyData

    both = vtkAppendPolyData()
    both.AddInputData(near)
    both.AddInputData(far)
    both.Update()
    poly = both.GetOutput()
    lod.build([poly], tmp_path / "lod", [("P", 1)], poly.GetBounds(), lod.surface_area(poly), (300, 3000, 30000))
    surf = lod.LodSurface(tmp_path / "lod")
    surf.coarsest()
    cam = vtkCamera()
    cam.SetFocalPoint(0, 0, 0)
    cam.SetPosition(0, 0, 3)  # close to the first sphere, the second far out of view
    assert surf.refine(cam, 1000, budget=40_000)
    levels = {lvl: n for (lvl, _), n in ((k, 1) for k in surf.shown)}
    assert max(levels) == 2
    fine = [k for k in surf.shown if k[0] == 2]
    tiles = {tuple(t["ijk"]): t for t in surf.levels[2]["tiles"]}
    assert all(tiles[ijk]["bounds"][0] < 10 for _, ijk in fine)  # only the near sphere's tiles refine
    assert surf.triangles <= 40_000
    assert not surf.refine(cam, 1000, budget=40_000)  # nothing changes for the same view
    cam.SetPosition(0, 0, 400)
    assert surf.refine(cam, 1000, budget=40_000)
    assert max(k[0] for k in surf.shown) < 2 and len(surf._cache) == len(surf.shown)


def test_the_engine_draws_a_snapshots_levels_and_refines_them(tmp_path: Path) -> None:
    run = make_run(tmp_path / "run", steps=1, n=24)
    s = ResultScene(run.parent, Budget(volume_cells=100))  # the volume is over budget: never read
    s.open(run.name)
    built = s.build_lod("solut/box", 0, targets=(200, 2000))
    assert built["path"] == "lod/solut/box/000000000"
    s2 = ResultScene(run.parent, Budget(volume_cells=100))
    s2.open(run.name)
    info = s2.show("solut/box", 0)
    assert "field" in s2.lods and not info["notice"]
    assert info["lod"]["field"]["levels"] == {0: 1}
    assert "P" in s2.field_names()
    s2.camera("zoom", factor=4.0)
    s2.frame(800, 600)
    assert 1 in s2.info()["lod"]["field"]["levels"]
    index = json.loads((run / built["path"] / "index.json").read_text())
    assert index["source"]["step"] == 0 and index["fields"][0]["units"] == "Pa"
    with pytest.raises(Exception, match="series"):
        s2.build_lod("nope", 0)


def test_a_flat_surface_of_many_triangles_coarsens_to_its_target_with_its_fields() -> None:
    """Quadric decimation of a flat 2D snapshot never finished (its quadrics are singular); clustering is one pass."""
    from vtkmodules.vtkFiltersSources import vtkPlaneSource

    plane = vtkPlaneSource()
    plane.SetResolution(400, 400)
    plane.Update()
    flat = lod.triangulated(plane.GetOutput())
    x = vtkDoubleArray()
    x.SetName("X")
    for i in range(flat.GetNumberOfPoints()):
        x.InsertNextValue(flat.GetPoint(i)[0])
    flat.GetPointData().AddArray(x)
    xc = vtkDoubleArray()  # each triangle's own value, for cell-value coloring
    xc.SetName("XC")
    for i in range(flat.GetNumberOfCells()):
        b = flat.GetCell(i).GetBounds()
        xc.InsertNextValue((b[0] + b[1]) / 2)
    flat.GetCellData().AddArray(xc)
    coarse = lod.clustered(flat, 20_000)
    tri = np.array([[coarse.GetPoint(coarse.GetCell(i).GetPointId(k))[0] for k in range(3)] for i in range(200)])
    xc_coarse = np.array([coarse.GetCellData().GetArray("XC").GetValue(i) for i in range(200)])
    assert np.all((xc_coarse > tri.min(axis=1) - 0.01) & (xc_coarse < tri.max(axis=1) + 0.01))
    assert 10_000 < coarse.GetNumberOfPolys() < 30_000
    pts = np.array([coarse.GetPoint(i) for i in range(coarse.GetNumberOfPoints())])
    values = np.array([coarse.GetPointData().GetArray("X").GetValue(i) for i in range(len(pts))])
    assert np.allclose(values, pts[:, 0])  # a linear field averaged with its points
    assert lod.surface_area(coarse) == pytest.approx(1.0, rel=0.02)
