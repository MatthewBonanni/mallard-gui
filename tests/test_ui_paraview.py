"""Open in ParaView on the results view's toolbar: a labeled button, on while a ParaView is found and off with
how to set one up while none is; ParaView is looked for while the page is shown, so an install removed or
replaced under the running GUI (6.0.1 by 6.2.1) is followed without a restart. The tests look and never click:
no ParaView is started."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright import sync_api

from .test_ui import pw  # noqa: F401  (fixtures)
from .test_ui_view_camera import RUN_ROWS
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
BUTTON = ".toolbar button.paraview:visible"


def fake_app(apps: Path, version: str) -> Path:
    app = apps / f"ParaView-{version}.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "paraview").write_text("#!/bin/sh\n")
    return app


@pytest.fixture
def world(pw: Any, tmp_path: Path) -> Iterator[tuple[Any, Path]]:  # noqa: F811
    apps = tmp_path / "Applications"
    fake_app(apps, "6.0.1")
    (tmp_path / "world").mkdir()
    project, env = make_world(tmp_path / "world")
    env = {**env, "MALLARD_GUI_APPLICATIONS": str(apps), "PATH": f"{tmp_path / 'world' / 'bin'}:/usr/bin:/bin"}
    env.pop("MALLARD_GUI_PARAVIEW", None)
    with serve(project, env) as url:
        browser = pw.webkit.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(url)
        page.wait_for_selector(".tree-row", state="attached", timeout=30_000)
        page.click(".rail button[aria-label='Results']")
        page.locator(RUN_ROWS, has_text="box-1").click()
        page.wait_for_function("trame.state.get('v_info').count > 0", timeout=30_000)
        yield page, apps
        browser.close()


def test_open_in_paraview_is_on_the_toolbar_and_follows_the_install(world: tuple[Any, Path]) -> None:
    pg, apps = world
    button = pg.locator(BUTTON)
    sync_api.expect(button).to_have_count(1)
    sync_api.expect(button).to_contain_text("ParaView")
    sync_api.expect(button).to_be_enabled(timeout=10_000)
    title = pg.locator(".toolbar .tb-wrap:has(button.paraview)").get_attribute("title")
    assert "ParaView-6.0.1.app" in title
    bar = pg.locator(".toolbar:visible")
    assert pg.evaluate("e => e.scrollWidth <= e.clientWidth + 1", bar.element_handle())  # the toolbar holds it
    # Still in the panel's Export rows, as before.
    sync_api.expect(pg.locator(".panel.right button:visible", has_text="Open in ParaView")).to_have_count(1)
    # The install goes (ParaView being replaced): the button is off and says how to get one found.
    shutil.rmtree(apps / "ParaView-6.0.1.app")
    sync_api.expect(button).to_be_disabled(timeout=15_000)
    title = pg.locator(".toolbar .tb-wrap:has(button.paraview)").get_attribute("title")
    assert "not found" in title and "MALLARD_GUI_PARAVIEW" in title and "/Applications" in title
    # The new one arrives: on again, for that one, with no restart.
    fake_app(apps, "6.2.1")
    sync_api.expect(button).to_be_enabled(timeout=15_000)
    assert "ParaView-6.2.1.app" in pg.locator(".toolbar .tb-wrap:has(button.paraview)").get_attribute("title")
