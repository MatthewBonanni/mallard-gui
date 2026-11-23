"""A field's statistics weight each cell by its measure: a refined region does not outweigh the rest."""

from __future__ import annotations

import numpy as np
import pytest

from mallard_gui.viewer.stats import field_stats


def test_statistics_are_of_the_field_over_the_domain_not_of_its_cells() -> None:
    values = np.array([0.0, 10.0, np.nan])
    measures = np.array([3.0, 1.0, 1.0])
    centers = np.array([[0.0, 0, 0], [1.0, 0, 0], [2.0, 0, 0]])
    s = field_stats(values, measures, centers, shown=(0.0, 10.0), bins=2)
    assert (s["mean"], s["integral"], s["measure"], s["nonfinite"]) == (2.5, 10.0, 4.0, 1)
    assert s["std"] == pytest.approx(np.sqrt(18.75)) and s["rms"] == pytest.approx(5.0)
    assert s["hist"]["fractions"] == [0.75, 0.25] and s["argmax"] == [1.0, 0.0, 0.0]
