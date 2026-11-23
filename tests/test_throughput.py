"""The run dialog's throughput: the estimate from earlier runs or the table of measured runs, and the measurement
from Mallard's per-interval stepping times (warm-up discarded, median ± interval, adaptive stopping)."""

from __future__ import annotations

import json
import shutil
import statistics
import sys
import threading
from pathlib import Path

import pytest
import tomlkit

from mallard_gui.checkinfo import summarize, with_throughput
from mallard_gui.throughput import (
    MIN_SAMPLES,
    WARMUP_INTERVALS,
    Cancelled,
    Samples,
    device_class,
    from_runs,
    from_table,
    measure,
    measure_input,
    physics_of,
)
from mallard_gui.throughput_table import nearest

from .conftest import FIXTURES, fixture_json


def records(values: list[float], interval: int = 10, ranks: int = 1) -> list[dict[str, object]]:
    out: list[dict[str, object]] = [{"type": "progress", "step": 0, "cells_per_second": None, "ranks": ranks}]
    out += [
        {"type": "progress", "step": interval * (i + 1), "cells_per_second": v, "ranks": ranks}
        for i, v in enumerate(values)
    ]
    return out


def test_warm_up_and_outliers_do_not_move_the_median() -> None:
    steady = [1.00e6, 1.02e6, 0.98e6, 1.01e6, 0.99e6, 1.00e6, 1.03e6, 0.97e6, 1.00e6, 1.01e6]
    slow_start = [2.0e5, 6.0e5]  # first-use allocations, caches
    outliers = [3.0e5, 2.5e6]  # a preempted interval, a timer glitch
    s = Samples()
    for r in [{"type": "start"}, *records(slow_start + steady + outliers, ranks=2)]:
        s.add(r)
    summary = s.summary()
    assert summary is not None
    assert summary.discarded == WARMUP_INTERVALS and summary.samples == len(steady) + len(outliers)
    assert summary.steps == 10 * summary.samples
    kept = [v / 2 for v in steady + outliers]  # per rank
    assert summary.median == statistics.median(kept) == 5.0e5
    mad = statistics.median(abs(v - 5.0e5) for v in kept) * 1.4826
    assert summary.plus_minus == pytest.approx(1.96 * 1.2533 * mad / len(kept) ** 0.5)
    assert summary.plus_minus < 0.02 * summary.median
    # One timer over every step, as `check --steps` has it, is the harmonic mean, warm-up and outliers included
    every = [v / 2 for v in slow_start + steady + outliers]
    assert statistics.harmonic_mean(every) < 0.8 * summary.median


def test_stopping_waits_for_enough_samples_and_a_tight_interval() -> None:
    s = Samples()
    for r in records([1e6] * (WARMUP_INTERVALS + MIN_SAMPLES - 1)):
        s.add(r)
    assert not s.done()  # tight, but too few
    s.add({"type": "progress", "step": 10**6, "cells_per_second": 1e6})
    assert s.done()
    noisy = Samples()
    for r in records([1e6, 2e6, 0.5e6, 1.5e6] * 5):
        noisy.add(r)
    assert not noisy.done()
    drifting = Samples(warmup=0)  # each half tight, the load changed in between
    for r in records([1.0e6, 1.001e6] * 10 + [0.8e6, 0.801e6] * 10):
        drifting.add(r)
    summary = drifting.summary()
    assert summary is not None and summary.plus_minus == pytest.approx(0.1e6, rel=0.01) and not drifting.done()


def fake_mallard(tmp_path: Path, values: list[float], sleep: float = 0.0) -> Path:
    """An executable that prints progress records with these throughputs as `--log-format jsonl` does."""
    exe = tmp_path / "mallard"
    lines = [json.dumps(r) for r in records(values)]
    exe.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, pathlib, tomllib\n"
        "doc = tomllib.loads(pathlib.Path('input.toml').read_text())\n"
        "assert 'write_data' not in doc and 't_stop' not in doc['run'] and '--log-format' in sys.argv\n"
        f"for line in {lines!r}:\n"
        "    print(line, flush=True)\n"
        f"    time.sleep({sleep})\n"
        "print('stepped', file=sys.stderr)\n"
    )
    exe.chmod(0o755)
    return exe


