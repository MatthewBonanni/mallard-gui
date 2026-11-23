"""Case setup beyond single values, in the app: previews of regions and probes with drag handles, points picked
on the view into keys, inflow arrows, quantities with a unit selector, the actions of the property panel, the
structured check report, the launch dialog with its cost estimate, the command palette and live control of
running runs. The app calls it at a few points (refresh, select, check, click) and places its layout pieces."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import client, html

from ..checkinfo import GPU_TARGETS, suggestions, summarize, target_label, with_throughput
from ..live import LiveControl, current_value, set_replies, settable_keys, setting_of
from ..mallard import CheckReport
from ..palette import Item, items, search
from ..setup import regions as rg
from ..setup.editor import canonical
from ..setup.geometry import Handle, Preview, Vec
from ..setup.inflow import Faces, faces_of, legend
from ..sizing import Point, charts, headline, point_of, rank_counts
from ..throughput import TIME_CAP, WARMUP_INTERVALS, Cancelled, Summary, rate
from ..units import UnitError
from ..view.live_cells import LiveCells
from ..view.overlay import SetupOverlay, speed_gradient
from ..view.scene import label_of
from .copy import COPY_PROPS, DOCS_BASE, copy_button, message
from .disclosure import disclosure
from .document_panel import COMMANDS, PAGES
from .dropdown import dropdown
from .empty import empty_state
from .fmt import number
from .paths import project_relative

if TYPE_CHECKING:
    from .app import App

# The box framed around a point of the report shown on the view, as a fraction of the domain's span
LOCATE_SPAN = 0.08
# A click right after a handle is let go is the end of the drag, not a pick.
CLICK_AFTER_DRAG = 0.4


CHART_KEYS = ("su_chart_time", "su_chart_mem", "su_chart_halo")


class SetupTools:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self.session = app.session
        from ..setup.editor import SetupEditor

        self.editor = SetupEditor(app.session)
        self.overlay = SetupOverlay(app.scene)
        self.overlay.on_drag = self._dragged
        self.overlay.on_release = self._released
        self.live_cells = LiveCells(app.scene)
        self._live_later: asyncio.TimerHandle | None = None
        self.caps = app.session.mallard.capabilities()
        self.live = LiveControl(app.session.mallard, self.caps)
        self._faces: dict[str, Faces] = {}
        self._shape: str | None = None
        self._located: list[tuple[str, Vec]] = []
        s = self.state
        s.su_actions = []
        s.su_raw_open = False
        s.su_pick = ""
        s.su_inflow_on = False
        s.su_inflow_zones = 0
        s.su_inflow = None
        s.su_drag = ""
        s.su_summary = {"sections": [], "run_label": "", "cost_known": False, "fit": {"text": "", "status": ""}}
        s.su_suggestions = {}
        s.su_raw = ""
        s.su_located = ""
        s.su_targets = size_targets(self.caps.gpus, self.caps.devices)
        s.su_devices = device_text(self.caps.devices)
        s.su_launch = {
            "open": False,
            "ranks": 1,
            "memory": "",
            "cap": int(TIME_CAP),
            "throughput": "",
            "tp": {},
            "busy": "",
            "error": "",
            "threads": app.session.backend.threads,
        }
        s.su_headline = []
        s.su_sweep = ""
        for name in CHART_KEYS:
            s[name] = {}
        self._points: dict[tuple[str, int], Point] = {}
        self._sweep: asyncio.Future[None] | None = None
        app.server.trigger("su_pick_ranks")(self.pick_ranks)
        app.server.enable_module({"styles": ["mallard_gui/launch.css"]})
        self._cancel_measure: threading.Event | None = None
        s.palette_i = 0
        s.su_live = {"keys": [], "channel": "", "reply": "", "ok": True, "replies": []}

    # The view: previews, handles, markers, inflow arrows

    def refresh(self) -> None:
        """After the case changed or a node was selected: the panel's actions and everything drawn."""
        selected = self.state.selected or ""
        self.state.su_actions = self.editor.actions(selected)
        self.draw()

    def draw(self) -> None:
        scene = self.app.scene
        if not scene.blocks:
            return
        selected = self.state.selected or ""
        shape = self.editor.shape_of(selected)
        self._shape = shape.node if shape else None
        self.overlay.set_previews(self.editor.previews(scene.bounds), self._shape)
        zone = selected.removeprefix("zone:") if selected.startswith("zone:") else ""
        self.overlay.set_markers(self.editor.markers(zone) if zone else [])
        self.overlay.located = self._located
        self._draw_inflow()
        self.app._update_view()

    def _draw_inflow(self) -> None:
        """With the toolbar's Inflow arrows on: arrows on every inflow boundary, colored by speed (one color when
        every speed is the same) and with a speed scale only when the speeds differ."""
        inflow = self.editor.inflow_zones()
        self.state.su_inflow_zones = len(inflow)
        zones = inflow if self.state.su_inflow_on else []
        for z in zones:
            if z not in self._faces and z in self.app.scene.blocks:
                poly = self.app.scene.blocks[z].actor.GetMapper().GetInput()
                self._faces[z] = faces_of(poly)
        flows = self.editor.inflows(zones, self._faces, None)
        lg = legend(flows, "")
        self.overlay.set_inflow(flows)
        if not lg["varies"]:
            self.state.su_inflow = None
            return
        units = {v.units for z, v in self.editor.values().items() if z in zones and v.velocity is not None}
        unit = "" if self.session.nondimensional else next(iter(units)) if len(units) == 1 else "m/s"
        self.state.su_inflow = {
            "max": number(lg["max"]),
            "unit": unit,
            "estimate": lg["estimate"],
            "sources": "; ".join(lg["sources"]),
            "gradient": speed_gradient(self.app.scene.theme),
        }

    def toggle_inflow(self) -> None:
        self.state.su_inflow_on = not self.state.su_inflow_on
        self._draw_inflow()
        self.app._update_view()

    def after_check(self, report: CheckReport, view_loaded: bool) -> None:
        s = self.state
        self._summarize(report.data)
        if s.su_launch.get("open"):
            self._sizing()
        s.su_suggestions = suggestions(report.data)
        self._located = []
        s.su_located = ""
        if view_loaded:
            self._faces = {}
        self.refresh()

    def _linked(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Keep the links of the report's rows that lead somewhere in this case: keys the catalog knows, nodes in
        the case tree (a region by the node its input entry has)."""
        ses = self.session
        nodes = {n["id"] for n in ses.tree()}
        for sec in summary["sections"]:
            for r in sec["rows"]:
                if r["key"] and not ses.knows(r["key"]):
                    r["key"] = ""
                node = r["node"]
                if node.startswith("region:"):
                    name = node.removeprefix("region:")
                    ids = (f"region:{name}", f"region:regions.{name}", f"region:{canonical(ses.doc, name)}")
                    node = next((i for i in ids if i in nodes), "")
                    r["k"] = label_of(name)
                r["node"] = node if node in nodes else ""
        return summary

    # The report's links: to the keys and nodes it is about, to places on the view, to the docs

    def report_goto(self, key: str, node: str) -> None:
        if node:
            self.app.select(node)
        elif key:
            self.palette_pick(f"key:{key}")

    def report_locate(self, label: str, x: list[float]) -> None:
        """Mark a point the report names (a worst cell, the cell limiting dt) on the case view and frame it."""
        p = [float(c) for c in x] + [0.0] * (3 - len(x))
        self._located = [(label, (p[0], p[1], p[2]))]
        self.state.su_located = label
        scene = self.app.scene
        if scene.blocks:
            b = scene.bounds
            half = LOCATE_SPAN * max(b[1] - b[0], b[3] - b[2], b[5] - b[4]) / 2 or 1.0
            scene.frame_bounds([c + sign * half for c in p for sign in (-1, 1)], margin=1.0)
        self.state.tab = "view"
        self.draw()

    def clear_located(self) -> None:
        self._located = []
        self.state.su_located = ""
        self.draw()

    def open_docs(self, path: str) -> None:
        if path:
            self.app.document._shell("openUrl", [DOCS_BASE + path])

    def _dragged(self, handle: Handle, position: Vec) -> None:
        if self._shape is None:
            return
        preview = self.editor.drag(self._shape, handle, position, self.app.scene.bounds)
        live = ""
        if preview is not None:
            self.overlay.update_preview(self._shape, preview)
            block = self.editor.block_of(self._shape)
            if block in self.app.scene.blocks:
                self._cancel_live()
                wait = self.live_cells.update(block, preview)
                if wait > 0:  # a slow mesh: the latest position once it is due, if no move comes first
                    loop = asyncio.get_event_loop()
                    self._live_later = loop.call_later(wait, self._live_due, handle, block, preview)
                live = self._live_text(block)
        self._drag_text(handle, live)

    def _live_text(self, block: str) -> str:
        cells = self.app.scene.blocks[block]
        return f" · {cells.cells} cells" if cells.visible else ""

    def _drag_text(self, handle: Handle, live: str) -> None:
        if self._shape is None:
            return
        value = self.editor.working(self._shape).get(handle.key)
        text = f"[{', '.join(number(c) for c in value)}]" if isinstance(value, list) else number(value)
        with self.state:
            self.state.su_drag = f"{handle.key} = {text}{live}"

    def _live_due(self, handle: Handle, block: str, preview: Preview) -> None:
        self._live_later = None
        if block in self.app.scene.blocks:
            self.live_cells.update(block, preview, force=True)
            self._drag_text(handle, self._live_text(block))
            self.app._update_view()

    def _cancel_live(self) -> None:
        if self._live_later is not None:
            self._live_later.cancel()
            self._live_later = None

    def _released(self, handle: Handle, position: Vec) -> None:
        if self._shape is None:
            return
        self._cancel_live()
        self.editor.release(self._shape, handle, position)
        block = self.editor.block_of(self._shape)
        if block in self.app.scene.blocks:  # where it was let go, until the check's own cells replace them
            shown = next((p for pid, _, p in self.editor.previews(self.app.scene.bounds) if pid == self._shape), None)
            if shown is not None:
                self.live_cells.update(block, shown, force=True)
        with self.state:
            self.state.su_drag = ""
            self.app._refresh_case()
            self.app._schedule_check()

    # The mesh file: the native dialog

    def choose_mesh(self) -> None:
        root = self.app.session.project.root
        current = self.app.session.doc.get("mesh.filename")
        start = (root / str(current)).parent if current else root
        self.app.paths.choose("mesh", start if start.is_dir() else root, "", self.set_mesh)

    def set_mesh(self, path: Path) -> None:
        self.app.set_key("mesh.filename", project_relative(self.app.session.project.root, path))

    # Picking points into keys

    def start_pick(self, path: str) -> None:
        self.state.su_pick = "" if self.state.su_pick == path else path
        self.state.pick = True

    def on_click(self, hit: dict[str, Any] | None, plane: Vec | None = None) -> bool:
        """Whether case setup used the click: the end of a drag, or a point picked into a key (the picked
        zone's point, else in 2D the point of the plane under the pointer)."""
        if time.monotonic() - self.overlay.last_drag < CLICK_AFTER_DRAG:
            return True
        path = self.state.su_pick
        if not path:
            return False
        point = hit.get("point") if hit else None
        point = point or plane
        if not point:
            return True
        self.editor.pick(path, point)
        self.state.su_pick = ""
        self.app._refresh_case()
        self.app._schedule_check()
        return True

    # The property panel

    def act(self, action: str, value: str = "") -> None:
        try:
            new = self.editor.act(self.state.selected, action, value, self.app.scene.bounds)
        except (ValueError, KeyError) as e:
            self.state.props = {**self.state.props, "edit_error": str(e)}
            return
        if new:
            self.state.selected = new
        self.app._refresh_case()
        self.app.select(self.state.selected)
        self.app._schedule_check()

    def set_unit(self, path: str, number_text: str, old: str, new: str) -> None:
        """The unit a quantity's field shows: the input keeps its SI number. A value written with a unit (an older
        input's `1 cm`) is written in SI as it changes."""
        ses = self.session
        try:
            if isinstance(ses.doc.get(path), str) and number_text and old:
                ses.set_value(path, f"{number_text} {old}", old)
                ses.show_unit(path, new)
                self.app._refresh_case()
                self.app._schedule_check()
                return
            ses.show_unit(path, new)
        except (UnitError, ValueError, KeyError) as e:
            self.state.props = {**self.state.props, "edit_error": str(e)}
            return
        self.state.props = self.app.inspector.props(self.state.selected)

    def rename_key(self, old: str, new: str) -> None:
        """The check's suggestion for an unknown key: the value moves to the key it names."""
        doc = self.session.doc
        value = doc.get(old)
        doc.delete(old)
        doc.set(new, value)
        doc.commit()
        self.app._refresh_case()
        self.app._schedule_check()

    # The launch dialog

    def _summarize(self, data: Mapping[str, Any]) -> None:
        note = self.session.launch.throughput_note
        self.state.su_summary = self._linked(summarize(data, self.session.nondimensional, note).to_dict())

    def _throughput_state(self) -> dict[str, Any]:
        tp = self.session.throughput
        return {
            "throughput": str(round(tp.value)) if tp else "",  # typed back: never grouped
            "tp": tp.to_dict() if tp else {},
        }

    def _recost(self) -> None:
        """The cost at the throughput just chosen, at once, by check's formula; the check that follows confirms."""
        report = self.session.report
        if report is not None and self.session.launch.throughput > 0:
            self._summarize(with_throughput(report.data, self.session.launch.throughput))
        self._sizing()

    # Sizing: the headline figures and the plots over rank counts

    def _sizing(self) -> None:
        """The headline from the last check at the chosen ranks and throughput, and the plots from the rank counts
        checked so far; the counts not checked yet are checked in the background."""
        report = self.session.report
        if report is None:
            return
        s, launch, tp = self.state, self.session.launch, self.session.throughput
        s.su_headline = headline(report.data, launch.ranks, launch.throughput, tp.kind if tp else "no throughput")
        cells = int((report.data.get("cost") or {}).get("cells") or 0)
        fewest = int((report.data.get("sizing") or {}).get("min_ranks") or 0)
        counts = rank_counts(cells, launch.ranks, fewest) if cells else []
        try:
            text = self.session.checked_path.read_bytes()
        except OSError:
            text = b""
        key = hashlib.sha256(text).hexdigest()
        points = [self._points[(key, n)] for n in counts if (key, n) in self._points]
        for name, cfg in charts(report.data, points, launch.ranks, launch.throughput).items():
            s[name] = cfg
        missing = [n for n in counts if (key, n) not in self._points]
        if missing and s.su_launch.get("open") and (self._sweep is None or self._sweep.done()):
            self._sweep = asyncio.ensure_future(self._check_counts(key, missing))

    async def _check_counts(self, key: str, counts: list[int]) -> None:
        """`mallard check --ranks N` for each count: Mallard's partition and peak memory per rank on N ranks."""
        path = self.session.checked_path
        for i, n in enumerate(counts):
            if not self.state.su_launch.get("open"):
                break
            with self.state:
                self.state.su_sweep = f"sizing: checking {n} rank{'s' if n > 1 else ''} ({i + 1} of {len(counts)})"
            report = await asyncio.to_thread(self.session.mallard.check, path, None, ["--ranks", str(n)])
            p = point_of(report.data)
            if p is not None:
                self._points[(key, n)] = p
        with self.state:
            self.state.su_sweep = ""
            self._sizing()

    def pick_ranks(self, _chart: str, ranks: float) -> None:
        """A click on a sizing plot: its rank count becomes the run's."""
        self.set_launch("ranks", str(round(ranks)))

    def open_launch(self) -> None:
        launch = self.session.launch
        if self.session.report is not None and self.session.throughput is None:
            self.session.preload_throughput()
            self._recost()
            self.app._schedule_check()
        self.state.su_launch = {
            **self.state.su_launch,
            **self._throughput_state(),
            "open": True,
            "ranks": launch.ranks,
            "memory": launch.memory,
            "error": "",
        }
        self._sizing()

    def close_launch(self) -> None:
        self.cancel_measure()
        self.state.su_launch = {**self.state.su_launch, "open": False}

    def set_launch(self, key: str, value: Any) -> None:
        launch = self.session.launch
        extra: dict[str, Any] = {}
        try:
            if key == "ranks":
                launch.ranks = max(1, int(value))
            elif key == "memory":
                launch.memory = str(value)
            elif key == "cap":
                value = max(5, int(float(value)))
            elif key == "throughput":
                self.session.give_throughput(float(value) if str(value).strip() else 0.0)
                self._recost()
                extra = self._throughput_state()
        except ValueError:
            self.state.su_launch = {**self.state.su_launch, "error": f"{key}: a number"}
            return
        self.state.su_launch = {**self.state.su_launch, key: value, **extra, "error": ""}
        if key == "ranks":
            self._sizing()
        if key != "cap":
            self.app._schedule_check()

    async def measure(self) -> None:
        """Times steady stepping until the result is tight enough or the cap; cancellable, with its progress."""
        s = self.state
        if self._cancel_measure is not None:
            return
        target = str(s.su_launch.get("memory") or "")
        if not target_here(target, self.caps.devices):
            s.su_launch = {
                **s.su_launch,
                "error": f"No {target_label(target)} available: set up a remote with one to measure",
            }
            return
        cancel = self._cancel_measure = threading.Event()
        loop = asyncio.get_running_loop()
        cap = float(s.su_launch.get("cap") or TIME_CAP)

        def progress(summary: Summary | None, n: int) -> None:
            if summary is None:
                text = f"warming up: {n} of {WARMUP_INTERVALS + 1} intervals"
            else:
                text = (
                    f"measuring: {rate(summary.median)} ± {rate(summary.plus_minus)} cells/s/rank, "
                    f"{number(summary.steps)} steps, ± {100 * summary.rse * 1.96:.1f} %"
                )
            loop.call_soon_threadsafe(self._measure_busy, text)

        self._measure_busy("starting the case")
        try:
            await asyncio.to_thread(self.session.measure_throughput, progress, cancel, cap)
        except Cancelled:
            with s:
                s.su_launch = {**s.su_launch, "busy": ""}
            return
        except (RuntimeError, OSError) as e:
            with s:
                s.su_launch = {**s.su_launch, "busy": "", "error": f"measure: {e}"}
            return
        finally:
            self._cancel_measure = None
        with s:
            self._recost()
            s.su_launch = {**s.su_launch, **self._throughput_state(), "busy": ""}
        self.app._schedule_check()

    def _measure_busy(self, text: str) -> None:
        if self._cancel_measure is None:
            return
        with self.state:
            self.state.su_launch = {**self.state.su_launch, "busy": text}

    def cancel_measure(self) -> None:
        if self._cancel_measure is not None:
            self._cancel_measure.set()

    def start(self) -> None:
        self.close_launch()
        self.app.start_run("", ranks=self.session.launch.ranks)

    # The command palette

    def palette_items(self) -> list[Item]:
        keys = {**self.session.general, **self.session.catalog}
        acts = [
            ("check", "Check the case", "mallard check"),
            ("launch", "Run...", "the launch dialog: machine, cost, start"),
            ("theme", "Toggle the theme", "light or dark"),
            ("inflow", "Inflow arrows", "on or off: velocity arrows on every inflow boundary"),
            ("fit", "Fit the view", "F"),
        ]
        acts += [(f"doc:{c}", label, detail) for c, label, detail in COMMANDS]
        recents = self.state.doc_recent
        acts += [(f"doc:open_recent:{r['path']}", f"Open Recent: {r['label']}", r["path"]) for r in recents]
        acts += [(f"page:{p}", f"Go to {label}", f"page  ⌘{i}") for i, (p, label) in enumerate(PAGES, 1)]
        types = rg.REGION_TYPES_2D if self.editor.dim == 2 else rg.REGION_TYPES_3D
        acts += [(f"add_region:{t}", f"Add region: {t}", "[regions]") for t in types]
        probes = rg.PROBE_TYPES_2D if self.editor.dim == 2 else rg.PROBE_TYPES_3D
        acts += [(f"add_probe:{t}", f"Add probe: {t}", "[[probes]]") for t in probes]
        return items(self.state.tree, [(k, v.set, v.type or "") for k, v in keys.items()], acts)

    def palette_search(self, query: str) -> list[dict[str, str]]:
        self.state.palette_i = 0
        if not query.strip():
            return []
        return [i.to_dict() for i in search(query, self.palette_items(), limit=20)]

    def palette_pick(self, ident: str) -> None:
        s, app = self.state, self.app
        s.search, s.search_results = "", []
        kind, _, rest = (ident or "").partition(":")
        if kind == "node":
            app.set_page("case")
            app.select(rest)
        elif kind == "key":
            app.set_page("case")
            if self.session.doc.has(rest):
                app._goto_path(rest)
            else:
                app.search_pick(rest)
        elif kind == "action":
            self._palette_action(rest)

    def _palette_action(self, action: str) -> None:
        app = self.app
        name, _, value = action.partition(":")
        if name == "check":
            app._schedule_check()
        elif name == "launch":
            app.set_page("case")
            self.open_launch()
        elif name == "theme":
            app.set_theme("light" if self.state.theme == "dark" else "dark")
        elif name == "inflow":
            app.set_page("case")
            self.toggle_inflow()
        elif name == "fit":
            app.fit()
        elif name == "page":
            app.set_page(value)
        elif name == "doc":
            command, _, arg = value.partition(":")
            app.document.command(command, arg or None)
        elif name in ("add_region", "add_probe"):
            app.set_page("case")
            self.state.selected = "section:regions" if name == "add_region" else "section:probes"
            self.act(name, value)

    # Live control of a running run

    def refresh_live(self, name: str) -> None:
        s = self.state
        record = self.session.project.run(name)
        run_dir = self.session.project.root / record.home.get("directory", f"runs/{name}")
        active = self.session.active.get(name)
        if active is None or not active.state.active or record.home.get("remote", "local") != "local":
            s.su_live = {"keys": [], "channel": "", "reply": "", "ok": True, "replies": [], "active": False}
            return
        data = _read_toml(run_dir / "input.toml")
        keys = settable_keys(self.caps.settable, data)
        st = active.follower.state
        s.su_live = {
            **s.su_live,
            "active": True,
            "keys": [self._live_key(k, data) for k in keys],
            "channel": self.live.channel(run_dir) or "none",
            "replies": set_replies(st.events, st.warnings)[-12:],
        }

    def _live_key(self, key: str, data: dict[str, Any]) -> dict[str, Any]:
        info = setting_of(self.caps, key)
        units = info.get("units") or ""
        return {
            "key": key,
            "value": _text(current_value(data, key)),
            "units": "" if self.session.nondimensional else units,
            "help": str(info.get("help") or ""),
            "placeholder": "steps" if info.get("type") == "count" else (f"in {units}, or with a unit" if units else ""),
        }

    def apply_live(self, key: str, value: str) -> None:
        name = self.state.run_sel
        record = self.session.project.run(name)
        run_dir = self.session.project.root / record.home.get("directory", f"runs/{name}")
        allowed = [k["key"] for k in self.state.su_live.get("keys", [])]
        reply = self.live.apply(run_dir, {key: value.strip()}, allowed)
        self.state.su_live = {
            **self.state.su_live,
            "reply": f"{key} = {value} · {reply.channel or 'not sent'}: {reply.text}",
            "ok": reply.ok,
        }

    # Layout pieces

    def row_widgets(self) -> None:
        """A quantity's number and unit, a picked point's button; inside a property row's value cell."""
        html.Input(
            v_if="!r.readonly && r.widget === 'quantity'",
            value=("r.number",),
            placeholder=("r.placeholder",),
            title=("(r.number || r.placeholder) + ' ' + r.unit_choice",),
            aria_label=("r.path",),
            spellcheck="false",
            classes="qnum",
            change=(self.app.set_key, "[r.path, $event.target.value, r.unit_choice]"),
            keydown_enter="$event.target.blur()",
        )
        dropdown(
            "r.unit_choice",
            "r.unit_options",
            (self.set_unit, "[r.path, r.number, r.unit_choice, $event.detail]"),
            ("r.path + ' unit'",),
            classes="qunit",
            v_if="!r.readonly && r.widget === 'quantity' && r.unit_options.length",
        )
        html.Button(
            "Pick",
            v_if="!r.readonly && r.widget === 'pick'",
            classes=("['pickbtn', {on: su_pick === r.path}]",),
            title="Click a point on the view",
            click=(self.start_pick, "[r.path]"),
        )

    def actions_bar(self) -> None:
        with html.Div(classes="su-actions", v_if="su_actions.length"):
            with html.Template(v_for="a in su_actions", key="a.id"):
                html.Button("{{ a.label }}", v_if="!a.options.length", classes="btn", click=(self.act, "[a.id, '']"))
                dropdown(
                    "''",
                    "a.options",
                    (self.act, "[a.id, $event.detail]"),
                    ("a.label",),
                    placeholder="a.label",
                    classes="btn su-select",
                    v_if="a.options.length",
                )

    def view_overlays(self) -> None:
        """Inside the case view: the pick banner, the drag readout and the inflow legend."""
        with html.Div(classes="su-banner", v_if="su_pick"):
            html.Span("click the view to set {{ su_pick.split('.').pop() }}")
            html.Button("Cancel", classes="btn", click="su_pick = ''")
        html.Div("{{ su_drag }}", classes="su-banner mono", v_if="su_drag && !su_pick")
        with html.Div(classes="su-banner", v_if="su_located && !su_pick && !su_drag"):
            html.Span("{{ su_located }}", title="from the check report")
            html.Button("Back to report", classes="btn", click="tab = 'report'")
            html.Button("Clear", classes="btn", click=self.clear_located)
        with html.Div(
            classes="su-speed",
            v_if="su_inflow && su_inflow_on",
            title=("'Inflow arrows: length and color by speed. ' + su_inflow.sources",),
        ):
            html.Div(classes="su-bar", style=("{background: su_inflow.gradient}",))
            with html.Div(classes="su-row mono"):
                html.Span("|u| 0")
                html.Span("{{ su_inflow.estimate ? '≈ ' : '' }}{{ su_inflow.max }} {{ su_inflow.unit }}")

    def inflow_button(self) -> None:
        """The case toolbar's Inflow arrows switch, in a case with an inflow boundary."""
        with html.Button(
            v_if="su_inflow_zones",
            classes=("su_inflow_on ? 'tb on' : 'tb'",),
            __properties=[("aria_pressed", "aria-pressed")],
            aria_pressed=("String(su_inflow_on)",),
            click=self.toggle_inflow,
            title="Inflow arrows: the velocity on every inflow boundary",
        ):
            html.Span(v_html="icons.wind")
            html.Span("Inflow arrows")

    def report_view(self) -> None:
        with html.Div(classes="su-report"):
            with html.Div(classes="su-sec"):
                with html.Div(classes="group-head"):
                    html.Span("DIAGNOSTICS")
                    html.Span("{{ problems.length || 'none' }}", classes="mono")
                with html.Div(
                    v_for="(p, i) in problems",
                    key="i",
                    classes=("['su-diag', p.severity, {focus: report_focus === i}]",),
                    tabindex="0",
                    __properties=[("data_code", "data-code"), *COPY_PROPS],
                    data_code=("p.code",),
                    data_copy=("p.copy",),
                ):
                    html.Span(v_html="icons[p.severity === 'error' ? 'error' : 'warning']", classes="sev")
                    with html.Div(classes="su-body"):
                        with html.Div(classes="su-head"):
                            html.Span("{{ p.code }}", classes="code")
                            html.Span("{{ p.key }}", classes="key mono", v_if="p.key")
                            html.Button(
                                "{{ p.line }}",
                                classes="line mono linkbtn",
                                v_if="p.lineno",
                                title=("'Go to ' + p.line + ' in ' + input_name",),
                                click=(self.app.document.goto_line, "[p.lineno, p.column]"),
                            )
                            copy_button()
                        html.Div("{{ p.message }}", classes="msg")
                        with html.Div(classes="refs"):
                            html.Button(
                                "{{ ref }}",
                                v_for="ref in p.refs",
                                key="ref",
                                classes="chip mono",
                                title=("'Go to ' + ref",),
                                click=(self.report_goto, "[ref, '']"),
                            )
                            html.Button(
                                "use {{ su_suggestions[p.key] }}",
                                v_if="su_suggestions[p.key]",
                                classes="chip",
                                click=(self.rename_key, "[p.key, su_suggestions[p.key]]"),
                            )
                            with html.Button(
                                v_if="p.docs",
                                classes="linkbtn docs",
                                title=("p.docs",),
                                click=(self.open_docs, "[p.docs]"),
                            ):
                                html.Span("docs")
                                html.Span(v_html="icons.external")
            with html.Div(v_for="sec in su_summary.sections", key="sec.title", classes="su-sec"):
                html.Div("{{ sec.title }}", classes="group-head")
                with html.Div(
                    v_for="(r, i) in sec.rows",
                    key="i",
                    classes=("['su-kv', r.status, (r.key || r.node) ? 'link' : '']",),
                    title=("r.key ? 'Go to ' + r.key : r.node ? 'Select ' + r.k : ''",),
                    click=(self.report_goto, "[r.key, r.node]"),
                ):
                    html.Span("{{ r.k }}", classes="k")
                    html.Span("{{ r.v }} {{ r.u }}", classes="v mono", title=("r.v",))
                    html.Span("{{ r.note }}", classes="n muted", title=("r.note",))
                    html.Span("{{ r.key }}", classes="key mono")
                    html.Button(
                        v_html="icons.pick",
                        v_if="r.x",
                        classes="icon-btn locate",
                        title="Show on the case view",
                        aria_label=("'Show ' + r.k.trim() + ' on the case view'",),
                        __events=[("click_stop", "click.stop")],
                        click_stop=(self.report_locate, "[r.k.trim(), r.x]"),
                    )
                    html.Span(v_if="!r.x")
            with html.Div(classes="su-raw"):
                with disclosure("su_raw_open", "su_raw_open = !su_raw_open", classes="group-head"):
                    html.Span("the report as mallard check wrote it")
                html.Pre("{{ report_text }}", v_if="su_raw_open")

    def problem_fix(self) -> None:
        html.Button(
            "use {{ su_suggestions[p.key] }}",
            v_if="su_suggestions[p.key]",
            classes="linkbtn",
            __events=[("click_stop", "click.stop")],
            click_stop=(self.rename_key, "[p.key, su_suggestions[p.key]]"),
        )

    def launch_dialog(self) -> None:
        """Two parts: RUN, what Launch passes to the process it starts, and ESTIMATE, what only `mallard check`'s
        sizing and cost read, with its results. Every control is in exactly one (`data-part`)."""
        for key in CHART_KEYS:
            client.ClientStateChange(value=key, change=f"window.mallardChart && window.mallardChart.update('{key}')")
        with html.Div(classes="su-modal", v_if="su_launch.open", click_self="su_launch = {...su_launch, open: false}"):
            with html.Div(classes="su-dialog", role="dialog", aria_label="Run"):
                with html.Div(classes="panel-head"):
                    with html.Span():
                        html.Span("Run ")
                        html.Span("{{ input_name }}", classes="mono")
                    with html.Span(classes="mono su-check-state", data_part="dialog"):
                        html.Span("estimate: ")
                        html.Span("checking", v_if="check.running")
                        html.Span("{{ check.status || 'not checked' }} {{ check.ago }}", v_else=True)
                with html.Div(classes="su-launch-body"):
                    with html.Div(classes="su-col su-col-settings"):
                        self._run_part()
                        self._estimate_part()
                        self._estimate_report()
                    with html.Div(classes="su-col su-col-results", data_part="estimate"):
                        self._estimate_results()
                with html.Div(classes="actions", data_part="dialog"):
                    html.Span("{{ su_launch.busy }}", classes="muted mono", role="status")
                    html.Div(classes="grow")
                    html.Button("Close", classes="btn", click=self.close_launch)

    def _run_part(self) -> None:
        with html.Section(classes="su-part run", data_part="run", aria_label="Run settings"):
            with html.Div(classes="group-head su-part-head"):
                html.Span("RUN")
                html.Span("used when you launch", classes="note")
            with html.Div(classes="su-form"):
                with html.Div(classes="prow"):
                    html.Div("where", classes="plabel")
                    html.Div("this machine (local)", classes="pval ro")
                with html.Div(classes="prow"):
                    html.Div("ranks", classes="plabel", title="MPI ranks Launch starts (mpirun -n)")
                    with html.Div(classes="pval"):
                        html.Input(
                            value=("su_launch.ranks",),
                            aria_label="ranks",
                            change=(self.set_launch, "['ranks', $event.target.value]"),
                        )
                        html.Span("run + estimate", classes="unit", title="Also sizes the estimate below")
                with html.Div(classes="prow"):
                    html.Div("threads per rank", classes="plabel", title="--kokkos-num-threads, from Settings")
                    html.Div("{{ su_launch.threads }}", classes="pval ro")
                with html.Div(classes="prow", v_if="su_devices"):
                    html.Div("devices", classes="plabel")
                    html.Div("{{ su_devices }}", classes="pval ro", title="as this Mallard sees them")
            with html.Div(classes="su-part-foot"):
                with html.Span(classes="muted su-launch-why", role="status", v_if="run_blocked || su_launch.busy"):
                    html.Span(classes="spin", v_if="check.running || su_launch.busy")
                    html.Span(
                        "{{ su_launch.busy ? 'Measuring the throughput: ' + su_launch.busy "
                        ": check.running ? 'Waiting for the estimate…' "
                        ": run_blocked === 'check first' ? 'Check the case first' : 'Blocked: ' + run_blocked }}"
                    )
                html.Div(classes="grow")
                with html.Button(
                    classes="btn primary su-launch-btn",
                    click=self.start,
                    disabled=("!!run_blocked || !!su_launch.busy",),
                    aria_label="Launch",
                ):
                    html.Span(v_html="icons.play")
                    html.Span(
                        "Launch on this machine, {{ su_launch.ranks }} rank{{ +su_launch.ranks === 1 ? '' : 's' }}"
                    )

    def _estimate_part(self) -> None:
        with html.Section(classes="su-part estimate", data_part="estimate", aria_label="Estimate settings"):
            with html.Div(classes="group-head su-part-head"):
                html.Span("ESTIMATE")
                html.Span("only affects the cost estimate", classes="note")
            with html.Div(classes="su-form"):
                with html.Div(classes="prow"):
                    html.Div("size for", classes="plabel", title="mallard check --memory")
                    with (
                        html.Div(classes="pval"),
                        html.Select(
                            aria_label="size for",
                            title="The memory per rank mallard check sizes the run against",
                            change=(self.set_launch, "['memory', $event.target.value]"),
                        ),
                    ):
                        html.Option(
                            "{{ t.label }}",
                            v_for="t in su_targets",
                            key="t.value",
                            value=("t.value",),
                            selected=("t.value === (su_launch.memory || '')",),
                        )
                with html.Div(classes="prow"):
                    html.Div("throughput", classes="plabel")
                    with html.Div(classes="pval"):
                        html.Input(
                            value=("su_launch.throughput",),
                            placeholder="cells/s per rank, or measure",
                            aria_label="throughput",
                            change=(self.set_launch, "['throughput', $event.target.value]"),
                        )
                        html.Span("cells/s/rank", classes="unit")
                with html.Div(classes="prow", v_if="su_launch.tp.text"):
                    html.Div("basis", classes="plabel")
                    html.Div(
                        "{{ su_launch.tp.text }} · {{ su_launch.tp.basis }}",
                        classes=("['pval', 'ro', 'su-tp', su_launch.tp.kind]",),
                        title=("su_launch.tp.basis",),
                        aria_label="throughput basis",
                    )
                with html.Div(classes="prow"):
                    html.Div("measure", classes="plabel")
                    with html.Div(classes="pval su-measure"):
                        html.Input(
                            value=("su_launch.cap",),
                            aria_label="measure for at most",
                            title="Measure stops once the median is known to ±3 %, or after this long",
                            change=(self.set_launch, "['cap', $event.target.value]"),
                        )
                        html.Span("s at most", classes="unit")
                        html.Button(
                            "Measure",
                            classes="btn",
                            v_if="!su_launch.busy",
                            click=self.measure,
                            title="Time this case's steady stepping on one rank here, until it is known to ±3 %",
                        )
                        html.Button("Stop measuring", classes="btn", v_else=True, click=self.cancel_measure)
                message("su_launch.error", "perr", v_if="su_launch.error")

    def _estimate_results(self) -> None:
        """What the estimate concludes, at a glance: the verdict, the large figures and the plots over ranks."""
        html.Div(
            "{{ su_summary.fit.text }}",
            classes=("['su-fit', su_summary.fit.status]",),
            role="status",
            v_if="su_summary.fit && su_summary.fit.text",
        )
        with html.Div(classes="su-headline", role="group", aria_label="Sizing at a glance"):
            with html.Div(
                v_for="h in su_headline",
                key="h.k",
                classes=("['su-fig', h.status]",),
                title=("h.note",),
            ):
                html.Div("{{ h.k }}", classes="su-fig-k")
                with html.Div(classes="su-fig-v mono"):
                    html.Span("{{ h.v }}")
                    html.Span("{{ h.u }}", classes="su-fig-u", v_if="h.u")
                with html.Div(classes="su-fig-bar", v_if="h.bar !== null && h.bar !== undefined"):
                    html.Div(style=("{width: (100 * h.bar).toFixed(1) + '%'}",))
                html.Div("{{ h.note }}", classes="su-fig-n")
        with html.Div(classes="su-charts"):
            with html.Div(classes="su-charts-head"):
                html.Span("over ranks: click a point to set the run's ranks", classes="muted")
                html.Span("{{ su_sweep }}", classes="muted mono", role="status")
            charts = (
                ("wall time and total compute hours", "", "su-chart primary"),
                ("memory per rank", "", "su-chart"),
                ("halo per rank", "", "su-chart secondary"),
            )
            for key, (title, note, classes) in zip(CHART_KEYS, charts, strict=True):
                with html.Div(classes=classes, title=note or None):
                    html.Div(title, classes="su-chart-t")
                    if key == "su_chart_time":
                        html.Div(
                            "{{ su_chart_time.basis }}",
                            v_if="su_chart_time.basis",
                            classes="su-chart-s muted",
                            title=("su_chart_time.basis",),
                        )
                    html.Div(classes="m-chart", id=key)

    def _estimate_report(self) -> None:
        """The check's cost, memory, sizing and time step rows the estimate rests on."""
        with html.Div(classes="su-report su-launch-report", data_part="estimate"):
            with html.Div(
                v_for="sec in su_summary.sections.filter(s => ['COST', 'MEMORY', 'SIZING', 'TIME STEP']"
                ".includes(s.title))",
                key="sec.title",
                classes="su-sec",
            ):
                html.Div("{{ sec.title }}", classes="group-head")
                with html.Div(v_for="r in sec.rows", key="r.k", classes=("['su-kv', r.status]",)):
                    html.Span("{{ r.k }}", classes="k")
                    html.Span("{{ r.v }} {{ r.u }}", classes="v mono")
                    html.Span("{{ r.note }}", classes="n muted")

    def control_tab(self) -> None:
        with html.Div(classes="scroll", v_if="run_tab === 'control'"):
            empty_state(
                "sliders",
                "Live control",
                "Settable keys (CFL, output intervals, the stop time) change here while a run on this machine is "
                "running.",
                "!su_live.active",
                compact=True,
            )
            with html.Div(v_if="su_live.active"):
                html.Div("settings from the next step on · via {{ su_live.channel }}", classes="prop-foot")
                with html.Div(v_for="k in su_live.keys", key="k.key", classes="prow"):
                    html.Div("{{ k.key }}", classes="plabel", title=("k.help || k.key",))
                    with html.Div(classes="pval"):
                        html.Input(
                            value=("k.value",),
                            aria_label=("k.key",),
                            placeholder=("k.placeholder",),
                            title=("k.help",),
                            spellcheck="false",
                            change=(self.apply_live, "[k.key, $event.target.value]"),
                            keydown_enter="$event.target.blur()",
                        )
                        html.Span("{{ k.units }}", classes="unit", v_if="k.units")
                html.Div(
                    "{{ su_live.reply }}",
                    classes="prop-foot",
                    style=("{color: su_live.ok ? '' : 'var(--m-danger)'}",),
                    v_if="su_live.reply",
                )
                with html.Div(v_for="(r, i) in su_live.replies", key="i", classes="events-row"):
                    html.Span("{{ r.step }}", classes="muted")
                    html.Span("{{ r.ok ? 'set' : 'refused' }}", classes="ekind")
                    html.Span("{{ r.setting }}", classes="etext")


def device_text(devices: dict[str, Any]) -> str:
    """What `initialize`'s `devices` says this build runs on: its backend, its GPUs or the host's threads."""
    if not devices:
        return ""
    gpus = [g for g in devices.get("visible") or [] if isinstance(g, dict)]
    if gpus:
        names = ", ".join(f"{g.get('name', 'GPU')} ({int(g.get('memory') or 0) / 2**30:.0f} GiB)" for g in gpus)
        return f"{devices.get('backend', '')} · {len(gpus)} GPU{'s' if len(gpus) > 1 else ''}: {names}"
    host = devices.get("host") or {}
    return f"{devices.get('backend', '')} · {host.get('cpu', '')} · {host.get('threads', 0)} threads"


def size_targets(gpus: list[str], devices: dict[str, Any]) -> list[dict[str, str]]:
    """The run dialog's `--memory` choices: this machine as detected (the check's default), then the GPUs."""
    visible = [g for g in devices.get("visible") or [] if isinstance(g, dict)]
    if visible:
        g = visible[0]
        found = f"{g.get('name', 'GPU')} {int(g.get('memory') or 0) / 2**30:.0f} GiB"
        found += f" ×{len(visible)}" if len(visible) > 1 else ""
    elif devices.get("host"):
        found = f"{devices['host'].get('threads', 0)} CPU threads, no memory limit"
    else:
        found = ""
    here = f"this machine (detected: {found})" if found else "this machine (its GPU's memory, else no limit)"
    return [{"value": "", "label": here}] + [{"value": g, "label": target_label(g)} for g in gpus]


def target_here(memory: str, devices: dict[str, Any]) -> bool:
    """Whether Measure can time the case on the sizing target here: a memory size or this machine always, a GPU
    model only when this machine has one of its memory."""
    key = memory.lower()
    if key not in GPU_TARGETS:
        return True
    model, _, gib = key.partition("-")
    for g in devices.get("visible") or []:
        if not isinstance(g, dict) or model not in str(g.get("name", "")).lower().replace(" ", ""):
            continue
        if not gib or abs(float(g.get("memory") or 0) / 2**30 - int(gib)) <= 0.15 * int(gib):
            return True
    return False


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _text(value: Any) -> str:
    return "" if value is None else str(value)
