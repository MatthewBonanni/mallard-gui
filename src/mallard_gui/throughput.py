"""Throughput (cell updates per second per rank) for the run dialog's wall-time estimate: preloaded from what
Mallard already recorded, or measured from Mallard's own stepping timer.

Mallard has no throughput model to ask: `mallard check` costs a run only at a throughput given (`--throughput`)
or timed (`--steps N`, one sample of N steps after a single untimed step). So the estimate before any measurement
is the stepping throughput of this project's earlier runs (their run logs' `end` records), else the nearest entry
of a table of measured runs by device, dimension, reconstruction and physics (`throughput_table.py`, regenerated
by `tools/throughput_table.py`), labeled as an estimate with that entry's basis.

The measurement runs the case itself with its outputs removed and a progress record every few steps
(`--log-format jsonl`), and keeps each record's `cells_per_second`, which Mallard computes from its stepping
time alone (no setup, output or diagnostics). The first intervals are discarded as warm-up; the median of the
rest is the result, with a 95 % interval from the median absolute deviation, robust to the slow intervals a
shared machine produces, widened to half the difference of the two halves' medians when the machine's load
drifts. It stops once that interval is tight enough or at a time cap.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import platform
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from .backends.staging import run_input
from .throughput_table import nearest
from .ui.fmt import number

WARMUP_INTERVALS = 2
MIN_SAMPLES = 8
TARGET_RSE = 0.03
TIME_CAP = 60.0
SAMPLE_SECONDS = 0.05  # stepping time per progress record
MAX_STEPS = 10**7
# Tables that write files or can stop the run; none changes the cost of a step much
OUTPUTS = ("write_data", "probes", "forces", "integrals", "triggers")


@dataclass
class Throughput:
    """A throughput with what it rests on: `kind` is "estimated" or "measured"."""

    value: float
    kind: str
    basis: str
    plus_minus: float = 0.0
    steps: int = 0
    samples: int = 0

    @property
    def rse(self) -> float:
        return self.plus_minus / 1.96 / self.value if self.value else math.inf

    def text(self) -> str:
        pm = f" ± {rate(self.plus_minus)}" if self.plus_minus else ""
        tail = f" ({number(self.steps)} steps)" if self.kind == "measured" else ""
        return f"{self.kind}: {rate(self.value)}{pm} cells/s/rank{tail}"

    def note(self) -> str:
        """For the cost's throughput row."""
        if self.kind == "measured":
            return f"measured ± {rate(self.plus_minus)} over {number(self.steps)} steps"
        return f"estimated from {self.basis}" if self.kind == "estimated" else self.kind

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "kind": self.kind, "basis": self.basis, "text": self.text()}


def rate(v: float) -> str:
    """A throughput to three significant figures, as a whole number."""
    return number(int(float(f"{v:.3g}"))) if v >= 1 else number(v)


# The estimate before measuring


def from_runs(run_logs: Iterable[tuple[str, Path]], cells: int, dim: int) -> Throughput | None:
    """The newest earlier run of this project, on this machine's build, whose log ended with a stepping
    throughput, closest in cells to this case."""
    best: tuple[float, float, Throughput] | None = None
    for name, log in run_logs:
        start, end = _ends(log)
        if not end or not start:
            continue
        cps, steps, wall = end.get("cells_per_second"), end.get("steps"), end.get("wall_stepping")
        prov = start.get("provenance") or {}
        run = prov.get("run") or {}
        ranks = int(run.get("ranks") or 1)
        if not isinstance(cps, (int, float)) or cps <= 0 or (prov.get("build") or {}).get("dim", dim) != dim:
            continue
        run_cells = _cells(log)
        distance = abs(math.log((run_cells or cells or 1) / (cells or 1)))
        basis = (
            f"run {name}: {number(run_cells)} cells, {ranks} rank{'s' if ranks > 1 else ''} on "
            f"{run.get('device') or run.get('host') or 'this machine'}, {number(steps)} steps in "
            f"{number(wall)} s of stepping"
        )
        candidate = Throughput(cps / ranks, "estimated", basis)
        key = (distance, -_mtime(log))
        if best is None or key < best[:2]:
            best = (*key, candidate)
    return best[2] if best else None


