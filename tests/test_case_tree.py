"""The case tree under the input reference's headings, a node's lines in the input (the text tab scrolls to them and
the caret's line selects a node), and Check showing its report once done."""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from trame.app import get_server

from mallard_gui.input_doc import InputDocument
from mallard_gui.mallard import MallardError
from mallard_gui.session import Session
from mallard_gui.ui.app import App

from .conftest import FakeMallard

_names = itertools.count()


def session(project: Path, mallard: FakeMallard | None = None) -> Session:
    s = Session(project, mallard or FakeMallard())  # type: ignore[arg-type]
    s.refresh_catalog()
    s.check()
    return s


def line_of(project: Path, text: str) -> int:
    return (project / "input.toml").read_text().splitlines().index(text) + 1


# The tree's order and headings


def test_sections_follow_the_setup_of_a_case_under_headings(cylinder_project: Path) -> None:
    heads = [n for n in session(cylinder_project).tree() if n["depth"] == 0]
    groups = [n["group"] for n in heads if n["group_head"]]
    assert groups == ["Geometry", "Physics", "Conditions", "Solver", "Output", "Execution"]
    order = [n["id"].removeprefix("section:") for n in heads]
    flow = ["mesh", "physics", "chemistry", "les", "initialize", "boundaries", "regions", "numerics", "run", "output"]
    flow += ["probes", "statistics", "parallel"]
    assert [s for s in order if s in flow] == flow
    assert {n["group"] for n in heads if n["id"] in ("section:chemistry", "section:les")} == {"Physics"}
    assert next(n for n in heads if n["id"] == "section:run")["group"] == "Solver"


def test_check_is_no_section_and_unset_sections_stay_in_their_group(cylinder_project: Path) -> None:
    tree = session(cylinder_project).tree()
    assert "section:check" not in {n["id"] for n in tree}
    nodes = {n["id"]: n for n in tree}
    assert not nodes["section:chemistry"]["present"] and nodes["section:physics"]["present"]
    assert not nodes["section:parallel"]["present"]
    assert not [n for n in tree if n.get("group") == "Other"]  # nothing piled at the end


def test_children_name_their_section(cylinder_project: Path) -> None:
    tree = session(cylinder_project).tree()
    assert {n["parent"] for n in tree if n["id"].startswith("zone:")} == {"section:boundaries"}
    assert {n["parent"] for n in tree if n["id"].startswith("el:probes")} == {"section:probes"}


# A node's lines


