"""Case setup in the app, without a browser: the trame state the setup pieces drive after a check, a selection,
a picked point, a palette choice and the launch dialog."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from trame.app import get_server

from mallard_gui.mallard import CheckReport
from mallard_gui.session import Session
from mallard_gui.throughput import Throughput
from mallard_gui.ui.app import App

from .conftest import FIXTURES, FakeMallard

_names = itertools.count()


class ViewMallard(FakeMallard):
    """The fake with its captured case view, as `check --view` gives it."""

    def check(
        self, input_file: Path, view_dir: Path | None = None, extra: Sequence[str] = (), catalog: Path | None = None
    ) -> CheckReport:
        report = super().check(input_file, view_dir, extra, catalog)
        report.view = FIXTURES / "cylinder_view" / "case.vtm"
        return report


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


@pytest.fixture
def app(cylinder_project: Path, loop: asyncio.AbstractEventLoop) -> App:
    server = get_server(f"setup-{next(_names)}", client_type="vue3")
    a = App(Session(cylinder_project, ViewMallard()), server)  # type: ignore[arg-type]
    a.ctrl.view_update = lambda: None
    loop.run_until_complete(a.run_check())
    return a


def test_inflow_arrows_are_off_until_switched_on_then_on_every_inflow_of_one_color(app: App) -> None:
    assert app.state.su_inflow_zones == 1 and not app.setup.overlay.arrows
    app.select("zone:farfield")
    assert not app.setup.overlay.arrows  # selecting an inflow draws nothing by itself
    app.select("section:run")
    app.setup.toggle_inflow()
    assert app.setup.overlay.arrows
    assert app.state.su_inflow is None  # one speed: no scale
    app.setup.toggle_inflow()
    assert not app.setup.overlay.arrows


def test_a_picked_point_sets_the_key_waiting_for_it(app: App) -> None:
    path = 'boundaries[name = "farfield"].origin'
    app.setup.start_pick(path)
    assert app.state.su_pick == path
    assert app.setup.on_click({"point": (0.0, 25.0, 0.0), "name": "farfield", "cell": 0})
    assert app.session.doc.get(path) == [0.0, 25.0] and app.state.su_pick == ""
    assert not app.setup.on_click({"point": (1.0, 1.0, 0.0)})  # no key waiting: an ordinary pick
    app.setup.start_pick(path)
    assert app.setup.on_click(None, (3.0, 4.0, 0.0))  # in 2D, off every zone: the plane's point
    assert app.session.doc.get(path) == [3.0, 4.0]


def test_the_check_report_is_arranged_and_the_run_button_carries_the_cost(
    app: App, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    titles = [x["title"] for x in app.state.su_summary["sections"]]
    assert "TIME STEP" in titles and "ENVIRONMENT" in titles
    app.setup.open_launch()
    # Estimated at once, without running anything: no earlier run here, so the table's nearest measured run
    tp = app.state.su_launch["tp"]
    assert tp["kind"] == "estimated" and "2D TENO-E 3, NS" in tp["basis"]
    assert app.state.su_launch["throughput"] and app.state.su_summary["cost_known"]
    cost = {r["k"]: r for r in next(x for x in app.state.su_summary["sections"] if x["title"] == "COST")["rows"]}
    assert cost["throughput"]["note"].startswith("estimated from ") and "TENO-E 3" in cost["throughput"]["note"]
    app.setup.set_launch("ranks", "4")
    loop.run_until_complete(app.run_check())
    costed = [c for c in app.session.mallard.calls if "--throughput" in c]  # type: ignore[attr-defined]
    assert costed[-1][:2] == ["--ranks", "4"]

    def measured(on_progress, cancel, cap):  # type: ignore[no-untyped-def]
        on_progress(None, 1)
        app.session._use(Throughput(1.25e6, "measured", "median", 2.0e4, 4000, 40))
        return app.session.throughput

    monkeypatch.setattr(app.session, "measure_throughput", measured)
    before = app.state.su_summary
    loop.run_until_complete(app.setup.measure())
    assert app.state.su_launch["tp"]["text"] == "measured: 1,250,000 ± 20,000 cells/s/rank (4,000 steps)"
    assert app.state.su_launch["throughput"] == "1250000" and not app.state.su_launch["busy"]
    cost = {r["k"]: r for r in next(x for x in app.state.su_summary["sections"] if x["title"] == "COST")["rows"]}
    assert cost["throughput"]["note"] == "measured ± 20,000 over 4,000 steps"  # before the check returns
    assert app.state.su_summary != before


def test_the_palette_finds_keys_nodes_and_actions_and_goes_there(app: App) -> None:
    hits = app.setup.palette_search("t_wall")
    assert hits[0]["id"] == "key:run.t_wall_stop"
    app.setup.palette_pick("node:zone:farfield")
    assert app.state.selected == "zone:farfield"
    hit = app.setup.palette_search("add region rect")[0]
    assert hit["id"] == "action:add_region:rectangle"
    app.setup.palette_pick(hit["id"])
    assert app.state.selected.startswith("region:regions.")
    assert '\n[regions]\nrectangle = {type = "rectangle"' in app.session.doc.text


def test_changing_a_unit_shows_the_value_in_it_and_keeps_the_si_number(app: App, cylinder_project: Path) -> None:
    path = cylinder_project / "input.toml"
    path.write_text(path.read_text().replace('units = "nondimensional"\n', ""))
    app.session.doc.reload()
    app.select("section:run")
    app.set_key("run.t_stop", "2 ms", "s")
    app.setup.set_unit("run.t_stop", "2", "ms", "us")
    assert app.session.doc.get("run.t_stop") == 0.002
    row = next(r for g in app.state.props["groups"] for r in g["rows"] if r["path"] == "run.t_stop")
    assert (row["number"], row["unit_choice"]) == ("2000", "us")
    app.session.doc.set("run.t_stop", "1 min")  # an older input's quantity: written in SI as its unit changes
    app.setup.set_unit("run.t_stop", "1", "min", "s")
    assert app.session.doc.get("run.t_stop") == 60.0


def test_a_wrong_unit_is_an_error_under_its_field(app: App, cylinder_project: Path) -> None:
    path = cylinder_project / "input.toml"
    path.write_text(path.read_text().replace('units = "nondimensional"\n', ""))
    app.session.doc.reload()
    app.select("section:run")
    app.set_key("run.t_stop", "3 Pa", "s")
    row = next(r for g in app.state.props["groups"] for r in g["rows"] if r["path"] == "run.t_stop")
    assert "not a unit of time" in row["error"] and row["number"] == "3 Pa"
    assert app.session.doc.get("run.t_stop") == 160.0


def test_the_run_parts_values_reach_the_launched_process_and_the_estimates_do_not(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[list[str]] = []

    class Proc:
        pid = 4242

    def popen(cmd: list[str], **kw: object) -> Proc:
        launched.append(cmd)
        return Proc()

    monkeypatch.setattr("mallard_gui.backends.local.subprocess.Popen", popen)
    app.setup.open_launch()
    app.setup.set_launch("ranks", "4")
    app.setup.set_launch("memory", "a100-80")
    app.setup.set_launch("throughput", "123456")
    app.setup.start()
    (cmd,) = launched
    assert cmd[:3] == ["mpirun", "-n", "4"]
    assert f"--kokkos-num-threads={app.state.su_launch['threads']}" in cmd
    assert not {"a100-80", "123456", "--memory", "--throughput"} & set(cmd)
