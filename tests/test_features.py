"""Optional features (chemistry, LES, sponges ...): off unless the input has their table, whatever their keys'
defaults; a switch turns each on (chemistry: after its mechanism) or off (after asking, with undo)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from trame.app import get_server

from mallard_gui.session import Session
from mallard_gui.ui.app import App

from .conftest import FakeMallard

_n = iter(range(10_000))


@pytest.fixture
def app(cylinder_project: Path) -> Iterator[App]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    a = App(Session(cylinder_project, FakeMallard()), get_server(f"features-{next(_n)}", client_type="vue3"))  # type: ignore[arg-type]
    a.ctrl.view_update = lambda: None
    yield a
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


def group(app: App, node: str, gid: str) -> dict[str, Any]:
    return next(g for g in app.inspector.props(node)["groups"] if g["id"] == gid)


def test_a_feature_without_its_table_is_off_and_hides_its_keys(app: App) -> None:
    assert not app.session.doc.has("chemistry")
    chem = group(app, "section:chemistry", "chemistry")
    assert chem["feature"]["on"] is False  # its `enabled` key defaults to true: not what the switch shows
    assert "chemistry.enabled" not in [r["path"] for r in chem["rows"]]


def test_chemistry_turns_on_after_its_mechanism_and_off_after_asking(app: App) -> None:
    root = app.session.input_path.parent
    (root / "h2o2.yaml").write_text("")
    app.inspector.feature_mechanism("chemistry")
    req = app.state.path_request
    assert req["filters"][0]["extensions"] == ["yaml", "yml"]
    app.paths.reply(req["seq"], str(root / "h2o2.yaml"))
    doc = app.session.doc
    assert doc.get("physics.gas") == "mixture" and doc.get("physics.mechanism") == "h2o2.yaml"
    assert doc.get("chemistry") == {} and "\n[chemistry]\n" in doc.text + "\n"
    assert group(app, "section:chemistry", "chemistry")["feature"]["on"] is True
    app.set_key("chemistry.rtol", "1e-8")
    app.inspector.feature("chemistry", False, True)
    assert doc.has("chemistry") and app.state.feature_confirm == "chemistry"  # asked, not done
    app.inspector.feature("chemistry", False, True)
    assert not doc.has("chemistry") and "[chemistry]" not in doc.text and "rtol" not in doc.text
    app.inspector.undo()
    assert app.session.doc.get("chemistry.rtol") == 1e-8


def test_the_enabled_key_is_the_switch(app: App) -> None:
    app.session.doc.set("chemistry", {"enabled": False, "rtol": 1e-7})
    app.session.doc.commit()
    chem = group(app, "section:chemistry", "chemistry")
    assert chem["feature"]["on"] is False and chem["feature"]["key"] == "chemistry.enabled"
    app.session.doc.set("physics.gas", "mixture")
    app.session.doc.set("physics.mechanism", "h2o2.yaml")
    app.inspector.feature("chemistry", True)
    assert app.session.doc.get("chemistry") == {"rtol": 1e-7}  # back to the default: on


LISTS = {"sponges": "sponges", "sources": "sources", "probes": "probes", "triggers": "triggers", "periodic": "periodic"}


def test_a_list_of_entries_has_no_switch_and_its_heads_count_without_a_limit(app: App) -> None:
    """Whether a case has sponges is whether its list has one: a switch that made an entry was the wrong control,
    and "2 of 7 set" read as a limit of 7 entries."""
    for section, table in LISTS.items():
        groups = app.inspector.props(f"section:{section}")["groups"]
        lists = [g for g in groups if g["id"] == table]
        assert lists and not any(g["feature"] for g in lists), section
        assert not any(g["hidden"] for g in groups), section
    for node in app.inspector.props("el:probes[0]")["groups"]:
        assert not node["feature"]
    heads = {n["id"]: n for n in app.state.tree if n["depth"] == 0}
    assert all("feature" not in heads[f"section:{s}"] for s in LISTS if f"section:{s}" in heads)
    stats = group(app, "section:statistics", "statistics")
    assert re.fullmatch(r"\d+ of \d+ options set", stats["note"]) and stats["feature"]["on"] is True
    probes = group(app, "section:probes", "probes")
    assert not re.search(r" of \d", probes["note"])


def test_the_uniform_volume_source_is_one_block_and_keeps_its_switch_beside_the_list(app: App) -> None:
    groups = {g["id"]: g for g in app.inspector.props("section:sources")["groups"]}
    assert groups["source"]["feature"]["name"] == "Body forces" and groups["source"]["feature"]["on"] is False
    assert not groups["sources"]["feature"]
    app.inspector.feature("source", True)
    assert app.session.doc.get("source") == {}
    assert group(app, "section:sources", "source")["feature"]["on"] is True
