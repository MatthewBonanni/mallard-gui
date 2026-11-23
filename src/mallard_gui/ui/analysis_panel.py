"""The case view's analysis panel: boundary sanity, the initial state and the mesh quality, each an overlay of
the case view drawn from what one `mallard check` writes (the client's `analyze`). The app reaches it at a few
points: its button beside the center tabs, its panel over the viewport, `view_loaded` after the case view reloads
and `mark_stale` after the input changes."""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from ..analysis.overlays import BoundaryOverlay, InitialOverlay, QualityOverlay
from ..analysis.report import Histogram, PeriodicPair, histograms, periodic_pairs, unassigned, y_plus
from ..mallard import Analysis, MallardError
from ..plots import axis_label, nice_ticks
from ..viewer.colormaps import COLORMAPS, DEFAULT, css_gradient, svg_swatch
from .dropdown import dropdown
from .fmt import grouped, number

if TYPE_CHECKING:
    from .app import App

TABS = (("boundaries", "Boundaries"), ("initial", "Initial state"), ("quality", "Mesh quality"))
COMPONENTS = ("magnitude", "x", "y", "z")
UNITS = {"RHO": "kg/m^3", "U": "m/s", "P": "Pa", "T": "K"}


class AnalysisPanel:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self.scene = app.scene
        self.out_dir = Path(tempfile.mkdtemp(prefix="mallard-gui-analysis-"))
        self.analysis: Analysis | None = None
        self.boundary = BoundaryOverlay(self.scene)
        self.initial = InitialOverlay(self.scene)
        self.quality = QualityOverlay(self.scene)
        self._hists: dict[str, Histogram] = {}
        self._task: asyncio.Task[None] | None = None
        self._initial_task: asyncio.Task[None] | None = None
        self._initial_stale = False
        self._for_tree = False  # opened by the case tree's Initial state: it goes when another node is chosen
        s = self.state
        s.ana_open = False
        s.ana_tab = "boundaries"
        s.ana_busy = False
        s.ana_doing = ""
        s.ana_error = ""
        s.ana_stale = False
        s.ana_normals = False
        s.ana_yplus = True
        s.ana_unassigned = []
        s.ana_periodic = []
        s.ana_yp = {}
        s.ana_bar = {}
        s.ana_fields = []
        s.ana_field = ""
        s.ana_component = "magnitude"
        s.ana_cmap = DEFAULT
        s.ana_cmaps = [{"value": n, "label": n, "swatch": svg_swatch(n)} for n in COLORMAPS]
        s.ana_range = ["", ""]
        s.ana_hists = []
        s.ana_metric = ""
        s.ana_bin = -1
        s.ana_selection = ""
        s.ana_worst_on = True
        s.ana_note = ""
        s.change("ana_tab")(lambda **_: self.apply())
        s.change("clip", "clip_origin", "clip_normal", "clip_flip", "theme")(self._view_changed)

    # Running Mallard

    def toggle(self) -> None:
        s = self.state
        s.ana_open = not s.ana_open
        self._for_tree = False
        if s.ana_open and (self.analysis is None or s.ana_stale):
            self.refresh()
        else:
            self.apply()

    def refresh(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.ensure_future(self._analyze())

    # The initial state alone: what the case tree's Initial state shows, without the rest of the analysis

    def show_initial(self) -> None:
        """The initial state on the case view, now: the panel on its Initial state tab, the field drawn as soon
        as Mallard has written it (at once when it is already held for this input)."""
        s = self.state
        self._for_tree = not s.ana_open or self._for_tree
        s.ana_open, s.ana_tab = True, "initial"
        self._want_initial()

    def leave_initial(self) -> None:
        """Another node of the case tree is chosen: the preview it opened goes."""
        if self._for_tree and self.state.ana_open and self.state.ana_tab == "initial":
            self.state.ana_open = False
            self.apply()
        self._for_tree = False

    def _want_initial(self) -> None:
        if self.initial.data is not None and not self._initial_stale:
            self.apply()
        elif not (self._initial_task and not self._initial_task.done()):
            self._initial_task = asyncio.ensure_future(self._load_initial())

    async def _load_initial(self) -> None:
        s, ses = self.state, self.app.session
        started = time.monotonic()
        with s:
            s.ana_busy, s.ana_error, s.ana_doing = True, "", "mallard: the initial state ..."

        async def clock() -> None:  # a large case takes a while to set up: say how long it has been
            while True:
                await asyncio.sleep(1.0)
                with s:
                    s.ana_doing = f"mallard: the initial state ... {time.monotonic() - started:.0f} s"

        ticking = asyncio.ensure_future(clock())
        try:
            path = await asyncio.to_thread(ses.mallard.initial_state, ses.input_path, self.out_dir)
        except (MallardError, OSError, ValueError) as e:
            with s:
                s.ana_busy, s.ana_doing, s.ana_error = False, "", f"initial state: {e}"
            return
        finally:
            ticking.cancel()
        with s:
            self._load_initial_file(path)
            self._initial_stale = False
            s.ana_busy, s.ana_doing = False, ""
            self.apply()

    def _load_initial_file(self, path: Path | None) -> None:
        """Read the snapshot and choose what to show first: the field kept if it is still there, else the first
        of U, P, T and RHO that varies over the domain (a uniform one is one color), else the first there is."""
        s = self.state
        self.initial.load(path)
        s.ana_fields = self.initial.fields()
        names = [f["name"] for f in s.ana_fields]
        if s.ana_field not in names:
            varying = [n for n in ("U", "P", "T", "RHO") if n in names and _varies(self.initial.range(n))]
            s.ana_field = (varying or [n for n in ("U", "P", "T", "RHO") if n in names] or names or [""])[0]
            s.ana_range = ["", ""]

    async def _analyze(self) -> None:
        s, ses = self.state, self.app.session
        with s:
            s.ana_busy, s.ana_error = True, ""
        try:
            analysis = await asyncio.to_thread(ses.mallard.analyze, ses.input_path, self.out_dir)
            if analysis.initial is None and analysis.report.data.get("setup_done"):
                # A build whose check does not write the initial state: Mallard writes it another way.
                with contextlib.suppress(MallardError, OSError, ValueError):
                    analysis.initial = await asyncio.to_thread(ses.mallard.initial_state, ses.input_path, self.out_dir)
        except (MallardError, OSError) as e:
            with s:
                s.ana_busy, s.ana_error = False, str(e)
            return
        with s:
            self.load(analysis)
            self._initial_stale = False
            s.ana_busy, s.ana_stale = False, False
            self.apply()

    def load(self, analysis: Analysis) -> None:
        """Read what the check wrote and fill the panel from the report."""
        self.analysis = analysis
        s, data = self.state, analysis.report.data
        self.boundary.load(analysis.view)
        self._load_initial_file(analysis.initial)
        cells = int((data.get("cost") or {}).get("cells") or 0)
        self.quality.load(analysis.mesh_fields, cells)
        s.ana_unassigned = [{"zone": z, "faces": n} for z, n in unassigned(data).items()]
        s.ana_periodic = [_periodic_row(p) for p in periodic_pairs(data)]
        yp = y_plus(data)
        s.ana_yp = {**yp, "max": number(yp["max"]), "mean": number(yp["mean"])} if yp else {}
        self._hists = {h.metric: h for h in histograms(data)}
        s.ana_hists = [_histogram_row(h) for h in self._hists.values()]
        if s.ana_metric not in self._hists:
            s.ana_metric, s.ana_bin = next(iter(self._hists), ""), -1
        notes = []
        if not analysis.report.data.get("setup_done"):
            notes.append("the setup stopped: the mesh alone is drawn (see Problems)")
        if self.quality.too_large:
            notes.append(f"{grouped(cells)} cells: histograms from Mallard, cells not read")
        s.ana_note = "; ".join(notes)

    def mark_stale(self) -> None:
        """The input changed: what is shown is of the previous input until Refresh."""
        if self.analysis is not None:
            self.state.ana_stale = True
        if self.initial.data is not None:
            self._initial_stale = True

    def view_loaded(self) -> None:
        if self.state.ana_open:
            self.apply()

    # Overlays

    def apply(self) -> None:
        s = self.state
        for overlay in (self.boundary, self.initial, self.quality):
            overlay.clear()
        s.ana_bar = {}
        if s.ana_open and s.ana_tab == "initial":
            self._show_initial()  # held on its own: it needs no analysis
        elif s.ana_open and self.analysis is not None:
            if s.ana_tab == "boundaries":
                self._show_boundaries()
            elif s.ana_tab == "quality":
                self._show_quality()
        self.app._update_view()

    def _view_changed(self, **_: Any) -> None:
        if self.state.ana_open and (self.analysis is not None or self.initial.data is not None):
            self.apply()

    def _show_boundaries(self) -> None:
        s = self.state
        zones = [b.get("zone", "") for b in (self.analysis.report.boundaries if self.analysis else [])]
        missing = [u["zone"] for u in s.ana_unassigned]
        rng = self.boundary.y_plus_range()
        lo, hi = (0.0, max(rng[1], 1.0)) if rng else (0.0, 1.0)
        self.boundary.show(zones, missing, s.ana_normals, s.ana_yplus, s.ana_cmap, (lo, hi))
        if s.ana_yplus and rng:
            s.ana_bar = _colorbar("first-cell y+", "", s.ana_cmap, lo, hi)

    def _show_initial(self) -> None:
        s = self.state
        if not s.ana_field:
            return
        component = self._component()
        auto = self.initial.range(s.ana_field, component)
        if auto is None:
            return
        lo, hi = auto
        try:
            lo, hi = float(s.ana_range[0]), float(s.ana_range[1])
        except (TypeError, ValueError, IndexError):
            s.ana_range = [number(lo), number(hi)]
        self.initial.show(s.ana_field, s.ana_cmap, (lo, hi), component)
        name = s.ana_field + ("" if component < 0 or self._components() == 1 else f"_{'XYZ'[component]}")
        s.ana_bar = _colorbar(name, UNITS.get(s.ana_field, ""), s.ana_cmap, lo, hi)

    def _components(self) -> int:
        return next((int(f["components"]) for f in self.state.ana_fields if f["name"] == self.state.ana_field), 1)

    def _component(self) -> int:
        if self._components() == 1 or self.state.ana_component == "magnitude":
            return -1
        return "xyz".index(self.state.ana_component)

    def _show_quality(self) -> None:
        s = self.state
        hist = self._hists.get(s.ana_metric)
        selected = s.ana_bin if s.ana_bin >= 0 else None
        self.quality.show(hist, selected, s.ana_worst_on)
        if hist is None or selected is None:
            s.ana_selection = ""
            return
        lo, hi = hist.bin_range(selected)
        cells = grouped(hist.counts[selected])
        drawn = "" if self.quality.data is not None else " (not drawn: cells not read)"
        s.ana_selection = f"{cells} cells with {hist.name} in [{number(lo)}, {number(hi)}]{drawn}"

    # Controls

    def set_tab(self, tab: str) -> None:
        self.state.ana_tab = tab
        if tab == "initial":
            self._want_initial()
        elif self.analysis is None:
            self.refresh()

    def toggle_option(self, name: str) -> None:
        key = f"ana_{name}"
        self.state[key] = not self.state[key]
        self.apply()

    def set_field(self, name: str) -> None:
        self.state.ana_field = name
        self.state.ana_range = ["", ""]
        self.apply()

    def set_component(self, component: str) -> None:
        self.state.ana_component = component
        self.state.ana_range = ["", ""]
        self.apply()

    def set_colormap(self, name: str) -> None:
        if name in COLORMAPS:
            self.state.ana_cmap = name
            self.apply()

    def set_range(self, lo: str, hi: str) -> None:
        self.state.ana_range = [lo, hi]
        self.apply()

    def auto_range(self) -> None:
        self.state.ana_range = ["", ""]
        self.apply()

    def select_bin(self, metric: str, k: int) -> None:
        s = self.state
        same = s.ana_metric == metric and s.ana_bin == k
        s.ana_metric, s.ana_bin = metric, -1 if same else int(k)
        self.apply()

    def select_metric(self, metric: str) -> None:
        self.state.ana_metric, self.state.ana_bin = metric, -1
        self.apply()

    def focus_point(self, x: list[float]) -> None:
        b = self.scene.bounds
        half = 0.04 * max(b[1] - b[0], b[3] - b[2], b[5] - b[4])
        z = x[2] if len(x) > 2 else 0.0
        self.scene.frame_bounds([x[0] - half, x[0] + half, x[1] - half, x[1] + half, z - half, z + half], 1.0)
        self.app._update_view()

    # Layout

    def tab_button(self) -> None:
        with html.Button(
            classes=("ana_open ? 'tab ana-toggle on' : 'tab ana-toggle'",),
            click=self.toggle,
            v_show="tab === 'view'",
            title="Boundary sanity, initial state and mesh quality, from mallard check",
        ):
            html.Span(v_html="icons.statistics", classes="ticon")
            html.Span("Analyze")

    def panel(self) -> None:
        with html.Div(classes="ana-colorbar", v_if="ana_open && ana_bar.css"):
            html.Div("{{ ana_bar.label }}", classes="cb-label")
            html.Div(classes="cb-bar", style=("{background: ana_bar.css}",))
            with html.Div(classes="cb-ticks"):
                html.Span("{{ t.text }}", v_for="t in ana_bar.ticks", key="t.pos", style=("{left: t.pos + '%'}",))
        with html.Div(classes="ana-panel", v_if="ana_open"):
            with html.Div(classes="ana-tabs", role="tablist"):
                for key, label in TABS:
                    html.Button(
                        label,
                        classes=(f"ana_tab === '{key}' ? 'tab on' : 'tab'",),
                        role="tab",
                        click=(self.set_tab, f"['{key}']"),
                    )
                html.Div(classes="grow")
                html.Button(
                    v_html="icons.refresh",
                    classes="icon-btn",
                    click=self.refresh,
                    title="Run mallard check again",
                    disabled=("ana_busy",),
                )
            with html.Div(classes="ana-body"):
                html.Div("{{ ana_doing || 'mallard check ...' }}", classes="ana-status muted", v_if="ana_busy")
                html.Div("{{ ana_error }}", classes="ana-status err", v_if="ana_error")
                html.Div("the input changed since: Refresh", classes="ana-status warn", v_if="ana_stale && !ana_busy")
                html.Div("{{ ana_note }}", classes="ana-status muted", v_if="ana_note")
                self._boundaries()
                self._initial()
                self._quality()

    def _boundaries(self) -> None:
        with html.Div(v_if="ana_tab === 'boundaries'"):
            html.Div("Unassigned faces", classes="ana-h")
            with html.Div(v_for="u in ana_unassigned", key="u.zone", classes="ana-row bad"):
                html.Span(v_html="icons.error", classes="sev")
                html.Span("{{ u.zone }}", classes="mono grow")
                html.Span("{{ u.faces }} faces without a condition", classes="muted")
            with html.Div(classes="ana-row ok", v_if="!ana_unassigned.length && !ana_busy"):
                html.Span(v_html="icons.ok", classes="sev")
                html.Span("every boundary face has a condition", classes="grow")
            with html.Div(classes="ana-toggles"):
                with html.Button(
                    classes=("ana_normals ? 'tb on' : 'tb'",),
                    click=(self.toggle_option, "['normals']"),
                    title="Face normals, pointing out of the domain",
                ):
                    html.Span("Normals")
                with html.Button(
                    classes=("ana_yplus ? 'tb on' : 'tb'",),
                    click=(self.toggle_option, "['yplus']"),
                    title="Walls colored by the first cell's y+",
                ):
                    html.Span("Wall y+")
            html.Div("Wall y+", classes="ana-h")
            with html.Div(v_if="ana_yp.max", classes="ana-kv"):
                html.Span("largest")
                html.Span("{{ ana_yp.max }}", classes=("['mono', ana_yp.status === 'warning' ? 'warn' : '']",))
                html.Span("mean")
                html.Span("{{ ana_yp.mean }}", classes="mono")
                html.Span("target")
                html.Span("{{ ana_yp.target || 'none' }}", classes="mono")
            html.Div("{{ ana_yp.basis }}", classes="ana-basis muted", v_if="ana_yp.basis")
            html.Div("not estimated (inviscid, or no flow at the initial state)", classes="muted", v_if="!ana_yp.max")
            html.Div("Periodic pairs", classes="ana-h")
            with html.Div(v_for="p in ana_periodic", key="p.label", classes="ana-card"):
                with html.Div(classes="ana-row"):
                    html.Span(v_html="icons[p.ok ? 'ok' : 'warning']", classes=("['sev', p.ok ? 'okc' : 'warn']",))
                    html.Span("{{ p.label }}", classes="mono grow")
                    html.Span("{{ p.faces }} faces", classes="muted")
                html.Div("{{ p.map }}", classes="ana-basis mono")
                html.Div("{{ p.detail }}", classes="ana-basis muted")
            html.Div("none", classes="muted", v_if="!ana_periodic.length")

    def _initial(self) -> None:
        with html.Div(v_if="ana_tab === 'initial'"):
            html.Div(
                "no initial state: the setup stopped (see Problems)",
                classes="muted",
                v_if="!ana_fields.length && !ana_busy",
            )
            with html.Div(v_if="ana_fields.length"):
                with html.Div(classes="prow"):
                    html.Div("field", classes="plabel")
                    with html.Div(classes="pval"):
                        with html.Select(aria_label="initial field", change=(self.set_field, "[$event.target.value]")):
                            html.Option(
                                "{{ f.name }}",
                                v_for="f in ana_fields",
                                key="f.name",
                                value=("f.name",),
                                selected=("f.name === ana_field",),
                            )
                with html.Div(classes="prow", v_if="(ana_fields.find(f => f.name === ana_field) || {}).components > 1"):
                    html.Div("component", classes="plabel")
                    with html.Div(classes="pval"):
                        with html.Select(aria_label="component", change=(self.set_component, "[$event.target.value]")):
                            html.Option(
                                "{{ c }}",
                                v_for=f"c in {list(COMPONENTS)}.slice(0, dim + 1)",
                                key="c",
                                value=("c",),
                                selected=("c === ana_component",),
                            )
                with html.Div(classes="prow"):
                    html.Div("colormap", classes="plabel")
                    with html.Div(classes="pval"):
                        dropdown(
                            "ana_cmap",
                            "ana_cmaps",
                            (self.set_colormap, "[$event.detail]"),
                            "colormap",
                            classes="cmap-dd",
                            swatch="(ana_cmaps.find(c => c.value === ana_cmap) || {}).swatch",
                        )
                with html.Div(classes="prow"):
                    html.Div("range", classes="plabel")
                    with html.Div(classes="pval vec range"):
                        html.Input(
                            value=("ana_range[0]",),
                            aria_label="range min",
                            change=(self.set_range, "[$event.target.value, ana_range[1]]"),
                            keydown_enter="$event.target.blur()",
                        )
                        html.Input(
                            value=("ana_range[1]",),
                            aria_label="range max",
                            change=(self.set_range, "[ana_range[0], $event.target.value]"),
                            keydown_enter="$event.target.blur()",
                        )
                        html.Button("Auto", classes="tb", click=self.auto_range, title="The field's range")
                html.Div(
                    "the state the run starts from, [initialize] with its regions; the section tool cuts it",
                    classes="ana-basis muted",
                )

    def _quality(self) -> None:
        with html.Div(v_if="ana_tab === 'quality'"):
            html.Div(
                "no histograms: the setup stopped, or this mallard predates them",
                classes="muted",
                v_if="!ana_hists.length && !ana_busy",
            )
            with html.Div(
                v_for="h in ana_hists",
                key="h.metric",
                classes=("['ana-card', 'hist', {sel: h.metric === ana_metric}]",),
            ):
                with html.Div(classes="ana-row", click=(self.select_metric, "[h.metric]")):
                    html.Span("{{ h.label }}", classes="grow")
                    html.Span(
                        "{{ h.above }} above {{ h.limit }}",
                        classes=("['mono', h.above ? 'warn' : 'muted']",),
                    )
                with html.Div(classes="bars", role="listbox", aria_label=("h.label",)):
                    html.Div(
                        v_for="(b, i) in h.bars",
                        key="i",
                        classes=("['bar', {over: b.over, on: h.metric === ana_metric && i === ana_bin, empty: !b.n}]",),
                        style=("{height: b.h + '%'}",),
                        title=("b.title",),
                        role="option",
                        click=(self.select_bin, "[h.metric, i]"),
                    )
                with html.Div(classes="bar-axis mono"):
                    html.Span("{{ h.lo }}")
                    html.Span("{{ h.hi }}")
            html.Div("{{ ana_selection }}", classes="ana-basis", v_if="ana_selection")
            with html.Div(classes="ana-toggles"):
                with html.Button(
                    classes=("ana_worst_on ? 'tb on' : 'tb'",),
                    click=(self.toggle_option, "['worst_on']"),
                    title="Highlight the worst cells of the metric",
                ):
                    html.Span("Worst cells")
            with html.Div(v_for="h in ana_hists.filter(h => h.metric === ana_metric)", key="h.metric"):
                with html.Div(
                    v_for="(w, i) in h.worst",
                    key="i",
                    classes="ana-row link",
                    click=(self.focus_point, "[w.x]"),
                    title="Frame this cell",
                ):
                    html.Span("{{ w.value }}", classes="mono")
                    html.Span("cell {{ w.cell }}", classes="muted grow")
                    html.Span("{{ w.at }}", classes="mono muted")
            html.Div("click a bin to select its cells; histograms over every cell, by Mallard", classes="muted")


def _colorbar(name: str, unit: str, colormap: str, lo: float, hi: float) -> dict[str, Any]:
    ticks = [t for t in nice_ticks(lo, hi, 5) if lo <= t <= hi] if hi > lo else [lo]
    span = (hi - lo) or 1.0
    return {
        "css": css_gradient(colormap),
        "label": axis_label(name, unit),
        "ticks": [{"pos": round(100 * (t - lo) / span, 2), "text": number(t)} for t in ticks],
    }


def _varies(rng: tuple[float, float] | None) -> bool:
    return rng is not None and rng[1] - rng[0] > 1e-12 * max(abs(rng[0]), abs(rng[1]), 1e-300)


def _periodic_row(p: PeriodicPair) -> dict[str, Any]:
    offset = (
        "node offsets not measured (a mesh read in parallel)"
        if p.node_offset_max is None
        else f"node offset {number(p.node_offset_max)} m ({number(p.node_offset_relative)} of the shortest edge)"
    )
    return {
        "label": p.label,
        "map": p.map_text,
        "faces": p.faces,
        "detail": f"area {number(p.area)} each side · {offset}",
        "ok": p.matches,
    }


def _histogram_row(h: Histogram) -> dict[str, Any]:
    top = max(h.counts) or 1
    bars = []
    for k, n in enumerate(h.counts):
        lo, hi = h.bin_range(k)
        bars.append(
            {
                "n": n,
                "h": 0 if n == 0 else max(4, round(100 * n / top)),
                "over": lo >= h.limit,
                "title": f"{n} cells in [{number(lo)}, {number(hi)}]",
            }
        )
    unit = "" if h.units == "dimensionless" else h.units
    return {
        "metric": h.metric,
        "label": axis_label(h.name, unit),
        "limit": number(h.limit),
        "above": h.above_limit,
        "lo": number(h.edges[0]),
        "hi": number(h.edges[-1]),
        "bars": bars,
        "worst": [
            {
                "value": number(w["value"]),
                "cell": w["cell"],
                "x": list(w["x"]),
                "at": "(" + ", ".join(number(c) for c in w["x"]) + ")",
            }
            for w in h.worst
        ],
    }
