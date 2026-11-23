"""The Results plots' axis units follow the run's own input: a nondimensional run's time has no seconds, though
its probe files' units line says s (Mallard #1363)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mallard_gui.data.locations import LocalLocation
from mallard_gui.ui.results_page import ResultsPage


def page_for(run: Path) -> ResultsPage:
    page = ResultsPage.__new__(ResultsPage)
    data = SimpleNamespace(location=lambda where: LocalLocation())
    page.app = SimpleNamespace(session=SimpleNamespace(data=data))  # type: ignore[assignment]
    page._nondimensional = page._run_nondimensional("local", str(run))
    return page


@pytest.mark.parametrize(("units", "label"), [("nondimensional", "t (nd)"), ("SI", "t (s)")])
def test_the_time_axis_has_the_runs_units(tmp_path: Path, units: str, label: str) -> None:
    (tmp_path / "input.toml").write_text(f'[physics]\nunits = "{units}"\n')
    assert page_for(tmp_path)._t_label() == label


def test_a_run_without_its_input_is_taken_as_dimensional(tmp_path: Path) -> None:
    assert page_for(tmp_path)._t_label() == "t (s)"
