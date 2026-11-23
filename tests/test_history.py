"""Histories of long runs in bounded memory: min/max/last per bin at a few resolutions."""

from __future__ import annotations

import math

from mallard_gui.csvseries import CsvFollower
from mallard_gui.history import History


def test_memory_is_bounded_and_the_coarsest_level_reaches_the_first_sample() -> None:
    h = History(cap=100, factor=4)
    for i in range(100_000):
        h.append(float(i), math.sin(i / 50.0))
    assert len(h.levels) > 3
    assert all(len(level.bins) <= 125 for level in h.levels)
    assert h.span == (0.0, 99_999.0)
    assert h.t[-1] == 99_999.0 and len(h.t) <= 125  # the finest level: the latest samples themselves
    assert sum(b.n for b in h.levels[-1].all()) == 100_000


def test_a_curve_costs_its_budget_and_keeps_every_peak() -> None:
    h = History(cap=200, factor=8)
    for i in range(50_000):
        h.append(i * 0.01, 1.0 if i == 31_337 else 0.0)
    xs, ys = h.curve(max_points=300)
    assert len(xs) <= 300 and xs == sorted(xs)
    assert max(ys) == 1.0 and xs[ys.index(1.0)] == 313.37  # the spike, at its time
    near = h.curve(498.495, 499.995, max_points=1000)
    assert len(near[0]) == 150 and near[0][0] > 498.49  # the latest window: its samples themselves
    far = h.curve(100.0, 101.0, max_points=1000)
    assert 0 < len(far[0]) < 100  # an early window: binned


def test_a_zoom_past_the_levels_held_rereads_only_its_byte_range() -> None:
    rows = "".join(f"{i},{i * 0.1:.1f},{i % 7}\n" for i in range(5000))
    data = f"step,t,p\n# units: 1,s,Pa\n{rows}".encode()
    f = CsvFollower("probe.csv", lambda off: data[off:])
    f.poll()
    h = f.data.series[("p", 0)]
    h.cap = 50  # as a long run would leave it
    for i in range(5000, 5100):
        h.append(i * 0.1, float(i % 7))
    assert h.level_for(10.0, 12.0, 1000) > 0  # held only coarsely there
    rng = h.offsets(10.0, 12.0)
    assert rng is not None
    start, end = rng
    assert data[start:].startswith(b"0,0.0,")  # the coarse bin holding t 10..12: rows 0-511
    zoom = f.data.reread(data[start:end], start).series[("p", 0)]
    assert zoom.t[0] <= 10.0 and zoom.t[-1] >= 12.0 and len(zoom.t) == 512
    assert zoom.values[zoom.t.index(10.5)] == 105 % 7
