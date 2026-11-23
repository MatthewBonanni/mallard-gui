"""Every pane split in a real browser: dragged within its limits, resized from the keyboard, reset by a double-click,
collapsed and brought back, kept in settings.toml across a reload and a restart, sane in a narrow window; and the
case view's canvas drawn at its new size with its camera kept. Needs Google Chrome (Playwright's channel)."""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .test_ui import pw  # noqa: F401  (fixtures)
from .ui.world import eventually, make_world, serve

pytestmark = pytest.mark.ui

# The splitters each page shows in the test world, after `show(page)`.
PAGES = {
    "Case setup": {"case-left", "case-right", "case-problems"},
    "Runs": {"runs-left", "runs-list", "runs-plots", "runs-right"},
    "Data": {"data-left", "data-list", "data-right"},
    "Results": {"results-left", "results-runs", "results-series", "results-plots", "results-right"},
    "Analysis": {"analysis-left", "analysis-right"},
    "Compare runs": {"compare-left", "compare-right"},
    "Remotes": {"remotes-left", "remotes-probe", "remotes-right"},
}
DEFAULTS = {"case-left": 275, "case-right": 390, "case-problems": 140}

SIZE = """name => { const s = document.querySelector(`.splitter[data-split="${name}"]`);
  const p = s.dataset.pane === 'prev' ? s.previousElementSibling : s.parentElement; const r = p.getBoundingClientRect();
  return s.dataset.axis === 'y' ? r.height : r.width; }"""
LIMITS = 'name => window.mallardPanes.limits(document.querySelector(`.splitter[data-split="${name}"]`))'


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    project, env = make_world(tmp_path_factory.mktemp("panes"))
    env["MALLARD_DESKTOP_DATA"] = str(project.parent / "desktop")
    return project, env


@pytest.fixture(scope="module")
def url(world: tuple[Path, dict[str, str]]) -> Iterator[str]:
    with serve(*world) as u:
        yield u


@pytest.fixture
def page(pw: Any, url: str) -> Iterator[Any]:  # noqa: F811
    browser, pg = fresh(pw, url)
    try:
        yield pg
    finally:
        browser.close()


def fresh(pw: Any, url: str, layout: bool = True) -> tuple[Any, Any]:  # noqa: F811
    """The app on the case page at its default pane sizes: trame's state is the server's, shared by every page
    open on it, so a test starts from what the one before it left."""
    browser = pw.chromium.launch(channel="chrome")
    pg = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.goto(url)
    pg.wait_for_selector(".tree-row", state="attached", timeout=30_000)
    pg.click(".rail button[aria-label='Case setup']")
    pg.evaluate("trame.state.set('tab', 'view')")
    if layout:
        pg.evaluate("trame.state.set('pane_layout', {})")
        pg.wait_for_function("!document.querySelector('.body').style.length")
    pg.wait_for_selector(".case-tree .tree-row", timeout=10_000)
    return browser, pg


def settings(world: tuple[Path, dict[str, str]]) -> dict[str, Any]:
    path = Path(world[1]["MALLARD_GUI_CONFIG_DIR"]) / "settings.toml"
    return tomllib.loads(path.read_text()) if path.exists() else {}


def show(pg: Any, title: str) -> None:
    pg.click(f".rail button[aria-label='{title}']")
    first = sorted(PAGES[title])[0]
    pg.wait_for_function(f"document.querySelector('.body').classList.contains('{first.split('-')[0]}')")
    if title == "Runs":
        pg.locator(".run-row:visible", has_text="input-1").first.click()
        pg.wait_for_selector(".runs-plots")
    elif title == "Results":
        pg.locator(".panel:has(> .panel-head > span:text-is('Results')) .run-row", has_text="box-1").click()
        drawn = ".results-view img.frame[src^='data:image/jpeg'], .results-view canvas:visible"
        pg.wait_for_selector(drawn, timeout=30_000)
    elif title == "Remotes":
        pg.locator(".rm-row:visible", has_text="hpc").first.click()


def size(pg: Any, name: str) -> float:
    return float(pg.evaluate(SIZE, name))


