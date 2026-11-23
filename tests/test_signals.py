from __future__ import annotations

import math

import numpy as np
import pytest

from mallard_gui.signals import (
    autocorrelation,
    running_mean,
    statistics,
    t_quantile,
    transient_end,
    welch,
)


def test_welch_density_integrates_to_the_variance_and_finds_the_shedding_peak() -> None:
    t = np.arange(0, 200, 0.1)
    y = 0.3 * np.sin(2 * np.pi * 0.1647 * t) + 0.05
    sp = welch(t, y, segment_length=1000, overlap=0.5, window="hann")
    df = sp.f[1] - sp.f[0]
    assert sum(sp.psd) * df == pytest.approx(0.3**2 / 2, rel=0.02)
    assert sp.peak_f == pytest.approx(0.1647, abs=0.1 * df)
    assert sp.segments == 3 and sp.length == 1000
    assert sp.strouhal(1.0, 0.2) == pytest.approx(0.1647 / 0.2, rel=0.01)


def test_welch_rejects_bad_controls() -> None:
    t = np.arange(100.0)
    with pytest.raises(ValueError, match="window"):
        welch(t, np.sin(t), window="kaiser")
    with pytest.raises(ValueError, match="overlap"):
        welch(t, np.sin(t), overlap=1.0)


def test_t_quantile_matches_the_tables() -> None:
    assert t_quantile(0.975, 19) == pytest.approx(2.093024, abs=1e-5)
    assert t_quantile(0.995, 5) == pytest.approx(4.032143, abs=1e-5)


def _ar1(phi: float, n: int, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n)
    x = np.empty(n)
    x[0] = e[0]
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def test_autocorrelation_time_of_ar1() -> None:
    phi = 0.8
    x = _ar1(phi, 40_000)
    ac = autocorrelation(np.arange(x.size, dtype=float), x)
    assert ac.tau == pytest.approx((1 + phi) / (1 - phi), rel=0.1)
    assert ac.rho[1] == pytest.approx(phi, abs=0.02)


def test_statistics_converge_only_with_enough_independent_samples() -> None:
    x = 10.0 + _ar1(0.5, 50_000)
    t = np.arange(x.size, dtype=float)
    long = statistics(t, x, tolerance=0.01)
    assert long.converged and long.reason == ""
    assert long.mean_ci.value == pytest.approx(10.0, abs=3 * long.mean_ci.half)
    assert long.running_mean[-1] == pytest.approx(long.mean, rel=1e-3)
    short = statistics(t[:200], x[:200], tolerance=0.0001)
    assert not short.converged and "±" in short.reason


def test_transient_end_finds_an_offset_and_rejects_a_drift() -> None:
    rng = np.random.default_rng(3)
    y = rng.standard_normal(4000)
    y[:1000] += 5.0
    end = transient_end(np.arange(y.size, dtype=float), y)
    assert end is not None and 995 <= end <= 1060
    assert transient_end(np.arange(2000.0), np.arange(2000.0)) is None


def test_running_mean_is_centered_over_its_width() -> None:
    t = np.arange(10.0)
    _, m = running_mean(t, t, 2.0)
    assert m[5] == pytest.approx(5.0) and m[0] == pytest.approx(0.5)
    assert math.isclose(running_mean(t, t, 0)[1][3], 3.0)
