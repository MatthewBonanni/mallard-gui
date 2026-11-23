"""A field's statistics over the cells it is shown on, each cell weighted by its measure (volume, area or length):
the mean, standard deviation and RMS are those of the field over the domain, not of its cells, so a refined
region does not outweigh the rest; the integral is the field times the measure, summed; the histogram gives
the share of the measure in each bin of the shown range."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np


def field_stats(
    values: np.ndarray[Any, Any],
    measures: np.ndarray[Any, Any],
    centers: np.ndarray[Any, Any],
    shown: Sequence[float] | None = None,
    bins: int = 32,
) -> dict[str, Any]:
    finite = np.isfinite(values) & np.isfinite(measures)
    out: dict[str, Any] = {"cells": int(values.size), "nonfinite": int(values.size - finite.sum())}
    v, w, c = values[finite], np.abs(measures[finite]), centers[finite]
    total = float(w.sum())
    if v.size == 0:
        return {**out, "measure": 0.0}
    weights = w if total > 0 else np.ones_like(v)
    mean = float(np.average(v, weights=weights))
    var = float(np.average((v - mean) ** 2, weights=weights))
    lo_i, hi_i = int(np.argmin(v)), int(np.argmax(v))
    lo, hi = (float(shown[0]), float(shown[1])) if shown and shown[1] > shown[0] else (float(v[lo_i]), float(v[hi_i]))
    if hi <= lo:
        hi = lo + (abs(lo) or 1.0) * 1e-6
    hist, _ = np.histogram(np.clip(v, lo, hi), bins=bins, range=(lo, hi), weights=weights)
    return {
        **out,
        "measure": total,
        "min": float(v[lo_i]),
        "max": float(v[hi_i]),
        "argmin": [float(x) for x in c[lo_i]],
        "argmax": [float(x) for x in c[hi_i]],
        "mean": mean,
        "std": var**0.5,
        "rms": float(np.sqrt(np.average(v**2, weights=weights))),
        "integral": float((v * w).sum()),
        "hist": {"lo": lo, "hi": hi, "fractions": [float(h) for h in hist / hist.sum()] if hist.sum() else []},
    }
