"""The orientation triad in the case view and the results view (images and browser-drawn), in Chrome and WebKit:
drawn in a corner of the view clear of the toolbar, the legend and the other overlays, its axes where the
view's camera sees x, y and z, turning with a preset and with an orbit, legible in both themes; no label or
handle is drawn on it."""

from __future__ import annotations

import io
import math
from collections.abc import Iterator, Sequence
from typing import Any

import numpy as np
import pytest
from PIL import Image
from playwright import sync_api

from mallard_gui.view.scene import THEMES, contrast

from .test_ui import pw  # noqa: F401  (fixtures)
from .test_ui_view_camera import RUN_ROWS
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
CASE = ".viewport:not(.results-view)"
AXES = """view => Object.fromEntries([...document.querySelectorAll(view + ' .triad .axis')].map(g =>
  [g.classList[1], [+g.dataset.x, +g.dataset.y, +g.dataset.toward]]))"""
CAMERA = """ref => { const c = window.mallardZoom.camera(ref);
  return c && {position: c.position, focal_point: c.focalPoint, view_up: c.viewUp}; }"""
# The triad's box against everything else laid over the view, and the bars above it.
CLEAR = """view => { const v = document.querySelector(view), t = v.querySelector('.triad').getBoundingClientRect();
  const box = v.getBoundingClientRect();
  const others = [...v.querySelectorAll('.legend, .readout, .colorbar, .ana-panel, .ana-colorbar, .su-banner, '
    + '.su-speed, .view-notice'), ...document.querySelectorAll('.toolbar, .optbar, .tabs')]
    .filter(e => e.offsetParent && getComputedStyle(e).visibility === 'visible').map(e => e.getBoundingClientRect());
  return {inside: t.left >= box.left && t.right <= box.right && t.top >= box.top && t.bottom <= box.bottom,
    size: [t.width, t.height],
    over: others.filter(o => t.left < o.right && t.right > o.left && t.top < o.bottom && t.bottom > o.top).length}; }"""


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def pg(request: pytest.FixtureRequest, pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    """The app on a 3D case (the captured box) with the 3D run of the browser tests' world."""
    project, env = make_world(tmp_path_factory.mktemp("triad"))
    with serve(project, env, check="box_check.json") as url:
        try:
            launch = pw.webkit.launch if request.param == "webkit" else pw.chromium.launch
            browser = launch(headless=True)
        except sync_api.Error:
            pytest.skip(f"{request.param} is not installed")
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(url)
        page.wait_for_selector(".tree-row", state="attached", timeout=30_000)
        yield page
        browser.close()


def seen(cam: dict[str, Sequence[float]]) -> dict[str, list[float]]:
    """Where a camera sees each axis: across, up and toward the viewer."""
    f = [a - b for a, b in zip(cam["focal_point"], cam["position"], strict=True)]
    n = math.hypot(*f)
    f = [c / n for c in f]
    up = cam["view_up"]
    r = [f[1] * up[2] - f[2] * up[1], f[2] * up[0] - f[0] * up[2], f[0] * up[1] - f[1] * up[0]]
    n = math.hypot(*r)
    r = [c / n for c in r]
    u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]]
    return {a: [r[i], u[i], -f[i]] for i, a in enumerate("xyz")}


def matches(pg: Any, view: str, cam: dict[str, Sequence[float]]) -> None:
    want = seen(cam)
    pg.wait_for_function(
        f"want => {{ const got = ({AXES})('{view}'); return Object.keys(want).every(a => got[a] && "
        "got[a].every((v, i) => Math.abs(v - want[a][i]) < 2e-3)); }",
        arg=want,
        timeout=10_000,
    )


def drag(pg: Any, view: str, dx: float, dy: float) -> None:
    box = pg.locator(view).first.bounding_box()
    x, y = box["x"] + 0.45 * box["width"], box["y"] + 0.6 * box["height"]
    pg.mouse.move(x - 4, y - 4)
    pg.mouse.move(x, y, steps=2)
    pg.mouse.down()
    pg.mouse.move(x + dx, y + dy, steps=6)
    pg.mouse.up()


def orbit(pg: Any, ref: str, azimuth: float, elevation: float) -> None:
    """An orbit of a browser-drawn view, as its interactor turns the camera. Not a synthetic drag: vtk.js takes a
    move more than 200 ms after the last for the start of a new one, and headless Chrome's software GL draws a
    frame of these views in about that, so a drag of a few moves was sometimes no orbit at all."""
    pg.evaluate(
        """([ref, a, e]) => { const v = window.mallardZoom.internals(ref), c = v.renderer.getActiveCamera();
        c.azimuth(a); c.elevation(e); c.orthogonalizeViewUp(); v.renderer.resetCameraClippingRange();
        v.renderWindow.render(); }""",
        [ref, azimuth, elevation],
    )


