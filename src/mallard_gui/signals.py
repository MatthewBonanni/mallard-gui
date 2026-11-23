"""Signal analysis of probe, force and monitor histories: spectra, autocorrelation and the statistics of a
window with their confidence.

The methods are Mallard's own (its probes' `spectra` and `[statistics.convergence]`), so what the GUI shows
of a file agrees with what the run reports of its samples: Welch's density normalized as
`scipy.signal.welch(scaling="density")`, the peak refined by a parabola through the logarithms of the
largest bin and its neighbors; MSER-5 for the end of the initial transient; non-overlapping batch means with
Student's t for the intervals; Sokal's automatic window for the integrated autocorrelation time, and a
window converged only when each batch spans at least 10 of it.

TODO(#1362): `mallard post signal` would compute these from a run's files where the run is; until then
they are computed here on the samples read by byte range.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import ArrayLike

MIN_SAMPLES = 8
WINDOWS = ("hann", "hamming", "blackman", "rectangular")
DETRENDS = ("mean", "linear", "none")
BATCHES = 20
SOKAL_C = 5.0
MSER_BATCH = 5


def _arrays(t: ArrayLike, y: ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    """Finite samples in time order, repeated times dropped (a continued run repeats its restart's)."""
    tt = np.asarray(t, dtype=float)
    yy = np.asarray(y, dtype=float)
    ok = np.isfinite(tt) & np.isfinite(yy)
    tt, yy = tt[ok], yy[ok]
    order = np.argsort(tt, kind="stable")
    tt, yy = tt[order], yy[order]
    keep = np.concatenate(([True], np.diff(tt) > 0)) if tt.size else np.zeros(0, dtype=bool)
    return tt[keep], yy[keep]


def window_of(t: ArrayLike, y: ArrayLike, t0: float | None, t1: float | None) -> tuple[np.ndarray, ...]:
    tt, yy = _arrays(t, y)
    lo = -math.inf if t0 is None else t0
    hi = math.inf if t1 is None else t1
    sel = (tt >= lo) & (tt <= hi)
    return tt[sel], yy[sel]


def uniform(t: ArrayLike, y: ArrayLike) -> tuple[np.ndarray, float]:
    """The series on an even grid of its median step, by linear interpolation: snapshots and CSV rows need
    not be evenly spaced."""
    tt, yy = _arrays(t, y)
    if tt.size < MIN_SAMPLES:
        raise ValueError(f"{tt.size} samples: a spectrum needs at least {MIN_SAMPLES}")
    dt = float(np.median(np.diff(tt)))
    n = math.floor(float(tt[-1] - tt[0]) / dt + 1e-9) + 1
    grid = tt[0] + dt * np.arange(n)
    return np.interp(grid, tt, yy), dt


def window_function(name: str, n: int) -> np.ndarray:
    """Periodic windows, as `scipy.signal.get_window` makes them for spectra."""
    k = np.arange(n)
    if name == "rectangular" or n < 2:
        return np.ones(n, dtype=float)
    a = 2 * np.pi * k / n
    if name == "hann":
        return 0.5 - 0.5 * np.cos(a)
    if name == "hamming":
        return 0.54 - 0.46 * np.cos(a)
    if name == "blackman":
        return 0.42 - 0.5 * np.cos(a) + 0.08 * np.cos(2 * a)
    raise ValueError(f"window {name!r}: one of {', '.join(WINDOWS)}")


def _detrend(seg: np.ndarray, how: str) -> np.ndarray:
    if how == "mean":
        return seg - seg.mean()
    if how == "linear":
        x = np.arange(seg.size, dtype=float)
        a, b = np.polyfit(x, seg, 1)
        out: np.ndarray = seg - (float(a) * x + float(b))
        return out
    if how == "none":
        return seg
    raise ValueError(f"detrend {how!r}: one of {', '.join(DETRENDS)}")


@dataclass
class Spectrum:
    f: list[float]
    psd: list[float]
    dt: float  # the uniform step the series was resampled to
    segments: int
    peak_f: float | None
    length: int = 0  # samples per segment
    df: float = 0.0  # bin width
    peak_psd: float | None = None
    window: str = "hann"
    overlap: float = 0.5

    def strouhal(self, length: float, velocity: float) -> float | None:
        """St = f L / U of the peak."""
        if self.peak_f is None or not velocity:
            return None
        return self.peak_f * length / velocity


def _refine(f: np.ndarray, psd: np.ndarray, k: int) -> float:
    """The peak's frequency from a parabola through the logarithms of bin k and its neighbors."""
    if k <= 0 or k >= psd.size - 1 or min(psd[k - 1], psd[k], psd[k + 1]) <= 0:
        return float(f[k])
    a, b, c = np.log(psd[k - 1]), np.log(psd[k]), np.log(psd[k + 1])
    den = a - 2 * b + c
    shift = 0.5 * (a - c) / den if den != 0 else 0.0
    return float(f[k] + max(-0.5, min(0.5, shift)) * (f[1] - f[0]))


def welch(
    t: ArrayLike,
    y: ArrayLike,
    segments: int = 4,
    *,
    segment_length: int | None = None,
    overlap: float = 0.5,
    window: str = "hann",
    detrend: str = "mean",
) -> Spectrum:
    """One-sided PSD in units of y² per Hz (y² per 1/time unit), the zero frequency left out.

    The segment is `segment_length` samples of the resampled series if given, else the length that cuts the
    series into `segments` segments at this overlap; the last segment ends at the latest sample."""
    if not 0.0 <= overlap < 1.0:
        raise ValueError("overlap: from 0 up to (not including) 1")
    yu, dt = uniform(t, y)
    n = yu.size
    if segment_length is None:
        segments = max(1, min(segments, n // MIN_SAMPLES))
        length = n if segments == 1 else int(n / (1 + (segments - 1) * (1 - overlap)))
    else:
        length = int(segment_length)
    length = max(MIN_SAMPLES, min(length, n))
    step = max(1, round(length * (1 - overlap)))
    w = window_function(window, length)
    scale = dt / np.sum(w**2)
    acc = np.zeros(length // 2 + 1)
    starts = list(range(n - length, -1, -step))[::-1]
    for start in starts:
        seg = _detrend(yu[start : start + length], detrend)
        acc += np.abs(np.fft.rfft(seg * w)) ** 2
    psd = acc * scale / len(starts)
    psd[1 : (length + 1) // 2] *= 2.0  # one-sided: every bin but zero and, for even lengths, Nyquist
    f = np.fft.rfftfreq(length, dt)
    f, psd = f[1:], psd[1:]
    peak = peak_psd = None
    if psd.size and np.max(psd) > 0:
        k = int(np.argmax(psd))
        peak, peak_psd = _refine(f, psd, k), float(psd[k])
    return Spectrum(
        [float(v) for v in f],
        [float(v) for v in psd],
        dt,
        len(starts),
        peak,
        length,
        float(1.0 / (length * dt)),
        peak_psd,
        window,
        overlap,
    )


@dataclass
class Autocorrelation:
    lag: list[float]  # in time units
    rho: list[float]
    tau: float  # integrated autocorrelation time, in samples (Sokal's window)
    dt: float  # the uniform step
    zero: float | None  # the first zero crossing's lag

    @property
    def integral_time(self) -> float:
        return self.tau * self.dt / 2.0


def _acf(x: np.ndarray) -> np.ndarray:
    x = x - x.mean()
    n = x.size
    size = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(x, size)
    r = np.fft.irfft(spec * np.conj(spec), size)[:n]
    return r / r[0] if r[0] > 0 else np.zeros(n)


def sokal_tau(rho: np.ndarray) -> float:
    """1 + 2 sum rho_k up to the smallest window M >= c tau(M) (Sokal), in samples."""
    tau = 1.0
    for m in range(1, rho.size):
        tau += 2.0 * float(rho[m])
        if m >= SOKAL_C * tau:
            return max(tau, 1.0)
    return max(tau, 1.0)


def autocorrelation(t: ArrayLike, y: ArrayLike, max_lag: float | None = None) -> Autocorrelation:
    yu, dt = uniform(t, y)
    rho = _acf(yu)
    tau = sokal_tau(rho)
    keep = rho.size // 2 if max_lag is None else min(rho.size, int(max_lag / dt) + 1)
    crossing = np.nonzero(rho[1:] <= 0)[0]
    zero = float((crossing[0] + 1) * dt) if crossing.size else None
    return Autocorrelation([float(k * dt) for k in range(keep)], [float(v) for v in rho[:keep]], tau, dt, zero)


# Student's t, for the intervals, without scipy.


def _betacf(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function (Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_cdf(x: float, dof: float) -> float:
    tail = 0.5 * _betainc(dof / 2.0, 0.5, dof / (dof + x * x))
    return 1.0 - tail if x >= 0 else tail


def t_quantile(p: float, dof: float) -> float:
    """The p quantile of Student's t with `dof` degrees of freedom, by bisection on its CDF."""
    lo, hi = -1e3, 1e3
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_cdf(mid, dof) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def mser5(y: np.ndarray) -> int | None:
    """The samples to delete as initial transient (MSER-5), or None while the series still drifts: the
    minimum is searched over the first half of the batches, and one at the end of the search is rejected."""
    k = y.size // MSER_BATCH
    if k < 4:
        return None
    z = y[y.size - k * MSER_BATCH :].reshape(k, MSER_BATCH).mean(axis=1)
    lead = y.size - k * MSER_BATCH
    rev = z[::-1]
    n = np.arange(1, k + 1, dtype=float)
    s1 = np.cumsum(rev)
    s2 = np.cumsum(rev * rev)
    # Statistic of the tail z[d:], for each d, from the reversed prefix sums.
    m = k - np.arange(k // 2 + 1)
    tail_n = n[m - 1]
    mean = s1[m - 1] / tail_n
    ss = s2[m - 1] - tail_n * mean * mean
    stat = ss / (tail_n * tail_n)
    d = int(np.argmin(stat))
    if d == stat.size - 1:
        return None
    return lead + d * MSER_BATCH


@dataclass
class Interval:
    value: float
    half: float  # the half width at the confidence

    @property
    def relative(self) -> float:
        return abs(self.half / self.value) if self.value else math.inf


@dataclass
class WindowStats:
    n: int
    t0: float
    t1: float
    mean: float
    rms: float  # of the fluctuation: the standard deviation
    minimum: float
    maximum: float
    mean_ci: Interval
    variance_ci: Interval
    tau: float  # samples
    n_eff: float
    batch: int  # samples per batch
    confidence: float
    tolerance: float
    converged: bool
    reason: str  # why not converged, in words
    t_stationary: float | None  # where MSER-5 puts the end of the transient, None while drifting
    running_t: list[float] = field(default_factory=list)
    running_mean: list[float] = field(default_factory=list)
    running_lo: list[float] = field(default_factory=list)
    running_hi: list[float] = field(default_factory=list)


def _batch_ci(x: np.ndarray, batches: int, q: float) -> Interval:
    m = x.size // batches
    if m < 1:
        return Interval(float(x.mean()) if x.size else math.nan, math.inf)
    used = x[x.size - m * batches :]
    means = used.reshape(batches, m).mean(axis=1)
    s = float(np.std(means, ddof=1))
    return Interval(float(used.mean()), q * s / math.sqrt(batches))


def statistics(
    t: ArrayLike,
    y: ArrayLike,
    t0: float | None = None,
    t1: float | None = None,
    *,
    confidence: float = 0.95,
    tolerance: float = 0.01,
    batches: int = BATCHES,
    checkpoints: int = 120,
) -> WindowStats:
    """Mean, RMS and extremes of the samples in [t0, t1], the confidence intervals of the mean and the
    variance by batch means, and whether the mean has converged to `tolerance` of itself. The running mean
    from t0, with its interval at `checkpoints` times, shows the convergence."""
    tt, yy = window_of(t, y, t0, t1)
    n = int(yy.size)
    if n < 2 * batches:
        raise ValueError(f"{n} samples in the window: the intervals need at least {2 * batches}")
    q = t_quantile(0.5 + confidence / 2.0, batches - 1)
    mean_ci = _batch_ci(yy, batches, q)
    var = (yy - yy.mean()) ** 2
    variance_ci = _batch_ci(var, batches, q)
    tau = sokal_tau(_acf(yy))
    batch = n // batches
    reasons = []
    if batch < 10 * tau:
        reasons.append(f"batches of {batch} samples, under 10 τ = {10 * tau:.0f}: run longer")
    if mean_ci.relative > tolerance:
        reasons.append(f"mean ± {100 * mean_ci.relative:.2g} %, over ± {100 * tolerance:.2g} %")
    trans = mser5(yy)
    # Running mean and its interval, at checkpoints from 2 batches of samples on.
    cs = np.concatenate(([0.0], np.cumsum(yy)))
    rt, rm, rlo, rhi = [], [], [], []
    for k in np.unique(np.linspace(2 * batches, n, min(checkpoints, n - 2 * batches + 1)).astype(int)):
        m = k // batches
        start = k - m * batches
        edges = start + m * np.arange(batches + 1)
        bm = (cs[edges[1:]] - cs[edges[:-1]]) / m
        mu = (cs[k] - cs[start]) / (m * batches)
        half = q * float(np.std(bm, ddof=1)) / math.sqrt(batches)
        rt.append(float(tt[k - 1]))
        rm.append(float(mu))
        rlo.append(float(mu - half))
        rhi.append(float(mu + half))
    return WindowStats(
        n=n,
        t0=float(tt[0]),
        t1=float(tt[-1]),
        mean=float(yy.mean()),
        rms=float(yy.std()),
        minimum=float(yy.min()),
        maximum=float(yy.max()),
        mean_ci=mean_ci,
        variance_ci=variance_ci,
        tau=tau,
        n_eff=n / tau,
        batch=batch,
        confidence=confidence,
        tolerance=tolerance,
        converged=not reasons,
        reason="; ".join(reasons),
        t_stationary=float(tt[trans]) if trans is not None else None,
        running_t=rt,
        running_mean=rm,
        running_lo=rlo,
        running_hi=rhi,
    )


def transient_end(t: ArrayLike, y: ArrayLike) -> float | None:
    """The time MSER-5 puts the end of the initial transient at, None while the series drifts."""
    tt, yy = _arrays(t, y)
    d = mser5(yy)
    return float(tt[d]) if d is not None else None


def running_mean(t: ArrayLike, y: ArrayLike, width: float) -> tuple[list[float], list[float]]:
    """The centered running mean over `width` time units (each sample weighted equally)."""
    tt, yy = _arrays(t, y)
    if not tt.size or width <= 0:
        return [float(v) for v in tt], [float(v) for v in yy]
    cs = np.concatenate(([0.0], np.cumsum(yy)))
    lo = np.searchsorted(tt, tt - width / 2, side="left")
    hi = np.searchsorted(tt, tt + width / 2, side="right")
    return [float(v) for v in tt], [float(v) for v in (cs[hi] - cs[lo]) / (hi - lo)]
