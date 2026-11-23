"""The case view drawn by vtk.js: a handle dragged in the page moves its shape's preview, and the view keeps
rendering afterwards."""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import tomllib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from playwright import sync_api

from mallard_gui.input_doc import recovery_path
from mallard_gui.view.scene import THEMES

from .test_ui import FAKE_NOTIFICATION, app_url, root  # noqa: F401  (fixtures)
from .ui.serve import MARK
from .ui.world import eventually

pytestmark = pytest.mark.ui


def edited(path: Path) -> dict[str, Any]:
    """The input as the GUI is editing it: its unsaved text while there is one, else the file."""
    unsaved = recovery_path(path)
    return tomllib.loads((unsaved if unsaved.exists() else path).read_text())


CAMERA_READY = (
    "(() => { try { return trame.refs.caseview.getCamera().position[2] !== 1; } catch (e) { return false; } })()"
)


OVERLAYS = "[...document.querySelector('.viewport').children].slice(1).forEach(e => { e.style.visibility = '%s'; })"


def view_pixels(pg: Any, clip: dict[str, float] | None = None) -> bytes:
    """What the view itself draws, as a PNG: nothing the page lays over it (pins, the legend, the pointer's
    readout, which changes with every move of the mouse)."""
    pg.evaluate(OVERLAYS % "hidden")
    try:
        pg.wait_for_timeout(300)
        shot: bytes = pg.screenshot(clip=clip or pg.locator(".viewport").first.bounding_box())
    finally:
        pg.evaluate(OVERLAYS % "")
    return shot


def canvas_digest(pg: object) -> str:
    return hashlib.sha1(view_pixels(pg)).hexdigest()


def redrawn(pg: object, old: str, timeout: float = 10.0) -> str:
    """The view's pixels once they differ from `old`: it draws a change a while after the server made it."""
    end = time.monotonic() + timeout
    while (now := canvas_digest(pg)) == old:
        if time.monotonic() > end:
            raise AssertionError("the view did not change")
    return now


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def engine_page(request: pytest.FixtureRequest, app_url: str) -> Iterator[object]:  # noqa: F811
    """Chrome and WebKit, the desktop app's engine (WKWebView)."""
    with sync_api.sync_playwright() as p:
        try:
            if request.param == "webkit":
                browser = p.webkit.launch(headless=True)
            else:
                browser = p.chromium.launch(channel="chrome", headless=True)
        except sync_api.Error:
            pytest.skip(f"{request.param} is not installed")
        pg = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        pg.add_init_script(FAKE_NOTIFICATION)
        pg.goto(app_url)
        pg.wait_for_selector(".tree-row", timeout=30_000)
        yield pg
        browser.close()


