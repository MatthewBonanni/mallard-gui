"""Saved views round-trip through the committed project file and leave its other keys alone."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mallard_gui.project import Project
from mallard_gui.views import SavedView, delete_view, save_view, views


def test_views_round_trip_through_the_project_file(cylinder_project: Path) -> None:
    project = Project.open(cylinder_project)
    view = SavedView(
        "wake at t 40",
        run="input-1",
        series="solut/cylinder",
        step=4000,
        t=40.0,
        zones=["solut/wall"],
        field="U",
        component="x",
        colormap="Cool to Warm",
        range=[-0.2, 1.2],
        camera={"position": [0.0, 0.0, 5.0], "focal_point": [0.0, 0.0, 0.0], "view_up": [0.0, 1.0, 0.0]},
        probe=[2.0, 0.5, 0.0],
        plot="spectrum",
    )
    save_view(project, view)
    data = tomllib.loads((cylinder_project / "mallard-project.toml").read_text())
    assert data["inputs"] == ["input.toml"]
    assert data["views"]["wake at t 40"]["colormap"] == "Cool to Warm"
    assert "line" not in data["views"]["wake at t 40"]  # empty values are left out
    (again,) = views(Project.open(cylinder_project))
    assert again == view
    delete_view(Project.open(cylinder_project), "wake at t 40")
    assert views(Project.open(cylinder_project)) == []


def test_bad_names_and_unreadable_views() -> None:
    with pytest.raises(ValueError, match="view name"):
        SavedView("../x", run="r", series="s")
    with pytest.raises(ValueError, match="plot"):
        SavedView("v", run="r", series="s", plot="pie")


def test_a_view_saved_with_a_former_colormap_name_opens_with_its_paraview_preset() -> None:
    assert SavedView.from_dict("v", {"run": "r", "series": "s", "colormap": "vik"}).colormap == "Cool to Warm"
    assert SavedView.from_dict("v", {"run": "r", "series": "s", "colormap": "batlow"}).colormap == "Fast"
