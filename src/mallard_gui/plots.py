"""Line plots as SVG: labeled axes with units, ticks, a grid, and a legend for more than one curve.

Colors are CSS variables, so a plot follows the theme it is shown in.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

# Okabe-Ito, without black: the categorical palette of the design.
OKABE_ITO = ("#56b4e9", "#e69f00", "#009e73", "#f0e442", "#0072b2", "#d55e00", "#cc79a7")
MAX_POINTS = 2000


@dataclass
class Curve:
    label: str
    x: Sequence[float]
    y: Sequence[float]


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """Round tick values covering [lo, hi]."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return []
    if hi == lo:
        pad = abs(lo) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    raw = (hi - lo) / max(n - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    ticks = []
    v = first
    while v <= hi + step * 1e-9:
        ticks.append(0.0 if abs(v) < step * 1e-9 else v)
        v += step
    return ticks


def fmt(v: float) -> str:
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e5 or a < 1e-3:
        return f"{v:.2e}".replace("e-0", "e-").replace("e+0", "e")
    return f"{v:.4g}"


def _decimate(x: Sequence[float], y: Sequence[float]) -> tuple[list[float], list[float]]:
    """At most MAX_POINTS, keeping each bin's minimum and maximum so peaks survive."""
    n = len(x)
    if n <= MAX_POINTS:
        return list(x), list(y)
    bins = MAX_POINTS // 2
    xs: list[float] = []
    ys: list[float] = []
    for b in range(bins):
        i0, i1 = b * n // bins, (b + 1) * n // bins
        seg = range(i0, max(i1, i0 + 1))
        lo = min(seg, key=lambda i: y[i])
        hi = max(seg, key=lambda i: y[i])
        for i in sorted({lo, hi}):
            xs.append(x[i])
            ys.append(y[i])
    return xs, ys


LEGEND_ROW = 12
# A generous advance for the 10 px monospace legend font, so an entry never runs past the edge.
LEGEND_CHAR = 6.2


def legend_entry_width(label: str) -> float:
    return 15 + LEGEND_CHAR * len(label)


def _legend_layout(labels: Sequence[str], left: float, right: float) -> list[tuple[float, int]]:
    """Each entry's x and row: entries flow left to right and wrap to a new row rather than past `right`."""
    out: list[tuple[float, int]] = []
    x, row = left, 0
    for label in labels:
        w = legend_entry_width(label)
        if x > left and x + w > right:
            x, row = left, row + 1
        out.append((x, row))
        x += w + 7
    return out


def _log_curves(curves: Sequence[Curve], logx: bool, logy: bool) -> list[Curve]:
    out = []
    for c in curves:
        pts = [(x, y) for x, y in zip(c.x, c.y, strict=False) if (not logx or x > 0) and (not logy or y > 0)]
        out.append(
            Curve(
                c.label,
                [math.log10(x) if logx else x for x, _ in pts],
                [math.log10(y) if logy else y for _, y in pts],
            )
        )
    return out


def _decades(lo: float, hi: float) -> list[float]:
    """Ticks of a log axis (values are log10): every decade, or every few when there are many."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return []
    a, b = math.floor(lo), math.ceil(hi)
    if b == a:
        b = a + 1
    step = max(1, math.ceil((b - a) / 6))
    return [float(k) for k in range(a, b + 1, step)]


def _pow10(v: float) -> str:
    return fmt(10**v) if -2 <= v <= 3 else f"1e{int(v)}"


def line_plot(
    curves: Sequence[Curve],
    xlabel: str,
    ylabel: str,
    width: int = 360,
    height: int = 190,
    title: str = "",
    logx: bool = False,
    logy: bool = False,
    mark_x: float | None = None,
    markers: Sequence[bool] = (),
) -> str:
    """`logx`/`logy` draw that axis in decades; `mark_x` draws a dashed line at that x (a spectral peak);
    `markers[i]` draws curve i as points (reference data) instead of a line."""
    if logx or logy:
        curves = _log_curves(curves, logx, logy)
        if mark_x is not None and logx:
            mark_x = math.log10(mark_x) if mark_x > 0 else None
    left, right, bottom = 52, 10, 30
    legend = _legend_layout([c.label for c in curves], left, width - right) if len(curves) > 1 else []
    rows = 1 + max((row for _, row in legend), default=0)
    top = 6 + LEGEND_ROW * rows if legend or title else 8
    pw, ph = width - left - right, height - top - bottom
    points = [
        (cx, cy) for c in curves for cx, cy in zip(c.x, c.y, strict=False) if math.isfinite(cx) and math.isfinite(cy)
    ]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="m-plot" viewBox="0 0 {width} {height}" '
        f'width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="{escape(ylabel)} over '
        f'{escape(xlabel)}">'
    ]
    if not points:
        parts.append(
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" class="m-plot-empty">no data yet</text></svg>'
        )
        return "".join(parts)
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    xt = _decades(x0, x1) if logx else nice_ticks(x0, x1)
    yt = _decades(y0, y1) if logy else nice_ticks(y0, y1)
    if xt:
        x0, x1 = min(x0, xt[0]), max(x1, xt[-1])
    if yt:
        y0, y1 = min(y0, yt[0]), max(y1, yt[-1])
    if x1 == x0:
        x0, x1 = x0 - 1, x1 + 1
    if y1 == y0:
        y0, y1 = y0 - 1, y1 + 1

    def sx(v: float) -> float:
        return left + (v - x0) / (x1 - x0) * pw

    def sy(v: float) -> float:
        return top + ph - (v - y0) / (y1 - y0) * ph

    for v in xt:
        X = sx(v)
        parts.append(f'<line class="m-grid" x1="{X:.1f}" y1="{top}" x2="{X:.1f}" y2="{top + ph}"/>')
        label = _pow10(v) if logx else fmt(v)
        parts.append(f'<text class="m-tick" x="{X:.1f}" y="{top + ph + 12}" text-anchor="middle">{label}</text>')
    for v in yt:
        Y = sy(v)
        parts.append(f'<line class="m-grid" x1="{left}" y1="{Y:.1f}" x2="{left + pw}" y2="{Y:.1f}"/>')
        label = _pow10(v) if logy else fmt(v)
        parts.append(f'<text class="m-tick" x="{left - 4}" y="{Y + 3:.1f}" text-anchor="end">{label}</text>')
    parts.append(f'<rect class="m-frame" x="{left}" y="{top}" width="{pw}" height="{ph}"/>')
    parts.append(f'<path class="m-axisline" d="M{left} {top}V{top + ph}H{left + pw}"/>')
    for i, c in enumerate(curves):
        xs, ys = _decimate(c.x, c.y)
        d = " ".join(
            f"{sx(a):.1f},{sy(b):.1f}" for a, b in zip(xs, ys, strict=False) if math.isfinite(a) and math.isfinite(b)
        )
        color = OKABE_ITO[i % len(OKABE_ITO)]
        if i < len(markers) and markers[i]:
            parts.extend(
                f'<circle cx="{sx(a):.1f}" cy="{sy(b):.1f}" r="2.2" fill="none" stroke="{color}" stroke-width="1.2"/>'
                for a, b in zip(xs, ys, strict=False)
                if math.isfinite(a) and math.isfinite(b)
            )
        else:
            parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{d}"/>')
    if mark_x is not None and x0 <= mark_x <= x1:
        X = sx(mark_x)
        parts.append(
            f'<line class="m-mark" x1="{X:.1f}" y1="{top}" x2="{X:.1f}" y2="{top + ph}" stroke-dasharray="3 3"/>'
        )
    parts.append(
        f'<text class="m-axis" x="{left + pw / 2}" y="{height - 3}" text-anchor="middle">{escape(xlabel)}</text>'
    )
    parts.append(
        f'<text class="m-axis" transform="translate(11 {top + ph / 2}) rotate(-90)" '
        f'text-anchor="middle">{escape(ylabel)}</text>'
    )
    for i, (c, (x, row)) in enumerate(zip(curves, legend, strict=False)):
        color = OKABE_ITO[i % len(OKABE_ITO)]
        y = LEGEND_ROW * row
        parts.append(f'<line x1="{x}" y1="{y + 9}" x2="{x + 12}" y2="{y + 9}" stroke="{color}" stroke-width="2"/>')
        parts.append(f'<text class="m-legend" x="{x + 15}" y="{y + 12}">{escape(c.label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def axis_label(name: str, unit: str) -> str:
    return f"{name} ({unit or 'nd'})"
