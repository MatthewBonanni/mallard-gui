"""The document in a real browser: an edit marks the title, Cmd+S saves it, Cmd+Z undoes it, a run with unsaved
edits asks to save first, and closing asks Save / Don't Save / Cancel. Needs Google Chrome (Playwright's
channel)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mallard_gui.input_doc import recovery_path

from .conftest import FIXTURES
from .test_ui import free_port
from .ui.world import eventually

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")
FIELD = "input[aria-label='run.t_stop']"


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("ui-doc")
    project = root / "case"
    project.mkdir()
    shutil.copy(FIXTURES / "cylinder_input.toml", project / "input.toml")
    shutil.copy(FIXTURES / "cylinder.msh", project / "cylinder.msh")
    env = {
        **os.environ,
        "MALLARD_GUI_CONFIG_DIR": str(root / "config"),
        "MALLARD_GUI_CACHE_DIR": str(root / "cache"),
        "MALLARD_GUI_SETTINGS": str(root / "settings.toml"),
    }
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.ui.serve", str(project), str(port)],
        cwd=Path(__file__).parent.parent,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        end = time.monotonic() + 60
        while time.monotonic() < end:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        project.joinpath(".url").write_text(f"http://127.0.0.1:{port}/")
        yield project
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def page(project: Path) -> Iterator[object]:
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except sync_api.Error:
            pytest.skip("Google Chrome is not installed")
        pg = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        pg.goto(project.joinpath(".url").read_text())
        pg.wait_for_selector(".tree-row", timeout=30_000)
        pg.locator(".tree-row", has_text="Run").first.click()
        pg.wait_for_selector(FIELD)
        yield pg
        browser.close()


def set_t_stop(pg: object, value: str) -> None:
    field = pg.locator(FIELD)  # type: ignore[attr-defined]
    field.fill(value)
    field.press("Enter")


def test_an_edit_marks_the_title_and_cmd_s_saves_it(page: object, project: Path) -> None:
    pg = page  # playwright Page
    set_t_stop(pg, "123")
    pg.wait_for_function("document.title.startsWith('• input.toml')")  # type: ignore[attr-defined]
    sync_api.expect(pg.locator(".brand .doc-dot")).to_be_visible()  # type: ignore[attr-defined]
    assert "t_stop = 123" not in (project / "input.toml").read_text()
    recovery = recovery_path(project / "input.toml")
    eventually(lambda: recovery.exists() and "t_stop = 123" in recovery.read_text())
    pg.locator(FIELD).focus()  # type: ignore[attr-defined]
    pg.keyboard.press("ControlOrMeta+s")  # type: ignore[attr-defined]
    pg.wait_for_function("!document.title.startsWith('•')")  # type: ignore[attr-defined]
    assert "t_stop = 123" in (project / "input.toml").read_text()
    eventually(lambda: not recovery.exists())
    sync_api.expect(pg.locator(".brand .doc-dot")).to_have_count(0)  # type: ignore[attr-defined]


def test_cmd_z_outside_a_field_undoes_the_last_edit(page: object) -> None:
    pg = page  # playwright Page
    set_t_stop(pg, "456")
    pg.wait_for_function("document.title.startsWith('•')")  # type: ignore[attr-defined]
    pg.locator(".prop-title").click()  # type: ignore[attr-defined]
    pg.keyboard.press("ControlOrMeta+z")  # type: ignore[attr-defined]
    pg.wait_for_function(f"document.querySelector(\"{FIELD}\").value === '123'")  # type: ignore[attr-defined]
    pg.wait_for_function("!document.title.startsWith('•')")  # type: ignore[attr-defined]


def test_a_run_with_unsaved_edits_asks_to_save_first(page: object) -> None:
    pg = page  # playwright Page
    set_t_stop(pg, "789")
    pg.wait_for_function("document.title.startsWith('•')")  # type: ignore[attr-defined]
    pg.wait_for_selector(".runbtn:not([disabled])", timeout=20_000)  # type: ignore[attr-defined]
    pg.click(".runbtn")  # type: ignore[attr-defined]
    pg.locator(".su-dialog .su-launch-btn").click()  # type: ignore[attr-defined]
    dialog = pg.get_by_role("alertdialog")  # type: ignore[attr-defined]
    dialog.wait_for()
    assert "before running" in dialog.inner_text()
    pg.wait_for_function("document.activeElement.textContent === 'Save and Run'")  # type: ignore[attr-defined]
    pg.keyboard.press("Escape")  # type: ignore[attr-defined]
    dialog.wait_for(state="detached")
    assert pg.evaluate("document.title.startsWith('•')")  # type: ignore[attr-defined]


def test_an_external_edit_reloads_a_clean_document_and_asks_about_a_dirty_one(page: object, project: Path) -> None:
    pg: Any = page
    pg.evaluate("window.mallardMenu('revert')")  # the earlier tests' edits
    pg.wait_for_function("!document.title.startsWith('•')")
    path = project / "input.toml"
    path.write_text(path.read_text().replace("cfl = 1.0", "cfl = 0.9"))  # no edits here: reloaded
    pg.wait_for_selector(".toast >> text=input.toml changed on disk", timeout=10_000)
    pg.locator(".tree-row", has_text="Run").first.click()
    pg.wait_for_function("document.querySelector(\"input[aria-label='run.cfl']\").value === '0.9'")
    assert not pg.evaluate("document.title.startsWith('•')")
    set_t_stop(pg, "321")  # an edit here, then another program's
    pg.wait_for_function("document.title.startsWith('•')")
    path.write_text(path.read_text().replace("cfl = 0.9", "cfl = 0.8"))
    notice = pg.get_by_role("alertdialog", name="The input changed on disk")
    notice.wait_for(timeout=10_000)
    assert "unsaved changes" in notice.inner_text()
    notice.get_by_role("button", name="Compare").click()
    diff = pg.locator(".ext-diff")
    diff.wait_for()
    assert "-cfl = 0.9" in diff.inner_text() and "+cfl = 0.8" in diff.inner_text()
    assert "t_stop = 321" in diff.inner_text()
    pg.get_by_role("dialog").get_by_role("button", name="Keep Mine").click()
    pg.wait_for_selector(".ext-diff", state="detached")
    assert pg.evaluate("document.title.startsWith('•')")  # still unsaved: the file is theirs
    assert pg.locator(FIELD).input_value() == "321"
    path.write_text(path.read_text().replace("cfl = 0.8", "cfl = 0.7"))
    notice.wait_for(timeout=10_000)
    notice.get_by_role("button", name="Reload").click()
    pg.wait_for_function("!document.title.startsWith('•')")
    assert "cfl = 0.7" in path.read_text()
    pg.wait_for_function(f"document.querySelector(\"{FIELD}\").value !== '321'")
    set_t_stop(pg, "789")  # unsaved again, for the next test
    pg.wait_for_function("document.title.startsWith('•')")


def test_close_with_unsaved_edits_asks_and_dont_save_closes(page: object, project: Path) -> None:
    pg = page  # playwright Page
    pg.keyboard.press("Meta+k")  # type: ignore[attr-defined]
    pg.keyboard.type("close project")  # type: ignore[attr-defined]
    pg.wait_for_selector(".search-results div.on >> text=Close Project")  # type: ignore[attr-defined]
    pg.keyboard.press("Enter")  # type: ignore[attr-defined]
    dialog = pg.get_by_role("alertdialog")  # type: ignore[attr-defined]
    dialog.wait_for()
    assert [b.inner_text() for b in dialog.get_by_role("button").all()] == ["Don't Save", "Cancel", "Save"]
    dialog.get_by_role("button", name="Cancel").click()
    dialog.wait_for(state="detached")
    assert recovery_path(project / "input.toml").exists()
    pg.keyboard.press("Meta+k")  # type: ignore[attr-defined]
    pg.keyboard.type("close project")  # type: ignore[attr-defined]
    pg.wait_for_selector(".search-results div.on >> text=Close Project")  # type: ignore[attr-defined]
    pg.keyboard.press("Enter")  # type: ignore[attr-defined]
    pg.get_by_role("alertdialog").get_by_role("button", name="Don't Save").click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".doc-closed >> text=input.toml is closed")  # type: ignore[attr-defined]
    eventually(lambda: not recovery_path(project / "input.toml").exists())
    assert "t_stop = 789" not in (project / "input.toml").read_text()
