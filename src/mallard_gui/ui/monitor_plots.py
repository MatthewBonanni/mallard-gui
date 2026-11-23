"""The Run monitor's four plots, interactive (chart.js) and live: each shows one source of the run (the run
log's dt, CFL, monitors, forces or probes, or a probe, force or reduction CSV file) and follows the latest
sample while the run goes. A zoom pauses following and reads finer data for its window, from the histories
held or by byte range from the run's files; a fit resumes it. Drawing a running run costs the plot's point
budget per poll, not the run's length.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from trame.widgets import client

from ..csvseries import CsvSeries, label
from ..export import PlotSpec
from ..plots import Curve, axis_label
from ..runlog import RunState
from ..signalsource import RUN_LOG, RunFiles, Signal, curve, history
from .fmt import number

if TYPE_CHECKING:
    from .app import App

KEYS = tuple(f"m_chart_{i}" for i in range(4))
MAX_POINTS = 1200
MAX_CURVES = 6
RANGE_TRIGGER = "monitor_range"
_RUN_NAMES = {"dt": "Time step", "cfl": "CFL number", "cells_per_second": "Throughput"}


def plot_sources(st: RunState) -> dict[str, str]:
    groups: dict[str, str] = {}
    for key, s in st.series.items():
        if s.group == "run":
            groups.setdefault(key, _RUN_NAMES.get(s.name, s.name))
        elif s.group == "monitors":
            groups.setdefault("monitors", "Health")
        elif s.group.startswith("forces_"):
            groups.setdefault(s.group, f"Forces {s.group.removeprefix('forces_')}")
        elif s.group == "probes":
            probe = s.name.split(".", 1)[0]
            groups.setdefault(f"probes/{probe}", f"Probe {probe}")
        else:
            groups.setdefault(s.group, s.group.capitalize())
    for path, c in st.csv.items():
        what = f"profile at t {number(c.t)}" if c.profile else "every sample"
        groups[f"csv:{path}"] = f"{label(path)} · {what}"
    return groups


def default_plots(sources: list[str], profiles: set[str]) -> list[str]:
    """A probe, the forces, a profile and the time step; the CSV files, which hold every sample, first."""
    csv = [s for s in sources if s.startswith("csv:")]
    series = [s for s in csv if s not in profiles and "reductions" not in s]
    order = [
        next((s for s in series if "/probe" in s), "") or next((s for s in sources if s.startswith("probes/")), ""),
        next((s for s in series if "/forces" in s), "") or next((s for s in sources if s.startswith("forces_")), ""),
        next((s for s in csv if s in profiles), "") or ("run/dt" if "run/dt" in sources else ""),
        "monitors" if "monitors" in sources else "",
    ]
    rest = [s for s in sources if s not in order]
    return [o or (rest.pop(0) if rest else "") for o in order]


def signals_of(st: RunState, source: str) -> list[Signal]:
    """The time series a plot source draws, at most MAX_CURVES."""
    if source.startswith("csv:"):
        path = source.removeprefix("csv:")
        c = st.csv.get(path)
        if c is None or c.profile:
            return []
        points = c.points()
        out = [
            Signal(f"csv:{path}#{n}#{p}", n if len(points) == 1 else f"{n} [{p}]", label(path), c.unit(n), path, n, p)
            for n in c.variables
            for p in points
            if (n, p) in c.series
        ]
        return out[:MAX_CURVES]
    if source.startswith("run/"):
        keys = [source] if source in st.series else []
    elif source.startswith("probes/"):
        probe = source.removeprefix("probes/")
        keys = [k for k, s in st.series.items() if s.group == "probes" and s.name.startswith(probe + ".")]
    else:
        keys = [k for k, s in st.series.items() if s.group == source]
    return [
        Signal(f"log:{k}", st.series[k].name.split(".", 1)[-1], st.series[k].group, st.series[k].unit, RUN_LOG, k)
        for k in keys[:MAX_CURVES]
    ]


def _named(labels: list[str], units: list[str]) -> tuple[list[str], str]:
    """Curve labels and the y unit: one unit on the axis, else each curve names its own."""
    if len(set(units)) > 1:
        return [f"{n} ({u or 'nd'})" for n, u in zip(labels, units, strict=True)], ""
    return labels, units[0] if units else ""


def _finite(x: list[float], y: list[float]) -> tuple[list[float], list[float]]:
    keep = [i for i in range(len(x)) if math.isfinite(x[i]) and math.isfinite(y[i])]
    return [x[i] for i in keep], [y[i] for i in keep]


def profile_spec(c: CsvSeries) -> PlotSpec:
    """A boundary probe's latest sample along the boundary."""
    names = c.variables[:MAX_CURVES]
    x = "s" if "s" in c.columns else "x"
    labels, unit = _named(names, [c.unit(n) for n in names])
    curves = [Curve(lab, c.column(x), c.column(n)) for lab, n in zip(labels, names, strict=True)]
    ylabel = axis_label(curves[0].label if len(curves) == 1 else "value", unit) if curves else ""
    return PlotSpec(curves, axis_label(x, c.unit(x)), ylabel)


