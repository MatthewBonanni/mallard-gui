"""The trame application: case setup, the case view with the section tool, check, and the run monitor.

The UI holds no state that matters: everything comes from the Session, which reads and writes files.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Iterator
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from trame.app import get_server
from trame.ui.html import DivLayout
from trame.widgets import client, html
from trame.widgets import vtk as vtkw
from trame_client.widgets.core import HTML_CTX, AbstractElement
from trame_common.obj import mcp as mcp_options
from trame_common.obj.component import can_be_decorated
from trame_server import Server

from .. import loading
from ..checkinfo import diagnostic_text, suggestions
from ..mallard import Diagnostic, Mallard, MallardError, Superseded
from ..pagelog import PageLog
from ..runlog import RunState
from ..session import ActiveRun, RestartError, Session
from ..tokens import css_variables, load_tokens
from ..view.scene import PRESETS, CaseScene
from ..view.scene_ids import reference_id
from .analysis_page import AnalysisPage
from .analysis_panel import AnalysisPanel
from .case_files_panel import CaseFilesPanel
from .compare_page import ComparePage
from .copy import COPY_PROPS, DOCS_BASE, copy_button, message
from .data_page import DataPage
from .disclosure import disclosure
from .document_panel import PAGES, DocumentPanel
from .dropdown import dropdown
from .empty import empty_state
from .export_panel import ExportPanel
from .file_watch import FileWatch
from .fmt import bytes_text as _bytes
from .fmt import duration as _duration
from .fmt import grouped
from .fmt import number as _fmt
from .health_panel import HealthPanel
from .icons import ICONS
from .inspector import Inspector
from .monitor_plots import KEYS, MonitorPlots
from .panes import BODY_STYLE, Panes, list_splitter, list_style, splitter
from .paths import PathChooser
from .remotes_page import RemotesPage
from .results_page import ResultsPage
from .section import section_bar, section_state
from .settings_page import SettingsPage
from .setup_tools import SetupTools

# Surfaces the browser draws itself up to this many polygons (the design's ~2 million for vtk.js); above it,
# images rendered here.
GEOMETRY_TRIANGLES = 2_000_000
# vtk.js camera controls: drag orbits, shift-drag or middle pans, wheel or right zooms; in 2D, drag pans.
ORBIT = [
    {"button": 1, "action": "Rotate"},
    {"button": 1, "action": "Pan", "shift": True},
    {"button": 1, "action": "Zoom", "control": True},
    {"button": 2, "action": "Pan"},
    {"button": 3, "action": "Zoom", "scrollEnabled": True},
]
PAN = [
    {"button": 1, "action": "Pan"},
    {"button": 1, "action": "Pan", "shift": True},
    {"button": 2, "action": "Pan"},
    {"button": 3, "action": "Zoom", "scrollEnabled": True},
]

SECTION_ICONS = {
    "mesh": "mesh",
    "physics": "physics",
    "boundaries": "boundaries",
    "initialize": "initialize",
    "numerics": "numerics",
    "regions": "regions",
    "probes": "probes",
    "output": "output",
    "statistics": "statistics",
    "run": "run",
    "chemistry": "chemistry",
    "les": "les",
    "radiation": "radiation",
    "sponges": "sponges",
    "sources": "sources",
    "triggers": "triggers",
    "parallel": "parallel",
    "particles": "particles",
    "periodic": "periodic",
}
SYSTEM_DARK = "window.matchMedia('(prefers-color-scheme: dark)').matches"
CHECK_DEBOUNCE = 0.4
POLL_INTERVAL = 1.0


_DECORATED = ("_trame_state_change", "_trame_trigger_names", "_trame_controller")
_binds: dict[type, bool] = {}


def _binds_methods(cls: type) -> bool:
    """Whether a widget class has methods trame binds on construction (decorated, or agents' tools)."""
    if cls not in _binds:
        _binds[cls] = bool(mcp_options.first_class_members(cls)) or any(
            any(name in getattr(fn, "__dict__", {}) for name in _DECORATED)
            for _, fn in inspect.getmembers(cls, can_be_decorated)
        )
    return _binds[cls]


@contextlib.contextmanager
def one_render() -> Iterator[None]:
    """Builds a layout in one pass. trame renders an element's whole subtree as each `with` block closes, to flush
    attribute defaults into the state, so a layout of depth d renders about d times; the layout's own final render
    flushes every default too. And every element searches its class for decorated methods, which plain widgets
    never have: done once per class here."""
    exit_, bind = AbstractElement.__exit__, AbstractElement._bind_annotated_methods

    def bind_once(self: AbstractElement) -> None:
        if _binds_methods(type(self)):
            bind(self)

    AbstractElement.__exit__ = lambda self, *_: HTML_CTX.exit(self)
    AbstractElement._bind_annotated_methods = bind_once
    try:
        yield
    finally:
        AbstractElement.__exit__ = exit_
        AbstractElement._bind_annotated_methods = bind


class App:
    def __init__(self, session: Session, server: Server | None = None) -> None:
        self.session = session
        self.server = server or get_server(client_type="vue3")
        self.state, self.ctrl = self.server.state, self.server.controller
        self.scene = CaseScene(offscreen=True)
        self.scene.on_clip_moved = self._clip_moved
        self.page_log = PageLog()
        self._check_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Future[None]] = set()
        self._poll_task: asyncio.Task[None] | None = None
        self._run_name: str | None = None
        self._init_state()
        self.inspector = Inspector(self)
        self.data_page = DataPage(self)
        self.remotes_page = RemotesPage(self)
        self.results_page = ResultsPage(self)
        self._pushed_camera: tuple[Any, ...] = ()
        self._scene_sent = 0.0  # when the browser was last sent a scene it has not applied yet
        self._scene_stale = False
        self._view_ready = False
        self.setup = SetupTools(self)
        self.paths = PathChooser(self)
        self.document = DocumentPanel(self)
        self.health = HealthPanel(self)
        self.case_files = CaseFilesPanel(self)
        self.exports = ExportPanel(self)
        self.compare_page = ComparePage(self)
        self.analysis = AnalysisPanel(self)
        self.analysis_page = AnalysisPage(self)
        self.monitor = MonitorPlots(self)
        self.settings_page = SettingsPage(self)
        self.panes = Panes(self)
        self.watch = FileWatch(self)
        self._register()
        self._layout()
        loading.install(self.server, session.project.name)
        self.ctrl.on_server_ready.add(self._ready)

    # State

    def _init_state(self) -> None:
        s = self.state
        s.trame__title = f"{self.session.project.name} - Mallard"
        s.trame__favicon = "mallard_gui/favicon.png"
        s.icons = ICONS
        s.page = "case"
        s.tab = "view"
        s.theme = "dark"
        s.project_name = self.session.project.name
        s.input_name = self.session.input_path.name
        s.tree = []
        s.collapsed = []
        s.selected = "section:boundaries"
        s.props = {"title": "", "groups": []}
        s.problems = []
        s.prob_sel, s.report_focus = [], -1
        s.check = {"status": "", "errors": 0, "warnings": 0, "seconds": 0.0, "running": False, "ago": ""}
        s.run_blocked = "check first"
        s.toml_text = self.session.doc.text
        s.toml_error = ""
        s.check_error = ""
        s.report_text = ""
        s.blocks = []
        s.skipped = []
        s.dim = 3  # until a case view says 2: the orientation buttons are there while none is loaded
        s.lock2d = False
        s.parallel = False
        s.preset = ""
        s.clip = False
        s.clip_origin = ["0", "0", "0"]
        s.clip_normal = ["0", "0", "1"]
        s.clip_flip = False
        s.snap = True
        s.sec = {"lo": 0.0, "hi": 1.0, "at": 0.0, "text": "0", "axis": "z"}
        s.sec_mark = True
        s.sec_more = False
        s.case_section = {"on": False}  # the plane and the actors it cuts, for the browser's own drawing (zoom.js)
        s.xray = False
        s.xray_opacity = 20
        s.pick = True
        s.mesh_edges = False
        s.caseview_mode = "local"
        s.case_pins = []
        s.case_body = None
        s.caseview_orbit = ORBIT
        s.caseview_pan = PAN
        s.readout = ""
        s.tip = None
        s.search = ""
        s.search_results = []
        s.build = ""
        s.cells = ""
        s.version = self.session.mallard.version()
        # Runs
        s.runs = []
        s.run_sel = ""
        s.run = {}
        s.run_info = []
        s.stats = []
        s.plot_sources = []
        s.plot_choice = ["", "", "", ""]
        s.run_events = []
        s.run_log = ""
        s.run_tab = "log"
        s.outputs = []
        s.lineage = []
        s.restart_pick = {}

    def _register(self) -> None:
        self.state.change("search")(self._search)
        self.server.trigger("palette_pick")(self.setup.palette_pick)
        self.server.trigger("goto_problem")(self.goto_problem)
        self.server.trigger("case_handle")(self.on_handle)
        self.server.trigger("case_camera")(self.on_camera)
        self.server.trigger("case_zoom")(self.on_zoom)
        self.server.trigger("case_fit")(self.fit)
        self.server.trigger("case_frame")(self.focus)
        self.server.trigger("case_scene_applied")(self.on_scene_applied)
        self.server.trigger("sec_slide")(self.slide_section)
        self.state.case_size = {}
        self.state.change("case_size")(self._case_resized)
        self.server.trigger("case_view_ready")(self.on_view_ready)
        self.server.trigger("page_event")(self.page_log.write)
        self.state.change("clip_origin", "clip_normal", "clip_flip")(self._clip_fields)
        self.state.change("xray_opacity")(self._xray_opacity)
        self.server.trigger("system_theme")(self.system_theme)
        self.server.trigger("toml_cursor")(self.select_line)
        self.server.trigger("mount_theme")(self.mount_theme)

    # Lifecycle

    def _ready(self, **_: Any) -> None:
        versions = {name: metadata.version(name) for name in ("mallard-gui", "trame-vtk", "vtk")}
        self.page_log.write("server", {"project": str(self.session.project.root), **versions}, source="server")
        self._tasks.add(task := asyncio.ensure_future(self._first_check()))
        task.add_done_callback(self._tasks.discard)

    async def _first_check(self) -> None:
        await self.run_check()
        with self.state:
            self.state.runs = self._runs_list()

    # Case tree and properties

    def _refresh_case(self) -> None:
        s, ses = self.state, self.session
        tree = ses.tree()
        for n in tree:
            n["icon"] = SECTION_ICONS.get(n["id"].removeprefix("section:"), "other") if n["depth"] == 0 else ""
        s.tree = tree
        if not any(n["id"] == s.selected for n in tree):
            s.selected = tree[0]["id"] if tree else ""
        s.props = self.inspector.props(s.selected)
        self.inspector.schedule_presets()
        for n in tree:
            if n["depth"] == 0:
                n["adds"] = self.inspector.section_adds(n["id"].removeprefix("section:"))
        s.problems = [self._problem(d) for d in ses.diagnostics]
        s.prob_sel, s.report_focus = [], -1
        s.toml_text = ses.doc.text
        s.run_blocked = ses.run_blocked
        self.document.sync()
        self.setup.refresh()

    def _problem(self, d: Diagnostic) -> dict[str, Any]:
        line = d.line or (self.session.doc.line_of(d.key) if d.key else None)
        column = d.column if d.line else None
        report = self.session.report
        rename = suggestions(report.data).get(d.key or "") if report else None
        where = f"{self.session.input_path.name} line {line}" + (f":{column}" if column else "") if line else ""
        return {
            "copy": diagnostic_text(
                d.severity,
                d.code,
                d.message,
                d.key or "",
                where,
                f"rename {d.key} to {rename}" if rename else "",
                DOCS_BASE + d.docs if d.docs else "",
            ),
            "severity": d.severity,
            "code": d.code,
            "key": d.key or "",
            "message": d.message,
            "line": (f"line {line}" + (f":{column}" if column else "")) if line else "line –",
            "lineno": line,
            "column": column,
            "node": self.session.link_of(d) or "",
            "docs": d.docs,
            "refs": list(d.refs),
        }

    def select(self, node_id: str, from_view: bool = False, reveal: bool = True) -> None:
        s = self.state
        s.selected = node_id
        s.props = self.inspector.props(node_id)
        s.key_help = None
        self.inspector.schedule_presets()
        if not from_view:
            s.tip = None  # the label of a pick in the view
            self.scene.select(self._block_of(node_id))
        if reveal:
            self.document.reveal_lines(self.session.spans_of(node_id))
        self.setup.refresh()
        if not from_view and reveal:  # chosen in the case tree (not by the text's caret)
            if node_id == "section:initialize":
                self.analysis.show_initial()
            else:
                self.analysis.leave_initial()
        self._update_view()

    def select_line(self, line: int) -> None:
        """The case-tree node of the line the caret is on in the text, while the text is the document's."""
        s = self.state
        if s.toml_text != self.session.doc.text:
            return
        node = self.session.node_at_line(int(line))
        if node and node != s.selected and any(n["id"] == node for n in s.tree):
            self.select(node, reveal=False)

    def toggle(self, node_id: str) -> None:
        collapsed = list(self.state.collapsed)
        if node_id in collapsed:
            collapsed.remove(node_id)
        else:
            collapsed.append(node_id)
        self.state.collapsed = collapsed

    def _block_of(self, node_id: str) -> str | None:
        kind, _, ident = node_id.partition(":")
        if kind in ("zone", "region") and ident in self.scene.blocks:
            return ident
        if kind == "el":
            path = ident
            el = self.session.doc.get(path) or {}
            for b in self.scene.blocks.values():
                if b.input_path and el.get("name") and b.input_path == f'{path.split("[")[0]}[name = "{el["name"]}"]':
                    return b.name
        return None

    def _node_of_block(self, block: str) -> str | None:
        b = self.scene.blocks.get(block)
        if b is None:
            return None
        if b.kind == "zone":
            return f"zone:{b.name}"
        if b.kind == "region":
            editor = self.setup.editor  # the tree's node of the shape drawn as this block, which has its handles
            return next((s.node for s in editor.shapes() if editor.block_of(s.node) == b.name), f"region:{b.name}")
        if b.kind == "probe" and b.input_path:
            try:
                seg = self.session.doc.resolve(b.input_path)
                return f"el:{seg[0]}[{seg[1]}]"
            except (KeyError, ValueError):
                return None
        return None

    # Editing

    def set_key(self, path: str, text: str, unit: str | None = None) -> None:
        try:
            self.session.set_value(path, str(text), unit)
        except (ValueError, KeyError) as e:
            props = self.inspector.props(self.state.selected)
            rows = [r for g in props["groups"] for r in g["rows"] if r["path"] == path]
            for r in rows:  # the error under the field, which keeps what was typed
                r.update(error=str(e), severity="error", value=str(text), number=str(text))
            self.state.props = props if rows else {**props, "edit_error": str(e)}
            return
        self._refresh_case()
        self._schedule_check()

    def assign_zone(self, zone: str, condition: str) -> None:
        if not condition:
            return
        self.session.assign_zone(zone, condition)
        self._refresh_case()
        self._schedule_check()

    def apply_toml(self, text: str) -> None:
        try:
            self.session.set_text(text)
        except Exception as e:  # noqa: BLE001  any parse error is shown, not raised
            self.state.toml_error = str(e)
            return
        self.state.toml_error = ""
        self._refresh_case()
        self._schedule_check()

    def revert_toml(self) -> None:
        self.state.toml_text = self.session.doc.text
        self.state.toml_error = ""

    def _search(self, search: str = "", **_: Any) -> None:
        self.state.search_results = self.setup.palette_search(search)

    def search_pick(self, path: str) -> None:
        """Jump to the node holding a catalog key (the command palette over keys)."""
        table = path.split(".", 1)[0]
        section = self.session.section_of(table)
        self.state.search = ""
        self.state.search_results = []
        self.state.page = "case"
        self.select(f"section:{section}")

    # Check

    def _schedule_check(self) -> None:
        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
        self.session.mallard.supersede("check")  # a check of the text before this edit sets the case up for nothing
        self._check_task = asyncio.ensure_future(self._debounced_check())

    async def _debounced_check(self) -> None:
        await asyncio.sleep(CHECK_DEBOUNCE)
        await self.run_check()

    async def run_check(self) -> None:
        s, ses = self.state, self.session
        with s:
            s.check = {**s.check, "running": True}
        ses.mallard.supersede("check")
        Mallard.group("check")  # this task's commands, and its threads'
        try:
            if not ses.catalog_in_check:
                await asyncio.to_thread(ses.refresh_catalog)
            report = await asyncio.to_thread(ses.check)
        except Superseded:
            return
        except (MallardError, OSError) as e:
            with s:
                s.check = {**s.check, "status": "failed", "running": False, "ago": time.strftime("%H:%M:%S")}
                s.check_error = f"mallard check could not run: {e}"
            return
        await self.show_report(report)

    async def check_and_show(self) -> None:
        """Check on request: its report once it is done, wherever the user went meanwhile."""
        await self.run_check()
        with self.state:
            self.set_page("case")
            self.state.tab = "report"

    async def show_report(self, report: Any) -> None:
        s = self.state
        with s:
            s.check_error = ""
            s.check = {
                "status": report.status,
                "errors": report.errors,
                "warnings": report.warnings,
                "seconds": round(report.seconds, 2),
                "running": False,
                "ago": time.strftime("%H:%M:%S"),
            }
            env = report.data.get("environment", {}).get("build", {})
            if env:
                s.build = f"{env.get('dim', '?')}D · {env.get('precision', '')} · {_backend(env.get('backend', ''))}"
            cells = (report.data.get("cost") or {}).get("cells")
            s.cells = grouped(cells) + " cells" if cells else ""
            s.report_text = _report_text(report.data)
            self.analysis.mark_stale()
            loaded = bool(report.view and report.view.exists())
            if loaded:
                self._load_view(report)
            self._refresh_case()
            self.setup.after_check(report, loaded)

    def _load_view(self, report: Any) -> None:
        assigned = {z["zone"] for z in self.session.zones() if "index" in z}
        first = not self.scene.blocks
        camera = self.scene.renderer.GetActiveCamera()
        saved = (
            camera.GetPosition(),
            camera.GetFocalPoint(),
            camera.GetViewUp(),
            camera.GetParallelScale(),
            camera.GetViewAngle(),
        )
        dim_before = self.scene.dim
        self.scene.load(report.view, report.data, assigned)
        self.state.tip = None  # its block was drawn anew
        if not first and dim_before == self.scene.dim:
            camera.SetPosition(*saved[0])
            camera.SetFocalPoint(*saved[1])
            camera.SetViewUp(*saved[2])
            camera.SetParallelScale(saved[3])
            camera.SetViewAngle(saved[4])  # the reload's fit narrows it: the view would zoom at every check
            self.scene.renderer.ResetCameraClippingRange()
        if first or dim_before != self.scene.dim:  # the first view, or a case that became 2D or 3D
            self.state.dim = self.scene.dim
            self.state.lock2d = self.scene.dim == 2
        self.scene.lock_2d = self.state.lock2d if self.scene.dim == 2 else False
        if first:
            self.state.clip_origin = [_fmt(c) for c in self.scene.clip_origin]
            self.scene.enable_plane_widget()
        self.scene.set_clip(self.state.clip)
        self.scene.set_xray(self.state.xray, self.state.xray_opacity / 100)
        self.scene.select(self._block_of(self.state.selected))
        self.state.blocks = [b.info() for b in self.scene.blocks.values() if b.kind != "mesh"]
        self.state.skipped = list(self.scene.skipped)
        self.analysis.view_loaded()
        self._update_view()

    def goto_problem(self, node: str) -> None:
        if node:
            self.select(node)

    def problem_details(self, i: int) -> None:
        """A problem's details: its entry in the check report, shown and highlighted."""
        s = self.state
        s.prob_sel = [int(i)]
        s.tab = "report"
        s.report_focus = int(i)

    # The view

    def _update_view(self) -> None:
        """The browser draws the case (vtk.js: orbit, pan and zoom never reach this process) while its
        surfaces fit the geometry budget; else, and while the clip plane's widget is out, this process
        renders images. Either way only what changed is sent: the scene's changed objects, or one image."""
        local = self.scene.triangles() <= GEOMETRY_TRIANGLES
        mode = "local" if local else "remote"
        self._section_state(local)
        if mode != self.state.caseview_mode:
            self.page_log.write("caseview_mode", {"to": mode, "triangles": self.scene.triangles()}, source="server")
        self.state.caseview_mode = mode
        overlay = self.setup.overlay if hasattr(self, "setup") else None
        if overlay is not None:
            self.state.case_pins = overlay.pins()
            self.state.case_body = overlay.body()
        self.state.flush()  # the state the view is drawn with (the 2D lock, a preset) reaches the page first
        if local:
            self._send_scene()
        else:
            self._view.update_image()
        camera = self._camera_key()
        if camera != self._pushed_camera:  # a preset or fit here; the page's camera places the pins
            self._pushed_camera = camera
            self._view.push_camera()

    def _section_state(self, local: bool) -> None:
        """The section plane for the page: the bar's slider, and, where the browser draws, the plane itself with
        the actors it cuts (vtk.js clips them as it draws, so a moved plane sends no geometry). The plane's drag
        widget is this process's: it is out only where this process renders the view."""
        scene = self.scene
        if scene.widget_shown == local:
            scene.widget_shown = not local
            scene.set_clip(scene.clip_enabled)
        self.state.sec = section_state(scene.section_extent(), scene.section_offset(), scene.clip_normal)
        section: dict[str, Any] = {"on": False}
        if scene.clip_enabled and local:
            section = {
                "on": True,
                "origin": list(scene.plane.GetOrigin()),
                "normal": list(scene.plane.GetNormal()),
                "actors": [reference_id(a) for a in scene.clipped],
            }
        if section != self.state.case_section:
            self.state.case_section = section

    def slide_section(self, fraction: float) -> None:
        """The bar's slider: the plane at that fraction of the scene's reach along its normal."""
        lo, hi = self.scene.section_extent()
        self.set_section_offset(lo + min(max(float(fraction), 0.0), 1.0) * (hi - lo))

    def set_section_offset(self, value: Any) -> None:
        """The plane moved along its normal to the position `value` (for an axis, that coordinate)."""
        try:
            at = float(value)
        except (TypeError, ValueError):
            return
        scene = self.scene
        n, shift = scene.clip_normal, at - scene.section_offset()
        self.state.clip_origin = [_fmt(o + shift * c) for o, c in zip(scene.clip_origin, n, strict=True)]

    def _send_scene(self) -> None:
        """The scene to the browser, once it has applied the last one sent (zoom.js says so): a scene changing
        faster than its arrays arrive (a dragged region's live highlight over a slow link) is sent as fast as it
        is drawn, the latest each time, instead of queueing every state in between. A page that does not answer
        within a second (none open, or one hidden) is sent to anyway."""
        now = time.monotonic()
        if self._scene_sent and now - self._scene_sent < 1.0:
            if not self._scene_stale:
                self._scene_stale = True
                with contextlib.suppress(RuntimeError):  # no loop: nothing was sent from one either
                    asyncio.get_running_loop().call_later(1.0, self._scene_overdue)
            return
        self._scene_sent, self._scene_stale = now, False
        self._view.update_geometry()

    def on_scene_applied(self) -> None:
        """The browser drew the last scene sent: the one made since goes."""
        self._scene_sent = 0.0
        if self._scene_stale:
            self._update_view()

    def _scene_overdue(self) -> None:
        if self._scene_stale:
            self.on_scene_applied()

    def _camera_key(self) -> tuple[Any, ...]:
        c = self.scene.renderer.GetActiveCamera()
        return (
            c.GetPosition(),
            c.GetFocalPoint(),
            c.GetViewUp(),
            c.GetParallelProjection(),
            c.GetParallelScale(),
            c.GetViewAngle(),
        )

    def on_handle(self, index: int, position: list[float], done: bool) -> None:
        """A drag handle moved in the page (its pin), or let go there."""
        overlay = self.setup.overlay
        pos = [float(c) for c in position][:3]
        if done:
            overlay.release_handle(int(index), pos)
        else:
            overlay.move_handle(int(index), pos)
        self._update_view()

    def on_camera(self, camera: Any) -> None:
        """The browser's camera after an orbit, pan or zoom, so presets, picks and images start from it."""
        if not isinstance(camera, dict) or "position" not in camera:
            return
        c = self.scene.renderer.GetActiveCamera()
        c.SetPosition(*camera["position"])
        c.SetFocalPoint(*camera["focalPoint"])
        c.SetViewUp(*camera["viewUp"])
        if camera.get("parallelScale"):
            c.SetParallelScale(float(camera["parallelScale"]))
        if camera.get("viewAngle"):
            c.SetViewAngle(float(camera["viewAngle"]))
        self.scene.renderer.ResetCameraClippingRange()
        self.scene.framed = False
        self._pushed_camera = self._camera_key()

    def _case_resized(self, case_size: Any = None, **_: Any) -> None:
        """The shape of the view where the browser draws it: what Fit and the presets frame for. A resized view
        keeps its camera."""
        size = (case_size or {}).get("size") or {}
        w, h = float(size.get("width") or 0), float(size.get("height") or 0)
        if w <= 0 or h <= 0:
            return
        self.scene.aspect = w / h
        if not self._view_ready and self.scene.framed and self.scene.blocks:  # opened before a page said its shape
            self.scene.fit()
            self._update_view()

    def on_view_ready(self) -> None:
        """The page's case view drew its first scene, framed by itself: it takes this camera instead. From here
        the camera is the user's: a view resized keeps it."""
        self._view_ready = True
        self._pushed_camera = self._camera_key()
        self._view.push_camera()

    def on_zoom(self, factor: float, nx: float, ny: float) -> None:
        """A wheel or pinch over the server's image: toward the point under the cursor (nx, ny: 0..1 from
        the top left)."""
        self.scene.zoom_at(float(factor), float(nx), float(ny))
        self._update_view()

    def preset(self, name: str) -> None:
        if name in PRESETS:
            self.scene.view_preset(name)
            self.state.preset = name
            self.state.lock2d = self.scene.lock_2d
            self._update_view()

    def focus(self) -> None:
        block = self._block_of(self.state.selected)
        if block and self.scene.focus(block):
            self._update_view()

    def save_image(self, camera: Any = None) -> None:
        """The case view as a PNG file the user names, at the pixels it is drawn with (the view's size times the
        screen's pixel ratio), in the theme shown: the scene alone, without the page's labels and handles."""
        self.on_camera(camera)
        observed = self.state.case_size or {}
        size = observed.get("size") or {}
        ratio = float(observed.get("pixelRatio") or 1)
        pixels = (round(float(size.get("width") or 800) * ratio), round(float(size.get("height") or 600) * ratio))
        root = self.session.project.root

        def save(path: Path) -> None:
            path = path if path.suffix.lower() == ".png" else path.with_name(path.name + ".png")
            try:
                self.scene.render_png(path, pixels)
            except OSError as e:
                self.state.doc_error = f"{path.name}: {e}"
                return
            self.state.readout = f"saved {path} · {pixels[0]}×{pixels[1]} px"

        self.paths.choose("image", root, f"{root.name}-case.png", save)

    def fit(self, camera: Any = None) -> None:
        """Fit, from where the view looks: `camera` is the browser's own when it draws (it reaches this process
        a moment after a drag ends, and the F key can come sooner)."""
        self.on_camera(camera)
        self.scene.fit()
        self._update_view()

    def toggle_view(self, what: str) -> None:
        s = self.state
        if what == "lock2d":
            s.lock2d = not s.lock2d
            self.scene.set_lock_2d(s.lock2d)
            if s.lock2d and self.scene.dim == 2:
                s.preset = "-Z"  # turned back to the plane
        elif what == "parallel":
            s.parallel = not s.parallel
            self.scene.set_parallel(s.parallel)
        elif what == "clip":
            s.clip = not s.clip
            self.scene.set_clip(s.clip)
        elif what == "xray":
            s.xray = not s.xray
            self.scene.set_xray(s.xray, s.xray_opacity / 100)
        elif what == "pick":
            s.pick = not s.pick
        elif what == "mesh":
            s.mesh_edges = not s.mesh_edges
            self.scene.set_mesh_edges(s.mesh_edges)
        elif what == "flip":
            s.clip_flip = not s.clip_flip
        elif what == "mark":
            s.sec_mark = not s.sec_mark
            self.scene.section_mark = s.sec_mark
            self.scene.set_clip(s.clip)
        self._update_view()

    def set_clip_axis(self, axis: str) -> None:
        normal = {"x": ["1", "0", "0"], "y": ["0", "1", "0"], "z": ["0", "0", "1"]}[axis]
        self.state.clip_normal = normal
        if not self.state.clip:
            self.state.clip = True
            self.scene.set_clip(True)

    def _clip_fields(self, clip_origin: list[str], clip_normal: list[str], clip_flip: bool, **_: Any) -> None:
        try:
            origin = [float(v) for v in clip_origin]
            normal = [float(v) for v in clip_normal]
        except (TypeError, ValueError):
            return
        if not any(normal):
            return
        self.scene.set_clip(self.state.clip, origin, normal, clip_flip, snap=self.state.snap)
        self._update_view()

    def _clip_moved(self) -> None:
        with self.state:
            self.state.clip_origin = [_fmt(c) for c in self.scene.clip_origin]
            self.state.clip_normal = [_fmt(c) for c in self.scene.clip_normal]

    def _xray_opacity(self, xray_opacity: Any, **_: Any) -> None:
        try:
            value = float(xray_opacity) / 100
        except (TypeError, ValueError):
            return
        self.scene.set_xray(self.state.xray, value)
        self._update_view()

    def isolate(self) -> None:
        block = self._block_of(self.state.selected)
        if block:
            self.scene.isolate([block])
            self.state.blocks = [b.info() for b in self.scene.blocks.values() if b.kind != "mesh"]
            self._update_view()

    def show_all(self) -> None:
        self.scene.show_all()
        self.state.blocks = [b.info() for b in self.scene.blocks.values() if b.kind != "mesh"]
        self._update_view()

    def set_visible(self, name: str) -> None:
        if name in self.scene.blocks:
            self.scene.set_visible(name, not self.scene.blocks[name].visible)
            self.state.blocks = [b.info() for b in self.scene.blocks.values() if b.kind != "mesh"]
            self._update_view()

    def on_click(self, event: dict[str, Any]) -> None:
        if not self.state.pick or not event:
            return
        pos = event.get("position") or {}
        local = _local_pick(event)
        if local is not None:
            hit = self.scene.pick_world(local[0], local[1])
            plane = (local[0][0], local[0][1], local[0][2])
        elif event.get("mode") == "local":
            hit, plane = None, None  # the browser found nothing under the cursor
        else:
            hit = self.scene.pick(float(pos.get("x", 0)), float(pos.get("y", 0)))
            plane = self.scene.display_to_world(float(pos.get("x", 0)), float(pos.get("y", 0)))
        if self.setup.on_click(hit, plane if self.scene.dim == 2 else None):
            self._update_view()
            return
        if hit is None:
            self.state.tip = None
            return
        node = self._node_of_block(hit["name"])
        block = self.scene.blocks[hit["name"]]
        self.scene.select(hit["name"])
        if node:
            self.select(node, from_view=True)
        point = hit.get("point")
        coords = " ".join(f"{a} {_fmt(v)}" for a, v in zip("xyz"[: self.scene.dim], point or [], strict=False))
        what = "face" if block.kind == "zone" else "cell"
        self.state.readout = f"{coords} · {what} {hit['cell']}" if point else ""
        clipped = " · picked through the cut" if self.state.clip else ""
        detail = grouped(block.cells)
        at = point or plane
        # Placed by the page at the picked point (pins.js), so it stays on it; without a point, no label.
        self.state.tip = (
            at
            and {
                "p": [float(c) for c in at][:3],
                "text": f"{block.label} · {self._condition(block.label) or block.kind}",
                "sub": f"{detail} {'faces' if block.kind == 'zone' else 'cells'}{clipped}",
            }
        ) or None
        self._update_view()

    def on_hover(self, event: dict[str, Any]) -> None:
        if self.scene.dim != 2 or not event:
            return
        pos = event.get("position") or {}
        local = _local_pick(event)
        if local is not None:
            x, y = local[0][0], local[0][1]
        else:
            x, y, _ = self.scene.display_to_world(float(pos.get("x", 0)), float(pos.get("y", 0)))
        self.state.readout = f"x {_fmt(x)}  y {_fmt(y)}"

    def _condition(self, zone: str) -> str:
        return next((z["condition"] for z in self.session.zones() if z["zone"] == zone), "")

    def set_theme(self, dark: bool | str) -> None:
        theme = dark if isinstance(dark, str) else ("dark" if dark else "light")
        self.state.theme = theme
        self.scene.set_theme(theme)
        self.results_page.set_theme(theme)
        self.state.blocks = [b.info() for b in self.scene.blocks.values() if b.kind != "mesh"]
        self.setup.draw()  # the overlays' colors are the theme's too
        self._update_view()

    def mount_theme(self, system_dark: bool) -> None:
        self.set_theme(self.settings_page.initial_theme(system_dark))

    def system_theme(self, dark: bool) -> None:
        """The system's appearance changed (or Settings went back to following it)."""
        if self.settings_page.settings.theme == "system":
            self.set_theme(bool(dark))

    def toggle_theme(self) -> None:
        """The topbar button and View > Toggle: the other theme, kept in Settings."""
        self.settings_page.change("appearance", "theme", "light" if self.state.theme == "dark" else "dark")

    def screenshot(self) -> None:
        out = self.session.project.root / "case-view.png"
        self.scene.render_png(out)

    # Runs

    def set_page(self, page: str) -> None:
        self.state.page = page
        if page == "data":
            self.state.runs = self._runs_list()
            self.data_page.open()
        elif page == "remotes":
            self.remotes_page.open()
        elif page == "results":
            self.state.runs = self._runs_list()
            self.results_page.open()
        elif page == "compare":
            self.state.runs = self._runs_list()
            self.compare_page.open()
        elif page == "analysis":
            self.state.runs = self._runs_list()
            self.analysis_page.open()
        elif page == "settings":
            self.settings_page.open()
        elif page == "runs":
            self.state.runs = self._runs_list()
            if not self.state.run_sel and self.state.runs:
                self.select_run(self.state.runs[0]["name"])
        else:
            self._update_view()

    def _runs_list(self) -> list[dict[str, Any]]:
        out = []
        for r in reversed(self.session.project.runs()):
            active = self.session.active.get(r.name)
            status = "running" if active and active.state.active else (r.outcome or "–")
            out.append({"name": r.name, "status": status, "remote": r.home.get("remote", "local")})
        return out

    def start_run(self, restart_from: str = "", ranks: int = 1) -> None:
        if self.session.run_blocked or not self.document.guard_run(lambda: self.start_run(restart_from, ranks)):
            return
        try:
            run = self.session.start_run(restart_from, ranks)
        except (RestartError, KeyError, OSError) as e:
            self.state.run = {**self.state.run, "requested": str(e)}
            return
        self._follow(run)

    async def pick_restart(self, path: str = "") -> None:
        """The pre-launch view of a restart of the selected run: its restarts to pick from, and what a new run
        of the case from the picked one does with each key the case changed (`Session.restart_check`)."""
        name = self.state.run_sel
        try:
            rows = await asyncio.to_thread(self.session.restarts, name)
        except (KeyError, OSError) as e:
            self.state.restart_pick = {"run": name, "rows": [], "error": str(e)}
            return
        path = path or (rows[0]["path"] if rows else str(self.session.project.run(name).last_restart.get("path", "")))
        shown = [
            {
                "path": r["path"],
                "step": r.get("step"),
                "t": _fmt(r["t"]) if r.get("t") is not None else "–",
                "bytes": _bytes(r.get("bytes")),
            }
            for r in rows
        ]
        with self.state:
            self.state.restart_pick = {"run": name, "rows": shown, "path": path, "checking": True}
        try:
            result = await asyncio.to_thread(self.session.restart_check, name, path)
        except (RestartError, MallardError, KeyError, OSError) as e:
            result = {"path": path, "error": str(e), "known": False, "changes": []}
        if self.state.restart_pick.get("path") != path or self.state.run_sel != name:
            return
        changes = result["changes"]
        blocked = [c["key"] for c in changes if c.get("effect") == "incompatible"]
        with self.state:
            self.state.restart_pick = {
                "run": name,
                "rows": shown,
                "path": path,
                "checking": False,
                "error": result["error"],
                "known": result["known"],
                "changes": [
                    {k: ("–" if c.get(k) is None else str(c.get(k))) for k in ("key", "restart", "case", "effect")}
                    | {"detail": str(c.get("detail") or "")}
                    for c in changes
                ],
                "blocked": blocked,
                "can_launch": not result["error"] and not blocked,
            }

    def launch_restart(self) -> None:
        pick = self.state.restart_pick
        if not pick.get("can_launch"):
            return
        name, path = pick["run"], pick["path"]
        if self.session.run_blocked or not self.document.guard_run(self.launch_restart):
            return
        try:
            run = self.session.start_run(name, restart_path=path)
        except (RestartError, KeyError, OSError) as e:
            self.state.run = {**self.state.run, "requested": str(e)}
            return
        self.state.restart_pick = {}
        self._follow(run)

    def continue_run(self) -> None:
        try:
            run = self.session.continue_run(self.state.run_sel)
        except (RestartError, KeyError, OSError) as e:
            self.state.run = {**self.state.run, "requested": str(e)}
            return
        self._follow(run)

    def _follow(self, run: ActiveRun) -> None:
        self._run_name = run.record.name
        self.state.run = {**self.state.run, "requested": ""}
        self.state.page = "runs"
        self.state.run_sel = run.record.name
        self.state.runs = self._runs_list()
        self.state.plot_choice = ["", "", "", ""]
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def _poll_loop(self) -> None:
        while any(r.state.active for r in self.session.active.values()):
            for name in list(self.session.active):
                outputs = len(self.session.active[name].follower.state.outputs)
                st = await asyncio.to_thread(self.session.poll_run, name)
                with self.state:
                    self.health.observe(name, st, self.session.active[name].state.active)
                    if name == self.state.run_sel:
                        self._show_run(name, st)
                    self.analysis_page.observe(name, st)
                if len(st.outputs) != outputs:
                    self.results_page.outputs_changed(name)
            with self.state:
                self.state.runs = self._runs_list()
            await asyncio.sleep(POLL_INTERVAL)
        # One last read for the end record.
        for name in list(self.session.active):
            st = await asyncio.to_thread(self.session.poll_run, name)
            with self.state:
                self.health.observe(name, st, False)
                if name == self.state.run_sel:
                    self._show_run(name, st)
        with self.state:
            self.state.runs = self._runs_list()

    def select_run(self, name: str) -> None:
        self.state.run_sel = name
        self.state.plot_choice = ["", "", "", ""]
        self._show_selected()

    def _show_selected(self) -> None:
        name = self.state.run_sel
        if name in self.session.active:
            self._show_run(name, self.session.active[name].follower.state)
        else:
            record = self.session.project.run(name)
            self._show_run(name, self.session.open_run(record))

    def stop_run(self) -> None:
        if self.state.run_sel in self.session.active:
            self.session.stop_run(self.state.run_sel)
            self.state.run = {**self.state.run, "requested": "stop requested: STOP file written"}

    def checkpoint_run(self) -> None:
        if self.state.run_sel in self.session.active:
            self.session.checkpoint_run(self.state.run_sel)
            self.state.run = {**self.state.run, "requested": "checkpoint requested: CHECKPOINT file written"}

    def set_plot(self, index: int, source: str) -> None:
        choice = list(self.state.plot_choice)
        choice[int(index)] = source
        self.state.plot_choice = choice
        self._show_selected()

    def _show_run(self, name: str, st: RunState) -> None:
        s = self.state
        record = self.session.project.run(name)
        active = self.session.active.get(name)
        status = "running" if active and active.state.active and (st.end is None or not active.started) else st.status
        live = bool(active and active.state.active)
        last = st.last
        s.run = {
            "name": name,
            "status": status,
            "progress": round(100 * st.progress, 1),
            "active": live,
            **self.session.continue_state(record, live),
            "controls": self.session.run_controls(),
            "can_restart": not live and bool(record.last_restart.get("path")),
            "restart": str(record.last_restart.get("path", "")),
            "parts": st.starts,
            "requested": s.run.get("requested", "") if s.run.get("name") == name else "",
            "text": (
                f"t {_fmt(last.get('t', 0))} · {100 * st.progress:.1f} % · "
                f"{_fmt(1000 * (last.get('wall_per_step') or 0))} ms/step"
                + (f" · ETA {_duration(last.get('eta'))}" if status == "running" else "")
            )
            if last
            else ("waiting for the run log" if status == "running" else ""),
            "unsupported": st.unsupported,
        }
        start = st.start
        prov = ((start.get("provenance") or {}).get("run") or {}) if start else {}
        s.run_info = [
            {"k": "run_id", "v": (st.run_id or "–")[:8]},
            {"k": "mallard", "v": record.mallard or "–"},
            {"k": "input", "v": record.input_sha256[:8] or "–"},
            {"k": "cells", "v": grouped(last.get("cells", 0)) if last else "–"},
            {"k": "ranks", "v": f"{prov.get('ranks', 1)} · {prov.get('host') or ''}" if prov else "–"},
            {"k": "device", "v": str(prov.get("device") or "–")},
            {"k": "started", "v": str(prov.get("start_time") or "").replace("T", " ")[:16] or "–"},
            {"k": "restart of", "v": _restart_of(start)},
            {"k": "parts", "v": str(st.starts) if st.starts > 1 else "1"},
            {"k": "directory", "v": record.home.get("directory", "")},
            {"k": "job", "v": f"{record.home.get('remote', 'local')} · pid {record.job}" if record.job else "–"},
        ]
        tiles = [
            ("step", last.get("step"), ""),
            ("dt", last.get("dt"), st.unit("run", "dt") or "nd"),
            ("cfl", last.get("cfl"), ""),
            ("cells_per_second", last.get("cells_per_second"), "1/s"),
            ("memory_host", (last.get("memory_host") or 0) / 1e9 if last else None, "GB"),
        ]
        monitors = last.get("monitors") or {}
        tiles += [(k, v, st.unit("monitors", k) or "") for k, v in monitors.items() if isinstance(v, (int, float))]
        s.stats = [{"k": k, "v": _fmt(v) if v is not None else "–", "u": u} for k, v, u in tiles][:12]
        self.monitor.show(name, st, live)
        s.run_events = [
            {
                "step": "" if e.get("step") is None else str(e.get("step")),
                "kind": e.get("kind", ""),
                "text": e.get("text", ""),
            }
            for e in reversed(st.events[-40:])
        ]
        s.outputs = [
            {
                "step": o.get("step"),
                "t": _fmt(o["t"]) if isinstance(o.get("t"), (int, float)) else "",
                "path": o.get("path"),
                "bytes": _bytes(o.get("bytes")),
                "restart": o.get("kind") == "restart",
            }
            for o in reversed(st.outputs[-40:])
        ]
        try:
            s.lineage = self.session.lineage(name)
        except (KeyError, OSError):
            s.lineage = []
        if s.restart_pick.get("run") not in (None, name):
            s.restart_pick = {}
        directory = self.session.project.root / record.home.get("directory", "")
        s.run_log = _tail(directory / "stdout.txt")
        self.setup.refresh_live(name)
        self.health.show(name, st)

    # Layout

    def _layout(self) -> None:
        tokens = load_tokens()
        assets = resources.files("mallard_gui.assets")
        self.server.enable_module(
            {
                "serve": {"mallard_gui": str(assets)},
                "styles": ["mallard_gui/style.css", "mallard_gui/analysis.css"],
                "scripts": [
                    "mallard_gui/diagnostics.js",
                    "mallard_gui/native.js",
                    "mallard_gui/typing.js",
                    "mallard_gui/triad.js",
                    "mallard_gui/dropdown.js",
                    "mallard_gui/copy.js",
                    "mallard_gui/pins.js",
                    "mallard_gui/zoom.js",
                    "mallard_gui/document.js",
                    "mallard_gui/toml.js",
                    "mallard_gui/chart.js",
                    "mallard_gui/panes.js",
                ],
            }
        )
        with one_render(), DivLayout(self.server, style="height: 100%"):
            client.Style(css_variables(tokens))
            client.ClientTriggers(
                # The first frame is drawn in its theme: the startup screen (loading.py) fades out.
                mounted=f"trigger('mount_theme', [{SYSTEM_DARK}])"
                ".finally(() => window.mallardSplash && window.mallardSplash.done())",
                beforeMount="window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', "
                "e => trigger('system_theme', [e.matches]))",
            )
            client.ClientStateChange(
                value="theme_pref", change=f"theme_pref === 'system' && trigger('system_theme', [{SYSTEM_DARK}])"
            )
            client.ClientStateChange(value="theme", change="window.document.documentElement.dataset.theme = theme")
            client.ClientTriggers(
                mounted="window.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 'k') "
                "{ e.preventDefault(); window.document.querySelector('.search input').focus(); } })"
            )
            self.health.layout_client()
            self.document.layout_client()
            self.analysis_page.layout_client()
            self.monitor.layout_client()
            self.paths.layout_client()
            with html.Div(classes="shell"):
                self._topbar()
                with html.Div(classes="notices"):  # in the flow: a notice never covers the page's controls
                    self.document.layout()
                    self.watch.layout()
                with html.Div(classes=("'body ' + page",), style=(BODY_STYLE,)):
                    self._rail()
                    with html.Div(v_show="page === 'case'", style="display: contents"):
                        self._case_page()
                    with html.Div(v_if="page === 'runs'", style="display: contents"):
                        self._runs_page()
                    with html.Div(v_if="page === 'data'", style="display: contents"):
                        self.data_page.layout()
                    with html.Div(v_if="page === 'remotes'", style="display: contents"):
                        self.remotes_page.layout()
                    with html.Div(v_show="page === 'results'", style="display: contents"):
                        self.results_page.layout()
                    with html.Div(v_if="page === 'compare'", style="display: contents"):
                        self.compare_page.layout()
                    with html.Div(v_if="page === 'analysis'", style="display: contents"):
                        self.analysis_page.layout()
                    with html.Div(v_if="page === 'settings'", style="display: contents"):
                        self.settings_page.layout()
                self._statusbar()
                self.health.layout_toasts()
            self.setup.launch_dialog()
            self.inspector.fields_dialog()
            self.paths.layout()

    def _topbar(self) -> None:
        with html.Div(classes="topbar"):
            with html.Div(classes="topbar-left"):
                with html.Div(classes="brand"):
                    html.Img(src="mallard_gui/logo@2x.png", classes="brand-mark", alt="Mallard")
                    html.Span("{{ project_name }}", classes="proj")
                    with html.Span(classes="doc-name mono", title=("doc_info.path",)):
                        html.Span("{{ input_name }}")
                        html.Span("•", classes="doc-dot", v_if="doc_edited", title="Edited: not saved yet")
                html.Div(classes="topbar-sep")
                with html.Div(classes="crumbs"):
                    html.Span(
                        "{{ {case: 'Case', runs: 'Runs', data: 'Data', remotes: 'Remotes', results: 'Results', "
                        "compare: 'Compare', analysis: 'Analysis', settings: 'Settings'}[page] }}",
                        classes="page-name",
                    )
                    html.Span("/", classes="sep", v_if="page === 'case'")
                    html.Span(
                        "{{ (selected || '').split(':')[0] === 'section' ? (props.title || '') : "
                        "(tree.find(n => n.id === selected) || {}).label }}",
                        classes="last mono",
                        v_if="page === 'case'",
                    )
                    html.Span("/", classes="sep", v_if="page === 'runs' && run_sel")
                    html.Span("{{ run_sel }}", classes="last", v_if="page === 'runs'")
                    html.Span("/", classes="sep", v_if="page === 'results' && v_run")
                    html.Span("{{ v_run }}", classes="last", v_if="page === 'results'")
                    html.Span("/", classes="sep", v_if="page === 'remotes' && rm_sel")
                    html.Span("{{ rm_sel }}", classes="last", v_if="page === 'remotes'")
                    html.Span("/", classes="sep", v_if="page === 'compare' && c_a && c_b")
                    html.Span("{{ c_a }} vs {{ c_b }}", classes="last", v_if="page === 'compare' && c_a && c_b")
                    html.Span("/", classes="sep", v_if="page === 'analysis' && a_run")
                    html.Span("{{ a_run }}", classes="last", v_if="page === 'analysis'")
            with html.Div(classes="search"):
                html.Span(v_html="icons.search", classes="icon")
                html.Input(
                    v_model=("search",),
                    placeholder="Keys, sections, actions",
                    aria_label="Search keys and actions",
                    # trame's html elements drop key modifiers (keydown_enter): one handler reads the key.
                    keydown="if ($event.key === 'ArrowDown') { $event.preventDefault(); "
                    "palette_i = Math.min(palette_i + 1, search_results.length - 1) } "
                    "else if ($event.key === 'ArrowUp') { $event.preventDefault(); "
                    "palette_i = Math.max(palette_i - 1, 0) } "
                    "else if ($event.key === 'Enter') { "
                    "trigger('palette_pick', [(search_results[palette_i] || {}).id]) } "
                    "else if ($event.key === 'Escape') { search = ''; $event.target.blur() }",
                )
                html.Kbd("⌘ K")
                with html.Div(classes="search-results", v_if="search_results.length"):
                    with html.Div(
                        v_for="(h, i) in search_results",
                        key="h.id",
                        classes=("{on: i === palette_i}",),
                        click=(self.setup.palette_pick, "[h.id]"),
                    ):
                        html.Span("{{ h.label }}", classes=("h.kind === 'key' ? 'mono' : ''",))
                        html.Span("{{ h.detail || h.kind }}", classes="muted mono")
            with html.Div(classes="topbar-right"):
                with html.Span(classes="chip", title="Runs start on this machine; remotes are set up in Remotes"):
                    html.Span(classes="dot ok")
                    html.Span("local")
                html.Button(
                    v_html="icons.theme",
                    classes="icon-btn",
                    title="Light or dark theme",
                    click=self.toggle_theme,
                )

    def _rail(self) -> None:
        with html.Div(classes="rail"):
            for i, (page, label) in enumerate(PAGES, 1):
                if page == "remotes":
                    html.Div(classes="rail-sep")
                self._rail_button(page, label, f"⌘{i}")
            html.Div(classes="spacer")
            self._rail_button("settings", "Settings", "⌘,", style="margin-bottom: 8px")

    def _rail_button(self, page: str, label: str, key: str, **kwargs: Any) -> None:
        html.Button(
            v_html=f"icons.{page}",
            title=f"{label}  {key}",
            aria_label=label,
            classes=(f"page === '{page}' ? 'on' : ''",),
            click=(self.set_page, f"['{page}']"),
            **kwargs,
        )

    def _case_page(self) -> None:
        # The case tree, under the input reference's headings; the check's status beside its title
        with html.Div(classes="panel"):
            splitter("case-left", "the case tree", minimum=180, maximum=640)
            with html.Div(classes="panel-head"):
                html.Span("Case")
                self._check_status()
            with (
                html.Div(classes="scroll case-tree", role="tree", aria_label="Case tree"),
                html.Template(v_for="n in tree", key="n.id"),
            ):
                with (
                    html.Template(v_if="n.group_head"),
                    disclosure(
                        "!collapsed.includes('group:' + n.group)",
                        (self.toggle, "['group:' + n.group]"),
                        classes="tree-group",
                    ),
                ):
                    html.Span("{{ n.group }}", classes="glabel")
                with html.Div(
                    v_show="!collapsed.includes('group:' + n.group) && !(n.parent && collapsed.includes(n.parent))",
                    classes=(
                        "['tree-row', {sel: n.id === selected, child: n.depth > 0, "
                        "absent: n.depth === 0 && (!n.present || n.feature === 'off')}]",
                    ),
                    role="treeitem",
                    tabindex="0",
                    __properties=[("aria_expanded", "aria-expanded")],
                    aria_expanded=("n.children ? String(!collapsed.includes(n.id)) : null",),
                    click=(self.select, "[n.id]"),
                    keydown_enter=(self.select, "[n.id]"),
                ):
                    html.Span(
                        v_html="n.children ? icons.next : ''",
                        classes="chev",
                        title=("n.children ? (collapsed.includes(n.id) ? 'Expand' : 'Collapse') : null",),
                        __events=[("click_stop", "click.stop")],
                        click_stop=(self.toggle, "[n.id]"),
                    )
                    html.Span(v_if="n.depth === 0", v_html="icons[n.icon]", classes="ticon")
                    html.Span("{{ n.label }}", classes="tlabel")
                    html.Span("{{ n.errors }}", classes="badge e", v_if="n.errors", title="errors")
                    html.Span("{{ n.warnings }}", classes="badge w", v_if="n.warnings && !n.errors", title="warnings")
                    html.Span(
                        "{{ n.feature === 'off' ? 'off' : n.present || n.value === 'none' ? "
                        "n.value || (n.feature ? 'on' : '') : 'not set' }}",
                        classes=("['tval', {bad: n.value === 'no condition', fon: n.feature === 'on'}]",),
                    )
                    with html.Span(classes="tend"):
                        html.Button(
                            v_html="icons.plus",
                            v_if="n.adds && n.adds.length",
                            classes="icon-btn tadd",
                            title=("'Add to ' + n.label",),
                            aria_label=("'Add to ' + n.label",),
                            __events=[("click_stop", "click.stop")],
                            click_stop=self.inspector._open_menu("n.adds"),
                        )
        # Center
        with html.Div(classes="center"):
            with html.Div(classes="tabs", role="tablist"):
                for key, label in (("view", "Case view"), ("toml", "{{ input_name }}"), ("report", "Check report")):
                    html.Button(
                        label, classes=(f"tab === '{key}' ? 'tab on' : 'tab'",), role="tab", click=f"tab = '{key}'"
                    )
                self.case_files.tab_button()
                self.analysis.tab_button()
            with html.Div(v_show="tab === 'view'", classes="center", style="flex: 1; min-height: 0"):
                self._view_toolbar()
                with html.Div(classes="viewport", mouseleave="tip = null"):
                    view = vtkw.VtkRemoteLocalView(
                        self.scene.window,
                        namespace="caseview",
                        ref="caseview",
                        mode=("caseview_mode", "local"),
                        disable_auto_switch=True,
                        # Images (the fallback) at the screen's pixel density, so lines stay one pixel.
                        still_ratio=2,
                        interactive_ratio=1,
                        still_quality=95,
                        interactive_quality=70,
                        interactor_settings=("lock2d ? caseview_pan : caseview_orbit",),
                        interactor_events=("view_events", ["EndAnimation"]),
                        EndAnimation=(self.on_camera, "[trame.refs.caseview.getCamera()]"),
                        picking_modes=("(pick ? ['click'] : []).concat(dim === 2 ? ['hover'] : [])",),
                        click=(self.on_click, "[$event]"),
                        hover=(self.on_hover, "[$event]"),
                        style="position: absolute; inset: 0;",
                    )
                    self._view = view
                    view.push_remote_camera_on_end_interaction()  # an orbit rendered here: pins follow it
                    self.ctrl.view_update = self._update_view
                    self.ctrl.view_reset = view.reset_camera
                    with html.Div(classes="legend", v_if="blocks.length"):
                        with html.Div(v_for="b in blocks", key="b.name"):
                            html.Span(classes="swatch", style=("{background: b.color, opacity: b.visible ? 1 : .3}",))
                            html.Span("{{ b.label }}")
                            html.Span(
                                "{{ b.kind === 'zone' ? (tree.find(n => n.id === 'zone:' + b.name) || {}).value"
                                " : b.kind }}",
                                classes="muted",
                            )
                    html.Div(
                        classes="triad",
                        v_if="blocks.length",
                        __properties=[("data_view", "data-view")],
                        data_view="caseview",
                    )
                    section_bar(
                        "",
                        self.set_clip_axis,
                        (self.toggle_view, "['flip']"),
                        self.set_section_offset,
                        (self.toggle_view, "['mark']"),
                        (self.toggle_view, "['clip']"),
                        shown="clip",
                    )
                    # The view's size for this process (Fit and the presets frame for its shape): an empty box over it.
                    client.SizeObserver("case_size", classes="view-size")
                    self.setup.view_overlays()
                    # Labels and drag handles over the view, placed by pins.js every frame.
                    with html.Div(classes="case-pins"):
                        # The picked zone's label, at the picked point.
                        with html.Div(
                            v_if="tip && tip.p",
                            classes="case-pin tip",
                            __properties=[("data_p", "data-p")],
                            data_p=("JSON.stringify(tip.p)",),
                        ):
                            html.Div("{{ tip.text }}")
                            html.Div("{{ tip.sub }}", classes="muted", style="color: var(--m-text-muted)")
                        html.Div(
                            "{{ pin.kind === 'label' ? pin.text : '' }}",
                            v_for="pin in case_pins",
                            key="pin.kind + pin.i",
                            classes=("'case-pin ' + pin.kind",),
                            title=("pin.kind === 'handle' ? 'drag to set ' + pin.text : pin.text",),
                            __properties=[("data_p", "data-p"), ("data_i", "data-i"), ("data_kind", "data-kind")],
                            data_p=("JSON.stringify(pin.p)",),
                            data_i=("pin.i",),
                            data_kind=("pin.kind",),
                        )
                    with html.Div(classes="readout"):
                        html.Div(
                            "drag {{ lock2d ? 'pan' : 'orbit' }} · shift pan · wheel zoom · F fit · ⇧F frame",
                            style="font-family: var(--m-font-sans)",
                        )
                        html.Div("{{ readout }}")
                    self.analysis.panel()
                    html.Div(
                        "{{ check.running ? 'mallard check ...' : 'no case view' }}",
                        classes="empty",
                        v_if="!blocks.length",
                    )
            with html.Div(v_if="tab === 'toml'", classes="textview"):
                with html.Div(classes="toml-editor"):
                    html.Div(classes="toml-under", aria_hidden="true")
                    html.Textarea(v_model=("toml_text",), spellcheck="false", wrap="off", aria_label="input.toml")
                with html.Div(classes="textbar"):
                    html.Span("{{ toml_error }}", style="color: var(--m-danger)", classes="mono grow")
                    html.Span("edits keep comments and layout", classes="muted", v_if="!toml_error")
                    html.Button("Revert", classes="btn", click=self.revert_toml)
                    html.Button("Apply", classes="btn primary", click=(self.apply_toml, "[toml_text]"))
            with html.Div(v_if="tab === 'report'", classes="textview"):
                message("check_error", "notice check-error", role="alert", v_if="check_error")
                self.setup.report_view()
            self.case_files.panel()
            self._problems()
        self._properties()

    def _view_toolbar(self) -> None:
        with html.Div(classes="toolbar"):
            for name in ("+X", "-X", "+Y", "-Y", "+Z", "-Z", "iso"):
                html.Button(
                    name.replace("-", "−").replace("iso", "Iso"),
                    classes=(f"preset === '{name}' ? 'tb mono on' : 'tb mono'",),
                    click=(self.preset, f"['{name}']"),
                    title=f"View {name}",
                )
            html.Div(classes="tb-gap")
            html.Button(
                "Ortho",
                classes=("parallel ? 'tb on' : 'tb'",),
                click=(self.toggle_view, "['parallel']"),
                title="Orthographic projection (else perspective)",
            )
            html.Button(
                "2D lock",
                classes=("lock2d ? 'tb on' : 'tb'",),
                click=(self.toggle_view, "['lock2d']"),
                title="Lock rotation to the plane",
            )
            html.Div(classes="tb-gap")
            with html.Button(
                classes=("clip ? 'tb on' : 'tb'",), click=(self.toggle_view, "['clip']"), title="Clip plane"
            ):
                html.Span(v_html="icons.clip")
                html.Span("Clip")
            with html.Button(
                classes=("xray ? 'tb on' : 'tb'",),
                click=(self.toggle_view, "['xray']"),
                title="Draw the other zones translucent",
            ):
                html.Span(v_html="icons.xray")
                html.Span("X-ray")
            with html.Button(classes="tb", click=self.isolate, title="Show only the selected zone"):
                html.Span(v_html="icons.isolate")
                html.Span("Isolate")
            with html.Button(classes="tb", click=self.show_all, title="Show every zone"):
                html.Span(v_html="icons.eye")
                html.Span("All")
            with html.Button(
                classes=("pick ? 'tb on' : 'tb'",), click=(self.toggle_view, "['pick']"), title="Click to pick a zone"
            ):
                html.Span(v_html="icons.pick")
                html.Span("Pick")
            self.setup.inflow_button()
            html.Div(classes="grow")
            html.Button(
                v_html="icons.mesh",
                classes=("mesh_edges ? 'icon-btn on' : 'icon-btn'",),
                click=(self.toggle_view, "['mesh']"),
                title="Show mesh: every face's edges (feature edges are always drawn)",
            )
            html.Button(
                v_html="icons.frame",
                classes="icon-btn",
                click=self.focus,
                title="Frame the selected zone  ⇧F",
                aria_label="Frame the selected zone",
                disabled=("!blocks.length",),
            )
            html.Button(
                v_html="icons.camera",
                classes="icon-btn",
                click=(self.save_image, "[window.mallardZoom.camera('caseview')]"),
                title="Save the view as a PNG image",
                disabled=("!blocks.length",),
            )
            html.Button(
                v_html="icons.fit",
                classes="icon-btn",
                click=(self.fit, "[window.mallardZoom.camera('caseview')]"),
                title="Fit the whole case to the view  F",
                aria_label="Fit the whole case to the view",
            )

    def _problems(self) -> None:
        client.ClientStateChange(
            value="report_focus", change="report_focus >= 0 && window.mallardCopy.reveal('.su-diag.focus')"
        )
        with html.Div(classes="problems"):
            splitter("case-problems", "the problems", axis="y", edge="start", minimum=60, maximum=600, keep=160)
            with html.Div(classes="panel-head"):
                html.Span("Problems")
                html.Span("{{ check.running ? 'checking ...' : 'mallard check on change' }}", classes="mono")
            with html.Div(classes="scroll", __properties=[("data_copy_list", "data-copy-list")], data_copy_list=""):
                with html.Div(
                    v_for="(p, i) in problems",
                    key="i",
                    classes=("['prob', p.severity, {sel: prob_sel.includes(i)}]",),
                    tabindex="0",
                    __properties=COPY_PROPS,
                    __events=[("keydown_enter", "keydown.enter.self")],
                    data_copy=("p.copy",),
                    click="prob_sel = window.mallardCopy.pick(prob_sel, i, $event); trigger('goto_problem', [p.node])",
                    dblclick=(self.problem_details, "[i]"),
                    keydown_enter=(self.problem_details, "[i]"),
                    title=("p.message + '  (double-click: details in the check report)'",),
                ):
                    html.Span(v_html="icons[p.severity === 'error' ? 'error' : 'warning']", classes="sev")
                    html.Span("{{ p.code }}", classes="code")
                    html.Span("{{ p.key }}", classes="key")
                    with html.Span(classes="msg"):
                        html.Span("{{ p.message }}")
                        self.setup.problem_fix()
                    html.Button(
                        "{{ p.line }}",
                        classes="line linkbtn",
                        title="Show in the text",
                        disabled=("!p.lineno",),
                        __events=[("click_stop", "click.stop")],
                        click_stop=(self.document.goto_line, "[p.lineno, p.column]"),
                    )
                    copy_button()
                with html.Div(classes="prob ok", v_if="!problems.length && check.status"):
                    html.Span(v_html="icons.ok", classes="sev")
                    html.Span("ok", classes="code")
                    html.Span("", classes="key")
                    html.Span("mallard check found nothing to report", classes="msg")
                    html.Span("", classes="line")

    def _properties(self) -> None:
        with html.Div(classes="panel right"):
            splitter("case-right", "the properties", edge="start", minimum=340, maximum=760)
            # The zone list, in sync with the view (3D: the section tool's companion).
            with html.Div(
                v_if="dim === 3 && blocks.length", style=list_style("case-zones", "40%") + "; overflow: auto"
            ):
                with html.Div(classes="panel-head"):
                    html.Span("Zones")
                    html.Span("in sync with the view", classes="mono")
                with html.Div(
                    v_for="b in blocks.filter(b => b.kind === 'zone')",
                    key="b.name",
                    classes=("['tree-row', 'child', {sel: selected === 'zone:' + b.name}]",),
                    style="padding-left: 8px",
                    click=(self.select, "['zone:' + b.name]"),
                ):
                    html.Span(classes="swatch", style=("{background: b.color}",))
                    html.Span("{{ b.label }}", classes="tlabel")
                    html.Span(
                        "{{ (tree.find(n => n.id === 'zone:' + b.name) || {}).value }}",
                        classes="tval",
                        style="max-width: 35%; font-family: var(--m-font-sans)",
                    )
                    html.Span("{{ b.cells }}", classes="tval", style="margin-left: 8px")
                    html.Button(
                        v_html="icons.eye",
                        classes="icon-btn",
                        title="Show or hide",
                        style=("{opacity: b.visible ? 1 : .35}",),
                        __events=[("click_stop", "click.stop")],
                        click_stop=(self.set_visible, "[b.name]"),
                    )
            list_splitter("case-zones", "the zone list", v_if="dim === 3 && blocks.length")
            with html.Div(classes="panel-head"):
                html.Span(
                    "{{ (selected || '').split(':')[0] === 'zone' ? 'Boundary' : "
                    "(selected || '').split(':')[0] === 'region' ? 'Region' : 'Properties' }}"
                )
                html.Span("{{ props.subtitle }}", classes="mono")
            with html.Div(classes="prop-title"):
                html.Span("{{ props.title }}", classes="name")
                html.Span("{{ props.path ? 'line ' + props.line : '' }}", classes="tag", v_if="props.line")
            self.setup.actions_bar()
            with html.Div(classes="scroll"):
                html.Div(
                    "{{ props.edit_error }}",
                    v_if="props.edit_error",
                    classes="prop-foot",
                    style="color: var(--m-danger)",
                )
                self.inspector.layout()
                html.Div(
                    "not drawn: {{ skipped.join(', ') }} (volumes; surfaces need #1313)",
                    classes="prop-foot",
                    v_if="skipped.length",
                )
            self.inspector.key_help()
            with html.Div(classes="actions"):
                html.Button(v_html="icons.toml", classes="icon-btn", title="input.toml", click="tab = 'toml'")
                html.Div(classes="grow")
                with html.Button(
                    classes="btn checkbtn",
                    click=self.check_and_show,
                    disabled=("check.running",),
                    title="Check the case, then show its report",
                ):
                    html.Span(v_html="icons.check")
                    html.Span("Check")
                with html.Button(
                    classes="btn primary runbtn",
                    click=self.setup.open_launch,
                    disabled=("!!run_blocked",),
                    title=(
                        "run_blocked ? 'Run is blocked: ' + run_blocked : 'Run locally: ' + "
                        "(su_summary.run_label || 'machine and cost in the next step')",
                    ),
                ):
                    html.Span(v_html="icons.play")
                    html.Span(
                        "{{ run_blocked ? 'Run (' + run_blocked + ')' : "
                        "'Run' + (su_summary.run_label ? ' · ' + su_summary.run_label : '') }}"
                    )

    def _prop_rows(self) -> None:
        """A group's rows: the key (a click shows its line in input.toml), its value (unit-aware), and where the
        value comes from with Reset; unset keys show their default (or the preset's value) as a placeholder."""
        with html.Div(
            v_for="r in g.rows",
            key="r.path + r.label",
            classes=(
                "['prow', r.severity, {set: r.set && !r.readonly && !!r.label, unset: !r.set && !r.readonly, "
                "missing: r.required && !r.set && !r.readonly, odim: !!r.other_dim, link: !r.label, "
                "comp: r.widget === 'composition', oneofrow: r.widget === 'oneof', nodefault: !!r.unset}]",
            ),
            mouseenter="key_help = r.help",
            focusin="key_help = r.help",
        ):
            html.Div(
                "{{ r.label.replaceAll('_', '_\u200b') }}",
                classes=("['plabel', {req: r.required}]",),
                title=("r.path + (r.help && r.help.type ? '  (' + r.help.type + ')' : '')",),
                v_if="r.label",
                click=(self.inspector.reveal, "[r.path]"),
            )
            with html.Div(classes="pval", style=("r.label ? '' : 'grid-column: 1 / 3'",)):
                html.Div(
                    "{{ r.value }}",
                    classes=("['ro', {link: !r.label}]",),
                    v_if="r.readonly",
                    title=("r.value",),
                    click=(self._goto_path, "[r.path]"),
                )
                dropdown(
                    "r.value",
                    "r.options.map(o => r.models[o] ? {value: o, label: o, note: r.models[o].cite} : o)",
                    (self.set_key, "[r.path, $event.detail]"),
                    ("r.path",),
                    placeholder=(
                        "r.unset || !r.placeholder ? r.placeholder"
                        " : r.placeholder + (r.origin ? ' (preset)' : ' (default)')"
                    ),
                    free="r.free",
                    v_if="!r.readonly && r.widget === 'select'",
                )
                self.inspector.model_button()
                dropdown(
                    "''",
                    "r.options",
                    (self.assign_zone, "[r.path.slice(5), $event.detail]"),
                    ("r.path",),
                    placeholder="'choose a condition'",
                    v_if="r.widget === 'assign'",
                )
                with html.Label(
                    v_if="!r.readonly && r.widget === 'bool'",
                    classes=("['pbool', {dim: !r.set}]",),
                    title=("r.set ? r.value : (r.placeholder || 'false') + (r.origin ? ' (preset)' : ' (default)')",),
                ):
                    html.Input(
                        type="checkbox",
                        aria_label=("r.path",),
                        checked=("(r.set ? r.value : r.placeholder) === 'true'",),
                        change=(self.set_key, "[r.path, String($event.target.checked)]"),
                    )
                    html.Span(
                        "{{ (r.set ? r.value : r.placeholder) === 'true' ? 'true' : 'false' }}"
                        "{{ r.set ? '' : (r.origin ? ' (preset)' : ' (default)') }}"
                    )
                self.setup.row_widgets()
                html.Input(
                    v_if=(
                        "!r.readonly && !['select', 'assign', 'quantity', 'bool', 'composition', 'oneof', 'fields', "
                        "'vector', 'points'].includes(r.widget)"
                    ),
                    value=("r.value",),
                    placeholder=("r.placeholder",),
                    title=("r.value || r.placeholder",),
                    aria_label=("r.path",),
                    spellcheck="false",
                    change=(self.set_key, "[r.path, $event.target.value]"),
                    keydown_enter="$event.target.blur()",
                )
                html.Span(
                    "{{ r.unit }}",
                    classes="unit",
                    v_if="r.unit && !r.readonly && !['quantity', 'pick', 'composition', 'fields'].includes(r.widget)",
                    title=("r.unit + ': the unit Mallard reads this number in'",),
                )
                html.Span(
                    "unit?",
                    classes="unit unknown",
                    v_if="r.unit_unknown && !r.readonly",
                    title=("'Unit not known from the key alone: ' + r.unit_unknown + '. Write it in SI.'",),
                )
                self.inspector.vector_cell()
                html.Span(
                    "{{ r.points.rows.length ? r.points.rows.length + (r.points.rows.length === 1 ? ' point' : "
                    "' points') : 'none' }}",
                    classes="muted",
                    v_if="r.widget === 'points'",
                )
                dropdown(
                    "r.points.unit",
                    "r.points.units",
                    (self.inspector.show_unit, "[r.path, $event.detail]"),
                    ("r.path + ' unit'",),
                    classes="qunit",
                    v_if="!r.readonly && r.widget === 'points' && r.points.units && r.points.units.length",
                )
                self.inspector.file_button()
                self.inspector.composition_head()
                self.inspector.fields_cell()
            with html.Div(classes="pext"):
                self.inspector.row_extras()
            self.inspector.composition_body()
            self.inspector.oneof_body()
            self.inspector.vector_error()
            self.inspector.points_body()
            message("r.error", "perr", v_if="r.error")

    def _check_status(self) -> None:
        with html.Button(
            classes=(
                "['check-status', check.running ? 'busy' : check.status === 'failed' || check.errors ? 'err' : "
                "check.warnings ? 'warn' : check.status ? 'ok' : '']",
            ),
            title=("check.status ? 'Check report: ' + check.status + ' at ' + check.ago : 'Check report'",),
            click="tab = 'report'",
        ):
            html.Span(
                v_html="icons[check.status === 'failed' || check.errors ? 'error' : check.warnings ? 'warning' : 'ok']",
                v_if="check.status && !check.running",
            )
            html.Span(
                "{{ check.running ? 'checking…' : check.status === 'failed' ? 'check failed' : "
                "check.errors ? check.errors + (check.errors === 1 ? ' error' : ' errors') : "
                "check.warnings ? check.warnings + (check.warnings === 1 ? ' warning' : ' warnings') : "
                "check.status ? 'check ok' : 'not checked' }}"
            )

    def _goto_path(self, path: str) -> None:
        if not path:
            return
        if path.startswith(("zone:", "region:")):
            self.select(path)
            return
        try:
            seg = self.session.doc.resolve(path)
        except (KeyError, ValueError):
            return
        if len(seg) > 1 and isinstance(seg[1], int):
            self.select(f"el:{seg[0]}[{seg[1]}]")
        else:
            self.select(f"section:{self.session.section_of(str(seg[0]))}")

    def _runs_page(self) -> None:
        with html.Div(classes="panel"):
            splitter("runs-left", "the run list", minimum=180, maximum=640)
            with html.Div(classes="panel-head"):
                html.Span("Runs")
            with html.Div(classes="scroll", style=list_style("runs-list", "45%")):
                with html.Div(classes="tree-row", style="padding-left: 6px"):
                    html.Span(v_html="icons.local", classes="ticon")
                    html.Span("Local", classes="tlabel")
                with html.Div(
                    v_for="r in runs",
                    key="r.name",
                    classes=("['run-row', {sel: r.name === run_sel}]",),
                    click=(self.select_run, "[r.name]"),
                ):
                    html.Span(
                        "{{ r.status === 'running' ? '▷' : r.status === 'finished' ? '✓' : "
                        "r.status === 'stopped' ? '□' : '·' }}"
                    )
                    html.Span("{{ r.name }}")
                    html.Span("{{ r.status }}", classes="rv")
                html.Div("No runs yet", classes="notice", v_if="!runs.length")
            list_splitter("runs-list", "the run list")
            with html.Div(classes="panel-head", v_if="run_sel"):
                html.Span("Run")
                html.Span("{{ run_sel }}", classes="mono")
            with html.Div(classes="scroll", v_if="run_sel"):
                with html.Div(v_for="i in run_info", key="i.k", classes="prow"):
                    html.Div("{{ i.k }}", classes="plabel")
                    html.Div("{{ i.v }}", classes="pval ro")
        with html.Div(classes="center runs-center"):
            with html.Div(classes="run-head", v_if="run_sel"):
                html.Span("{{ run.name }}", classes="name")
                html.Span("{{ run.status }}", classes=("['pill', run.status]",))
                html.Span("{{ run.requested }}", classes="muted requested", title=("run.requested",))
                html.Div(classes="grow")
                with html.Button(
                    classes="btn",
                    click=self.continue_run,
                    disabled=("!run.can_continue",),
                    title=(
                        "run.can_continue ? 'Continue in this run directory from ' + run.restart + "
                        "' (its SHA-256 checked first)' : 'Cannot continue: ' + (run.continue_why || []).join('; ')",
                    ),
                ):
                    html.Span(v_html="icons.play")
                    html.Span("Continue")
                with html.Button(
                    classes="btn",
                    click=self.pick_restart,
                    disabled=("!run.can_restart || !!run_blocked",),
                    title=(
                        "'A new run of the case input, initialized from one of this run\\'s restarts, checked "
                        "against the case first'",
                    ),
                ):
                    html.Span(v_html="icons.restart")
                    html.Span("Restart from")
                with html.Button(
                    classes="btn",
                    click=self.checkpoint_run,
                    disabled=("!run.active || !(run.controls || {}).checkpoint",),
                    title="Write a CHECKPOINT control file",
                ):
                    html.Span(v_html="icons.bookmark")
                    html.Span("Checkpoint")
                with html.Button(
                    classes="btn danger",
                    click=self.stop_run,
                    disabled=("!run.active || !(run.controls || {}).stop",),
                    title="Write a STOP control file: a restart and the final output, then exit",
                ):
                    html.Span(v_html="icons.stop")
                    html.Span("Stop")
            with html.Div(classes="progress", v_if="run_sel"):
                html.Div(style=("{width: (run.progress || 0) + '%'}",))
            html.Div("{{ run.text }}", classes="progress-text", v_if="run_sel")
            html.Div("{{ run.unsupported }}", classes="notice", v_if="run.unsupported")
            html.Div(
                "This Mallard cannot continue from {{ run.restart }}: {{ (run.continue_refused || []).join('; ') }}",
                classes="notice continue-why",
                v_if="run_sel && (run.continue_refused || []).length",
            )
            self._restart_pick_layout()
            self.health.layout_monitor()
            with html.Div(classes="stats", v_if="run_sel"):
                with html.Div(v_for="t in stats", key="t.k", classes="stat"):
                    html.Div("{{ t.k }}", classes="k")
                    with html.Div(classes="v"):
                        html.Span("{{ t.v }}")
                        html.Small("{{ t.u }}")
            with html.Div(classes="plots runs-plots", v_if="run_sel"):
                splitter("runs-plots", "the plots", axis="y", minimum=160, maximum=1600, keep=80)
                for i in range(4):
                    with html.Div(classes="plot"):
                        with html.Div(classes="plot-head"):
                            html.Span(classes="grow")
                            self.exports.buttons(f"monitor{i}")
                            with html.Select(
                                aria_label=f"plot {i + 1}", change=(self.set_plot, f"[{i}, $event.target.value]")
                            ):
                                html.Option(
                                    "{{ p.label }}",
                                    v_for="p in plot_sources",
                                    key="p.id",
                                    value=("p.id",),
                                    selected=(f"p.id === plot_choice[{i}]",),
                                )
                        html.Div(classes="m-chart", id=KEYS[i])
            with html.Div(classes="panel-head", v_if="run_sel"):
                html.Span("Events")
                html.Span("mallard.jsonl", classes="mono")
            with html.Div(classes="scroll", v_if="run_sel"):
                with html.Div(v_for="(e, i) in run_events", key="i", classes="events-row"):
                    html.Span("{{ e.step }}", classes="muted")
                    html.Span("{{ e.kind }}", classes="ekind")
                    html.Span("{{ e.text }}", classes="etext")
            empty_state(
                "runs",
                "No runs yet",
                "Runs of this case appear here as they start, with their progress, health and plots. "
                "Check the case, then Run launches it.",
                "!run_sel && !runs.length",
                ("Go to Case setup", "case", (self.set_page, "['case']")),
            )
            empty_state(
                "runs",
                "No run selected",
                "Pick a run on the left to follow its progress, health alerts and monitor plots.",
                "!run_sel && runs.length",
            )
        with html.Div(classes="panel right"):
            splitter("runs-right", "the log", edge="start", minimum=240, maximum=900)
            empty_state(
                "terminal",
                "Log, control and outputs",
                "The selected run's log, its live settings and the files it wrote.",
                "!run_sel",
                compact=True,
            )
            with html.Div(classes="tabs", v_if="run_sel"):
                for key, label in (("log", "Log"), ("control", "Control"), ("outputs", "Outputs")):
                    html.Button(label, classes=(f"run_tab === '{key}' ? 'tab on' : 'tab'",), click=f"run_tab = '{key}'")
            html.Pre("{{ run_log }}", classes="log", v_if="run_sel && run_tab === 'log' && run_log")
            empty_state(
                "terminal",
                "No console output",
                "This run wrote nothing to stdout.txt in its directory; its records are in mallard.jsonl.",
                "run_sel && run_tab === 'log' && !run_log",
                compact=True,
            )
            with html.Div(v_if="run_sel", style="display: contents"):
                self.setup.control_tab()
            empty_state(
                "download",
                "No outputs yet",
                "Snapshots and restarts appear here as the run writes them.",
                "run_sel && run_tab === 'outputs' && !outputs.length",
                compact=True,
            )
            with html.Div(classes="lineage", v_if="run_sel && run_tab === 'outputs' && lineage.length"):
                html.Span("Restart of", classes="muted")
                with html.Span(v_for="(l, i) in lineage", key="i", classes="lineage-link"):
                    html.Span("{{ l.file.split('/').pop() }} · step {{ l.step }}")
                    html.Span("{{ l.run ? ' of ' + l.run : ' of a run not in this project' }}", classes="muted")
                    html.Span(" ← ", v_if="i < lineage.length - 1", classes="muted")
            with html.Div(classes="scroll", v_if="run_sel && run_tab === 'outputs' && outputs.length"):
                with html.Div(
                    v_for="(o, i) in outputs",
                    key="i",
                    classes="events-row",
                    style="grid-template-columns: 44px 56px 1fr 52px 24px",
                ):
                    html.Span("{{ o.step }}", classes="muted")
                    html.Span("{{ o.t }}", classes="muted", title="t")
                    html.Span("{{ (o.path || '').split('/').pop() }}", classes="etext", title=("o.path",))
                    html.Span("{{ o.bytes }}", classes="muted", style="text-align: right")
                    html.Button(
                        v_if="o.restart",
                        v_html="icons.restart",
                        classes="icon-btn",
                        disabled=("run.active || !!run_blocked",),
                        click=(self.pick_restart, "[o.path]"),
                        aria_label=("'Restart from ' + o.path",),
                        title=("'Restart from ' + o.path + ': a new run of the case input, checked against it first'",),
                    )
                    html.Span(v_if="!o.restart")

    def _restart_pick_layout(self) -> None:
        """The restart to start from and Mallard's answer for each key the case changed since it was written;
        launch stays off while any change is incompatible."""
        with html.Div(classes="restart-pick", v_if="run_sel && restart_pick.run === run_sel"):
            with html.Div(classes="restart-pick-head"):
                html.Span("Restart from {{ restart_pick.run }}", classes="name")
                html.Div(classes="grow")
                html.Button("Cancel", classes="btn", click="restart_pick = {}")
                html.Button(
                    "Launch from {{ (restart_pick.path || '').split('/').pop() }}",
                    classes="btn primary",
                    disabled=("!restart_pick.can_launch || restart_pick.checking || !!run_blocked",),
                    click=self.launch_restart,
                    title=(
                        "restart_pick.blocked && restart_pick.blocked.length ? 'The restart cannot start a run of "
                        "the case as it is: ' + restart_pick.blocked.join(', ') : 'A new run of the case from it'",
                    ),
                )
            with html.Div(classes="restart-rows", role="listbox", aria_label="Restarts"):
                with html.Div(
                    v_for="r in restart_pick.rows",
                    key="r.path",
                    role="option",
                    aria_selected=("String(r.path === restart_pick.path)",),
                    __properties=[("aria_selected", "aria-selected")],
                    classes=("r.path === restart_pick.path ? 'events-row on' : 'events-row'",),
                    style="grid-template-columns: 60px 80px 1fr 60px; cursor: pointer",
                    click=(self.pick_restart, "[r.path]"),
                ):
                    html.Span("{{ r.step }}", classes="muted")
                    html.Span("t {{ r.t }}", classes="muted")
                    html.Span("{{ r.path }}", classes="etext")
                    html.Span("{{ r.bytes }}", classes="muted", style="text-align: right")
            html.Div("Checking the case against it…", classes="muted", v_if="restart_pick.checking")
            html.Div("{{ restart_pick.error }}", classes="notice", v_if="!restart_pick.checking && restart_pick.error")
            html.Div(
                "This Mallard does not say what a restart does with keys the case changed; "
                "check them against the run's input before launching.",
                classes="notice",
                v_if="!restart_pick.checking && !restart_pick.error && !restart_pick.known",
            )
            html.Div(
                "Launch is off: the case changes {{ restart_pick.blocked.join(', ') }} in a way this restart "
                "cannot continue (below).",
                classes="notice",
                v_if="!restart_pick.checking && (restart_pick.blocked || []).length",
            )
            html.Div(
                "The case changes no key from the input this restart was written with.",
                classes="muted",
                v_if="!restart_pick.checking && restart_pick.known && !(restart_pick.changes || []).length",
            )
            with html.Div(
                classes="restart-changes",
                v_if="!restart_pick.checking && (restart_pick.changes || []).length",
            ):
                with html.Div(classes="events-row head", style="grid-template-columns: 1.4fr 1fr 1fr 112px 2fr"):
                    for label in ("key", "in the restart", "in the case", "effect", ""):
                        html.Span(label, classes="muted")
                with html.Div(
                    v_for="c in restart_pick.changes",
                    key="c.key",
                    classes=("'events-row change ' + c.effect",),
                    style="grid-template-columns: 1.4fr 1fr 1fr 112px 2fr",
                ):
                    html.Code("{{ c.key }}")
                    html.Code("{{ c.restart }}")
                    html.Code("{{ c.case }}")
                    html.Span("{{ c.effect }}", classes=("['pill', c.effect]",))
                    html.Span("{{ c.detail }}", classes="etext")

    def _statusbar(self) -> None:
        """Where the case runs and with what, the case's size, the last check's verdict and the file's state, each
        a segment; the selected run on the right while the Runs page is shown."""
        with html.Div(classes="statusbar", role="status"):
            with html.Span(classes="loc", title="Runs and checks of this case use this machine"):
                html.Span(v_html="icons.local")
                html.Span("Local")
            html.Span(
                "Mallard {{ version }}{{ build ? ' · ' + build : '' }}",
                classes="seg",
                title="The mallard build checks and runs use",
            )
            html.Span("{{ cells }}", classes="seg", v_if="cells")
            with html.Span(
                classes=(
                    "['seg', 'chk', check.running ? '' : check.errors ? 'err' : check.warnings ? 'warn' : 'okc']",
                ),
                v_if="check.status || check.running",
                title=("check.running ? 'mallard check is running' : 'mallard check took ' + check.seconds + ' s'",),
            ):
                html.Span(classes="dot")
                html.Span("Checking…", v_if="check.running")
                html.Span(
                    "{{ check.errors ? check.errors + (check.errors === 1 ? ' error' : ' errors') "
                    ": check.warnings ? check.warnings + (check.warnings === 1 ? ' warning' : ' warnings') "
                    ": 'Check passed' }}",
                    v_else=True,
                )
                html.Span("checked {{ check.ago }}", classes="when", v_if="!check.running && check.ago")
            html.Span(
                "{{ input_name }} · {{ doc_edited ? 'unsaved changes' : 'saved' }}",
                classes=("['seg', {edited: doc_edited}]",),
            )
            with html.Span(v_if="page === 'runs' && run.name", classes="seg run", style="margin-left: auto"):
                html.Span("{{ run.name }} · {{ run.status }} · {{ run.progress }} %")


