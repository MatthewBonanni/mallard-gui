"""The run dialog's sizing: headline figures from the check of the chosen ranks, and curves over rank counts
from `mallard check --ranks N` at each N (Mallard partitions the mesh for each and reports the largest part's
owned and halo cells and peak memory).

Wall time over ranks is `mallard check`'s own cost formula, wall per unit time = cells / (ranks x throughput x
dt) (`Solver::check_cost`), divided by the parallel efficiency at each rank count where the check models it
(`cost.scaling`, with its relative uncertainty `cost.uncertainty`). A Mallard without the model gives none: the
wall time is then the ideal-scaling bound, and the halo fraction (halo / owned cells of the largest part) is what
the curves show of the cost of more ranks.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .checkinfo import target_label
from .grouping import grouped
from .ui.fmt import bytes_text, duration, number

IDEAL = "Ideal scaling: this Mallard's check models no parallel efficiency"
MIN_CELLS_PER_RANK = 64  # Mallard's own bound on the parts it considers (`check_sizing`)
MAX_POINTS = 10


@dataclass
class Point:
    """What `mallard check --ranks N` says of a run on N ranks."""

    ranks: int
    owned: int
    halo: int
    bytes_peak: float

    @property
    def halo_fraction(self) -> float:
        return self.halo / self.owned if self.owned else 0.0


def rank_counts(cells: int, chosen: int, fewest: int = 0) -> list[int]:
    """Powers of two from 1 past the chosen ranks and the fewest that fit, with both, at most one part per
    `MIN_CELLS_PER_RANK` cells."""
    top = max(1, cells // MIN_CELLS_PER_RANK)
    want = min(top, max(8, 4 * chosen, 2 * fewest))
    out = {1, min(chosen, top)} | ({min(fewest, top)} if fewest else set())
    n = 2
    while n <= want and len(out) < MAX_POINTS:
        out.add(n)
        n *= 2
    return sorted(out)


def point_of(data: Mapping[str, Any]) -> Point | None:
    mem = data.get("memory") or {}
    ranks = int(mem.get("target_ranks") or (data.get("cost") or {}).get("ranks") or 0)
    peak = mem.get("target_bytes_peak") or mem.get("bytes_peak")
    if not ranks or not peak:
        return None
    return Point(
        ranks, int(mem.get("target_owned") or mem.get("local_owned") or 0), int(mem.get("target_halo") or 0), peak
    )


def sweep(
    check: Callable[[Sequence[str]], Mapping[str, Any]],
    counts: Sequence[int],
    on_point: Callable[[int, int], None] = lambda done, total: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[Point]:
    """`check(args)` at `--ranks N` for each count: Mallard's partition and memory for each."""
    points = []
    for i, n in enumerate(counts):
        if cancelled():
            break
        p = point_of(check(["--ranks", str(n)]))
        if p is not None:
            points.append(p)
        on_point(i + 1, len(counts))
    return points


@dataclass
class Scaling:
    """Mallard's parallel-efficiency model of the case (`cost.scaling` of `check --format json`)."""

    efficiency: dict[int, float]  # by rank count
    uncertainty: float  # relative, one standard deviation, of the wall times
    basis: str

    def at(self, ranks: int) -> float | None:
        """The efficiency on `ranks`: the model's at that count, else between the counts about it (log-linear in
        the ranks), else None."""
        if ranks in self.efficiency:
            return self.efficiency[ranks]
        below = [n for n in self.efficiency if n < ranks]
        above = [n for n in self.efficiency if n > ranks]
        if not below or not above:
            return None
        a, b = max(below), min(above)
        f = math.log(ranks / a) / math.log(b / a)
        return self.efficiency[a] + f * (self.efficiency[b] - self.efficiency[a])


def scaling_of(data: Mapping[str, Any]) -> Scaling | None:
    cost = data.get("cost") or {}
    rows = [r for r in cost.get("scaling") or [] if r.get("ranks") and (r.get("efficiency") or 0) > 0]
    if not rows:
        return None
    model = cost.get("model") or {}
    unc = float(cost.get("uncertainty") or 0)
    rms = 100 * float(model.get("fit_error") or 0)
    fit = f", fitted to {model['runs']} runs of {model['cases']} cases ({rms:.0f} % rms)" if model.get("runs") else ""
    device = f" for {model['device']}" if model.get("device") else ""
    return Scaling(
        {int(r["ranks"]): float(r["efficiency"]) for r in rows},
        unc,
        f"Mallard's efficiency model{device}{fit}; band ±{100 * unc:.0f} % (1σ)",
    )


def _time_left(data: Mapping[str, Any]) -> float:
    """Simulated time to the stop condition, as the cost rows count it."""
    ts, cost = data.get("time_step") or {}, data.get("cost") or {}
    left = (cost.get("t_stop") or 0) - (ts.get("t") or 0)
    if left > 0:
        return float(left)
    return float((cost.get("n_steps") or 0) * (ts.get("dt") or 0))


def wall(data: Mapping[str, Any], ranks: int, throughput: float, scaling: Scaling | None = None) -> float:
    """Wall seconds to the stop condition on `ranks` ranks at `throughput` (of one rank alone), by check's formula
    and the efficiency `scaling` gives there (else ideal scaling); 0 if unknown."""
    cells = (data.get("cost") or {}).get("cells") or 0
    dt = (data.get("time_step") or {}).get("dt") or 0
    if not (cells and dt and throughput > 0 and ranks > 0):
        return 0.0
    efficiency = (scaling.at(ranks) if scaling else None) or 1.0
    return cells / (ranks * throughput * dt) * _time_left(data) / efficiency


