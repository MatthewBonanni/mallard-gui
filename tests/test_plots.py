from __future__ import annotations

import itertools
import re
import xml.etree.ElementTree as ET

from mallard_gui.plots import (
    MAX_POINTS,
    Curve,
    _decimate,
    axis_label,
    fmt,
    legend_entry_width,
    line_plot,
    nice_ticks,
)


def test_nice_ticks_cover_range_with_round_steps() -> None:
    assert nice_ticks(0, 1) == [0, 0.25, 0.5, 0.75, 1.0]
    ticks = nice_ticks(-3.7, 12.2)
    assert ticks[0] >= -3.7 and ticks[-1] <= 12.2
    steps = {round(b - a, 9) for a, b in itertools.pairwise(ticks)}
    assert steps == {5.0}
    assert len(nice_ticks(2.0, 2.0)) >= 2
    assert nice_ticks(0, float("inf")) == []


def test_fmt() -> None:
    assert fmt(0) == "0"
    assert fmt(0.25) == "0.25"
    assert fmt(1.5e-6) == "1.50e-6"
    assert fmt(2.5e6) == "2.50e6"


def test_axis_label_nondimensional() -> None:
    assert axis_label("dt", "s") == "dt (s)"
    assert axis_label("cfl", "") == "cfl (nd)"


def test_line_plot_has_labeled_axes_and_legend() -> None:
    svg = line_plot([Curve("a", [0, 1, 2], [1, 2, 3]), Curve("b", [0, 1, 2], [3, 2, 1])], "t (s)", "p (Pa)")
    root = ET.fromstring(svg)
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert "t (s)" in texts and "p (Pa)" in texts
    assert "a" in texts and "b" in texts
    assert len(re.findall(r"<polyline", svg)) == 2


def test_line_plot_decimates_long_series() -> None:
    n = 100_000
    svg = line_plot([Curve("x", list(range(n)), [float(i % 7) for i in range(n)])], "step", "v")
    points = re.search(r'<polyline[^>]* points="([^"]*)"', svg)
    assert points is not None
    assert len(points.group(1).split()) <= MAX_POINTS


def test_decimation_keeps_peaks() -> None:
    n = 50_000
    y = [0.0] * n
    y[12_345] = 9.0
    y[40_000] = -4.0
    xs, ys = _decimate(list(range(n)), y)
    assert len(xs) <= MAX_POINTS
    assert (max(ys), min(ys)) == (9.0, -4.0)
    assert xs == sorted(xs)


def test_legend_wraps_inside_the_plot_instead_of_clipping() -> None:
    labels = ["Fx (N)", "Fy (N)", "Fz (N)", "Mx (N m)", "My (N m)", "Mz (N m)", "drag coefficient (nd)"]
    svg = line_plot([Curve(n, [0, 1], [0, 1]) for n in labels], "t (s)", "value", width=360)
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    entries = [t for t in root.iter(f"{ns}text") if t.get("class") == "m-legend"]
    assert [t.text for t in entries] == labels
    for t in entries:
        assert float(t.get("x", 0)) - 15 + legend_entry_width(t.text or "") <= 360 - 10
    rows = {float(t.get("y", 0)) for t in entries}
    assert len(rows) > 1
    frame = next(r for r in root.iter(f"{ns}rect") if r.get("class") == "m-frame")
    assert float(frame.get("y", 0)) > max(rows)


def test_marked_curves_are_points_not_lines() -> None:
    from mallard_gui.plots import Curve, line_plot

    svg = line_plot(
        [Curve("run", [0, 1], [0, 1]), Curve("ref", [0, 0.5, 1], [0, 0.4, 1])], "t", "y", markers=[False, True]
    )
    assert svg.count("<polyline") == 1 and svg.count("<circle") == 3
