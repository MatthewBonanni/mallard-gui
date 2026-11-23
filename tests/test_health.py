from __future__ import annotations

import json
from typing import Any

import pytest

from mallard_gui.csvseries import CsvSeries
from mallard_gui.health import CRITICAL, INFO, WARNING, HealthMonitor, wall_limit
from mallard_gui.runlog import RunState

from .conftest import FIXTURES

START: dict[str, Any] = {
    "type": "start",
    "schema": "mallard-run-log",
    "version": 2,
    "run_id": "r1",
    "check": {"time_step": {"cfl_limit": 1.2}},
    "provenance": {"input": {"resolved": "[run]\nt_wall_stop = 100.0\n"}},
}


def progress(step: int, **fields: Any) -> dict[str, Any]:
    record = {"type": "progress", "step": step, "t": 0.01 * step, "dt": 0.01, "cfl": 0.5, "wall": float(step)}
    record.update(fields)
    return record


def feed(records: list[dict[str, Any]], mon: HealthMonitor | None = None) -> tuple[HealthMonitor, RunState, list[str]]:
    mon = mon or HealthMonitor()
    st = RunState()
    keys: list[str] = []
    for i, r in enumerate(records):
        st.add(r)
        keys += [a.key for a in mon.update(st, now=float(i))]
    return mon, st, keys


def steady(n: int, **fields: Any) -> list[dict[str, Any]]:
    return [START, *(progress(i, **fields) for i in range(1, n + 1))]


def test_a_healthy_run_has_no_alerts() -> None:
    mon, _, keys = feed(steady(30, monitors={"max_mach": 0.3}))
    assert keys == []
    assert mon.worst == ""


def test_the_captured_cylinder_run_raises_only_information() -> None:
    mon, _, _ = feed([json.loads(line) for line in (FIXTURES / "cylinder_run.jsonl").read_text().splitlines()])
    assert {a.severity for a in mon.alerts} == {INFO}  # W-FORMAT-VTU and the end: nothing about the solution
    assert {a.code for a in mon.alerts if a.source == "mallard"} >= {"W-FORMAT-VTU"}


def test_mallard_health_warning_is_a_warning_with_its_code_and_docs() -> None:
    warning = {
        "type": "warning",
        "code": "W-HEALTH-MARGIN",
        "message": "density -1.82 is below 1e-3 of its mean",
        "docs": "docs/guide/running.md#w-health-margin",
    }
    mon, _, keys = feed([*steady(3), warning])
    assert keys == ["mallard:W-HEALTH-MARGIN"]
    (a,) = mon.alerts
    assert (a.severity, a.source, a.docs) == (WARNING, "mallard", "docs/guide/running.md#w-health-margin")


def test_a_captured_divergence_is_one_alert_completed_by_the_report() -> None:
    # A fixed dt the flow outgrows: CFL around the limit for 340 steps, then the event, the divergence
    # report, the end and the E-DIVERGED error, all in one poll.
    records = [json.loads(line) for line in (FIXTURES / "diverged_run.jsonl").read_text().splitlines()]
    mon, _, keys = feed(records)
    assert keys.count("mallard:diverged") == 1
    assert keys.count("gui:cfl-limit") == 1  # the CFL number crossing the limit every other row is one alert
    diverged = next(a for a in mon.alerts if a.key == "mallard:diverged")
    assert diverged.severity == CRITICAL and "time step too large" in diverged.text
    assert diverged.docs.endswith("#the-divergence-report")
    assert not any(a.kind == "end" for a in mon.alerts)  # the error is already announced


def test_a_value_turning_null_is_a_nan() -> None:
    records = steady(5, monitors={"min_rho": 1.0})
    records.append(progress(6, monitors={"min_rho": None}))
    _, _, keys = feed(records)
    assert keys == ["gui:nan"]


def test_nullable_progress_fields_are_not_nan() -> None:
    records = steady(5, eta=10.0)
    records.append(progress(6, eta=None, wall_per_step=None))
    assert feed(records)[2] == []


