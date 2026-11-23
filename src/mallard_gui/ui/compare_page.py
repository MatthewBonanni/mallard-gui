"""Compare runs: two runs' resolved inputs side by side with the keys that differ, and their probe and force
series on one plot, with reference or experimental data from a CSV file."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trame.widgets import html

from ..compare import diff_inputs, reference_csv, resolved_input, sectioned, series_of, series_sources
from ..export import PlotSpec
from ..plots import Curve, axis_label, line_plot
from ..runlog import RunState
from .empty import empty_state
from .panes import splitter
from .paths import project_relative

if TYPE_CHECKING:
    from .app import App


class ComparePage:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self._runs: dict[str, RunState] = {}
        self._reference: list[Curve] = []
        s = self.state
        s.c_a = s.c_b = ""
        s.c_rows = []
        s.c_changed = 0
        s.c_total = 0
        s.c_only = True
        s.c_which = ""
        s.c_sources = []
        s.c_source = ""
        s.c_plot = ""
        s.c_ref_path = ""
        s.c_ref_text = ""

    def open(self) -> None:
        runs = [r["name"] for r in self.state.runs]
        if not self.state.c_a and runs:
            self.state.c_a = runs[1] if len(runs) > 1 else runs[0]
        if not self.state.c_b and runs:
            self.state.c_b = runs[0]
        self._runs.clear()
        self.refresh()

    def _state(self, name: str) -> RunState:
        if name not in self._runs:
            active = self.app.session.active.get(name)
            self._runs[name] = (
                active.follower.state if active else self.app.session.open_run(self.app.session.project.run(name))
            )
        return self._runs[name]

    def pick(self, which: str, name: str) -> None:
        if which == "a":
            self.state.c_a = name
        else:
            self.state.c_b = name
        self.refresh()

    def set_only(self, only: bool) -> None:
        self.state.c_only = bool(only)
        self.refresh()

    def set_source(self, source: str) -> None:
        self.state.c_source = source
        self._plot()

    def refresh(self) -> None:
        s = self.state
        if not s.c_a or not s.c_b:
            return
        a, b = self._state(s.c_a), self._state(s.c_b)
        (ia, wa), (ib, wb) = resolved_input(a), resolved_input(b)
        rows = diff_inputs(ia, ib)
        changed = [r for r in rows if r.status != "same"]
        s.c_total, s.c_changed = len(rows), len(changed)
        s.c_rows = sectioned(changed if s.c_only else rows)
        s.c_which = (
            "resolved inputs (defaults filled in)"
            if wa == wb == "resolved"
            else "inputs as written (no resolved copy in a run log)"
            if wa and wb
            else "a run without a start record: no input to compare"
        )
        sources = series_sources(a) | series_sources(b)
        s.c_sources = [{"id": k, "label": v} for k, v in sources.items()]
        if s.c_source not in sources:
            s.c_source = next(iter(sources), "")
        self._plot()

    def choose_reference(self) -> None:
        root = self.app.session.project.root
        self.app.paths.choose("reference", root, "", lambda p: self.load_reference(project_relative(root, p)))

    def load_reference(self, path: str) -> None:
        s = self.state
        s.c_ref_path = path
        if not path.strip():
            self._reference, s.c_ref_text = [], ""
            self._plot()
            return
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.app.session.project.root / p
        try:
            curves, x = reference_csv(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            self._reference, s.c_ref_text = [], f"{p.name}: {e.strerror if isinstance(e, OSError) else e}"
            self._plot()
            return
        self._reference = [Curve(f"{p.stem} · {c.label}", c.x, c.y) for c in curves]
        s.c_ref_text = (
            f"{len(curves)} column{'s' if len(curves) != 1 else ''} against {x}, {sum(len(c.x) for c in curves)} points"
            if curves
            else f"{p.name}: no numeric columns"
        )
        self._plot()

    def _plot(self) -> None:
        s = self.state
        if not s.c_source:
            s.c_plot = ""
            self.app.exports.remember("compare", None)
            return
        curves, styles, units = [], [], set()
        for name in (s.c_a, s.c_b):
            t, v, unit = series_of(self._state(name), s.c_source)
            if t:
                curves.append(Curve(name, t, v))
                styles.append("")
                units.add(unit)
        for c in self._reference:
            curves.append(c)
            styles.append("points")
        label = next((x["label"] for x in s.c_sources if x["id"] == s.c_source), s.c_source)
        unit = units.pop() if len(units) == 1 else ""
        xlabel, ylabel = axis_label("t", ""), axis_label(label.split(" · ")[-1], unit)
        s.c_plot = line_plot(curves, xlabel, ylabel, width=360, height=220, markers=[st == "points" for st in styles])
        self.app.exports.remember("compare", PlotSpec(curves, xlabel, ylabel, label, styles))

    # Layout

    def layout(self) -> None:
        with html.Div(classes="panel"):
            splitter("compare-left", "the compare panel", minimum=180, maximum=640)
            with html.Div(classes="panel-head"):
                html.Span("Compare")
                html.Span("two runs", classes="mono")
            with html.Div(classes="scroll"):
                for which, title in (("a", "run A"), ("b", "run B")):
                    with html.Div(classes="prow"):
                        html.Div(title, classes="plabel")
                        with html.Div(classes="pval"):
                            with html.Select(aria_label=title, change=(self.pick, f"['{which}', $event.target.value]")):
                                html.Option(
                                    "{{ r.name }}",
                                    v_for="r in runs",
                                    key="r.name",
                                    value=("r.name",),
                                    selected=(f"r.name === c_{which}",),
                                )
                html.Div(
                    "Inputs as each run's start record gives them, resolved by Mallard: a default that changed "
                    "between builds shows too.",
                    classes="notice",
                )
        with html.Div(classes="center"):
            empty_state(
                "compare",
                "Compare needs two runs",
                "Two runs' inputs side by side as Mallard resolved them, and one probe or force column of both "
                "overlaid with reference data. This case has fewer than two runs.",
                "runs.length < 2",
            )
            empty_state(
                "compare",
                "Pick two runs",
                "Choose run A and run B on the left.",
                "runs.length >= 2 && !(c_a && c_b)",
            )
            with html.Div(classes="cmp-bar", v_if="c_a && c_b"):
                html.Span("{{ c_a }}", classes="tag")
                html.Span("vs", classes="muted")
                html.Span("{{ c_b }}", classes="tag")
                html.Span("{{ c_changed }} of {{ c_total }} keys differ · {{ c_which }}", classes="muted")
                html.Div(classes="grow")
                with html.Label(classes="chip"):
                    html.Input(
                        type="checkbox",
                        checked=("c_only",),
                        change=(self.set_only, "[$event.target.checked]"),
                        aria_label="changed only",
                        style="width: auto; height: auto; margin: 0",
                    )
                    html.Span("Changed only")
            with html.Div(classes="cmp-diff", v_if="c_a && c_b"):
                with html.Table(classes="cmp-table"):
                    with html.Tr():
                        html.Th("key", style="width: 34%")
                        html.Th("{{ c_a }}")
                        html.Th("{{ c_b }}")
                    with html.Tr(v_for="r in c_rows", key="r.key", classes=("r.section ? 'sec' : r.status",)):
                        with html.Td(v_if="r.section", colspan="3"):
                            html.Span("{{ r.section }}")
                            html.Span(
                                "{{ r.changed === r.total ? r.total : r.changed + ' of ' + r.total }} differ",
                                classes="muted",
                                v_if="r.changed",
                            )
                        with html.Template(v_else=""):
                            html.Td("{{ r.leaf }}", classes="k", title=("r.key",))
                            html.Td("{{ r.a || '–' }}", classes="v a", title=("r.a",))
                            html.Td("{{ r.b || '–' }}", classes="v b", title=("r.b",))
                html.Div("The inputs are the same.", classes="notice", v_if="c_total && !c_changed && c_only")
        with html.Div(classes="panel right"):
            splitter("compare-right", "the overlay panel", edge="start", minimum=240, maximum=760)
            empty_state(
                "analysis",
                "Overlay",
                "One probe, force or reduction column of both runs, with reference data as points.",
                "!(c_a && c_b)",
                compact=True,
            )
            with html.Div(classes="panel-head", v_show="c_a && c_b"):
                html.Span("Overlay")
                self.app.exports.buttons("compare")
            with html.Div(classes="scroll", v_show="c_a && c_b"):
                with html.Div(classes="prow"):
                    html.Div("series", classes="plabel")
                    with html.Div(classes="pval"):
                        with html.Select(aria_label="series", change=(self.set_source, "[$event.target.value]")):
                            html.Option(
                                "{{ x.label }}",
                                v_for="x in c_sources",
                                key="x.id",
                                value=("x.id",),
                                selected=("x.id === c_source",),
                            )
                html.Div(v_html="c_plot", classes="plot", v_if="c_plot", style="border-right: none")
                html.Div("No probe or force series in these runs.", classes="notice", v_if="!c_sources.length")
                with html.Div(classes="group-head"):
                    html.Span("REFERENCE DATA")
                    html.Span("CSV", classes="note")
                with html.Div(classes="prow"):
                    html.Div("file", classes="plabel")
                    with html.Div(classes="pval"):
                        html.Input(
                            value=("c_ref_path",),
                            aria_label="reference file",
                            placeholder="data/experiment.csv",
                            change=(self.load_reference, "[$event.target.value]"),
                            keydown_enter="$event.target.blur()",
                        )
                        html.Button(
                            "Choose…", classes="pickbtn", title="Choose a CSV file", click=self.choose_reference
                        )
                with html.Div(classes="prow", v_if="c_ref_text"):
                    html.Div("", classes="plabel")
                    html.Div("{{ c_ref_text }}", classes="pval ro muted", style="font-family: var(--m-font-sans)")
                html.Div(
                    "First column x (the same quantity as the plot's), every other column a curve; a header row "
                    "names them, # lines are comments. Shown as points.",
                    classes="notice",
                )