def test_measure_stops_once_the_median_is_known(tmp_path: Path, cylinder_project: Path) -> None:
    values = [1e5, 4e5] + [1e6, 1.01e6, 0.99e6] * 20
    seen: list[int] = []
    result = measure(
        fake_mallard(tmp_path, values), cylinder_project / "input.toml", 2, 10, lambda s, n: seen.append(n)
    )
    assert result.kind == "measured" and result.value == 1e6
    assert result.samples == MIN_SAMPLES and seen[-1] == WARMUP_INTERVALS + MIN_SAMPLES  # stopped early
    assert result.steps == 10 * MIN_SAMPLES
    assert result.text().startswith("measured: 1,000,000 ± ") and result.text().endswith("(80 steps)")


def test_measure_stops_at_the_time_cap_and_cancels(tmp_path: Path, cylinder_project: Path) -> None:
    noisy = [1e6, 2e6, 0.5e6, 1.5e6] * 50
    result = measure(fake_mallard(tmp_path, noisy, 0.01), cylinder_project / "input.toml", 2, 10, time_cap=0.3)
    assert WARMUP_INTERVALS + MIN_SAMPLES <= result.samples < 100 and result.rse > 0.03
    cancel = threading.Event()
    with pytest.raises(Cancelled):
        measure(
            fake_mallard(tmp_path, noisy, 0.01),
            cylinder_project / "input.toml",
            2,
            10,
            lambda s, n: cancel.set() if n > 3 else None,
            cancel,
        )


def test_the_measured_input_writes_nothing_and_steps_without_end(cylinder_project: Path) -> None:
    doc = tomlkit.parse(measure_input(cylinder_project / "input.toml", 25)).unwrap()
    assert not {"write_data", "probes", "forces"} & doc.keys()
    assert "t_stop" not in doc["run"] and doc["run"]["n_steps"] >= 10**6
    assert doc["output"]["check_interval"] == 25 and doc["output"]["report"] == ""
    assert Path(doc["mesh"]["filename"]).is_absolute()


def test_the_estimate_comes_from_an_earlier_run_else_the_guide(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "input-1"
    run.mkdir(parents=True)
    shutil.copy(FIXTURES / "cylinder_run.jsonl", run / "mallard.jsonl")
    est = from_runs([("input-1", run / "mallard.jsonl"), ("gone", tmp_path / "nothing.jsonl")], 1536, 2)
    assert est is not None and est.kind == "estimated"
    assert est.value == pytest.approx(530527.3117497923)  # the end record's stepping throughput
    assert est.basis.startswith("run input-1: 1,536 cells, 1 rank")
    assert from_runs([("input-1", run / "mallard.jsonl")], 1536, 3) is None  # another dimension's build


def test_without_runs_the_estimate_is_the_tables_nearest_entry_scaled_to_the_threads() -> None:
    ns5 = {"physics": {"type": "navier_stokes"}, "numerics": {"face_reconstruction": {"type": "TENO", "order": 5}}}
    entry, _ = nearest("a100", 3, "teno5", "ns")  # type: ignore[misc]
    gpu = from_table(ns5, 3, gpu=True, threads=4)
    assert gpu.value == entry.cells_per_second_per_rank and "3D TENO-E 5, NS" in gpu.basis and gpu.kind == "estimated"
    cpu, _ = nearest(device_class(False), 2, "teno3", "euler")  # type: ignore[misc]
    est = from_table({}, 2, gpu=False, threads=3)  # Mallard's defaults: Euler, TENO-E order 3
    assert est.value == pytest.approx(cpu.cells_per_second_per_rank * 3) and est.basis.endswith("scaled to 3 threads")
    reacting = {"physics": {"gas": "mixture"}, "chemistry": {}, "les": {}}
    assert physics_of(reacting) == "reacting" and physics_of({**reacting, "chemistry": {"enabled": False}}) == "les"


def test_a_new_throughput_recosts_by_checks_formula() -> None:
    data = fixture_json("cylinder_check_cost.json")
    faster = with_throughput(data, 2 * data["cost"]["throughput"])
    assert faster["cost"]["wall_per_time"] == pytest.approx(data["cost"]["wall_per_time"] / 2)
    rows = {r.k: r for r in summarize(faster, throughput_note="measured ± 1 over 80 steps").rows["COST"]}
    assert rows["throughput"].note == "measured ± 1 over 80 steps"
