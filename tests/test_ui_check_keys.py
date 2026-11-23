"""The problems panel on a report of a build with structured diagnostics (Mallard PR #1377): every error of an input
reported at once, setup warnings among them, and a diagnostic's line and column opening the text at that spot."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from playwright import sync_api

from .conftest import FIXTURES
from .test_ui import open_app, pw  # noqa: F401  (fixtures)
from .ui.world import eventually, make_world, serve

pytestmark = pytest.mark.ui
INPUT = FIXTURES / "check_keys" / "writers.toml"


@pytest.fixture(scope="module")
def url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    project, env = make_world(tmp_path_factory.mktemp("check-keys"), runs=False)
    shutil.copy(INPUT, project / "input.toml")
    with serve(project, env, check="check_keys/writers.json") as u:
        yield u


def offset(text: str, line: int, column: int) -> int:
    lines = text.split("\n")
    return sum(len(x) + 1 for x in lines[: line - 1]) + column - 1


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_every_error_at_once_and_its_line_in_the_text(pw: Any, url: str, engine: str) -> None:  # noqa: F811
    browser, pg = open_app(pw, engine, url)
    try:
        rows = pg.locator(".problems .prob")
        eventually(lambda: rows.count(), lambda n: n == 6)
        keys = [rows.nth(i).locator(".key").inner_text() for i in range(3)]
        assert keys == ["write_data[0].time_interval", "write_data[1].time_interval", "write_data"]
        assert rows.nth(2).locator(".code").inner_text() == "W-FORMAT-VTU"
        line = rows.nth(1).locator("button.line")
        assert line.inner_text() == "line 76:17"
        line.click()
        field = pg.locator('textarea[aria-label="input.toml"]')
        caret = eventually(lambda: field.evaluate("t => document.activeElement === t && t.selectionStart"))
        assert caret == offset(INPUT.read_text(encoding="utf-8"), 76, 17)
    finally:
        pg.evaluate("() => window.trame.state.set('tab', 'view')")  # the server's state is every page's
        browser.close()


DOCS = "https://github.com/MatthewBonanni/mallard/blob/main/docs/guide/input_files.md#e-key-unused"
RHO = (
    "error E-KEY-UNUSED\nkey: initialize.rho\nat: input.toml line 21:7\n"
    "line 21: initialize.rho (did you mean output.reference.rho?)\n"
    f"fix: rename initialize.rho to output.reference.rho\ndocs: {DOCS}"
)


@pytest.fixture
def clip(pw: Any, url: str) -> Iterator[Any]:  # noqa: F811
    """Chrome with the clipboard granted, so a test reads what the page copied."""
    try:
        browser = pw.chromium.launch(channel="chrome")
    except sync_api.Error:
        pytest.skip("chrome is not installed")
    context = browser.new_context(
        viewport={"width": 1440, "height": 900}, permissions=["clipboard-read", "clipboard-write"]
    )
    pg = context.new_page()
    pg.goto(url)
    pg.wait_for_selector(".tree-row", timeout=30_000)
    eventually(lambda: pg.locator(".problems .prob").count(), lambda n: n == 6)
    yield pg
    pg.evaluate("() => window.trame.state.set('tab', 'view')")
    browser.close()


def clipboard(pg: Any) -> str:
    return str(pg.evaluate("() => navigator.clipboard.readText()"))


def reset_clipboard(pg: Any) -> None:
    pg.evaluate("() => navigator.clipboard.writeText('')")


def test_a_problem_copies_its_full_text_by_button_menu_and_keys(clip: Any) -> None:
    pg = clip
    rows = pg.locator(".problems .prob")
    rho = rows.nth(3)
    rho.hover()
    button = rho.locator(".copy-btn")
    assert button.is_visible()
    button.click()
    eventually(lambda: clipboard(pg), lambda t: t == RHO)
    reset_clipboard(pg)
    rows.nth(4).click(button="right")  # the context menu
    pg.locator(".copy-menu button", has_text="Copy").click()
    eventually(lambda: clipboard(pg), lambda t: t.startswith("error E-KEY-UNUSED\nkey: initialize.u\n"))
    reset_clipboard(pg)
    rows.nth(3).click()  # select, then Cmd+C
    pg.keyboard.press("ControlOrMeta+c")
    eventually(lambda: clipboard(pg), lambda t: t == RHO)
    rows.nth(5).click(modifiers=["Shift"])  # several: each in full, a blank line apart
    eventually(lambda: pg.locator(".problems .prob.sel").count(), lambda n: n == 3)
    reset_clipboard(pg)
    pg.keyboard.press("ControlOrMeta+c")
    parts = eventually(lambda: clipboard(pg).split("\n\n"), lambda p: len(p) == 3)
    assert parts[0] == RHO and "key: initialize.u" in parts[1] and "key: initialize.p" in parts[2]
    rows.nth(4).click(button="right")
    assert pg.locator(".copy-menu button").inner_text() == "Copy 3 messages"
    pg.keyboard.press("Escape")


def test_double_click_or_enter_opens_the_problem_highlighted_in_the_check_report(clip: Any) -> None:
    pg = clip
    rows = pg.locator(".problems .prob")
    rows.nth(4).dblclick()
    focus = pg.locator(".su-report .su-diag.focus")
    eventually(lambda: focus.count(), lambda n: n == 1)
    assert "initialize.u" in focus.inner_text() and focus.get_attribute("data-code") == "E-KEY-UNUSED"
    shown = "el => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight }"
    assert focus.evaluate(shown)
    rows.nth(1).focus()
    pg.keyboard.press("Enter")
    eventually(lambda: focus.inner_text(), lambda t: "write_data[prefix" in t or "time_interval" in t)
    assert pg.locator(".su-report .su-diag.focus").count() == 1
    reset_clipboard(pg)
    focus.hover()
    focus.locator(".copy-btn").click()  # the report's entry copies the same text
    eventually(lambda: clipboard(pg), lambda t: t.startswith("error E-INPUT\nkey: write_data[1].time_interval\n"))


def test_toasts_and_run_alerts_copy_in_full(clip: Any) -> None:
    pg = clip
    pg.evaluate(
        "() => window.trame.state.set('h_toasts', "
        "[{id: 99, title: 'Export failed', text: 'disk full: /x/y.csv', severity: 'critical', path: ''}])"
    )
    toast = pg.locator(".toast", has_text="Export failed")
    toast.hover()
    reset_clipboard(pg)
    toast.locator(".copy-btn").click()
    eventually(lambda: clipboard(pg), lambda t: t == "Export failed\ndisk full: /x/y.csv")
    pg.evaluate("() => window.trame.state.set('h_toasts', [])")
