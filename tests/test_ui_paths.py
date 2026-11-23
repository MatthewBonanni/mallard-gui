"""Choosing paths in a real browser: with the desktop shell's bridge (mocked here), every choice goes to its native
dialog and the server's picker never shows; without it, the server's picker shows, labeled as the server
machine's files. Needs Google Chrome (Playwright's channel)."""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .test_ui_document import project

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")
__all__ = ["project"]

# A shell whose choosePath answers with window.__answer and records each call; version 2 had pickSave and no
# choosePath.
SHELL = """
window.__calls = [];
window.__answer = null;
const record = (name) => (arg) => { window.__calls.push([name, arg]); return Promise.resolve(window.__answer); };
Object.defineProperty(window, 'mallardShell', { value: Object.freeze(%s) });
"""
SHELL_V3 = SHELL % "{ version: 3, choosePath: record('choosePath'), setDocument: record('setDocument') }"
SHELL_V2 = SHELL % "{ version: 2, pickSave: record('pickSave'), setDocument: record('setDocument') }"


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except sync_api.Error:
            pytest.skip("Google Chrome is not installed")
        yield b
        b.close()


def open_page(browser: Any, project: Path, shell: str | None) -> Any:
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    if shell:
        pg.add_init_script(shell)
    pg.goto(project.joinpath(".url").read_text())
    pg.wait_for_selector(".tree-row", timeout=30_000)
    return pg


def calls(pg: Any, name: str) -> list[Any]:
    return [arg for n, arg in pg.evaluate("window.__calls") if n == name]


def test_in_the_shell_save_as_uses_the_native_panel(browser: Any, project: Path) -> None:
    pg = open_page(browser, project, SHELL_V3)
    pg.wait_for_function("window.__calls.some(c => c[0] === 'setDocument')")  # told at once, not on a change
    target = project / "native.toml"
    pg.evaluate(f"window.__answer = {str(target)!r}")
    pg.evaluate("window.mallardMenu('save_as')")
    pg.wait_for_function("window.__calls.some(c => c[0] === 'choosePath')")
    (req,) = calls(pg, "choosePath")
    assert (req["mode"], req["name"], req["directory"]) == ("save", "input.toml", str(project))
    assert req["filters"] == [{"name": "Mallard input", "extensions": ["toml"]}]
    pg.wait_for_function("document.title.startsWith('native.toml')")
    assert target.is_file() and pg.locator(".path-picker").count() == 0
    pg.close()


def test_in_the_shell_the_mesh_and_open_use_native_panels_and_cancel_does_nothing(browser: Any, project: Path) -> None:
    pg = open_page(browser, project, SHELL_V3)
    pg.evaluate("window.mallardMenu('open')")
    pg.locator(".tree-row", has_text="Mesh").first.click()
    pg.locator("button[aria-label='Choose a file for mesh.filename']").click()
    pg.wait_for_function("window.__calls.filter(c => c[0] === 'choosePath').length === 2")
    opened, mesh = calls(pg, "choosePath")
    assert opened["mode"] == "open" and opened["filters"][0]["extensions"] == ["toml"]
    assert mesh["mode"] == "open" and {"msh", "cgns", "h5"} <= set(mesh["filters"][0]["extensions"])
    pg.wait_for_timeout(300)
    assert pg.locator(".path-picker").count() == 0
    pg.close()


def test_an_older_shell_without_choose_path_still_gets_its_native_save_panel(browser: Any, project: Path) -> None:
    pg = open_page(browser, project, SHELL_V2)
    pg.evaluate("window.mallardMenu('save_as')")
    pg.wait_for_function("window.__calls.some(c => c[0] === 'pickSave')")
    assert calls(pg, "pickSave")[0]["directory"] == str(project)
    pg.wait_for_timeout(300)
    assert pg.locator(".path-picker").count() == 0
    pg.close()


def test_in_a_browser_the_servers_picker_says_whose_files_it_shows(browser: Any, project: Path) -> None:
    pg = open_page(browser, project, None)
    pg.keyboard.press("ControlOrMeta+Shift+s")
    picker = pg.locator(".path-picker")
    picker.wait_for()
    assert "the machine running mallard-gui" in picker.inner_text()
    pg.keyboard.press("Escape")
    picker.wait_for(state="detached")
    pg.close()


def test_settings_choose_an_executable_natively_in_the_shell_and_from_listed_executables_otherwise(
    browser: Any, project: Path
) -> None:
    exe = project.parent / "bin" / "mallard3d"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (exe.parent / "notes.txt").write_text("not executable")
    settings = project.parent / "settings.toml"
    field = "input[aria-label='mallard 3D']"

    pg = open_page(browser, project, SHELL_V3)
    pg.evaluate(f"window.__answer = {str(exe)!r}")
    pg.click(".rail button[aria-label='Settings']")
    pg.get_by_role("button", name="choose mallard 3D").click()
    pg.wait_for_function("window.__calls.some(c => c[0] === 'choosePath')")
    (req,) = calls(pg, "choosePath")
    assert (req["mode"], req["filters"]) == ("open", [])
    pg.wait_for_function(f'document.querySelector("{field}").value === {str(exe)!r}')
    assert tomllib.loads(settings.read_text())["mallard"]["path_3d"] == str(exe)
    assert pg.locator(".path-picker").count() == 0
    pg.click(".rail button[aria-label='Case setup']")  # the page is the server's: the next one opens on it
    pg.close()

    pg = open_page(browser, project, None)
    pg.click(".rail button[aria-label='Settings']")
    pg.get_by_role("button", name="choose mallard 3D").click()
    picker = pg.locator(".path-picker")
    picker.wait_for()
    sync_api.expect(picker.locator(".doc-entry.file")).to_have_text(["·mallard3d"])
    assert picker.locator("input[aria-label='directory']").input_value() == str(exe.parent.resolve())
    picker.locator(".doc-entry.file").click()
    picker.wait_for(state="detached")
    pg.fill(field, "")
    pg.press(field, "Enter")
    pg.wait_for_function(f"document.querySelector(\"{field}\").value === ''")
    pg.click(".rail button[aria-label='Case setup']")
    pg.close()