def test_a_dragged_cut_plane_moves_whole_and_the_view_keeps_rendering(engine_page: object, root: Path) -> None:  # noqa: F811
    pg = engine_page
    pg.click(".rail button[aria-label='Case setup']")  # type: ignore[attr-defined]
    pg.wait_for_function(CAMERA_READY, timeout=30_000)  # type: ignore[attr-defined]
    pg.locator(".tree-row", has_text=re.compile(r"^\s*wake 1\s*cut_plane")).first.click()  # type: ignore[attr-defined]
    handle = pg.locator(".case-pin.handle").first  # type: ignore[attr-defined]
    handle.wait_for(timeout=10_000)

    def wake() -> dict[str, Any]:
        data = edited(root / "case" / "input.toml")
        entry = next(w for w in data["write_data"] if "wake" in w["prefix"] and "near" not in w["prefix"])
        region: dict[str, Any] = entry["regions"][0]
        return region

    start = wake()
    before = canvas_digest(pg)
    hb = handle.bounding_box()
    x, y = hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2
    pg.mouse.move(x, y)  # type: ignore[attr-defined]
    pg.mouse.down()  # type: ignore[attr-defined]
    for k in range(1, 11):
        pg.mouse.move(x + 8 * k, y - 4 * k)  # type: ignore[attr-defined]
        pg.wait_for_timeout(30)  # type: ignore[attr-defined]
    pg.mouse.up()  # type: ignore[attr-defined]
    for _ in range(50):
        if wake()["point"] != start["point"]:
            break
        pg.wait_for_timeout(100)  # type: ignore[attr-defined]
    region = wake()
    shift = [region["point"][i] - start["point"][i] for i in range(2)]
    assert shift[0] > 0 and shift[1] > 0
    corners = [c + shift[i % 2] for i, c in enumerate(c for corner in start["bounds"] for c in corner)]
    flat = [c for corner in region["bounds"] for c in corner]
    assert flat == pytest.approx(corners, abs=1e-4)  # the piece moved with its handle
    moved = redrawn(pg, before)  # the piece is drawn where its handle went
    box = pg.locator(".viewport").first.bounding_box()  # type: ignore[attr-defined]
    cx, cy = box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.25  # empty space: a pan
    pg.mouse.move(cx, cy)  # type: ignore[attr-defined]
    pg.mouse.down()  # type: ignore[attr-defined]
    pg.mouse.move(cx + 80, cy + 40, steps=8)  # type: ignore[attr-defined]
    pg.mouse.up()  # type: ignore[attr-defined]
    redrawn(pg, moved)  # still rendering after the drag


def drag_handle(pg: Any, text: str, dx: float, dy: float) -> None:
    handle = pg.locator(f".case-pin.handle[title='drag to set {text}']").first
    handle.wait_for(timeout=10_000)
    hb = handle.bounding_box()
    x, y = hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2
    pg.mouse.move(x, y)
    pg.mouse.down()
    for k in range(1, 9):
        pg.mouse.move(x + dx * k / 8, y + dy * k / 8)
        pg.wait_for_timeout(25)
    pg.mouse.up()


def test_handles_drag_again_and_again(engine_page: object, root: Path) -> None:  # noqa: F811
    """Each drag of a region's handles moves it, the same handle many times and after another one; the view
    the check reloads after each keeps the camera, from the page's first framing on."""
    pg: Any = engine_page
    pg.reload()
    pg.wait_for_selector(".tree-row", timeout=30_000)
    select_near_wake(pg, fit=False)

    def near_wake() -> dict[str, Any]:
        data = edited(root / "case" / "input.toml")
        entry = next(w for w in data["write_data"] if "near_wake" in w["prefix"])
        region: dict[str, Any] = entry["region"]
        return region

    def changed(old: dict[str, Any]) -> dict[str, Any]:
        for _ in range(60):
            now = near_wake()
            if now != old:
                return now
            pg.wait_for_timeout(100)
        raise AssertionError(f"the drag left the region at {old}")

    camera = "(() => { const c = trame.refs.caseview.getCamera(); return [c.viewAngle, ...c.position]; })()"
    box = pg.locator(".viewport").first.bounding_box()
    pg.mouse.move(box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.25)
    pg.mouse.wheel(0, -100)  # the page's camera goes to the server
    pg.wait_for_timeout(800)
    region, pixels, framing = near_wake(), canvas_digest(pg), pg.evaluate(camera)
    for text, dx, dy in [
        ("center", 40, 20),
        ("center", -30, 25),
        ("center", 20, -30),
        ("size", 30, 30),
        ("center", 25, 0),
    ]:
        drag_handle(pg, text, dx, dy)
        now = changed(region)
        assert (now["center"] != region["center"]) == (text == "center")
        assert (now.get("size") != region.get("size")) == (text == "size")
        region = now
        pg.wait_for_timeout(1000)  # the check after the release, and the view it reloads
        pixels = redrawn(pg, pixels)
        assert pg.evaluate(camera) == pytest.approx(framing), "the reloaded view moved the camera"


def near_wake_region(project: Path) -> dict[str, Any]:
    data = edited(project / "case" / "input.toml")
    region: dict[str, Any] = next(w for w in data["write_data"] if "near_wake" in w["prefix"])["region"]
    return region


