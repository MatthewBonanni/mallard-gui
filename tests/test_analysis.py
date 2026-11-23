from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from mallard_gui.analysis.overlays import BoundaryOverlay, InitialOverlay, QualityOverlay, uppercase_arrays
from mallard_gui.analysis.report import histograms, periodic_pairs, unassigned, y_plus
from mallard_gui.mallard import Analysis, CheckReport, Mallard, analysis_of
from mallard_gui.view.scene import CaseScene

from .conftest import FIXTURES, FakeMallard

ANALYSIS = FIXTURES / "cylinder_analysis"


@pytest.fixture
def analysis(tmp_path: Path) -> Analysis:
    return FakeMallard().analyze(tmp_path / "input.toml", tmp_path / "analysis")


@pytest.fixture
def scene(analysis: Analysis) -> CaseScene:
    s = CaseScene(offscreen=True)
    s.window.SetSize(400, 300)
    assert analysis.view is not None
    s.load(analysis.view, analysis.report.data)
    return s


def report_data() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ANALYSIS / "check.json").read_text(encoding="utf-8"))
    return data


def test_analysis_files_resolve_from_the_directory_the_check_ran_in(tmp_path: Path) -> None:
    report = CheckReport.from_json(report_data())
    a = analysis_of(report, tmp_path)
    assert a.view == (tmp_path / "view" / "case.vtm").resolve()
    assert a.mesh_fields == (tmp_path / "mesh_000000.vtu").resolve()
    assert a.initial == (tmp_path / "initial_000000.vtu").resolve()


def test_report_sections_read_as_mallard_wrote_them() -> None:
    data = report_data()
    assert [h.metric for h in histograms(data)] == ["SKEWNESS", "ASPECT_RATIO", "NON_ORTHOGONALITY", "VOLUME_RATIO"]
    assert all(h.cells == data["cost"]["cells"] for h in histograms(data))
    yp = y_plus(data)
    assert yp is not None and yp["max"] == data["resolution"]["y_plus_max"]["value"]
    assert unassigned({"unassigned": [{"zone": "farfield", "faces": 64}]}) == {"farfield": 64}
    pairs = periodic_pairs(
        {
            "periodic": [
                {"zones": ["start", "end"], "map": "rotation", "angle": 90, "center": [0, 0], "faces": 64,
                 "area": 1.0, "node_offset_max": 1e-15, "node_offset_relative": 1e-14},
                {"zones": None, "map": "translation", "translation": [2, 0], "faces": 8, "area": 1.0,
                 "node_offset_max": 0.01, "node_offset_relative": 0.08},
            ]
        }
    )  # fmt: skip
    assert pairs[0].map_text == "rotation 90° about (0, 0)" and pairs[0].matches
    assert pairs[1].label == "periodic" and pairs[1].map_text == "translation (2, 0)" and not pairs[1].matches


def test_walls_are_colored_by_mallards_y_plus(analysis: Analysis, scene: CaseScene) -> None:
    overlay = BoundaryOverlay(scene)
    overlay.load(analysis.view)
    assert overlay.walls() == ["cylinder"]
    rng = overlay.y_plus_range()
    assert rng is not None
    assert rng[1] == pytest.approx(analysis.report.data["resolution"]["y_plus_max"]["value"], rel=1e-9)
    overlay.show(["cylinder", "farfield"], [], normals=False, y_plus=True)
    (wall,) = scene.overlay_layers[BoundaryOverlay.LAYER]
    colors = wall.GetMapper().GetInput().GetCellData().GetScalars()
    assert colors.GetName() == "Y_PLUS" and colors.GetNumberOfComponents() >= 3  # mapped here, for vtk.js too


def test_the_case_views_face_arrays_read_in_either_spelling() -> None:
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    def zone(name: str) -> vtkPolyData:
        data = vtkPolyData()
        arr = numpy_to_vtk([1.0, 2.0], deep=True)
        arr.SetName(name)
        data.GetCellData().AddArray(arr)
        return data

    for name in ("Y_PLUS", "y_plus"):  # Mallard's, and before #1468
        cells = uppercase_arrays(zone(name)).GetCellData()
        assert cells.GetArray("Y_PLUS") is not None and cells.GetArray("y_plus") is None


def test_unassigned_zones_and_normals_are_drawn(analysis: Analysis, scene: CaseScene) -> None:
    overlay = BoundaryOverlay(scene)
    overlay.load(analysis.view)
    overlay.show(["cylinder"], ["farfield"], normals=True, y_plus=False)
    actors = scene.overlay_layers[BoundaryOverlay.LAYER]
    assert len(actors) == 3  # farfield highlighted, the arrows of both zones
    arrows = actors[1].GetMapper().GetInput()
    one = arrows.GetNumberOfPoints() // 64
    assert one > 0 and arrows.GetNumberOfPoints() == 64 * one  # an arrow per cylinder face
    # Mallard's normals point out of the domain: on the cylinder, toward its axis
    b = arrows.GetBounds()
    assert max(abs(b[0]), abs(b[1]), abs(b[2]), abs(b[3])) <= 0.5 + 1e-9
    scene.set_clip(True)
    assert all(a.GetMapper().GetNumberOfClippingPlanes() == 1 for a in actors)
    overlay.clear()
    assert BoundaryOverlay.LAYER not in scene.overlay_layers