def reset(pg: Any, name: str) -> None:
    """Double-click the splitter away from its collapse toggle."""
    pg.locator(f".splitter[data-split='{name}']").dblclick(position={"x": 4, "y": 4})


def drag(pg: Any, name: str, by: float) -> None:
    """Drag the splitter `by` px in the direction that grows its pane."""
    s = pg.locator(f".splitter[data-split='{name}']")
    box = s.bounding_box()
    axis, edge = s.get_attribute("data-axis"), s.get_attribute("data-edge")
    d = by if edge == "end" else -by
    # Away from the middle, where the collapse toggle sits.
    x, y = (
        (box["x"] + 4, box["y"] + box["height"] * 0.25)
        if axis == "x"
        else (box["x"] + box["width"] * 0.25, box["y"] + 4)
    )
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.mouse.move(x + (d if axis == "x" else 0), y + (d if axis == "y" else 0), steps=6)
    pg.mouse.up()
    pg.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


@pytest.mark.parametrize("title", list(PAGES))
def test_each_splitter_drags_its_pane_within_its_limits(page: Any, title: str) -> None:
    pg = page
    show(pg, title)
    shown = set(
        pg.evaluate(
            "[...document.querySelectorAll('.splitter')].filter(s => s.getClientRects().length)"
            ".map(s => s.dataset.split)"
        )
    )
    assert shown == PAGES[title]
    for name in sorted(shown):
        before = size(pg, name)
        lim = pg.evaluate(LIMITS, name)
        step = 40 if before + 40 <= lim["max"] else -40
        drag(pg, name, step)
        assert size(pg, name) == pytest.approx(before + step, abs=1.5), name
        drag(pg, name, 5000)
        assert size(pg, name) == pytest.approx(pg.evaluate(LIMITS, name)["max"], abs=1.5), name
        drag(pg, name, -5000)
        assert size(pg, name) == pytest.approx(lim["min"], abs=1.5), name
        reset(pg, name)


def test_keyboard_resizes_and_collapses(page: Any) -> None:
    pg = page
    s = pg.locator(".splitter[data-split='case-left']")
    s.focus()
    for _ in range(3):
        pg.keyboard.press("ArrowRight")
    assert size(pg, "case-left") == pytest.approx(305, abs=1)
    pg.keyboard.press("Shift+ArrowLeft")
    assert size(pg, "case-left") == pytest.approx(255, abs=1)
    assert s.get_attribute("aria-valuenow") == "255"
    pg.keyboard.press("Home")
    assert size(pg, "case-left") == pytest.approx(180, abs=1)
    pg.keyboard.press("End")
    assert size(pg, "case-left") == pytest.approx(640, abs=1)
    pg.keyboard.press("ArrowRight")  # at its maximum
    assert size(pg, "case-left") == pytest.approx(640, abs=1)
    pg.keyboard.press("Home")
    # The right pane grows to the left; the problems panel up.
    pg.locator(".splitter[data-split='case-right']").focus()
    pg.keyboard.press("ArrowLeft")
    assert size(pg, "case-right") == pytest.approx(400, abs=1)
    pg.locator(".splitter[data-split='case-problems']").focus()
    pg.keyboard.press("ArrowUp")
    assert size(pg, "case-problems") == pytest.approx(150, abs=1)
    # Enter hides the pane down to its strip and brings it back at its size.
    pg.keyboard.press("Enter")
    pg.wait_for_function(f"({SIZE})('case-problems') === 12")
    assert not pg.locator(".problems .panel-head").is_visible()
    pg.keyboard.press("Enter")
    pg.wait_for_function(f"Math.round(({SIZE})('case-problems')) === 150")


def test_the_toggle_and_the_strip_collapse_and_restore(page: Any) -> None:
    pg = page
    s = pg.locator(".splitter[data-split='case-left']")
    s.hover()
    s.locator(".splitter-toggle").click()
    pg.wait_for_function(f"({SIZE})('case-left') === 12")
    assert not pg.locator(".case-tree").is_visible()
    assert s.locator(".splitter-toggle").get_attribute("aria-label") == "Show the case tree"
    s.click()  # anywhere on the strip
    pg.wait_for_function(f"({SIZE})('case-left') === 275")
    assert pg.locator(".case-tree").is_visible()


