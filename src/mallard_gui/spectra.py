"""Spectra of probe time series: the one-sided power spectral density by Welch's method (Hann windows,
half overlap, the mean removed), on the series resampled to its median time step, since snapshots and
CSV rows need not be evenly spaced. The peak's frequency is the shedding frequency of a wake probe."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

MIN_SAMPLES = 8


@dataclass
class Spectrum:
    f: list[float]
    psd: list[float]
    dt: float  # the uniform step the series was resampled to
    segments: int
    peak_f: float | None


def uniform(t: Sequence[float], y: Sequence[float]) -> tuple[np.ndarray, float]:
    """The series on an even grid of its median step, by linear interpolation (repeated times dropped)."""
    tt = np.asarray(t, dtype=float)
    yy = np.asarray(y, dtype=float)
    ok = np.isfinite(tt) & np.isfinite(yy)
    tt, yy = tt[ok], yy[ok]
    order = np.argsort(tt, kind="stable")
    tt, yy = tt[order], yy[order]
    keep = np.concatenate(([True], np.diff(tt) > 0))
    tt, yy = tt[keep], yy[keep]
    if tt.size < MIN_SAMPLES:
        raise ValueError(f"{tt.size} samples: a spectrum needs at least {MIN_SAMPLES}")
    dt = float(np.median(np.diff(tt)))
    n = math.floor(float(tt[-1] - tt[0]) / dt) + 1
    grid = tt[0] + dt * np.arange(n)
    return np.interp(grid, tt, yy), dt


def welch(t: Sequence[float], y: Sequence[float], segments: int = 4) -> Spectrum:
    """PSD in units of y² per Hz, frequencies in Hz (1/time unit), the zero frequency left out."""
    yu, dt = uniform(t, y)
    n = yu.size
    segments = max(1, min(segments, n // MIN_SAMPLES))
    length = 2 * n // (segments + 1) if segments > 1 else n
    step = length // 2 if segments > 1 else length
    window = np.hanning(length) if length > 2 else np.ones(length)
    scale = 1.0 / (np.sum(window**2) / dt)
    acc = np.zeros(length // 2 + 1)
    used = 0
    for start in range(0, n - length + 1, step):
        seg = yu[start : start + length]
        spec = np.fft.rfft((seg - seg.mean()) * window)
        acc += np.abs(spec) ** 2
        used += 1
    psd = acc * scale / max(used, 1)
    psd[1:-1] *= 2.0  # one-sided
    f = np.fft.rfftfreq(length, dt)
    f, psd = f[1:], psd[1:]
    peak = float(f[int(np.argmax(psd))]) if psd.size and np.max(psd) > 0 else None
    return Spectrum([float(v) for v in f], [float(v) for v in psd], dt, used, peak)
