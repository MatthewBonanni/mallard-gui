from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mallard_gui.mallard import (
    CheckReport,
    KeyInfo,
    Mallard,
    MallardError,
    Superseded,
    find_mallard,
    locate,
    parse_catalog,
    stderr_diagnostics,
)

from .conftest import FIXTURES, fixture_json


def test_catalog_widgets_units_defaults() -> None:
    cat = parse_catalog(fixture_json("cylinder_keys.json"))
    assert cat["boundaries"].set
    assert cat["numerics.face_reconstruction.stencil_factor"].default_value == "2.0"
    assert KeyInfo("a", values=["x", "y"]).widget == "select"
    assert KeyInfo("a", type="bool").widget == "bool"
    assert KeyInfo("a", type="array").widget == "toml"
    assert KeyInfo("a", type="integer").widget == "number"
    assert KeyInfo("a", dimension="pressure (kg m^-1 s^-2)").unit == "kg m^-1 s^-2"
    assert KeyInfo("a", default='"mesh.msh"').default_value == "mesh.msh"
    with pytest.raises(MallardError):
        parse_catalog({"input": None})


def test_locate_key_and_line() -> None:
    key, line, text = locate('bad.toml: line 31: boundaries[name = "farfield"].T: missing; `farfield` needs `T`.')
    assert (key, line) == ('boundaries[name = "farfield"].T', 31)
    assert text.startswith("missing")
    assert locate("DNS: cells of 6.38 eta on average") == (None, None, "DNS: cells of 6.38 eta on average")


def test_check_report_error_located() -> None:
    report = CheckReport.from_json(fixture_json("cylinder_check_error.json"))
    assert report.status == "error"
    assert report.errors == 1
    d = report.diagnostics[0]
    assert (d.code, d.key, d.line) == ("E-INPUT", 'boundaries[name = "farfield"].T', 31)


def test_check_report_unused_key_from_unused_keys() -> None:
    report = CheckReport.from_json(fixture_json("cylinder_check_unused.json"))
    d = next(d for d in report.diagnostics if d.code == "E-KEY-UNUSED")
    assert (d.key, d.line) == ("run.bogus", 13)


def test_check_report_adds_stderr_warnings_and_recounts() -> None:
    stderr = (FIXTURES / "check_stderr.txt").read_text(encoding="utf-8")
    plain = CheckReport.from_json(fixture_json("cylinder_check.json"))
    report = CheckReport.from_json(fixture_json("cylinder_check.json"), stderr)
    extra = [d for d in report.diagnostics if d.source == "stderr"]
    assert [d.code for d in extra] == ["W-FORMAT-VTU"]
    assert extra[0].docs == "docs/guide/running.md#w-format-vtu"
    assert report.warnings == plain.warnings + 1
    assert report.dim == 2
    assert {b["zone"] for b in report.boundaries} == {"cylinder", "farfield"}


def test_stderr_diagnostic_attached_to_its_table() -> None:
    [d] = stderr_diagnostics((FIXTURES / "check_stderr.txt").read_text(encoding="utf-8"))
    assert d.severity == "warning"
    report = CheckReport.from_json(fixture_json("cylinder_check.json"), (FIXTURES / "check_stderr.txt").read_text())
    assert next(d for d in report.diagnostics if d.source == "stderr").section == "write_data"


def test_check_report_rejects_newer_version() -> None:
    data = fixture_json("cylinder_check.json")
    data["version"] = 2
    with pytest.raises(MallardError):
        CheckReport.from_json(data)


def test_find_mallard_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "mallard"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("MALLARD", str(exe))
    assert find_mallard() == exe
    monkeypatch.delenv("MALLARD")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert find_mallard() == exe
    # The [solver] extra's mallard, in the GUI's own environment, comes before PATH's.
    env_bin = tmp_path / "env" / "bin"
    env_bin.mkdir(parents=True)
    (env_bin / "mallard").write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.executable", str(env_bin / "python"))
    assert find_mallard() == env_bin / "mallard"
    with pytest.raises(MallardError):
        find_mallard(str(tmp_path / "missing"))


@pytest.mark.mallard
def test_real_mallard_check_and_keys(real_mallard, cylinder_project: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    assert real_mallard.version()
    catalog, _ = real_mallard.list_keys(cylinder_project / "input.toml")
    assert catalog is not None and catalog["run.t_stop"].set
    report = real_mallard.check(cylinder_project / "input.toml", tmp_path / "view")
    assert report.errors == 0
    assert report.view is not None and report.view.exists()


def test_the_initial_states_input_runs_one_step_and_writes_step_zero_alone(
    cylinder_project: Path, tmp_path: Path
) -> None:
    """For builds whose check does not write the initial state: the case's input with one step, one snapshot of
    the domain at step 0 in place of every output, and its files named in full (the run is made elsewhere)."""
    import tomllib

    from mallard_gui.mallard import initial_input

    text = (cylinder_project / "input.toml").read_text()
    before = tomllib.loads(text)
    doc = tomllib.loads(initial_input(text, cylinder_project, tmp_path / "out" / "initial"))
    assert doc["run"]["n_steps"] == 1 and "t_stop" not in doc["run"]
    assert doc["write_data"] == [
        {"prefix": str(tmp_path / "out" / "initial"), "fields": ["RHO", "U", "P", "T"], "format": "vtu",
         "interval": 1_000_000_000}
    ]  # fmt: skip
    assert not {"probes", "forces", "statistics"} & set(doc)
    assert doc["mesh"]["filename"] == str((cylinder_project / "cylinder.msh").resolve())
    for section in ("initialize", "boundaries", "physics", "numerics"):
        assert doc[section] == before[section]  # the case itself, untouched


@pytest.mark.mallard
def test_mallard_writes_the_initial_state_without_running_the_case(
    real_mallard: Mallard, cylinder_project: Path, tmp_path: Path
) -> None:
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader

    path = real_mallard.initial_state(cylinder_project / "input.toml", tmp_path / "analysis")
    reader = vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(path))
    reader.Update()
    cells = reader.GetOutput().GetCellData()
    assert reader.GetOutput().GetNumberOfCells() == 1536
    lo, hi = cells.GetArray("U").GetRange(-1)
    assert 0 <= lo < 0.05 and 0.15 < hi < 0.3  # still at the wall, 0.2 far away, the kick behind the cylinder
    assert not list(cylinder_project.glob("solut*")) and not list(cylinder_project.glob("*.restart"))  # nothing left


def test_a_superseded_command_stops_at_once_and_says_so(tmp_path: Path) -> None:
    import threading
    import time

    exe = tmp_path / "mallard"
    exe.write_text("#!/bin/sh\nexec sleep 30\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    m = Mallard(exe)
    raised: list[BaseException] = []

    def check() -> None:
        Mallard.group("check")
        try:
            m.run(["check", "input.toml"], cwd=tmp_path)
        except BaseException as e:  # noqa: BLE001  what the thread raised, for the test
            raised.append(e)

    thread = threading.Thread(target=check)
    thread.start()
    time.sleep(0.5)
    m.supersede("other")  # another group's work: this one runs on
    time.sleep(0.2)
    assert thread.is_alive()
    start = time.monotonic()
    m.supersede("check")
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - start < 2
    assert len(raised) == 1 and isinstance(raised[0], Superseded)
