"""Vector keys: one box per component (x/y/z, u/v/w for velocities), as many as the case's dimension; the
array is written once every component is valid, expressions only where the key takes them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mallard_gui.session import Session

from .conftest import FakeMallard


def row(s: Session, node: str, path: str) -> dict[str, Any]:
    return next(r for g in s.properties(node)["groups"] for r in g["rows"] if r["path"] == path)


def test_a_velocity_is_a_box_per_component_and_writes_the_array(cylinder_project: Path) -> None:
    s = Session(cylinder_project, FakeMallard())
    u = row(s, "zone:farfield", 'boundaries[name = "farfield"].u')
    assert u["widget"] == "vector" and u["vector"]["labels"] == ["u", "v"]  # a 2D case
    assert u["vector"]["values"] == ["0.2", "0.0"]
    assert s.set_vector('boundaries[name = "farfield"].u', ["0.3", "1e-3"]) == ["", ""]
    assert s.doc.get('boundaries[name = "farfield"].u') == [0.3, 0.001]
    assert s.set_vector('boundaries[name = "farfield"].u', ["0.3", "0.1 * y"]) == ["", ""]  # an expression
    assert s.doc.get('boundaries[name = "farfield"].u') == [0.3, "0.1 * y"]


def test_an_invalid_component_writes_nothing_and_says_which(cylinder_project: Path) -> None:
    s = Session(cylinder_project, FakeMallard())
    before = s.doc.text
    assert s.set_vector("periodic.center", ["1", "x + 1"]) == ["", "a number"]  # a point takes numbers only
    assert s.set_vector("periodic.center", ["", "2"]) == ["needed", ""]
    assert s.doc.text == before
    assert s.set_vector("periodic.center", ["1", "2"]) == ["", ""] and s.doc.get("periodic.center") == [1, 2]
    s.set_vector("periodic.center", ["", ""])  # all empty: back to the default
    assert not s.doc.has("periodic.center")


def test_a_scalar_already_written_keeps_its_field(cylinder_project: Path) -> None:
    s = Session(cylinder_project, FakeMallard())
    s.doc.set('boundaries[name = "cylinder"].u', 0.5)
    assert row(s, "zone:cylinder", 'boundaries[name = "cylinder"].u')["widget"] != "vector"


PARTICLE_KEYS = {
    "particles": {"case": True, "dims": [2, 3]},
    "particles.lattice": {"case": True, "dims": [2, 3]},
    "particles.type": {"case": True, "type": "string", "dims": [2, 3], "values": ["tracer", "solid"]},
    "particles.velocity": {"case": True, "type": "array", "dimension": "velocity (m s^-1)", "dims": [2, 3]},
    "particles.lattice.n": {"case": True, "type": "array", "dims": [2, 3]},
    "particles.lattice.lo": {"case": True, "type": "array", "dimension": "length (m)", "dims": [2, 3]},
    "particles.points": {"case": True, "type": "array", "dimension": "length (m)", "dims": [2, 3]},
}


def particles(cylinder_project: Path) -> Session:
    fake = FakeMallard()
    fake.key_entries = PARTICLE_KEYS
    s = Session(cylinder_project, fake)
    s.doc.set("particles.type", "solid")
    s.doc.set("particles.lattice.lo", [0.0, 0.0])
    s.doc.commit()
    s.refresh_catalog()
    s.check()
    return s


def every(s: Session, node: str) -> dict[str, dict[str, Any]]:
    return {r["path"]: r for g in s.properties(node)["groups"] for r in g["rows"]}


def test_a_particle_velocity_is_u_v_of_numbers_and_lattice_counts_are_integers(cylinder_project: Path) -> None:
    s = particles(cylinder_project)
    rows = every(s, "section:particles")
    assert rows["particles.velocity"]["vector"]["labels"] == ["u", "v"]
    assert s.set_vector("particles.velocity", ["1", "0.5 * y"]) == ["", "a number"]
    assert rows["particles.lattice.n"]["widget"] == "vector"
    assert s.set_vector("particles.lattice.n", ["8", "2.5"]) == ["", "an integer"]
    assert s.set_vector("particles.lattice.n", ["8", "4"]) == ["", ""] and s.doc.get("particles.lattice.n") == [8, 4]


def test_a_list_of_points_is_a_row_of_boxes_per_point(cylinder_project: Path) -> None:
    s = particles(cylinder_project)
    points = every(s, "section:particles")["particles.points"]["points"]
    assert (points["labels"], points["rows"], points["units"]) == (["x", "y"], [], [])  # nondimensional: no unit
    assert s.set_points("particles.points", [["0.1", "0.2"], ["", ""], ["1e-3", "3"]]) == [["", ""], ["", ""]]
    assert s.doc.get("particles.points") == [[0.1, 0.2], [0.001, 3]]  # the blank row dropped
    assert every(s, "section:particles")["particles.points"]["points"]["rows"] == [["0.1", "0.2"], ["0.001", "3"]]
    before = s.doc.text
    assert s.set_points("particles.points", [["0.1", "x"], ["", "1"]]) == [["", "a number"], ["needed", ""]]
    assert s.doc.text == before
    s.set_points("particles.points", [["", ""]])
    assert not s.doc.has("particles.points")