def from_table(data: Mapping[str, Any], dim: int, gpu: bool, threads: int, species: int = 0) -> Throughput:
    """The table of measured runs' nearest entry for this input on this kind of machine, a CPU entry scaled from
    its threads to `threads`."""
    found = nearest(device_class(gpu), dim or 2, reconstruction_of(data), physics_of(data), species)
    if found is None:
        return Throughput(0.0, "estimated", "no measured run to estimate from")
    entry, basis = found
    value = entry.rate(max(1, threads))
    if entry.cpu:
        basis += f", scaled to {threads} thread{'s' if threads != 1 else ''}"
    return Throughput(value, "estimated", basis.removeprefix("measured: "))


def device_class(gpu: bool) -> str:
    if gpu:
        return "a100"  # the table's only GPU
    return "cpu-apple-m" if sys.platform == "darwin" and platform.machine() == "arm64" else "cpu-x86"


def reconstruction_of(data: Mapping[str, Any]) -> str:
    numerics = data.get("numerics")
    rec = numerics.get("face_reconstruction") if isinstance(numerics, Mapping) else None
    if not isinstance(rec, Mapping):
        return "teno3"
    if str(rec.get("type", "TENO")).lower().startswith("muscl"):
        return "muscl"
    order = int(rec.get("order", 3))
    return "teno3" if order <= 3 else "teno5" if order <= 5 else "teno6"


def physics_of(data: Mapping[str, Any]) -> str:
    physics = data.get("physics")
    if not isinstance(physics, Mapping):
        physics = {}
    chemistry = data.get("chemistry")
    if physics.get("gas") == "mixture" and isinstance(chemistry, Mapping) and chemistry.get("enabled", True):
        return "reacting"
    if isinstance(data.get("les"), Mapping):
        return "les"
    return "ns" if str(physics.get("type", "euler")).lower() == "navier_stokes" else "euler"


