"""The case view's orientation buttons: there and clickable for a 3D case (in a narrow window too), each turning
the view to the side it names; there while no case view is loaded yet; there for a 2D case too, where ±Z face the
plane and keep the 2D lock and the others look from out of the plane and lift it; the lock following the case
from 2D to 3D and back."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from .test_ui import pw  # noqa: F401  (fixtures)
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui

NAMES = ("+X", "−X", "+Y", "−Y", "+Z", "−Z", "Iso")
VIEW = ".viewport:not(.results-view)"
LOOK = """(() => { const c = window.mallardZoom && window.mallardZoom.camera && window.mallardZoom.camera('caseview')
    || trame.refs.caseview.getCamera();
  const d = c.focalPoint.map((f, i) => f - c.position[i]), n = Math.hypot(...d); return d.map(v => v / n); })()"""
# Watches each frame until the view faces -Z, noting the 2D lock the page had then.
TURNED = (
    "(() => { window.__turned = null; const look = () => "
    + LOOK
    + """;
  const frame = () => { if (look().every((v, i) => Math.abs(v - [0, 0, -1][i]) < 1e-6)) {
    window.__turned = {lock2d: trame.state.get('lock2d')}; } else requestAnimationFrame(frame); };
  requestAnimationFrame(frame); })()"""
)
# Each button: drawn, and the element under its middle (nothing lies over it).
BUTTONS = """() => [...document.querySelectorAll('.toolbar')].filter(t => t.offsetParent).flatMap(t =>
  [...t.querySelectorAll('button[title^="View "]')].map(b => { const r = b.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return {name: b.innerText.trim(), shown: r.width > 0 && getComputedStyle(b).visibility === 'visible',
            on_top: top === b || b.contains(top), enabled: !b.disabled}; }))"""


def opened(pw: Any, url: str, width: int = 1440, height: int = 900) -> tuple[Any, Any]:  # noqa: F811
    browser = pw.webkit.launch(headless=True)
    pg = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=2)
    pg.goto(url)
    pg.wait_for_selector(".tree-row", state="attached", timeout=30_000)
    pg.click(".rail button[aria-label='Case setup']")
    return browser, pg


@pytest.fixture(scope="module")
def box_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """The app on a 3D case: the captured check and case view of a small box."""
    project, env = make_world(tmp_path_factory.mktemp("box"), runs=False)
    with serve(project, env, check="box_check.json") as url:
        yield url


@pytest.mark.parametrize("width", [1440, 1100])
def test_a_3d_case_has_its_orientation_buttons_and_they_turn_the_view(pw: Any, box_url: str, width: int) -> None:  # noqa: F811
    browser, pg = opened(pw, box_url, width)
    try:
        pg.wait_for_selector(f"{VIEW} canvas", state="visible", timeout=30_000)
        pg.wait_for_function("trame.state.get('blocks').length > 0", timeout=30_000)
        buttons = pg.evaluate(BUTTONS)
        assert [b["name"] for b in buttons] == list(NAMES)
        assert all(b["shown"] and b["on_top"] and b["enabled"] for b in buttons), buttons
        box = pg.locator(VIEW).first.bounding_box()
        before = pg.screenshot(clip=box)
        for name, look in (("+X", [1, 0, 0]), ("−Y", [0, -1, 0]), ("+Z", [0, 0, 1])):
            pg.locator(".toolbar button:visible", has_text=name).first.click()
            pg.wait_for_function(f"{LOOK}.every((v, i) => Math.abs(v - {look}[i]) < 1e-6)", timeout=10_000)
            pg.wait_for_timeout(400)
            now = pg.screenshot(clip=box)
            assert now != before, f"{name} left the picture as it was"
            before = now
    finally:
        browser.close()


def test_the_orientation_buttons_are_there_before_any_case_view(pw: Any, tmp_path: Any) -> None:  # noqa: F811
    """Until a view had loaded the case counted as 2D, whose buttons are hidden: a case whose check gives no
    view (a 3D case under a 2D executable, a mesh not found) had none, for good."""
    project, env = make_world(tmp_path, runs=False)
    with serve(project, {**env, "MALLARD_GUI_TEST_NO_VIEW": "1"}) as url:
        browser, pg = opened(pw, url)
        try:
            pg.wait_for_selector(".viewport .empty", timeout=30_000)  # no case view
            pg.wait_for_function("!trame.state.get('check').running", timeout=30_000)
            buttons = pg.evaluate(BUTTONS)
            assert [b["name"] for b in buttons] == list(NAMES) and all(b["shown"] and b["on_top"] for b in buttons)
        finally:
            browser.close()


def test_the_buttons_follow_the_case_from_2d_to_3d(pw: Any, tmp_path: Any) -> None:  # noqa: F811
    """The view's dimension was taken from its first load alone: a case that became 3D kept a 2D view's toolbar."""
    project, env = make_world(tmp_path, runs=False)
    with serve(project, env) as url:
        browser, pg = opened(pw, url)
        try:
            pg.wait_for_function("trame.state.get('blocks').length > 0 && trame.state.get('dim') === 2", timeout=30_000)
            assert pg.evaluate("trame.state.get('lock2d')") is True
            pg.evaluate("trame.trigger('test_use_check', ['box_check.json'])")
            pg.wait_for_function("trame.state.get('dim') === 3", timeout=20_000)
            assert pg.evaluate("trame.state.get('lock2d')") is False
            pg.evaluate("trame.trigger('test_use_check', ['cylinder_check.json'])")
            pg.wait_for_function("trame.state.get('dim') === 2 && trame.state.get('lock2d') === true", timeout=20_000)
        finally:
            browser.close()


def test_a_2d_case_has_all_its_orientation_buttons(pw: Any, tmp_path: Any) -> None:  # noqa: F811
    """The buttons were hidden for a 2D case. ±Z face the plane from either side and keep the 2D lock; ±X, ±Y and
    Iso look from out of the plane, which the lock forbids, so they lift it; locked again, the plane faces the
    view."""
    project, env = make_world(tmp_path, runs=False)
    with serve(project, env) as url:
        browser, pg = opened(pw, url)
        try:
            pg.wait_for_selector(f"{VIEW} canvas", state="visible", timeout=30_000)
            pg.wait_for_function("trame.state.get('blocks').length > 0 && trame.state.get('dim') === 2", timeout=30_000)
            buttons = pg.evaluate(BUTTONS)
            assert [b["name"] for b in buttons] == list(NAMES)
            assert all(b["shown"] and b["on_top"] and b["enabled"] for b in buttons), buttons
            for name, look, locked in (
                ("+Z", [0, 0, 1], True),
                ("−Z", [0, 0, -1], True),
                ("+X", [1, 0, 0], False),
                ("−Y", [0, -1, 0], False),
            ):
                if not locked:
                    assert pg.evaluate("trame.state.get('lock2d')") is True
                pg.locator(".toolbar button:visible", has_text=name).first.click()
                pg.wait_for_function(f"{LOOK}.every((v, i) => Math.abs(v - {look}[i]) < 1e-6)", timeout=10_000)
                assert pg.evaluate("trame.state.get('lock2d')") is locked, name
                if not locked:
                    # Every frame drawn facing the plane again is drawn with the lock already on.
                    pg.evaluate(TURNED)
                    pg.locator(".toolbar button:visible", has_text="2D lock").click()
                    pg.wait_for_function("window.__turned", timeout=10_000)
                    assert pg.evaluate("window.__turned") == {"lock2d": True}
        finally:
            browser.close()