# Helpers


def _backend(name: str) -> str:
    """Kokkos's execution space as hardware: `CPU (OpenMP)`, `CUDA GPU`."""
    known = {"cuda": "CUDA GPU", "hip": "HIP GPU", "sycl": "SYCL GPU"}
    return known.get(name.lower(), f"CPU ({name})" if name else "")


def _local_pick(event: Any) -> tuple[list[float], str] | None:
    """A pick the browser made (vtk.js): the world position and the picked actor's reference."""
    picks = event.get("selection") if isinstance(event, dict) and "selection" in event else event
    if isinstance(picks, dict):
        picks = [picks]
    if not isinstance(picks, list):
        return None
    for p in picks:
        if isinstance(p, dict) and p.get("worldPosition"):
            return [float(c) for c in p["worldPosition"][:3]], str(p.get("remoteId") or "")
    return None


def _restart_of(start: dict[str, Any]) -> str:
    """`restart_004303.restart · step 4303` for a part started from a restart file."""
    of = (start or {}).get("restart_of") or {}
    if not of.get("file"):
        return "–"
    name = Path(str(of["file"])).name
    return f"{name} · step {of['step']}" if of.get("step") is not None else name


def _tail(path: Path, lines: int = 200) -> str:
    try:
        with path.open("rb") as f:
            f.seek(max(0, f.seek(0, 2) - 64_000))
            data = f.read()
    except FileNotFoundError:
        return ""
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])


def _report_text(data: dict[str, Any]) -> str:
    import json

    shown = {k: v for k, v in data.items() if k != "environment"}
    return json.dumps(shown, indent=2)
