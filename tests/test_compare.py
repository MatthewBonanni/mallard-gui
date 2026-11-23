from __future__ import annotations

import json

from mallard_gui.compare import (
    diff_inputs,
    flatten,
    reference_csv,
    resolved_input,
    sectioned,
    series_of,
    series_sources,
)
from mallard_gui.csvseries import CsvSeries
from mallard_gui.runlog import RunState

from .conftest import FIXTURES


def test_arrays_of_tables_line_up_by_name_whatever_their_order() -> None:
    a = {"boundaries": [{"name": "wall", "T": 300.0}, {"name": "inlet", "p": 1.0}]}
    b = {"boundaries": [{"name": "inlet", "p": 2.0}, {"name": "wall", "T": 300.0}]}
    rows = diff_inputs(a, b, changed_only=True)
    assert [(r.key, r.a, r.b, r.status) for r in rows] == [('boundaries[name = "inlet"].p', "1.0", "2.0", "changed")]


def test_unnamed_arrays_by_position_and_added_keys() -> None:
    flat = flatten({"probes": [{"point": [0, 1]}, {"point": [2, 3]}], "run": {"cfl": 1.0}})
    assert flat == {"probes[1].point": "[0, 1]", "probes[2].point": "[2, 3]", "run.cfl": "1.0"}
    rows = diff_inputs({"run": {}}, {"run": {"t_stop": 1.0}})
    assert [(r.key, r.status) for r in rows] == [("run.t_stop", "added")]


def test_resolved_input_from_the_start_record() -> None:
    st = RunState()
    st.add(json.loads((FIXTURES / "cylinder_run.jsonl").read_text().splitlines()[0]))
    data, which = resolved_input(st)
    assert which == "resolved"
    assert data["output"]["run_log"] == "mallard.jsonl"  # a default, absent from the input as written


def test_series_sources_and_values_from_probe_files() -> None:
    st = RunState()
    c = CsvSeries("./solut/forces_cylinder.csv")
    for line in ("step,t,Fx", "# units: 1,s,N", "1,0.1,2.0", "2,0.2,3.0"):
        c.add_line(line)
    st.csv[c.path] = c
    (key,) = series_sources(st)
    assert series_of(st, key) == ([0.1, 0.2], [2.0, 3.0], "N")


def test_reference_csv_with_header_comments_and_gaps() -> None:
    curves, x = reference_csv("# Williamson 1996\nt,Cd,Cl\n0,1.3,0.1\n1,1.4,\n2,1.35,-0.1\n")
    assert x == "t"
    assert [(c.label, list(c.x), list(c.y)) for c in curves] == [
        ("Cd", [0.0, 1.0, 2.0], [1.3, 1.4, 1.35]),
        ("Cl", [0.0, 2.0], [0.1, -0.1]),
    ]
    curves, x = reference_csv("0,1\n1,2\n")
    assert x == "x" and list(curves[0].y) == [1.0, 2.0]


def test_rows_fall_under_a_head_per_table_naming_their_key_within_it() -> None:
    rows = diff_inputs(
        {"mesh": {"type": "file", "n": 1}, "boundaries": [{"name": "w", "type": "wall"}]},
        {"mesh": {"type": "file", "n": 2}, "boundaries": [{"name": "w", "type": "wall"}]},
    )
    got = sectioned(rows)
    heads = [(r["section"], r["changed"], r["total"]) for r in got if "section" in r]
    assert heads == [("boundaries", 0, 2), ("mesh", 1, 2)]
    leaves = [r["leaf"] for r in got if "leaf" in r]
    assert leaves == ['[name = "w"].name', '[name = "w"].type', "n", "type"]
