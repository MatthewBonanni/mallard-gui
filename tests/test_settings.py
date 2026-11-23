"""settings.toml: values checked and kept, the old recent-projects file migrated, a broken file never
overwritten; the executable picked by the case's dimension; preferred units for empty fields."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from mallard_gui import recent
from mallard_gui.cli import executables_of
from mallard_gui.executables import Executables
from mallard_gui.session import Session
from mallard_gui.settings import Settings, SettingsError
from mallard_gui.units import field_of

from .conftest import FakeMallard


def test_values_are_checked_saved_and_unknown_keys_kept(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('future = "kept"\n[runs]\nthreads = 4\nqueue = "debug"\n')
    s = Settings(path)
    assert s.threads == 4 and s.theme == "system"
    s.set("appearance", "theme", "dark")
    s.set("units", "length", "mm")
    s.set("runs", "threads", "8")
    with pytest.raises(SettingsError, match="kPa is not a unit of length"):
        s.set("units", "length", "kPa")
    with pytest.raises(SettingsError, match="at least 1"):
        s.set("runs", "threads", 0)
    with pytest.raises(SettingsError, match="no such file"):
        s.set("mallard", "path_2d", str(tmp_path / "missing"))
    data = tomllib.loads(path.read_text())
    assert data["schema"] == "mallard-gui-settings" and data["future"] == "kept"
    assert data["runs"] == {"threads": 8, "queue": "debug"}
    assert data["appearance"] == {"theme": "dark"} and data["units"] == {"length": "mm"}
    s.set("appearance", "theme", "system")  # back to the default: left out of the file
    assert "appearance" not in tomllib.loads(path.read_text())
    assert Settings(path).preferred_units() == {(1, 0, 0, 0, 0, 0): "mm"}


def test_a_broken_file_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text("theme = = dark\n")
    s = Settings(path)
    assert "using the defaults" in s.error and s.theme == "system"
    with pytest.raises(OSError):
        s.set("appearance", "theme", "dark")
    s.add_recent(path)  # dropped, not written
    assert path.read_text() == "theme = = dark\n"


def test_the_old_recent_projects_file_moves_into_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        inputs.append(tmp_path / name / "input.toml")
        inputs[-1].write_text("")
    legacy = tmp_path / "shell" / "recent-projects"
    legacy.parent.mkdir()
    legacy.write_text(f"{inputs[1]}\n{tmp_path / 'gone.toml'}\n{inputs[0]}\n")
    monkeypatch.setenv("MALLARD_GUI_RECENT", str(legacy))
    monkeypatch.setenv("MALLARD_GUI_SETTINGS", str(tmp_path / "settings.toml"))
    assert recent.load() == inputs[::-1]
    assert not legacy.exists()
    legacy.write_text(f"{inputs[0]}\n")  # an older shell writing again: settings.toml wins
    recent.add(inputs[0])
    assert recent.load() == inputs
    assert tomllib.loads((tmp_path / "settings.toml").read_text())["recent"][0] == str(inputs[0].resolve())


def fake_info(dims: dict[str, int]) -> Any:
    calls: list[str] = []

    def info(exe: Path, mesh: Path) -> dict[str, Any] | None:
        calls.append(exe.name)
        return {"dim": dims[exe.name]} if exe.name in dims else None

    info.calls = calls  # type: ignore[attr-defined]
    return info


def test_the_dimension_comes_from_the_mesh_and_picks_its_executable(tmp_path: Path) -> None:
    (tmp_path / "wing.msh").write_text("mesh")
    info = fake_info({"mallard3d-build": 3})  # the front end cannot read this mesh; the 3D build can
    exe = Executables(Path("/opt/mallard"), {2: Path("/b2/mallard2d-build"), 3: Path("/b3/mallard3d-build")}, info)
    assert exe.dimension({"mesh": {"type": "file", "filename": "wing.msh"}}, tmp_path) == 3
    assert exe.dimension({"mesh": {"type": "file", "filename": "wing.msh"}}, tmp_path) == 3
    assert info.calls == ["mallard", "mallard2d-build", "mallard3d-build"]  # cached until the mesh changes
    assert exe.dimension({"mesh": {"type": "box", "Nx": 4, "Lz": 1.0}}, tmp_path) == 3
    assert exe.dimension({"mesh": {"type": "box", "Nx": 4}}, tmp_path) == 2
    assert exe.dimension({"mesh": {"filename": "missing.msh"}}, tmp_path) is None
    assert exe.for_dim(3) == Path("/b3/mallard3d-build")
    assert exe.for_dim(None) == Path("/opt/mallard")
    assert Executables(Path("/opt/mallard"), {2: Path("/b2/m")}).for_dim(3) == Path("/opt/mallard")


def test_a_session_runs_the_executable_of_its_cases_dimension(cylinder_project: Path) -> None:
    exe = Executables(Path("/nonexistent/mallard"), {2: Path("/b2/mallard")}, fake_info({"mallard": 2}))
    session = Session(cylinder_project, FakeMallard(), executables=exe)  # type: ignore[arg-type]
    assert session.dim == 2 and session.mallard.executable == Path("/b2/mallard")
    assert session.backend.mallard == Path("/b2/mallard")
    session.close()


def test_without_a_front_end_a_dimensions_executable_stands_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MALLARD", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    build = tmp_path / "build-3d" / "mallard"
    build.parent.mkdir()
    build.write_text("")
    s = Settings(tmp_path / "settings.toml")
    s.set("mallard", "path_3d", str(build))
    exe = executables_of(s, None)
    assert exe.front == build and exe.by_dim == {3: build}


def test_empty_fields_start_in_the_preferred_unit_and_show_the_default_in_it(cylinder_project: Path) -> None:
    f = field_of(None, "length (m)", False, {(1, 0, 0, 0, 0, 0): "mm"})
    assert f is not None and f.unit == "mm"
    stored = field_of(0.5, "length (m)", False, {(1, 0, 0, 0, 0, 0): "mm"})
    assert stored is not None and (stored.number, stored.unit) == ("500", "mm")  # SI in the input, shown in mm
    path = cylinder_project / "input.toml"
    path.write_text(path.read_text().replace('units = "nondimensional"', 'units = "si"'))
    session = Session(cylinder_project, FakeMallard())  # type: ignore[arg-type]
    session.refresh_catalog()
    session.preferred_units = {(0, 0, 1, 0, 0, 0): "ms"}
    rows = {r["path"]: r for g in session.properties("section:run")["groups"] for r in g["rows"]}
    assert rows["run.t_wall_stop"]["unit_choice"] == "ms"
    assert (rows["run.t_stop"]["number"], rows["run.t_stop"]["unit_choice"]) == ("160000", "ms")  # 160 s
    stats = {r["path"]: r for g in session.properties("section:statistics")["groups"] for r in g["rows"]}
    assert (stats["statistics.phase.t_start"]["unit_choice"], stats["statistics.phase.t_start"]["placeholder"]) == (
        "ms",
        "60000",
    )
    session.close()


def test_pane_sizes_are_kept_in_layout_and_bad_entries_dropped(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text('[layout]\ncase-left = 320\ncase-right = "wide"\nRuns = 9\nruns-right-collapsed = true\n')
    s = Settings(path)
    assert s.layout() == {"case-left": 320, "runs-right-collapsed": True}
    s.set_layout({"case-left": 301.6, "case-problems": 0, "case-problems-collapsed": True, "x-collapsed": 1, "y": True})
    assert tomllib.loads(path.read_text())["layout"] == {"case-left": 302, "case-problems-collapsed": True}
    s.set_layout({})
    assert "layout" not in tomllib.loads(path.read_text())
