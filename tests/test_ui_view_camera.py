"""The camera commands of the results view and the case view, in Chrome and WebKit (the desktop app's engine),
where the viewer renders images and where the browser draws: with 2D lock a drag pans and never turns the view,
the orientation buttons show the side they name, and Fit frames the scene from where the view looks."""

from __future__ import annotations

import io
import math
from collections.abc import Iterator, Sequence
from typing import Any

import numpy as np
import pytest
from PIL import Image
from playwright import sync_api

from mallard_gui.project import Project, RunRecord

from .runs import make_run
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui

MODES = ("images", "geometry")
RUN_ROWS = ".panel:has(> .panel-head > span:text-is('Results')) .run-row"
SHOWN_OVER = ".legend, .colorbar, .readout, .view-notice, .empty, .triad, .optbar"
OVERLAYS = f"document.querySelectorAll('.results-view :is({SHOWN_OVER})')"
CAMERA_OF = """(() => { let c = null; try { c = trame.refs.%s.getCamera(); } catch (e) { return null; }
    return c && c.position && {position: c.position, focal_point: c.focalPoint, view_up: c.viewUp}; })()"""
BROWSER_CAMERA = CAMERA_OF % "geoview"


@pytest.fixture(scope="module")
def world_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The browser tests' world with a 2D run beside the 3D box."""
    project, env = make_world(tmp_path_factory.mktemp("camera"))
    make_run(project / "runs" / "plane-1", steps=2, dim=2)
    Project.open(project).save_run(
        RunRecord("plane-1", home={"remote": "local", "directory": "runs/plane-1"}, outcome="finished")
    )
    with serve(project, env) as url:
        yield url


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def pg(request: pytest.FixtureRequest, world_url: str) -> Iterator[Any]:
    with sync_api.sync_playwright() as p:
        try:
            if request.param == "webkit":
                browser = p.webkit.launch(headless=True)
            else:
                browser = p.chromium.launch(channel="chrome", headless=True)
        except sync_api.Error:
            pytest.skip(f"{request.param} is not installed")
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(world_url)
        page.wait_for_selector(".tree-row", state="attached", timeout=30_000)
        yield page
        browser.close()


def open_run(pg: Any, run: str, mode: str) -> None:
    """The run in the results view, drawn in `mode`, framed as it opens."""
    pg.click(".rail button[aria-label='Results']")
    pg.locator(RUN_ROWS, has_text=run).click()
    pg.wait_for_function(f"trame.state.get('v_run') === '{run}' && trame.state.get('v_info').count > 0", timeout=30_000)
    pg.get_by_role("button", name=mode, exact=True).click()
    settled(pg, mode)
    if pg.evaluate("trame.state.get('v_lock2d')") != (run == "plane-1"):
        pg.locator(".toolbar button:visible", has_text="2D lock").click()
    if pg.evaluate("trame.state.get('v_dim')") == 3:
        pg.locator(".toolbar button:visible", has_text="Iso").click()
    else:
        pg.locator(".toolbar button[title^='Fit']:visible").click()
    settled(pg, mode)


def settled(pg: Any, mode: str) -> None:
    """The view drawn in `mode` and no longer changing."""
    if mode == "geometry":
        pg.wait_for_selector(".results-view canvas", state="visible", timeout=30_000)
        pg.wait_for_function(f"!!{BROWSER_CAMERA}", timeout=30_000)
    else:
        pg.wait_for_selector(".results-view img.frame[src^='data:image']", timeout=30_000)
    last: np.ndarray[Any, Any] | None = None
    for _ in range(40):
        pg.wait_for_timeout(250)
        png = pg.screenshot(clip=pg.locator(".results-view").first.bounding_box())
        now = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.int16)
        if last is not None and now.shape == last.shape and still(last, now):
            return
        last = now
    raise AssertionError("the view keeps changing")


def still(a: np.ndarray[Any, Any], b: np.ndarray[Any, Any]) -> bool:
    """Two pictures of a view that is not changing. Under load Chrome rasters a native slider's rounded end again
    a few levels off (two pixels by 3 of 255, between frames of one view): that is not the view changing."""
    d = np.abs(a - b).max(axis=2)
    return int((d > 0).sum()) <= 16 and int(d.max(initial=0)) <= 8


def camera(pg: Any, mode: str) -> dict[str, list[float]]:
    """Where the view looks from: the browser's own camera where it draws, else the viewer's."""
    if mode == "geometry":
        cam: dict[str, list[float]] = pg.evaluate(BROWSER_CAMERA)
    else:
        cam = pg.evaluate("trame.trigger('test_results_camera')")
    return cam