def usable_memory(data: Mapping[str, Any]) -> float:
    """The memory per rank a run may fill on the target: check's sizing memory less its margin and overhead."""
    sizing = data.get("sizing") or {}
    memory = sizing.get("memory") or 0
    return memory * (1 - (sizing.get("margin") or 0)) - (sizing.get("outside_kokkos") or 0) if memory else 0.0


def headline(data: Mapping[str, Any], ranks: int, throughput: float, basis: str) -> list[dict[str, Any]]:
    """The figures that size a run, biggest first: each {k, v, u, note, status, bar}."""
    sizing, mem = data.get("sizing") or {}, data.get("memory") or {}
    gpu = bool(sizing.get("gpu") or mem.get("device_memory"))
    target = target_label(str(sizing.get("name", ""))) if sizing else ""
    out: list[dict[str, Any]] = []

    def add(k: str, v: str, u: str = "", note: str = "", status: str = "", bar: float | None = None) -> None:
        out.append({"k": k, "v": v, "u": u, "note": note, "status": status, "bar": bar})

    if sizing.get("max_local_cells"):
        add("max cells per rank", grouped(int(sizing["max_local_cells"])), "", f"on {target}")
        fewest = int(sizing.get("min_ranks") or 0)
        add(
            "ranks to fit",
            grouped(fewest) if fewest else "–",
            "",
            f"fewest on {target}",
            "ok" if fewest and ranks >= fewest else "error" if fewest else "",
        )
    else:
        add("max cells per rank", "–", "", "choose a target device to size for")
    scaling = scaling_of(data)
    efficiency = scaling.at(ranks) if scaling else None
    seconds = wall(data, ranks, throughput, scaling)
    spread = f" ±{100 * scaling.uncertainty:.0f} %" if scaling and efficiency else ""
    add(
        "wall time",
        duration(seconds) + spread if seconds else "–",
        "",
        f"on {ranks} rank{'s' if ranks != 1 else ''}; {basis}"
        + (f"; efficiency {100 * efficiency:.0f} %" if efficiency else "; ideal scaling"),
    )
    hours = seconds * ranks / 3600
    add("GPU-hours" if gpu else "rank-hours", number(hours) if seconds else "–", "h", "")
    peak = mem.get("target_bytes_peak") or mem.get("bytes_peak") or 0
    capacity = usable_memory(data)
    share = peak / capacity if capacity else None
    add(
        "memory per rank",
        bytes_text(peak),
        "",
        f"of {bytes_text(capacity)} usable on {target}" if capacity else "peak; no device to compare with",
        "error" if share and share > 1 else "ok" if share else "",
        min(share, 1.0) if share else None,
    )
    owned, halo = mem.get("target_owned") or 0, mem.get("target_halo") or 0
    add(
        "halo per rank",
        f"{100 * halo / owned:.0f} %" if owned else "–",
        "",
        "of owned cells, largest part" + ("" if scaling else "; efficiency is not modeled by this Mallard"),
        "warn" if owned and halo / owned > (sizing.get("halo_fraction") or 0.1) else "",
    )
    return out


def charts(
    data: Mapping[str, Any], points: Sequence[Point], ranks: int, throughput: float
) -> dict[str, dict[str, Any]]:
    """The three sizing plots in chart.js's form, the chosen ranks and the fewest that fit marked on each; a
    click picks a rank count (`su_pick_ranks`)."""
    xs = [p.ranks for p in points]
    marks = [{"x": ranks, "label": f"{ranks} chosen"}]
    fewest = int((data.get("sizing") or {}).get("min_ranks") or 0)
    if fewest and fewest != ranks:
        marks.append({"x": fewest, "label": f"fits from {fewest}"})
    gpu = bool((data.get("sizing") or {}).get("gpu") or (data.get("memory") or {}).get("device_memory"))
    scaling = scaling_of(data)
    seconds = [wall(data, p.ranks, throughput, scaling) for p in points]
    unit, scale = next(((u, f) for u, f in (("h", 3600), ("min", 60)) if max(seconds, default=0) >= f), ("s", 1))
    walls = [w / scale for w in seconds]
    common = {"xlabel": "ranks", "xname": "ranks", "logx": True, "marks": marks, "pick": "su_pick_ranks", "xsnap": True}
    empty = "sizing: checking each rank count" if not points else ""
    hours = [w * n for w, n in zip(walls, xs, strict=True)]
    time_series: list[dict[str, Any]] = [
        {"label": "wall", "x": xs, "y": walls},
        {"label": f"{'GPU' if gpu else 'rank'} x wall", "x": xs, "y": hours},
    ]
    if scaling and scaling.uncertainty > 0:
        for series, ys in zip(time_series, (walls, hours), strict=True):
            series["band"] = {
                "x": xs,
                "lo": [y * (1 - scaling.uncertainty) for y in ys],
                "hi": [y * (1 + scaling.uncertainty) for y in ys],
            }
    capacity = usable_memory(data)
    mem_series: list[dict[str, Any]] = [{"label": "peak", "x": xs, "y": [p.bytes_peak / 1e6 for p in points]}]
    if capacity and xs:
        mem_series.append({"label": "usable", "x": [xs[0], xs[-1]], "y": [capacity / 1e6] * 2, "dash": True})
    return {
        "su_chart_time": {
            **common,
            "series": time_series if throughput > 0 else [],
            "ylabel": f"{unit}, {'GPU' if gpu else 'rank'}-{unit}",
            "logy": True,
            "empty": empty or "needs a throughput",
            "basis": scaling.basis if scaling else IDEAL,
        },
        "su_chart_mem": {**common, "series": mem_series, "ylabel": "MB per rank", "logy": True, "empty": empty},
        "su_chart_halo": {
            **common,
            "series": [{"label": "halo / owned", "x": xs, "y": [100 * p.halo_fraction for p in points]}],
            "ylabel": "% of owned",
            "empty": empty,
        },
    }
