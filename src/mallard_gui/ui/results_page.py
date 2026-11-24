"""The results viewer page: any snapshot of a run, where the run is. A local run is read in place by the
engine in this process; a remote run by a viewer job next to its data, whose frames arrive in memory.

Frames are images (the design's image mode): while the camera moves, a coarse level at half resolution;
full quality on release. Camera moves are coalesced, so a slow round trip drops intermediate frames
instead of queueing them. Probes and lines over time are read next to the data in batches, plotted as
they arrive and cancellable between batches.
"""

from __future__ import annotations

import asyncio
import base64
import math
import time
import tomllib
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import client, html
from trame.widgets import vtk as vtkw

from ..export import PlotSpec, caption_frame
from ..plots import Curve, axis_label, line_plot, nice_ticks
from ..postjobs import PostJob, average_args, diff_args, extract_args, plane_region
from ..spectra import welch
from ..view.scene import PLANE_PRESETS, PRESETS
from ..viewer.batch import BatchImages
from ..viewer.client import RemoteViewer, StdioViewer, Viewer, ViewerUnavailable
from ..viewer.colormaps import COLORMAPS, DEFAULT, color_at, css_gradient, svg_swatch
from ..viewer.engine import COMPONENTS, ViewerError
from ..viewer.k8s import SocketViewer
from ..viewer.parallel import ParallelViewer
from ..views import SavedView, delete_view, save_view, views
from .copy import COPY_PROPS, copy_button
from .dropdown import dropdown
from .empty import empty_state
from .fmt import grouped, number
from .geometry_view import GeometryScene, want_geometry
from .panes import list_splitter, list_style, splitter
from .section import note_row, section_bar, section_state

if TYPE_CHECKING:
    from .app import App

PROBE_BATCH = 8
LINE_TIMES = 6
FULL_AFTER = 0.25  # seconds of quiet after a wheel zoom before the full-quality frame
DRAG_THRESHOLD = 3.0  # css px: a smaller press-and-release is a click


# How a cell field is drawn; a third, Mallard's high-order reconstruction sampled, is to come
INTERPOLATION_CHOICES = (
    (
        "cells",
        "Cell values",
        "Cell values: each cell drawn flat in the one value Mallard wrote for it, as the solver sees it",
    ),
    (
        "linear",
        "Interpolated",
        "Interpolated: the cells' values averaged to their corners and blended linearly across each cell, smooth but "
        "softer at shocks and extremes",
    ),
)


def zoom_op(f: float, nx: float, ny: float) -> tuple[str, float, float, float]:
    """A zoom by f toward (nx, ny), 0..1 from the top left: scaled about the center, the point there moves from
    u to f u (u from the center, in view fractions), so a pan of u (1 - f) brings it back."""
    return ("zoom", (nx - 0.5) * (1.0 - f), (ny - 0.5) * (1.0 - f), f)


def camera_calls(ops: list[tuple[str, float, float, float]]) -> list[dict[str, Any]]:
    """The viewer's camera calls of merged moves: a zoom is its scale about the center, then its pan."""
    calls: list[dict[str, Any]] = []
    for op, dx, dy, factor in ops:
        if op == "zoom":
            calls.append({"op": "zoom", "factor": factor})
            if dx or dy:
                calls.append({"op": "pan", "dx": dx, "dy": dy})
        else:
            calls.append({"op": op, "dx": dx, "dy": dy, "factor": factor})
    return calls


def _viewer_camera(camera: Any) -> dict[str, Any]:
    """The browser's camera (vtk.js) in the viewer's terms; empty for none."""
    if not isinstance(camera, dict) or "position" not in camera:
        return {}
    return {
        "position": camera["position"],
        "focal_point": camera["focalPoint"],
        "view_up": camera["viewUp"],
        "view_angle": float(camera.get("viewAngle") or 30.0),
        "parallel": bool(camera.get("parallelProjection")),
        "parallel_scale": float(camera.get("parallelScale") or 1.0),
    }