def direction(cam: dict[str, list[float]]) -> list[float]:
    d = [f - p for f, p in zip(cam["focal_point"], cam["position"], strict=True)]
    n = math.hypot(*d)
    return [c / n for c in d]


def up(cam: dict[str, list[float]]) -> list[float]:
    """The view's up, made square to its direction (a camera may hold any vector of that half plane)."""
    d, u = direction(cam), cam["view_up"]
    k = sum(a * b for a, b in zip(d, u, strict=True))
    v = [a - k * b for a, b in zip(u, d, strict=True)]
    n = math.hypot(*v)
    return [c / n for c in v]


def same(a: Sequence[float], b: Sequence[float]) -> bool:
    return sum(x * y for x, y in zip(a, b, strict=True)) > 1 - 1e-6


def picture(pg: Any) -> np.ndarray[Any, Any]:
    """The view's own pixels (H, W, 3), with nothing the page lays over it."""
    pg.evaluate(f"{OVERLAYS}.forEach(e => {{ e.dataset.was = e.style.visibility; e.style.visibility = 'hidden'; }})")
    try:
        pg.wait_for_timeout(150)
        shot = pg.screenshot(clip=pg.locator(".results-view").first.bounding_box())
    finally:
        pg.evaluate(f"{OVERLAYS}.forEach(e => {{ e.style.visibility = e.dataset.was; }})")
    return np.asarray(Image.open(io.BytesIO(shot)).convert("RGB")).astype(float)


