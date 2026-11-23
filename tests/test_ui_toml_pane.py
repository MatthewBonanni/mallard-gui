"""The input.toml tab in a real browser: TOML syntax colors, a case-tree click scrolling the text to the node's lines
and highlighting them (also when the tab is shown later), the caret's line selecting its node; the case tree's
headings and indentation. Needs Google Chrome (Playwright's channel)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .conftest import FIXTURES
from .test_ui import open_app, pw  # noqa: F401  (fixtures)
from .ui.world import eventually, make_world, serve

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")
TOML_JS = Path(__file__).parent.parent / "src" / "mallard_gui" / "assets" / "toml.js"
FIELD = 'textarea[aria-label="input.toml"]'
LINES = (FIXTURES / "cylinder_input.toml").read_text(encoding="utf-8").splitlines()


def line_of(text: str) -> int:
    return LINES.index(text) + 1


SAMPLE = """# a comment
[table]
bare-key = "basic \\" string"
"quoted key" = 'literal'
dotted.key = 1_000
float = -3.5e-2
hex = 0xDEAD_beef
inf = +inf
yes = true
when = 1979-05-27T07:32:00Z
day = 1979-05-27
at = 07:32:00
spaced = 1979-05-27 07:32:00.5
multi = \"\"\"
two "quoted" lines\"\"\"
raw = '''x'''
inline = {a = 1, b.c = "d"}
array = [1, [false, 2.0], {e = 'f'}]  # trailing

