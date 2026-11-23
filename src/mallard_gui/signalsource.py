"""The signals of a run, whatever writes them, and their samples at full resolution where the run is.

A signal is a column of a probe, force or reduction CSV file (per point), or a number of the run log's
progress records (`dt`, `cfl`, the monitors, forces and probes it carries). In memory each is a bounded
`History`; the samples of a window it holds too coarsely, and an export of every sample, are read from the
run's own file by byte range (the offsets the history keeps), over the run's location: a remote run's file is
never copied whole to plot one curve. The GUI parses Mallard's CSV and JSON lines only.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO

from .csvseries import CsvSeries, label
from .data.locations import Location
from .history import History
from .runlog import RunState

CHUNK = 4 << 20
WHOLE = 1 << 62
RUN_LOG = "mallard.jsonl"


@dataclass(frozen=True)
class Signal:
    id: str
    label: str
    group: str
    unit: str
    file: str  # the run directory's file holding every sample
    column: str  # the CSV column, or the run log key path ("monitors/min_rho")
    point: int = 0

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "group": self.group, "unit": self.unit}


_RUN_NAMES = {"dt": "time step dt", "cfl": "CFL number", "cells_per_second": "throughput"}


def catalog(st: RunState) -> list[Signal]:
    """Every signal of the run: the CSV files' columns first (every sample), then the run log's numbers."""
    out: list[Signal] = []
    for path, c in st.csv.items():
        if c.profile:
            continue
        points = c.points()
        for col in c.variables:
            for p in points:
                if (col, p) not in c.series:
                    continue
                name = col if len(points) == 1 else f"{col} [{p}]"
                out.append(Signal(f"csv:{path}#{col}#{p}", name, label(path), c.unit(col), path, col, p))
    in_csv = {label(p) for p, c in st.csv.items() if not c.profile}
    for key, s in st.series.items():
        # The run log's copies of what a CSV file holds sample by sample are left out.
        if s.group in in_csv or (s.group == "probes" and f"probe_{s.name.split('.', 1)[0]}" in in_csv):
            continue
        group = s.group
        name = _RUN_NAMES.get(s.name, s.name) if s.group == "run" else s.name
        out.append(Signal(f"log:{key}", name, group, s.unit, RUN_LOG, key))
    return out


def find(st: RunState, sid: str) -> Signal | None:
    return next((s for s in catalog(st) if s.id == sid), None)


def history(st: RunState, sig: Signal) -> History | None:
    if sig.id.startswith("csv:"):
        c = st.csv.get(sig.file)
        return c.series.get((sig.column, sig.point)) if c else None
    s = st.series.get(sig.column)
    return s.history if s else None


class RunFiles:
    """Byte ranges of a run directory's files, on whatever location holds the run."""

    def __init__(self, location: Location, root: str) -> None:
        self.location, self.root = location, root

    def path(self, name: str) -> str:
        name = name.removeprefix("./")
        return name if name.startswith("/") else f"{self.root.rstrip('/')}/{name}"

    def lines(self, name: str, start: int = 0, end: int = -1) -> Iterator[tuple[int, bytes]]:
        """Complete lines of [start, end) (to the end of the file for -1), each with its byte offset."""
        length = WHOLE if end < 0 else max(0, end - start)
        src = self.location.reader(self.path(name), start, length)
        try:
            at, partial = start, b""
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                parts = (partial + chunk).split(b"\n")
                partial = parts.pop()
                for line in parts:
                    yield at, line
                    at += len(line) + 1
            if partial and end >= 0:
                yield at, partial  # the range ends inside the file: its last line is complete there
        finally:
            src.close()


def _csv_rows(lines: Iterator[tuple[int, bytes]], columns: list[str]) -> Iterator[list[str]]:
    for _, raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith("#"):
            continue
        cells = line.split(",")
        if len(cells) != len(columns) or cells[0] == columns[0]:
            continue
        yield cells


def _csv_samples(
    files: RunFiles, c: CsvSeries, sig: Signal, start: int, end: int, t0: float, t1: float
) -> tuple[list[float], list[float]]:
    it, iv = c.columns.index("t"), c.columns.index(sig.column)
    ip = c.columns.index("point") if "point" in c.columns else -1
    ts: list[float] = []
    ys: list[float] = []
    for cells in _csv_rows(files.lines(sig.file, start, end), c.columns):
        try:
            if ip >= 0 and int(float(cells[ip])) != sig.point:
                continue
            t = float(cells[it])
            if t0 <= t <= t1:
                ts.append(t)
                ys.append(float(cells[iv]))
        except ValueError:
            continue
    return ts, ys