def test_double_click_resets_to_the_default(page: Any) -> None:
    pg = page
    for name, by in (("case-left", 90), ("case-right", -40), ("case-problems", 60)):
        drag(pg, name, by)
        assert size(pg, name) == pytest.approx(DEFAULTS[name] + by, abs=1.5)
        reset(pg, name)
        pg.wait_for_function(f"Math.round(({SIZE})('{name}')) === {DEFAULTS[name]}")
    assert pg.evaluate("trame.state.get('pane_layout')") == {}


def test_sizes_persist_across_a_reload_and_a_restart(page: Any, world: tuple[Path, dict[str, str]], pw: Any) -> None:  # noqa: F811
    pg = page
    drag(pg, "case-left", 60)
    drag(pg, "case-right", 50)
    pg.locator(".splitter[data-split='case-problems']").focus()
    pg.keyboard.press("Enter")
    stored = {"case-left": 335, "case-right": 440, "case-problems-collapsed": True}
    eventually(lambda: settings(world).get("layout"), lambda v: v == stored)
    pg.reload()
    pg.wait_for_selector(".tree-row", timeout=30_000)
    assert (size(pg, "case-left"), size(pg, "case-right"), size(pg, "case-problems")) == (335, 440, 12)
    with serve(*world) as again:  # a new server reads them from settings.toml
        browser, pg2 = fresh(pw, again, layout=False)
        try:
            assert pg2.evaluate("trame.state.get('pane_layout')") == stored
            assert (size(pg2, "case-left"), size(pg2, "case-right"), size(pg2, "case-problems")) == (335, 440, 12)
        finally:
            browser.close()


CANVAS = """() => { const c = document.querySelector('.viewport canvas'); const r = c.getBoundingClientRect();
  return {w: c.width, h: c.height, cw: r.width, ch: r.height, dpr: devicePixelRatio,
          cam: trame.refs.caseview.getCamera()}; }"""


def test_the_case_view_redraws_at_its_new_size_with_its_camera(page: Any) -> None:
    pg = page
    pg.wait_for_selector(".viewport canvas", state="visible", timeout=30_000)
    pg.wait_for_function(
        "(() => { try { return !!trame.refs.caseview.getCamera().position; } catch (e) { return false; } })()",
        timeout=30_000,
    )
    pg.wait_for_function("window.mallardZoom.framed()", timeout=10_000)  # the view has the server's framing
    pg.wait_for_timeout(500)
    before = pg.evaluate(CANVAS)
    drag(pg, "case-left", 200)
    drag(pg, "case-problems", 150)
    pg.wait_for_function(
        f"(() => {{ const v = ({CANVAS})(); return v.w === Math.round(v.cw * v.dpr) && v.h === Math.round(v.ch * v.dpr)"
        f" && v.cw < {before['cw'] - 150}; }})()",
        timeout=20_000,
    )
    after = pg.evaluate(CANVAS)
    assert after["ch"] == pytest.approx(before["ch"] - 150, abs=2)
    for key in ("position", "focalPoint", "viewUp"):
        assert after["cam"][key] == pytest.approx(before["cam"][key])


def test_a_narrow_window_keeps_every_pane_usable(page: Any) -> None:
    pg = page
    drag(pg, "case-left", 5000)
    drag(pg, "case-right", 5000)
    pg.set_viewport_size({"width": 1100, "height": 700})
    pg.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    center = pg.evaluate("document.querySelector('.body > div > .center').getBoundingClientRect().width")
    assert center >= 359
    assert size(pg, "case-left") >= 180 and size(pg, "case-right") >= 220
    assert pg.evaluate("document.documentElement.scrollWidth") <= 1100
    # Dragged in the narrow window, a pane still leaves the center its minimum.
    reset(pg, "case-left")
    reset(pg, "case-right")
    drag(pg, "case-right", 5000)
    assert pg.evaluate("document.querySelector('.body > div > .center').getBoundingClientRect().width") >= 359