def wait_changed(pg: Any, project: Path, old: dict[str, Any]) -> dict[str, Any]:
    for _ in range(60):
        now = near_wake_region(project)
        if now != old:
            return now
        pg.wait_for_timeout(100)
    raise AssertionError(f"the region stayed at {old}")


def select_near_wake(pg: Any, fit: bool = True) -> None:
    """near_wake selected, back where the fixture has it (earlier tests drag it), and the view fitted."""
    pg.click(".rail button[aria-label='Case setup']")
    pg.wait_for_function(CAMERA_READY, timeout=30_000)
    pg.locator(".tree-row", has_text=re.compile(r"^\s*near_wake\s*rectangle")).first.click()
    for attempt in range(3):  # a check still running from an earlier drag redraws the fields, dropping an edit
        for key, value in (("center", "[3.0, 0.0]"), ("size", "[6.0, 3.0]")):
            field = pg.locator(f"input[aria-label$='region.{key}']").first
            field.fill(value)
            field.press("Enter")
        pg.evaluate("document.activeElement.blur()")  # so F is the view's key, not typed into the field
        try:
            pg.wait_for_function(
                "(() => { const e = document.querySelector(\".case-pin.handle[title='drag to set center']\");"
                " return e && JSON.parse(e.dataset.p)[0] === 3; })()",
                timeout=5_000,
            )
            break
        except sync_api.TimeoutError:
            if attempt == 2:
                raise
    if fit:
        pg.keyboard.press("f")
    pg.wait_for_timeout(500)


def handle_xy(pg: Any, text: str) -> tuple[float, float]:
    hb = pg.locator(f".case-pin.handle[title='drag to set {text}']").first.bounding_box()
    return hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2


def test_dragging_the_selected_regions_body_moves_it(engine_page: object, root: Path) -> None:  # noqa: F811
    pg: Any = engine_page
    select_near_wake(pg)
    (cx, cy), (sx, sy) = handle_xy(pg, "center"), handle_xy(pg, "size")
    x, y = cx - 0.6 * (sx - cx), cy - 0.6 * (sy - cy)  # inside, toward the corner without a handle
    assert pg.evaluate(f"document.elementFromPoint({x}, {y}).classList.contains('case-pin')") is False
    old = near_wake_region(root)
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.mouse.move(x + 30, y + 15, steps=6)
    pg.mouse.up()
    now = wait_changed(pg, root, old)
    assert now["size"] == old["size"]
    assert now["center"][0] > old["center"][0] and now["center"][1] < old["center"][1]  # screen y points down
    nx, ny = handle_xy(pg, "center")
    assert [nx - cx, ny - cy] == pytest.approx([30, 15], abs=3)  # it followed the pointer


def test_a_cancelled_drag_keeps_its_last_position_and_lets_go(engine_page: object, root: Path) -> None:  # noqa: F811
    """A pointercancel (a native gesture taking over) releases the handle where it was, never leaving it held."""
    pg: Any = engine_page
    select_near_wake(pg)
    x, y = handle_xy(pg, "center")
    old = near_wake_region(root)
    pg.evaluate(
        """([x, y]) => {
        const el = document.elementFromPoint(x, y);
        const ev = (type, dx) => el.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true,
            pointerId: 7, button: 0, clientX: x + dx, clientY: y}));
        ev('pointerdown', 0); ev('pointermove', 10); ev('pointermove', 25); ev('pointercancel', 25);
    }""",
        [x, y],
    )
    now = wait_changed(pg, root, old)
    assert now["center"][0] > old["center"][0] and now["size"] == old["size"]
    sync_api.expect(pg.locator(".case-pin.held")).to_have_count(0)
    eventually(lambda: handle_xy(pg, "center")[0], lambda hx: hx == pytest.approx(x + 25, abs=3))