def test_a_tables_span_is_its_header_and_body_with_its_subtables(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    first = line_of(cylinder_project, "[statistics]")
    assert s.spans_of("section:statistics") == [(first, line_of(cylinder_project, "level = 0.0"))]
    numerics = s.spans_of("section:numerics")
    assert numerics[0][0] == line_of(cylinder_project, "[numerics]")
    assert numerics[0][1] > line_of(cylinder_project, "[numerics.face_reconstruction]")


def test_an_array_of_tables_element_maps_to_its_own_header(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    headers = [i + 1 for i, x in enumerate((cylinder_project / "input.toml").read_text().splitlines())]
    text = (cylinder_project / "input.toml").read_text().splitlines()
    write_data = [n for n, x in zip(headers, text, strict=True) if x == "[[write_data]]"]
    ((a, b),) = s.spans_of("el:write_data[2]")
    assert a == write_data[2] and b < write_data[3]
    ((z, _),) = s.spans_of("zone:farfield")
    assert text[z - 1] == "[[boundaries]]" and text[z] == 'name = "farfield"'


def test_another_table_between_elements_splits_the_span(cylinder_project: Path) -> None:
    spans = session(cylinder_project).spans_of("section:output")
    assert len(spans) == 2  # [[write_data]] after [statistics] is a range of its own
    assert spans[1][0] == max(
        i + 1 for i, x in enumerate((cylinder_project / "input.toml").read_text().splitlines()) if x == "[[write_data]]"
    )


def test_a_key_spans_its_value_lines(cylinder_project: Path) -> None:
    doc = InputDocument(cylinder_project / "input.toml")
    u = line_of(cylinder_project, 'u = ["0.2 * (1 - exp(-4 * (x^2 + y^2 - 0.25)))",')
    assert doc.spans_of(["initialize.u"]) == [(u, u + 1)]


def test_an_unset_section_has_no_lines(cylinder_project: Path) -> None:
    assert session(cylinder_project).spans_of("section:chemistry") == []


def test_the_caret_line_names_its_node(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    assert s.node_at_line(line_of(cylinder_project, "[numerics.face_reconstruction]") + 1) == "section:numerics"
    assert s.node_at_line(line_of(cylinder_project, 'name = "farfield"')) == "zone:farfield"
    assert s.node_at_line(line_of(cylinder_project, 'name = "wake_arc"')) == "el:probes[1]"
    u = line_of(cylinder_project, 'u = ["0.2 * (1 - exp(-4 * (x^2 + y^2 - 0.25)))",')
    assert s.node_at_line(u + 1) == "section:initialize"  # the second line of a value


# The app


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    asyncio.set_event_loop(None)


def make_app(project: Path, mallard: FakeMallard | None = None) -> App:
    server = get_server(f"tree-{next(_names)}", client_type="vue3")
    a = App(Session(project, mallard or FakeMallard()), server)  # type: ignore[arg-type]
    a.ctrl.view_update = lambda: None
    return a


def test_selecting_a_node_reveals_its_lines_without_switching_tab(
    cylinder_project: Path, loop: asyncio.AbstractEventLoop
) -> None:
    app = make_app(cylinder_project)
    app.state.tab = "view"
    app.select("section:statistics")
    assert app.state.doc_reveal["spans"][0][0] == line_of(cylinder_project, "[statistics]")
    assert app.state.tab == "view"


def test_the_caret_selects_its_node_only_in_the_documents_text(
    cylinder_project: Path, loop: asyncio.AbstractEventLoop
) -> None:
    app = make_app(cylinder_project)
    app._refresh_case()
    seq = app.state.doc_reveal.get("seq")
    app.select_line(line_of(cylinder_project, "[numerics]"))
    assert app.state.selected == "section:numerics"
    assert app.state.doc_reveal.get("seq") == seq  # no scrolling back from where the caret is
    app.state.toml_text = app.session.doc.text + "\n# typing"
    app.select_line(line_of(cylinder_project, "[run]"))
    assert app.state.selected == "section:numerics"


class GatedMallard(FakeMallard):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.fail = fail

    def check(self, *args: Any, **kwargs: Any) -> Any:
        if not self.gate.wait(10):
            raise TimeoutError
        if self.fail:
            raise MallardError("mallard: cannot execute")
        return super().check(*args, **kwargs)


def test_check_shows_its_report_once_done_wherever_the_user_went(
    cylinder_project: Path, loop: asyncio.AbstractEventLoop
) -> None:
    fake = GatedMallard()
    app = make_app(cylinder_project, fake)
    app.state.tab = "toml"
    task = loop.create_task(app.check_and_show())
    loop.run_until_complete(asyncio.sleep(0.2))
    assert app.state.check["running"] and app.state.tab == "toml"
    app.set_page("runs")
    fake.gate.set()
    loop.run_until_complete(task)
    assert (app.state.page, app.state.tab) == ("case", "report")
    assert app.state.check["status"] and not app.state.check_error


def test_a_check_that_cannot_run_says_so_in_the_report(cylinder_project: Path, loop: asyncio.AbstractEventLoop) -> None:
    fake = GatedMallard(fail=True)
    fake.gate.set()
    app = make_app(cylinder_project, fake)
    loop.run_until_complete(app.check_and_show())
    assert app.state.tab == "report"
    assert app.state.check["status"] == "failed" and not app.state.check["running"]
    assert "cannot execute" in app.state.check_error
