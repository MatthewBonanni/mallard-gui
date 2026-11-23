"""The case view's Clip in Chrome and WebKit: the browser goes on drawing the view (no round trip to orbit a
clipped case), cut by vtk.js where the server's own render cuts it; its settings are a bar along the top of the view,
whose presets, side, slider and mark act on the picture; a 2D case is cut by the plane it opens with."""

from __future__ import annotations

import base64
import io
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from PIL import Image
from playwright import sync_api

from mallard_gui.view.scene import THEMES

from .test_ui import pw  # noqa: F401  (fixtures)
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
VIEW = ".viewport:not(.results-view)"
HIDE = "[...document.querySelector('.viewport').children].slice(1).forEach(e => { e.style.visibility = '%s'; })"


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def pg(request: pytest.FixtureRequest, pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("section"), runs=False)
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
        page.wait_for_function("!window.mallardZoom.framed || window.mallardZoom.framed()", timeout=30_000)
        page.wait_for_timeout(500)
        yield page
        browser.close()


def mask(png: bytes, theme: str) -> np.ndarray[Any, Any]:
    """Where a picture of the view draws something, in cells of 1/64 of its width (pictures of two sizes and
    two renderers compare by where they draw, not pixel by pixel)."""
    img = np.asarray(Image.open(io.BytesIO(png)).convert("RGB").resize((512, 448))).astype(float)
    drawn = np.abs(img - 255 * np.array(THEMES[theme]["background"])).sum(axis=2) > 24
    cells: np.ndarray[Any, Any] = drawn.reshape(56, 8, 64, 8).any(axis=(1, 3))
    return cells


def browser_picture(pg: Any) -> bytes:
    pg.evaluate(HIDE % "hidden")
    try:
        pg.wait_for_timeout(400)
        shot: bytes = pg.screenshot(clip=pg.locator(VIEW).first.bounding_box())
    finally:
        pg.evaluate(HIDE % "")
    return shot


def server_picture(pg: Any) -> bytes:
    """The scene as the server renders it from the browser's camera, at the view's shape."""
    pg.evaluate("trame.trigger('case_camera', [window.mallardZoom.camera('caseview')])")
    box = pg.locator(VIEW).first.bounding_box()
    data = pg.evaluate(
        "([w, h]) => trame.trigger('test_case_image', [w, h])", [round(box["width"]), round(box["height"])]
    )
    return base64.b64decode(data)


def cut_matches(pg: Any) -> tuple[float, np.ndarray[Any, Any]]:
    theme = pg.evaluate("trame.state.get('theme')")
    a, b = browser_picture(pg), server_picture(pg)
    here, there = mask(a, theme), mask(b, theme)
    return float((here == there).mean()), here


def test_clip_cuts_the_case_where_the_browser_draws_it(pg: Any) -> None:
    """Clip handed the view to the server: every orbit and zoom of a clipped case was a round trip (laggy), and
    in 2D the plane it opened with (the page's own) cut nothing."""
    whole = mask(browser_picture(pg), pg.evaluate("trame.state.get('theme')"))
    pg.locator(".toolbar button:visible", has_text="Clip").click()
    pg.wait_for_function("trame.state.get('clip') === true")
    pg.wait_for_timeout(500)
    assert pg.evaluate("trame.state.get('caseview_mode')") == "local"  # the browser still draws
    pg.wait_for_function("window.mallardZoom.clipped() > 0")
    assert pg.evaluate("trame.state.get('sec').axis") == "x"  # a plane that cuts a 2D case
    same, cut = cut_matches(pg)
    assert same > 0.97, "the browser's cut is not the server's"
    cols = np.nonzero(whole.any(axis=0))[0]
    middle = (cols.min() + cols.max()) // 2
    assert not cut[:, : middle - 2].any() and cut[:, middle + 1 :].sum() > 0.4 * whole.sum()  # the half x > 0
    # Orbiting it is the browser's own: the camera moves with no image from the server.
    frames = pg.evaluate("window.__frames = 0; 1")
    box = pg.locator(VIEW).first.bounding_box()
    x, y = box["x"] + 0.7 * box["width"], box["y"] + 0.5 * box["height"]
    before = pg.evaluate("window.mallardZoom.camera('caseview').focalPoint")
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.mouse.move(x - 60, y + 30, steps=6)
    pg.mouse.up()
    assert pg.evaluate("window.mallardZoom.camera('caseview').focalPoint") != before and frames == 1
    assert pg.evaluate("trame.state.get('caseview_mode')") == "local"
    same, _ = cut_matches(pg)
    assert same > 0.97
    pg.keyboard.press("f")
    pg.locator(".toolbar button:visible", has_text="Clip").click()
    pg.wait_for_function("trame.state.get('clip') === false && window.mallardZoom.clipped() === 0")
    assert (mask(browser_picture(pg), pg.evaluate("trame.state.get('theme')")) == whole).mean() > 0.99


