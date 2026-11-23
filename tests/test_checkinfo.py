from __future__ import annotations

import pytest

from mallard_gui.checkinfo import ELSEWHERE, SUMMARIZED, LaunchOptions, suggestions, summarize
from mallard_gui.ui.fmt import duration

from .conftest import fixture_json


def section(summary: object, title: str) -> dict[str, str]:
    return {r.k: r.v for r in summary.rows[title]}  # type: ignore[attr-defined]


def test_cost_to_the_end_is_wall_per_time_times_the_time_left() -> None:
    data = fixture_json("cylinder_check_cost.json")
    s = summarize(data)
    cost = section(s, "COST")
    assert s.cost_known
    wpt = data["cost"]["wall_per_time"]
    assert cost["wall to the end"] == duration(wpt * 160)
    assert cost["steps"] == f"{round(160 / data['time_step']['dt']):,}"
    assert "rank-hours" in cost  # a CPU build
    assert s.run_label.endswith(cost["wall to the end"])


def test_without_a_throughput_the_cost_says_what_it_needs() -> None:
    s = summarize(fixture_json("cylinder_check.json"))
    assert not s.cost_known
    assert section(s, "COST")["wall time"] == "–"


def test_every_part_of_the_report_has_a_section() -> None:
    s = summarize(fixture_json("cylinder_check_cost.json")).to_dict()
    titles = [x["title"] for x in s["sections"]]
    assert titles == [
        "TIME STEP",
        "COST",
        "MEMORY",
        "SIZING",
        "RESOLUTION",
        "MESH QUALITY",
        "BOUNDARIES",
        "REGIONS",
        "ENVIRONMENT",
        "REPORT",
    ]
    res = next(x for x in s["sections"] if x["title"] == "RESOLUTION")["rows"]
    assert any(r["k"] == "first-cell y+, largest" and r["status"] == "ok" for r in res)


def test_nondimensional_cases_drop_si_units() -> None:
    s = summarize(fixture_json("cylinder_check_cost.json"), nondimensional=True)
    assert all(r.u == "" for r in s.rows["TIME STEP"])


def test_launch_options_become_check_arguments() -> None:
    assert LaunchOptions(4, "a100-80").args() == ["--ranks", "4", "--memory", "a100-80"]
    assert LaunchOptions(2, throughput=5e6).args() == ["--ranks", "2", "--throughput", "5e+06"]
    assert LaunchOptions().args() == []


@pytest.mark.parametrize("key", ["run.cfll"])
def test_unused_keys_carry_their_suggestion(key: str) -> None:
    data = {"unused_keys": [{"key": key, "line": 3, "suggestion": "run.cfl"}, {"key": "x", "suggestion": None}]}
    assert suggestions(data) == {key: "run.cfl"}


@pytest.mark.parametrize("name", ["cylinder_check.json", "cylinder_check_cost.json", "cylinder_analysis/check.json"])
def test_every_part_of_the_check_report_is_shown_somewhere(name: str) -> None:
    """A part Mallard adds to the report is summarized or routed on purpose, never dropped silently."""
    data = fixture_json(name)
    assert set(data) <= SUMMARIZED | set(ELSEWHERE), set(data) - SUMMARIZED - set(ELSEWHERE)


def test_rows_link_to_the_key_and_the_place_they_concern() -> None:
    data = fixture_json("cylinder_check.json")
    data["time_step"]["set_by"] = "dt"
    data["mesh_quality"]["SKEWNESS"]["above_limit"] = 2
    data["unassigned"] = [{"zone": "inlet", "faces": 12}]
    s = summarize(data)
    step = {r.k: r for r in s.rows["TIME STEP"]}
    assert step["dt"].key == "run.dt"  # dt given, not set by the CFL number
    assert step["limiting cell"].x == data["time_step"]["limiting_x"]
    worst = data["mesh_quality"]["SKEWNESS"]["worst"]
    cells = [r for r in s.rows["MESH QUALITY"] if r.k.strip().startswith("cell ")]
    assert [r.x for r in cells] == [w["x"] for w in worst[1:]]
    zones = {r.k: r for r in s.rows["BOUNDARIES"]}
    assert zones["inlet"].status == "error" and zones["inlet"].node == "zone:inlet"
    assert zones["cylinder"].node == "zone:cylinder"


def test_the_sizing_verdict_says_what_a_rank_needs_and_where_it_fits() -> None:
    data = fixture_json("cylinder_check_cost.json")
    fit = summarize(data).fit
    assert fit["status"] == "ok"
    assert fit["text"].startswith("Needs 6.22 MB per rank on 2 ranks: fits on 1GB per rank from 1 rank")
    data["sizing"] = {**data["sizing"], "name": "a100-80", "gpu": True, "min_ranks": 4}
    fit = summarize(data).fit
    assert fit["status"] == "error"
    assert "does not fit on NVIDIA A100 80 GB; it needs at least 4 ranks" in fit["text"]
    del data["sizing"]
    assert "not sized for a device" in summarize(data).fit["text"]


def test_size_targets_name_this_machine_as_detected_then_each_gpu_with_its_memory() -> None:
    from mallard_gui.ui.setup_tools import size_targets

    gpu = {"visible": [{"name": "NVIDIA A100-SXM4-80GB", "memory": 85_093_171_200}] * 2}
    targets = size_targets(["a100-40", "mi250x", "future-1"], gpu)
    assert [t["value"] for t in targets] == ["", "a100-40", "mi250x", "future-1"]
    assert targets[0]["label"] == "this machine (detected: NVIDIA A100-SXM4-80GB 79 GiB ×2)"
    assert targets[1]["label"] == "NVIDIA A100 40 GB"
    assert targets[2]["label"] == "AMD MI250X 64 GB (one GCD)"
    assert targets[3]["label"] == "future-1 per rank"
    cpu = size_targets([], {"host": {"cpu": "M3", "threads": 8}})
    assert cpu[0]["label"] == "this machine (detected: 8 CPU threads, no memory limit)"
