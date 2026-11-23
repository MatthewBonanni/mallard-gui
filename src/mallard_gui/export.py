"""Technical export: plots and probe series as CSV, figures as light/dark SVG pairs in the docs' style, an
animation of the results view over time, and the snapshot series handed to ParaView.

Heavy rendering is ParaView's: "Open in ParaView" writes a `.pvsm` state of the series with the view's field
and colormap (through ParaView's own `pvpython`) and opens it there.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .csvseries import CsvSeries
from .plots import Curve
from .viewer.colormaps import canonical, points

MAX_FRAMES = 240


@dataclass
class PlotSpec:
    """What a plot shows, kept beside its SVG so it can be exported as data or as a figure."""

    curves: list[Curve]
    xlabel: str
    ylabel: str
    title: str = ""
    styles: list[str] = field(default_factory=list)  # per curve: "" (line) or "points" (reference data)


def safe_name(text: str) -> str:
    """A file name from a label: `Probe wake_point · every sample` -> `probe_wake_point_every_sample`."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_").lower() or "plot"


def _num(v: float) -> str:
    return repr(float(v)) if math.isfinite(v) else ("nan" if math.isnan(v) else ("inf" if v > 0 else "-inf"))


def plot_csv(spec: PlotSpec) -> str:
    """One x column and a column per curve where the curves share their x; else one row per point."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    curves = spec.curves
    if not curves:
        return ""
    shared = all(len(c.x) == len(curves[0].x) and list(c.x) == list(curves[0].x) for c in curves)
    if shared:
        w.writerow([spec.xlabel, *(c.label for c in curves)])
        for i, x in enumerate(curves[0].x):
            w.writerow([_num(x), *(_num(c.y[i]) for c in curves)])
    else:
        w.writerow(["curve", spec.xlabel, spec.ylabel])
        for c in curves:
            for x, y in zip(c.x, c.y, strict=False):
                w.writerow([c.label, _num(x), _num(y)])
    return out.getvalue()


def series_csv(c: CsvSeries) -> str:
    """A probe, force or reduction file as the GUI followed it: `t`, `point` and its variables, with units;
    a boundary probe's latest profile as its rows."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    if c.profile:
        w.writerow(c.columns)
        out.write("# units: " + ",".join(c.unit(col) or "1" for col in c.columns) + "\n")
        for row in c.rows:
            w.writerow([_num(v) for v in row])
        return out.getvalue()
    names = c.variables
    points = c.points()
    w.writerow(["t", "point", *names])
    out.write("# units: " + ",".join([c.unit("t") or "1", "1", *(c.unit(n) or "1" for n in names)]) + "\n")
    for p in points:
        first = next(((n, p) for n in names if (n, p) in c.series), None)
        if first is None:
            continue
        ts = c.series[first].t  # the samples held: the latest of a long run (the file has them all)
        values = {n: c.series[(n, p)].values for n in names if (n, p) in c.series}
        for i, t in enumerate(ts):
            w.writerow([_num(t), p, *(_num(values[n][i]) if n in values and i < len(values[n]) else "" for n in names)])
    return out.getvalue()


def svg_pair(spec: PlotSpec, stem: Path) -> list[Path]:
    """The plot as `<stem>-light.svg` and `<stem>-dark.svg` in the style of Mallard's documentation figures."""
    from .figstyle import CYCLE, Palette, save_pair

    def draw(P: Palette) -> Any:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(6.4, 3.6))
        for i, c in enumerate(spec.curves):
            color = P[CYCLE[i % len(CYCLE)]]
            style = spec.styles[i] if i < len(spec.styles) else ""
            if style == "points":
                ax.plot(c.x, c.y, "o", ms=4, mfc="none", color=P["fg"], label=c.label)
            else:
                ax.plot(c.x, c.y, color=color, label=c.label)
        ax.set_xlabel(spec.xlabel)
        ax.set_ylabel(spec.ylabel)
        ax.grid(True, color=P["faint"], linewidth=0.8)
        ax.set_axisbelow(True)
        if spec.title:
            ax.set_title(spec.title)
        if len(spec.curves) > 1:
            ax.legend()
        return fig

    return save_pair(stem, draw)


# Animation


def encode_animation(frames: Sequence[bytes], path: Path, fps: float = 8.0) -> Path:
    """Frames (PNG or JPEG bytes) as a GIF (Pillow), or as an MP4 through `ffmpeg` when it is on PATH."""
    from PIL import Image

    if not frames:
        raise ValueError("no frames")
    images = [Image.open(io.BytesIO(f)).convert("RGB") for f in frames]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".gif":
        images[0].save(
            path,
            save_all=True,
            append_images=images[1:],
            duration=int(1000 / fps),
            loop=0,
            optimize=False,
        )
        return path
    if path.suffix != ".mp4":
        raise ValueError(f"{path.name}: .gif or .mp4")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("MP4 needs ffmpeg on PATH; GIF needs nothing")
    w, h = images[0].size
    w, h = w - w % 2, h - h % 2  # yuv420p wants even sizes
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{w}x{h}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        str(path),
    ]
    data = b"".join(im.resize((w, h)).tobytes() if im.size != (w, h) else im.tobytes() for im in images)
    subprocess.run(cmd, input=data, check=True, capture_output=True)
    return path