SCENE = """(() => {
  const walk = (inst, depth) => {
    if (!inst || depth > 12) return null;
    if (Object.prototype.hasOwnProperty.call(inst.provides || {}, "view")) return inst.provides.view;
    const stack = [inst.subTree];
    while (stack.length) {
      const v = stack.pop();
      if (!v) continue;
      if (v.component) { const found = walk(v.component, depth + 1); if (found) return found; }
      if (Array.isArray(v.children)) stack.push(...v.children);
      if (v.dynamicChildren) stack.push(...v.dynamicChildren);
    }
    return null;
  };
  const view = walk(trame.refs.caseview._, 0);
  return view.renderer.getActors().filter((a) => a.getVisibility() && a.getMapper() && a.getMapper().getInputData())
    .map((a) => { const b = a.getBounds(); return [a.getMapper().getInputData().getNumberOfPolys(), b[0], b[1]]; });
})()"""


BANNER_AT = """x => Math.abs(parseFloat(
  (document.querySelector('.su-banner')?.innerText.match(/center = \\[([-\\d.]+)/) || [])[1]) - x) < 0.5"""


def drawn_cells(pg: Any, cells: int, x: float, timeout: float = 5.0) -> bool:
    """Whether the browser's scene draws `cells` polygons spanning x, within `timeout` seconds: it draws each
    update of the scene once its geometry has arrived, a while after the server made it."""
    end = time.monotonic() + timeout
    while True:
        if any(n == cells and lo <= x <= hi for n, lo, hi in pg.evaluate(SCENE)):
            return True
        if time.monotonic() > end:
            return False
        pg.wait_for_timeout(100)


def live_cells(pg: Any) -> int:
    text = pg.locator(".su-banner").first.inner_text()
    m = re.search(r"(\d+) cells", text)
    assert m, f"no live cell count in {text!r}"
    return int(m[1])


def test_the_highlighted_cells_follow_a_held_drag(engine_page: object, root: Path) -> None:  # noqa: F811
    """While a handle is held, the region's cells are found again and drawn where it is; nothing is written."""
    pg: Any = engine_page
    select_near_wake(pg)
    (cx, _), (sx, _) = handle_xy(pg, "center"), handle_xy(pg, "size")
    half_w = sx - cx
    old = near_wake_region(root)
    pg.mouse.move(cx, cy := handle_xy(pg, "center")[1])
    pg.mouse.down()
    held = []
    for to in (2, 4, 0):
        pg.mouse.move(cx + to * half_w, cy, steps=6)
        x = old["center"][0] + to * old["size"][0] / 2
        pg.wait_for_function(BANNER_AT, arg=x)
        cells = live_cells(pg)
        assert drawn_cells(pg, cells, x), f"the browser does not draw the {cells} cells at x = {x} while held"
        held.append(cells)
    assert near_wake_region(root) == old  # nothing written while held
    pg.mouse.up()
    assert held[0] != held[1] > 0  # found again as it moved over cells of other sizes


CAMERA = (
    "(() => { const c = trame.refs.caseview.getCamera(); return [...c.position, ...c.focalPoint, c.viewAngle]; })()"
)


class _Steady:
    """True of a value once it is given twice running."""

    def __init__(self) -> None:
        self.last: Any = None

    def __call__(self, value: Any) -> bool:
        same, self.last = value == self.last, value
        time.sleep(0.3)
        return bool(same)


