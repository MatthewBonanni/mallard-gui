"""The Run page's run details show when the run started in the local time zone: the run log's time is UTC."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from .test_ui import pw  # noqa: F401  (fixture)
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def page(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("run-info"))
    with serve(project, {**env, "TZ": "America/New_York"}) as url:
        browser = pw.chromium.launch(channel="chrome")
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(url)
        pg.wait_for_selector(".tree-row", timeout=30_000)
        yield pg
        browser.close()


def test_a_runs_start_is_shown_in_local_time(page: Any) -> None:
    pg = page
    pg.click(".rail button[aria-label='Runs']")
    pg.locator(".panel:has(> .panel-head > span:text-is('Runs')) .run-row", has_text="input-1").click()
    started = pg.locator(".prow:visible", has=pg.locator("text=started")).first
    # The run log's start_time is 2026-11-20T18:52:56Z.
    assert started.inner_text().split("\n")[-1].strip() == "2026-11-20 13:52 EST"
