"""Particles in the results viewer, in a real browser: the run's particle file drawn over the flow, colored
by a quantity with its own colorbar, then one color, then hidden."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .test_ui import app_url, open_box_run, page, pw, root  # noqa: F401  (fixtures)
from .test_viewer import PARTICLES

pytestmark = pytest.mark.ui


def test_the_runs_particles_are_drawn_colored_by_a_quantity(page: Any, root: Path) -> None:  # noqa: F811
    run = root / "case" / "runs" / "box-1"
    (run / "input.toml").write_text((run / "input.toml").read_text() + '[particles]\nfile = "cloud.csv"\n')
    (run / "cloud.csv").write_text(PARTICLES)
    pg = page
    pg.click(".rail button[aria-label='Results']")
    open_box_run(pg)
    pg.get_by_role("button", name="images", exact=True).click()
    row = ".tree-row:has(input[aria-label='show cloud'])"
    pg.wait_for_selector(row, timeout=20_000)
    assert pg.locator(".panel-head:has(> span:text-is('Particles')) .mono").inner_text() == "at step 30"
    pg.wait_for_selector(".pcolorbar >> text=particles: turns")
    box = pg.locator(".results-view").first.bounding_box()
    pg.wait_for_timeout(500)
    colored = pg.screenshot(clip=box)
    pg.select_option("select[aria-label='particle color']", "")
    pg.wait_for_selector(".pcolorbar", state="detached")
    pg.click(row)
    pg.wait_for_function("!trame.state.get('v_particles').length")
    pg.wait_for_timeout(1000)
    assert pg.screenshot(clip=box) != colored