class ResultsPage:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self.viewer: Viewer | None = None
        self.run_dir = ""
        self._nondimensional = False  # the open run's input says `physics.units = "nondimensional"`
        self._ops: list[tuple[str, float, float, float]] = []
        self._seq: Any = 0  # the page's name for its last move or zoom (zoom.js), which the next frame will show
        self._want: str | None = None  # "interactive" | "full"
        self._pump: asyncio.Task[None] | None = None
        self._full_later: asyncio.TimerHandle | None = None
        self._press: tuple[float, float, int, bool] | None = None
        self._last: tuple[float, float] = (0.0, 0.0)
        self._moved = False
        self._cancel = False
        self._series: dict[str, dict[str, Any]] = {}
        self._pending_p0: list[str] | None = None
        self._rtt = 0.0
        self._links: deque[float] = deque(maxlen=7)  # round trips less render time: the link's latency
        self._probe_series: tuple[list[float], list[float], str, dict[str, Any]] | None = None
        self._calls = asyncio.Lock()
        self.geo = GeometryScene()
        self._geo_camera = False  # the viewer's camera moved here (a preset, Fit): the browser's view takes it
        self.batch: BatchImages | None = None
        self._stats_key: tuple[Any, ...] | None = None
        s = self.state
        s.v_run = ""
        s.v_where = ""
        s.v_viewer = ""
        s.v_status = ""
        s.v_error = ""
        s.v_series = []
        s.v_primary = ""
        s.v_zones = []
        s.v_particles = []  # the particle series shown
        s.v_pcolorbar = {}
        s.v_info = {}
        s.v_stats = {}  # the shown field's statistics (viewer/stats.py): domain, slice, and their histograms
        s.v_stats_on = "domain"
        s.v_index = 0
        s.v_frame = ""
        s.v_frame_seq = 0
        s.v_camera = {}
        s.v_meta = {}
        s.v_size = {}
        s.v_cmaps = [{"value": n, "label": n, "swatch": svg_swatch(n)} for n in COLORMAPS]
        s.v_components = list(COMPONENTS)
        s.v_min = ""
        s.v_max = ""
        s.v_colorbar = {}
        s.v_split = False  # a second view beside the first, the same camera colored by another field
        s.v_field2 = ""
        s.v_comp2 = "magnitude"
        s.v_frame2 = ""
        s.v_colorbar2 = {}
        s.v_split_note = ""
        s.v_clip = False
        s.v_slice = False
        s.v_clip_origin = ["0", "0", "0"]
        s.v_clip_normal = ["0", "0", "1"]
        s.v_clip_flip = False
        s.v_snap = True
        s.v_sec = {"lo": 0.0, "hi": 1.0, "at": 0.0, "text": "0", "axis": "z"}
        s.v_sec_mark = True
        s.v_sec_more = False
        s.v_xray = False
        s.v_xray_opacity = 20
        s.v_lock2d = False
        s.v_parallel = False
        s.v_preset = ""
        s.v_mode = "orbit"
        s.v_point = ["", "", ""]
        s.v_p0 = ["", "", ""]
        s.v_p1 = ["", "", ""]
        s.v_line_times = "spread"
        s.v_probe_plot = ""
        s.v_line_plot = ""
        s.v_probe_text = ""
        s.v_line_text = ""
        s.v_busy = ""
        s.v_dim = 3
        s.v_render_mode = "auto"  # images | geometry | auto
        s.v_geometry = False
        s.v_geo_note = ""
        s.v_render = {}
        s.v_unavailable = []
        s.v_batch = False
        s.v_views = []
        s.v_view_name = ""
        s.v_whole = {}  # the parallel viewer job a volume over the budget would take: ranks, text
        s.v_jobs = []
        s.v_diff_with = 0
        s.v_avg_t = ["", ""]
        s.v_probe_kind = "time"
        s.v_snaps = []  # the primary series' snapshots: step and t
        app.state.change("v_size")(self._resized)
        app.state.change("v_clip_origin", "v_clip_normal", "v_clip_flip")(self._section_fields)
        app.state.change("v_xray_opacity")(self._xray_opacity)
        server = app.server
        server.trigger("v_down")(self.mouse_down)
        server.trigger("v_move")(self.mouse_move)
        server.trigger("v_up")(self.mouse_up)
        server.trigger("v_wheel")(self.wheel)
        server.trigger("geo_camera")(self.on_geo_camera)
        server.trigger("geo_fit")(self.fit)
        server.trigger("geo_frame")(self.focus)
        server.trigger("v_scrub")(self.scrub)
        server.trigger("v_sec_slide")(self.slide_section)

    # Opening a run

    def open(self) -> None:
        self.app.exports.watch_paraview()
        runs = self.state.runs
        if not self.state.v_run and runs:
            self.select_run(runs[0]["name"])
        elif self.viewer is not None:
            self._request("full")

    def select_run(self, name: str) -> None:
        if name == self.state.v_run and self.state.v_frame and not self.state.v_error:
            return  # already open
        self.state.v_run = name
        self._schedule(self._open(name))

    def use_login(self) -> None:
        """The user's explicit choice, each time: the viewer on the shared login node."""
        if self.state.v_run:
            self._schedule(self._open(self.state.v_run, login=True))

    def use_parallel(self) -> None:
        """The user's choice, shown its cost first: a parallel viewer job renders the volume whole."""
        ranks = int((self.state.v_whole or {}).get("ranks") or 0)
        if self.state.v_run and ranks:
            self._schedule(self._open(self.state.v_run, ranks=ranks))

    def use_batch(self) -> None:
        """No live view: each image rendered by a batch job next to the data."""
        if self.state.v_run:
            self._schedule(self._open_batch(self.state.v_run))

    def _schedule(self, coro: Any) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self.app._tasks.add(task)
        task.add_done_callback(self.app._tasks.discard)
        return task

    async def _open(self, name: str, login: bool = False, ranks: int = 0) -> None:
        s = self.state
        record = self.app.session.project.run(name)
        where = record.home.get("remote", "local")
        with s:
            s.v_error, s.v_frame, s.v_series, s.v_info = "", "", [], {}
            s.v_frame2, s.v_colorbar2, s.v_split_note = "", {}, ""
            s.v_probe_plot = s.v_line_plot = ""
            s.v_unavailable, s.v_batch, s.v_geometry = [], False, False
            s.v_clip = s.v_slice = s.v_clip_flip = s.v_xray = False  # a run opens whole, as its viewer has it
            s.v_sec_mark = True
            s.v_views = self._view_list(name)
            self.batch = None
            s.v_where = where
            s.v_status = "opening in place" if where == "local" else f"starting the viewer job on {where}"
            if ranks:
                s.v_status = f"starting the parallel viewer ({ranks} ranks) on {where}"
            s.v_whole = {}
        loop = asyncio.get_running_loop()

        def waiting(seconds: float) -> None:
            if s.v_run == name:
                loop.call_soon_threadsafe(self._set_status, f"waiting for the viewer job on {where}: {seconds:.0f} s")

        try:
            if ranks:
                viewer, run_dir = await asyncio.to_thread(self.app.session.parallel_viewer, record, ranks, waiting)
            else:
                viewer, run_dir = await asyncio.to_thread(self.app.session.viewer, record, waiting, login)
            if s.v_run != name:
                return  # another run was picked while this one's viewer started
            self.viewer, self.run_dir = viewer, run_dir
            self._nondimensional = await asyncio.to_thread(self._run_nondimensional, where, run_dir)
            opened = await self._call("open", run=self.run_dir)
            await self._call("set_theme", theme=s.theme)
            render = await self._call("render_info")
        except ViewerUnavailable as e:
            with s:
                s.v_status, s.v_error = "", str(e).split(" (", 1)[0]
                s.v_unavailable = [{"launch": a, "why": w} for a, w in e.attempts]
            return
        except (ViewerError, KeyError, ValueError, OSError) as e:
            if s.v_run == name:
                with s:
                    s.v_status, s.v_error = "", str(e)
            return
        if opened is None or s.v_run != name:
            return
        with s:
            s.v_render = render or {}
        await self._apply_series(opened)

    async def _apply_series(self, opened: dict[str, Any], show: str = "") -> None:
        s = self.state
        self._series = {x["id"]: x for x in opened["series"]}
        listed = [
            {k: v for k, v in x.items() if k != "snapshots"} | {"count": len(x["snapshots"])} for x in opened["series"]
        ]
        volumes = [x["id"] for x in listed if x["kind"] == "volume" and x["count"]]
        primary = volumes[0] if volumes else next((x["id"] for x in listed if x["count"]), "")
        with s:
            s.v_series = listed
            s.v_dim = int(opened["dim"])
            s.v_lock2d = s.v_dim == 2
            s.v_viewer = self._viewer_text()
            s.v_status = ""
            s.v_zones = [x["id"] for x in listed if x["kind"] == "zone"]
            s.v_particles = [x["id"] for x in listed if x["kind"] == "particles"]
        if show and show in self._series:
            await self._show(show, len(self._series[show]["snapshots"]) - 1)
        elif primary:
            count = self._series[primary]["snapshots"]
            await self._show(primary, len(count) - 1, list(s.v_zones))

    def outputs_changed(self, name: str) -> None:
        """A running run wrote snapshots: list them; a view at the latest snapshot follows to the new latest."""
        if name == self.state.v_run and self.viewer is not None:
            self._schedule(self._reread())

    async def _reread(self) -> None:
        opened = await self._call("open", run=self.run_dir)
        if opened is None:
            return
        s = self.state
        was = self._series.get(s.v_primary, {}).get("snapshots", [])
        at_latest = int(s.v_index) >= len(was) - 1
        self._series = {x["id"]: x for x in opened["series"]}
        with s:
            s.v_series = [
                {k: v for k, v in x.items() if k != "snapshots"} | {"count": len(x["snapshots"])}
                for x in opened["series"]
            ]
        now = self._series.get(s.v_primary, {}).get("snapshots", [])
        if s.v_primary and at_latest and len(now) > len(was):
            await self._show(s.v_primary, len(now) - 1, list(s.v_zones))
        elif not s.v_primary and opened["series"]:
            await self._open(s.v_run)

    def _set_status(self, text: str) -> None:
        with self.state:
            self.state.v_status = text

    def _viewer_text(self) -> str:
        v = self.viewer
        if isinstance(v, ParallelViewer):
            return f"{v.where} · parallel viewer, {v.ranks} ranks composited by pvserver"
        if isinstance(v, RemoteViewer):
            return f"{v.where} · viewer job pid {v.info.get('pid')} on {v.info.get('host')}"
        if isinstance(v, SocketViewer):
            return f"{v.where} · viewer pod {v.info.get('host')}, through a localhost port-forward"
        if isinstance(v, StdioViewer):
            return "this machine, reading the run in place"
        return "local"

    async def _call(self, method: str, **params: Any) -> Any:
        if self.viewer is None:
            return None
        start = time.perf_counter()
        try:
            # One at a time, in the order asked: each runs in a thread, and two settings made in a row (Clip, then
            # an origin typed) reached the viewer, and answered, in either order.
            async with self._calls:
                result = await asyncio.to_thread(self.viewer.call, method, **params)
        except ViewerError as e:
            with self.state:
                self.state.v_error = str(e)
            return None
        if method == "frame" and isinstance(result, dict):
            self._rtt = 1000 * (time.perf_counter() - start)
            self._links.append(max(self._rtt - float(result.get("ms") or 0.0), 0.0))
        return result

    # What is shown

    async def _show(self, series: str, index: int, zones: list[str] | None = None) -> None:
        if self.batch is not None:
            snaps = self._series[series]["snapshots"]
            index = max(0, min(index, len(snaps) - 1))
            with self.state:
                info = dict(self.state.v_info or {})
                info.update(
                    series=series, index=index, count=len(snaps), step=snaps[index]["step"], t=snaps[index]["t"]
                )
                self.state.v_info, self.state.v_primary, self.state.v_index = info, series, index
            self._request("full")
            return
        params: dict[str, Any] = {"series": series, "index": index}
        if zones is not None:
            params["zones"] = zones
        if any(x["kind"] == "particles" for x in self._series.values()):
            params["particles"] = list(self.state.v_particles)
        info = await self._call("show", **params)
        if info is None:
            return
        with self.state:
            if self.state.v_primary != series or len(self.state.v_snaps) != info["count"]:
                snaps = self._series.get(series, {}).get("snapshots", [])
                self.state.v_snaps = [{"step": x["step"], "t": x["t"]} for x in snaps]
            self.state.v_primary = series
            self._apply_info(info)
        await self._offer_whole(int(info.get("over_budget") or 0))
        self._request("full")

    async def _offer_whole(self, cells: int) -> None:
        """For a volume over this viewer's budget, the parallel viewer job that would render it whole."""
        whole: dict[str, Any] = {}
        if cells and not isinstance(self.viewer, ParallelViewer):
            try:
                record = self.app.session.project.run(self.state.v_run)
                job = await asyncio.to_thread(self.app.session.parallel_plan, record, cells)
                whole = {"ranks": job.ranks, "text": job.text}
            except (KeyError, ValueError) as e:
                whole = {"ranks": 0, "text": str(e)}
        with self.state:
            self.state.v_whole = whole

    def _apply_info(self, info: dict[str, Any]) -> None:
        s = self.state
        s.v_info = info
        s.v_index = info["index"]
        s.v_zones = info["zones"]
        s.v_error = ""
        particles = info.get("particles") or {}
        s.v_particles = particles.get("shown", [])
        s.v_pcolorbar = self._particle_colorbar(info) if particles.get("range") else {}
        if info.get("range"):
            lo, hi = info["range"]
            s.v_min, s.v_max = number(lo), number(hi)
            s.v_colorbar = self._colorbar(info, lo, hi)
        else:
            s.v_colorbar = {}
        sec = info.get("section") or {}
        if sec:
            s.v_clip_origin = [number(c) for c in sec["origin"]]
            s.v_clip_normal = [number(c) for c in sec["normal"]]
            self._section_bar(sec)
        self._want_stats(info)

    def _want_stats(self, info: dict[str, Any]) -> None:
        """The statistics again when what they are of changed: the snapshot, field, component, range or slice."""
        sec = info.get("section") or {}
        key = (info.get("series"), info.get("index"), info.get("field"), info.get("component"),
               tuple(info.get("range") or ()), sec.get("slice"), tuple(sec.get("origin") or ()),
               tuple(sec.get("normal") or ()))  # fmt: skip
        if key != self._stats_key and self.viewer is not None and self.batch is None:
            self._stats_key = key
            self._schedule(self._stats(info))

    async def _stats(self, info: dict[str, Any]) -> None:
        try:
            async with self._calls:
                got = await asyncio.to_thread(self.viewer.call, "stats") if self.viewer else None
        except ViewerError as e:  # a viewer without them (ParaView's): said in the section, not as an error
            text = str(e)
            got = {"error": "this viewer does not compute statistics" if _unsupported(text) else text}
        meta: dict[str, Any] = next((f for f in info.get("fields") or [] if f["name"] == info.get("field")), {})
        units = meta.get("units", "")
        with self.state:
            self.state.v_stats = stats_view(got or {}, info.get("colormap", DEFAULT), units, int(self.state.v_dim or 3))

    def _stats_section(self) -> None:
        """The shown field's statistics this snapshot, over the domain or the slice, weighted by each cell's
        measure; min and max with where they are, a click probing there; the histogram over the shown range."""
        place = "((v_stats.places || {})[v_stats_on] || (v_stats.places || {}).domain)"
        with html.Div(classes="group-head"):
            html.Span("STATISTICS")
            html.Span("this snapshot, by volume", classes="note", v_if="v_dim > 2")
            html.Span("this snapshot, by area", classes="note", v_if="v_dim <= 2")
        html.Div("{{ v_stats.error }}", v_if="v_stats.error", classes="prop-empty muted")
        with html.Div(v_if=f"!v_stats.error && {place}", classes="fstats"):
            with html.Div(
                v_if="(v_stats.places || {}).slice", classes="segmented st-where", role="group", aria_label="over"
            ):
                for key, label in (("domain", "Domain"), ("slice", "Slice")):
                    html.Button(
                        label,
                        classes=(f"['seg', {{on: v_stats_on === '{key}'}}]",),
                        aria_pressed=(f"String(v_stats_on === '{key}')",),
                        __properties=[("aria_pressed", "aria-pressed")],
                        click=f"v_stats_on = '{key}'",
                    )
            with html.Div(v_for=f"r in {place}.rows", key="r.label", classes="prow st-row"):
                html.Div("{{ r.label }}", classes="plabel")
                with html.Div(classes="pval st-val"):
                    html.Span("{{ r.value }}", classes="mono st-num")
                    html.Span("{{ r.note }}", v_if="r.note", classes="muted st-note")
                    html.Button(
                        "{{ r.at }}",
                        v_if="r.point",
                        classes="linkbtn mono st-at",
                        title=("'Probe over time at ' + r.at",),
                        aria_label=("'Probe at the ' + r.label",),
                        click=(self.probe, "[r.point]"),
                    )
            with html.Div(v_if=f"{place}.svg", classes="st-hist"):
                html.Div(v_html=f"{place}.svg", classes="st-plot")
                with html.Div(classes="st-axis mono"):
                    html.Span(f"{{{{ {place}.lo }}}}")
                    html.Span(f"{{{{ {place}.hi }}}}")
            html.Div(f"{{{{ {place}.summary }}}}", classes="muted st-summary")

    def _colorbar(self, info: dict[str, Any], lo: float, hi: float) -> dict[str, Any]:
        meta: dict[str, Any] = next((f for f in info["fields"] if f["name"] == info["field"]), {})
        name = info["field"]
        if meta.get("components", 1) > 1:
            name += f" ({info['component']})" if info["component"] == "magnitude" else f"_{info['component'].upper()}"
        ticks = [t for t in nice_ticks(lo, hi, 5) if lo <= t <= hi]
        return {
            "css": css_gradient(info["colormap"]),
            "label": axis_label(name, meta.get("units", "")),
            "ticks": [{"pos": round(100 * (t - lo) / (hi - lo), 2), "text": number(t)} for t in ticks],
        }

    def _particle_colorbar(self, info: dict[str, Any]) -> dict[str, Any]:
        p = info["particles"]
        lo, hi = p["range"]
        ticks = [t for t in nice_ticks(lo, hi, 5) if lo <= t <= hi]
        return {
            "css": css_gradient(info["colormap"]),
            "label": f"particles: {p['field']}",
            "ticks": [{"pos": round(100 * (t - lo) / (hi - lo), 2), "text": number(t)} for t in ticks],
        }

    def toggle_particles(self, sid: str) -> None:
        shown = list(self.state.v_particles)
        shown.remove(sid) if sid in shown else shown.append(sid)
        self.state.v_particles = shown
        self._schedule(self._show(self.state.v_primary, self.state.v_index))

    def set_particle_field(self, field: str) -> None:
        self._schedule(self._plain_info("set_particles", field=field))

    async def _plain_info(self, method: str, **params: Any) -> None:
        info = await self._call(method, **params)
        if info is not None:
            with self.state:
                self._apply_info(info)
            self._request("full")

    def select_series(self, sid: str) -> None:
        if sid in self._series:
            n = len(self._series[sid]["snapshots"])
            index = min(self.state.v_index, n - 1) if sid == self.state.v_primary else n - 1
            self._schedule(self._show(sid, index))

    def toggle_zone(self, sid: str) -> None:
        zones = list(self.state.v_zones)
        zones.remove(sid) if sid in zones else zones.append(sid)
        self._schedule(self._show(self.state.v_primary, self.state.v_index, zones))

    def step_to(self, where: str) -> None:
        count = int((self.state.v_info or {}).get("count", 0))
        if not count:
            return
        i = int(self.state.v_index)
        index = {"first": 0, "prev": i - 1, "next": i + 1, "last": count - 1}.get(where, i)
        index = max(0, min(index, count - 1))
        if index != i:
            self._schedule(self._show(self.state.v_primary, index))

    def scrub(self, index: Any) -> None:
        try:
            i = int(index)
        except (TypeError, ValueError):
            return
        if i != self.state.v_index:
            self.state.v_index = i
            self._schedule(self._show(self.state.v_primary, i))

    def set_field(self, field: str) -> None:
        self._schedule(self._field(field=field))

    def set_component(self, component: str) -> None:
        self._schedule(self._field(component=component))

    def set_interpolation(self, mode: str) -> None:
        self._schedule(self._field(interpolation=mode))

    def set_colormap(self, name: str) -> None:
        self._schedule(self._field(colormap=name))

    def set_auto(self, auto: bool) -> None:
        if auto:
            self._schedule(self._field(auto_range=True))
        else:
            rng = (self.state.v_info or {}).get("range")
            if rng:
                self._schedule(self._field(range=list(rng)))

    def set_range(self, lo: Any, hi: Any) -> None:
        try:
            r = [float(lo), float(hi)]
        except (TypeError, ValueError):
            self.state.v_error = f"range {lo!r} to {hi!r}: two numbers"
            return
        self._schedule(self._field(range=r))

    async def _field(self, **params: Any) -> None:
        await self._plain_info("set_field", **params)

    # The section tool

    def toggle(self, what: str, camera: Any = None) -> None:
        s = self.state
        if what == "clip":
            s.v_clip = not s.v_clip
            self._schedule(self._section(clip=s.v_clip))
        elif what == "slice":
            s.v_slice = not s.v_slice
            self._schedule(self._section(slice=s.v_slice))
        elif what == "flip":
            s.v_clip_flip = not s.v_clip_flip
        elif what == "mark":
            s.v_sec_mark = not s.v_sec_mark
            self._schedule(self._section(mark=s.v_sec_mark))
        elif what == "section_off":
            s.v_clip = s.v_slice = False
            self._schedule(self._section(clip=False, slice=False))
        elif what == "xray":
            s.v_xray = not s.v_xray
            self._schedule(self._plain("set_xray", on=s.v_xray, opacity=s.v_xray_opacity / 100))
        elif what == "lock2d":
            s.v_lock2d = not s.v_lock2d
            if s.v_lock2d and s.v_dim == 2:
                s.v_preset = "-Z"  # turned back to the plane
            self._camera("lock2d", factor=1.0 if s.v_lock2d else 0.0, camera=camera)
        elif what == "parallel":
            s.v_parallel = not s.v_parallel
            self._camera("parallel", factor=1.0 if s.v_parallel else 0.0, camera=camera)
        elif what in ("probe", "line", "orbit"):
            s.v_mode = "orbit" if s.v_mode == what else what
            self._pending_p0 = None

    def set_clip_axis(self, axis: str) -> None:
        self.state.v_clip_normal = {"x": ["1", "0", "0"], "y": ["0", "1", "0"], "z": ["0", "0", "1"]}[axis]
        if not self.state.v_clip and not self.state.v_slice:
            self.state.v_slice = True
            self._schedule(self._section(slice=True))

    def _section_fields(self, v_clip_origin: list[str], v_clip_normal: list[str], v_clip_flip: bool, **_: Any) -> None:
        try:
            origin = [float(v) for v in v_clip_origin]
            normal = [float(v) for v in v_clip_normal]
        except (TypeError, ValueError):
            return
        if not any(normal) or self.viewer is None:
            return
        self._schedule(self._section(origin=origin, normal=normal, flipped=v_clip_flip, snap=self.state.v_snap))

    async def _section(self, **params: Any) -> None:
        sec = await self._call("set_section", **params)
        if sec is not None:
            with self.state:
                info = dict(self.state.v_info or {})
                info["section"] = sec
                self.state.v_info = info
                self._section_bar(sec)
                self._want_stats(info)
            self._request("full")

    def _section_bar(self, sec: dict[str, Any]) -> None:
        """The bar's slider and value, from the plane as the viewer has it."""
        if "extent" in sec:
            self.state.v_sec = section_state(tuple(sec["extent"]), float(sec["offset"]), sec["normal"])

    def slide_section(self, fraction: float) -> None:
        """The bar's slider: the plane at that fraction of the data's reach along its normal."""
        sec = self.state.v_sec
        self.set_section_offset(sec["lo"] + min(max(float(fraction), 0.0), 1.0) * (sec["hi"] - sec["lo"]))

    def set_section_offset(self, value: Any) -> None:
        """The plane moved along its normal to the position `value` (for an axis, that coordinate)."""
        section = (self.state.v_info or {}).get("section") or {}
        try:
            at = float(value)
            origin, normal = [float(c) for c in section["origin"]], [float(c) for c in section["normal"]]
        except (TypeError, ValueError, KeyError):
            return
        shift = at - sum(o * c for o, c in zip(origin, normal, strict=True))
        self.state.v_clip_origin = [number(o + shift * c) for o, c in zip(origin, normal, strict=True)]

    def _xray_opacity(self, v_xray_opacity: Any, **_: Any) -> None:
        try:
            value = float(v_xray_opacity) / 100
        except (TypeError, ValueError):
            return
        if self.viewer is not None:
            self._schedule(self._plain("set_xray", on=self.state.v_xray, opacity=value))

    async def _plain(self, method: str, **params: Any) -> None:
        await self._call(method, **params)
        self._request("full")

    def set_theme(self, theme: str) -> None:
        if self.viewer is not None:
            self._schedule(self._plain("set_theme", theme=theme))

    # The camera and frames

    # A camera command starts from the view as it is drawn: in geometry mode the page sends its own camera with
    # the click (`camera`), as the viewer hears of a drag only a moment after it ends.

    def preset(self, name: str, camera: Any = None) -> None:
        if name in PRESETS:
            self.state.v_preset = name
            if self.state.v_dim == 2 and name not in PLANE_PRESETS:  # out of the plane: the viewer lifts the lock
                self.state.v_lock2d = False
            self._camera("preset", preset=name, camera=camera)

    def fit(self, camera: Any = None) -> None:
        self._camera("fit", camera=camera)

    def focus(self, camera: Any = None) -> None:
        self._camera("focus", camera=camera)

    def _camera(self, op: str, factor: float = 1.0, preset: str = "", camera: Any = None) -> None:
        size = (self.state.v_size or {}).get("size") or {}
        w, h = float(size.get("width") or 0), float(size.get("height") or 0)
        aspect = w / h if w > 0 and h > 0 else 0.0
        cam = _viewer_camera(camera)

        async def run() -> None:
            if cam:
                await self._call("set_camera", **cam)
            await self._call("camera", op=op, factor=factor, preset=preset, aspect=aspect)
            self._geo_camera = True
            self._request("full")

        self._schedule(run())

    def save_image(self, camera: Any = None) -> None:
        """The results view as a PNG file the user names, rendered by the viewer at the pixels the view is drawn
        with (its size times the screen's pixel ratio), in the theme shown, with the time and the colorbar
        written on it (the page's own are not in a rendered image)."""
        if self.viewer is None or self.batch is not None:
            return
        w, h = self._frame_size(False)
        info = dict(self.state.v_info or {})
        cam = _viewer_camera(camera)
        record = self.app.session.project.run(self.state.v_run)
        out = self.app.session.project.root / "exports" / record.name
        name = f"{str(info.get('series', 'view')).rsplit('/', 1)[-1]}_{info.get('step', 0)}.png"

        async def save(path: Path) -> None:
            if cam:
                await self._call("set_camera", **cam)
            shot = await self._call("screenshot", width=w, height=h)
            if shot is None:
                return
            rng = info.get("range") or [None, None]
            t = info.get("t")
            text = f"step {info.get('step')}" + (f" · t {number(t)}" if isinstance(t, (int, float)) else "")
            image = caption_frame(
                shot["image"], text, info.get("colormap", ""), rng[0], rng[1], self._field_label(info)
            )
            path = path if path.suffix.lower() == ".png" else path.with_name(path.name + ".png")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(image)
            except OSError as e:
                with self.state:
                    self.state.v_error = f"{path.name}: {e}"
                return
            with self.state:
                self.state.v_geo_note = f"saved {path} · {shot['width']}×{shot['height']} px"

        def chosen(path: Path) -> None:
            self._schedule(save(path))

        self.app.paths.choose("image", out if out.is_dir() else out.parent, name, chosen)

    def _resized(self, v_size: Any = None, **_: Any) -> None:
        self._request("full")

    def _frame_size(self, interactive: bool) -> tuple[int, int]:
        size = (self.state.v_size or {}).get("size") or {}
        ratio = float((self.state.v_size or {}).get("pixelRatio") or 1)
        return max(int(float(size.get("width") or 800) * ratio), 16), max(
            int(float(size.get("height") or 600) * ratio), 16
        )

    def _request(self, want: str) -> None:
        if want == "full" or self._want != "full":
            self._want = want
        if self._pump is None or self._pump.done():
            self._pump = self._schedule(self._run_pump())

    async def _run_pump(self) -> None:
        if self.batch is not None:
            await self._batch_frame()
            self._ops, self._want = [], None
            return
        while self.viewer is not None and (self._ops or self._want):
            ops, self._ops = self._merge(self._ops), []
            want, self._want = self._want, None
            seq = self._seq
            for call in camera_calls(ops):
                await self._call("camera", **call)
            interactive = want == "interactive"
            if self.state.v_geometry and not interactive:
                await self._maybe_geometry()  # the browser draws: no image to render
                if self.state.v_geometry:
                    continue
            w, h = self._frame_size(interactive)
            frame = await self._call("frame", width=w, height=h, interactive=interactive)
            if frame is None:
                return
            data = base64.b64encode(frame["image"]).decode("ascii")
            with self.state:
                self.state.v_frame = f"data:{frame['mime']};base64,{data}"
                self.state.v_frame_seq = seq
                if frame.get("camera"):
                    self.state.v_camera = frame["camera"]  # the one this image was rendered with: the triad's
                self.state.v_meta = {
                    "ms": frame["ms"],
                    "rtt": round(self._rtt),
                    "size": f"{frame['width']}×{frame['height']}",
                    "triangles": grouped(frame["triangles"]),
                    "quality": "coarse, half size" if interactive else "full",
                }
            if self.state.v_split:
                await self._frame2(w, h, interactive)
            if not interactive:
                await self._maybe_geometry()

    async def _maybe_geometry(self) -> None:
        """Geometry mode when chosen, or (auto) when round trips are long and the surfaces fit the budget."""
        s = self.state
        info = s.v_info or {}
        budget = int((info.get("budget") or {}).get("geometry_triangles", 2_000_000))
        # Auto: always for a viewer here; for a remote one, on the link's median latency over a few frames.
        local = not isinstance(self.viewer, (RemoteViewer, SocketViewer))
        remote = not local and len(self._links) >= 3
        link = sorted(self._links)[len(self._links) // 2] if remote else 0.0
        mode = "images" if s.v_split else s.v_render_mode  # both views are the viewer's pictures, in step
        if not want_geometry(mode, link, int(info.get("triangles") or 0), budget, local):
            if s.v_geometry:
                with s:
                    s.v_geometry = False
            return
        payload = await self._call("geometry")
        if payload is None:
            with s:
                s.v_geometry, s.v_geo_note = False, "images: the surfaces are over the geometry budget"
            return
        self.geo.load(payload)
        if not s.v_geometry:
            self._geo_camera = True  # a view shown anew frames itself (vtk.js): it takes the viewer's camera
        with s:
            s.v_geometry = True
            s.v_geo_note = f"{grouped(payload['triangles'])} triangles drawn in the browser"
        self.app.ctrl.geo_update()
        if self._geo_camera:  # vtk.js takes a scene's camera once: later ones are pushed
            self._geo_camera = False
            self._geo_view.push_camera()

    def on_geo_camera(self, camera: Any) -> None:
        """The browser's camera after an orbit in geometry mode: the viewer's too, so the next payload and
        any image start from it."""
        cam = _viewer_camera(camera)
        if not cam:
            return
        self.geo.set_camera(cam)
        self._schedule(self._call("set_camera", **cam))

    def set_render_mode(self, mode: str) -> None:
        if mode in ("images", "geometry", "auto"):
            self.state.v_render_mode = mode
            self.state.v_geo_note = ""
            self._request("full")

    # The split view

    def set_split(self) -> None:
        s = self.state
        s.v_split = not s.v_split
        s.v_frame2, s.v_colorbar2, s.v_split_note, s.v_geo_note = "", {}, "", ""
        self._request("full")

    def set_field2(self, field: str) -> None:
        self.state.v_field2, self.state.v_frame2 = field, ""
        self._request("full")

    def set_comp2(self, component: str) -> None:
        self.state.v_comp2 = component
        self._request("full")

    async def _frame2(self, width: int, height: int, interactive: bool) -> None:
        """The second view at the size of the first, after it and with its camera: another field over its own
        range in the same colormap."""
        s = self.state
        info = s.v_info or {}
        fields = [f["name"] for f in info.get("fields") or []]
        if s.v_field2 not in fields:
            s.v_field2 = next((f for f in fields if f != info.get("field")), fields[0] if fields else "")
        if not s.v_field2 or self.viewer is None:
            return
        viewer = self.viewer
        try:
            async with self._calls:
                got = await asyncio.to_thread(
                    viewer.call,
                    "frame_field",
                    field=s.v_field2,
                    component=s.v_comp2,
                    width=width,
                    height=height,
                    interactive=interactive,
                )
        except ViewerError as e:
            text = str(e)
            with s:
                s.v_frame2, s.v_colorbar2 = "", {}
                s.v_split_note = "this viewer draws one field at a time" if _unsupported(text) else text
            return
        data = base64.b64encode(got["image"]).decode("ascii")
        with s:
            s.v_frame2, s.v_split_note = f"data:{got['mime']};base64,{data}", ""
            rng = got.get("range")
            s.v_colorbar2 = (
                self._colorbar({**info, "field": got["field"], "component": got["component"]}, *rng) if rng else {}
            )

    async def _batch_frame(self) -> None:
        s = self.state
        assert self.batch is not None
        info = s.v_info or {}
        size = (s.v_size or {}).get("size") or {}
        view = {
            "series": s.v_primary,
            "index": int(s.v_index),
            "zones": list(s.v_zones),
            "field": info.get("field", ""),
            "component": info.get("component", ""),
            "colormap": info.get("colormap", ""),
            "interpolation": info.get("interpolation", ""),
            "range": None if info.get("auto_range", True) else info.get("range"),
            "preset": s.v_preset or "iso",
            "theme": s.theme,
            "width": int(float(size.get("width") or 800)),
            "height": int(float(size.get("height") or 600)),
        }
        with s:
            s.v_status = f"rendering in a batch job on {self.batch.where}"
        try:
            png = await asyncio.to_thread(self.batch.render, self.run_dir, view)
        except (ViewerError, OSError) as e:
            with s:
                s.v_status, s.v_error = "", str(e)
            return
        with s:
            s.v_status = ""
            s.v_frame = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            s.v_meta = {"size": view["width"], "quality": "batch image", "ms": "–", "rtt": "–", "triangles": "–"}

    async def _open_batch(self, name: str) -> None:
        from ..data.locations import join
        from ..viewer.catalog import series_from_log

        s = self.state
        record = self.app.session.project.run(name)
        try:
            batch, self.run_dir = await asyncio.to_thread(self.app.session.batch_images, record)
            self.batch = batch
            location = self.app.session.data.location(batch.where)
            log = await asyncio.to_thread(location.read_bytes, join(self.run_dir, "mallard.jsonl"))
            text = await asyncio.to_thread(location.read_bytes, join(self.run_dir, "input.toml"))
        except (ViewerError, ValueError, OSError, KeyError) as e:
            with s:
                s.v_error = str(e)
            return
        series = [x.to_dict() for x in series_from_log(log, text.decode("utf-8", "replace"))]
        self._series = {x["id"]: x for x in series}
        volumes = [x for x in series if x["kind"] == "volume" and x["snapshots"]]
        if not volumes:
            with s:
                s.v_error = "no volume series in the run log"
            return
        primary = volumes[0]
        last = len(primary["snapshots"]) - 1
        snap = primary["snapshots"][last]
        with s:
            s.v_unavailable, s.v_error, s.v_batch = [], "", True
            s.v_series = [
                {k: v for k, v in x.items() if k != "snapshots"} | {"count": len(x["snapshots"])} for x in series
            ]
            s.v_primary, s.v_index = primary["id"], last
            s.v_zones = []
            s.v_viewer = f"{batch.where} · batch images (no live view)"
            s.v_info = {
                "series": primary["id"],
                "index": last,
                "count": len(primary["snapshots"]),
                "step": snap["step"],
                "t": snap["t"],
                "zones": [],
                "field": (primary["fields"] or [""])[0],
                "component": "magnitude",
                "colormap": DEFAULT,
                "auto_range": True,
                "fields": [],
                "blocks": [],
                "section": {},
            }
        self._request("full")

    @staticmethod
    def _merge(ops: list[tuple[str, float, float, float]]) -> list[tuple[str, float, float, float]]:
        """Consecutive moves of one kind become one: a slow round trip skips frames, not motion. A zoom is a
        scale by f about the view's center then a pan by (dx, dy), x -> f x + d on screen, so two compose as
        x -> f2 f1 x + (f2 d1 + d2)."""
        out: list[tuple[str, float, float, float]] = []
        for op in ops:
            if out and out[-1][0] == op[0]:
                k, dx, dy, f = out[-1]
                scale = op[3] if k == "zoom" else 1.0
                out[-1] = (k, scale * dx + op[1], scale * dy + op[2], f * op[3])
            else:
                out.append(op)
        return out

    def mouse_down(self, x: float, y: float, button: int = 0, shift: bool = False) -> None:
        self._press = (float(x), float(y), int(button), bool(shift))
        self._last = (float(x), float(y))
        self._moved = False

    def mouse_move(self, x: float, y: float, seq: Any = 0) -> None:
        if self._press is None:
            return
        self._seq = seq
        size = (self.state.v_size or {}).get("size") or {}
        w, h = float(size.get("width") or 800), float(size.get("height") or 600)
        x, y = float(x), float(y)
        if not self._moved and math.dist((x, y), self._press[:2]) < DRAG_THRESHOLD:
            return
        self._moved = True
        dx, dy = (x - self._last[0]) / w, (y - self._last[1]) / h
        self._last = (x, y)
        _, _, button, shift = self._press
        op = "pan" if shift or button == 1 or self.state.v_lock2d else "orbit"
        self._ops.append((op, dx, dy, 1.0))
        self._request("interactive")

    def mouse_up(self, x: float, y: float) -> None:
        press, self._press = self._press, None
        if press is None:
            return
        if self._moved:
            self._request("full")
            return
        if self.state.v_mode in ("probe", "line"):
            self._schedule(self._place(float(x), float(y)))

    def wheel(self, factor: float, nx: float = 0.5, ny: float = 0.5, seq: Any = 0) -> None:
        """A wheel or pinch over the image, as zoom.js measures it: > 1 zooms in, toward the point under the
        cursor at (nx, ny), 0..1 from the top left, which stays where it is."""
        self._seq = seq
        self._ops.append(zoom_op(float(factor), float(nx), float(ny)))
        self._request("interactive")
        if self._full_later is not None:
            self._full_later.cancel()
        self._full_later = asyncio.get_running_loop().call_later(FULL_AFTER, self._request, "full")

    # Probes and lines over time

    async def _place(self, x: float, y: float) -> None:
        size = (self.state.v_size or {}).get("size") or {}
        hit = await self._call(
            "pick_point",
            x=x,
            y=y,
            css_width=float(size.get("width") or 800),
            css_height=float(size.get("height") or 600),
        )
        if not hit:
            return
        point = [number(c) for c in hit["point"]]
        s = self.state
        if s.v_mode == "probe":
            s.v_point = point
            await self._probe()
        elif self._pending_p0 is None:
            self._pending_p0 = point
            with s:
                s.v_p0, s.v_p1 = point, ["", "", ""]
                s.v_line_text = "click the line's other end"
            await self._markers(line=([float(c) for c in point], None))
        else:
            with s:
                s.v_p1 = point
            self._pending_p0 = None
            await self._line()

    # The page sends what its fields hold with the click: edited state is held back while the client waits on
    # a call (a frame, the scene's arrays), and the click's trigger would reach this process before it.
    def probe(self, point: list[str] | None = None) -> None:
        if point is not None:
            self.state.v_point = list(point)
        self._schedule(self._probe())

    def line(self, p0: list[str] | None = None, p1: list[str] | None = None, times: str = "") -> None:
        if p0 is not None and p1 is not None:
            self.state.v_p0, self.state.v_p1 = list(p0), list(p1)
        if times:
            self.state.v_line_times = times
        self._schedule(self._line())

    def cancel(self) -> None:
        self._cancel = True

    def _indices(self, all_snapshots: bool) -> list[int]:
        count = int((self.state.v_info or {}).get("count", 0))
        if all_snapshots:
            return list(range(count))
        current = int(self.state.v_index)
        if self.state.v_line_times == "now" or current == 0:
            return [current]
        n = min(LINE_TIMES, current + 1)
        return sorted({round(k * current / (n - 1)) for k in range(n)})

    async def _probe(self) -> None:
        s = self.state
        try:
            point = [float(c) for c in s.v_point[: s.v_dim]]
        except (TypeError, ValueError):
            s.v_error = "probe: a point (x, y, z), or click on the view in Probe mode"
            return
        await self._markers(probe=point)
        info = dict(s.v_info or {})
        field, series = info.get("field", ""), s.v_primary
        indices = self._indices(True)
        t: list[float] = []
        values: list[float] = []
        self._cancel = False
        for k in range(0, len(indices), PROBE_BATCH):
            batch = indices[k : k + PROBE_BATCH]
            with s:
                s.v_busy = f"probe: {k} of {len(indices)} snapshots"
            got = await self._call("probe", series=series, point=point, indices=batch, fields=[field])
            if got is None:
                break
            for ti, v in zip(got["t"], got["values"].get(field, []), strict=False):
                if ti is not None and v is not None:
                    t.append(float(ti))
                    values.append(float(v))
            self._probe_series = (t, values, field, info)
            with s:
                self._draw_probe()
                s.v_probe_text = f"{field} at ({', '.join(number(c) for c in point)}) · {len(t)} snapshots"
            if self._cancel:
                break
        with s:
            s.v_busy = ""

    async def _line(self) -> None:
        s = self.state
        try:
            p0 = [float(c) for c in s.v_p0[: s.v_dim]]
            p1 = [float(c) for c in s.v_p1[: s.v_dim]]
        except (TypeError, ValueError):
            s.v_error = "line: two points, or click its ends on the view in Line mode"
            return
        await self._markers(line=(p0, p1))
        info = dict(s.v_info or {})
        field = info.get("field", "")
        with s:
            s.v_busy = "line: reading"
        got = await self._call("line", series=s.v_primary, p0=p0, p1=p1, indices=self._indices(False), field=field)
        with s:
            s.v_busy = ""
            if got is None:
                return
            curves = []
            for c in got["curves"]:
                pts = [(x, v) for x, v in zip(got["s"], c["values"], strict=False) if v is not None]
                label = f"t {number(c['t'])}" if c["t"] is not None else f"step {c['step']}"
                curves.append(Curve(label, [p[0] for p in pts], [p[1] for p in pts]))
            along = axis_label("s", self._unit("m"))
            s.v_line_plot = line_plot(curves, along, self._field_label(info))
            self.app.exports.remember("line", PlotSpec(curves, along, self._field_label(info), "line"))
            s.v_line_text = f"{field} along ({', '.join(map(number, p0))}) to ({', '.join(map(number, p1))})"

    def set_probe_kind(self, kind: str) -> None:
        self.state.v_probe_kind = kind
        self._draw_probe()

    def _draw_probe(self) -> None:
        """The probe over time, or its spectrum (Welch's PSD) with the peak frequency marked."""
        s = self.state
        if self._probe_series is None:
            return
        t, values, field, info = self._probe_series
        if s.v_probe_kind == "spectrum":
            try:
                sp = welch(t, values)
            except ValueError as e:
                s.v_probe_plot = line_plot([], axis_label("f", self._unit("Hz")), "PSD")
                s.v_geo_note = ""
                s.v_probe_text = f"spectrum: {e}"
                return
            units = next((f.get("units", "") for f in info.get("fields", []) if f["name"] == field), "")
            per_hz = "" if self._nondimensional else "Hz⁻¹"
            ylabel = axis_label(f"PSD of {field}", f"({units})² {per_hz}".strip() if units else per_hz)
            s.v_probe_plot = line_plot(
                [Curve(field, sp.f, sp.psd)],
                axis_label("f", self._unit("Hz")),
                ylabel,
                logx=True,
                logy=True,
                mark_x=sp.peak_f,
            )
            hz, sec = (" Hz", " s") if not self._nondimensional else ("", "")
            peak = f"peak {number(sp.peak_f)}{hz} · " if sp.peak_f else ""
            s.v_probe_text = f"{field} spectrum · {peak}{sp.segments} Welch segments, Δt {number(sp.dt)}{sec}"
            self.app.exports.remember(
                "probe",
                PlotSpec([Curve(field, sp.f, sp.psd)], axis_label("f", self._unit("Hz")), ylabel, "probe-spectrum"),
            )
        else:
            spec = PlotSpec([Curve(field, t, values)], self._t_label(), self._field_label(info), "probe")
            s.v_probe_plot = line_plot(spec.curves, spec.xlabel, spec.ylabel)
            self.app.exports.remember("probe", spec)

    # Saved views

    def _view_list(self, run: str) -> list[dict[str, Any]]:
        return [
            {"name": v.name, "series": v.series, "step": v.step, "field": v.field}
            for v in views(self.app.session.project)
            if v.run == run
        ]

    def save_view(self, name: str) -> None:
        self._schedule(self._save_view(str(name).strip()))

    async def _save_view(self, name: str) -> None:
        s = self.state
        info = s.v_info or {}
        camera = await self._call("get_camera") if self.batch is None else {}
        try:
            view = SavedView(
                name or f"view {len(s.v_views) + 1}",
                run=s.v_run,
                series=s.v_primary,
                step=info.get("step"),
                t=info.get("t"),
                zones=list(s.v_zones),
                field=info.get("field", ""),
                component=info.get("component", "magnitude"),
                colormap=info.get("colormap", DEFAULT),
                interpolation=info.get("interpolation", "cells"),
                range=[] if info.get("auto_range", True) else list(info.get("range") or []),
                camera=camera or {},
                section=dict(info.get("section") or {}),
                probe=[float(c) for c in s.v_point[: s.v_dim] if str(c).strip()] if any(s.v_point) else [],
                plot=s.v_probe_kind,
            )
            save_view(self.app.session.project, view)
        except (ValueError, OSError) as e:
            with s:
                s.v_error = str(e)
            return
        with s:
            s.v_views = self._view_list(s.v_run)
            s.v_view_name = ""

    def restore_view(self, name: str) -> None:
        view = next((v for v in views(self.app.session.project) if v.name == name), None)
        if view is not None:
            self._schedule(self._restore(view))

    async def _restore(self, view: SavedView) -> None:
        s = self.state
        series = self._series.get(view.series)
        if series is None:
            s.v_error = f"view {view.name}: no series {view.series} in this run"
            return
        snaps = series["snapshots"]
        index = len(snaps) - 1
        step = view.step
        if step is not None and any(x["step"] is not None for x in snaps):
            index = min(range(len(snaps)), key=lambda i: abs((snaps[i]["step"] or 0) - step))
        await self._show(view.series, index, view.zones)
        params: dict[str, Any] = {"field": view.field, "component": view.component, "colormap": view.colormap}
        params["interpolation"] = view.interpolation
        params |= {"range": view.range} if view.range else {"auto_range": True}
        await self._field(**params)
        if view.section:
            sec = view.section
            await self._section(
                clip=sec.get("clip"),
                slice=sec.get("slice"),
                origin=sec.get("origin"),
                normal=sec.get("normal"),
                flipped=sec.get("flipped"),
            )
            with s:
                s.v_clip, s.v_slice = bool(sec.get("clip")), bool(sec.get("slice"))
        if view.camera:
            await self._call("set_camera", **view.camera)
        with s:
            s.v_probe_kind = view.plot
        if view.probe:
            with s:
                s.v_point = [number(c) for c in view.probe] + [""] * (3 - len(view.probe))
            await self._probe()
        self._request("full")

    def delete_view(self, name: str) -> None:
        delete_view(self.app.session.project, name)
        self.state.v_views = self._view_list(self.state.v_run)

    # Post-processing jobs where the run is

    def _snapshot_path(self, index: int) -> tuple[str, int | None, float | None]:
        snap = self._series[self.state.v_primary]["snapshots"][index]
        return str(snap["path"]), snap["step"], snap["t"]

    def _submit(self, kind: str, args: list[str]) -> None:
        s = self.state
        record = self.app.session.project.run(s.v_run)
        loop = asyncio.get_running_loop()
        try:
            launcher, cwd = self.app.session.post_launcher(record)
        except (ValueError, KeyError) as e:
            s.v_error = str(e)
            return

        def done(job: PostJob) -> None:
            loop.call_soon_threadsafe(self._job_done, job)

        self.app.session.postjobs.submit(PostJob(kind, s.v_run, "", args), launcher, cwd, done=done)
        self._list_jobs()

    def _list_jobs(self) -> None:
        jobs = [j.to_dict() for j in self.app.session.postjobs.jobs.values() if j.run == self.state.v_run]
        self.state.v_jobs = list(reversed(jobs))

    def _job_done(self, job: PostJob) -> None:
        with self.state:
            self._list_jobs()
        if job.state == "done" and job.kind in ("average", "extract"):
            self._schedule(self._rescan(f"{job.kind}/{job.args[job.args.index('--name') + 1]}"))

    async def _rescan(self, show: str = "") -> None:
        opened = await self._call("rescan")
        if opened is not None:
            await self._apply_series(opened, show)

    def diff_with(self, other: Any) -> None:
        try:
            j = int(other)
        except (TypeError, ValueError):
            return
        a, _, _ = self._snapshot_path(int(self.state.v_index))
        b, _, _ = self._snapshot_path(j)
        field = (self.state.v_info or {}).get("field", "")
        self._submit("diff", diff_args(a, b, [field] if field else []))

    def average(self, t0: Any, t1: Any) -> None:
        snaps = self._series[self.state.v_primary]["snapshots"]
        try:
            lo = float(t0) if str(t0).strip() else min(x["t"] for x in snaps)
            hi = float(t1) if str(t1).strip() else max(x["t"] for x in snaps)
        except (TypeError, ValueError):
            self.state.v_error = f"average from {t0!r} to {t1!r}: two times"
            return
        files = list(dict.fromkeys(str(x["path"]) for x in snaps if x["t"] is not None and lo <= x["t"] <= hi))
        if not files:
            self.state.v_error = f"no snapshot with {lo} <= t <= {hi}"
            return
        name = f"{self.state.v_primary.rsplit('/', 1)[-1]}_mean"
        self._submit("average", average_args(files, lo, hi, name=name))

    def extract_slice(self) -> None:
        s = self.state
        path, step, _ = self._snapshot_path(int(s.v_index))
        sec = (s.v_info or {}).get("section") or {}
        field = (s.v_info or {}).get("field", "")
        region = plane_region(sec.get("origin", [0, 0, 0]), sec.get("normal", [0, 0, 1]))
        name = f"slice_{step if step is not None else s.v_index}"
        self._submit("extract", extract_args(path, step, region, [field] if field else [], name=name))

    def export_surface(self) -> None:
        self._schedule(self._export())

    async def _export(self) -> None:
        step = (self.state.v_info or {}).get("step")
        got = await self._call("export_surface", name=f"surface_{step if step is not None else 'now'}")
        if got is not None:
            with self.state:
                self.state.v_busy = ""
                self.state.v_geo_note = (
                    f"wrote {got['path']} ({number(got['bytes'] / 1e6)} MB, {got['triangles']} triangles)"
                )
            await self._rescan()

    def cancel_job(self, job_id: str) -> None:
        self.app.session.postjobs.cancel(job_id)
        self._list_jobs()

    async def _markers(
        self, probe: list[float] | None = None, line: tuple[list[float], list[float] | None] | None = None
    ) -> None:
        """Mark on the view where the probe or line being plotted is: the probe's point, the line's ends."""
        points: list[Any] = [["probe", probe]] if probe else []
        lines: list[Any] = []
        if line:
            points += [["p0", line[0]]] + ([["p1", line[1]]] if line[1] else [])
            lines = [[line[0], line[1]]] if line[1] else []
        await self._plain("set_markers", points=points, lines=lines)

    def _field_label(self, info: dict[str, Any]) -> str:
        meta: dict[str, Any] = next((f for f in info.get("fields", []) if f["name"] == info.get("field")), {})
        name = info.get("field", "")
        if meta.get("components", 1) > 1 and info.get("component") != "magnitude":
            name += f"_{str(info.get('component', '')).upper()}"
        return axis_label(name, meta.get("units", ""))

    def _run_nondimensional(self, where: str, run_dir: str) -> bool:
        """Whether the run's own input is nondimensional: its probe files' units line says s and m whatever the
        run's units (Mallard #1363), so the plots go by the input."""
        from ..data.locations import join

        try:
            text = self.app.session.data.location(where).read_bytes(join(run_dir, "input.toml"))
            data = tomllib.loads(text.decode("utf-8", "replace"))
        except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError):
            return False
        return (data.get("physics") or {}).get("units") == "nondimensional"

    def _unit(self, unit: str) -> str:
        return "" if self._nondimensional else unit

    def _t_label(self) -> str:
        return axis_label("t", self._unit("s"))

    # Layout

    def layout(self) -> None:
        self._left()
        with html.Div(classes="center"):
            self._toolbar()
            with html.Div(classes="views", v_show="v_run"):
                with html.Div(classes="viewport results-view"), client.SizeObserver("v_size"):
                    geo = vtkw.VtkLocalView(
                        self.geo.window,
                        ref="geoview",
                        v_show="v_geometry",
                        interactor_settings=("v_lock2d ? caseview_pan : caseview_orbit",),
                        interactor_events=("view_events", ["EndAnimation"]),
                        EndAnimation=(self.on_geo_camera, "[trame.refs.geoview.getCamera()]"),
                        style="position: absolute; inset: 0;",
                    )
                    self.app.ctrl.geo_update = geo.update
                    self._geo_view = geo
                    html.Img(
                        v_if="v_frame && !v_geometry",
                        src=("v_frame",),
                        classes="frame",
                        alt="the rendered view",
                        draggable="false",
                        mousedown="$event.preventDefault(); trigger('v_down', [...window.mallardZoom.at($event), "
                        "$event.button, $event.shiftKey])",
                        mouseup="trigger('v_up', window.mallardZoom.at($event))",
                        mouseleave="$event.buttons && trigger('v_up', window.mallardZoom.at($event))",
                        contextmenu_prevent="",
                        __events=[("contextmenu_prevent", "contextmenu.prevent")],
                        style=("{cursor: v_mode === 'orbit' ? 'grab' : 'crosshair'}",),
                    )
                    section_bar(
                        "v_",
                        self.set_clip_axis,
                        (self.toggle, "['flip']"),
                        self.set_section_offset,
                        (self.toggle, "['mark']"),
                        (self.toggle, "['section_off']"),
                        shown="v_clip || v_slice",
                        dim="v_dim",
                        what="Clip and Slice",
                    )
                    html.Div(
                        classes="triad",
                        v_if="v_info.count",
                        __properties=[("data_view", "data-view")],
                        data_view="results",
                    )
                    with html.Div(classes="colorbar", v_if="v_colorbar.css"):
                        html.Div("{{ v_colorbar.label }}", classes="cb-label")
                        html.Div(classes="cb-bar", style=("{background: v_colorbar.css}",))
                        with html.Div(classes="cb-ticks"):
                            html.Span(
                                "{{ t.text }}",
                                v_for="t in v_colorbar.ticks",
                                key="t.pos",
                                style=("{left: t.pos + '%'}",),
                            )
                    with html.Div(classes="colorbar pcolorbar", v_if="v_pcolorbar.css && v_particles.length"):
                        html.Div("{{ v_pcolorbar.label }}", classes="cb-label")
                        html.Div(classes="cb-bar", style=("{background: v_pcolorbar.css}",))
                        with html.Div(classes="cb-ticks"):
                            html.Span(
                                "{{ t.text }}",
                                v_for="t in v_pcolorbar.ticks",
                                key="t.pos",
                                style=("{left: t.pos + '%'}",),
                            )
                    with html.Div(classes="legend", v_if="(v_info.blocks || []).filter(b => b.kind === 'zone').length"):
                        with html.Div(v_for="b in v_info.blocks.filter(b => b.kind === 'zone')", key="b.name"):
                            html.Span(classes="swatch", style=("{background: b.color}",))
                            html.Span("{{ b.label }}")
                            html.Span("zone surface", classes="muted")
                    with html.Div(classes="readout", v_show="!v_split"):  # the hint crowds a half view
                        html.Div(
                            "{{ v_mode === 'probe' ? 'click: probe over time' : v_mode === 'line' ? "
                            "'click both ends: line over time' : (v_lock2d ? 'drag pan' : 'drag orbit') + "
                            "' · shift pan · wheel zoom' }}",
                            style="font-family: var(--m-font-sans)",
                        )
                    with html.Div(classes="view-notice", v_if="v_info.notice"):
                        html.Span("{{ v_info.notice }}")
                        with html.Span(v_if="v_whole.text", classes="whole"):
                            html.Span(" · whole, by a parallel viewer job: {{ v_whole.text }}")
                            html.Button("Start it", classes="btn", click=self.use_parallel, v_if="v_whole.ranks")
                    with html.Div(classes="unavailable", v_if="v_unavailable.length"):
                        html.Div("No viewer could start next to the data. Tried:", classes="ua-head")
                        with html.Div(v_for="a in v_unavailable", key="a.launch", classes="ua-row"):
                            html.Span("{{ a.launch }}", classes="mono")
                            html.Span("{{ a.why }}", classes="muted")
                        with html.Div(classes="ua-actions"):
                            with html.Button(
                                classes="btn",
                                click=self.use_login,
                                title="Login nodes are shared and small; runs there are cut short",
                            ):
                                html.Span(v_html="icons.server")
                                html.Span("Use the login node")
                            with html.Button(classes="btn", click=self.use_batch, title="Each image is a batch job"):
                                html.Span(v_html="icons.camera")
                                html.Span("Batch images")
                    html.Div(
                        "{{ v_error || v_status || 'opening ...' }}",
                        classes=("['empty', {err: v_error}]",),
                        v_if="(!v_frame || v_error || v_status) && !v_unavailable.length",
                    )
                with html.Div(classes="viewport split-view", v_if="v_split"):
                    html.Img(
                        v_if="v_frame2",
                        src=("v_frame2",),
                        classes="frame",
                        alt="the view colored by the second field",
                        draggable="false",
                    )
                    with html.Div(classes="split-head"):
                        with html.Select(aria_label="second field", change=(self.set_field2, "[$event.target.value]")):
                            html.Option(
                                "{{ f.name }}",
                                v_for="f in v_info.fields || []",
                                key="f.name",
                                value=("f.name",),
                                selected=("f.name === v_field2",),
                            )
                        with html.Select(
                            aria_label="second component",
                            v_if="((v_info.fields || []).find(f => f.name === v_field2) || {}).components > 1",
                            change=(self.set_comp2, "[$event.target.value]"),
                        ):
                            html.Option(
                                "{{ c }}",
                                v_for="c in v_components.slice(0, v_dim + 1)",
                                key="c",
                                value=("c",),
                                selected=("c === v_comp2",),
                            )
                    with html.Div(classes="colorbar", v_if="v_colorbar2.css"):
                        html.Div("{{ v_colorbar2.label }}", classes="cb-label")
                        html.Div(classes="cb-bar", style=("{background: v_colorbar2.css}",))
                        with html.Div(classes="cb-ticks"):
                            html.Span(
                                "{{ t.text }}",
                                v_for="t in v_colorbar2.ticks",
                                key="t.pos",
                                style=("{left: t.pos + '%'}",),
                            )
                    html.Div("{{ v_split_note || 'rendering ...' }}", classes="empty", v_if="!v_frame2")
            empty_state(
                "results",
                "No results yet",
                "Snapshots a run writes open here for analysis: fields, probes and lines over time, with images "
                "and GIF or MP4 animations of the view. Open in ParaView hands it the view for heavier rendering.",
                "!v_run && !runs.length",
                ("Go to Case setup", "case", (self.app.set_page, "['case']")),
            )
            empty_state(
                "results",
                "No run selected",
                "Pick a run on the left: its snapshots open where the run is, on this machine or a remote.",
                "!v_run && runs.length",
            )
            self._timebar()
            self._plots()
        self._right()

    def _left(self) -> None:
        with html.Div(classes="panel"):
            splitter("results-left", "the run list", minimum=180, maximum=640)
            with html.Div(classes="panel-head"):
                html.Span("Results")
                html.Span("{{ v_where }}", classes="mono")
            with html.Div(classes="scroll", style=list_style("results-runs", "34%")):
                with html.Div(
                    v_for="r in runs",
                    key="r.name",
                    classes=("['run-row', {sel: r.name === v_run}]",),
                    click=(self.select_run, "[r.name]"),
                ):
                    html.Span(v_html="r.remote === 'local' ? icons.local : icons.server", classes="ticon")
                    html.Span("{{ r.name }}")
                    html.Span("{{ r.remote }}", classes="rv")
                html.Div("No runs yet", classes="notice", v_if="!runs.length")
            list_splitter("results-runs", "the run list")
            with html.Div(classes="panel-head", v_if="v_series.length"):
                html.Span("Series")
                html.Span("snapshots", classes="mono")
            with html.Div(classes="scroll", style=list_style("results-series", "30%"), v_if="v_series.length"):
                with html.Div(
                    v_for="x in v_series.filter(x => x.kind !== 'zone' && x.kind !== 'particles')",
                    key="x.id",
                    classes=("['tree-row', 'child', {sel: x.id === v_primary}]",),
                    style="padding-left: 8px",
                    click=(self.select_series, "[x.id]"),
                    title=("x.id",),
                ):
                    html.Span("{{ x.label }}", classes="tlabel")
                    html.Span("{{ x.kind }}", classes="tval", style="font-family: var(--m-font-sans)")
                    html.Span("{{ x.count }}", classes="tval", style="margin-left: 8px; min-width: 24px")
            list_splitter("results-series", "the series list", v_if="v_series.length")
            with html.Div(classes="panel-head", v_if="v_series.some(x => x.kind === 'zone')"):
                html.Span("Zone surfaces")
                html.Span("at the same time", classes="mono")
            with html.Div(classes="scroll", style="flex: none", v_if="v_series.some(x => x.kind === 'zone')"):
                with html.Div(
                    v_for="x in v_series.filter(x => x.kind === 'zone')",
                    key="x.id",
                    classes="tree-row child",
                    style="padding-left: 8px",
                    click=(self.toggle_zone, "[x.id]"),
                ):
                    html.Input(
                        type="checkbox",
                        checked=("v_zones.includes(x.id)",),
                        aria_label=("'show ' + x.label",),
                        style="width: auto; height: auto; margin: 0 6px 0 0",
                    )
                    html.Span("{{ x.label }}", classes="tlabel")
                    html.Span("{{ x.region }}", classes="tval")
            self._particles_list()
            with html.Div(classes="panel-head", v_if="v_info.series"):
                html.Span("Snapshot")
                html.Span("{{ v_info.index + 1 }} of {{ v_info.count }}", classes="mono")
            with html.Div(classes="scroll", v_if="v_info.series"):
                for k, v in (
                    ("step", "{{ v_info.step }}"),
                    ("t", "{{ typeof v_info.t === 'number' ? +v_info.t.toPrecision(6) : '–' }}"),
                    ("cells", "{{ (v_info.cells || 0).toLocaleString('en-US') }}"),
                    ("triangles", "{{ (v_info.triangles || 0).toLocaleString('en-US') }}"),
                    ("in memory", "{{ ((v_info.cached_bytes || 0) / 1048576).toFixed(1) }} MB"),
                ):
                    with html.Div(classes="prow"):
                        html.Div(k, classes="plabel")
                        html.Div(v, classes="pval ro")

    def _particles_list(self) -> None:
        shown = "v_info.particles && v_series.some(x => x.kind === 'particles')"
        with html.Div(classes="panel-head", v_if=shown):
            html.Span("Particles")
            html.Span(
                "{{ v_info.particles.step == null || v_info.particles.step === v_info.step ? 'at the same time' : "
                "'at step ' + v_info.particles.step }}",
                classes="mono",
                title="The particle file is written at the end of the run",
            )
        with html.Div(classes="scroll", style="flex: none", v_if=shown):
            with html.Div(
                v_for="x in v_series.filter(x => x.kind === 'particles')",
                key="x.id",
                classes="tree-row child",
                style="padding-left: 8px",
                click=(self.toggle_particles, "[x.id]"),
            ):
                html.Input(
                    type="checkbox",
                    checked=("v_particles.includes(x.id)",),
                    aria_label=("'show ' + x.label",),
                    style="width: auto; height: auto; margin: 0 6px 0 0",
                )
                html.Span("{{ x.label }}", classes="tlabel")
                html.Span(
                    "{{ v_particles.includes(x.id) ? v_info.particles.count.toLocaleString('en-US') : '' }}",
                    classes="tval",
                )
            html.Div(
                "Inside the volume: X-ray or Clip shows them",
                classes="notice",
                v_if="v_particles.length && v_dim === 3 && !v_xray && !v_clip",
            )
            with html.Div(classes="prow", v_if="v_particles.length"):
                html.Div("color", classes="plabel")
                with html.Div(classes="pval"):
                    with html.Select(
                        aria_label="particle color", change=(self.set_particle_field, "[$event.target.value]")
                    ):
                        html.Option("one color", value="", selected=("!v_info.particles.field",))
                        html.Option(
                            "{{ f }}",
                            v_for="f in v_info.particles.fields",
                            key="f",
                            value=("f",),
                            selected=("f === v_info.particles.field",),
                        )

    def _toolbar(self) -> None:
        with html.Div(classes="toolbar", v_show="v_run"):
            for name in ("+X", "-X", "+Y", "-Y", "+Z", "-Z", "iso"):
                html.Button(
                    name.replace("-", "−").replace("iso", "Iso"),
                    classes=(f"v_preset === '{name}' ? 'tb mono on' : 'tb mono'",),
                    click=(self.preset, f"['{name}', window.mallardZoom.camera('geoview')]"),
                    disabled=("!v_frame",),
                    title=f"View {name}",
                )
            html.Div(classes="tb-gap")
            html.Button(
                "Ortho",
                classes=("v_parallel ? 'tb on' : 'tb'",),
                click=(self.toggle, "['parallel', window.mallardZoom.camera('geoview')]"),
            )
            html.Button(
                "2D lock",
                classes=("v_lock2d ? 'tb on' : 'tb'",),
                click=(self.toggle, "['lock2d', window.mallardZoom.camera('geoview')]"),
                title="Drags pan and never rotate; a 2D case is turned back to its plane",
            )
            html.Div(classes="tb-gap")
            with html.Button(classes=("v_clip ? 'tb on' : 'tb'",), click=(self.toggle, "['clip']"), title="Clip plane"):
                html.Span(v_html="icons.clip")
                html.Span("Clip")
            with html.Button(
                classes=("v_slice ? 'tb on' : 'tb'",),
                click=(self.toggle, "['slice']"),
                title="The cut through the volume on the plane, computed where the data is",
            ):
                html.Span(v_html="icons.slice")
                html.Span("Slice")
            with html.Button(
                classes=("v_xray ? 'tb on' : 'tb'",), click=(self.toggle, "['xray']"), title="Surfaces translucent"
            ):
                html.Span(v_html="icons.xray")
                html.Span("X-ray")
            html.Button(
                "Compare",
                classes=("v_split ? 'tb on' : 'tb'",),
                click=self.set_split,
                disabled=("!v_frame || v_batch",),
                title="A second view beside this one: the same camera, colored by another field",
            )
            html.Div(classes="tb-gap")
            with html.Button(
                classes=("v_mode === 'probe' ? 'tb on' : 'tb'",),
                click=(self.toggle, "['probe']"),
                title="Click a point: its values over time",
            ):
                html.Span(v_html="icons.probe")
                html.Span("Probe")
            with html.Button(
                classes=("v_mode === 'line' ? 'tb on' : 'tb'",),
                click=(self.toggle, "['line']"),
                title="Click two points: the field along the line, over time",
            ):
                html.Span(v_html="icons.line")
                html.Span("Line")
            html.Div(classes="grow")
            self.app.exports.toolbar_button()
            html.Button(
                v_html="icons.frame",
                classes="icon-btn",
                click=(self.focus, "[window.mallardZoom.camera('geoview')]"),
                title="Frame the zone surfaces shown  ⇧F",
                aria_label="Frame the zone surfaces shown",
                disabled=("!v_zones.length",),
            )
            html.Button(
                v_html="icons.camera",
                classes="icon-btn",
                click=(self.save_image, "[window.mallardZoom.camera('geoview')]"),
                title="Save the view as a PNG image",
                disabled=("!v_info.count || v_batch",),
            )
            html.Button(
                v_html="icons.fit",
                classes="icon-btn",
                click=(self.fit, "[window.mallardZoom.camera('geoview')]"),
                title="Fit the whole domain to the view  F",
                aria_label="Fit the whole domain to the view",
            )

    def _timebar(self) -> None:
        with html.Div(classes="timebar", v_if="v_info.count"):
            for icon, where, title in (
                ("first", "first", "First snapshot"),
                ("prev", "prev", "Previous snapshot"),
            ):
                html.Button(
                    v_html=f"icons.{icon}", classes="icon-btn", title=title, click=(self.step_to, f"['{where}']")
                )
            html.Input(
                type="range",
                min="0",
                max=("v_info.count - 1",),
                value=("v_index",),
                aria_label="snapshot",
                input="Date.now() - ($event.target.dataset.t || 0) > 120 && "
                "($event.target.dataset.t = Date.now(), trigger('v_scrub', [$event.target.value]))",
                change="trigger('v_scrub', [$event.target.value])",
                classes="scrub",
            )
            for icon, where, title in (("next", "next", "Next snapshot"), ("last", "last", "Last snapshot")):
                html.Button(
                    v_html=f"icons.{icon}", classes="icon-btn", title=title, click=(self.step_to, f"['{where}']")
                )
            html.Span(
                "step {{ v_info.step }} · t {{ typeof v_info.t === 'number' ? +v_info.t.toPrecision(4) : '–' }}",
                classes="mono time-text",
            )

    def _plots(self) -> None:
        with html.Div(classes="plots results-plots", v_if="v_info.count"):
            splitter("results-plots", "the plots", axis="y", edge="start", minimum=100, maximum=900, keep=160)
            with html.Div(classes="plot"):
                with html.Div(classes="plot-head"):
                    html.Span("{{ v_probe_text || 'Probe over time' }}", classes="grow")
                    for kind in ("time", "spectrum"):
                        html.Button(
                            kind,
                            classes=(f"v_probe_kind === '{kind}' ? 'tb on' : 'tb'",),
                            click=(self.set_probe_kind, f"['{kind}']"),
                            title="The probe over time" if kind == "time" else "Its power spectral density (Welch)",
                        )
                    self.app.exports.buttons("probe")
                html.Div(v_html="v_probe_plot", v_if="v_probe_plot")
                html.Div(
                    "Probe mode, then click the view; or type a point on the right.",
                    classes="notice",
                    v_if="!v_probe_plot",
                )
            with html.Div(classes="plot"):
                with html.Div(classes="plot-head"):
                    html.Span("{{ v_line_text || 'Line over time' }}")
                    self.app.exports.buttons("line")
                html.Div(v_html="v_line_plot", v_if="v_line_plot")
                html.Div(
                    "Line mode, then click both ends; or type them on the right.",
                    classes="notice",
                    v_if="!v_line_plot",
                )

    def _right(self) -> None:
        with html.Div(classes="panel right"):
            splitter("results-right", "the field panel", edge="start", minimum=240, maximum=760)
            empty_state(
                "results",
                "Field, probes and export",
                "The field shown, its colormap and range, probes and lines over time, and Open in ParaView, for "
                "the run you pick.",
                "!v_run",
                compact=True,
            )
            with html.Div(classes="panel-head", v_show="v_run"):
                html.Span("Field")
                html.Span("{{ v_info.field }}", classes="mono")
            with html.Div(classes="scroll", v_show="v_run"):
                with html.Div(classes="prow"):
                    html.Div("field", classes="plabel")
                    with html.Div(classes="pval"):
                        with html.Select(aria_label="field", change=(self.set_field, "[$event.target.value]")):
                            html.Option(
                                "{{ f.name }}{{ f.long_name ? ' · ' + f.long_name : '' }}",
                                v_for="f in v_info.fields || []",
                                key="f.name",
                                value=("f.name",),
                                selected=("f.name === v_info.field",),
                            )
                with html.Div(
                    classes="prow",
                    v_if="((v_info.fields || []).find(f => f.name === v_info.field) || {}).components > 1",
                ):
                    html.Div("component", classes="plabel")
                    with html.Div(classes="pval"):
                        with html.Select(aria_label="component", change=(self.set_component, "[$event.target.value]")):
                            html.Option(
                                "{{ c }}",
                                v_for="c in v_components.slice(0, v_dim + 1)",
                                key="c",
                                value=("c",),
                                selected=("c === v_info.component",),
                            )
                with html.Div(
                    classes="prow",
                    title="How the field is rendered. Probes, lines and statistics always use the cell values "
                    "Mallard wrote, whichever is shown.",
                ):
                    html.Div("rendering", classes="plabel")
                    with html.Div(classes="pval"):
                        with html.Div(classes="segmented interp", role="group", aria_label="rendering"):
                            for key, label, tip in INTERPOLATION_CHOICES:
                                html.Button(
                                    label,
                                    classes=(f"['seg', {{on: (v_info.interpolation || 'cells') === '{key}'}}]",),
                                    title=tip,
                                    aria_pressed=(f"String((v_info.interpolation || 'cells') === '{key}')",),
                                    __properties=[("aria_pressed", "aria-pressed")],
                                    click=(self.set_interpolation, f"['{key}']"),
                                )
                with html.Div(classes="prow"):
                    html.Div("colormap", classes="plabel")
                    with html.Div(classes="pval"):
                        dropdown(
                            "v_info.colormap",
                            "v_cmaps",
                            (self.set_colormap, "[$event.detail]"),
                            "colormap",
                            classes="cmap-dd",
                            swatch="(v_cmaps.find(c => c.value === v_info.colormap) || {}).swatch",
                        )
                with html.Div(classes="prow"):
                    html.Div("range", classes="plabel")
                    with html.Div(classes="pval vec range"):
                        html.Input(
                            value=("v_min",),
                            aria_label="range min",
                            change=(self.set_range, "[$event.target.value, v_max]"),
                            keydown_enter="$event.target.blur()",
                        )
                        html.Input(
                            value=("v_max",),
                            aria_label="range max",
                            change=(self.set_range, "[v_min, $event.target.value]"),
                            keydown_enter="$event.target.blur()",
                        )
                with html.Div(classes="prow"):
                    html.Div("", classes="plabel")
                    with html.Div(classes="pval", style="padding: 0 8px; gap: 6px"):
                        html.Input(
                            type="checkbox",
                            checked=("v_info.auto_range",),
                            aria_label="range from this snapshot",
                            change=(self.set_auto, "[$event.target.checked]"),
                            style="width: auto; height: auto",
                        )
                        html.Span("per snapshot (else fixed in time)", classes="muted")
                with html.Div(classes="prow"):
                    html.Div("slice", classes="plabel")
                    html.Div(
                        "{{ v_slice ? ((v_info.section || {}).slice_cells || 0).toLocaleString('en-US')"
                        " + ' cells cut, where the data is' : 'off' }}",
                        classes="pval ro muted",
                        style="font-family: var(--m-font-sans)",
                    )
                self._stats_section()
                with html.Div(classes="group-head"):
                    html.Span("PROBE AND LINE")
                    html.Span("over time", classes="note")
                for label, var in (("point", "v_point"), ("from", "v_p0"), ("to", "v_p1")):
                    with html.Div(classes="prow"):
                        html.Div(label, classes="plabel")
                        with html.Div(classes="pval vec"):
                            for i in range(3):
                                html.Input(
                                    v_if="v_dim > 2" if i == 2 else None,
                                    value=(f"{var}[{i}]",),
                                    aria_label=f"{label} {'xyz'[i]}",
                                    change=f"{var} = {var}.map((v, j) => j === {i} ? $event.target.value : v)",
                                )
                with html.Div(classes="prow"):
                    html.Div("line at", classes="plabel")
                    with html.Div(classes="pval", style="gap: 3px; padding: 0 6px"):
                        html.Button(
                            "now",
                            classes=("v_line_times === 'now' ? 'tb on' : 'tb'",),
                            click="v_line_times = 'now'",
                        )
                        html.Button(
                            f"{LINE_TIMES} times to now",
                            classes=("v_line_times === 'spread' ? 'tb on' : 'tb'",),
                            click="v_line_times = 'spread'",
                        )
                self.app.exports.layout_results_rows()
                with html.Div(classes="group-head"):
                    html.Span("VIEWER")
                    html.Span("next to the data", classes="note")
                with html.Div(classes="prow"):
                    html.Div("where", classes="plabel")
                    html.Div("{{ v_viewer }}", classes="pval ro", title=("v_viewer",))
                with html.Div(classes="prow"):
                    html.Div("render", classes="plabel")
                    html.Div(
                        "{{ v_render.renderer ? (v_render.gpu ? 'GPU · ' : 'CPU · ') + v_render.renderer : '–' }}",
                        classes="pval ro",
                        title=("v_render.window",),
                    )
                with html.Div(classes="prow"):
                    html.Div("mode", classes="plabel")
                    with html.Div(classes="pval", style="gap: 3px; padding: 0 6px"):
                        for mode, title in (
                            ("auto", "Geometry when round trips are long and the surfaces fit"),
                            ("images", "Images rendered next to the data"),
                            ("geometry", "Decimated surfaces drawn in the browser (vtk.js)"),
                        ):
                            html.Button(
                                mode,
                                classes=(f"v_render_mode === '{mode}' ? 'tb on' : 'tb'",),
                                click=(self.set_render_mode, f"['{mode}']"),
                                title=title,
                                disabled=("v_batch",),
                            )
                note_row("", "{{ v_geo_note || (v_geometry ? 'geometry' : 'images; the coarse level while moving') }}")
                with html.Div(classes="prow", v_if="v_meta.size"):
                    html.Div("last frame", classes="plabel")
                    html.Div(
                        "{{ v_meta.triangles }} triangles · {{ v_meta.quality }} · {{ v_meta.ms }} ms render · "
                        "{{ v_meta.rtt }} ms round trip",
                        classes="pval ro mono",
                        aria_label="last frame",
                    )
                with html.Div(classes="prow"):
                    html.Div("budget", classes="plabel")
                    html.Div(
                        "{{ v_info.budget ? (v_info.budget.volume_cells / 1e6) + ' M cells · ' + "
                        "(v_info.budget.triangles / 1e6) + ' M triangles' : '' }}",
                        classes="pval ro",
                    )
                self._saved_views()
                self._jobs()
            with html.Div(classes="actions", v_show="v_run"):
                html.Span("{{ v_busy }}", classes="muted grow", style="font-size: 11px")
                with html.Button(classes="btn", click=self.cancel, disabled=("!v_busy",)):
                    html.Span(v_html="icons.stop")
                    html.Span("Cancel")
                with html.Button(
                    classes="btn",
                    click=(self.line, "[v_p0, v_p1, v_line_times]"),
                    disabled=("!v_info.count || !!v_busy",),
                ):
                    html.Span(v_html="icons.line")
                    html.Span("Line")
                with html.Button(
                    classes="btn primary", click=(self.probe, "[v_point]"), disabled=("!v_info.count || !!v_busy",)
                ):
                    html.Span(v_html="icons.probe")
                    html.Span("Probe")

    def _saved_views(self) -> None:
        with html.Div(classes="group-head"):
            html.Span("SAVED VIEWS")
            html.Span("in the project file", classes="note")
        with html.Div(classes="prow"):
            html.Div("name", classes="plabel")
            with html.Div(classes="pval", style="gap: 4px; padding-right: 6px"):
                html.Input(
                    value=("v_view_name",),
                    aria_label="view name",
                    placeholder="view name",
                    input="v_view_name = $event.target.value",
                    keydown_enter=(self.save_view, "[v_view_name]"),
                )
                html.Button("Save", classes="tb", click=(self.save_view, "[v_view_name]"), disabled=("!v_info.count",))
        with html.Div(v_for="v in v_views", key="v.name", classes="tree-row child", style="padding-left: 8px"):
            html.Span("{{ v.name }}", classes="tlabel", click=(self.restore_view, "[v.name]"), title="Restore")
            html.Span("{{ v.field }} · step {{ v.step }}", classes="tval")
            html.Button(v_html="icons.trash", classes="icon-btn", click=(self.delete_view, "[v.name]"), title="Delete")

    def _jobs(self) -> None:
        with html.Div(classes="group-head"):
            html.Span("COMPARE, AVERAGE, EXTRACT")
            html.Span("jobs where the run is", classes="note")
        with html.Div(classes="prow"):
            html.Div("diff with", classes="plabel")
            with html.Div(classes="pval", style="gap: 4px; padding-right: 6px"):
                with html.Select(aria_label="snapshot to compare", change="v_diff_with = +$event.target.value"):
                    html.Option(
                        "step {{ x.step }} · t {{ typeof x.t === 'number' ? +x.t.toPrecision(4) : '–' }}",
                        v_for="(x, i) in v_snaps",
                        key="i",
                        value=("i",),
                        selected=("i === v_diff_with",),
                    )
                html.Button(
                    "Diff",
                    classes="tb",
                    click=(self.diff_with, "[v_diff_with]"),
                    disabled=("!v_info.count || v_batch",),
                    title="mallard diff of this snapshot and that one",
                )
        with html.Div(classes="prow"):
            html.Div("average t", classes="plabel")
            with html.Div(classes="pval vec range", style="padding-right: 6px"):
                for i, hint in enumerate(("from", "to")):
                    html.Input(
                        value=(f"v_avg_t[{i}]",),
                        aria_label=f"average {hint}",
                        placeholder=hint,
                        change=f"v_avg_t = v_avg_t.map((v, j) => j === {i} ? $event.target.value : v)",
                    )
                html.Button(
                    "Average",
                    classes="tb",
                    click=(self.average, "[v_avg_t[0], v_avg_t[1]]"),
                    disabled=("!v_info.count || v_batch",),
                    title="mallard post average over the window, as a job",
                )
        with html.Div(classes="prow"):
            html.Div("extract", classes="plabel")
            with html.Div(classes="pval", style="gap: 3px; padding: 0 6px"):
                html.Button(
                    "Slice",
                    classes="tb",
                    click=self.extract_slice,
                    disabled=("!v_info.count || v_batch",),
                    title="mallard post extract of the section plane at this step: a small snapshot to view",
                )
                html.Button(
                    "Surfaces",
                    classes="tb",
                    click=self.export_surface,
                    disabled=("!v_info.count || v_batch",),
                    title="The shown surfaces, decimated, written next to the data to view or copy",
                )
        with html.Div(
            v_for="j in v_jobs",
            key="j.id",
            classes="job-row",
            title=("j.command",),
            __properties=COPY_PROPS,
            data_copy=("j.state === 'failed' ? j.kind + ' failed: ' + (j.error || '') + '\\n' + j.command : ''",),
        ):
            html.Span("{{ j.kind }}", classes="mono")
            html.Span(
                "{{ j.state === 'running' ? 'running on ' + j.where : j.state === 'done' ? j.summary : "
                "j.state + (j.error ? ': ' + j.error : '') }}",
                classes=("['job-text', {err: j.state === 'failed'}]",),
            )
            copy_button(v_if="j.state === 'failed'")
            html.Button(
                v_html="icons.stop",
                classes="icon-btn",
                v_if="j.state === 'running'",
                click=(self.cancel_job, "[j.id]"),
                title="Cancel the job",
            )


