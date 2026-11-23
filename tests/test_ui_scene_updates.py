"""The case view over a slow link: a region dragged with its cells highlighted live sends a scene update every
few tens of milliseconds, each of whose arrays arrives later than the next update is sent. The browser's scene
is one of those updates whole at every frame, never the points of one with the cells of another."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from playwright import sync_api

from .test_ui_case_view import handle_xy, select_near_wake
from .ui.world import eventually, make_world, serve

pytestmark = pytest.mark.ui

# After each painted frame, every dataset the view draws is noted by its counts of points, polygons and lines.
WATCH = """
window.__scene = {frames: 0, drawn: {}};
(() => {
  const ch = new MessageChannel();
  const view = () => {
    try { return trame.refs.caseview.$refs.localViewRef.$.provides.view; } catch (e) { return null; }
  };
  ch.port1.onmessage = () => {
    const v = view();
    if (!v || !v.renderer) return;
    const w = window.__scene;
    w.frames++;
    for (const a of v.renderer.getActors()) {
      const data = a.getVisibility() && a.getMapper() && a.getMapper().getInputData();
      if (!data || !data.getPoints || !data.getPolys) continue;
      const key = [data.getPoints().getNumberOfPoints(), data.getNumberOfPolys(), data.getNumberOfLines()].join(",");
      w.drawn[key] = (w.drawn[key] || 0) + 1;
    }
  };
  const tick = () => { requestAnimationFrame(tick); ch.port2.postMessage(0); };
  requestAnimationFrame(tick);
})();
"""


# Each request for an array of the scene leaves 50 to 450 ms late, each on its own: a slow link with jitter, over
# which an update's geometry is still on its way when the next update is sent, and arrives out of order.
SLOW = """
window.__slow = 0;
(() => {
  const send = WebSocket.prototype.send;
  const needle = [...'viewport.geometry.array.get'].map((c) => c.charCodeAt(0));
  const asks = (data) => {
    if (typeof data === 'string') return data.includes('viewport.geometry.array.get');
    const u = data instanceof ArrayBuffer ? new Uint8Array(data)
      : ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : null;
    if (!u || u.length > 4096) return false;
    for (let i = 0; i + needle.length <= u.length; i++) if (needle.every((c, j) => u[i + j] === c)) return true;
    return false;
  };
  let seed = 7;
  const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  WebSocket.prototype.send = function (data) {
    if (!asks(data)) return send.call(this, data);
    window.__slow++;
    setTimeout(() => send.call(this, data), 50 + 400 * rand());
  };
})();
"""
DRAWN = """(() => { const v = trame.refs.caseview.$refs.localViewRef.$.provides.view;
  return v.renderer.getActors().map((a) => a.getVisibility() && a.getMapper() && a.getMapper().getInputData())
    .filter((d) => d && d.getPoints && d.getPolys && d.getPoints().getNumberOfPoints())
    .map((d) => [d.getPoints().getNumberOfPoints(), d.getNumberOfPolys(), d.getNumberOfLines()])
    .sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]); })()"""


@pytest.fixture(scope="module")
def slow_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with serve(*make_world(tmp_path_factory.mktemp("slow"), runs=False)) as url:
        yield url


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def pg(request: pytest.FixtureRequest, slow_url: str) -> Iterator[Any]:
    with sync_api.sync_playwright() as p:
        try:
            if request.param == "webkit":
                browser = p.webkit.launch(headless=True)
            else:
                browser = p.chromium.launch(channel="chrome", headless=True)
        except sync_api.Error:
            pytest.skip(f"{request.param} is not installed")
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.add_init_script(WATCH)
        page.add_init_script(SLOW)
        page.goto(slow_url)
        page.wait_for_selector(".tree-row", state="attached", timeout=60_000)
        yield page
        browser.close()


def test_a_region_dragged_over_a_slow_link_is_never_drawn_from_two_updates(pg: Any) -> None:
    select_near_wake(pg)
    (cx, cy), (sx, _) = handle_xy(pg, "center"), handle_xy(pg, "size")
    half = sx - cx
    pg.mouse.move(cx, cy)
    pg.mouse.down()
    for sweep in range(4):  # back and forth over cells of many sizes, as a hand does
        for k in range(1, 25):
            to = (k if sweep % 2 == 0 else 24 - k) / 6
            pg.mouse.move(cx + to * half, cy + 6 * (k % 3))
            pg.wait_for_timeout(35)
    pg.mouse.move(cx, cy)
    pg.mouse.up()
    pg.mouse.move(cx + 300, cy + 200)
    # The scene catches up with the server: the region drawn back where the drag ended.
    back = "(() => { const e = document.querySelector('.case-pin.handle[title=\\'drag to set center\\']'); "
    pg.wait_for_function(back + "return e && Math.abs(JSON.parse(e.dataset.p)[0] - 3) < 0.2; })()", timeout=20_000)
    pg.wait_for_timeout(1500)
    seen = pg.evaluate("window.__scene")
    sent = {",".join(str(c) for c in counts) for counts in pg.evaluate("trame.trigger('test_datasets_sent')")}
    assert seen["frames"] > 100 and pg.evaluate("window.__slow") > 20  # the arrays did come late
    mixed = {k: n for k, n in seen["drawn"].items() if k not in sent and k != "0,0,0"}
    assert not mixed, f"points of one update drawn with the cells of another (points,polygons,lines: frames): {mixed}"
    # And once everything has arrived, the browser draws the scene the server has now, not an earlier update
    # whose arrays came last.
    # (The last scene may still be on its way: sent once the one before is drawn, its arrays late.)
    now = [list(c) for c in pg.evaluate("trame.trigger('test_datasets_now')")]
    eventually(lambda: pg.evaluate(DRAWN), lambda drawn: drawn == now, timeout=15.0)
