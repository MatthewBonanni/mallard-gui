"""The cells a region holds while its handle is dragged: Mallard's rule (the cells the region overlaps) on the
domain's cells in 2D, drawn in place of the check's highlight; hidden where the domain's cells aren't loaded."""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pytest
from vtkmodules.util.numpy_support import vtk_to_numpy
from vtkmodules.vtkFiltersCore import vtkCellCenters

from mallard_gui.setup.geometry import preview
from mallard_gui.view.live_cells import LiveCells, inside
from mallard_gui.view.scene import CaseScene

from .conftest import FIXTURES, fixture_json

RECT = {"type": "rectangle", "center": [3.0, 0.0], "size": [6.0, 3.0]}
NEAR_WAKE = 'write_data[prefix = "./solut/near_wake"].region'


@pytest.fixture
def scene() -> CaseScene:
    s = CaseScene(offscreen=True)
    s.load(FIXTURES / "cylinder_view" / "case.vtm", fixture_json("cylinder_check.json"))
    return s


def centers(scene: CaseScene, block: str) -> Any:
    cc = vtkCellCenters()
    cc.SetInputData(scene.blocks[block].actor.GetMapper().GetInput())
    cc.Update()
    return vtk_to_numpy(cc.GetOutput().GetPoints().GetData())[:, :2]


def test_the_even_odd_rule_holds_a_ring_and_excludes_its_edges() -> None:
    ring = preview({"type": "annulus", "center": [0, 0], "radius": 2.0, "inner_radius": 1.0}, 2)
    from mallard_gui.view.live_cells import _segments

    edges = _segments(ring.poly)
    pts = np.array([[0.0, 0.0], [1.5, 0.0], [0.0, -1.5], [3.0, 0.0], [edges[0, 0], edges[0, 1]]])
    assert inside(pts, edges, eps=1e-9).tolist() == [False, True, True, False, False]


def test_a_regions_cells_are_mallards_own(scene: CaseScene) -> None:
    """For the rectangle the check classified: within one boundary cell of its 398."""
    live = LiveCells(scene)
    cells = live.select(preview(RECT, 2))
    assert cells is not None and abs(len(cells) - scene.blocks[NEAR_WAKE].cells) <= 2


def test_a_moved_region_is_drawn_with_its_cells_at_once(scene: CaseScene) -> None:
    live = LiveCells(scene)
    before = centers(scene, NEAR_WAKE).mean(axis=0)
    live.update(NEAR_WAKE, preview(RECT, 2))  # the domain's cells, read once
    moved = {**RECT, "center": [4.0, 1.0]}
    start = time.perf_counter()
    live.update(NEAR_WAKE, preview(moved, 2))
    ms = 1000 * (time.perf_counter() - start)
    selected = live.select(preview(moved, 2))
    assert selected is not None and scene.blocks[NEAR_WAKE].cells == len(selected) > 100
    assert centers(scene, NEAR_WAKE).mean(axis=0) - before == pytest.approx(
        [1.0, 1.0], abs=0.4
    )  # cells grow away from the cylinder
    assert ms < 50, f"{ms:.1f} ms per update"


def test_a_cut_line_holds_the_cells_it_crosses(scene: CaseScene) -> None:
    live = LiveCells(scene)
    cut = {"type": "cut_plane", "point": [1.0, 0.0], "normal": [1.0, 0.0], "bounds": [[0.9, -2.0], [1.1, 2.0]]}
    cells = live.select(preview(cut, 2))
    assert cells is not None and len(cells) > 5


def test_without_the_domains_cells_the_old_highlight_is_hidden(scene: CaseScene) -> None:
    scene.dim = 3  # as in 3D, where the volume is never loaded
    LiveCells(scene).update(NEAR_WAKE, preview(RECT, 2))
    assert not scene.blocks[NEAR_WAKE].visible