class MonitorPlots:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self._run = ""
        self._st: RunState | None = None
        self._files: RunFiles | None = None
        self._live = False
        self._shown = ["", "", "", ""]
        self._ranges: list[tuple[float, float] | None] = [None] * 4
        self._cache: dict[tuple[str, float, float], tuple[list[float], list[float]]] = {}
        for key in KEYS:
            self.state[key] = {}
        app.server.trigger(RANGE_TRIGGER)(self.on_range)

    def show(self, name: str, st: RunState, live: bool) -> None:
        """Draw the run's plots: on selecting it, on a poll while it runs, and when a plot's source changes."""
        s = self.state
        if name != self._run:
            self._run = name
            self._ranges = [None] * 4
            self._cache.clear()
            self._shown = ["", "", "", ""]
            try:
                home = self.app.session.data.home(self.app.session.project.run(name))
                self._files = RunFiles(home.location, home.root)
            except (KeyError, ValueError):
                self._files = None
        self._st, self._live = st, live
        sources = plot_sources(st)
        s.plot_sources = [{"id": k, "label": v} for k, v in sources.items()]
        defaults = default_plots(list(sources), {f"csv:{p}" for p, c in st.csv.items() if c.profile})
        choice = [c if c in sources else defaults[i] for i, c in enumerate(s.plot_choice)]
        s.plot_choice = choice
        for i, c in enumerate(choice):
            if c != self._shown[i]:
                self._shown[i] = c
                self._ranges[i] = None
            self._draw(i)

    def on_range(self, key: str, x0: Any = None, x1: Any = None) -> None:
        """A plot's view settled: its window's data again, finer (None: the whole run, following)."""
        if key not in KEYS:
            return
        i = KEYS.index(key)
        try:
            lo, hi = float(x0), float(x1)
        except (TypeError, ValueError):
            self._ranges[i] = None
        else:
            self._ranges[i] = (lo, hi) if math.isfinite(lo) and math.isfinite(hi) and hi > lo else None
        if self._st is not None:
            self._draw(i)

    def _curve(self, sig: Signal, rng: tuple[float, float] | None) -> tuple[list[float], list[float]]:
        """The points of a signal for a window. A window wholly before the latest sample cannot change, so
        what was read for it (perhaps from the file) is kept while the run goes on."""
        st = self._st
        assert st is not None
        if rng is None:
            return curve(st, sig, self._files, None, None, MAX_POINTS)
        key = (sig.id, *rng)
        got = self._cache.get(key)
        if got is None:
            got = curve(st, sig, self._files, rng[0], rng[1], MAX_POINTS)
            h = history(st, sig)
            span = h.span if h else None
            if span is not None and span[1] > rng[1]:
                self._cache[key] = got
        return got

    def _draw(self, i: int) -> None:
        st = self._st
        assert st is not None
        source = self._shown[i]
        key = f"monitor{i}"
        view = f"{self._run}|{source}"
        title = plot_sources(st).get(source, source)
        c = st.csv.get(source.removeprefix("csv:")) if source.startswith("csv:") else None
        if c is not None and c.profile:
            spec = profile_spec(c)
            spec.title = title
            self.state[KEYS[i]] = self._cfg(spec, view, xname=spec.xlabel.split(" ", 1)[0], live=False)
            self.app.exports.remember(key, spec, c)
            return
        sigs = signals_of(st, source)
        xlabel = axis_label("t", st.unit("run", "t"))
        if not sigs:
            spec = PlotSpec([], xlabel, "", title)
            self.state[KEYS[i]] = {"series": [], "empty": "no data yet", "view": view}
            self.app.exports.remember(key, spec, c)
            return
        labels, unit = _named([g.label for g in sigs], [g.unit for g in sigs])
        curves = [Curve(lab, *_finite(*self._curve(g, self._ranges[i]))) for lab, g in zip(labels, sigs, strict=True)]
        ylabel = axis_label(curves[0].label if len(curves) == 1 else "value", unit)
        spec = PlotSpec(curves, xlabel, ylabel, title)
        logy = any("resid" in g.label.lower() for g in sigs)
        self.state[KEYS[i]] = self._cfg(spec, view, xname="t", live=self._live, logy=logy)
        self.app.exports.remember(key, spec, c)

    def layout_client(self) -> None:
        for key in KEYS:
            client.ClientStateChange(value=key, change=f"window.mallardChart && window.mallardChart.update('{key}')")

    @staticmethod
    def _cfg(spec: PlotSpec, view: str, *, xname: str, live: bool, logy: bool = False) -> dict[str, Any]:
        return {
            "series": [{"label": c.label, "x": list(c.x), "y": list(c.y)} for c in spec.curves],
            "xlabel": spec.xlabel,
            "ylabel": spec.ylabel,
            "xname": xname,
            "live": live,
            "logy": logy,
            "view": view,
            "range": RANGE_TRIGGER,
        }