def test_the_view_opens_framed_as_f_frames_it(engine_page: object, app_url: str) -> None:  # noqa: F811
    """Drawn first, the page's view frames itself (vtk.js); it takes the server's framing, which F gives back
    after a pan and a zoom."""
    pg: Any = engine_page
    fresh: Any = pg.context.browser.new_page(device_scale_factor=2, viewport={"width": 1440, "height": 900})
    try:
        fresh.bring_to_front()
        fresh.add_init_script(FAKE_NOTIFICATION)
        fresh.goto(app_url)
        fresh.wait_for_selector(".tree-row", timeout=30_000)
        fresh.click(".rail button[aria-label='Case setup']")
        fresh.wait_for_function(CAMERA_READY, timeout=30_000)
        fresh.wait_for_function("window.mallardZoom.framed()", timeout=10_000)  # it asked for the server's camera
        opened = eventually(lambda: fresh.evaluate(CAMERA), _Steady())
        box = fresh.locator(".viewport").first.bounding_box()
        x, y = box["x"] + box["width"] * 0.7, box["y"] + box["height"] * 0.7
        fresh.mouse.move(x - 20, y - 20)
        fresh.mouse.move(x, y, steps=4)  # vtk.js takes the pointer once it has entered
        fresh.mouse.down()
        fresh.mouse.move(x + 80, y + 40, steps=5)
        fresh.mouse.up()
        fresh.mouse.wheel(0, -300)
        moved = f"(o => {CAMERA}.some((v, i) => Math.abs(v - o[i]) > 1e-3))"
        fresh.wait_for_function(moved, arg=opened, timeout=5_000)
        fresh.wait_for_timeout(400)  # the server takes the moved camera
        fresh.keyboard.press("f")
        fresh.wait_for_function(f"(o => {CAMERA}.every((v, i) => Math.abs(v - o[i]) < 1e-6))", arg=opened)
    finally:
        fresh.close()


def test_the_check_report_links_to_keys_nodes_and_places(engine_page: object) -> None:
    """Every diagnostic's named keys, every row's key or node, and every location the report gives lead to it."""
    pg: Any = engine_page
    pg.click(".rail button[aria-label='Case setup']")
    pg.get_by_role("tab", name="Check report").click()
    diag = pg.locator(".su-diag[data-code='W-DNS-ETA']")
    diag.locator(".chip", has_text="les").click()
    selected = "[...document.querySelectorAll('.tree-row.sel')].some(e => e.textContent.trim().startsWith('LES'))"
    pg.wait_for_function(selected)
    pg.locator(".su-kv", has_text="farfield").filter(has_text="faces").click()
    pg.wait_for_selector(".prop-title >> text=farfield")
    pg.locator(".su-kv", has_text="CFL").first.click()
    pg.wait_for_selector(".prop-title >> text=Run")
    pg.locator(".su-kv", has_text="limiting cell").locator(".locate").click()
    pg.wait_for_selector(".su-banner >> text=limiting cell")
    pg.wait_for_selector(".case-pin.located[title='limiting cell']")
    zoomed = pg.evaluate(CAMERA)
    pg.get_by_role("button", name="Back to report").click()
    pg.wait_for_selector(".su-diag")
    pg.get_by_role("tab", name="Case view").click()
    pg.get_by_role("button", name="Clear").click()
    pg.wait_for_selector(".case-pin.located", state="detached")
    # the server's camera, framed on the point, is shared with later tests
    pg.click("button[aria-label='Fit the whole case to the view']")
    eventually(lambda: pg.evaluate(CAMERA), lambda c: c != zoomed)


def test_a_control_inside_a_clickable_row_acts_without_selecting_the_row(engine_page: object) -> None:
    """`click_stop` without its `__events` declaration rendered no handler: the tree's caret never collapsed a
    section (the row's own click selected it instead), nor did the zone list's eye or a suggested key's fix act."""
    pg: Any = engine_page
    pg.click(".rail button[aria-label='Case setup']")
    caret = pg.locator(".tree-row:has-text('Probes') .chev").first
    caret.click()
    pg.wait_for_selector(".tree-row[aria-expanded='false']:has-text('Probes')")
    pg.wait_for_selector(".tree-row:has-text('wake_point')", state="hidden")
    caret.click()
    pg.wait_for_selector(".tree-row:has-text('wake_point')", state="visible")