[[array.of.tables]]
"""


@pytest.fixture(scope="module")
def tokens(pw: Any) -> list[list[str]]:  # noqa: F811
    try:
        browser = pw.chromium.launch(channel="chrome")
    except sync_api.Error:
        pytest.skip("chromium is not installed")
    try:
        pg = browser.new_page()
        pg.set_content("<html><body></body></html>")
        pg.add_script_tag(path=str(TOML_JS))
        return list(pg.evaluate("s => window.mallardToml.tokenize(s)", SAMPLE))
    finally:
        browser.close()


def classes(tokens: list[list[str]], cls: str) -> list[str]:
    return [t for c, t in tokens if c == cls]


def test_tokens_cover_the_text(tokens: list[list[str]]) -> None:
    assert "".join(t for _, t in tokens) == SAMPLE


def test_each_toml_construct_gets_its_class(tokens: list[list[str]]) -> None:
    assert classes(tokens, "tk-comment") == ["# a comment", "# trailing"]
    assert classes(tokens, "tk-table") == ["[table]", "[[array.of.tables]]"]
    keys = classes(tokens, "tk-key")
    assert keys[:7] == ["bare-key", '"quoted key"', "dotted", "key", "float", "hex", "inf"]
    assert {"a", "b", "c", "e"} <= set(keys)
    assert classes(tokens, "tk-string") == [
        '"basic \\" string"',
        "'literal'",
        '"""\ntwo "quoted" lines"""',
        "'''x'''",
        '"d"',
        "'f'",
    ]
    assert classes(tokens, "tk-number") == ["1_000", "-3.5e-2", "0xDEAD_beef", "+inf", "1", "1", "2.0"]
    assert classes(tokens, "tk-bool") == ["true", "false"]
    assert classes(tokens, "tk-date") == ["1979-05-27T07:32:00Z", "1979-05-27", "07:32:00", "1979-05-27 07:32:00.5"]
    assert "".join(classes(tokens, "tk-punct")).count("{") == 2  # the inline tables
    assert "".join(classes(tokens, "tk-punct")).count("[") == 2  # the arrays, not the headers


def test_a_value_that_is_no_toml_stays_plain(pw: Any) -> None:  # noqa: F811
    browser = pw.chromium.launch(channel="chrome")
    try:
        pg = browser.new_page()
        pg.add_script_tag(path=str(TOML_JS))
        out = pg.evaluate("s => window.mallardToml.tokenize(s)", "x = 12abc\ny = truex\n")
        assert [c for c, t in out if t in ("12abc", "truex")] == ["", ""]
    finally:
        browser.close()


@pytest.fixture(scope="module")
def url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    project, env = make_world(tmp_path_factory.mktemp("toml-pane"), runs=False)
    env["MALLARD_DESKTOP_DATA"] = str(project.parent / "desktop")
    with serve(project, env) as u:
        yield u


@pytest.fixture
def page(pw: Any, url: str) -> Iterator[Any]:  # noqa: F811
    browser, pg = open_app(pw, "chromium", url)
    pg.click(".rail button[aria-label='Case setup']")
    try:
        yield pg
    finally:
        pg.evaluate("() => window.trame.state.set('tab', 'view')")  # the server's state is every page's
        browser.close()


def mark_lines(pg: Any) -> list[list[int]]:
    return list(
        pg.evaluate("[...document.querySelectorAll('.toml-mark')].map(m => [+m.dataset.first, +m.dataset.last])")
    )


def in_view(pg: Any, line: int) -> bool:
    return bool(
        pg.evaluate(
            """([sel, line]) => { const t = document.querySelector(sel); const s = getComputedStyle(t);
            const y = parseFloat(s.paddingTop) + (line - 1) * parseFloat(s.lineHeight);
            return y >= t.scrollTop && y + parseFloat(s.lineHeight) <= t.scrollTop + t.clientHeight; }""",
            [FIELD, line],
        )
    )


def test_the_text_is_colored_and_stays_editable(page: Any) -> None:
    pg = page
    pg.get_by_role("tab", name="input.toml").click()
    pg.wait_for_selector(".toml-code .tk-table")
    assert pg.locator(".toml-code .tk-table", has_text="[statistics.phase]").count() == 1
    field = pg.locator(FIELD)
    field.click()
    pg.keyboard.press("Meta+ArrowUp" if pg.evaluate("navigator.platform").startswith("Mac") else "Control+Home")
    pg.keyboard.type("# typed\n")
    eventually(lambda: pg.locator(".toml-code .tk-comment").first.inner_text(), lambda t: t == "# typed")
    pg.wait_for_timeout(500)  # the typed text reaches the server before Revert does
    pg.get_by_role("button", name="Revert").click()  # the server's text, set as the value, is colored again
    eventually(lambda: pg.locator(".toml-code .tk-comment").first.inner_text(), lambda t: t != "# typed")
    assert pg.evaluate("document.querySelector('.toml-code code').textContent") == field.input_value() + "\n"


def test_a_tree_click_scrolls_the_text_to_the_section_and_highlights_it(page: Any) -> None:
    pg = page
    pg.get_by_role("tab", name="input.toml").click()
    pg.wait_for_selector(".toml-code .tk-table")
    pg.locator(".tree-row", has_text="Statistics").first.click()
    first = line_of("[statistics]")
    eventually(lambda: mark_lines(pg), lambda m: m == [[first, line_of("level = 0.0")]])
    assert in_view(pg, first)
    assert pg.evaluate("document.activeElement === document.querySelector('.tree-row.sel')")
    pg.locator(".tree-row.child", has_text="wake_arc").click()
    arc = line_of('name = "wake_arc"') - 1
    eventually(lambda: mark_lines(pg), lambda m: m and m[0][0] == arc)
    assert in_view(pg, arc)


def test_a_click_while_another_tab_shows_positions_the_text_for_when_it_is_shown(page: Any) -> None:
    pg = page
    pg.get_by_role("tab", name="Case view").click()
    pg.locator(".tree-row", has_text="Statistics").first.click()
    eventually(lambda: pg.evaluate("window.trame.state.get('doc_reveal').spans"), bool)
    assert pg.evaluate("window.trame.state.get('tab')") == "view"
    assert pg.locator(FIELD).count() == 0
    # The first frame the textarea is drawn in already has it scrolled and highlighted: no visible jump.
    pg.evaluate(
        """sel => { window.__first = null; const seen = new MutationObserver(() => {
          const t = document.querySelector(sel);
          if (t && !window.__first) requestAnimationFrame(() => { window.__first = {
            top: t.scrollTop, marks: document.querySelectorAll('.toml-mark').length,
            focused: document.activeElement === t }; }), seen.disconnect();
        }); seen.observe(document.body, {childList: true, subtree: true}); }""",
        FIELD,
    )
    pg.get_by_role("tab", name="input.toml").click()
    first = eventually(lambda: pg.evaluate("window.__first"))
    assert first["top"] > 0 and first["marks"] == 1 and not first["focused"]
    assert in_view(pg, line_of("[statistics]"))


def test_the_caret_line_selects_its_node(page: Any) -> None:
    pg = page
    pg.get_by_role("tab", name="input.toml").click()
    pg.wait_for_selector(".toml-code .tk-table")
    line = line_of("[numerics.face_reconstruction]") + 1
    pg.evaluate(
        """([sel, line]) => { const t = document.querySelector(sel);
        const pos = t.value.split('\\n').slice(0, line - 1).join('\\n').length + 1;
        t.focus(); t.setSelectionRange(pos, pos); t.dispatchEvent(new MouseEvent('click', {bubbles: true})); }""",
        [FIELD, line],
    )
    eventually(lambda: pg.locator(".tree-row.sel .tlabel").inner_text(), lambda t: t == "Numerics")


def test_children_sit_right_of_their_section_and_sections_right_of_their_heading(page: Any) -> None:
    rows = page.evaluate(
        """() => [...document.querySelectorAll('.case-tree > div')].filter(e => e.offsetParent).map(e => ({
          group: e.classList.contains('tree-group'), child: e.classList.contains('child'),
          x: e.querySelector('.glabel, .tlabel').getBoundingClientRect().left }))"""
    )
    heading = section = None
    checked = 0
    for r in rows:
        if r["group"]:
            heading = r["x"]
        elif not r["child"]:
            assert heading is not None and r["x"] >= heading + 12
            section = r["x"]
        else:
            assert section is not None and r["x"] >= section + 12
            checked += 1
    assert checked >= 5


def test_a_heading_collapses_its_sections_and_its_chevron_turns(page: Any) -> None:
    pg = page
    heading = pg.locator(".tree-group", has_text="Physics")
    assert heading.get_attribute("aria-expanded") == "true"
    turned = "e => getComputedStyle(e.querySelector('.chev svg')).transform"
    open_transform = heading.evaluate(turned)
    heading.click()
    pg.wait_for_selector(".tree-row:has-text('Chemistry')", state="hidden")
    assert heading.get_attribute("aria-expanded") == "false"
    eventually(lambda: heading.evaluate(turned), lambda t: t != open_transform)
    heading.click()
    pg.wait_for_selector(".tree-row:has-text('Chemistry')", state="visible")


def test_the_check_status_sits_by_the_tree_and_opens_the_report(page: Any) -> None:
    pg = page
    status = pg.locator(".panel-head .check-status")
    eventually(lambda: status.inner_text(), lambda t: "warning" in t)
    assert pg.locator(".tree-row .tlabel", has_text=re.compile(r"^check$")).count() == 0
    status.click()
    eventually(lambda: pg.evaluate("window.trame.state.get('tab')"), lambda t: t == "report")
