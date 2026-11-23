"""ParaView's colormaps, as ParaView ships them, so the view, a saved image or animation and the ParaView handoff
show one map: `colormaps.json` holds each preset's name, color space and control points as ParaView 6.2.1 gives
them (pvpython: `vtkSMTransferFunctionPresets.GetInstance().GetPresetAsString(i)`); VTK's
vtkColorTransferFunction interpolates them in that color space as ParaView does, and the ParaView state and the
parallel viewer's driver (which ships the file beside it) set the same points, whatever ParaView's own presets.
Fast (Lab) is ParaView's default since 6.0 and the GUI's."""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    p["Name"]: p for p in json.loads(Path(__file__).with_name("colormaps.json").read_text(encoding="utf-8"))
}
COLORMAPS = tuple(PRESETS)  # the picker's order
DEFAULT = "Fast"
DIVERGING = {"Cool to Warm", "Blue Orange (divergent)"}  # centered on zero for a signed field
# Names the GUI used before it took ParaView's: what a saved view or setting that names one gets.
LEGACY = {"cividis": "Cividis", "batlow": DEFAULT, "vik": "Cool to Warm", "viridis": "Viridis",
          "coolwarm": "Cool to Warm", "gray": "Grayscale"}  # fmt: skip
TABLE = 256


def canonical(name: str | None) -> str:
    """A colormap's name as ParaView has it: a former GUI name mapped, an unknown one the default."""
    return name if name in PRESETS else LEGACY.get(name or "", DEFAULT)


def points(name: str) -> tuple[str, list[float]]:
    """The preset's color space and its RGBPoints (x, r, g, b, ...) over [0, 1], as a ParaView LUT takes them."""
    p = PRESETS[canonical(name)]
    pts = p["RGBPoints"]
    x0, x1 = min(pts[0::4]), max(pts[0::4])
    out = [(v - x0) / (x1 - x0) if i % 4 == 0 else v for i, v in enumerate(pts)]
    return p.get("ColorSpace", "RGB"), out


def transfer_function(name: str, lo: float = 0.0, hi: float = 1.0) -> Any:
    """The preset as a vtkColorTransferFunction over [lo, hi], in its own color space."""
    from vtkmodules.vtkRenderingCore import vtkColorTransferFunction

    space, pts = points(name)
    f = vtkColorTransferFunction()
    {"Lab": f.SetColorSpaceToLab, "Diverging": f.SetColorSpaceToDiverging, "HSV": f.SetColorSpaceToHSV}.get(
        space, f.SetColorSpaceToRGB
    )()
    for i in range(0, len(pts), 4):
        f.AddRGBPoint(lo + pts[i] * (hi - lo), *pts[i + 1 : i + 4])
    return f


@functools.cache
def table(name: str) -> tuple[tuple[float, float, float], ...]:
    f = transfer_function(name)
    return tuple(tuple(f.GetColor(i / (TABLE - 1))) for i in range(TABLE))


def color_at(name: str, x: float) -> tuple[float, float, float]:
    """The colormap's color at x in [0, 1], components in [0, 1]."""
    t = table(canonical(name))
    return t[round(min(max(x, 0.0), 1.0) * (TABLE - 1))]


def _stops(name: str, n: int = 17) -> list[tuple[float, str]]:
    out = []
    for k in range(n):
        r, g, b = (round(255 * c) for c in color_at(name, k / (n - 1)))
        out.append((k / (n - 1), f"rgb({r},{g},{b})"))
    return out


def css_gradient(name: str) -> str:
    """The colormap as a CSS linear gradient, left to right, for the colorbar."""
    return f"linear-gradient(to right, {', '.join(f'{c} {round(100 * x)}%' for x, c in _stops(name))})"


def symmetric(lo: float, hi: float) -> tuple[float, float]:
    """A diverging map's range: centered on zero."""
    m = max(abs(lo), abs(hi))
    return -m, m


def svg_swatch(name: str, width: int = 48, height: int = 10) -> str:
    """The colormap as a small SVG, for the colormap picker."""
    key = "cm-" + re.sub(r"[^a-z0-9]+", "-", canonical(name).lower()).strip("-")
    stops = "".join(f'<stop offset="{x:g}" stop-color="{c}"/>' for x, c in _stops(name))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<defs><linearGradient id="{key}">{stops}</linearGradient></defs>'
        f'<rect width="{width}" height="{height}" rx="2" fill="url(#{key})"/></svg>'
    )
