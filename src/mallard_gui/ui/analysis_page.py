"""The Analysis page: a run's histories, signals, forces and extracted lines, for technical analysis.

Histories overlays any of the run's signals (probe, force and reduction CSV columns, the run log's dt, CFL,
monitors) on one interactive plot, live while the run goes, with a running mean; a zoom reads finer data
from the run's files by byte range, and CSV exports every sample from them. Signal analyzes one signal over
a window: Welch's spectrum with its controls and the Strouhal number of its peak, the autocorrelation, and
mean, RMS and their intervals with whether the mean has converged. Forces gives Cd and Cl of a `[[forces]]`
zone with reference quantities in units, their means and fluctuations over the window. Lines and walls
extracts a line or a wall's distribution from a snapshot through `mallard post extract`, drawn with axes in
the units picked.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import client, html

from ..coefficients import DIMS, Reference, coefficients, default_reference, force_columns, parse_reference
from ..compare import resolved_input
from ..export import PlotSpec, safe_name
from ..extract import (
    WALL_FIELDS,
    convert_values,
    line_args,
    probe_file,
    read_profile,
    sources,
    unit_choices,
    wall_args,
    wall_input,
)
from ..health import CRITICAL, INFO
from ..plots import Curve
from ..postjobs import PostJob
from ..runlog import RunState
from ..signals import (
    DETRENDS,
    WINDOWS,
    WindowStats,
    autocorrelation,
    running_mean,
    statistics,
    transient_end,
    welch,
)
from ..signalsource import RunFiles, Signal, catalog, curve, export_csv, history, samples
from ..units import UnitError, entry, entry_convert, entry_text
from .empty import empty_state
from .fmt import number
from .panes import splitter

if TYPE_CHECKING:
    from .app import App

TABS = (("histories", "Histories"), ("signal", "Signal"), ("forces", "Forces"), ("lines", "Lines and walls"))
CHARTS = ("a_chart_hist", "a_chart_sig", "a_chart_psd", "a_chart_acf", "a_chart_coef", "a_chart_line")
MAX_POINTS = 2000
LIVE_EVERY = 3.0  # seconds between recomputing the window analyses of a running run
SMOOTH_SAMPLES = 200_000  # a running mean averages the samples themselves up to this many, else bin means


def _finite(x: Sequence[float], y: Sequence[float]) -> tuple[list[float], list[float]]:
    pts = [(a, b) for a, b in zip(x, y, strict=False) if math.isfinite(a) and math.isfinite(b)]
    return [a for a, _ in pts], [b for _, b in pts]


def _series(label: str, x: Sequence[float], y: Sequence[float], **style: Any) -> dict[str, Any]:
    fx, fy = _finite(x, y)
    return {"label": label, "x": fx, "y": fy, **style}


def _axis(name: str, unit: str) -> str:
    return f"{name} ({unit or 'nd'})"


def _num(text: Any) -> float | None:
    try:
        v = float(str(text).strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


class AnalysisPage:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self._st: RunState | None = None
        self._files: RunFiles | None = None
        self._sigs: dict[str, Signal] = {}
        self._ranges: dict[str, tuple[float, float] | None] = {}
        self._cache: dict[str, tuple[int, tuple[list[float], list[float]]]] = {}
        self._last_live = 0.0
        self._specs: dict[str, PlotSpec] = {}
        s = self.state
        s.a_run = ""
        s.a_tab = "histories"
        s.a_signals = []
        s.a_groups = []
        s.a_note = ""
        s.a_hist_sel = []
        s.a_smooth = ""
        s.a_sig = ""
        s.a_t0 = ""
        s.a_t1 = ""
        s.a_tol = "1"
        s.a_window = "hann"
        s.a_seglen = ""
        s.a_overlap = "50"
        s.a_detrend = "mean"
        s.a_windows = list(WINDOWS)
        s.a_detrends = list(DETRENDS)
        s.a_stats = []
        s.a_spec = []
        s.a_sig_error = ""
        s.a_ref = {"rho": "", "velocity": "", "length": "1", "area": "1", "alpha": "0"}
        s.a_ref_err = {}
        s.a_ref_fields = {}
        s.a_ref_rev = {}
        s.a_dim = 2
        s.a_nondim = False
        s.a_forces = []
        s.a_force = ""
        s.a_ft0 = ""
        s.a_ft1 = ""
        s.a_coef = []
        s.a_coef_note = ""
        s.a_ex_sources = []
        s.a_ex_src = ""
        s.a_ex_kind = "wall"
        s.a_ex_p0 = ["0", "0", "0"]
        s.a_ex_p1 = ["1", "0", "0"]
        s.a_ex_n = "200"
        s.a_ex_walls = []
        s.a_ex_zone = ""
        s.a_ex_fields = ""
        s.a_ex_jobs = []
        s.a_ex_results = []
        s.a_ex_result = ""
        s.a_ex_y = ""
        s.a_ex_ys = []
        s.a_ex_xunit = ""
        s.a_ex_xunits = []
        s.a_ex_yunit = ""
        s.a_ex_yunits = []
        s.a_ex_norm = False
        s.a_ex_note = ""
        for key in CHARTS:
            s[key] = {}
        server = app.server
        server.trigger("chart_range")(self.on_range)
        server.trigger("chart_span")(self.on_span)
        self._reference_fields()

    # The run

    def open(self) -> None:
        s = self.state
        names = [r["name"] for r in s.runs]
        if s.a_run not in names:
            s.a_run = s.run_sel if s.run_sel in names else (names[0] if names else "")
        if s.a_run:
            self._load(s.a_run)

    def select_run(self, name: str) -> None:
        self.state.a_run = name
        self._ranges.clear()
        self._cache.clear()
        for key in CHARTS:
            self.state[key] = {}
        self._load(name)

    def _load(self, name: str) -> None:
        session = self.app.session
        record = session.project.run(name)
        active = session.active.get(name)
        st = active.follower.state if active else session.open_run(record)
        try:
            home = session.data.home(record)
            self._files = RunFiles(home.location, home.root)
        except (KeyError, ValueError):
            self._files = None
        self._set_run(st)
        self._list_results()
        self.refresh()

    def _list_results(self) -> None:
        """The probe files of extractions made before, in the run's `extract/`."""
        s = self.state
        found: dict[str, int] = {}
        if self._files is not None:
            with contextlib.suppress(OSError):
                found = self._files.location.list_files(self._files.path("extract"))
        names = sorted((n for n in found if n.startswith("probe_") and n.endswith(".csv")), reverse=True)
        s.a_ex_results = [
            {"path": f"extract/{n}", "label": n.removeprefix("probe_").removesuffix(".csv")} for n in names
        ]
        if s.a_ex_result not in [r["path"] for r in s.a_ex_results]:
            s.a_ex_result = s.a_ex_results[0]["path"] if s.a_ex_results else ""
            s.a_ex_y = ""

    def _set_run(self, st: RunState) -> None:
        s = self.state
        self._st = st
        sigs = catalog(st)
        self._sigs = {x.id: x for x in sigs}
        s.a_signals = [x.to_dict() for x in sigs]
        s.a_groups = list(dict.fromkeys(x.group for x in sigs))
        if not [i for i in s.a_hist_sel if i in self._sigs]:
            first = next((x.id for x in sigs if x.id.startswith("csv:") and "probe" in x.file), "")
            s.a_hist_sel = [first or (sigs[0].id if sigs else "")] if sigs else []
        if s.a_sig not in self._sigs:
            s.a_sig = next((x.id for x in sigs if x.id.startswith("csv:") and "probe" in x.file), "") or (
                sigs[0].id if sigs else ""
            )
        resolved, _ = resolved_input(st)
        build = ((st.start.get("provenance") or {}).get("build") or {}) if st.start else {}
        s.a_dim = int(build.get("dim") or 2)
        s.a_nondim = (resolved.get("physics") or {}).get("units") == "nondimensional"
        if not any(s.a_ref.get(k) for k in ("rho", "velocity")):
            s.a_ref = default_reference(resolved)
        self._reference_fields()
        forces = [p for p, c in st.csv.items() if not c.profile and force_columns(c.columns)]
        forces += [g for g in dict.fromkeys(x.group for x in st.series.values()) if g.startswith("forces_")]
        s.a_forces = [{"id": f, "label": f.rsplit("/", 1)[-1].removesuffix(".csv")} for f in forces]
        if s.a_force not in forces:
            s.a_force = forces[0] if forces else ""
        s.a_ex_sources = [
            {
                "path": x["path"],
                "label": f"{Path(x['path']).name} · t {number(x['t'])}" if x["t"] is not None else x["path"],
            }
            for x in sources(st.outputs, st.end)
        ]
        if s.a_ex_src not in [x["path"] for x in s.a_ex_sources]:
            s.a_ex_src = s.a_ex_sources[0]["path"] if s.a_ex_sources else ""
        walls = [b.get("name") for b in resolved.get("boundaries") or [] if str(b.get("type", "")).startswith("wall")]
        s.a_ex_walls = [w for w in walls if isinstance(w, str)]
        if s.a_ex_zone not in s.a_ex_walls:
            s.a_ex_zone = s.a_ex_walls[0] if s.a_ex_walls else ""
        if not s.a_ex_fields:
            viscous = (resolved.get("physics") or {}).get("type") == "navier_stokes"
            reference = bool((resolved.get("output") or {}).get("reference"))
            s.a_ex_fields = ",".join(
                f for f in WALL_FIELDS[:4] if (f != "CP" or reference) and (f not in ("CF", "Y_PLUS") or viscous)
            )
        t = [x.history.span for x in st.series.values() if x.history.span]
        s.a_note = (
            f"{len(sigs)} signals · t {number(min(a for a, _ in t))} to {number(max(b for _, b in t))}"
            if t
            else "no samples yet"
        )

    def observe(self, name: str, st: RunState) -> None:
        """A poll of a running run: its new samples, drawn if this page shows it."""
        if name != self.state.a_run or self.state.page != "analysis":
            return
        self._set_run(st)
        self._histories()
        now = time.monotonic()
        if now - self._last_live >= LIVE_EVERY:
            self._last_live = now
            self._cache.clear()
            if self.state.a_tab == "signal":
                self._signal()
            elif self.state.a_tab == "forces":
                self._forces()

    def set_tab(self, tab: str) -> None:
        self.state.a_tab = tab
        self.refresh()

    def refresh(self) -> None:
        tab = self.state.a_tab
        if tab == "histories":
            self._histories()
        elif tab == "signal":
            self._signal()
        elif tab == "forces":
            self._forces()
        else:
            self._lines()

    # Chart interaction

    def on_range(self, key: str, x0: Any = None, x1: Any = None) -> None:
        """The view of a chart settled: draw its data again for that x range (finer bins, or samples read by
        byte range where the levels held are too coarse)."""
        lo, hi = _num(x0), _num(x1)
        self._ranges[key] = (lo, hi) if lo is not None and hi is not None and hi > lo else None
        if key == "a_chart_hist":
            self._histories(keep_view=True)

    def on_span(self, key: str, x0: Any, x1: Any) -> None:
        lo, hi = _num(x0), _num(x1)
        if lo is None or hi is None:
            return
        if key == "a_chart_sig":
            self.state.a_t0, self.state.a_t1 = number(lo), number(hi)
            self._signal()
        elif key == "a_chart_coef":
            self.state.a_ft0, self.state.a_ft1 = number(lo), number(hi)
            self._forces()

    # Histories

    def pick_signal(self, sid: str) -> None:
        """A signal clicked in the list: the Signal tab's one signal, or one more of the histories overlaid. By
        the tab this process shows, not by the control the page drew: a click just after a tab's button comes
        before the page has drawn the new tab, on the old tab's control."""
        if self.state.a_tab == "signal":
            self.set_signal(sid)
        else:
            self.toggle_signal(sid)

    def toggle_signal(self, sid: str) -> None:
        sel = list(self.state.a_hist_sel)
        if sid in sel:
            sel.remove(sid)
        else:
            sel.append(sid)
        self.state.a_hist_sel = sel
        self._histories()

    def set_smooth(self, text: str) -> None:
        self.state.a_smooth = str(text).strip()
        self._histories(keep_view=True)

    def _t_unit(self) -> str:
        return self._st.unit("run", "t") if self._st else ""

    def _smoothed(
        self, sig: Signal, width: float, t0: float | None, t1: float | None
    ) -> tuple[list[float], list[float]]:
        h = history(self._st, sig) if self._st else None
        if h is None:
            return [], []
        held = sum(b.n for b in h.levels[-1].all())
        if held <= SMOOTH_SAMPLES:
            t, y = samples(self._st, sig, self._files, t0, t1)  # type: ignore[arg-type]
        else:
            t, y, _ = h.means(t0, t1, MAX_POINTS)
        return running_mean(t, y, width)

    def _histories(self, keep_view: bool = False) -> None:
        s = self.state
        st = self._st
        sel = [self._sigs[i] for i in s.a_hist_sel if i in self._sigs]
        if st is None or not sel:
            s.a_chart_hist = {"series": [], "empty": "pick signals on the left"}
            return
        rng = self._ranges.get("a_chart_hist")
        t0, t1 = rng if rng else (None, None)
        units = {x.unit for x in sel}
        width = _num(s.a_smooth) or 0.0
        series, curves = [], []
        for sig in sel:
            x, y = curve(st, sig, self._files, t0, t1, MAX_POINTS)
            name = f"{sig.group} · {sig.label}" if len({q.label for q in sel}) < len(sel) else sig.label
            if len(units) > 1:
                name += f" ({sig.unit or 'nd'})"
            series.append(_series(name, x, y, faint=width > 0))
            curves.append(Curve(name, x, y))
            if width > 0:
                mx, my = self._smoothed(sig, width, t0, t1)
                mx, my = _decimate_pair(mx, my)
                series.append(_series(f"{name} · mean {number(width)}", mx, my))
                curves.append(Curve(f"{name} · mean", mx, my))
        unit = units.pop() if len(units) == 1 else ""
        ylabel = _axis(sel[0].label if len(sel) == 1 else "value", unit)
        xlabel = _axis("t", self._t_unit())
        s.a_chart_hist = {"series": series, "xlabel": xlabel, "ylabel": ylabel, "xname": "t", "keep": keep_view}
        self._specs["analysis-hist"] = PlotSpec(curves, xlabel, ylabel, "histories")

    # Signal

    def _all(self, sig: Signal) -> tuple[list[float], list[float]]:
        """Every sample of a signal, read once per poll."""
        h = history(self._st, sig) if self._st else None
        n = h.samples if h else 0
        got = self._cache.get(sig.id)
        if got is None or got[0] != n:
            got = (n, samples(self._st, sig, self._files))  # type: ignore[arg-type]
            self._cache[sig.id] = got
        return got[1]

    def set_signal(self, sid: str) -> None:
        self.state.a_sig = sid
        self.state.a_t0 = self.state.a_t1 = ""
        self._signal()

    def set_option(self, name: str, value: Any) -> None:
        self.state[name] = value
        self.refresh()

    def window_after_transient(self, which: str = "signal") -> None:
        st = self._st
        if st is None:
            return
        s = self.state
        if which == "forces":
            t, coef = self._coefficients()
            series = [coef[k] for k in ("Cd", "Cl", "Cs") if k in coef]
            key0, key1 = "a_ft0", "a_ft1"
        else:
            sig = self._sigs.get(s.a_sig)
            if sig is None:
                return
            t, y = self._all(sig)
            series = [y]
            key0, key1 = "a_t0", "a_t1"
        # The latest end over the signals: every one stationary from there (as [statistics.convergence]).
        ends = [transient_end(t, y) for y in series]
        if not series or any(e is None for e in ends):
            s[key0] = ""
            self.app.health.toast("Still drifting", "MSER-5 finds no end of the transient yet: run longer", INFO)
        else:
            s[key0] = number(max(e for e in ends if e is not None))
        s[key1] = ""
        self.refresh()

    def window_all(self, which: str = "signal") -> None:
        if which == "forces":
            self.state.a_ft0 = self.state.a_ft1 = ""
        else:
            self.state.a_t0 = self.state.a_t1 = ""
        self.refresh()

    def _st_reference(self) -> Reference:
        ref, errors = parse_reference(self.state.a_ref, self.state.a_nondim)
        self.state.a_ref_err = errors
        return ref

    def set_reference(self, key: str, number: str, unit: str = "") -> None:
        text = str(number) if key == "alpha" else entry_text(str(number), unit, DIMS[key][0])
        self.state.a_ref = {**self.state.a_ref, key: text.strip()}
        # The input anew: after "40 cm/s" typed again its field is unchanged, and the input would keep the text.
        self.state.a_ref_rev = {**self.state.a_ref_rev, key: self.state.a_ref_rev.get(key, 0) + 1}
        self._reference_fields()
        self.refresh()

    def set_reference_unit(self, key: str, number: str, old: str, new: str) -> None:
        dim = DIMS[key][0]
        if not str(number).strip():  # nothing to convert: the unit the number will be typed in
            fields = dict(self.state.a_ref_fields)
            fields[key] = {**fields[key], "unit": new}
            self.state.a_ref_fields = fields
            return
        try:
            text = entry_convert(str(number), old, new, dim)
        except UnitError:
            text = entry_text(str(number), new, dim)
        self.state.a_ref = {**self.state.a_ref, key: text}
        self._reference_fields()
        self.refresh()

    def _reference_fields(self) -> None:
        s = self.state
        fields = {}
        for key, (dim, _si) in DIMS.items():
            text = str(s.a_ref.get(key, ""))
            f = entry(text, dim, s.a_nondim or key == "alpha", self.app.session.preferred_units)
            fields[key] = {"number": f.number, "unit": f.unit, "choices": f.choices}
        s.a_ref_fields = fields

    def _signal(self) -> None:
        s = self.state
        sig = self._sigs.get(s.a_sig)
        s.a_sig_error = ""
        if self._st is None or sig is None:
            for key in ("a_chart_sig", "a_chart_psd", "a_chart_acf"):
                s[key] = {"series": [], "empty": "pick a signal"}
            s.a_stats = s.a_spec = []
            return
        t, y = self._all(sig)
        t0, t1 = _num(s.a_t0), _num(s.a_t1)
        t_unit = self._t_unit()
        x, yy = _decimate_pair(t, y)
        series = [_series(sig.label, x, yy, faint=True)]
        marks: list[dict[str, Any]] = []
        stats: WindowStats | None = None
        tol = (_num(s.a_tol) or 1.0) / 100.0
        try:
            stats = statistics(t, y, t0, t1, tolerance=tol)
        except ValueError as e:
            s.a_sig_error = str(e)
        span = [t0 if t0 is not None else (t[0] if t else None), t1 if t1 is not None else (t[-1] if t else None)]
        if stats is not None:
            band = {"x": stats.running_t, "lo": stats.running_lo, "hi": stats.running_hi}
            series.append(_series("running mean", stats.running_t, stats.running_mean, band=band))
            if stats.t_stationary is not None:
                marks.append({"x": stats.t_stationary, "label": "transient ends (MSER-5)"})
            s.a_stats = _stats_rows(stats, sig.unit, t_unit)
        else:
            s.a_stats = []
        s.a_chart_sig = {
            "series": series,
            "xlabel": _axis("t", t_unit),
            "ylabel": _axis(sig.label, sig.unit),
            "xname": "t",
            "span": span,
            "marks": marks,
            "select": True,
        }
        self._specs["analysis-signal"] = PlotSpec(
            [Curve(sig.label, x, yy)] + ([Curve("running mean", stats.running_t, stats.running_mean)] if stats else []),
            _axis("t", t_unit),
            _axis(sig.label, sig.unit),
            f"{sig.group} {sig.label}",
        )
        tw = [a for a in t if (t0 is None or a >= t0) and (t1 is None or a <= t1)]
        yw = [b for a, b in zip(t, y, strict=True) if (t0 is None or a >= t0) and (t1 is None or a <= t1)]
        f_unit = f"1/{t_unit}" if t_unit and t_unit != "1" else ""
        try:
            seglen = _num(s.a_seglen)
            dt = (tw[-1] - tw[0]) / max(len(tw) - 1, 1) if len(tw) > 1 else 1.0
            sp = welch(
                tw,
                yw,
                segment_length=round(seglen / dt) if seglen else None,
                overlap=min(0.9, max(0.0, (_num(s.a_overlap) or 0.0) / 100.0)),
                window=s.a_window,
                detrend=s.a_detrend,
            )
        except ValueError as e:
            s.a_chart_psd = {"series": [], "empty": f"spectrum: {e}"}
            s.a_chart_acf = {"series": [], "empty": ""}
            s.a_spec = []
            return
        ref = self._st_reference()
        st_peak = sp.strouhal(ref.length, ref.velocity) if not s.a_ref_err else None
        psd_unit = (f"({sig.unit})²·{t_unit}" if t_unit else f"({sig.unit})²") if sig.unit else ""
        s.a_chart_psd = {
            "series": [_series("PSD", sp.f, sp.psd)],
            "xlabel": _axis("f", f_unit),
            "ylabel": _axis(f"PSD of {sig.label}", psd_unit),
            "xname": "f",
            "logx": True,
            "logy": True,
            "marks": [
                {
                    "x": sp.peak_f,
                    "label": f"f {number(sp.peak_f)}" + (f" · St {number(st_peak)}" if st_peak is not None else ""),
                }
            ]
            if sp.peak_f
            else [],
        }
        self._specs["analysis-psd"] = PlotSpec(
            [Curve("PSD", sp.f, sp.psd)], _axis("f", f_unit), _axis("PSD", psd_unit), f"{sig.label} PSD"
        )
        s.a_spec = [
            {"k": "peak f", "v": number(sp.peak_f) if sp.peak_f else "–", "u": f_unit},
            {"k": "Strouhal", "v": number(st_peak) if st_peak is not None else "–", "u": ""},
            {"k": "period", "v": number(1 / sp.peak_f) if sp.peak_f else "–", "u": t_unit},
            {"k": "bin width", "v": number(sp.df), "u": f_unit},
            {"k": "segments", "v": f"{sp.segments} × {sp.length} samples", "u": ""},
            {"k": "resampled Δt", "v": number(sp.dt), "u": t_unit},
        ]
        ac = autocorrelation(tw, yw)
        marks = [{"x": ac.zero, "label": f"first zero {number(ac.zero)}"}] if ac.zero else []
        s.a_chart_acf = {
            "series": [_series("ρ", ac.lag, ac.rho)],
            "xlabel": _axis("lag", t_unit),
            "ylabel": "autocorrelation ρ",
            "xname": "lag",
            "marks": marks,
        }
        self._specs["analysis-acf"] = PlotSpec(
            [Curve("rho", ac.lag, ac.rho)], _axis("lag", t_unit), "autocorrelation", f"{sig.label} autocorrelation"
        )

    # Forces

    def set_force(self, fid: str) -> None:
        self.state.a_force = fid
        self._forces()

    def _coefficients(self) -> tuple[list[float], dict[str, list[float]]]:
        st, s = self._st, self.state
        if st is None or not s.a_force:
            return [], {}
        if s.a_force in st.csv:
            c = st.csv[s.a_force]
            cols = [col for group in force_columns(c.columns).values() for col in group]
            sigs = [self._sigs.get(f"csv:{s.a_force}#{col}#0") for col in cols]
        else:
            sigs = [x for x in self._sigs.values() if x.group == s.a_force and x.label.startswith("F")]
        sigs_ok = [x for x in sigs if x is not None]
        if not sigs_ok:
            return [], {}
        data = {x.label.split(" [")[0]: self._all(x) for x in sigs_ok}
        t = next(iter(data.values()))[0]
        forces = {k: v[1] for k, v in data.items() if len(v[1]) == len(t)}
        ref = self._st_reference()
        return t, coefficients(t, forces, ref, int(s.a_dim))

    def _forces(self) -> None:
        s = self.state
        t, coef = self._coefficients()
        if not t:
            s.a_chart_coef = {"series": [], "empty": "no [[forces]] in this run"}
            s.a_coef = []
            return
        t0, t1 = _num(s.a_ft0), _num(s.a_ft1)
        series, curves = [], []
        for name in ("Cd", "Cl", "Cs"):
            if name in coef:
                x, y = _decimate_pair(t, coef[name])
                series.append(_series(name, x, y))
                curves.append(Curve(name, x, y))
        span = [t0 if t0 is not None else t[0], t1 if t1 is not None else t[-1]]
        t_unit = self._t_unit()
        s.a_chart_coef = {
            "series": series,
            "xlabel": _axis("t", t_unit),
            "ylabel": "coefficient",
            "xname": "t",
            "span": span,
            "select": True,
        }
        self._specs["analysis-coef"] = PlotSpec(curves, _axis("t", t_unit), "coefficient", "force coefficients")
        rows = []
        tol = (_num(s.a_tol) or 1.0) / 100.0
        ref = self._st_reference()
        note = []
        for name, values in coef.items():
            try:
                ws = statistics(t, values, t0, t1, tolerance=tol)
            except ValueError as e:
                note.append(str(e))
                continue
            rows.append(
                {
                    "k": name,
                    "mean": number(ws.mean),
                    "ci": f"± {number(ws.mean_ci.half)}",
                    "rms": number(ws.rms),
                    "min": number(ws.minimum),
                    "max": number(ws.maximum),
                    "amp": number((ws.maximum - ws.minimum) / 2),
                    "ok": ws.converged,
                    "why": ws.reason or "converged",
                }
            )
        if "Cl" in coef:
            tw = [a for a in t if (t0 is None or a >= t0) and (t1 is None or a <= t1)]
            yw = [b for a, b in zip(t, coef["Cl"], strict=True) if (t0 is None or a >= t0) and (t1 is None or a <= t1)]
            try:
                sp = welch(tw, yw)
                stv = sp.strouhal(ref.length, ref.velocity)
                if sp.peak_f:
                    note.append(f"lift peaks at f {number(sp.peak_f)}" + (f", St {number(stv)}" if stv else ""))
            except ValueError:
                pass
        s.a_coef = rows
        q = ref.q
        surface = "L" if int(s.a_dim) == 2 else "S"
        note.insert(0, f"q∞ = {number(q)}, C = F / (q∞ {surface})" + (" per unit depth" if int(s.a_dim) == 2 else ""))
        s.a_coef_note = " · ".join(note)

    # Lines and walls

    def set_ex(self, name: str, value: Any) -> None:
        self.state[name] = value

    def extract(self) -> None:
        s = self.state
        record = self.app.session.project.run(s.a_run)
        src = (
            next((x for x in sources(self._st.outputs, self._st.end) if x["path"] == s.a_ex_src), None)
            if self._st
            else None
        )
        if src is None:
            s.a_ex_note = "no snapshot to extract from: the run needs a restart file or a whole-mesh VTKHDF snapshot"
            return
        fields = [f.strip() for f in str(s.a_ex_fields).split(",") if f.strip()]
        step = src.get("step")
        try:
            if s.a_ex_kind == "line":
                dim = int(s.a_dim)
                p0 = [float(v) for v in s.a_ex_p0[:dim]]
                p1 = [float(v) for v in s.a_ex_p1[:dim]]
                name = f"line_{step if step is not None else 'last'}"
                args = line_args(src["path"], step, p0, p1, int(s.a_ex_n), fields, name)
            else:
                name = f"{s.a_ex_zone}_{step if step is not None else 'last'}"
                args = wall_args(src["path"], step, self._wall_input(name, fields), name)
            launcher, cwd = self.app.session.post_launcher(record)
        except (ValueError, KeyError, OSError) as e:
            s.a_ex_note = str(e)
            return
        loop = asyncio.get_running_loop()

        def done(job: PostJob) -> None:
            loop.call_soon_threadsafe(self._extracted, job, name)

        self.app.session.postjobs.submit(PostJob("extract", s.a_run, "", args), launcher, cwd, done=done)
        s.a_ex_note = f"mallard post extract {s.a_ex_kind} from {Path(src['path']).name}: running"
        self._list_jobs()

    def _wall_input(self, name: str, fields: list[str]) -> str:
        """The run's input with the wall's boundary probe, written to the run's `extract/` where the run is."""
        files = self._files
        if files is None:
            raise ValueError("the run's directory is not reachable")
        text = b"".join(line + b"\n" for _, line in files.lines("input.toml")).decode("utf-8", "replace")
        rel = f"extract/{name}.toml"
        path = files.path(rel)
        files.location.remove([path])
        sink = files.location.appender(path)
        try:
            sink.write(wall_input(text, name, self.state.a_ex_zone, fields).encode("utf-8"))
        except BaseException:
            sink.abort()
            raise
        sink.close()
        return rel

    def _list_jobs(self) -> None:
        jobs = [j.to_dict() for j in self.app.session.postjobs.jobs.values() if j.run == self.state.a_run]
        self.state.a_ex_jobs = list(reversed(jobs))[:6]

    def _extracted(self, job: PostJob, name: str) -> None:
        with self.state:
            self._list_jobs()
            if job.state != "done":
                self.state.a_ex_note = f"extract failed: {job.error}"
                return
            path = probe_file(job.report, name)
            results = [r for r in self.state.a_ex_results if r["path"] != path]
            self.state.a_ex_results = [{"path": path, "label": name}, *results]
            self.state.a_ex_result = path
            self.state.a_ex_y = ""
            self.state.a_ex_note = f"extracted {path}"
            self._lines()

    def pick_result(self, path: str) -> None:
        self.state.a_ex_result = path
        self.state.a_ex_y = ""
        self._lines()

    def _lines(self) -> None:
        s = self.state
        if not s.a_ex_result or self._files is None:
            s.a_chart_line = {"series": [], "empty": "extract a line or a wall"}
            return
        try:
            data = b"".join(line + b"\n" for _, line in self._files.lines(s.a_ex_result))
            prof = read_profile(data.decode("utf-8", "replace"), s.a_ex_result)
        except (OSError, ValueError) as e:
            s.a_chart_line = {"series": [], "empty": str(e)}
            return
        names = list(prof.columns)
        s.a_ex_ys = names
        if s.a_ex_y not in names:
            s.a_ex_y = next((n for n in ("CP", "P", "U_X") if n in names), names[0] if names else "")
            s.a_ex_yunit = ""
        y_unit = prof.units.get(s.a_ex_y, "1")
        s.a_ex_yunits = unit_choices(y_unit)
        s.a_ex_xunits = unit_choices(prof.x_unit)
        if s.a_ex_yunit not in s.a_ex_yunits:
            s.a_ex_yunit = s.a_ex_yunits[0] if s.a_ex_yunits else ""
        if s.a_ex_xunit not in s.a_ex_xunits:
            s.a_ex_xunit = s.a_ex_xunits[0] if s.a_ex_xunits else ""
        try:
            ref = self._st_reference()
            if s.a_ex_norm:
                x = [
                    v / ref.length
                    for v in convert_values(prof.x, prof.x_unit, s.a_ex_xunits[0] if s.a_ex_xunits else "")
                ]
                xlabel = f"{prof.x_name} / L"
            else:
                x = convert_values(prof.x, prof.x_unit, s.a_ex_xunit or prof.x_unit)
                xlabel = _axis(prof.x_name, s.a_ex_xunit if prof.x_unit != "1" else "")
            y = convert_values(prof.columns.get(s.a_ex_y, []), y_unit, s.a_ex_yunit or y_unit)
        except ValueError as e:
            s.a_chart_line = {"series": [], "empty": str(e)}
            return
        ylabel = _axis(s.a_ex_y, s.a_ex_yunit if y_unit != "1" else "")
        s.a_chart_line = {"series": [_series(s.a_ex_y, x, y)], "xlabel": xlabel, "ylabel": ylabel, "xname": prof.x_name}
        step = int(prof.step) if prof.step is not None else "–"
        s.a_ex_note = f"{Path(s.a_ex_result).name} · step {step} · t {number(prof.t)} · {len(x)} samples"
        self._specs["analysis-line"] = PlotSpec([Curve(s.a_ex_y, x, y)], xlabel, ylabel, Path(s.a_ex_result).stem)

    # Export

    def export(self, key: str, fmt: str) -> None:
        """CSV of every sample of the shown signals in the view, read from the run's files; SVG of a plot."""
        from ..export import plot_csv, svg_pair

        spec = self._specs.get(key)
        out_dir = self.app.session.project.root / "exports" / (self.state.a_run or "case")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            if fmt == "csv" and key == "analysis-hist" and self._st is not None and self._files is not None:
                sel = [self._sigs[i] for i in self.state.a_hist_sel if i in self._sigs]
                rng = self._ranges.get("a_chart_hist")
                buf = io.StringIO()
                rows = export_csv(self._st, sel, self._files, buf, *(rng or (None, None)))
                path = out_dir / "histories.csv"
                path.write_text(buf.getvalue(), encoding="utf-8")
                self.app.health.toast(f"Exported every sample ({rows} rows)", str(path), INFO, path=str(path))
                return
            if spec is None or not spec.curves:
                self.app.health.toast("Nothing to export", "the plot has no data yet", INFO)
                return
            stem = out_dir / safe_name(spec.title or key)
            if fmt == "csv":
                path = stem.with_suffix(".csv")
                path.write_text(plot_csv(spec), encoding="utf-8")
            else:
                path = svg_pair(spec, stem)[0]
            self.app.health.toast(f"Exported {fmt.upper()}", str(path), INFO, path=str(path))
        except (OSError, ValueError) as e:
            self.app.health.toast("Export failed", str(e), CRITICAL)

    # Layout

    def layout_client(self) -> None:
        for key in CHARTS:
            client.ClientStateChange(value=key, change=f"window.mallardChart && window.mallardChart.update('{key}')")

    def _chart(self, key: str, height: str = "100%") -> None:
        html.Div(classes="m-chart", id=key, style=f"height: {height}")

    def _export_buttons(self, key: str, csv_title: str = "The plot's data as CSV") -> None:
        with html.Span(classes="export"):
            html.Button("CSV", classes="tb", click=(self.export, f"['{key}', 'csv']"), title=csv_title)
            html.Button(
                "SVG",
                classes="tb",
                click=(self.export, f"['{key}', 'svg']"),
                title="The plot as a light and a dark SVG, in the style of Mallard's documentation figures",
            )

    def _text(self, label: str, var: str, title: str = "", placeholder: str = "") -> None:
        with html.Div(classes="prow", title=title):
            html.Div(label, classes="plabel")
            with html.Div(classes="pval"):
                html.Input(
                    value=(var,),
                    aria_label=label,
                    placeholder=placeholder,
                    change=(self.set_option, f"['{var}', $event.target.value]"),
                    keydown_enter="$event.target.blur()",
                )

    def _select(self, label: str, var: str, options: str, title: str = "") -> None:
        with html.Div(classes="prow", title=title):
            html.Div(label, classes="plabel")
            with html.Div(classes="pval"):
                with html.Select(aria_label=label, change=(self.set_option, f"['{var}', $event.target.value]")):
                    html.Option("{{ o }}", v_for=f"o in {options}", key="o", value=("o",), selected=(f"o === {var}",))

    def layout(self) -> None:
        with html.Div(classes="panel"):
            splitter("analysis-left", "the signal list", minimum=180, maximum=640)
            with html.Div(classes="panel-head"):
                html.Span("Analysis")
                html.Span("{{ a_run }}", classes="mono")
            with html.Div(classes="prow"):
                html.Div("run", classes="plabel")
                with html.Div(classes="pval"):
                    with html.Select(aria_label="analysis run", change=(self.select_run, "[$event.target.value]")):
                        html.Option(
                            "{{ r.name }}",
                            v_for="r in runs",
                            key="r.name",
                            value=("r.name",),
                            selected=("r.name === a_run",),
                        )
            html.Div("{{ a_note }}", classes="muted a-note")
            with html.Div(classes="scroll a-signals"):
                with html.Div(v_for="g in a_groups", key="g"):
                    html.Div("{{ g }}", classes="group-head mono")
                    with html.Label(
                        v_for="x in a_signals.filter(x => x.group === g)",
                        key="x.id",
                        classes=("['a-sig', {on: a_tab === 'signal' ? x.id === a_sig : a_hist_sel.includes(x.id)}]",),
                        title=("x.id",),
                    ):
                        html.Input(
                            type="checkbox",
                            v_if="a_tab !== 'signal'",
                            checked=("a_hist_sel.includes(x.id)",),
                            change=(self.pick_signal, "[x.id]"),
                            aria_label=("x.label",),
                        )
                        html.Input(
                            type="radio",
                            v_if="a_tab === 'signal'",
                            name="a-signal",
                            checked=("x.id === a_sig",),
                            change=(self.pick_signal, "[x.id]"),
                            aria_label=("x.label",),
                        )
                        html.Span("{{ x.label }}", classes="tlabel")
                        html.Span("{{ x.unit }}", classes="rv mono")
                html.Div(
                    "No signals: the run has written no samples yet.",
                    classes="notice",
                    v_if="a_run && !a_signals.length",
                )
                html.Div("No runs yet", classes="notice", v_if="!a_run")
        with html.Div(classes="center"):
            empty_state(
                "analysis",
                "Nothing to analyze yet",
                "A run's histories, signal statistics and spectra, force coefficients, and lines and walls "
                "extracted from its snapshots, read from the run's own files.",
                "!a_run",
                ("Go to Case setup", "case", (self.app.set_page, "['case']")),
            )
            with html.Div(classes="tabs", role="tablist", v_show="a_run"):
                for key, label in TABS:
                    html.Button(
                        label,
                        classes=(f"a_tab === '{key}' ? 'tab on' : 'tab'",),
                        role="tab",
                        click=(self.set_tab, f"['{key}']"),
                    )
            with html.Div(v_show="a_run", style="display: contents"):
                self._histories_center()
                self._signal_center()
                self._forces_center()
                self._lines_center()
        with html.Div(classes="panel right"):
            splitter("analysis-right", "the display panel", edge="start", minimum=240, maximum=760)
            empty_state(
                "sliders",
                "Display and extract",
                "Averaging, windows, reference values and extracts for the run you pick.",
                "!a_run",
                compact=True,
            )
            with html.Div(classes="panel-head", v_show="a_run"):
                html.Span(
                    "{{ {histories: 'Display', signal: 'Window and spectrum', forces: 'Coefficients', "
                    "lines: 'Extract'}[a_tab] }}"
                )
            with html.Div(classes="scroll", v_show="a_run"):
                self._histories_right()
                self._signal_right()
                self._forces_right()
                self._lines_right()
                self._reference_rows()

    def _histories_center(self) -> None:
        with html.Div(v_if="a_tab === 'histories'", classes="a-pane"):
            with html.Div(classes="plot-head"):
                html.Span("{{ a_hist_sel.length }} signal{{ a_hist_sel.length === 1 ? '' : 's' }}")
                self._export_buttons(
                    "analysis-hist", "Every sample of these signals in the view, read from the run's files"
                )
            self._chart("a_chart_hist", "calc(100% - 32px)")

    def _histories_right(self) -> None:
        with html.Div(v_if="a_tab === 'histories'"):
            self._text(
                "running mean",
                "a_smooth",
                "A centered running mean over this many time units, drawn over the samples",
                "time span",
            )
            html.Div(
                "Wheel zooms the time axis at the cursor, shift-wheel the value axis; drag a box to zoom, double "
                "click to fit. Zooming past what memory holds reads those samples from the run's files.",
                classes="notice",
            )

    def _signal_center(self) -> None:
        with html.Div(v_if="a_tab === 'signal'", classes="a-pane a-grid"):
            with html.Div(classes="a-cell wide"):
                with html.Div(classes="plot-head"):
                    html.Span("Signal, its running mean and 95 % interval over the window")
                    self._export_buttons("analysis-signal")
                self._chart("a_chart_sig", "calc(100% - 32px)")
            with html.Div(classes="a-cell"):
                with html.Div(classes="plot-head"):
                    html.Span("Power spectral density (Welch)")
                    self._export_buttons("analysis-psd")
                self._chart("a_chart_psd", "calc(100% - 32px)")
            with html.Div(classes="a-cell"):
                with html.Div(classes="plot-head"):
                    html.Span("Autocorrelation")
                    self._export_buttons("analysis-acf")
                self._chart("a_chart_acf", "calc(100% - 32px)")

    def _signal_right(self) -> None:
        with html.Div(v_if="a_tab === 'signal'"):
            with html.Div(classes="group-head"):
                html.Span("WINDOW")
                html.Span("drag on the plot in window mode", classes="note")
            self._text("from t", "a_t0", "Start of the window (blank: the first sample)", "first")
            self._text("to t", "a_t1", "End of the window (blank: the latest sample, following a run)", "latest")
            with html.Div(classes="prow"):
                html.Div("", classes="plabel")
                with html.Div(classes="pval", style="gap: 3px; padding: 0 6px"):
                    html.Button(
                        "after transient",
                        classes="tb",
                        click=(self.window_after_transient, "['signal']"),
                        title="From where MSER-5 puts the end of the initial transient (Mallard's method)",
                    )
                    html.Button("all", classes="tb", click=(self.window_all, "['signal']"))
            self._text("tolerance %", "a_tol", "Converged when the mean's 95 % half width is within this", "1")
            html.Div("{{ a_sig_error }}", classes="notice", v_if="a_sig_error")
            with html.Div(classes="group-head"):
                html.Span("STATISTICS")
                html.Span("batch means, Student's t", classes="note")
            with html.Div(v_for="r in a_stats", key="r.k", classes="prow"):
                html.Div("{{ r.k }}", classes="plabel")
                html.Div(
                    "{{ r.v }} {{ r.u }}",
                    classes=("['pval ro mono', r.cls || '']",),
                    title=("r.title || ''",),
                )
            with html.Div(classes="group-head"):
                html.Span("SPECTRUM")
                html.Span("Welch", classes="note")
            self._select("window", "a_window", "a_windows")
            self._text("segment", "a_seglen", "Segment length in time units (blank: 4 segments)", "4 segments")
            self._text("overlap %", "a_overlap", "Overlap of the segments", "50")
            self._select("detrend", "a_detrend", "a_detrends", "What each segment has removed before its FFT")
            with html.Div(v_for="r in a_spec", key="r.k", classes="prow"):
                html.Div("{{ r.k }}", classes="plabel")
                html.Div("{{ r.v }} {{ r.u }}", classes="pval ro mono")

    def _forces_center(self) -> None:
        with html.Div(v_if="a_tab === 'forces'", classes="a-pane"):
            with html.Div(classes="plot-head"):
                with html.Select(aria_label="forces", change=(self.set_force, "[$event.target.value]")):
                    html.Option(
                        "{{ f.label }}",
                        v_for="f in a_forces",
                        key="f.id",
                        value=("f.id",),
                        selected=("f.id === a_force",),
                    )
                self._export_buttons("analysis-coef")
            self._chart("a_chart_coef", "calc(60% - 32px)")
            with html.Table(classes="a-table", v_if="a_coef.length"):
                with html.Tr():
                    for h in ("", "mean", "95 %", "rms′", "min", "max", "amplitude", ""):
                        html.Th(h)
                with html.Tr(v_for="r in a_coef", key="r.k"):
                    html.Td("{{ r.k }}", classes="k")
                    for k in ("mean", "ci", "rms", "min", "max", "amp"):
                        html.Td(f"{{{{ r.{k} }}}}", classes="mono")
                    html.Td(
                        "{{ r.ok ? 'converged' : 'not converged' }}",
                        classes=("r.ok ? 'okc' : 'warn'",),
                        title=("r.why",),
                    )
            html.Div("{{ a_coef_note }}", classes="muted a-note")

    def _forces_right(self) -> None:
        with html.Div(v_if="a_tab === 'forces'"):
            with html.Div(classes="group-head"):
                html.Span("WINDOW")
                html.Span("drag on the plot in window mode", classes="note")
            self._text("from t", "a_ft0", "Start of the averaging window", "first")
            self._text("to t", "a_ft1", "End of the averaging window", "latest")
            with html.Div(classes="prow"):
                html.Div("", classes="plabel")
                with html.Div(classes="pval", style="gap: 3px; padding: 0 6px"):
                    html.Button(
                        "after transient",
                        classes="tb",
                        click=(self.window_after_transient, "['forces']"),
                        title="From where MSER-5 puts the end of the lift's (else the drag's) transient",
                    )
                    html.Button("all", classes="tb", click=(self.window_all, "['forces']"))

    def _reference_rows(self) -> None:
        with html.Div(v_if="a_tab !== 'histories'"):
            with html.Div(classes="group-head"):
                html.Span("REFERENCE")
                html.Span("{{ a_nondim ? 'nondimensional' : 'with units: 30 m/s, 1.2 kg/m^3' }}", classes="note")
            rows = [
                ("rho", "ρ∞", "Free-stream density"),
                ("velocity", "U∞", "Free-stream speed: q∞ = ρ∞ U∞² / 2, and St = f L / U∞"),
                ("length", "L", "Reference length: Cd and Cl per unit depth in 2D, and St = f L / U∞"),
                ("area", "S", "Reference area of the coefficients in 3D"),
                ("alpha", "α (deg)", "Angle of the free stream in the x-y plane: drag along it, lift normal"),
            ]
            for key, label, title in rows:
                with html.Div(classes="prow", title=title, **({"v_if": "a_dim === 3"} if key == "area" else {})):
                    html.Div(label, classes="plabel")
                    with html.Div(classes="pval"):
                        f = f"a_ref_fields.{key}"
                        html.Input(
                            value=(f"{f}?.number",),
                            key=(f"'{key}-' + (a_ref_rev.{key} || 0)",),
                            aria_label=f"reference {key}",
                            spellcheck="false",
                            change=(self.set_reference, f"['{key}', $event.target.value, {f}?.unit]"),
                            keydown_enter="$event.target.blur()",
                            classes=(f"['qnum', a_ref_err.{key} ? 'bad' : '']",),
                        )
                        with html.Select(
                            v_if=f"{f}?.choices.length",
                            classes="qunit",
                            aria_label=f"reference {key} unit",
                            change=(
                                self.set_reference_unit,
                                f"['{key}', {f}.number, {f}.unit, $event.target.value]",
                            ),
                        ):
                            html.Option(
                                "{{ u }}",
                                v_for=f"u in {f}.choices",
                                key="u",
                                value=("u",),
                                selected=(f"u === {f}.unit",),
                            )
                html.Div(f"{{{{ a_ref_err.{key} }}}}", classes="field-error", v_if=f"a_ref_err.{key}")

    def _lines_center(self) -> None:
        with html.Div(v_if="a_tab === 'lines'", classes="a-pane"):
            with html.Div(classes="plot-head"):
                with html.Select(
                    aria_label="extracted", v_if="a_ex_result", change=(self.pick_result, "[$event.target.value]")
                ):
                    html.Option(
                        "{{ r.label }}",
                        v_for="r in a_ex_results",
                        key="r.path",
                        value=("r.path",),
                        selected=("r.path === a_ex_result",),
                    )
                with html.Select(
                    aria_label="profile field",
                    v_if="a_ex_result",
                    change=(self.set_line_option, "['a_ex_y', $event.target.value]"),
                ):
                    html.Option("{{ o }}", v_for="o in a_ex_ys", key="o", value=("o",), selected=("o === a_ex_y",))
                html.Span("in", classes="muted", v_if="a_ex_yunits.length")
                with html.Select(
                    aria_label="field unit",
                    v_if="a_ex_yunits.length",
                    change=(self.set_line_option, "['a_ex_yunit', $event.target.value]"),
                ):
                    html.Option(
                        "{{ o }}", v_for="o in a_ex_yunits", key="o", value=("o",), selected=("o === a_ex_yunit",)
                    )
                html.Span("against", classes="muted", v_if="a_ex_result")
                with html.Select(
                    aria_label="abscissa unit",
                    v_if="a_ex_xunits.length && !a_ex_norm",
                    change=(self.set_line_option, "['a_ex_xunit', $event.target.value]"),
                ):
                    html.Option(
                        "{{ o }}", v_for="o in a_ex_xunits", key="o", value=("o",), selected=("o === a_ex_xunit",)
                    )
                with html.Label(classes="chip", v_if="a_ex_result"):
                    html.Input(
                        type="checkbox",
                        checked=("a_ex_norm",),
                        change=(self.set_line_option, "['a_ex_norm', $event.target.checked]"),
                        aria_label="divide by L",
                        style="width: auto; height: auto; margin: 0",
                    )
                    html.Span("/ L")
                self._export_buttons("analysis-line")
            self._chart("a_chart_line", "calc(100% - 56px)")
            html.Div("{{ a_ex_note }}", classes="muted a-note")

    def set_point(self, which: str, i: int, value: str) -> None:
        name = f"a_ex_{which}"
        p = list(self.state[name])
        p[int(i)] = str(value).strip()
        self.state[name] = p

    def set_line_option(self, name: str, value: Any) -> None:
        self.state[name] = value
        self._lines()

    def _lines_right(self) -> None:
        with html.Div(v_if="a_tab === 'lines'"):
            with html.Div(classes="prow", title="A restart file or a whole-mesh VTKHDF snapshot of the run"):
                html.Div("snapshot", classes="plabel")
                with html.Div(classes="pval"):
                    with html.Select(aria_label="snapshot", change=(self.set_ex, "['a_ex_src', $event.target.value]")):
                        html.Option(
                            "{{ x.label }}",
                            v_for="x in a_ex_sources",
                            key="x.path",
                            value=("x.path",),
                            selected=("x.path === a_ex_src",),
                        )
            html.Div(
                "No restart file or whole-mesh VTKHDF snapshot: `post extract` reads those (add a [[write_data]] "
                'with format = "restart").',
                classes="notice",
                v_if="!a_ex_sources.length",
            )
            with html.Div(classes="prow"):
                html.Div("what", classes="plabel")
                with html.Div(classes="pval", style="gap: 3px; padding: 0 6px"):
                    for kind, label, fields in (("wall", "wall", "P,CP,CF,Y_PLUS"), ("line", "line", "P,U")):
                        html.Button(
                            label,
                            classes=(f"a_ex_kind === '{kind}' ? 'tb on' : 'tb'",),
                            click=f"a_ex_kind = '{kind}'; a_ex_fields = '{fields}'",
                        )
            with html.Div(classes="prow", v_if="a_ex_kind === 'wall'"):
                html.Div("wall", classes="plabel")
                with html.Div(classes="pval"):
                    with html.Select(aria_label="wall", change=(self.set_ex, "['a_ex_zone', $event.target.value]")):
                        html.Option(
                            "{{ w }}", v_for="w in a_ex_walls", key="w", value=("w",), selected=("w === a_ex_zone",)
                        )
            for which, label in (("p0", "from"), ("p1", "to")):
                with html.Div(classes="prow", v_if="a_ex_kind === 'line'"):
                    html.Div(label, classes="plabel")
                    with html.Div(classes="pval", style="gap: 3px"):
                        for i, axis in enumerate("xyz"):
                            html.Input(
                                v_if=f"{i} < a_dim",
                                value=(f"a_ex_{which}[{i}]",),
                                aria_label=f"line {label} {axis}",
                                change=(self.set_point, f"['{which}', {i}, $event.target.value]"),
                                style="min-width: 0",
                            )
            with html.Div(classes="prow", v_if="a_ex_kind === 'line'"):
                html.Div("points", classes="plabel")
                with html.Div(classes="pval"):
                    html.Input(value=("a_ex_n",), aria_label="line points", change="a_ex_n = $event.target.value")
            with html.Div(classes="prow", title="Fields of the extraction, as [[probes]] take them"):
                html.Div("fields", classes="plabel")
                with html.Div(classes="pval"):
                    html.Input(
                        value=("a_ex_fields",), aria_label="extract fields", change="a_ex_fields = $event.target.value"
                    )
            with html.Div(classes="actions", style="padding: 6px"):
                html.Button(
                    "Extract",
                    classes="btn primary",
                    click=self.extract,
                    disabled=("!a_ex_src",),
                    title="mallard post extract --region where the run is: a post job, never re-sampled here",
                )
            with html.Div(v_for="j in a_ex_jobs", key="j.id", classes="prow"):
                html.Div("{{ j.state }}", classes="plabel")
                html.Div("{{ j.summary || j.command }}", classes="pval ro muted", title=("j.command",))


