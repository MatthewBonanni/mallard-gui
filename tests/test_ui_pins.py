"""The case view's pins (labels, handles, the picked zone's label), in Chrome and WebKit at pixel ratios 1 and 2:
a pin sits on the pixels the view draws for its point, whatever the projection, the zoom, the window's size or
who draws (the browser, or the server's images), and no pin is ever painted before
it is placed."""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from PIL import Image
from playwright import sync_api

from .test_ui import app_url, root  # noqa: F401  (fixtures)
from .ui.serve import MARK

pytestmark = pytest.mark.ui

POINT = [4.5, 2.0, 3.0]  # off the plane: the ball whole from any side the test looks from
CONFIGS = [("chromium", 2, 1440, 900), ("webkit", 2, 1440, 900), ("webkit", 1, 1180, 760), ("chromium", 1, 1000, 640)]
# Painted pins, sampled after each frame is drawn: those not placed yet, and those over the toolbar.
WATCH = """
window.__pins = {frames: 0, unplaced: 0, over: 0, painted: 0};
(() => {
  const ch = new MessageChannel();
  ch.port1.onmessage = () => {
    const w = window.__pins;
    w.frames++;
    const bars = [...document.querySelectorAll('.toolbar, .tabs')].filter(e => e.offsetParent)
      .map(e => e.getBoundingClientRect());
    for (const el of document.querySelectorAll('.case-pin')) {
      if (!el.offsetParent || getComputedStyle(el).visibility !== 'visible') continue;
      w.painted++;
      const layer = el.parentElement.getBoundingClientRect(), r = el.getBoundingClientRect();
      const m = /translate\\(([-\\d.]+)px, ([-\\d.]+)px\\)/.exec(el.style.transform || '');
      if (!m || +m[1] < 0 || +m[2] < 0 || +m[1] > layer.width || +m[2] > layer.height) w.unplaced++;
      if (bars.some(b => r.left < b.right && r.right > b.left && r.top < b.bottom - 1 && r.bottom > b.top)) w.over++;
    }
  };
  const tick = () => { requestAnimationFrame(tick); ch.port2.postMessage(0); };
  requestAnimationFrame(tick);
})();
"""
VIEW = ".viewport:not(.results-view)"
# A pin of the test's own at a world point: pins.js places every pin in the layer.
PIN = """p => { const el = document.createElement('div'); el.className = 'case-pin test';
    el.dataset.p = JSON.stringify(p); document.querySelector('.case-pins').appendChild(el); }"""
SCENE = "(() => { try { return trame.refs.caseview.getCamera().position[2] !== 1; } catch (e) { return false; } })()"


@pytest.fixture(scope="module", params=CONFIGS, ids=lambda c: f"{c[0]}-x{c[1]}-{c[2]}")
def pg(request: pytest.FixtureRequest, app_url: str) -> Iterator[Any]:  # noqa: F811
    engine, ratio, width, height = request.param
    with sync_api.sync_playwright() as p:
        try:
            if engine == "webkit":
                browser = p.webkit.launch(headless=True)
            else:
                browser = p.chromium.launch(channel="chrome", headless=True)
        except sync_api.Error:
            pytest.skip(f"{engine} is not installed")
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=ratio)
        page.add_init_script(WATCH)
        page.goto(app_url)
        page.wait_for_selector(".tree-row", state="attached", timeout=30_000)
        page.click(".rail button[aria-label='Case setup']")
        page.wait_for_selector(f"{VIEW} canvas", state="visible", timeout=30_000)
        yield page
        browser.close()


def set_view(pg: Any, **want: bool) -> None:
    """The case view's toggles (lock2d, parallel, clip) as wanted."""
    names = {"lock2d": "2D lock", "parallel": "Ortho", "clip": "Clip"}
    for key, on in want.items():
        if bool(pg.evaluate(f"trame.state.get('{key}')")) != on:
            pg.locator(".toolbar button:visible", has_text=names[key]).click()
            pg.wait_for_function(f"!!trame.state.get('{key}') === {str(on).lower()}")


def pin_and_mark(pg: Any) -> tuple[list[float], list[float]]:
    """Where the label's pin is, and where the view draws the ball at its point: page coordinates, CSS px."""
    pg.wait_for_timeout(700)  # the view has drawn the last move, here or as the server's image
    pin = pg.evaluate(
        """() => { const el = document.querySelector('.case-pin.test');
        if (!el || getComputedStyle(el).visibility !== 'visible') return null;
        const m = /translate\\(([-\\d.]+)px, ([-\\d.]+)px\\)/.exec(el.style.transform);
        const b = el.parentElement.getBoundingClientRect(); return [b.left + +m[1], b.top + +m[2]]; }"""
    )
    assert pin is not None, "the label is not shown"
    box = pg.locator(VIEW).first.bounding_box()
    # The view's own pixels: the pins and whatever the page lays over the view (the legend can cover the ball).
    over = f"document.querySelectorAll('{VIEW} :is(.case-pins, .legend, .su-speed, .triad)')"
    for _ in range(40):  # the mark is added on the server: the browser has it a round trip and a frame later
        pg.evaluate(f"{over}.forEach(e => {{ e.style.opacity = 0; }})")
        try:
            shot = pg.screenshot(clip=box)
        finally:
            pg.evaluate(f"{over}.forEach(e => {{ e.style.opacity = ''; }})")
        img = np.asarray(Image.open(io.BytesIO(shot)).convert("RGB"), dtype=int)
        ratio = img.shape[1] / box["width"]
        ys, xs = np.nonzero(np.abs(img - np.array([round(255 * c) for c in MARK])).sum(axis=2) < 60)
        if len(xs) > 12 * ratio**2:
            break
        pg.wait_for_timeout(250)
    assert len(xs) > 12 * ratio**2, "the view does not draw the ball"
    return pin, [box["x"] + (xs.mean() + 0.5) / ratio, box["y"] + (ys.mean() + 0.5) / ratio]


