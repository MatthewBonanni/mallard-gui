"""The case tree's Initial state shows the initial field on the case view at once: the field Mallard writes for
the case, colored over the domain with its colorbar, a field and a colormap to choose; held for the input, so a
second visit draws without Mallard; gone when another node is chosen."""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from PIL import Image
from playwright import sync_api

from mallard_gui.viewer.colormaps import color_at

from .test_ui import pw  # noqa: F401  (fixtures)
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
VIEW = ".viewport:not(.results-view)"


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def pg(request: pytest.FixtureRequest, pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("initial"), runs=False)
    with serve(project, env) as url:
        try:
            launch = pw.webkit.launch if request.param == "webkit" else pw.chromium.launch
            browser = launch(headless=True)
        except sync_api.Error:
            pytest.skip(f"{request.param} is not installed")
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(url)
        page.wait_for_selector(".tree-row", state="attached", timeout=30_000)
        page.click(".rail button[aria-label='Case setup']")
        page.wait_for_function("trame.state.get('blocks').length > 0", timeout=30_000)
        yield page
        browser.close()


def view(pg: Any) -> np.ndarray[Any, Any]:
    """The case view's pixels (H, W, 3) without what the page lays over it."""
    hide = "[...document.querySelector('.viewport').children].slice(1).forEach(e => { e.style.visibility = '%s'; })"
    pg.evaluate(hide % "hidden")
    try:
        pg.wait_for_timeout(300)
        shot = pg.screenshot(clip=pg.locator(VIEW).first.bounding_box())
    finally:
        pg.evaluate(hide % "")
    return np.asarray(Image.open(io.BytesIO(shot)).convert("RGB")).astype(float)


def colored(img: np.ndarray[Any, Any], colormap: str) -> float:
    """The share of the view drawn in the colormap's colors: within a small distance of some color of it."""
    ramp = 255 * np.array([color_at(colormap, k / 31) for k in range(32)])
    far = np.abs(img[::4, ::4, None, :] - ramp[None, None, :, :]).sum(axis=3).min(axis=2)
    return float((far < 40).mean())


def pick_colormap(pg: Any, name: str) -> None:
    """The colormap from its dropdown, where each map shows its swatch."""
    pg.locator(".ana-panel button[aria-label='colormap']").click()
    item = pg.locator(".dd-pop .dd-item", has=pg.locator(f".dd-label:text-is('{name}')"))
    assert item.locator(".dd-swatch svg").count() == 1
    item.click()


def test_initial_state_in_the_case_tree_shows_the_initial_field(pg: Any) -> None:
    pg.locator(".case-tree .tree-row", has_text="Mesh").first.click()
    plain = view(pg)
    assert colored(plain, "Fast") < 0.05  # the mesh and the zones: nothing in a field's colors
    pg.locator(".case-tree .tree-row", has_text="Initial state").first.click()
    pg.wait_for_selector(".ana-panel select[aria-label='initial field']", timeout=20_000)
    sync_api.expect(pg.locator(".ana-tabs .tab.on")).to_have_text("Initial state")
    assert pg.evaluate("trame.state.get('ana_field')") == "U"  # the field that varies, not the uniform RHO
    pg.wait_for_selector(".ana-colorbar .cb-label >> text=/U/")
    shown = view(pg)
    assert colored(shown, "Fast") > 0.25, "the domain is not drawn in the field's colors"
    # A field and a colormap to choose.
    pg.locator(".ana-panel select[aria-label='initial field']").select_option("P")
    pg.wait_for_selector(".ana-colorbar .cb-label >> text=/P/")
    pick_colormap(pg, "Cool to Warm")
    pg.wait_for_function("trame.state.get('ana_cmap') === 'Cool to Warm'")
    pg.wait_for_timeout(600)
    assert colored(view(pg), "Cool to Warm") > 0.25
    pick_colormap(pg, "Fast")
    # Another node: the preview goes, and the view is the case again.
    pg.locator(".case-tree .tree-row", has_text="Mesh").first.click()
    sync_api.expect(pg.locator(".ana-panel")).to_have_count(0)
    pg.wait_for_timeout(600)
    assert colored(view(pg), "Fast") < 0.05
    # Again: held for this input, drawn without asking Mallard.
    asked = pg.evaluate("trame.trigger('test_initial_states')")
    pg.locator(".case-tree .tree-row", has_text="Initial state").first.click()
    pg.wait_for_selector(".ana-panel select[aria-label='initial field']")
    pg.wait_for_timeout(600)
    assert colored(view(pg), "Fast") > 0.25
    assert pg.evaluate("trame.trigger('test_initial_states')") == asked == 1
    pg.locator(".case-tree .tree-row", has_text="Mesh").first.click()