def test_the_clips_bar_presets_slider_side_and_mark_act_on_the_picture(pg: Any) -> None:
    """The section's settings were rows appended to the end of the right pane. They are a bar along the top of
    the view, under its toolbar, while Clip is on."""
    theme = pg.evaluate("trame.state.get('theme')")
    whole = mask(browser_picture(pg), theme)
    assert pg.locator(".optbar:visible").count() == 0
    pg.locator(".toolbar button:visible", has_text="Clip").click()
    bar = pg.locator(".optbar:visible")
    bar.wait_for()
    assert "SECTION" in bar.inner_text() and "SECTION" not in pg.locator(".panel.right:visible").inner_text()
    # Along the top of the view, right under its toolbar, over nothing else the view shows there.
    tools, view = pg.locator(".toolbar:visible").bounding_box(), pg.locator(VIEW).first.bounding_box()
    at = bar.bounding_box()
    assert at["y"] == pytest.approx(tools["y"] + tools["height"], abs=2) and at["y"] == pytest.approx(view["y"], abs=2)
    assert at["width"] == pytest.approx(view["width"], abs=2) and at["height"] < 70
    covered = pg.evaluate(
        """view => { const b = document.querySelector(view + ' .optbar').getBoundingClientRect();
        return [...document.querySelectorAll(view + ' :is(.legend, .triad, .ana-panel, .su-banner, .view-notice)')]
          .filter(e => e.offsetParent && e.getBoundingClientRect().top < b.bottom - 1).length; }""",
        VIEW,
    )
    assert covered == 0
    assert [b.inner_text() for b in bar.locator("button.tb").all()][:3] == ["X", "Y", "Flip"]  # no Z in 2D

    def kept() -> np.ndarray[Any, Any]:
        same, cut = cut_matches(pg)
        assert same > 0.97, "the browser's cut is not the server's"
        return cut

    cols, rows = np.nonzero(whole.any(axis=0))[0], np.nonzero(whole.any(axis=1))[0]
    cx, cy = (cols.min() + cols.max()) // 2, (rows.min() + rows.max()) // 2
    slider = pg.locator(".optbar input[aria-label='section position']").bounding_box()
    pg.mouse.click(slider["x"] + 0.75 * slider["width"], slider["y"] + slider["height"] / 2)
    pg.wait_for_function("trame.state.get('sec').at > 5")
    right_part = kept()
    assert not right_part[:, : cx + 4].any() and right_part[:, cx + 8 :].any()  # cut well right of the middle
    value = pg.locator(".optbar input[aria-label='section position value']")
    assert float(value.input_value()) == pytest.approx(pg.evaluate("trame.state.get('sec').at"), rel=1e-3)
    value.click()
    value.press("ControlOrMeta+a")
    pg.keyboard.type("0")
    value.press("Enter")
    pg.wait_for_function("trame.state.get('sec').at === 0")
    bar.locator("button", has_text="Flip").click()
    pg.wait_for_function("trame.state.get('clip_flip') === true")
    left_half = kept()
    assert not left_half[:, cx + 3 :].any() and left_half[:, : cx - 1].sum() > 0.4 * whole.sum()
    bar.locator("button", has_text="Flip").click()
    bar.locator("button", has_text="Y").click()
    pg.wait_for_function("trame.state.get('sec').axis === 'y'")
    upper = kept()  # y > 0: the upper half of the picture
    assert not upper[cy + 3 :, :].any() and upper[: cy - 1, :].sum() > 0.4 * whole.sum()
    # The mark: the plane's line in the accent color, there until Plane is turned off.
    accent = 255 * np.array(THEMES[theme]["accent"])

    def marked() -> int:
        img = np.asarray(Image.open(io.BytesIO(browser_picture(pg))).convert("RGB")).astype(float)
        return int((np.abs(img - accent).sum(axis=2) < 40).sum(axis=1).max())  # its longest run along a row

    assert marked() > 400
    bar.locator("button", has_text="Plane").click()
    pg.wait_for_function("trame.state.get('sec_mark') === false")
    pg.wait_for_timeout(500)
    assert marked() < 100
    bar.locator("button", has_text="Plane").click()
    bar.locator("button", has_text="Numbers").click()
    sync_api.expect(bar.locator("input[aria-label='origin x']")).to_be_visible()
    bar.locator("button", has_text="Numbers").click()
    bar.locator("button", has_text="X").click()
    bar.locator("button[title^='Turn']").click()
    pg.wait_for_function("trame.state.get('clip') === false")
    assert pg.locator(".optbar:visible").count() == 0
    assert (mask(browser_picture(pg), theme) == whole).mean() > 0.99