def _unsupported(error: str) -> bool:
    """A viewer without the method: an older one here ("unknown method") or the parallel one ("has no")."""
    return error.startswith(("unknown method ", "the parallel viewer has no "))


def stats_view(got: dict[str, Any], colormap: str, units: str, dim: int) -> dict[str, Any]:
    """The statistics section's rows and histogram, from the viewer's `stats`: each place (the domain, the slice)
    with its figures, where the extremes are (to probe there) and its histogram in the colormap's colors."""
    if got.get("error"):
        return {"error": got["error"]}
    unit = f" {units}" if units and units != "1" else ""
    places = {}
    for where in ("domain", "slice"):
        s = got.get(where)
        if not s or "mean" not in s:
            continue
        sdim = dim - 1 if where == "slice" else dim
        measure = {3: "volume", 2: "area", 1: "length"}.get(sdim, "measure")
        rows = [
            {"label": "mean", "value": number(s["mean"]) + unit},
            {"label": "std dev", "value": number(s["std"]) + unit},
            {"label": "RMS", "value": number(s["rms"]) + unit},
            {"label": "min", "value": number(s["min"]) + unit, "point": s["argmin"][:dim]},
            {"label": "max", "value": number(s["max"]) + unit, "point": s["argmax"][:dim]},
            {"label": "integral", "value": number(s["integral"]), "note": f"× {measure}"},
        ]
        for r in rows:
            if "point" in r:
                r["at"] = "(" + ", ".join(number(c) for c in r["point"]) + ")"
                r["point"] = [number(c) for c in r["point"]]
        hist = s.get("hist") or {}
        fractions = hist.get("fractions") or []
        top = max(fractions, default=0) or 1
        n = len(fractions)
        rgb = [",".join(str(round(255 * c)) for c in color_at(colormap, (k + 0.5) / n)) for k in range(n)]
        bars = "".join(
            f'<rect x="{k * 240 / n:.2f}" y="{48 * (1 - f / top):.2f}" width="{240 / n - 1:.2f}" '
            f'height="{48 * f / top:.2f}" fill="rgb({rgb[k]})"/>'
            for k, f in enumerate(fractions)
        )
        places[where] = {
            "rows": rows,
            "summary": f"{s['cells']:,} cells · {measure} {number(s['measure'])}"
            + (f" · {s['nonfinite']:,} not finite" if s.get("nonfinite") else ""),
            "svg": f'<svg viewBox="0 0 240 48" preserveAspectRatio="none" class="st-bars">{bars}</svg>' if n else "",
            "lo": number(hist["lo"]) if n else "",
            "hi": number(hist["hi"]) if n else "",
        }
    return {"places": places}