def test_nan_in_a_probe_file() -> None:
    mon = HealthMonitor()
    st = RunState()
    st.add(START)
    c = CsvSeries("./solut/probe_p.csv")
    for line in ("step,t,P", "1,0.1,1.0", "2,0.2,nan"):
        c.add_line(line)
    st.csv[c.path] = c
    assert [a.key for a in mon.update(st, 0.0)] == ["gui:nan:./solut/probe_p.csv"]


def test_cfl_above_the_integrators_limit() -> None:
    records = steady(10)
    records.append(progress(11, cfl=1.5))
    _, _, keys = feed(records)
    assert "gui:cfl-limit" in keys


def test_cfl_spike_fires_once_and_rearms_after_it_clears() -> None:
    records = [*steady(10, cfl=0.3), progress(11, cfl=0.9), progress(12, cfl=0.9)]
    records += [progress(i, cfl=0.3) for i in range(13, 20)] + [progress(20, cfl=0.9)]
    _, _, keys = feed(records)
    assert keys.count("gui:cfl-spike") == 2


def test_dt_collapse() -> None:
    records = [*steady(10), progress(11, dt=1e-4)]
    mon, _, keys = feed(records)
    assert keys == ["gui:dt"]
    assert mon.alerts[0].severity == CRITICAL


@pytest.mark.parametrize("name", ["MAX_MACH", "max_mach"])  # Mallard's monitors, and before #1467
def test_monitor_growing_by_orders_of_magnitude_is_diverging(name: str) -> None:
    records = [START, *(progress(i, monitors={name: 0.3 * 3**i}) for i in range(1, 8))]
    _, _, keys = feed(records)
    assert keys == [f"gui:diverging:{name}"]


def test_ledger_residual_diverging_but_round_off_is_not() -> None:
    def ledger(rel: float) -> dict[str, Any]:
        return {"balance": {"mass": {"relative": rel}}}

    growing = [START, *(progress(i, health=ledger(1e-9 * 4**i)) for i in range(1, 8))]
    assert feed(growing)[2] == ["gui:diverging:mass residual"]
    round_off = [START, *(progress(i, health=ledger(1e-17 * 4**i)) for i in range(1, 8))]
    assert feed(round_off)[2] == []


def test_wall_time_limit_from_the_resolved_input() -> None:
    assert wall_limit(START) == 100.0
    records = [START, progress(1, wall=50.0, eta=200.0, progress=0.2), progress(2, wall=95.0, progress=0.4)]
    mon, _, keys = feed(records)
    assert keys == ["gui:wall-eta", "gui:wall"]
    assert "about 40 %" in mon.alerts[0].text  # 20 % done, half the rest in the 50 s left of a 200 s ETA
    close = [START, progress(1, wall=4.0, eta=36.5, progress=0.1)]  # finishes 0.5 s past the limit: ~99 %
    assert feed(close)[2] == []


def test_job_time_limit_when_the_input_has_none() -> None:
    start = {**START, "provenance": {}}
    _, _, keys = feed([start, progress(1, wall=95.0)], HealthMonitor(job_wall_limit=100.0))
    assert keys == ["gui:wall"]


def test_stalled_run_log_while_the_job_runs() -> None:
    mon = HealthMonitor()
    st = RunState()
    st.add(START)
    for i in range(1, 6):
        st.add(progress(i))
        mon.update(st, now=float(i))
    assert mon.update(st, now=100.0) == []  # under the floor of two minutes
    assert [a.key for a in mon.update(st, now=200.0)] == ["gui:stalled"]
    assert mon.update(st, now=300.0, running=True) == []  # once


def test_a_fresh_run_state_starts_over() -> None:
    mon, _, _ = feed([*steady(10), progress(11, dt=1e-4)])
    assert mon.alerts
    st = RunState()
    st.add(START)
    mon.update(st, 0.0)
    assert mon.alerts == []