def drawn_near(pg: Any, color: Sequence[float], x: float, y: float, reach: int = 8) -> bool:
    """Whether the view draws `color` (0..1 RGB) within `reach` px of (x, y)."""
    shot = view_pixels(pg, {"x": x - reach, "y": y - reach, "width": 2 * reach, "height": 2 * reach})
    rgb = [round(255 * c) for c in color]
    pixels = np.asarray(Image.open(io.BytesIO(shot)).convert("RGB"), dtype=int)
    return bool((np.abs(pixels - np.array(rgb)).sum(axis=2) < 90).any())


def outline_at_handle(pg: Any) -> bool:
    """Whether the selected region's outline, in the accent color, is drawn where its size handle is."""
    accent = THEMES[pg.evaluate("trame.state.get('theme')")]["accent"]
    return drawn_near(pg, accent, *handle_xy(pg, "size"))


def on_screen(pg: Any, point: Sequence[float]) -> tuple[float, float]:
    """Where the view's camera puts a world point, in page coordinates."""
    x, y = pg.evaluate(
        """p => { const b = document.querySelector('.case-pins').getBoundingClientRect();
        const s = mallardPins.project(mallardPins.frame(trame.refs.caseview.getCamera(), b.width, b.height), p);
        return [b.left + s[0], b.top + s[1]]; }""",
        list(point),
    )
    return float(x), float(y)


def test_the_view_draws_on_when_a_new_actor_takes_a_freed_address(engine_page: object, root: Path) -> None:  # noqa: F811
    """A release frees the previews' VTK objects and builds new ones in one scene update. The browser's scene
    knew them by address, so a new actor at a freed property's address (the heap hands out the block last
    freed) reached it as an update of that property: the update threw halfway, what it removed stayed drawn
    and what it added never was, and no later update finished, so the handles left the shapes drawn. With an
    actor rebuilt so, that update is drawn whole, and the view still follows a drag, a pan and a zoom."""
    pg: Any = engine_page
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    select_near_wake(pg)
    actors = SCENE.replace(".filter((a) => a.getVisibility() && a.getMapper() && a.getMapper().getInputData())", "")
    drawn = len(pg.evaluate(actors))
    marks = pg.evaluate("trame.trigger('test_reuse_address')")  # tests/ui/serve.py
    assert marks["reused"], "the heap never gave a new actor a freed property's address"
    eventually(lambda: drawn_near(pg, MARK, *on_screen(pg, marks["new"])))  # the update's new actor is drawn
    eventually(lambda: not drawn_near(pg, MARK, *on_screen(pg, marks["old"])))  # and what it removed is gone
    assert len(pg.evaluate(actors)) <= drawn + 1  # the browser's scene holds the one mark, none of those removed
    old = near_wake_region(root)
    drag_handle(pg, "center", 40, 20)
    wait_changed(pg, root, old)
    eventually(lambda: outline_at_handle(pg))
    box = pg.locator(".viewport").first.bounding_box()
    cx, cy = box["x"] + box["width"] * 0.8, box["y"] + box["height"] * 0.25
    before = canvas_digest(pg)
    pg.mouse.move(cx - 20, cy - 20)
    pg.mouse.move(cx, cy, steps=4)
    pg.mouse.down()
    pg.mouse.move(cx + 80, cy + 40, steps=8)  # empty space: a pan
    pg.mouse.up()
    panned = redrawn(pg, before)
    assert outline_at_handle(pg), "the handle left the outline in a pan"
    pg.mouse.wheel(0, -200)
    redrawn(pg, panned)
    assert outline_at_handle(pg), "the handle left the outline in a zoom"
    assert errors == []


