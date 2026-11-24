"""Pages with nothing to show yet say what belongs there and how to get it, centered, instead of empty forms,
tables and tabs; their one action goes where the next step is."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from .test_ui import pw  # noqa: F401  (fixture)
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")

# Page, the empty state's title, and controls of a selected run that must not be shown without one.
PAGES = {
    "Runs": ("No runs yet", [".run-head", ".panel.right .tabs"]),
    "Results": ("No results yet", [".toolbar", "select[aria-label='field']", ".panel.right .actions"]),
    "Data": ("No runs yet", [".toolbar", ".ftable", ".panel.right .actions"]),
    "Analysis": ("Nothing to analyze yet", [".body.analysis .tabs", ".panel.right .scroll"]),
    "Compare runs": ("Compare needs two runs", [".cmp-bar", ".cmp-table"]),
}


@pytest.fixture(scope="module")
def page(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    with serve(*make_world(tmp_path_factory.mktemp("empty"), runs=False)) as url:
        browser = pw.chromium.launch(channel="chrome")
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(url)
        pg.wait_for_selector(".tree-row", timeout=30_000)
        yield pg
        browser.close()


@pytest.mark.parametrize("title", list(PAGES))
def test_an_empty_page_says_what_belongs_there(page: Any, title: str) -> None:
    pg = page
    pg.click(f".rail button[aria-label='{title}']")
    heading, hidden = PAGES[title]
    state = pg.locator(".center .empty-state:visible")
    sync_api.expect(state).to_have_count(1)
    sync_api.expect(state.locator(".es-title")).to_have_text(heading)
    box, center = state.bounding_box(), pg.locator(".center:visible").first.bounding_box()
    assert abs((box["y"] + box["height"] / 2) - (center["y"] + center["height"] / 2)) < 40  # centered
    for selector in hidden:
        assert not pg.locator(f"{selector}:visible").count(), selector
    assert "not built yet" not in pg.evaluate("document.body.innerText")


def test_the_empty_runs_page_goes_to_case_setup(page: Any) -> None:
    pg = page
    pg.click(".rail button[aria-label='Runs']")
    pg.locator(".center .empty-state:visible").get_by_role("button", name="Go to Case setup").click()
    pg.wait_for_selector(".case-tree .tree-row")
    assert pg.locator(".rail button.on").get_attribute("aria-label") == "Case setup"


def test_the_empty_results_page_names_its_own_exports(page: Any) -> None:
    pg = page
    pg.click(".rail button[aria-label='Results']")
    text = pg.locator(".center .empty-state:visible").inner_text()
    assert "GIF or MP4" in text
    assert "ParaView's job" not in text