def caption_frame(
    frame: bytes, text: str, colormap: str = "", lo: float | None = None, hi: float | None = None, label: str = ""
) -> bytes:
    """The frame with its time written on it and, for a field, a colorbar: the view's HTML overlays are not in
    the rendered image."""
    from PIL import Image, ImageDraw

    from .viewer.colormaps import color_at

    im = Image.open(io.BytesIO(frame)).convert("RGB")
    draw = ImageDraw.Draw(im)
    w, h = im.size
    size = max(12, h // 40)
    font = _font(size)
    pad = size // 2
    draw.text((pad, pad), text, fill=(255, 255, 255), font=font, stroke_width=max(1, size // 8), stroke_fill=(0, 0, 0))
    if colormap and lo is not None and hi is not None:
        bw, bh = w // 3, max(8, size // 2)
        x0, y0 = w - bw - pad * 2, h - bh - size * 2 - pad
        for i in range(bw):
            r, g, b = color_at(colormap, i / max(bw - 1, 1))
            draw.line([(x0 + i, y0), (x0 + i, y0 + bh)], fill=(int(255 * r), int(255 * g), int(255 * b)))
        sw = max(1, size // 8)
        draw.text((x0, y0 - size - 2), label, fill=(255, 255, 255), font=font, stroke_width=sw, stroke_fill=(0, 0, 0))
        for v, x, anchor in ((lo, x0, "la"), (hi, x0 + bw, "ra")):
            draw.text(
                (x, y0 + bh + 2),
                f"{v:.4g}",
                fill=(255, 255, 255),
                font=font,
                anchor=anchor,
                stroke_width=sw,
                stroke_fill=(0, 0, 0),
            )
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def _font(size: int) -> Any:
    from importlib import resources

    from PIL import ImageFont

    try:
        path = resources.files("mallard_gui.assets") / "fonts" / "InterVariable.ttf"
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default(size)


def frame_indices(count: int, limit: int = MAX_FRAMES) -> list[int]:
    """Every snapshot, or `limit` of them evenly spread, the last always among them."""
    if count <= limit:
        return list(range(count))
    return sorted({round(k * (count - 1) / (limit - 1)) for k in range(limit)})


# ParaView


@dataclass
class ParaView:
    gui: Path  # the paraview executable (or the .app on macOS)
    pvpython: Path | None


def find_paraview(
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    apps: Path = Path("/Applications"),
) -> ParaView | None:
    """`$MALLARD_GUI_PARAVIEW` (the executable or the .app), else `paraview` on PATH, else the newest
    `/Applications/ParaView-*.app`."""
    env = dict(os.environ) if env is None else env
    if env.get("MALLARD_GUI_APPLICATIONS"):
        apps = Path(env["MALLARD_GUI_APPLICATIONS"])  # another directory of apps than /Applications
    candidates: list[Path] = []
    if env.get("MALLARD_GUI_PARAVIEW"):
        candidates.append(Path(env["MALLARD_GUI_PARAVIEW"]))
    found = which("paraview")
    if found:
        candidates.append(Path(found))
    if apps.is_dir():
        candidates += sorted(apps.glob("ParaView*.app"), key=_version_key, reverse=True)
    for c in candidates:
        if c.suffix == ".app" and (c / "Contents" / "MacOS" / "paraview").exists():
            pv = c / "Contents" / "bin" / "pvpython"
            return ParaView(c, pv if pv.exists() else None)
        if c.is_file():
            pv = c.parent / "pvpython"
            return ParaView(c, pv if pv.exists() else (Path(p) if (p := which("pvpython")) else None))
    return None


def _version_key(p: Path) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", p.name)) or (0,)


@dataclass
class ViewState:
    """What of the results view a ParaView state carries: the time shown, the field's component and range
    (empty: the data's), the camera (`get_camera`'s keys), the section's slice or clip plane."""

    t: float | None = None
    component: str = "magnitude"
    range: list[float] = field(default_factory=list)
    camera: dict[str, Any] = field(default_factory=dict)
    section: dict[str, Any] = field(default_factory=dict)


def state_script(
    files: Sequence[Path], field_name: str, colormap: str, out: Path | None, view: ViewState | None = None
) -> str:
    """A ParaView Python script that opens the series at the view's time, colors it by the field with the
    view's colormap, range and camera, and, given `out`, saves the state as `.pvsm`. Without `out` it is
    itself a state ParaView loads (File > Load State)."""
    view = view or ViewState()
    lines = [
        "from paraview.simple import *",
        f"reader = OpenDataFile({[str(f) for f in files]!r})",
        "view = GetActiveViewOrCreate('RenderView')",
        "source = reader",
    ]
    sec = view.section
    if sec.get("slice") or sec.get("clip"):
        kind = "Slice" if sec.get("slice") else "Clip"
        lines += [
            f"source = {kind}(Input=reader)",
            f"source.{'SliceType' if kind == 'Slice' else 'ClipType'} = 'Plane'",
            f"source.{'SliceType' if kind == 'Slice' else 'ClipType'}.Origin = {list(sec.get('origin', [0, 0, 0]))!r}",
            f"source.{'SliceType' if kind == 'Slice' else 'ClipType'}.Normal = {list(sec.get('normal', [0, 0, 1]))!r}",
        ]
        if kind == "Clip" and sec.get("flipped"):
            lines.append("source.Invert = 0")
    lines.append("display = Show(source, view)")
    if view.t is not None:
        lines += [
            "scene = GetAnimationScene()",
            "scene.UpdateAnimationUsingDataTimeSteps()",
            f"scene.AnimationTime = {float(view.t)!r}",
        ]
    if field_name:
        lines += [
            "names = list(reader.CellData.keys())",
            f"association = 'CELLS' if {field_name!r} in names else 'POINTS'",
            f"ColorBy(display, (association, {field_name!r}))",
            "display.SetScalarBarVisibility(view, True)",
            f"lut = GetColorTransferFunction({field_name!r})",
        ]
        if view.component in ("x", "y", "z"):
            lines += ["lut.VectorMode = 'Component'", f"lut.VectorComponent = {'xyz'.index(view.component)}"]
        space, pts = points(colormap)
        lines += [
            f"# {canonical(colormap)}, as the GUI draws it (ParaView's preset of that name)",
            f"lut.ColorSpace = {space!r}",
            f"lut.RGBPoints = {pts!r}",
            "display.RescaleTransferFunctionToDataRange(True, False)",
        ]
        if len(view.range) == 2:
            lo, hi = (float(v) for v in view.range)
            lines += [f"lut.RescaleTransferFunction({lo!r}, {hi!r})", "lut.AutomaticRescaleRangeMode = 'Never'"]
    cam = view.camera
    if cam.get("position") and cam.get("focal_point"):
        lines += [
            f"view.CameraPosition = {list(cam['position'])!r}",
            f"view.CameraFocalPoint = {list(cam['focal_point'])!r}",
            f"view.CameraViewUp = {list(cam.get('view_up', [0, 1, 0]))!r}",
            f"view.CameraViewAngle = {float(cam.get('view_angle', 30.0))!r}",
            f"view.CameraParallelProjection = {int(bool(cam.get('parallel')))}",
        ]
        if cam.get("parallel_scale"):
            lines.append(f"view.CameraParallelScale = {float(cam['parallel_scale'])!r}")
    else:
        lines.append("view.ResetCamera()")
    lines.append("Render(view)")
    if out is not None:
        lines.append(f"SaveState({str(out)!r})")
    return "\n".join(lines) + "\n"


def series_files(run_dir: Path, series_id: str, snapshots: Sequence[dict[str, Any]]) -> list[Path]:
    """The files ParaView reads for a series: its `.pvd` collection where there is one, else each snapshot."""
    pvd = run_dir / f"{series_id}.pvd"
    if pvd.is_file():
        return [pvd]
    return list(dict.fromkeys(run_dir / str(s["path"]) for s in snapshots if s.get("path")))


def open_in_paraview(
    pv: ParaView | None,
    files: Sequence[Path],
    field: str,
    colormap: str,
    out_dir: Path,
    name: str,
    run: Callable[..., Any] = subprocess.run,
    spawn: Callable[[list[str]], Any] | None = None,
    view: ViewState | None = None,
) -> tuple[Path, str]:
    """Writes `<name>.pvsm` (through pvpython) and opens it in ParaView. Without ParaView, writes `<name>.py`,
    a state to load by hand. Returns the state file and what was done."""
    out_dir.mkdir(parents=True, exist_ok=True)
    spawn = spawn or _spawn
    if pv is None or pv.pvpython is None:
        script = out_dir / f"{name}.py"
        script.write_text(state_script(files, field, colormap, None, view), encoding="utf-8")
        if pv is None:
            return script, "ParaView not found: File > Load State opens this script"
        spawn(_launch(pv, ["--script=" + str(script)]))
        return script, "opened in ParaView (no pvpython beside it for a .pvsm)"
    state = out_dir / f"{name}.pvsm"
    script = out_dir / f"{name}.pvsm.py"
    script.write_text(state_script(files, field, colormap, state, view), encoding="utf-8")
    run(
        [str(pv.pvpython), "--force-offscreen-rendering", str(script)],
        check=True,
        capture_output=True,
        timeout=180,
    )
    script.unlink(missing_ok=True)
    if not state.exists():
        raise RuntimeError(f"pvpython wrote no {state.name}")
    spawn(_launch(pv, ["--state=" + str(state)]))
    return state, "opened in ParaView"


def _launch(pv: ParaView, args: list[str]) -> list[str]:
    if pv.gui.suffix == ".app" and sys.platform == "darwin":
        return ["open", "-n", "-a", str(pv.gui), "--args", *args]
    return [str(pv.gui), *args]


def _spawn(cmd: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