def test_the_page_reports_what_goes_wrong_to_page_log(engine_page: object, root: Path) -> None:  # noqa: F811
    """The desktop app shows no console: an uncaught error, a lost WebGL context and the case view's mode as
    the page sees it are written to page.log beside settings.toml."""
    pg: Any = engine_page
    pg.click(".rail button[aria-label='Case setup']")
    pg.evaluate("setTimeout(() => { throw new Error('an error for page.log'); })")
    pg.evaluate(
        """() => { const c = document.querySelector('.viewport').appendChild(document.createElement('canvas'));
        c.style.display = 'none';
        c.getContext('webgl2').getExtension('WEBGL_lose_context').loseContext(); }"""
    )
    pg.evaluate("trame.trigger('test_images', [true])")  # the server renders, as over the triangle budget
    pg.wait_for_function("trame.state.get('caseview_mode') === 'remote'")
    pg.evaluate("trame.trigger('test_images', [false])")
    pg.wait_for_function("trame.state.get('caseview_mode') === 'local'")

    def events() -> set[tuple[str, str]]:
        lines = [json.loads(ln) for ln in (root / "config" / "page.log").read_text().splitlines()]
        return {(ln["source"], ln["kind"]) for ln in lines if ln["kind"] != "error" or "for page.log" in ln["detail"]}

    expected = {("page", "error"), ("page", "webglcontextlost"), ("page", "caseview_mode"), ("server", "caseview_mode")}
    expected |= {("page", "page"), ("server", "server")}  # the engine and the versions, at start
    eventually(events, lambda seen: expected <= seen)


def test_a_picked_zones_label_stays_at_the_pick_and_a_pan_picks_nothing(engine_page: object) -> None:
    """The label of a zone clicked in the browser-drawn view was placed from a screen position such picks do
    not carry, so it sat at the view's top left, over the toolbar, until another click; and the release of
    every pan was a click, picking whatever was under it. The label is at the picked point, follows it in a
    pan, and goes when the pointer leaves the view; a region picked in the view keeps its handles."""
    pg: Any = engine_page
    select_near_wake(pg)
    tip = pg.locator(".viewport .tip")
    box = pg.locator(".viewport").first.bounding_box()
    x, y = on_screen(pg, [-25.0, 0.0, 0.0])  # the far field's leftmost point
    cx, cy = handle_xy(pg, "center")
    pg.mouse.move(cx - 40, cy - 60)
    pg.mouse.down()
    pg.mouse.move(cx - 30, cy + 10, steps=6)  # a pan released inside the selected region
    pg.mouse.up()
    pg.wait_for_timeout(600)
    sync_api.expect(tip).to_have_count(0)
    sync_api.expect(pg.locator(".case-pin.handle")).to_have_count(2)
    x, y = on_screen(pg, [-25.0, 0.0, 0.0])
    for dx in (0, 1, -1, 2, -2):  # the far field is a line one pixel wide: where it is drawn depends on the view's size
        pg.mouse.click(x + dx, y)
        pg.wait_for_timeout(500)
        if tip.count():
            break
    sync_api.expect(tip).to_contain_text("farfield")

    def offset() -> tuple[float, float]:
        t = tip.bounding_box()
        px, py = on_screen(pg, [-25.0, 0.0, 0.0])
        return t["x"] - px, t["y"] + t["height"] - py

    eventually(offset, lambda d: 0 <= d[0] <= 30 and -30 <= d[1] <= 0)  # right above the picked point
    t = tip.bounding_box()
    assert t["y"] >= box["y"] and t["x"] >= box["x"], "the label is outside the view"
    pg.mouse.move(x + 60, y - 80)
    pg.mouse.down()
    pg.mouse.move(x + 100, y - 50, steps=6)  # a pan: the label goes with its point
    pg.mouse.up()
    eventually(offset, lambda d: 0 <= d[0] <= 30 and -30 <= d[1] <= 0)
    assert abs(on_screen(pg, [-25.0, 0.0, 0.0])[0] - x - 40) < 3
    pg.mouse.move(box["x"] - 30, box["y"] + 200, steps=4)  # out of the view
    sync_api.expect(tip).to_have_count(0)
    pg.mouse.click(*on_screen(pg, [4.5, 0.75, 0.0]))  # the region's own cells: selected as in the tree, handles and all
    sync_api.expect(tip).to_contain_text("near_wake")
    sync_api.expect(pg.locator(".case-pin.handle")).to_have_count(2)
