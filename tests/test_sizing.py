"""The run dialog's sizing: headline figures from the check, plots over rank counts from `mallard check --ranks
N`, and a click on a plot setting the run's ranks."""

from __future__ import annotations

import asyncio
import itertools
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from trame.app import get_server

from mallard_gui.checkinfo import summarize
from mallard_gui.session import Session
from mallard_gui.sizing import Point, charts, headline, point_of, rank_counts, wall
from mallard_gui.ui.app import App
from mallard_gui.ui.fmt import duration
from mallard_gui.ui.setup_tools import target_here

from .conftest import FakeMallard, fixture_json

_names = itertools.count()


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
    a = App(Session(cylinder_project, FakeMallard()), get_server(f"sizing-{next(_names)}", client_type="vue3"))  # type: ignore[arg-type]
    a.ctrl.view_update = lambda: None
    loop.run_until_complete(a.run_check())
    return a


def figures(app_or_list: Any) -> dict[str, dict[str, Any]]:
    rows = app_or_list.state.su_headline if isinstance(app_or_list, App) else app_or_list
    return {h["k"]: h for h in rows}


def test_the_headline_is_the_checks_own_numbers() -> None:
    data = fixture_json("cylinder_check_cost.json")
    tp = data["cost"]["throughput"]
    fig = figures(headline(data, 2, tp, "measured"))
    assert fig["max cells per rank"]["v"] == "186,724" and "1GB per rank" in fig["max cells per rank"]["note"]
    assert fig["ranks to fit"]["v"] == "1" and fig["ranks to fit"]["status"] == "ok"
    cost = {r.k: r for r in summarize(data).rows["COST"]}
    assert fig["wall time"]["v"] == cost["wall to the end"].v  # check's wall per unit time, at its own ranks
    assert fig["wall time"]["note"] == "on 2 ranks; measured; ideal scaling"  # this Mallard models no efficiency
    assert fig["rank-hours"]["v"] == cost["rank-hours"].v
    peak, usable = data["memory"]["target_bytes_peak"], 1e9 * (1 - 0.05)
    assert fig["memory per rank"]["bar"] == pytest.approx(peak / usable)
    m = data["memory"]
    assert fig["halo per rank"]["v"] == f"{100 * m['target_halo'] / m['target_owned']:.0f} %" == "44 %"
    assert fig["halo per rank"]["status"] == "warn"  # over the check's 10 %


def test_mallards_efficiency_model_slows_the_wall_time_and_bands_the_curves() -> None:
    data = fixture_json("cylinder_check_cost.json")
    tp = data["cost"]["throughput"]
    ideal = wall(data, 2, tp)
    data["cost"].update(
        scaling=[{"ranks": 1, "efficiency": 1.0}, {"ranks": 2, "efficiency": 0.8}, {"ranks": 4, "efficiency": 0.5}],
        uncertainty=0.25,
        model={"device": "A100", "runs": 40, "cases": 6, "fit_error": 0.08},
    )
    fig = figures(headline(data, 2, tp, "measured"))
    assert fig["wall time"]["v"] == duration(ideal / 0.8) + " ±25 %"
    assert fig["wall time"]["note"].endswith("efficiency 80 %")
    assert "not modeled" not in fig["halo per rank"]["note"]
    points = [Point(n, 100, 10, 1e6) for n in (1, 2, 3, 4, 8)]
    time = charts(data, points, 2, tp)["su_chart_time"]
    w = time["series"][0]
    at3 = 0.8 + math.log(1.5) / math.log(2) * (0.5 - 0.8)  # 3 ranks: between 2's and 4's, log-linear
    assert w["y"][2] / w["y"][1] == pytest.approx((2 / 3) * 0.8 / at3)
    assert w["y"][4] == pytest.approx(w["y"][0] / 8)  # past the model's counts: ideal
    assert w["band"]["hi"][1] == pytest.approx(1.25 * w["y"][1]) and w["band"]["lo"][1] == pytest.approx(
        0.75 * w["y"][1]
    )
    assert "A100" in time["basis"] and "±25 %" in time["basis"]
    assert charts(fixture_json("cylinder_check_cost.json"), points, 2, tp)["su_chart_time"]["basis"].startswith(
        "Ideal scaling"
    )


