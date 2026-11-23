"""Spectra of probe series: Welch's PSD on unevenly sampled data finds the shedding frequency, Parseval holds,
and log axes plot decades with the peak marked."""

from __future__ import annotations

import math
import random

import pytest

from mallard_gui.plots import Curve, line_plot
from mallard_gui.spectra import uniform, welch


def test_peak_frequency_of_an_unevenly_sampled_sine() -> None:
    rng = random.Random(1)
    t, x = [], 0.0
    for _ in range(2000):
        x += 0.01 * (1 + 0.3 * (rng.random() - 0.5))
        t.append(x)
    y = [3.0 + math.sin(2 * math.pi * 1.64 * ti) for ti in t]  # St 0.164 at D = 1, U = 0.1
    s = welch(t, y)
    assert s.segments == 4
    assert s.peak_f == pytest.approx(1.64, rel=0.05)
    assert s.f[0] > 0  # the mean is not a peak


def test_parseval_the_psd_integrates_to_the_variance() -> None:
    t = [0.001 * k for k in range(4096)]
    y = [math.sin(2 * math.pi * 50 * ti) + 0.5 * math.cos(2 * math.pi * 120 * ti) for ti in t]
    s = welch(t, y, segments=1)
    df = s.f[1] - s.f[0]
    variance = 0.5 + 0.125
    assert sum(s.psd) * df == pytest.approx(variance, rel=0.1)  # Hann leaks, it does not lose power


def test_too_few_samples_and_repeated_times() -> None:
    with pytest.raises(ValueError, match="at least"):
        welch([0, 1, 2], [1, 2, 3])
    y, dt = uniform([0, 1, 1, 2, 3, 4, 5, 6, 7, 8], [0, 1, 9, 2, 3, 4, 5, 6, 7, 8])
    assert dt == 1.0 and list(y[:3]) == [0, 1, 2]


def test_log_axes_tick_decades_and_mark_the_peak() -> None:
    f = [10 ** (k / 10) for k in range(-10, 21)]
    svg = line_plot([Curve("P", f, [1 / v for v in f])], "f (Hz)", "PSD", logx=True, logy=True, mark_x=1.0)
    assert ">0.1<" in svg and ">100<" in svg and 'class="m-mark"' in svg
    assert line_plot([Curve("P", [0, -1], [0, 1])], "f", "PSD", logx=True).count("no data yet") == 1
