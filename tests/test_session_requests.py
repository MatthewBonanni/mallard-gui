"""Mallard's own answers through its session requests (initialize, units.catalog, file.info, case.files,
case.boundary_values, case.view frames, key forms), and the fallbacks of builds without them."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mallard_gui import units
from mallard_gui.live import settable_keys, setting_of
from mallard_gui.mallard import Capabilities, Mallard, capabilities_from_initialize, parse_catalog
from mallard_gui.project import RunRecord
from mallard_gui.service import MallardService
from mallard_gui.session import Session
from mallard_gui.setup.editor import frame_markers
from mallard_gui.ui.case_files_panel import files_view

from .conftest import FIXTURES, FakeMallard, fixture_json


@pytest.fixture(autouse=True)
def _bundled_units() -> Iterator[None]:
    yield
    units.use_catalog(None)


def test_units_and_their_choices_come_from_mallards_catalog() -> None:
    assert units.use_catalog(fixture_json("unit_catalog.json")) and units.source == "mallard"
    angle = (0, 0, 0, 0, 0, 1)
    assert units.CHOICES[angle] == ("rad", "deg")  # the unit of factor 1 first, as the field stores SI
    assert units.convert("180", units.parse_unit("deg"), units.parse_unit("rad"))[0].startswith("3.14159265")
    assert units.convert("1", units.parse_unit("μm"), units.parse_unit("m")) == ("0.000001", True)
    assert units.CHOICES[(1, 1, -2, 0, 0, 0)] == ("N", "kN", "lbf")


def test_builds_without_a_unit_catalog_keep_the_bundled_table() -> None:
    units.use_catalog(fixture_json("unit_catalog.json"))
    assert not units.use_catalog(None) and units.source == "bundled"
    assert "deg" not in units.UNITS
    assert not units.use_catalog({"schema": "mallard-unit-catalog", "units": "broken"})
    assert units.source == "bundled" and "atm" in units.UNITS


def test_run_controls_and_settings_come_from_initialize() -> None:
    caps = capabilities_from_initialize(fixture_json("initialize.json"), Capabilities(gpus=["A100-40GB"]))
    assert caps.source == "initialize" and caps.gpus == ["A100-40GB"]
    assert caps.control_file("STOP") and caps.control_file("CHECKPOINT") and not caps.control_file("SET")
    assert not caps.set_file and "set" in caps.commands
    keys = settable_keys(caps.settable, {"write_data": [{"prefix": "flow"}, {}], "probes": [{"name": "p1"}]})
    assert "write_data.flow.time_interval" in keys and "write_data.1.interval" in keys
    assert "probes.p1.interval" in keys and "t_stop" in keys
    assert setting_of(caps, "write_data.flow.time_interval")["units"] == "s"
    assert setting_of(caps, "probes.p1.interval")["type"] == "count"


def test_a_build_without_initialize_control_keeps_what_its_help_says() -> None:
    fallback = Capabilities(settable=["t_stop"], set_file=True)
    assert capabilities_from_initialize({"schema": "mallard-capabilities"}, fallback) is fallback
    assert fallback.control_file("SET") and fallback.control_file("STOP")


def test_key_forms_decide_arrays_and_table_rows(cylinder_project: Path) -> None:
    catalog = parse_catalog(
        {
            "keys": {
                "sources": {"form": "array"},
                "sponges": {"type": "array", "form": "array"},
                "regions": {"form": "named"},
                "check": {"form": "table"},
                "check.interval": {"type": "integer"},
            }
        }
    )
    session = Session(cylinder_project, FakeMallard())  # type: ignore[arg-type]
    session.general = catalog
    assert session.is_array("sources")  # no type, no name or region key: only its form says so
    assert not session.is_array("regions") and not session.is_array("check")
    rows = session._table_groups("run", "SETTINGS")
    assert all(r.widget != "toml" or r.placeholder for g in rows for r in g.rows)
    row = session._row("check", None, False)
    assert row.widget == "toml" and row.type == "table" and "TOML table" in row.placeholder
    session.catalog = {**session.catalog, "check": parse_catalog({"keys": {"check": {"set": False}}})["check"]}
    assert session.form("check") == "table"  # a case's catalog lists unset tables without their form


def _stopped_run(project: Path) -> RunRecord:
    run_dir = project / "runs" / "input-1"
    (run_dir / "rst").mkdir(parents=True)
    (run_dir / "rst" / "restart_000005.restart").write_bytes(b"x")
    return RunRecord(
        name="input-1",
        home={"remote": "local", "directory": "runs/input-1"},
        outcome="stopped",
        last_restart={"path": "./rst/restart_000005.restart"},
    )


class InfoMallard(FakeMallard):
    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__()
        self.info = info
        self.asked = 0

    def file_info(self, path: Path) -> dict[str, Any]:
        self.asked += 1
        return self.info


def test_continue_follows_file_info_and_says_why_not(cylinder_project: Path) -> None:
    why = "Restart file rst/restart_000005.restart holds a 2D run, but Mallard was built with Mallard_DIM = 3."
    mallard = InfoMallard({"kind": "restart", "continuable": False, "incompatibilities": [why]})
    session = Session(cylinder_project, mallard)  # type: ignore[arg-type]
    record = _stopped_run(cylinder_project)
    state = session.continue_state(record, live=False)
    assert state == {"can_continue": False, "continue_why": [why], "continue_refused": [why]}
    session.continue_state(record, live=False)
    assert mallard.asked == 1  # asked again only when the restart changes
    mallard.info = {"kind": "restart", "continuable": True, "incompatibilities": []}
    (cylinder_project / "runs/input-1/rst/restart_000005.restart").write_bytes(b"xy")
    assert session.continue_state(record, live=False)["can_continue"]


def test_older_builds_continue_as_before(cylinder_project: Path) -> None:
    session = Session(cylinder_project, FakeMallard())  # type: ignore[arg-type]
    record = _stopped_run(cylinder_project)
    assert session.continue_state(record, live=False)["can_continue"]
    record.outcome = "completed"
    state = session.continue_state(record, live=False)
    assert not state["can_continue"] and state["continue_refused"] == []


def test_case_files_flag_missing_files() -> None:
    result = {
        "schema": "mallard-case-files",
        "version": 1,
        "input": "input.toml",
        "complete": False,
        "files": [
            {"role": "input", "path": "input.toml", "resolved": "/c/input.toml", "exists": True, "bytes": 2000},
            {"role": "mesh", "path": "mesh.msh", "resolved": "/c/mesh.msh", "exists": False, "bytes": None},
        ],
    }
    view = files_view(result)
    assert view["status"] == "missing" and view["summary"] == "2 files · 2 kB · 1 missing"
    assert [f["size"] for f in view["files"]] == ["2 kB", "missing"] and view["note"]
    assert files_view(None)["status"] == "unsupported"


def test_frames_mark_where_u_starts_and_which_way_it_grows() -> None:
    frames = fixture_json("boundary_values/case_view.json")["frames"]
    left = next(f for f in frames if f["zone"] == "left")
    (origin, o), (grows, g) = frame_markers(left, 2)
    assert (origin, o, grows) == ("u = 0", (0.0, 0.0, 0.0), "u grows") and g == pytest.approx((0.0, 0.02, 0.0))
    assert frame_markers({"zone": "ring", "shape": None, "error": "closed curve"}, 2) == []


def test_a_build_without_mallard_session_answers_nothing() -> None:
    service = MallardService([sys.executable, "-c", "import sys; sys.exit(2)"], Path.cwd())
    assert not service.available and not service.has("units.catalog") and service.failure


@pytest.mark.mallard
def test_a_real_session_answers_every_request(real_mallard: Mallard, tmp_path: Path) -> None:
    shutil.copy(FIXTURES / "boundary_values" / "input.toml", tmp_path / "input.toml")
    service = real_mallard.service(tmp_path / "input.toml")
    if not service.has("case.boundary_values"):
        pytest.skip("this mallard has no case.boundary_values")
    assert real_mallard.capabilities().source == "initialize"
    assert units.use_catalog(real_mallard.units_catalog())
    values = real_mallard.boundary_values(tmp_path / "input.toml", None, tmp_path / "bv")
    assert values is not None and [e["condition"] for e in values["entries"]][:2] == ["inlet_state", "inlet_mass_flow"]
    files = real_mallard.case_files(tmp_path / "input.toml")
    assert files is not None and files["files"][0]["role"] == "input"
    edited = (tmp_path / "input.toml").read_text().replace("Nx = 40", "Nx = 20")
    values = real_mallard.boundary_values(tmp_path / "input.toml", edited, tmp_path / "bv2")
    assert values is not None and values["entries"][1]["faces"] == 20  # the editor's text, unsaved
    real_mallard.close()