def test_rank_counts_cover_the_chosen_and_the_fewest_within_mallards_bound() -> None:
    assert rank_counts(1536, 4, 1) == [1, 2, 4, 8, 16]
    assert rank_counts(1536, 3, 0) == [1, 2, 3, 4, 8]
    assert rank_counts(200, 16, 0) == [1, 2, 3]  # at most a part per 64 cells
    assert point_of({"memory": {}}) is None


def test_the_plots_follow_the_ranks_checked_and_a_click_sets_the_ranks(
    app: App, loop: asyncio.AbstractEventLoop
) -> None:
    app.setup.open_launch()
    loop.run_until_complete(app.setup._sweep)  # type: ignore[arg-type]
    swept = [c for c in app.session.mallard.calls if c[:1] == ["--ranks"]]  # type: ignore[attr-defined]
    assert sorted(int(c[1]) for c in swept) == [1, 2, 4, 8]
    mem = app.state.su_chart_mem
    assert mem["series"][0]["x"] == [1, 2, 4, 8] and mem["pick"] == "su_pick_ranks"
    assert mem["marks"][0]["x"] == 1
    halo = app.state.su_chart_halo["series"][0]["y"]
    assert halo[0] == 0 and halo == sorted(halo)
    # A click on the point at 8 ranks: the run's ranks, the headline and every plot's mark
    app.setup.pick_ranks("su_chart_time", 8.0)
    assert app.state.su_launch["ranks"] == "8" and app.session.launch.ranks == 8
    assert figures(app)["wall time"]["note"].startswith("on 8 ranks")
    assert all(app.state[k]["marks"][0] == {"x": 8, "label": "8 chosen"} for k in ("su_chart_time", "su_chart_mem"))


def test_the_plots_redraw_for_another_throughput_and_device(app: App, loop: asyncio.AbstractEventLoop) -> None:
    app.setup.open_launch()
    loop.run_until_complete(app.setup._sweep)  # type: ignore[arg-type]
    before = app.state.su_chart_time["series"][0]["y"]
    app.setup.set_launch("throughput", str(2 * app.session.launch.throughput))
    after = app.state.su_chart_time["series"][0]["y"]
    assert after == pytest.approx([y / 2 for y in before])
    data = app.session.report.data  # type: ignore[union-attr]
    assert figures(app)["wall time"]["v"] == duration(wall(data, 1, app.session.launch.throughput))
    app.setup.set_launch("memory", "a100-40")
    loop.run_until_complete(app.run_check())
    usable = app.state.su_chart_mem["series"][1]
    assert usable["y"][0] == pytest.approx(40e9 * 0.95 / 1e6)
    assert figures(app)["ranks to fit"]["note"] == "fewest on NVIDIA A100 40 GB"
    assert charts(data, [], 1, 0)["su_chart_time"]["empty"] == "sizing: checking each rank count"


def test_measure_on_a_gpu_this_machine_lacks_says_so_and_runs_nothing(
    app: App, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[bool] = []
    monkeypatch.setattr(app.session, "measure_throughput", lambda *a, **k: ran.append(True))
    app.setup.open_launch()
    app.setup.set_launch("memory", "a100-80")
    loop.run_until_complete(app.setup.measure())
    assert app.state.su_launch["error"] == "No NVIDIA A100 80 GB available: set up a remote with one to measure"
    assert not ran and not app.state.su_launch["busy"]


def test_a_gpu_target_is_here_only_with_that_model_and_memory() -> None:
    a100_40 = {"visible": [{"name": "NVIDIA A100-SXM4-40GB", "memory": 39.4 * 2**30}]}
    assert target_here("a100-40", a100_40) and not target_here("a100-80", a100_40)
    assert not target_here("h200", a100_40) and target_here("", {}) and target_here("8GB", {})
