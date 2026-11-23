"""Export from the run monitor and the results viewer: a plot as CSV or as a light/dark SVG pair, a probe
file as CSV, the results view over time as a GIF or MP4, and the snapshot series opened in ParaView.

Files go to `<project>/exports/<run>/`; a toast names each and reveals it in the Finder.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from ..csvseries import CsvSeries
from ..export import (
    PlotSpec,
    ViewState,
    caption_frame,
    encode_animation,
    find_paraview,
    frame_indices,
    open_in_paraview,
    plot_csv,
    safe_name,
    series_csv,
    series_files,
    svg_pair,
)
from ..health import CRITICAL, INFO

if TYPE_CHECKING:
    from .app import App


class ExportPanel:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self.specs: dict[str, PlotSpec] = {}
        self.sources: dict[str, CsvSeries] = {}  # a plot's probe file, exported whole as CSV
        self.state.x_busy = ""
        self.state.x_ffmpeg = shutil.which("ffmpeg") is not None
        self.state.x_paraview = ""
        self.state.x_pv = {"found": True, "title": ""}  # ParaView as it was last looked for (check_paraview)
        self._pv_poll: asyncio.TimerHandle | None = None
        self._cancel = False

    def remember(self, key: str, spec: PlotSpec | None, source: CsvSeries | None = None) -> None:
        if spec is None:
            self.specs.pop(key, None)
        else:
            self.specs[key] = spec
        if source is None:
            self.sources.pop(key, None)
        else:
            self.sources[key] = source

    def _dir(self, run: str) -> Path:
        return self.app.session.project.root / "exports" / (run or "case")

    def _done(self, what: str, path: Path) -> None:
        rel = (
            path.relative_to(self.app.session.project.root)
            if path.is_relative_to(self.app.session.project.root)
            else path
        )
        self.app.health.toast(f"Exported {what}", str(rel), INFO, path=str(path))

    def _failed(self, what: str, e: Exception) -> None:
        self.app.health.toast(f"{what} failed", str(e), CRITICAL)

    def export_plot(self, key: str, fmt: str) -> None:
        spec = self.specs.get(key)
        s = self.state
        run = {"probe": s.v_run, "line": s.v_run, "compare": f"compare-{s.c_a}-{s.c_b}"}.get(key, s.run_sel)
        if spec is None or not spec.curves:
            self.app.health.toast("Nothing to export", "the plot has no data yet", INFO)
            return
        stem = self._dir(run) / safe_name(spec.title or spec.ylabel or key)
        try:
            if fmt == "csv":
                source = self.sources.get(key)
                path = stem.with_suffix(".csv")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(series_csv(source) if source is not None else plot_csv(spec), encoding="utf-8")
                self._done("CSV", path)
            else:
                paths = svg_pair(spec, stem)
                self._done("SVG light/dark pair", paths[0])
        except (OSError, ValueError) as e:
            self._failed("Export", e)

    # Results: animation and ParaView

    def animate(self, fmt: str) -> None:
        task = asyncio.ensure_future(self._animate(fmt))
        self.app._tasks.add(task)
        task.add_done_callback(self.app._tasks.discard)

    def cancel(self) -> None:
        self._cancel = True

    async def _animate(self, fmt: str) -> None:
        rp = self.app.results_page
        s = self.state
        info = dict(s.v_info or {})
        series, count, back = s.v_primary, int(info.get("count", 0)), int(s.v_index)
        if not series or not count or s.x_busy:
            return
        indices = frame_indices(count)
        w, h = rp._frame_size(False)
        frames: list[bytes] = []
        self._cancel = False
        for k, i in enumerate(indices):
            with s:
                s.x_busy = f"animation: frame {k + 1} of {len(indices)}"
            shown = await rp._call("show", series=series, index=i)
            frame = await rp._call("frame", width=w, height=h, interactive=False)
            if shown is None or frame is None or self._cancel:
                break
            t = shown.get("t")
            text = (
                f"{shown.get('field', '')}   t {t:.5g}" if isinstance(t, (int, float)) else f"step {shown.get('step')}"
            )
            rng = shown.get("range") or [None, None]
            frames.append(
                caption_frame(
                    frame["image"],
                    text,
                    str(shown.get("colormap", "")),
                    rng[0],
                    rng[1],
                    (s.v_colorbar or {}).get("label", ""),
                )
            )
        await rp._show(series, back)
        path = self._dir(s.v_run) / f"{safe_name(series)}_{safe_name(str(info.get('field', '')))}.{fmt}"
        try:
            if frames:
                await asyncio.to_thread(encode_animation, frames, path)
                with s:
                    self._done(f"animation ({len(frames)} frames)", path)
        except (OSError, ValueError) as e:
            with s:
                self._failed("Animation", e)
        finally:
            with s:
                s.x_busy = ""

    # ParaView is looked for again whenever it matters: installs change under a running GUI (6.0.1 replaced by
    # 6.2.1), so what was found at start says nothing now.

    def check_paraview(self) -> None:
        """Look for ParaView now (the results page shown, the pointer on the button): the button follows."""
        task = asyncio.ensure_future(self._check_paraview())
        self.app._tasks.add(task)
        task.add_done_callback(self.app._tasks.discard)

    async def _check_paraview(self) -> None:
        pv = await asyncio.to_thread(find_paraview)
        if pv is None:
            found, title = (
                False,
                (
                    "ParaView was not found. Install it in /Applications, put paraview on PATH, or set "
                    "MALLARD_GUI_PARAVIEW to its executable or .app: the button turns on when it is there."
                ),
            )
        else:
            found, title = (
                True,
                (
                    f"Open this series in ParaView ({pv.gui}) as a state: the time, field, colormap, range, camera "
                    "and section shown"
                ),
            )
        if self.state.x_pv != {"found": found, "title": title}:
            with self.state:
                self.state.x_pv = {"found": found, "title": title}

    def watch_paraview(self) -> None:
        """While the results page is shown, look every few seconds."""
        if self._pv_poll is not None:
            self._pv_poll.cancel()
            self._pv_poll = None
        if self.state.page != "results":
            return
        self.check_paraview()
        with contextlib.suppress(RuntimeError):  # no loop: nothing to follow
            self._pv_poll = asyncio.get_running_loop().call_later(5.0, self.watch_paraview)

    def toolbar_button(self) -> None:
        """Open in ParaView on the results view's toolbar: off, saying how to set it up, while there is none."""
        with html.Span(classes="tb-wrap", mouseenter=self.check_paraview, title=("x_pv.title",)):
            with html.Button(
                classes="tb paraview",
                click=self.paraview,
                disabled=("!x_pv.found || !v_info.count || !!x_busy || v_where !== 'local'",),
                aria_label="Open in ParaView",
            ):
                html.Span(v_html="icons.external")
                html.Span("ParaView")

    def paraview(self) -> None:
        task = asyncio.ensure_future(self._paraview())
        self.app._tasks.add(task)
        task.add_done_callback(self.app._tasks.discard)

    async def _paraview(self) -> None:
        rp = self.app.results_page
        s = self.state
        series = rp._series.get(s.v_primary)
        if series is None:
            return
        if s.v_where != "local":
            with s:
                self.app.health.toast(
                    "Open in ParaView", "the run is on a remote: copy it here first (Data page), then open it", INFO
                )
            return
        files = series_files(Path(rp.run_dir), s.v_primary, series["snapshots"])
        info: dict[str, Any] = dict(s.v_info or {})
        name = f"{safe_name(s.v_primary)}"
        with s:
            s.x_busy = "ParaView: writing the state"
        camera = await rp._call("get_camera") if rp.batch is None else None
        view = ViewState(
            t=info.get("t") if isinstance(info.get("t"), (int, float)) else None,
            component=str(info.get("component", "magnitude")),
            range=[] if info.get("auto_range", True) else [float(v) for v in info.get("range") or []],
            camera=dict(camera or {}),
            section=dict(info.get("section") or {}),
        )
        try:
            pv = await asyncio.to_thread(find_paraview)
            path, what = await asyncio.to_thread(
                open_in_paraview,
                pv,
                files,
                str(info.get("field", "")),
                str(info.get("colormap", "")),
                self._dir(s.v_run),
                name,
                view=view,
            )
            with s:
                self.app.health.toast(f"ParaView: {what}", str(path), INFO, path=str(path))
        except (OSError, RuntimeError, ValueError) as e:
            with s:
                self._failed("Open in ParaView", e)
        except Exception as e:  # noqa: BLE001 - pvpython's failure (CalledProcessError, timeout) is shown, not raised
            with s:
                self._failed("Open in ParaView", e)
        finally:
            with s:
                s.x_busy = ""

    # Layout

    def buttons(self, key: str) -> None:
        """CSV and SVG buttons for a plot head."""
        with html.Span(classes="export"):
            html.Button(
                "CSV",
                classes="tb",
                click=(self.export_plot, f"['{key}', 'csv']"),
                title="The plot's data as CSV (a probe file: every sample, with units)",
            )
            html.Button(
                "SVG",
                classes="tb",
                click=(self.export_plot, f"['{key}', 'svg']"),
                title="The plot as a light and a dark SVG, in the style of Mallard's documentation figures",
            )

    def layout_results_rows(self) -> None:
        """The results viewer's export rows, in its right panel."""
        with html.Div(classes="group-head"):
            html.Span("EXPORT")
            html.Span("analysis here, rendering in ParaView", classes="note")
        with html.Div(classes="prow"):
            html.Div("animation", classes="plabel")
            with html.Div(classes="pval", style="gap: 3px; padding: 0 6px"):
                with html.Button(
                    classes="tb",
                    click=(self.animate, "['gif']"),
                    disabled=("!v_info.count || !!x_busy",),
                    title="The view over every snapshot (at most 240), as a GIF",
                ):
                    html.Span(v_html="icons.film")
                    html.Span("GIF")
                html.Button(
                    "MP4",
                    classes="tb",
                    click=(self.animate, "['mp4']"),
                    disabled=("!v_info.count || !!x_busy || !x_ffmpeg",),
                    title=("x_ffmpeg ? 'The view over every snapshot, as an MP4' : 'MP4 needs ffmpeg on PATH'",),
                )
                html.Button("Cancel", classes="tb", click=self.cancel, v_if="x_busy.startsWith('animation')")
        with html.Div(classes="prow"):
            html.Div("ParaView", classes="plabel")
            with html.Div(classes="pval", style="gap: 3px; padding: 0 6px"):
                with html.Button(
                    classes="tb",
                    click=self.paraview,
                    disabled=("!x_pv.found || !v_info.count || !!x_busy",),
                    title=("x_pv.title",),
                ):
                    html.Span(v_html="icons.external")
                    html.Span("Open in ParaView")
        with html.Div(classes="prow", v_if="x_busy"):
            html.Div("", classes="plabel")
            html.Div("{{ x_busy }}", classes="pval ro muted", style="font-family: var(--m-font-sans)")