def axis_pixels(pg: Any, view: str) -> dict[str, int]:
    """How many of the triad's pixels are drawn in each axis's color (as the page styles it), for the axes that
    do not point away from the viewer (those are drawn faint, under the others)."""
    colors = pg.evaluate(
        "view => Object.fromEntries([...document.querySelectorAll(view + ' .triad .axis:not(.away)')].map(g => "
        "[g.classList[1], getComputedStyle(g.querySelector('line')).stroke.match(/[0-9]+/g).slice(0, 3).map(Number)]))",
        view,
    )
    assert colors
    shot = pg.screenshot(clip=pg.locator(f"{view} .triad").bounding_box())
    img = np.asarray(Image.open(io.BytesIO(shot)).convert("RGB")).astype(float)
    return {a: int((np.abs(img - np.array(c)).sum(axis=2) < 60).sum()) for a, c in colors.items()}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_case_views_triad_shows_the_axes_as_the_camera_sees_them(pg: Any, theme: str) -> None:
    pg.click(".rail button[aria-label='Case setup']")
    pg.wait_for_function(
        "trame.state.get('blocks').length > 0 && !!window.mallardZoom.camera('caseview')", timeout=30_000
    )
    if pg.evaluate("trame.state.get('theme')") != theme:
        pg.locator("button[title='Light or dark theme']").click()
        pg.wait_for_function(f"trame.state.get('theme') === '{theme}'")
    sync_api.expect(pg.locator(f"{CASE} .triad")).to_be_visible()
    where = pg.evaluate(CLEAR, CASE)
    assert where["inside"] and where["over"] == 0 and where["size"] == [64, 64], where
    matches(pg, CASE, pg.evaluate(CAMERA, "caseview"))
    pg.locator(".toolbar button:visible", has_text="+X").first.click()  # along +x, y up: z to the right
    pg.wait_for_function(f"(({AXES})('{CASE}').x || [0, 0, 0])[2] < -0.999")
    axes = pg.evaluate(AXES, CASE)
    assert axes["y"] == pytest.approx([0, 1, 0], abs=2e-3) and axes["z"] == pytest.approx([1, 0, 0], abs=2e-3)
    pg.locator(".toolbar button:visible", has_text="Iso").first.click()
    pg.wait_for_function(f"(({AXES})('{CASE}').x || [0, 0, 0])[2] > 0.5")
    before = pg.evaluate(AXES, CASE)
    orbit(pg, "caseview", 40, 15)  # the triad turns with the view, as it moves
    matches(pg, CASE, pg.evaluate(CAMERA, "caseview"))
    assert pg.evaluate(AXES, CASE) != before
    # Legible: each axis drawn in its color, which stands out from the view's background.
    pixels = axis_pixels(pg, CASE)
    assert all(n > 60 for n in pixels.values()), pixels
    background = THEMES[theme]["background"]
    for axis in "xyz":
        rgb = pg.evaluate(
            "([v, a]) => getComputedStyle(document.querySelector(`${v} .triad .axis.${a} line`)).stroke"
            ".match(/[0-9]+/g).slice(0, 3).map(Number)",
            [CASE, axis],
        )
        assert contrast([c / 255 for c in rgb], background) >= 3.0, (axis, rgb)
    # No label or handle is drawn on the triad: a pin whose point lies under it is hidden.
    pg.evaluate(
        """view => { const v = document.querySelector(view);
        const layer = v.querySelector('.case-pins').getBoundingClientRect();
        const t = v.querySelector('.triad').getBoundingClientRect(), cam = window.mallardZoom.camera('caseview');
        const P = window.mallardPins, f = P.frame(cam, layer.width, layer.height);
        const p = P.unproject(f, t.left + t.width / 2 - layer.left, t.top + t.height / 2 - layer.top, cam.focalPoint);
        const el = document.createElement('div'); el.className = 'case-pin test'; el.dataset.p = JSON.stringify(p);
        el.textContent = 'under the triad'; v.querySelector('.case-pins').appendChild(el); }""",
        CASE,
    )
    pg.wait_for_timeout(300)
    assert pg.evaluate("getComputedStyle(document.querySelector('.case-pin.test')).visibility") == "hidden"
    pg.evaluate("document.querySelector('.case-pin.test').remove()")
    pg.keyboard.press("f")
    if theme == "dark":
        pg.locator("button[title='Light or dark theme']").click()


@pytest.mark.parametrize("mode", ["images", "geometry"])
def test_the_results_views_triad_follows_its_camera(pg: Any, mode: str) -> None:
    view = ".results-view"
    pg.click(".rail button[aria-label='Results']")
    pg.locator(RUN_ROWS, has_text="box-1").click()
    pg.wait_for_function("trame.state.get('v_run') === 'box-1' && trame.state.get('v_info').count > 0", timeout=30_000)
    pg.get_by_role("button", name=mode, exact=True).click()
    pg.wait_for_function(f"trame.state.get('v_geometry') === {str(mode == 'geometry').lower()}", timeout=30_000)

    def camera() -> dict[str, Sequence[float]]:
        if mode == "geometry":
            cam: dict[str, Sequence[float]] = pg.evaluate(CAMERA, "geoview")
        else:
            pg.wait_for_timeout(700)  # the last image asked for has arrived
            cam = pg.evaluate("trame.trigger('test_results_camera')")
        return cam

    sync_api.expect(pg.locator(f"{view} .triad")).to_be_visible(timeout=20_000)
    where = pg.evaluate(CLEAR, view)
    assert where["inside"] and where["over"] == 0, where
    pg.locator(".toolbar button:visible", has_text="−Z").first.click()  # along -z, y up: x to the right
    pg.wait_for_function(f"(({AXES})('{view}').z || [0, 0, 0])[2] > 0.999", timeout=10_000)
    axes = pg.evaluate(AXES, view)
    assert axes["x"] == pytest.approx([1, 0, 0], abs=2e-3) and axes["y"] == pytest.approx([0, 1, 0], abs=2e-3)
    if pg.evaluate("trame.state.get('v_lock2d')"):
        pg.locator(".toolbar button:visible", has_text="2D lock").click()
    if mode == "geometry":
        orbit(pg, "geoview", 35, -20)
    else:
        drag(pg, view, 110, -60)  # this process's interactor, which turns on every move
    pg.wait_for_function(f"Math.abs((({AXES})('{view}').z || [0, 0, 1])[2]) < 0.99", timeout=10_000)
    matches(pg, view, camera())
    assert all(n > 60 for n in axis_pixels(pg, view).values())
    pg.get_by_role("button", name="auto", exact=True).click()
