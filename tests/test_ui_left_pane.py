"""The left of the window: the case tree's notes in one column, empty lists that say so, the rail's pages by
name and number, and the case view's toolbar (Fit and Frame told apart, the Inflow arrows switch)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .test_ui import open_app, pw  # noqa: F401  (fixtures)
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
RAIL = ["Case setup", "Runs", "Data", "Results", "Analysis", "Compare runs", "Remotes"]
NOTES = """[...document.querySelectorAll('.case-tree .tree-row')].filter(r => r.offsetParent).map(r => ({
  label: r.querySelector('.tlabel').textContent, note: r.querySelector('.tval').textContent.trim(),
  right: r.querySelector('.tval').getBoundingClientRect().right, add: !!r.querySelector('.tadd'),
  badge: !!r.querySelector('.badge')}))"""


@pytest.fixture(scope="module")
def pg(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    root: Path = tmp_path_factory.mktemp("left")
    with serve(*make_world(root, runs=False)) as url:
        browser, page = open_app(pw, "chromium", url)
        page.click(".rail button[aria-label='Case setup']")
        page.wait_for_function("trame.state.get('blocks').length > 0", timeout=30_000)
        yield page
        browser.close()


def test_every_note_of_the_case_tree_ends_at_one_edge(pg: Any) -> None:
    pg.evaluate(
        "trame.state.set('tree', trame.state.get('tree').map(n => n.label === 'Mesh' ? {...n, warnings: 3} : n))"
    )
    pg.wait_for_selector(".tree-row .badge.w")
    rows = pg.evaluate(NOTES)
    assert any(r["add"] for r in rows) and any(r["badge"] for r in rows)  # rows that end with a + or a badge
    assert {round(r["right"]) for r in rows} == {round(rows[0]["right"])}, rows


def test_an_empty_list_says_none_and_a_switched_off_feature_says_off(pg: Any) -> None:
    notes = {r["label"]: r["note"] for r in pg.evaluate(NOTES)}
    assert notes["Sponges"] == notes["Triggers"] == "none"
    assert notes["Chemistry"] == "off"
    assert notes["Parallel"] == "not set"  # a table, not a list


def test_features_are_rows_of_the_tree_with_no_strip_of_chips_above_it(pg: Any) -> None:
    assert pg.locator(".panel .feature-strip, .fchip-btn").count() == 0
    assert pg.locator(".case-tree .tree-row", has_text="Chemistry").count() == 1


def test_the_rail_names_each_page_and_its_number_which_goes_there(pg: Any) -> None:
    titles = pg.locator(".rail button").evaluate_all("bs => bs.map(b => [b.ariaLabel, b.title])")
    assert titles[:-1] == [[name, f"{name}  ⌘{i}"] for i, name in enumerate(RAIL, 1)]
    assert titles[-1] == ["Settings", "Settings  ⌘,"]
    try:
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press("Control+2")
        pg.wait_for_function("trame.state.get('page') === 'runs'")
        pg.keyboard.press("Control+5")
        pg.wait_for_function("trame.state.get('page') === 'analysis'")
    finally:
        pg.click(".rail button[aria-label='Case setup']")


def test_fit_and_frame_look_different_and_say_their_keys(pg: Any) -> None:
    fit = pg.locator(".toolbar button[aria-label='Fit the whole case to the view']")
    frame = pg.locator(".toolbar button[aria-label='Frame the selected zone']")
    assert fit.inner_html() != frame.inner_html()
    assert fit.get_attribute("title").endswith("  F") and frame.get_attribute("title").endswith("  ⇧F")


def test_inflow_arrows_are_one_switch_on_the_toolbar(pg: Any) -> None:
    button = pg.locator(".toolbar button", has_text="Inflow arrows")
    assert button.get_attribute("aria-pressed") == "false"
    button.click()
    pg.wait_for_selector(".toolbar button[aria-pressed='true']", timeout=10_000)
    assert pg.locator(".viewport .su-speed").count() == 0  # one inflow, one speed: no scale
    button.click()
    pg.wait_for_selector(".toolbar button[aria-pressed='false']", timeout=10_000)
