from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mallard_gui.input_doc import InputDocument, format_path, format_value, parse_path, parse_value

from .conftest import FIXTURES

TEXT = (FIXTURES / "cylinder_input.toml").read_text(encoding="utf-8")


@pytest.fixture
def doc(tmp_path: Path) -> InputDocument:
    path = tmp_path / "input.toml"
    path.write_text(TEXT, encoding="utf-8")
    return InputDocument(path)


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    a, b = before.splitlines(), after.splitlines()
    assert len(a) == len(b), "line count changed"
    return [(x, y) for x, y in zip(a, b, strict=True) if x != y]


def test_parse_path_indices_and_selectors() -> None:
    assert parse_path('boundaries[name = "far field"].u[1]') == ["boundaries", ("name", "far field"), "u", 1]
    assert parse_path("numerics.face_reconstruction.order") == ["numerics", "face_reconstruction", "order"]
    assert format_path(parse_path('boundaries[name = "x"].T')) == 'boundaries[name = "x"].T'
    with pytest.raises(ValueError):
        parse_path("a..b")


def test_resolve_selector_to_index(doc: InputDocument) -> None:
    assert doc.resolve('boundaries[name = "farfield"].T') == ["boundaries", 1, "T"]
    assert doc.get('probes[name = "wall"].boundary') == "cylinder"
    with pytest.raises(KeyError):
        doc.resolve('boundaries[name = "nope"].T')


def test_set_scalar_changes_only_that_value_and_keeps_comment(doc: InputDocument) -> None:
    doc.set("run.t_stop", 200.0)
    assert changed_lines(TEXT, doc.text) == [("t_stop = 160.0", "t_stop = 200.0")]
    doc.set("output.check_interval", 10)
    assert tomllib.loads(doc.text)["output"]["check_interval"] == 10


def test_set_through_selector_in_split_array_of_tables(doc: InputDocument) -> None:
    # [[write_data]] is split by [[probes]] and [statistics]; tomlkit's own dump regroups it.
    doc.set('write_data[prefix = "./solut/phase"].time_interval', 50.0)
    assert changed_lines(TEXT, doc.text) == [("time_interval = 100.0", "time_interval = 50.0")]


def test_set_inside_inline_value(doc: InputDocument) -> None:
    doc.set("output.reference.rho", 2.0)
    data = tomllib.loads(doc.text)
    assert data["output"]["reference"] == {"rho": 2.0, "u": [0.2, 0.0], "p": 0.7142857142857143}
    assert "# The free stream of CP and CF" in doc.text


def test_set_multiline_value_replaced_whole(doc: InputDocument) -> None:
    doc.set("initialize.u", ["0.2", "0.0"])
    data = tomllib.loads(doc.text)
    assert data["initialize"]["u"] == ["0.2", "0.0"]
    assert data["initialize"]["p"] == "0.7142857142857143"
    assert "# A transverse kick behind the cylinder" in doc.text


def test_insert_new_key_into_existing_and_new_tables(doc: InputDocument) -> None:
    doc.set("run.control_file_interval", 5)
    doc.set("les.model", "smagorinsky")
    data = tomllib.loads(doc.text)
    assert data["run"] == {"t_stop": 160.0, "cfl": 1.0, "control_file_interval": 5}
    assert data["les"] == {"model": "smagorinsky"}
    doc.set('boundaries[name = "cylinder"].T', 1.0)
    assert tomllib.loads(doc.text)["boundaries"][0]["T"] == 1.0


def test_delete_then_reinsert_round_trips(doc: InputDocument) -> None:
    path = 'boundaries[name = "farfield"].T'
    doc.delete(path)
    assert not doc.has(path)
    doc.set(path, 0.7142857142857143)
    assert doc.text == TEXT


def test_append_element_after_last_of_its_array(doc: InputDocument) -> None:
    index = doc.append_element("boundaries", {"name": "inlet", "type": "farfield"})
    assert index == 2
    assert [e.label for e in doc.elements("boundaries")] == ["cylinder", "farfield", "inlet"]
    lines = doc.text.splitlines()
    assert lines.index("[[boundaries]]", lines.index("T = 0.7142857142857143") - 6) < lines.index('name = "inlet"')
    assert lines.index('name = "inlet"') < lines.index("[numerics]")


def test_save_writes_and_dirty_tracks(doc: InputDocument) -> None:
    doc.set("run.cfl", 0.5)
    assert doc.dirty
    doc.save()
    assert not doc.dirty
    assert InputDocument(doc.path).get("run.cfl") == 0.5


def test_line_of(doc: InputDocument) -> None:
    lines = TEXT.splitlines()
    assert lines[doc.line_of('boundaries[name = "farfield"].T') - 1] == "T = 0.7142857142857143"  # type: ignore[operator]
    assert lines[doc.line_of("statistics.phase") - 1] == "[statistics.phase]"  # type: ignore[operator]


@pytest.mark.parametrize(
    ("text", "type_", "value"),
    [
        ("1e-3", "number", 1e-3),
        ("abc", "string", "abc"),
        ('"a b"', None, "a b"),
        ("[1, 2]", "array", [1, 2]),
        ("true", "bool", True),
        ("plain words", None, "plain words"),
    ],
)
def test_parse_value(text: str, type_: str | None, value: object) -> None:
    assert parse_value(text, type_) == value


@pytest.mark.parametrize(("text", "type_"), [("abc", "number"), ("1.5", "integer"), ("yes", "bool")])
def test_parse_value_rejects_wrong_type(text: str, type_: str) -> None:
    with pytest.raises(ValueError):
        parse_value(text, type_)


def test_format_value_inline_table() -> None:
    assert tomllib.loads("v = " + format_value({"a": 1, "b": [1.0, "x"]}))["v"] == {"a": 1, "b": [1.0, "x"]}
