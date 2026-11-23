"""The check report's structured diagnostics (`key`, `line`, `column`, Mallard PR #1377) and its `unassigned` and
`periodic` members (PR #1378), from reports captured from that build (`tests/fixtures/check_keys`: each input
and its `mallard check --format json`)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from mallard_gui.analysis.report import periodic_pairs
from mallard_gui.checkinfo import summarize
from mallard_gui.mallard import CheckReport
from mallard_gui.session import Session
from mallard_gui.ui.analysis_panel import _periodic_row

from .conftest import FIXTURES, FakeMallard, fixture_json

CASES = FIXTURES / "check_keys"


def case(tmp_path: Path, name: str) -> Session:
    shutil.copy(CASES / f"{name}.toml", tmp_path / "input.toml")
    shutil.copy(FIXTURES / "cylinder.msh", tmp_path / "cylinder.msh")
    s = Session(tmp_path, FakeMallard(check=f"check_keys/{name}.json"))  # type: ignore[arg-type]
    s.refresh_catalog()
    s.check()
    return s


def report(name: str) -> dict[str, Any]:
    return dict(fixture_json(f"check_keys/{name}.json"))


def test_structured_fields_are_taken_as_given_and_stderr_is_not_read() -> None:
    stderr = "warning W-PROBE-OUTSIDE: probes[0].point: outside the mesh\n  docs/guide/running.md#w-probe-outside\n"
    r = CheckReport.from_json(report("writers"), stderr)
    assert [(d.code, d.key, d.line, d.column) for d in r.diagnostics[:3]] == [
        ("E-INPUT", "write_data[0].time_interval", 70, 17),
        ("E-INPUT", "write_data[1].time_interval", 76, 17),
        ("W-FORMAT-VTU", "write_data", 68, 1),
    ]
    assert all(d.structured and d.source == "check" for d in r.diagnostics)
    syntax = CheckReport.from_json(report("syntax")).diagnostics[0]
    assert (syntax.key, syntax.line, syntax.column) == (None, 16, 25)


def test_every_error_reported_at_once_links_to_its_element(tmp_path: Path) -> None:
    s = case(tmp_path, "writers")
    assert s.run_blocked == "5 errors"
    first, second, vtu = s.diagnostics[:3]
    assert s.node_of(first) == "el:write_data[0]" and s.node_of(second) == "el:write_data[1]"
    assert s.node_of(vtu) == "section:output"
    assert s.diagnostic_for("write_data[1].time_interval") is second
    tree = {n["id"]: n for n in s.tree()}
    assert tree["el:write_data[0]"]["errors"] == tree["el:write_data[1]"]["errors"] == 1


def test_keys_by_index_and_by_name_reach_the_zone(tmp_path: Path) -> None:
    s = case(tmp_path, "boundaries")
    bad_type, missing_t = s.diagnostics
    assert s.node_of(bad_type) == "zone:cylinder" and s.node_of(missing_t) == "zone:farfield"
    rows = {r["label"]: r for g in s.properties("zone:farfield")["groups"] for r in g["rows"]}
    assert rows["T"]["severity"] == "error" and not rows["T"]["set"]


def test_a_structured_key_is_never_widened_by_the_message_text(tmp_path: Path) -> None:
    """The CFL warning's text also names `run.dt`, and the DNS warning's `[les]`: a build that gives keys is
    taken at its word, so neither becomes a link."""
    s = case(tmp_path, "warnings")
    vtu, dns, cfl = s.diagnostics
    assert (cfl.key, cfl.refs) == ("run.cfl", ("run.cfl",))
    assert (dns.key, dns.refs) == (None, ())
    assert s.link_of(dns) is None
    assert vtu.severity == "warning" and vtu.key == "write_data"


@pytest.mark.parametrize(("name", "zone", "faces"), [("unassigned", "farfield", 64), ("unassigned_3d", "top", 32)])
def test_zones_without_a_condition_are_rows_and_tree_nodes(tmp_path: Path, name: str, zone: str, faces: int) -> None:
    data = report(name)
    rows = {r.k: r for r in summarize(data).rows["BOUNDARIES"]}
    assert (rows[zone].v, rows[zone].status, rows[zone].note, rows[zone].node) == (
        "no condition",
        "error",
        f"{faces} faces",
        f"zone:{zone}",
    )
    if name == "unassigned":  # the cylinder's mesh: the session's case
        s = case(tmp_path, name)
        assert f"zone:{zone}" in {n["id"] for n in s.tree()}
        [row] = s.properties(f"zone:{zone}")["groups"][0]["rows"]
        assert row["widget"] == "assign"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("periodic", {"left / right": "translation (2, 0) · 8 faces"}),
        ("rotation", {"start / end": "rotation 45° about (0, 0) · 4 faces"}),
        ("periodic_3d", {"left / right": "translation (2, 0, 0) · 16 faces",
                         "back / front": "translation (0, 0, 1) · 32 faces"}),
    ],
)  # fmt: skip
def test_periodic_pairs_as_mallard_joined_them(name: str, expected: dict[str, str]) -> None:
    rows = [r for r in summarize(report(name)).rows["BOUNDARIES"] if r.v == "periodic"]
    assert {r.k: r.note for r in rows} == {k: f"{v} · nodes off by 0 of the shortest edge" for k, v in expected.items()}
    assert all(r.status == "ok" for r in rows)


def test_periodic_node_offsets_omitted_on_a_distributed_mesh() -> None:
    data = report("periodic")
    for p in data["periodic"]:
        del p["node_offset_max"], p["node_offset_relative"]
    [pair] = periodic_pairs(data)
    assert pair.node_offset_max is None and pair.matches
    [row] = [r for r in summarize(data).rows["BOUNDARIES"] if r.v == "periodic"]
    assert row.status == "ok" and row.note.endswith("node offsets not measured (a mesh read in parallel)")
    assert "not measured" in _periodic_row(pair)["detail"]