def test_a_pin_sits_on_the_pixels_drawn_for_its_point(pg: Any) -> None:
    set_view(pg, lock2d=True, parallel=False, clip=False)
    pg.keyboard.press("f")
    pg.evaluate("([p, r]) => trame.trigger('test_mark', [p, r])", [POINT, 1.2])
    pg.evaluate(PIN, POINT)
    box = pg.locator(VIEW).first.bounding_box()
    x, y = box["x"] + 0.4 * box["width"], box["y"] + 0.6 * box["height"]

    def drag(dx: float, dy: float) -> None:
        pg.mouse.move(x - 4, y - 4)
        pg.mouse.move(x, y, steps=2)
        pg.mouse.down()
        pg.mouse.move(x + dx, y + dy, steps=5)
        pg.mouse.up()

    def check(what: str) -> None:
        pin, ball = pin_and_mark(pg)
        assert pin == pytest.approx(ball, abs=1.0), f"{what}: the pin is at {pin}, its point is drawn at {ball}"

    check("as opened")
    pg.mouse.move(x, y)
    pg.mouse.wheel(0, -500)
    check("zoomed in")
    drag(-70, 45)
    check("panned")
    set_view(pg, parallel=True)
    check("in a parallel projection")
    pg.mouse.wheel(0, 200)
    check("in a parallel projection, zoomed out")
    set_view(pg, parallel=False, lock2d=False)
    drag(60, -35)
    check("orbited")
    size = pg.viewport_size
    pg.set_viewport_size({"width": size["width"] - 230, "height": size["height"] - 90})
    check("in a smaller window")
    pg.set_viewport_size(size)
    pg.evaluate("trame.trigger('test_images', [true])")  # the server draws, as over the triangle budget
    pg.wait_for_function("trame.state.get('caseview_mode') === 'remote'")
    check("over the server's image")
    pg.evaluate("trame.trigger('test_images', [false])")
    pg.wait_for_function("trame.state.get('caseview_mode') === 'local'")
    set_view(pg, lock2d=True)
    pg.evaluate("document.querySelector('.case-pin.test').remove()")
    pg.keyboard.press("f")


def test_no_pin_is_painted_before_it_is_placed(pg: Any) -> None:
    """A pin not placed yet (the page just loaded, the view not built, its layer hidden or empty, the server
    drawing) sat at the layer's top left, under the toolbar, until a frame placed it."""
    pg.locator(".tree-row", has_text="near_wake").first.click()
    sync_api.expect(pg.locator(".case-pin.handle")).to_have_count(2)
    pg.evaluate("p => trame.state.set('tip', {p, text: 'mark', sub: 'label'})", POINT)
    pg.reload()  # the server still has the selection: the pins are in the page's first state
    pg.wait_for_selector(".tree-row", state="attached", timeout=30_000)
    pg.wait_for_selector(f"{VIEW} canvas", state="visible", timeout=30_000)
    pg.wait_for_function(SCENE, timeout=30_000)
    sync_api.expect(pg.locator(".case-pin.handle")).to_have_count(2)
    pg.wait_for_timeout(1000)
    for tab in ("input.toml", "Case view"):  # the view hidden, then shown
        pg.locator(".tabs .tab", has_text=tab).first.click()
        pg.wait_for_timeout(500)
    pg.click(".rail button[aria-label='Results']")
    pg.wait_for_timeout(300)
    pg.click(".rail button[aria-label='Case setup']")
    pg.wait_for_timeout(500)
    pg.evaluate("trame.trigger('test_images', [true])")
    pg.wait_for_function("trame.state.get('caseview_mode') === 'remote'")
    pg.wait_for_timeout(800)
    pg.evaluate("trame.trigger('test_images', [false])")
    pg.wait_for_function("trame.state.get('caseview_mode') === 'local'")
    pg.wait_for_timeout(800)
    seen = pg.evaluate("window.__pins")
    pg.evaluate("trame.state.set('tip', null)")
    assert seen["painted"] > 100, seen  # the pins were there to see
    assert seen["unplaced"] == 0 and seen["over"] == 0, seen
