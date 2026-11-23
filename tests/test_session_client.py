"""The GUI against `mallard session`'s protocol (a stand-in): initialize with the schemas it reads, a
progressive cut written next to the data then drawn by the viewer, picks, errors and shutdown."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mallard_gui.session_client import QueryService, SessionClient, SessionError
from mallard_gui.viewer.engine import ResultScene

from .conftest import FIXTURES
from .runs import make_run


@pytest.fixture
def run(tmp_path: Path) -> Path:
    return make_run(tmp_path / "run", steps=1, n=20)


def start(run: Path) -> SessionClient:
    mesh = run / "solut" / "box_000000.vtu"
    return SessionClient.start([sys.executable, str(FIXTURES / "fake_session.py"), str(mesh)], cwd=str(run))


def test_progressive_cut_then_drawn_by_the_viewer(run: Path) -> None:
    client = start(run)
    try:
        assert client.capabilities["schema"] == "mallard-capabilities"
        query = QueryService(client, str(run / "extract" / "cuts"))
        with pytest.raises(SessionError, match="no case") as e:
            next(query.cut([0.5, 0.5, 0.5], [1, 0, 0]))
        assert e.value.code == -32001
        query.open("input.toml")
        progress: list[dict[str, object]] = []
        client.progress = progress.append
        cuts = list(query.cut([0.5, 0.5, 0.5], [1, 0, 0], targets=(100, 0)))
        assert [c.target for c in cuts] == [100, 0]
        assert cuts[0].triangles <= 110 < cuts[1].triangles == 2 * 20 * 20
        assert progress and progress[0]["phase"] == "cut"
        pick = query.pick([0.51, 0.51, 0.51])
        assert pick.zone == "domain" and pick.center == pytest.approx([0.525, 0.525, 0.525])
        with pytest.raises(SessionError, match="outside"):
            query.pick([5, 5, 5])
        scene = ResultScene(run.parent)
        scene.open(run.name)
        scene.show("solut/box", 0)
        rel = str(Path(cuts[1].file).relative_to(run))
        assert scene.overlay(rel)["cells"] == 800
        assert "slice" in scene.blocks and scene.blocks["slice"].label == "cut"
    finally:
        client.close()
    assert client.proc.returncode == 0


def test_stopping_a_progressive_cut_cancels_the_request_in_flight(run: Path) -> None:
    client = start(run)
    try:
        query = QueryService(client, str(run / "extract" / "cuts"))
        query.open("input.toml")
        cuts = query.cut([0.5, 0.5, 0.5], [0, 0, 1], targets=(50, 0))
        first = next(cuts)
        assert first.target == 50
        cuts.close()  # the plane moved: no full cut
        assert query.pick([0.5, 0.5, 0.5]).id >= 0  # the session still answers, in order
    finally:
        client.close()
