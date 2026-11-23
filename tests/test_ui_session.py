"""Mallard's own answers in the page: the case's Files tab with a missing file flagged, and the reason a stopped
run cannot continue, from a build with `case.files` and `file.info`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from mallard_gui.project import Project, RunRecord

from .conftest import RequestsMallard
from .test_ui import open_app, pw  # noqa: F401  (fixtures)
from .ui.world import eventually, make_world, serve

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    project, env = make_world(tmp_path_factory.mktemp("session"))
    run = project / "runs" / "input-2"
    (run / "rst").mkdir(parents=True)
    (run / "rst" / "restart_000100.restart").write_bytes(b"restart")
    (run / "mallard.jsonl").write_text("")
    Project.open(project).save_run(
        RunRecord(
            "input-2",
            home={"remote": "local", "directory": "runs/input-2"},
            outcome="stopped",
            last_restart={"path": "./rst/restart_000100.restart"},
        )
    )
    with serve(project, {**env, "MALLARD_GUI_TEST_REQUESTS": "1"}) as u:
        yield u


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_files_tab_and_continue_reasons(pw: Any, url: str, engine: str) -> None:  # noqa: F811
    browser, pg = open_app(pw, engine, url)
    try:
        pg.get_by_role("tab", name="Files").click()
        missing = pg.locator(".cf-row.missing")
        eventually(lambda: missing.count(), lambda n: n == 1)
        assert "missing.msh" in missing.inner_text() and "missing" in missing.locator(".cf-size").inner_text()
        assert "1 missing" in pg.locator(".case-files .textbar").inner_text()
        pg.click(".rail button[aria-label='Runs']")
        pg.locator(".run-row:visible", has_text="input-2").click()
        notice = pg.locator(".continue-why")
        eventually(lambda: notice.count(), lambda n: n == 1)
        assert RequestsMallard.REFUSAL in notice.inner_text()
        assert pg.get_by_role("button", name="Continue").is_disabled()
    finally:
        pg.click(".rail button[aria-label='Case setup']")  # the server's state is every page's: leave it as found
        browser.close()
