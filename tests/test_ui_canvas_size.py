"""A view's canvas fills its view whatever size it was last drawn at. vtk.js sizes the canvas's pixels after the
view's box changes; until it has, the canvas (styled with a width alone) kept the shape of its old pixels, taller
or shorter than the view: a view resized by a splitter overflowed it for a moment."""

from __future__ import annotations

from typing import Any

import pytest

from .test_ui import app_url, page, pw, root  # noqa: F401  (fixtures)

pytestmark = pytest.mark.ui

BOXES = """() => { const c = document.querySelector('.viewport:not(.results-view) canvas');
  const r = c.getBoundingClientRect(), v = c.closest('.viewport').getBoundingClientRect();
  return {canvas: [r.width, r.height], view: [v.width, v.height]}; }"""


def test_the_case_views_canvas_fills_the_view_whatever_its_pixels(page: Any) -> None:  # noqa: F811
    pg = page
    pg.click(".rail button[aria-label='Case setup']")
    pg.wait_for_selector(".viewport:not(.results-view) canvas", state="visible", timeout=30_000)
    # As after a resize, before vtk.js has sized the pixels: pixels of another shape than the view.
    pg.evaluate(
        "(() => { const c = document.querySelector('.viewport:not(.results-view) canvas');"
        " c.width = 1234; c.height = 1536; })()"
    )
    boxes = pg.evaluate(BOXES)
    assert boxes["canvas"] == pytest.approx(boxes["view"], abs=1)
    pg.set_viewport_size({"width": 1300, "height": 800})  # and vtk.js sizes them when the view changes
    pg.wait_for_function(
        "(() => { const c = document.querySelector('.viewport:not(.results-view) canvas');"
        " const r = c.getBoundingClientRect(); return c.width === Math.round(r.width * devicePixelRatio)"
        " && c.height === Math.round(r.height * devicePixelRatio); })()"
    )
    pg.set_viewport_size({"width": 1440, "height": 900})
