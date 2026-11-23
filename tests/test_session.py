from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from mallard_gui.mallard import Mallard
from mallard_gui.session import Session

from .conftest import FakeMallard
from .test_throughput import fake_mallard


def session(project: Path, mallard: FakeMallard | None = None) -> Session:
    s = Session(project, mallard or FakeMallard())  # type: ignore[arg-type]
    s.refresh_catalog()
    s.check()
    return s


def test_tree_sections_zones_and_regions(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    ids = [n["id"] for n in s.tree()]
    assert ids.index("section:mesh") < ids.index("section:boundaries") < ids.index("section:run")
    assert {"zone:cylinder", "zone:farfield"} <= set(ids)
    labels = {n["label"] for n in s.tree() if n["depth"] == 1}
    assert {"near_wake", "wake_point", "wall"} <= labels


def test_the_regions_panel_lists_every_region_of_the_tree_not_none_yet(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    assert "regions" not in s.doc.data  # every region inline or the mesh's own
    tree = [n["id"] for n in s.tree() if n.get("parent") == "section:regions"]
    group = next(g for g in s.properties("section:regions")["groups"] if g["id"] == "regions")
    assert [r["path"] for r in group["rows"]] == tree and group["note"] == f"regions {len(tree)}"
    assert group["add"]["path"] == "regions"


def test_error_badges_block_run_and_show_missing_key(cylinder_project: Path) -> None:
    s = session(cylinder_project, FakeMallard("cylinder_check_error.json"))
    assert s.run_blocked == "1 error"
    nodes = {n["id"]: n for n in s.tree()}
    assert nodes["zone:farfield"]["errors"] == 1
    assert nodes["section:boundaries"]["errors"] == 1
    s.doc.delete('boundaries[name = "farfield"].T')
    rows = [r for g in s.properties("zone:farfield")["groups"] for r in g["rows"]]
    t = next(r for r in rows if r["path"].endswith(".T"))
    assert t["error"]


def test_warnings_never_block(cylinder_project: Path) -> None:
    s = session(cylinder_project, FakeMallard("cylinder_check_unused.json"))
    assert s.run_blocked == "1 error"
    s = session(cylinder_project)
    assert s.report is not None and s.report.warnings > 0
    assert s.run_blocked == ""


def test_set_value_types_by_catalog(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    s.set_value("run.t_stop", "200")
    assert s.doc.data["run"]["t_stop"] == 200
    s.set_value('boundaries[name = "farfield"].T', "")
    assert "T" not in s.doc.data["boundaries"][1]
    with pytest.raises(ValueError):
        s.set_value("run.t_stop", "soon")


def test_assign_zone(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    s.assign_zone("inlet", "farfield")
    assert s.doc.data["boundaries"][-1] == {"name": "inlet", "type": "farfield"}


def test_structure_survives_failing_check(cylinder_project: Path) -> None:
    m = FakeMallard()
    s = session(cylinder_project, m)
    m.check_fixture = "cylinder_check_error.json"
    s.check()
    assert s.structure is not None and s.structure.dim == 2
    assert {z["zone"] for z in s.zones()} == {"cylinder", "farfield"}


def test_region_used_by(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    assert sorted(s.used_by("cylinder")) == ["forces[0].zone", "probes[2].boundary", "write_data[1].region"]
    rows = [r for g in s.properties("region:cylinder")["groups"] if g["title"] == "USED BY" for r in g["rows"]]
    assert len(rows) == 3


def test_every_catalog_section_is_in_the_tree(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    ids = {n["id"] for n in s.tree()}
    assert {"section:sponges", "section:sources", "section:chemistry", "section:triggers"} <= ids
    assert s.is_array("sponges") and s.is_array("sources") and not s.is_array("chemistry")
    groups = {g["id"]: g for g in s.properties("section:chemistry")["groups"]}
    assert {"rtol", "sparse"} <= {r["label"] for r in groups["chemistry"]["rows"]}
    # Every section opens, those of named entries too (the general catalog's `regions.<name>`).
    for n in s.tree():
        s.properties(n["id"])


def test_boundary_zones_pick_their_coordinate_points(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    groups = {g["title"]: g["rows"] for g in s.properties("zone:farfield")["groups"]}
    assert [(r["label"], r["widget"]) for r in groups["COORDINATES"]] == [("origin", "pick"), ("toward", "pick")]
    assert all(r["label"] not in ("origin", "toward") for r in groups["CONDITION"])


def dimensional_text(project: Path) -> Path:
    path = project / "input.toml"
    path.write_text(path.read_text().replace('units = "nondimensional"\n', ""))
    return project


def dimensional(project: Path) -> Session:
    return session(dimensional_text(project))


def test_quantities_are_written_in_si_and_shown_in_the_unit_last_typed_or_chosen(cylinder_project: Path) -> None:
    s = dimensional(cylinder_project)

    def row() -> dict[str, Any]:
        return next(r for g in s.properties("section:run")["groups"] for r in g["rows"] if r["label"] == "t_stop")

    s.set_value("run.t_stop", "2 ms", "s")
    assert s.doc.data["run"]["t_stop"] == 0.002
    assert (row()["widget"], row()["number"], row()["unit_choice"]) == ("quantity", "2", "ms")
    assert "s" in row()["unit_options"]
    s.set_value("run.t_stop", "3", "ms")  # a plain number in the unit shown
    assert s.doc.get("run.t_stop") == 0.003
    s.show_unit("run.t_stop", "s")  # the selector: the input keeps its value
    assert s.doc.get("run.t_stop") == 0.003 and (row()["number"], row()["unit_choice"]) == ("0.003", "s")
    with pytest.raises(ValueError, match="not a unit of time"):
        s.set_value("run.t_stop", "3 Pa", "s")
    with pytest.raises(ValueError, match="not a unit of time"):
        s.show_unit("run.t_stop", "Pa")


def test_nondimensional_cases_refuse_units(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    with pytest.raises(ValueError, match="nondimensional"):
        s.set_value("run.t_stop", "2 ms", "")
    row = next(r for g in s.properties("section:run")["groups"] for r in g["rows"] if r["label"] == "t_stop")
    assert row["unit"] == "" and row["unit_options"] == []


def test_a_measured_throughput_sizes_every_later_check(cylinder_project: Path, tmp_path: Path) -> None:
    fake = FakeMallard()
    s = session(cylinder_project, fake)
    s.check()
    assert fake.extra == []
    assert s.preload_throughput().kind == "estimated" and s.launch.throughput > 0
    fake.executable = fake_mallard(tmp_path, [2e5] + [8e5] * 20)
    s.launch.ranks = 4
    result = s.measure_throughput(lambda summary, n: None, threading.Event())
    assert result.kind == "measured" and s.launch.throughput == 8e5
    s.check()
    assert fake.extra == ["--ranks", "4", "--throughput", "800000"]


def test_diagnostics_without_a_key_link_to_the_keys_their_text_names(cylinder_project: Path) -> None:
    """A build before structured keys (PR #1377): the GUI finds the key in the message, among the catalog's keys."""
    from mallard_gui.mallard import CheckReport

    from .conftest import fixture_json

    s = session(cylinder_project)
    data = fixture_json("cylinder_check.json")
    data["diagnostics"].append(
        {
            "severity": "warning",
            "code": "W-CFL-LIMIT",
            "message": "CFL 1.5 at the initial state is above 1.2, the stability limit of SSPRK3 for smooth flow: "
            "lower run.cfl (or run.dt)",
            "docs": "docs/guide/check.md#w-cfl-limit",
        }
    )
    report = CheckReport.from_json(data)
    s.link_diagnostics(report)
    dns, cfl = report.diagnostics
    assert (cfl.key, cfl.refs) == ("run.cfl", ("run.cfl", "run.dt"))
    assert s.node_of(cfl) == "section:run"
    assert dns.key is None and dns.refs == ("les",)  # a section named as advice is a link, not the warning's key
    assert s.node_of(dns) is None and s.link_of(dns) == "section:les"
    assert "section:les" in {n["id"] for n in s.tree()}


def test_a_check_of_unsaved_edits_names_the_input_not_the_recovery_file(
    cylinder_project: Path, real_mallard: Mallard
) -> None:
    """`mallard check` reads unsaved edits from the hidden recovery file and names it in its messages; the user
    knows the input they edit."""
    s = Session(cylinder_project, real_mallard)
    s.doc.set("run.t_stop", "abc")
    s.doc.commit()
    assert s.checked_path != s.input_path
    s.check()
    report = s.report
    assert report is not None
    texts = [d.message for d in report.diagnostics] + [d["message"] for d in report.data["diagnostics"]]
    assert any(t.startswith("input.toml: line") for t in texts), texts
    assert not any(s.checked_path.name in t for t in texts), texts


def test_a_vector_has_one_unit_for_its_components_from_the_reference_where_the_catalog_has_none(
    cylinder_project: Path,
) -> None:
    s = dimensional(cylinder_project)
    path = 'boundaries[name = "farfield"].u'
    assert s.general["boundaries.u"].dimension is None  # the catalog does not say: the Units table does

    def vector() -> dict[str, Any]:
        rows = (r for g in s.properties("zone:farfield")["groups"] for r in g["rows"])
        return next(r for r in rows if r["path"] == path)["vector"]

    assert (vector()["unit"], vector()["values"]) == ("m/s", ["0.2", "0.0"]) and "km/h" in vector()["units"]
    assert s.set_vector(path, ["72", "0"], "km/h") == ["", ""]
    assert s.doc.get(path) == [20, 0]
    assert (vector()["unit"], vector()["values"]) == ("km/h", ["72", "0"])
    assert s.set_vector(path, ["10 ft/s", "0"], "km/h") == ["", ""]  # a unit typed in a box
    assert s.doc.get(path) == [3.048, 0] and vector()["unit"] == "ft/s"
    assert s.set_vector(path, ["3 K", "0"], "m/s")[0].startswith("K is not a unit of velocity")


def test_a_key_whose_unit_depends_on_another_says_so_and_angles_stay_degrees(cylinder_project: Path) -> None:
    fake = FakeMallard()
    fake.key_entries = {"statistics.phase.level": {"type": "number"}}  # a catalog that does not resolve it
    s = session(dimensional_text(cylinder_project), fake)
    rows = {r["path"]: r for g in s.properties("section:statistics")["groups"] for r in g["rows"]}
    level = rows["statistics.phase.level"]
    assert "variable" in level["unit_unknown"] and level["widget"] != "quantity" and not level["unit_options"]
    assert rows["statistics.phase.period"]["unit_unknown"] == ""
    sutherland = s.info("physics.sutherland_S")  # the catalog's temperature would offer degC for a difference
    assert sutherland is not None and sutherland.dimension is None
    rows = {r["path"]: r for g in s.properties("section:physics")["groups"] for r in g["rows"]}
    if "physics.sutherland_S" in rows:
        assert rows["physics.sutherland_S"]["unit"] == "K" and rows["physics.sutherland_S"]["widget"] != "quantity"