def _dig(record: dict[str, object], key: str) -> object:
    group, _, name = key.partition("/")
    node: object = record if group == "run" else record.get(group)
    for part in name.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _log_samples(
    files: RunFiles, sig: Signal, start: int, end: int, t0: float, t1: float
) -> tuple[list[float], list[float]]:
    ts: list[float] = []
    ys: list[float] = []
    for _, raw in files.lines(RUN_LOG, start, end):
        if b'"progress"' not in raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "progress":
            continue
        t, v = record.get("t"), _dig(record, sig.column)
        if isinstance(t, (int, float)) and isinstance(v, (int, float)) and not isinstance(v, bool) and t0 <= t <= t1:
            ts.append(float(t))
            ys.append(float(v))
    return ts, ys


def samples(
    st: RunState, sig: Signal, files: RunFiles | None, t0: float | None = None, t1: float | None = None
) -> tuple[list[float], list[float]]:
    """Every sample of the signal in [t0, t1]: from memory where the history still holds them one by one,
    else read from the run's file over the byte range the history's offsets give (the whole file when
    they are unknown)."""
    h = history(st, sig)
    if h is None:
        return [], []
    span = h.span
    if span is None:
        return [], []
    lo = span[0] if t0 is None else t0
    hi = span[1] if t1 is None else t1
    fine = h.levels[0].all()
    if files is None or (fine and fine[0].t0 <= lo):
        sel = [(b.t0, b.last) for b in fine if lo <= b.t0 <= hi]
        return [t for t, _ in sel], [v for _, v in sel]
    rng = h.offsets(lo, hi) or (0, -1)
    start, end = rng
    if sig.id.startswith("csv:"):
        c = st.csv[sig.file]
        return _csv_samples(files, c, sig, start, end, lo, hi)
    return _log_samples(files, sig, start, end, lo, hi)


def curve(
    st: RunState,
    sig: Signal,
    files: RunFiles | None,
    t0: float | None = None,
    t1: float | None = None,
    max_points: int = 2000,
) -> tuple[list[float], list[float]]:
    """Points for a plot of [t0, t1]: the history's bins, or, where the levels held are too coarse for the
    window, its samples read again by byte range, each bin's extremes kept."""
    h = history(st, sig)
    if h is None:
        return [], []
    span = h.span
    if span is None:
        return [], []
    lo = span[0] if t0 is None else t0
    hi = span[1] if t1 is None else t1
    k = h.level_for(lo, hi, max_points // 2)
    held = sum(b.n for b in h.levels[k].all() if b.t1 >= lo and b.t0 <= hi)
    bins = sum(1 for b in h.levels[k].all() if b.t1 >= lo and b.t0 <= hi)
    if files is None or k == 0 or bins >= max_points // 4 or held == bins:
        return h.curve(lo, hi, max_points)
    t, y = samples(st, sig, files, lo, hi)
    return decimate(t, y, max_points)


def decimate(t: list[float], y: list[float], max_points: int = 2000) -> tuple[list[float], list[float]]:
    """At most `max_points`, each bin's minimum and maximum kept so peaks survive."""
    n = len(t)
    if n <= max_points:
        return t, y
    bins = max_points // 2
    xs: list[float] = []
    ys: list[float] = []
    for b in range(bins):
        i0, i1 = b * n // bins, max((b + 1) * n // bins, b * n // bins + 1)
        seg = range(i0, i1)
        lo = min(seg, key=lambda i: y[i])
        hi = max(seg, key=lambda i: y[i])
        for i in sorted({lo, hi}):
            xs.append(t[i])
            ys.append(y[i])
    return xs, ys


def export_csv(
    st: RunState,
    sigs: list[Signal],
    files: RunFiles,
    out: IO[str],
    t0: float | None = None,
    t1: float | None = None,
) -> int:
    """Every sample of the signals in [t0, t1], read from the run's files, as CSV with a units line:
    `t` and a column per signal (empty where a signal has no sample at that time). Returns the rows."""
    lo = -math.inf if t0 is None else t0
    hi = math.inf if t1 is None else t1
    data = [dict(zip(*samples(st, s, files, lo, hi), strict=True)) for s in sigs]
    times = sorted(set().union(*data)) if data else []
    t_unit = st.unit("run", "t") or "1"
    out.write(",".join(["t", *(_header(s) for s in sigs)]) + "\n")
    out.write("# units: " + ",".join([t_unit, *(s.unit or "1" for s in sigs)]) + "\n")
    for t in times:
        out.write(",".join([repr(t), *(repr(d[t]) if t in d else "" for d in data)]) + "\n")
    return len(times)


def _header(s: Signal) -> str:
    return f"{s.group}.{s.label}".replace(",", " ").replace(" ", "_")