def test_bins_select_the_cells_mallard_counted(analysis: Analysis, scene: CaseScene) -> None:
    overlay = QualityOverlay(scene)
    overlay.load(analysis.mesh_fields, cells=1536)
    for hist in histograms(analysis.report.data):
        selected = [
            overlay.cells_between(hist.metric, *hist.bin_range(k), k == len(hist.counts) - 1)
            for k in range(len(hist.counts))
        ]
        assert [c.GetNumberOfCells() if c is not None else -1 for c in selected] == hist.counts, hist.metric
    hist = histograms(analysis.report.data)[0]
    k = max(range(len(hist.counts)), key=lambda i: hist.counts[i])
    assert overlay.show(hist, k, worst=True) == hist.counts[k]
    assert len(scene.overlay_layers[QualityOverlay.LAYER]) == 3  # the bin, the worst cells, their markers


def test_large_meshes_keep_mallards_histograms_without_reading_cells(analysis: Analysis, scene: CaseScene) -> None:
    overlay = QualityOverlay(scene)
    overlay.load(analysis.mesh_fields, cells=10**9)
    assert overlay.too_large and overlay.data is None
    hist = histograms(analysis.report.data)[0]
    assert overlay.show(hist, 0, worst=True) == 0
    assert len(scene.overlay_layers[QualityOverlay.LAYER]) == 1  # the worst cells' markers, from the report


def test_initial_state_fields_and_section(analysis: Analysis, scene: CaseScene) -> None:
    overlay = InitialOverlay(scene)
    overlay.load(analysis.initial)
    assert [f["name"] for f in overlay.fields()] == ["RHO", "U", "P", "T"]
    rng = overlay.range("U", 0)
    assert rng is not None and rng[1] == pytest.approx(0.2, rel=1e-6)  # the farfield's u
    overlay.show("U", "Viridis", rng, 0)
    (surface,) = scene.overlay_layers[InitialOverlay.LAYER]
    colors = surface.GetMapper().GetInput().GetCellData().GetScalars()
    assert colors.GetName() == "U" and len({colors.GetTuple(i) for i in range(colors.GetNumberOfTuples())}) > 1
    assert surface.GetMapper().GetInput().GetNumberOfCells() == 1536
    assert surface.GetPosition()[2] > 0  # in front of the 2D mesh's lines, in vtk.js too


def test_panel_fills_from_the_analysis(tmp_path: Path) -> None:
    from trame.app import get_server

    from mallard_gui.session import Session
    from mallard_gui.ui.app import App

    shutil.copy(FIXTURES / "cylinder_input.toml", tmp_path / "input.toml")
    shutil.copy(FIXTURES / "cylinder.msh", tmp_path / "cylinder.msh")
    mallard = FakeMallard()
    app = App(Session(tmp_path, mallard), get_server(name=str(tmp_path), client_type="vue3"))  # type: ignore[arg-type]
    app.ctrl.view_update = lambda: None
    panel = app.analysis
    panel.load(mallard.analyze(tmp_path / "input.toml", tmp_path / "out"))
    s = app.state
    assert s.ana_hists[0]["metric"] == "SKEWNESS"
    assert s.ana_field == "U"  # the first of U, P, T, RHO that varies and s.ana_unassigned == [] and s.ana_yp["max"]
    s.ana_open = True
    panel.set_tab("quality")
    panel.select_bin("SKEWNESS", 7)
    assert s.ana_selection.startswith("256 cells with skewness in [")
    panel.select_bin("SKEWNESS", 7)  # again: deselected
    assert s.ana_bin == -1 and s.ana_selection == ""
    panel.set_tab("initial")
    panel.apply()
    assert s.ana_bar["label"].startswith("U")
    panel.mark_stale()
    assert s.ana_stale


@pytest.mark.mallard
def test_mallard_writes_what_the_overlays_draw(real_mallard: Mallard, cylinder_project: Path) -> None:
    if not real_mallard.capabilities().initial:
        pytest.skip("this mallard predates `check --initial` (PR #1343)")
    a = real_mallard.analyze(cylinder_project / "input.toml", cylinder_project / "analysis")
    assert a.report.data["setup_done"]
    assert a.view and a.mesh_fields and a.initial
    assert all(p.exists() for p in (a.view, a.mesh_fields, a.initial))
    assert histograms(a.report.data)
    overlay = BoundaryOverlay(CaseScene(offscreen=True))
    overlay.load(a.view)
    assert overlay.walls() == ["cylinder"]
