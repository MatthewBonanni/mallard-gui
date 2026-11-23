"""The data manager page: a run's files with their state at each location, selections, and transfers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from ..backends.ssh import RemoteError
from ..data.locations import TransferError
from ..data.manager import SELECTIONS, DataManager
from ..data.manifest import FileEntry
from .empty import empty_state
from .fmt import bytes_text, duration, number
from .panes import list_splitter, list_style, splitter
from .relocate_panel import RelocatePanel

if TYPE_CHECKING:
    from .app import App

STATE_TEXT = {
    "home": "home",
    "present": "present",
    "prefix": "prefix",
    "partial": "partial",
    "absent": "absent",
    "differs": "differs",
    "": "no run root",
}


class DataPage:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self._entries: dict[str, FileEntry] = {}
        self._poll: asyncio.Task[None] | None = None
        s = self.state
        s.data_run = ""
        s.data_home = ""
        s.data_locs = []
        s.data_rows = []
        s.data_sel = []
        s.data_what = ""
        s.data_selections = [{"id": k, "label": v} for k, v in SELECTIONS.items()]
        s.data_tmin = ""
        s.data_tmax = ""
        s.data_dst = ""
        s.data_route = "auto"
        s.data_plan = {}
        s.data_xfer = {"running": False, "pct": 0, "text": "", "error": ""}
        s.data_errors = {}
        s.data_busy = False
        s.data_probing = ""
        s.data_reach = ""
        self.relocate = RelocatePanel(app)

    @property
    def dm(self) -> DataManager:
        return self.app.session.data

    # Actions

    def open(self) -> None:
        runs = self.state.runs
        if not self.state.data_run and runs:
            self.select_run(runs[0]["name"])

    def select_run(self, name: str) -> None:
        self.state.data_run = name
        self.state.data_sel = []
        self.state.data_plan = {}
        self.relocate.reset()
        self._schedule(self._refresh(name))

    def refresh(self) -> None:
        if self.state.data_run:
            self._schedule(self._refresh(self.state.data_run))

    def _schedule(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self.app._tasks.add(task)
        task.add_done_callback(self.app._tasks.discard)

    async def _refresh(self, name: str) -> None:
        with self.state:
            self.state.data_busy = True
        record = self.app.session.project.run(name)
        try:
            errors = await asyncio.to_thread(self.dm.refresh, record)
        except (KeyError, RemoteError, TransferError, OSError, ValueError) as e:
            errors = {"home": str(e)}
        with self.state:
            self.state.data_errors = errors
            self.state.data_busy = False
            self._show(name)

    def _show(self, name: str) -> None:
        s = self.state
        record = self.app.session.project.run(name)
        try:
            home = self.dm.home(record)
        except KeyError as e:
            s.data_home, s.data_rows, s.data_locs = str(e), [], []
            return
        s.data_home = self.short(home.label)
        names = self.dm.location_names()
        locs = []
        for n in names:
            root = self.dm.copy_root(record, n)
            kind = "local" if n == "local" else "server"
            short = self.short(f"{n}:{root}").partition(":")[2] if root else ""
            locs.append({"name": n, "icon": kind, "root": short, "home": n == home.location.name})
        s.data_locs = locs
        if not s.data_dst or s.data_dst == home.location.name:
            preferred = self.app.settings_page.settings.get("runs", "remote")
            s.data_dst = next(
                (x["name"] for x in sorted(locs, key=lambda x: x["name"] != preferred) if not x["home"] and x["root"]),
                "",
            )
        rows = self.dm.states(record, names)
        self._entries = {r["path"]: FileEntry.from_dict(r) for r in rows}
        s.data_rows = [
            {
                "path": r["path"],
                "kind": r["kind"],
                "category": r["category"],
                "size": bytes_text(r["size"]),
                "step": "" if r["step"] is None else number(r["step"]),
                "t": "" if r["t"] is None else number(r["t"]),
                "at": {
                    n: {
                        "state": a["state"],
                        "text": _state_text(a["state"], a["have"], r["size"]),
                    }
                    for n, a in r["at"].items()
                },
            }
            for r in rows
        ]

    def short(self, label: str) -> str:
        """`local:runs/x` for copies in the project, as the design shows them; remote paths stay whole."""
        name, _, path = label.partition(":")
        root = str(self.app.session.project.root)
        if name == "local" and (path == root or path.startswith(root + "/")):
            return f"local:{path[len(root) + 1 :] or '.'}"
        return label

    def choose(self, what: str) -> None:
        record = self.app.session.project.run(self.state.data_run)
        params: dict[str, Any] = {}
        if what == "snapshots":
            try:
                params = {"t_min": float(self.state.data_tmin or "-inf"), "t_max": float(self.state.data_tmax or "inf")}
            except ValueError:
                return
        self.state.data_what = what
        self.state.data_sel = [e.path for e in self.dm.select(record, what, **params)]
        self.plan()

    def toggle(self, path: str) -> None:
        sel = list(self.state.data_sel)
        sel.remove(path) if path in sel else sel.append(path)
        self.state.data_sel = sel
        self.state.data_what = ""
        self.plan()

    def set_dst(self, name: str) -> None:
        self.state.data_dst = name
        self.plan()

    def set_route(self, route: str) -> None:
        self.state.data_route = route
        self.plan()

    def plan(self) -> None:
        s = self.state
        if not s.data_sel or not s.data_dst:
            s.data_plan = {}
            return
        record = self.app.session.project.run(s.data_run)
        entries = [self._entries[p] for p in s.data_sel if p in self._entries]
        try:
            plan = self.dm.plan(record, entries, s.data_dst, s.data_route, probe=False)
        except (KeyError, RemoteError, TransferError, OSError, ValueError) as e:
            s.data_plan = {"error": str(e)}
            return
        seconds = plan.seconds
        s.data_plan = {
            "route": plan.route,
            "from": self.short(plan.items[0].source.label) if plan.items else "",
            "to": self.short(plan.destination.label),
            "count": len(plan.moving),
            "bytes": bytes_text(plan.bytes),
            "resumed": bytes_text(plan.resumed) if plan.resumed else "",
            "skipped": len(plan.skipped),
            "skipped_bytes": bytes_text(plan.bytes_skipped),
            "reasons": sorted({i.skip for i in plan.skipped}),
            "eta": duration(seconds)
            if seconds is not None
            else ("–" if not plan.bytes else "unknown: first transfer on this route"),
            "rate": f"{bytes_text(plan.throughput)}/s" if plan.throughput else "",
        }
        self._plan = plan
        self._show_reach(plan.items[0].source.location.name if plan.items else "", s.data_dst)

    def _show_reach(self, src: str, dst: str) -> None:
        s = self.state
        if not src or src == dst:
            s.data_reach = ""
            return
        if self.dm.needs_probe(src, dst):
            s.data_reach = "probing whether they reach each other ..."
            if s.data_probing != f"{src}>{dst}":
                s.data_probing = f"{src}>{dst}"
                self._schedule(self._probe(src, dst))
            return
        there, back = self.dm.reach(src, dst), self.dm.reach(dst, src)
        if not there and not back:
            s.data_reach = ""
            return
        parts = []
        if there.get("sees") or back.get("sees"):
            parts.append("same storage")
        if there.get("ssh"):
            parts.append(f"{src} reaches {dst}")
        if back.get("ssh"):
            parts.append(f"{dst} reaches {src}")
        s.data_reach = " · ".join(parts) or "no direct path: through local"

    async def _probe(self, src: str, dst: str) -> None:
        try:
            await asyncio.to_thread(self.dm.probe_routes, src, dst)
        except (KeyError, RemoteError, TransferError, OSError, ValueError) as e:
            with self.state:
                self.state.data_reach = f"probe failed: {str(e).splitlines()[0][:200]}"
                self.state.data_probing = ""
            return
        with self.state:
            self.state.data_probing = ""
            self.plan()

    def start(self) -> None:
        if not self.state.data_plan or self.state.data_plan.get("error") or self.dm.transfer.running:
            return
        self.dm.start(self._plan)
        self.state.data_xfer = {"running": True, "pct": 0, "text": "starting", "error": ""}
        self._poll = asyncio.ensure_future(self._follow())

    def cancel(self) -> None:
        self.dm.cancel()

    async def _follow(self) -> None:
        t = self.dm.transfer
        while t.running:
            with self.state:
                self.state.data_xfer = self._xfer(t)
            await asyncio.sleep(0.3)
        with self.state:
            self.state.data_xfer = self._xfer(t)
        await self._refresh(self.state.data_run)
        with self.state:
            self.plan()

    @staticmethod
    def _xfer(t: Any) -> dict[str, Any]:
        pct = round(100 * t.done / t.total, 1) if t.total else (100 if not t.running else 0)
        text = f"{bytes_text(t.done)} of {bytes_text(t.total)} · {t.current}"
        error = t.error
        if not t.running and t.report is not None:
            r = t.report
            state = "cancelled: partial kept, resumable" if r.cancelled else "done, verified by SHA-256"
            text = f"{len(r.moved)} moved · {bytes_text(r.bytes)} in {duration(r.seconds)} · {state}"
            error = "; ".join(r.errors)
        return {"running": t.running, "pct": pct, "text": text, "error": error}

    # Layout

    def layout(self) -> None:
        with html.Div(classes="panel"):
            splitter("data-left", "the run list", minimum=180, maximum=640)
            with html.Div(classes="panel-head"):
                html.Span("Runs")
            with html.Div(classes="scroll", style=list_style("data-list", "50%")):
                with html.Div(
                    v_for="r in runs",
                    key="r.name",
                    classes=("['run-row', {sel: r.name === data_run}]",),
                    click=(self.select_run, "[r.name]"),
                ):
                    html.Span(v_html="icons[r.remote === 'local' ? 'local' : 'server']", classes="ticon")
                    html.Span("{{ r.name }}")
                    html.Span("{{ r.remote }}", classes="rv")
                html.Div("No runs yet", classes="notice", v_if="!runs.length")
            list_splitter("data-list", "the run list")
            with html.Div(classes="panel-head"):
                html.Span("Locations")
                html.Span("copies of this run", classes="mono")
            with html.Div(classes="scroll"):
                with html.Div(v_for="l in data_locs", key="l.name", classes="loc-row"):
                    html.Span(v_html="icons[l.icon]", classes="ticon")
                    with html.Div(classes="loc-main"):
                        with html.Div():
                            html.Span("{{ l.name }}", classes="mono", style="font-weight: 600")
                            html.Span("home", classes="tag", v_if="l.home")
                        html.Div("{{ l.root || 'no run root: set one in Remotes' }}", classes="loc-root mono")
                        html.Div("{{ data_errors[l.name] }}", classes="perr-inline", v_if="data_errors[l.name]")
        with html.Div(classes="center"):
            empty_state(
                "data",
                "No runs yet",
                "Every file of a run at every location it is kept (this machine, clusters, Kubernetes volumes), "
                "and copies between them, checked by content.",
                "!data_run && !runs.length",
            )
            empty_state(
                "data",
                "No run selected",
                "Pick a run on the left to see its files at every location and copy them between locations.",
                "!data_run && runs.length",
            )
            with html.Div(classes="run-head", v_if="data_run"):
                html.Span(v_html="icons.data", classes="ticon")
                html.Span("{{ data_run || 'Data' }}", classes="name")
                html.Span("{{ data_home }}", classes="mono muted where", title=("data_home",))
                html.Div(classes="grow")
                with html.Button(
                    classes="btn", click=self.refresh, disabled=("data_busy || !data_run",), title="Rebuild manifests"
                ):
                    html.Span(v_html="icons.refresh")
                    html.Span("{{ data_busy ? 'Reading ...' : 'Refresh' }}")
            with html.Div(classes="toolbar", v_if="data_run"):
                html.Span("Select", classes="muted", style="margin-right: 4px")
                html.Button(
                    "{{ s.label }}",
                    v_for="s in data_selections",
                    key="s.id",
                    classes=("data_what === s.id ? 'tb on' : 'tb'",),
                    click=(self.choose, "[s.id]"),
                )
                html.Div(classes="tb-gap")
                html.Span("t", classes="mono muted")
                html.Input(v_model=("data_tmin",), classes="tb-input mono", placeholder="min", aria_label="t min")
                html.Span("–", classes="muted")
                html.Input(v_model=("data_tmax",), classes="tb-input mono", placeholder="max", aria_label="t max")
            with html.Div(classes="scroll", v_if="data_run"):
                with html.Table(classes="ftable"):
                    with html.Thead(), html.Tr():
                        html.Th("")
                        html.Th("file")
                        html.Th("kind")
                        html.Th("size", classes="num")
                        html.Th("step", classes="num")
                        html.Th("t", classes="num")
                        with html.Th(v_for="l in data_locs", key="l.name", classes="loc-col"):
                            html.Span(v_html="icons[l.icon]")
                            html.Span("{{ l.name }}")
                    with html.Tbody():
                        with html.Tr(
                            v_for="r in data_rows",
                            key="r.path",
                            classes=("{sel: data_sel.includes(r.path)}",),
                            click=(self.toggle, "[r.path]"),
                        ):
                            with html.Td():
                                html.Input(
                                    type="checkbox", checked=("data_sel.includes(r.path)",), aria_label=("r.path",)
                                )
                            html.Td("{{ r.path }}", classes="mono path")
                            html.Td("{{ r.kind }}", classes=("['kind', r.kind]",))
                            html.Td("{{ r.size }}", classes="num mono")
                            html.Td("{{ r.step }}", classes="num mono muted")
                            html.Td("{{ r.t }}", classes="num mono muted")
                            with html.Td(v_for="l in data_locs", key="l.name", classes=("['st', r.at[l.name].state]",)):
                                html.Span(v_html="icons[r.at[l.name].state || 'absent']", v_if="r.at[l.name].state")
                                html.Span("{{ r.at[l.name].text }}")
                html.Div("No files", classes="notice", v_if="!data_rows.length && !data_busy")
            with html.Div(classes="textbar mono", style="font-size: 11px", v_if="data_run"):
                html.Span("{{ data_rows.length }} files · {{ data_sel.length }} selected", classes="muted")
                html.Div(classes="grow")
                html.Span(
                    "objects by SHA-256 · streams by length and prefix SHA-256",
                    classes="muted",
                )
        with html.Div(classes="panel right"):
            splitter("data-right", "the transfer panel", edge="start", minimum=240, maximum=760)
            empty_state(
                "transfer",
                "Transfer",
                "What would move, what the destination already holds, and how long it takes, for the files you select.",
                "!data_run",
                compact=True,
            )
            with html.Div(classes="panel-head", v_show="data_run"):
                html.Span("Transfer")
                html.Span("{{ data_plan.route || '' }}", classes="mono")
            with html.Div(classes="scroll", v_show="data_run"):
                with html.Div(classes="prow"):
                    html.Div("to", classes="plabel")
                    with html.Div(classes="pval"), html.Select(change=(self.set_dst, "[$event.target.value]")):
                        html.Option(
                            "{{ l.name }}{{ l.root ? '' : ' (no run root)' }}",
                            v_for="l in data_locs.filter(l => !l.home)",
                            key="l.name",
                            value=("l.name",),
                            selected=("l.name === data_dst",),
                            disabled=("!l.root",),
                        )
                with html.Div(classes="prow"):
                    html.Div("route", classes="plabel")
                    with html.Div(classes="pval"), html.Select(change=(self.set_route, "[$event.target.value]")):
                        for value, label in (
                            ("auto", "auto (cheapest)"),
                            ("stream", "stream (local and a remote)"),
                            ("funnel", "through local (remote to remote)"),
                            ("direct", "direct: source pushes (remote to remote)"),
                            ("pull", "direct: destination pulls (remote to remote)"),
                            ("shared", "shared storage (copy in place)"),
                        ):
                            html.Option(label, value=value, selected=(f"data_route === '{value}'",))
                with html.Div(classes="prow", v_if="data_reach"):
                    html.Div("reach", classes="plabel")
                    html.Div("{{ data_reach }}", classes="pval ro", title=("data_reach",))
                with html.Div(v_if="data_plan.error", classes="prop-foot", style="color: var(--m-danger)"):
                    html.Span("{{ data_plan.error }}")
                with html.Div(v_if="data_plan.to"):
                    for label, expr in (
                        ("from", "data_plan.from"),
                        ("to", "data_plan.to"),
                        ("moving", "data_plan.count + ' files · ' + data_plan.bytes"),
                        ("resumed", "data_plan.resumed || '–'"),
                        ("skipped", "data_plan.skipped + ' · ' + data_plan.skipped_bytes"),
                        ("why", "data_plan.reasons.join(', ') || '–'"),
                        ("estimate", "data_plan.eta"),
                        ("measured", "data_plan.rate || '–'"),
                    ):
                        with html.Div(classes="prow"):
                            html.Div(label, classes="plabel")
                            html.Div(f"{{{{ {expr} }}}}", classes="pval ro", title=(expr,))
                html.Div(
                    "Select files, or a group above, to see what would move.",
                    classes="notice",
                    v_if="!data_plan.to && !data_plan.error",
                )
                with html.Div(v_if="data_xfer.text", style="padding: 8px"):
                    with html.Div(classes="progress", style="margin: 0 0 6px"):
                        html.Div(style=("{width: data_xfer.pct + '%'}",))
                    html.Div("{{ data_xfer.pct }} % · {{ data_xfer.text }}", classes="mono", style="font-size: 11px")
                    html.Div(
                        "{{ data_xfer.error }}",
                        classes="mono",
                        style="font-size: 11px; color: var(--m-danger)",
                        v_if="data_xfer.error",
                    )
            with html.Div(classes="actions", v_show="data_run"):
                html.Div(classes="grow")
                with html.Button(classes="btn", click=self.cancel, disabled=("!data_xfer.running",)):
                    html.Span(v_html="icons.stop")
                    html.Span("Cancel")
                with html.Button(
                    classes="btn primary",
                    click=self.start,
                    disabled=("data_xfer.running || !data_plan.count",),
                    title="Copies land as .partial files, renamed once their SHA-256 matches",
                ):
                    html.Span(v_html="icons.transfer")
                    html.Span("{{ data_plan.resumed ? 'Resume' : 'Transfer' }}")
            with html.Div(classes="relocate", v_if="data_run"):
                self.relocate.layout()


def _state_text(state: str, have: int, size: int) -> str:
    if state in ("prefix", "partial"):
        return f"{STATE_TEXT[state]} {round(100 * have / size) if size else 0} %"
    return STATE_TEXT.get(state, state)