def _ends(log: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    start = end = None
    try:
        with log.open("rb") as f:
            first = f.readline()
            f.seek(max(0, f.seek(0, 2) - 65536))
            tail = f.read().splitlines()
    except OSError:
        return None, None
    with contextlib.suppress(ValueError):
        start = json.loads(first)
    for line in reversed(tail):
        with contextlib.suppress(ValueError):
            record = json.loads(line)
            if isinstance(record, dict) and record.get("type") == "end":
                end = record
                break
    return start, end


def _cells(log: Path) -> int:
    with contextlib.suppress(OSError), log.open("rb") as f:
        f.seek(max(0, f.seek(0, 2) - 65536))
        for line in reversed(f.read().splitlines()):
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                if isinstance(record, dict) and record.get("cells"):
                    return int(record["cells"])
    return 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# Measuring


@dataclass
class Summary:
    """What the kept samples say so far."""

    median: float
    plus_minus: float
    samples: int
    steps: int
    discarded: int

    @property
    def rse(self) -> float:
        return self.plus_minus / 1.96 / self.median if self.median else math.inf


@dataclass
class Samples:
    """Per-interval throughputs (cells/s/rank) from progress records, in order, with each interval's steps."""

    warmup: int = WARMUP_INTERVALS
    values: list[float] = field(default_factory=list)
    steps: list[int] = field(default_factory=list)
    _last_step: int | None = None

    def add(self, record: Mapping[str, Any]) -> bool:
        """A run log record; True when it added a sample."""
        if record.get("type", "progress") != "progress":
            return False
        step = record.get("step")
        cps = record.get("cells_per_second")
        previous, self._last_step = self._last_step, step if isinstance(step, int) else self._last_step
        if not isinstance(cps, (int, float)) or cps <= 0 or not isinstance(step, int) or previous is None:
            return False
        ranks = int(record.get("ranks") or 1)
        self.values.append(float(cps) / ranks)
        self.steps.append(step - previous)
        return True

    @property
    def kept(self) -> list[float]:
        return self.values[self.warmup :]

    def summary(self) -> Summary | None:
        kept = self.kept
        if not kept:
            return None
        med = statistics.median(kept)
        mad = statistics.median(abs(v - med) for v in kept) * 1.4826
        # The median's standard error, sqrt(pi/2) sigma / sqrt(n), with sigma from the MAD
        pm = 1.96 * 1.2533 * mad / math.sqrt(len(kept)) if len(kept) > 1 else math.inf
        if len(kept) >= 4:
            # A drifting machine (load coming and going): the halves' medians disagree by more than independent
            # samples would, so the interval covers that too
            half = len(kept) // 2
            pm = max(pm, abs(statistics.median(kept[:half]) - statistics.median(kept[half:])) / 2)
        return Summary(med, pm, len(kept), sum(self.steps[self.warmup :]), min(self.warmup, len(self.values)))

    def done(self, target: float = TARGET_RSE, min_samples: int = MIN_SAMPLES) -> bool:
        s = self.summary()
        return s is not None and s.samples >= min_samples and s.rse < target


def measure_input(input_file: Path, interval: int) -> str:
    """The case's input as the measurement runs it: no files written, a progress record every `interval` steps,
    and only a step count to stop it."""
    doc = tomlkit.parse(run_input(input_file, absolute_paths=True))
    for name in OUTPUTS:
        doc.pop(name, None)
    run = doc.setdefault("run", tomlkit.table())
    for key in ("t_stop", "t_wall_stop"):
        run.pop(key, None)
    run["n_steps"] = MAX_STEPS
    output = doc.setdefault("output", tomlkit.table())
    output["check_interval"] = max(1, interval)
    output["report"] = ""
    return tomlkit.dumps(doc)


def interval_for(estimate: float, cells: int) -> int:
    """Steps per progress record so that each is about `SAMPLE_SECONDS` of stepping at `estimate`."""
    if estimate <= 0 or cells <= 0:
        return 10
    return int(min(1000, max(1, round(SAMPLE_SECONDS * estimate / cells))))


class Cancelled(Exception):
    pass


def measure(
    executable: Path,
    input_file: Path,
    threads: int,
    interval: int,
    on_progress: Callable[[Summary | None, int], None] = lambda s, n: None,
    cancel: threading.Event | None = None,
    target: float = TARGET_RSE,
    time_cap: float = TIME_CAP,
    env: Mapping[str, str] | None = None,
) -> Throughput:
    """Runs the case on one rank until the median's interval is under `target` of it or `time_cap` seconds of
    stepping went by; `on_progress(summary, samples)` after each record. Raises `Cancelled` or `RuntimeError`."""
    cancel = cancel or threading.Event()
    finished = threading.Event()
    samples = Samples()
    with tempfile.TemporaryDirectory(prefix="mallard-measure-") as tmp:
        (Path(tmp) / "input.toml").write_text(measure_input(input_file, interval), encoding="utf-8")
        cmd = [str(executable), "-i", "input.toml", "--log-format", "jsonl", f"--kokkos-num-threads={threads}"]
        err_path = Path(tmp) / "stderr.txt"
        err_file = err_path.open("wb")  # the text log: a pipe nobody reads would fill and stall the run
        proc = subprocess.Popen(
            cmd,
            cwd=tmp,
            stdout=subprocess.PIPE,
            stderr=err_file,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **(env or {})},
            start_new_session=True,
        )
        watcher = threading.Thread(target=_stop_on, args=(cancel, finished, proc), daemon=True)
        watcher.start()
        stepping_from: float | None = None  # the first progress record: setup is over
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type", "progress") != "progress":
                    continue
                stepping_from = stepping_from or time.monotonic()
                if not samples.add(record):
                    continue
                on_progress(samples.summary(), len(samples.values))
                if samples.done(target) or time.monotonic() - stepping_from > time_cap:
                    break
        finally:
            finished.set()
            _terminate(proc)
            watcher.join()
            err_file.close()
        if cancel.is_set():
            raise Cancelled()
        summary = samples.summary()
        if summary is None:
            err = err_path.read_text(errors="replace").strip()
            raise RuntimeError(err.splitlines()[-1] if err else "the measurement ended before timing any steps")
    basis = (
        f"median of {summary.samples} intervals of Mallard's stepping time on 1 rank, {threads} threads; "
        f"{summary.discarded} warm-up intervals discarded; ± 95 %"
    )
    return Throughput(summary.median, "measured", basis, summary.plus_minus, summary.steps, summary.samples)


def _stop_on(cancel: threading.Event, finished: threading.Event, proc: subprocess.Popen[bytes]) -> None:
    while not finished.wait(0.1):
        if cancel.is_set():
            _terminate(proc)
            return


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
