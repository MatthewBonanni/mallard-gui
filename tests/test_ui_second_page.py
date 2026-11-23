"""A page opened after another one used the app (a reload, a second window, the desktop app's window on a server
a browser also shows) draws the case view and the results view: trame's state is shared by every page, and a
view mounted on a later page reads it as it is then."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .test_ui import pw  # noqa: F401  (fixture)
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")
Image = pytest.importorskip("PIL.Image")

RESULTS_ROWS = ".panel:has(> .panel-head > span:text-is('Results')) .run-row"


@pytest.fixture
def url(tmp_path: Path) -> Iterator[str]:
    with serve(*make_world(tmp_path)) as u:
        yield u


def open_page(pw: Any, url: str) -> tuple[Any, Any, list[str]]:  # noqa: F811
    browser = pw.chromium.launch(channel="chrome")
    pg = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))

    def on_console(m: Any) -> None:
        if m.type == "error" and "Error" in m.text:  # vtk.js's mount errors are logged, not thrown
            errors.append(m.text)

    pg.on("console", on_console)
    pg.goto(url)
    pg.wait_for_selector(".tree-row", state="attached", timeout=30_000)
    return browser, pg, errors


def colors(pg: Any, selector: str) -> int:
    """Distinct colors drawn in the element: a blank view is one or two."""
    png = pg.locator(selector).first.screenshot()
    img = Image.open(io.BytesIO(png)).convert("RGB").resize((160, 120))
    return len(set(img.get_flattened_data()))


def test_a_later_page_draws_the_views_after_another_page_watched_a_run(pw: Any, url: str) -> None:  # noqa: F811
    first, pg, _ = open_page(pw, url)
    pg.click(".rail button[aria-label='Runs']")
    pg.locator(".run-row:visible", has_text="input-1").first.click()
    pg.wait_for_selector(".events-row")  # the run's events are in the shared state now
    first.close()

    browser, pg, errors = open_page(pw, url)
    try:
        pg.click(".rail button[aria-label='Case setup']")
        pg.evaluate("trame.state.set('tab', 'view')")
        pg.wait_for_selector(".viewport canvas", state="visible", timeout=30_000)
        pg.wait_for_timeout(1500)
        assert colors(pg, ".viewport") > 20

        pg.click(".rail button[aria-label='Results']")
        pg.locator(RESULTS_ROWS, has_text="box-1").click()
        pg.get_by_role("button", name="geometry", exact=True).click()
        pg.wait_for_selector(".results-view canvas", state="visible", timeout=30_000)
        pg.wait_for_timeout(1500)
        assert colors(pg, ".results-view") > 20
        assert errors == []
    finally:
        browser.close()