def _decimate_pair(t: Sequence[float], y: Sequence[float]) -> tuple[list[float], list[float]]:
    from ..signalsource import decimate

    return decimate(list(t), list(y), MAX_POINTS)


def _stats_rows(ws: WindowStats, unit: str, t_unit: str) -> list[dict[str, Any]]:
    rel = ws.mean_ci.relative
    return [
        {"k": "window", "v": f"{number(ws.t0)} – {number(ws.t1)}", "u": t_unit},
        {"k": "samples", "v": f"{ws.n} ({number(ws.n_eff)} independent)", "u": ""},
        {"k": "mean", "v": f"{number(ws.mean)} ± {number(ws.mean_ci.half)}", "u": unit},
        {
            "k": "± relative",
            "v": (f"{100 * rel:.2g} % of the mean" if math.isfinite(rel) else "–")
            + (f" · {100 * ws.mean_ci.half / ws.rms:.2g} % of rms′" if ws.rms else ""),
            "u": "",
        },
        {"k": "rms′", "v": number(ws.rms), "u": unit},
        {"k": "variance", "v": f"{number(ws.variance_ci.value)} ± {number(ws.variance_ci.half)}", "u": ""},
        {"k": "min / max", "v": f"{number(ws.minimum)} / {number(ws.maximum)}", "u": unit},
        {"k": "τ (autocorr.)", "v": f"{number(ws.tau)} samples", "u": ""},
        {
            "k": "transient",
            "v": f"ends at {number(ws.t_stationary)}" if ws.t_stationary is not None else "still drifting",
            "u": t_unit if ws.t_stationary is not None else "",
        },
        {
            "k": "converged",
            "v": "yes" if ws.converged else "no",
            "u": "",
            "cls": "okc" if ws.converged else "warn",
            "title": ws.reason or f"mean within ± {100 * ws.tolerance:.2g} %, batches over 10 τ",
        },
    ]