def drawn(img: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """Where the scene is drawn: the pixels that are not the view's background (its corner's color)."""
    mask: np.ndarray[Any, Any] = np.abs(img - img[2, 2]).sum(axis=2) > 40
    return mask


def box_of(mask: np.ndarray[Any, Any]) -> tuple[float, float, float, float]:
    """The drawn scene's bounding box in CSS px: left, top, right, bottom."""
    ys, xs = np.nonzero(mask)
    return xs.min() / 2, ys.min() / 2, (xs.max() + 1) / 2, (ys.max() + 1) / 2


def drag(pg: Any, dx: float, dy: float, at: tuple[float, float] = (0.5, 0.5)) -> None:
    box = pg.locator(".results-view").first.bounding_box()
    x, y = box["x"] + at[0] * box["width"], box["y"] + at[1] * box["height"]
    pg.mouse.move(x - 5, y - 5)
    pg.mouse.move(x, y, steps=2)  # vtk.js takes the pointer once it has entered
    pg.mouse.down()
    pg.mouse.move(x + dx, y + dy, steps=6)
    pg.mouse.up()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("run", ["plane-1", "box-1"])
def test_with_2d_lock_a_drag_pans_and_never_rotates(pg: Any, run: str, mode: str) -> None:
    """The browser-drawn view orbited under any drag, the lock on or off: a 2D case turned edge on."""
    open_run(pg, run, mode)
    if run == "box-1":
        pg.locator(".toolbar button:visible", has_text="2D lock").click()
    pg.wait_for_function("trame.state.get('v_lock2d') === true")
    box = pg.locator(".results-view").first.bounding_box()
    pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    pg.mouse.wheel(0, 300)  # room around the picture for it to move in
    pg.wait_for_timeout(600)
    settled(pg, mode)
    before, was = camera(pg, mode), picture(pg)
    drag(pg, 60, 24)
    settled(pg, mode)
    after, now = camera(pg, mode), picture(pg)
    assert same(direction(after), direction(before)) and same(up(after), up(before)), "the drag turned the view"
    a, b = box_of(drawn(was)), box_of(drawn(now))
    shift = [q - p for p, q in zip(a, b, strict=True)]
    # The picture moved with the pointer, whole: every side of it by the drag (a perspective view's nearer
    # parts a little more).
    assert all(abs(s - 60) < 6 for s in shift[0::2]) and all(abs(s - 24) < 4 for s in shift[1::2]), shift
    assert drawn(now).sum() == pytest.approx(drawn(was).sum(), rel=0.03)


def yellow(img: np.ndarray[Any, Any], mask: np.ndarray[Any, Any]) -> tuple[float, float, float]:
    """How yellow (cividis: high) rather than blue (low) the drawn scene is: over its left third, its right
    third and all of it."""
    left, _, right, _ = (int(2 * v) for v in box_of(mask))
    third = (right - left) // 3
    y = img[..., 0] + img[..., 1] - 2 * img[..., 2]

    def mean(lo: int, hi: int) -> float:
        return float(y[:, lo:hi][mask[:, lo:hi]].mean())

    return mean(left, left + third), mean(right - third, right), mean(left, right)


@pytest.mark.parametrize("mode", MODES)
def test_the_orientation_buttons_show_the_side_they_name(pg: Any, mode: str) -> None:
    """The box's P rises with x (dark blue to yellow): each button shows the face it names, P rising the way
    that view sees x."""
    open_run(pg, "box-1", mode)
    drag(pg, 70, -40)  # from an orbit of the user's, not the opening view
    settled(pg, mode)
    seen = {}
    for name, look, top in (
        ("+X", [1, 0, 0], [0, 1, 0]),
        ("-X", [-1, 0, 0], [0, 1, 0]),
        ("+Y", [0, 1, 0], [0, 0, 1]),
        ("-Y", [0, -1, 0], [0, 0, 1]),
        ("+Z", [0, 0, 1], [0, 1, 0]),
        ("-Z", [0, 0, -1], [0, 1, 0]),
    ):
        pg.locator(".toolbar button:visible", has_text=name.replace("-", "−")).click()
        settled(pg, mode)
        cam = camera(pg, mode)
        assert same(direction(cam), look) and same(up(cam), top), f"{name}: looks along {direction(cam)}"
        img = picture(pg)
        mask = drawn(img)
        left, top_px, right, bottom = box_of(mask)
        assert (right - left) == pytest.approx(bottom - top_px, rel=0.03), f"{name}: a face of the cube is square"
        seen[name] = yellow(img, mask)
    assert seen["+X"][2] < -60 and abs(seen["+X"][0] - seen["+X"][1]) < 8  # the x = 0 face: low, all of it
    assert seen["-X"][2] > 60 and abs(seen["-X"][0] - seen["-X"][1]) < 8  # the x = 1 face: high
    assert seen["+Z"][0] - seen["+Z"][1] > 60  # along +z with y up, x rises to the left
    assert seen["-Z"][1] - seen["-Z"][0] > 60
    assert seen["+Y"][1] - seen["+Y"][0] > 60  # along +y with z up, x rises to the right
    assert seen["-Y"][0] - seen["-Y"][1] > 60
    pg.locator(".toolbar button:visible", has_text="Iso").click()
    settled(pg, mode)
    assert same(direction(camera(pg, mode)), [-(3**-0.5)] * 3)


@pytest.mark.parametrize("mode", MODES)
def test_a_2d_run_has_all_its_orientation_buttons(pg: Any, mode: str) -> None:
    """The buttons were hidden for a 2D run. ±Z face the plane and keep the 2D lock; +X looks at it edge on, out
    of the plane, and lifts the lock."""
    open_run(pg, "plane-1", mode)
    sync_api.expect(pg.locator(".toolbar button[title^='View ']:visible")).to_have_count(7)
    for name, look, locked in (("+Z", [0, 0, 1], True), ("-Z", [0, 0, -1], True), ("+X", [1, 0, 0], False)):
        pg.locator(".toolbar button:visible", has_text=name.replace("-", "−")).click()
        settled(pg, mode)
        assert same(direction(camera(pg, mode)), look), f"{name}: looks along {direction(camera(pg, mode))}"
        assert pg.evaluate("trame.state.get('v_lock2d')") is locked, name
    pg.locator(".toolbar button:visible", has_text="2D lock").click()
    settled(pg, mode)
    assert same(direction(camera(pg, mode)), [0, 0, -1])


@pytest.mark.parametrize("mode", MODES)
def test_fit_frames_the_results_from_where_the_view_looks(pg: Any, mode: str) -> None:
    """Fit turned the view back to the opening one. It keeps the orbit, and only moves and zooms."""
    open_run(pg, "box-1", mode)
    drag(pg, 130, 70)  # an orbit
    pg.keyboard.down("Shift")
    drag(pg, -160, 90)  # a pan
    pg.keyboard.up("Shift")
    pg.mouse.wheel(0, 240)
    settled(pg, mode)
    orbit = camera(pg, mode)
    assert not same(direction(orbit), [-(3**-0.5)] * 3)
    pg.locator(".toolbar button[title^='Fit']:visible").click()
    pg.wait_for_timeout(500)
    settled(pg, mode)
    fitted = camera(pg, mode)
    assert same(direction(fitted), direction(orbit)) and same(up(fitted), up(orbit)), "Fit turned the view"
    view = pg.locator(".results-view").first.bounding_box()
    left, top, right, bottom = box_of(drawn(picture(pg)))
    # Framed: inside the view, centered, and filling most of one of its sides.
    assert left > 0 and top > 0 and right < view["width"] and bottom < view["height"]
    assert (left + right) / 2 == pytest.approx(view["width"] / 2, abs=0.02 * view["width"])
    assert (top + bottom) / 2 == pytest.approx(view["height"] / 2, abs=0.02 * view["height"])
    assert max((right - left) / view["width"], (bottom - top) / view["height"]) > 0.8


CASE_CAMERA = CAMERA_OF % "caseview"


@pytest.mark.parametrize("how", ["button", "key"])
def test_fit_frames_the_case_from_where_the_view_looks(pg: Any, how: str) -> None:
    """The case view's Fit (and F) turned an orbited case back to the opening view."""
    pg.click(".rail button[aria-label='Case setup']")
    pg.wait_for_selector(".viewport:not(.results-view) canvas", state="visible", timeout=30_000)
    pg.wait_for_function(f"!!{CASE_CAMERA}", timeout=30_000)
    lock = pg.locator(".toolbar button:visible", has_text="2D lock")
    if pg.evaluate("trame.state.get('lock2d')"):
        lock.click()  # the cylinder is 2D: unlocked, a drag orbits it
    pg.wait_for_function("trame.state.get('lock2d') === false")
    pg.wait_for_timeout(300)  # the view has its orbit controls
    box = pg.locator(".viewport").first.bounding_box()
    x, y = box["x"] + 0.5 * box["width"], box["y"] + 0.5 * box["height"]
    pg.mouse.move(x - 5, y - 5)
    pg.mouse.move(x, y, steps=2)
    pg.mouse.down()
    pg.mouse.move(x + 110, y + 60, steps=6)
    pg.mouse.up()
    pg.mouse.wheel(0, 300)
    orbit = pg.evaluate(CASE_CAMERA)
    assert not same(direction(orbit), [0, 0, -1])
    if how == "key":
        pg.keyboard.press("f")  # at once: before the server has heard of the drag
    else:
        pg.locator(".toolbar button[title^='Fit']:visible").click()
    moved = "(o => { const c = %s; return c.position.some((v, i) => Math.abs(v - o.position[i]) > 1e-6); })"
    pg.wait_for_function(moved % CASE_CAMERA, arg=orbit, timeout=10_000)
    fitted = pg.evaluate(CASE_CAMERA)
    assert same(direction(fitted), direction(orbit)) and same(up(fitted), up(orbit)), "Fit turned the view"
    lock.click()  # locked again, a 2D case is back in its plane
    pg.wait_for_function(f"(() => {{ const c = {CASE_CAMERA}; return Math.abs(c.position[2] - c.focal_point[2]) > 0 && "
                         "Math.abs(c.position[0] - c.focal_point[0]) < 1e-9; })()", timeout=10_000)  # fmt: skip
    assert same(direction(pg.evaluate(CASE_CAMERA)), [0, 0, -1])


def test_a_reloaded_page_pans_the_image_by_the_drag(pg: Any) -> None:
    """Frames named the server's count of moves, which outlives a page: after a reload every frame seemed to
    show moves the new page had not made yet, so the image kept its preview's offset over a frame that already
    held the pan, and moved twice as far as the pointer."""
    open_run(pg, "plane-1", "images")
    pg.keyboard.down("Shift")
    for _ in range(3):
        drag(pg, 20, 10)
    pg.keyboard.up("Shift")
    settled(pg, "images")
    pg.reload()
    pg.wait_for_selector(".tree-row", state="attached", timeout=30_000)
    open_run(pg, "plane-1", "images")
    box = pg.locator(".results-view").first.bounding_box()
    pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    pg.mouse.wheel(0, 300)  # room around the picture for it to move in
    pg.wait_for_timeout(600)
    settled(pg, "images")
    was = box_of(drawn(picture(pg)))
    pg.keyboard.down("Shift")
    drag(pg, 60, 24)
    pg.keyboard.up("Shift")
    settled(pg, "images")
    assert pg.evaluate("document.querySelector('.results-view img.frame').style.transform") == ""
    shift = [q - p for p, q in zip(was, box_of(drawn(picture(pg))), strict=True)]
    assert all(abs(s - 60) < 3 for s in shift[0::2]) and all(abs(s - 24) < 3 for s in shift[1::2]), shift
