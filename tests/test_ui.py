"""The app in a real browser. On every page, within each control, icons and labels share one vertical
center, button content is centered, and buttons on one row have one height; and the remotes page edits
remotes.toml and probes a remote through the fake ssh. Needs Google Chrome (Playwright's channel)."""

from __future__ import annotations

import math
import re
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .ui.world import FAKE_NOTIFICATION, eventually, free_port, make_world, serve

__all__ = ["FAKE_NOTIFICATION", "free_port"]

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")
ALIGNMENT = (Path(__file__).parent / "ui" / "alignment.js").read_text()
PAGES = ("Case setup", "Runs", "Data", "Results", "Analysis", "Compare runs", "Remotes", "Settings")
ENGINES = ("chromium", "webkit", "native")  # native: WebKit as the desktop app styles it (html.native)


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ui")


@pytest.fixture(scope="module")
def app_url(root: Path) -> Iterator[str]:
    with serve(*make_world(root)) as url:
        yield url


@pytest.fixture(scope="module")
def pw() -> Iterator[Any]:
    """One Playwright per module: its sync API does not nest."""
    with sync_api.sync_playwright() as p:
        yield p


def open_app(pw: Any, engine: str, url: str) -> tuple[Any, Any]:
    """The app at 1440×900 and device pixel ratio 2 in Chrome, or WebKit (the desktop app's engine, WKWebView)
    as a browser page or as the desktop app's page."""
    try:
        browser = pw.chromium.launch(channel="chrome") if engine == "chromium" else pw.webkit.launch(headless=True)
    except sync_api.Error:
        pytest.skip(f"{engine} is not installed")
    pg = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
    pg.add_init_script(FAKE_NOTIFICATION)
    if engine == "native":
        pg.add_init_script("document.documentElement.classList.add('native')")
    pg.goto(url)
    pg.wait_for_selector(".tree-row", timeout=30_000)
    return browser, pg


@pytest.fixture(scope="module")
def page(pw: Any, app_url: str) -> Iterator[object]:
    browser, pg = open_app(pw, "chromium", app_url)
    yield pg
    browser.close()


@pytest.fixture(scope="module", params=ENGINES)
def engine_page(request: pytest.FixtureRequest, pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    """Each engine on a server of its own: trame's state is the server's, shared by every page open on it."""
    with serve(*make_world(tmp_path_factory.mktemp(f"ui-{request.param}"))) as url:
        browser, pg = open_app(pw, request.param, url)
        yield pg
        browser.close()


def layout_problems(pg: Any, timeout: float = 5.0) -> str:
    """The layout's problems once the page has settled: a page still drawing (fonts, a view, a late state
    update under load) is measured again, a problem that stays is reported."""
    end = time.monotonic() + timeout
    while True:
        problems = pg.evaluate(ALIGNMENT, 1.0)
        if not problems or time.monotonic() > end:
            return "\n".join(f"{p['control']}: {p['problem']} ({p['dy']})" for p in problems)
        pg.wait_for_timeout(250)


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("title", PAGES)
def test_controls_are_aligned(engine_page: object, title: str, theme: str) -> None:
    pg = engine_page  # playwright Page
    pg.evaluate(f"document.documentElement.dataset.theme = '{theme}'")  # type: ignore[attr-defined]
    pg.click(f".rail button[aria-label='{title}']")  # type: ignore[attr-defined]
    if title == "Results":
        open_box_run(pg)
    pg.wait_for_timeout(800)  # type: ignore[attr-defined]
    problems = layout_problems(pg)
    assert not problems, problems
    overflow = pg.evaluate(  # type: ignore[attr-defined]
        "[...document.querySelectorAll('.toolbar')].filter(t => t.offsetParent && t.scrollWidth > t.clientWidth + 1)"
        ".map(t => `${t.innerText.replace(/\\s+/g, ' ')} (${t.scrollWidth} > ${t.clientWidth} px)`)"
    )
    assert overflow == [], f"toolbar content wider than the toolbar: {overflow}"


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("tab", ["Boundaries", "Initial state", "Mesh quality"])
def test_analysis_panel_is_aligned(engine_page: object, tab: str, theme: str) -> None:
    pg = engine_page  # playwright Page
    pg.evaluate(f"document.documentElement.dataset.theme = '{theme}'")  # type: ignore[attr-defined]
    pg.click(".rail button[aria-label='Case setup']")  # type: ignore[attr-defined]
    if not pg.locator(".ana-panel").count():  # type: ignore[attr-defined]
        pg.click("button.ana-toggle")  # type: ignore[attr-defined]
    pg.locator(".ana-tabs .tab", has_text="Mesh quality").click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".ana-panel .bars", timeout=20_000)  # type: ignore[attr-defined]
    pg.locator(".ana-tabs .tab", has_text=tab).click()  # type: ignore[attr-defined]
    pg.wait_for_timeout(500)  # type: ignore[attr-defined]
    problems = layout_problems(pg)
    assert not problems, problems
    overflow = pg.evaluate(  # type: ignore[attr-defined]
        "[...document.querySelectorAll('.ana-panel, .ana-panel .ana-row, .ana-tabs')]"
        ".filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.className)"
    )
    assert overflow == [], f"wider than its box: {overflow}"
    if tab == "Mesh quality":
        pg.locator(".ana-card.hist .bar:not(.empty)").first.click()  # type: ignore[attr-defined]
        pg.wait_for_selector(".ana-panel >> text=/cells with/")  # type: ignore[attr-defined]


def open_box_run(pg: object) -> None:
    rows = ".panel:has(> .panel-head > span:text-is('Results')) .run-row"
    pg.locator(rows, has_text="box-1").click()  # type: ignore[attr-defined]
    shown = ".results-view img.frame[src^='data:image/jpeg'], .results-view canvas:visible"
    pg.wait_for_selector(shown, timeout=30_000)  # type: ignore[attr-defined]  # local: drawn in the browser


def test_results_page_renders_a_local_run_in_place(page: object) -> None:
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Results']")  # type: ignore[attr-defined]
    open_box_run(pg)
    pg.get_by_role("button", name="images", exact=True).click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".results-view img.frame[src^='data:image/jpeg']", timeout=30_000)  # type: ignore[attr-defined]
    pg.wait_for_selector(".colorbar", timeout=10_000)  # type: ignore[attr-defined]
    image = "(() => { const i = document.querySelector('img.frame'); return [i.naturalWidth, i.clientWidth] })()"
    size = pg.evaluate(image)  # type: ignore[attr-defined]
    assert size[0] >= size[1] > 100  # rendered at the viewport's device pixels
    body = pg.locator(".body.results")  # type: ignore[attr-defined]
    sync_api.expect(body).to_contain_text("inlet")  # the zone surface series
    sync_api.expect(body).to_contain_text("P")


def test_remotes_page_adds_a_remote_tests_it_and_records_an_install(page: object, root: Path) -> None:
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Remotes']")  # type: ignore[attr-defined]
    pg.click(".panel-head button[title='Add a remote']")  # type: ignore[attr-defined]
    pg.wait_for_selector(".run-head .name >> text=New remote")  # type: ignore[attr-defined]
    pg.wait_for_function("!document.querySelector(\"input[aria-label='host']\").value")  # type: ignore[attr-defined]
    pg.fill("input[aria-label='name']", "cluster")  # type: ignore[attr-defined]
    pg.fill("input[aria-label='host']", "hpc")  # type: ignore[attr-defined]
    pg.fill("input[aria-label='run root']", "/scratch/runs")  # type: ignore[attr-defined]
    pg.fill("input[aria-label='partition']", "gpu")  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Add remote").click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".rm-row.sel >> text=cluster")  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Test connection").click()  # type: ignore[attr-defined]
    pg.wait_for_selector("text=gpu (gpu:a100:4)", timeout=20_000)  # type: ignore[attr-defined]
    home = root / "homes" / "hpc" / "mallard" / "0.10.0-cpu"
    pg.locator("tr", has_text=str(home / "bin" / "mallard")).get_by_role("button", name="Use").click()  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Refresh the queue").click()  # type: ignore[attr-defined]
    pg.wait_for_selector("text=cyl-re100")  # type: ignore[attr-defined]
    saved = tomllib.loads((root / "config" / "remotes.toml").read_text())["remotes"]
    assert saved["cluster"]["host"] == "hpc"
    assert saved["cluster"]["defaults"] == {"partition": "gpu"}
    assert saved["cluster"]["storage"] == {"run_root": "/scratch/runs"}
    assert saved["cluster"]["installs"] == [{"path": str(home / "bin" / "mallard"), "version": "0.10.0"}]
    assert "hpc" in saved  # the fixture's remote is untouched


def test_remotes_page_upgrades_after_confirming_and_verifying(page: object, root: Path) -> None:
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Remotes']")  # type: ignore[attr-defined]
    pg.locator(".rm-row", has_text="cluster").click()  # type: ignore[attr-defined]
    pg.fill("input[aria-label='install version']", "0.11.0")  # type: ignore[attr-defined]
    pg.press("input[aria-label='install version']", "Enter")  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Upgrade to 0.11.0").click()  # type: ignore[attr-defined]
    dialog = pg.get_by_role("dialog")  # type: ignore[attr-defined]
    dialog.get_by_text("Upgrade Mallard 0.11.0 on cluster").wait_for(timeout=20_000)
    new = root / "homes" / "hpc" / "mallard" / "0.11.0-cpu"
    assert str(new) in dialog.inner_text()
    assert "SHA-256 " in dialog.inner_text()
    assert not new.exists()  # nothing ran before the confirmation
    dialog.get_by_role("button", name="Upgrade").click()
    pg.wait_for_selector(".install-log >> text=now the default", timeout=30_000)  # type: ignore[attr-defined]
    installs = tomllib.loads((root / "config" / "remotes.toml").read_text())["remotes"]["cluster"]["installs"]
    assert [i["version"] for i in installs] == ["0.11.0", "0.10.0"]
    assert installs[0]["verified"].startswith("passed ") and installs[0]["method"] == "archive"
    assert (new / "install.toml").exists() and (root / "homes" / "hpc" / "mallard" / "0.10.0-cpu").exists()


def test_theme_button_switches_the_document_theme(page: object) -> None:
    pg = page  # playwright Page
    theme = "document.documentElement.dataset.theme"
    background = "getComputedStyle(document.body).backgroundColor"
    pg.click("button[title='Light or dark theme']")  # type: ignore[attr-defined]
    pg.wait_for_function(f"['light', 'dark'].includes({theme})")  # type: ignore[attr-defined]
    first, bg = pg.evaluate(f"[{theme}, {background}]")  # type: ignore[attr-defined]
    pg.click("button[title='Light or dark theme']")  # type: ignore[attr-defined]
    pg.wait_for_function(f"{theme} !== '{first}'")  # type: ignore[attr-defined]
    assert pg.evaluate(background) != bg  # type: ignore[attr-defined]


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("part", ["launch", "report", "control", "palette"])
def test_setup_parts_are_aligned(engine_page: object, part: str, theme: str) -> None:
    pg = engine_page  # playwright Page
    pg.evaluate(f"document.documentElement.dataset.theme = '{theme}'")  # type: ignore[attr-defined]
    if part == "control":
        pg.click(".rail button[aria-label='Runs']")  # type: ignore[attr-defined]
        pg.get_by_role("button", name="Control").click()  # type: ignore[attr-defined]
        pg.wait_for_selector(".empty-state >> text=while a run on this machine is running")  # type: ignore[attr-defined]
    else:
        pg.click(".rail button[aria-label='Case setup']")  # type: ignore[attr-defined]
    if part == "launch":
        pg.click(".runbtn")  # type: ignore[attr-defined]
        pg.wait_for_selector(".su-dialog input[aria-label='ranks']")  # type: ignore[attr-defined]
    elif part == "report":
        pg.get_by_role("tab", name="Check report").click()  # type: ignore[attr-defined]
        pg.wait_for_selector(".su-sec >> text=TIME STEP")  # type: ignore[attr-defined]
    elif part == "palette":
        pg.keyboard.press("Meta+k")  # type: ignore[attr-defined]
        pg.keyboard.type("farf")  # type: ignore[attr-defined]
        pg.wait_for_selector(".search-results div.on >> text=farfield")  # type: ignore[attr-defined]
    pg.wait_for_timeout(500)  # type: ignore[attr-defined]
    problems = layout_problems(pg)
    assert not problems, problems
    if part == "launch":
        pg.get_by_role("button", name="Close").click()  # type: ignore[attr-defined]
        pg.wait_for_selector(".su-dialog", state="detached")  # type: ignore[attr-defined]
    elif part == "report":
        pg.get_by_role("tab", name="Case view").click()  # type: ignore[attr-defined]
    elif part == "palette":
        pg.keyboard.press("Enter")  # type: ignore[attr-defined]
        pg.wait_for_selector(".prop-title >> text=farfield")  # type: ignore[attr-defined]


SPACE_GROUPED = "\\b\\d{1,3}(?:[ \u2009\u202f\u00a0]\\d{3})+\\b"


def test_numbers_group_with_commas_on_every_page(page: object) -> None:
    pg = page  # playwright Page
    texts = {}
    for title in PAGES:
        pg.click(f".rail button[aria-label='{title}']")  # type: ignore[attr-defined]
        pg.wait_for_timeout(400)  # type: ignore[attr-defined]
        texts[title] = pg.evaluate("document.body.innerText")  # type: ignore[attr-defined]
    pg.click(".rail button[aria-label='Case setup']")  # type: ignore[attr-defined]
    pg.get_by_role("tab", name="Check report").click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".su-sec >> text=TIME STEP")  # type: ignore[attr-defined]
    texts["check report"] = pg.evaluate("document.body.innerText")  # type: ignore[attr-defined]
    pg.click(".runbtn")  # type: ignore[attr-defined]
    pg.wait_for_selector(".su-dialog input[aria-label='ranks']")  # type: ignore[attr-defined]
    texts["run dialog"] = pg.evaluate("document.querySelector('.su-dialog').innerText")  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Close").click()  # type: ignore[attr-defined]
    pg.get_by_role("tab", name="Case view").click()  # type: ignore[attr-defined]
    spaced = {k: re.findall(SPACE_GROUPED, t) for k, t in texts.items()}
    assert not any(spaced.values()), spaced
    assert "1,536" in texts["check report"]  # the cells, grouped


def test_every_run_dialog_control_is_in_exactly_one_part(page: object) -> None:
    """RUN (passed to the launched process), ESTIMATE (only the cost estimate) or the dialog's own."""
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Case setup']")  # type: ignore[attr-defined]
    pg.click(".runbtn")  # type: ignore[attr-defined]
    pg.wait_for_selector(".su-dialog input[aria-label='ranks']")  # type: ignore[attr-defined]
    parts = pg.evaluate(  # type: ignore[attr-defined]
        """[...document.querySelectorAll('.su-dialog :is(input, select, button)')].map((c) => {
            const owners = [];
            for (let e = c.closest('[data-part]'); e; e = e.parentElement && e.parentElement.closest('[data-part]')) {
                owners.push(e.dataset.part);
            }
            return [c.getAttribute('aria-label') || c.textContent.trim(), owners];
        })"""
    )
    assert all(len(owners) == 1 for _, owners in parts), parts
    by = {name: owners[0] for name, owners in parts}
    assert by["ranks"] == by["Launch"] == "run"
    assert by["size for"] == by["throughput"] == by["measure for at most"] == by["Measure"] == "estimate"
    assert pg.locator(".su-launch-btn").inner_text().strip() == "Launch on this machine, 1 rank"  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Close").click()  # type: ignore[attr-defined]


def test_a_click_on_a_sizing_plot_sets_the_runs_ranks(page: object) -> None:
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Case setup']")  # type: ignore[attr-defined]
    pg.click(".runbtn")  # type: ignore[attr-defined]
    ranks = pg.locator(".su-dialog input[aria-label='ranks']")  # type: ignore[attr-defined]
    try:
        pg.wait_for_selector("#su_chart_mem svg polyline", timeout=30_000)  # type: ignore[attr-defined]
        pg.wait_for_selector(".su-fig:has-text('max cells per rank') >> text=186,724")  # type: ignore[attr-defined]
        box = pg.locator("#su_chart_mem svg").bounding_box()  # type: ignore[attr-defined]
        for f in (0.37, 0.61):  # between sampled counts: the readout is a whole one of them
            pg.mouse.move(box["x"] + box["width"] * f, box["y"] + box["height"] / 2)  # type: ignore[attr-defined]
            readout = pg.locator("#su_chart_mem").get_attribute("data-readout")  # type: ignore[attr-defined]
            assert re.match(r"ranks (1|2|4|8)( |$)", readout or ""), readout
        pg.mouse.click(box["x"] + box["width"] - 14, box["y"] + box["height"] / 2)  # type: ignore[attr-defined]
        pg.wait_for_function("document.querySelector(\".su-dialog input[aria-label='ranks']\").value === '8'")  # type: ignore[attr-defined]
        pg.wait_for_selector(".su-launch-btn >> text=Launch on this machine, 8 ranks")  # type: ignore[attr-defined]
        pg.wait_for_selector(".su-fig >> text=on 8 ranks")  # type: ignore[attr-defined]
    finally:
        ranks.fill("1")
        ranks.press("Enter")
        ranks.blur()
        pg.wait_for_selector(".su-launch-btn >> text=Launch on this machine, 1 rank")  # type: ignore[attr-defined]
        pg.get_by_role("button", name="Close").click()  # type: ignore[attr-defined]


def test_a_disabled_launch_is_gray_and_says_what_it_waits_for(page: Any) -> None:
    """A disabled primary was the accent at half opacity, which reads as a dim but working green button."""
    pg = page
    pg.click(".rail button[aria-label='Case setup']")
    pg.click(".runbtn")
    btn = pg.locator(".su-launch-btn")
    pg.wait_for_selector(".su-launch-btn:enabled")
    enabled_bg = btn.evaluate("b => getComputedStyle(b).backgroundColor")
    # The server's next refresh of the case puts its own run_blocked back: blocked again until each is seen.
    blocked = "(() => { const st = window.trame.state; if (!st.get('run_blocked')) st.set('run_blocked', '2 errors');"
    try:
        pg.wait_for_function(
            f"{blocked} const b = document.querySelector('.su-launch-btn'), s = getComputedStyle(b);"
            f" return b.disabled && s.backgroundColor !== '{enabled_bg}' && s.opacity === '1' }})()"
        )
        pg.wait_for_function(
            f"{blocked} const why = document.querySelector('.su-launch-why');"
            " return !!why && why.innerText === 'Blocked: 2 errors' })()"
        )
        pg.evaluate("window.trame.state.set('su_launch', {...window.trame.state.get('su_launch'), busy: 'measuring'})")
        pg.wait_for_selector(".su-launch-why .spin")
    finally:
        pg.evaluate("window.trame.state.set('run_blocked', '')")
        pg.evaluate("window.trame.state.set('su_launch', {...window.trame.state.get('su_launch'), busy: ''})")
        pg.get_by_role("button", name="Close").click()


VISIBLE = """(sel) => [...document.querySelectorAll(sel)].map((e) => {
    const r = e.getBoundingClientRect();
    let clipped = r.top < 0 || r.left < 0 || r.bottom > innerHeight || r.right > innerWidth;
    for (let a = e.parentElement; a && !clipped; a = a.parentElement) {
        if (getComputedStyle(a).overflow === 'visible') continue;
        const b = a.getBoundingClientRect();
        clipped = r.top < b.top - 1 || r.bottom > b.bottom + 1 || r.left < b.left - 1 || r.right > b.right + 1;
    }
    return {text: (e.textContent || '').slice(0, 40), clipped, cut: e.scrollWidth > e.clientWidth + 1};
})"""


@pytest.mark.parametrize("size", [(1440, 900), (1100, 700)])
def test_the_run_dialog_shows_its_verdict_figures_and_plots_whole(page: Any, size: tuple[int, int]) -> None:
    """At a laptop's size and a small window's, without scrolling: the sizing verdict, every figure's value and
    the three plots over ranks are in view and none is cut off."""
    pg = page
    pg.set_viewport_size({"width": size[0], "height": size[1]})
    try:
        pg.click(".rail button[aria-label='Case setup']")
        pg.click(".runbtn")
        pg.wait_for_selector("#su_chart_mem svg polyline", timeout=30_000)
        pg.wait_for_selector(".su-fig:has-text('max cells per rank') >> text=186,724")
        pg.wait_for_selector(".su-fit")
        shown = [
            *pg.evaluate(VISIBLE, ".su-fit"),
            *pg.evaluate(VISIBLE, ".su-fig-v"),
            *pg.evaluate(VISIBLE, ".su-chart svg"),
        ]
        assert len(shown) == 1 + 6 + 3
        assert not [s for s in shown if s["clipped"] or s["cut"]], shown
    finally:
        pg.get_by_role("button", name="Close").click()
        pg.set_viewport_size({"width": 1440, "height": 900})


def aligned(pg: object) -> None:
    for theme in ("dark", "light"):
        pg.evaluate(f"document.documentElement.dataset.theme = '{theme}'")  # type: ignore[attr-defined]
        pg.wait_for_timeout(300)  # type: ignore[attr-defined]
        problems = layout_problems(pg)
        assert not problems, f"{theme}: {problems}"


def test_remotes_page_shows_and_tests_a_kubernetes_remote(page: object) -> None:
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Remotes']")  # type: ignore[attr-defined]
    pg.locator(".rm-row", has_text="lab").click()  # type: ignore[attr-defined]
    pg.wait_for_function("document.querySelector(\"input[aria-label='namespace']\")?.value === 'me'")  # type: ignore[attr-defined]
    assert pg.locator("input[aria-label='context']").input_value() == "lab"  # type: ignore[attr-defined]
    assert pg.locator("input[aria-label='volume claim']").input_value() == "mallard-data"  # type: ignore[attr-defined]
    assert pg.locator("input[aria-label='memory']").input_value() == "4Gi"  # type: ignore[attr-defined]
    assert pg.locator("input[aria-label='partition']").count() == 0  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Test connection").click()  # type: ignore[attr-defined]
    pg.wait_for_selector("text=NVIDIA-A100-SXM4-80GB (1)", timeout=20_000)  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Refresh the queue").click()  # type: ignore[attr-defined]
    pg.wait_for_selector("text=mallard-gui-cyl-1-ab12")  # type: ignore[attr-defined]
    assert pg.locator("tr", has_text="mallard-gui-cyl-1-ab12").inner_text().count("running") == 1  # type: ignore[attr-defined]
    aligned(pg)


def test_data_page_relocate_panel_reports_a_run_without_a_restart(page: object) -> None:
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Data']")  # type: ignore[attr-defined]
    pg.locator(".run-row:visible", has_text="box-1").click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".relocate >> text=Move or continue")  # type: ignore[attr-defined]
    for value in ("pull", "shared"):
        assert pg.locator(f"select option[value='{value}']").count() == 1  # type: ignore[attr-defined]
    pg.locator(".panel.right select:visible").first.select_option("lab")  # type: ignore[attr-defined]
    pg.locator(".relocate").get_by_role("button", name="Check restart").click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".relocate .rl-text.err >> text=no restart file", timeout=20_000)  # type: ignore[attr-defined]
    aligned(pg)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_run_health_and_an_export_toast(page: object, root: Path, theme: str) -> None:
    pg = page  # playwright Page
    pg.evaluate(f"document.documentElement.dataset.theme = '{theme}'")  # type: ignore[attr-defined]
    pg.click(".rail button[aria-label='Runs']")  # type: ignore[attr-defined]
    pg.locator(".run-row:visible", has_text="input-1").click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".health .alert >> text=W-FORMAT-VTU")  # type: ignore[attr-defined]
    tops = pg.locator(".stats .stat").evaluate_all("els => els.map(e => e.getBoundingClientRect().top)")  # type: ignore[attr-defined]
    assert len(set(tops)) < len(tops) / 2, tops  # figures are a band of tiles, not a column
    pg.locator(".plots:not(.results-plots) .plot").first.get_by_role("button", name="CSV").click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".toast >> text=Exported CSV")  # type: ignore[attr-defined]
    exported = list((root / "case" / "exports" / "input-1").glob("*.csv"))
    assert exported and exported[0].read_text().startswith("t (nd),U_Y,P\n0.0,")  # the plot as shown
    problems = layout_problems(pg)
    assert not problems, problems
    pg.locator(".toast button[title='Dismiss']").first.click()  # type: ignore[attr-defined]


def test_notify_asks_the_browser_once_and_shows_through_the_web_api(page: object) -> None:
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Runs']")  # type: ignore[attr-defined]
    pg.locator(".run-row:visible", has_text="input-1").click()  # type: ignore[attr-defined]
    box = pg.locator(".health input[aria-label='notify']")  # type: ignore[attr-defined]
    assert not box.is_checked()  # on, but not granted yet
    box.click()
    pg.wait_for_function("window.__asked === 1")  # type: ignore[attr-defined]
    pg.wait_for_function("document.querySelector(\".health input[aria-label='notify']\").checked")  # type: ignore[attr-defined]
    pg.evaluate("trame.state.set('h_native', {id: 99, title: 'Mallard GUI test', body: 'b', icon: 'i.png'})")  # type: ignore[attr-defined]
    pg.wait_for_function("window.__notes.length === 1")  # type: ignore[attr-defined]
    note = pg.evaluate("window.__notes[0]")  # type: ignore[attr-defined]
    assert note == {"title": "Mallard GUI test", "body": "b", "icon": "i.png", "tag": "mallard-99"}


def test_a_feature_is_off_without_its_table_and_its_switch_turns_it_on_and_off(page: Any) -> None:
    pg = page
    pg.click(".rail button[aria-label='Case setup']")
    chem = pg.locator(".tree-row", has_text="Chemistry").first
    assert chem.locator(".tval").inner_text() == "off"
    chem.click()
    switch = pg.locator(".group-head .fswitch").first
    assert switch.get_attribute("aria-checked") == "false"
    assert pg.locator(".prow .plabel", has_text="enabled").count() == 0  # the switch is chemistry's on and off
    pg.wait_for_selector(".feature-off >> text=Finite-rate chemistry")
    les = pg.locator(".tree-row", has_text="LES").first
    les.click()
    pg.locator(".feature-off button", has_text="Turn on").click()
    pg.wait_for_selector(".group-head .fswitch[aria-checked='true']")
    pg.wait_for_function("document.querySelector('.tree-row.sel .tval').textContent === 'on'")
    pg.wait_for_selector(".prow .plabel >> text=model")
    pg.locator(".group-head .fswitch").first.click()  # nothing of the user's in it: off at once
    pg.wait_for_selector(".group-head .fswitch[aria-checked='false']")
    pg.wait_for_function("!(trame.state.get('toml_text') || '').includes('[les]')")


def test_compare_page_shows_the_keys_that_differ(page: object) -> None:
    pg = page  # playwright Page
    pg.click(".rail button[aria-label='Compare runs']")  # type: ignore[attr-defined]
    pg.select_option("select[aria-label='run A']", "input-1")  # type: ignore[attr-defined]
    pg.select_option("select[aria-label='run B']", "box-1")  # type: ignore[attr-defined]
    pg.wait_for_selector(".cmp-table tr.removed")  # type: ignore[attr-defined]
    assert "keys differ" in pg.locator(".cmp-bar").inner_text()  # type: ignore[attr-defined]
    heads = pg.locator(".cmp-table tr.sec td > span:first-child").all_inner_texts()  # type: ignore[attr-defined]
    assert "mesh" in [h.lower() for h in heads], heads
    assert pg.locator(".cmp-table tr.sec + tr td.k").first.inner_text() != ""  # type: ignore[attr-defined]


def test_wheel_zoom_is_fine_and_continuous_toward_the_cursor(page: object) -> None:
    pg = page
    pg.click(".rail button[aria-label='Results']")  # type: ignore[attr-defined]
    open_box_run(pg)
    pg.get_by_role("button", name="geometry", exact=True).click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".results-view canvas", state="visible", timeout=20_000)  # type: ignore[attr-defined]
    events = [
        {"deltaY": 100, "deltaMode": 0},  # a mouse notch
        {"deltaY": 3, "deltaMode": 1},  # the same in lines (Firefox)
        {"deltaY": 4, "deltaMode": 0},  # a trackpad's small pixel delta
        {"deltaY": -4, "deltaMode": 0},
        {"deltaY": 5, "deltaMode": 0, "ctrlKey": True},  # a pinch step
        {"deltaY": 5000, "deltaMode": 0},  # a burst
    ]
    f = pg.evaluate("(es) => es.map((e) => window.mallardZoom.factor(e))", events)  # type: ignore[attr-defined]
    assert f[0] == pytest.approx(math.exp(-0.08))
    assert f[1] == pytest.approx(math.exp(-48 * 0.0008))
    assert 0.996 < f[2] < 1 and f[2] * f[3] == pytest.approx(1.0)
    assert f[4] == pytest.approx(math.exp(-0.05))
    assert f[5] == pytest.approx(math.exp(-0.16))
    box = pg.locator(".results-view canvas:visible").first.bounding_box()  # type: ignore[attr-defined]
    x, y = box["x"] + 0.75 * box["width"], box["y"] + 0.3 * box["height"]
    pg.mouse.move(x, y)  # type: ignore[attr-defined]
    camera = "(() => { try { return trame.refs.geoview.getCamera(); } catch (e) { return null; } })()"
    pg.wait_for_function(f"(() => {{ const c = {camera}; return c && c.position[2] !== 1; }})()")  # type: ignore[attr-defined]

    def gap() -> float:
        c = pg.evaluate(camera)  # type: ignore[attr-defined]
        return math.dist(c["position"], c["focalPoint"])

    focal = pg.evaluate(camera)["focalPoint"]  # type: ignore[attr-defined]
    start = gap()
    for _ in range(25):
        pg.mouse.wheel(0, -4)  # type: ignore[attr-defined]
    pg.wait_for_timeout(100)  # type: ignore[attr-defined]
    small = gap() / start
    pg.mouse.wheel(0, -100)  # type: ignore[attr-defined]
    pg.wait_for_timeout(100)  # type: ignore[attr-defined]
    notch = gap() / start / small
    assert 0.9 < small < 1 and small == pytest.approx(notch, rel=1e-3)  # by distance, not by event count
    assert pg.evaluate(camera)["focalPoint"] != focal  # type: ignore[attr-defined]  # toward the cursor


FOCUS_RING = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return {tag: 'body', ring: false};
  const s = getComputedStyle(el);
  const outline = s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 1;
  const what = el.getAttribute('title') || el.getAttribute('aria-label') || el.textContent.trim().slice(0, 20);
  return {tag: el.tagName.toLowerCase() + '.' + el.className + ' ' + what, ring: outline || s.boxShadow !== 'none'};
}"""


@pytest.mark.parametrize("title", ["Case setup", "Settings"])
def test_keyboard_navigation_shows_where_focus_is(engine_page: object, title: str) -> None:
    pg: Any = engine_page
    pg.click(f".rail button[aria-label='{title}']")
    pg.wait_for_timeout(500)
    pg.evaluate("document.activeElement && document.activeElement.blur()")
    stops = []
    for _ in range(30):
        pg.keyboard.press("Tab")
        stops.append(pg.evaluate(FOCUS_RING))
    assert sum(s["tag"] != "body" for s in stops) > 10, stops  # the controls are reachable by keyboard
    hidden = [s["tag"] for s in stops if s["tag"] != "body" and not s["ring"]]
    assert not hidden, f"focused without a visible ring: {hidden}"


HOVERED = (
    ".rail button:not(.on)",
    ".tb:not(.on):not(:disabled)",
    ".btn:not(:disabled)",
    ".tree-row:not(.sel)",
    ".tab:not(.on)",
)


@pytest.mark.parametrize("selector", HOVERED)
def test_controls_answer_the_pointer(engine_page: object, selector: str) -> None:
    pg: Any = engine_page
    pg.click(".rail button[aria-label='Case setup']")
    pg.mouse.move(1, 899)
    target = pg.locator(f"{selector}:visible").first.element_handle()
    look = "e => { const s = getComputedStyle(e); return [s.backgroundColor, s.color, s.borderColor].join() }"
    before = target.evaluate(look)
    target.hover()
    try:
        pg.wait_for_function(f"([e, before]) => ({look})(e) !== before", arg=[target, before], timeout=3_000)
    except sync_api.TimeoutError:
        pytest.fail(f"{selector}: no hover feedback")


def test_settings_page_saves_each_change_and_applies_it(page: object, root: Path) -> None:
    pg: Any = page
    pg.click(".rail button[aria-label='Settings']")
    pg.wait_for_selector(".settings-title")
    pg.locator(".segmented .seg", has_text="Light").click()
    pg.wait_for_function("document.documentElement.dataset.theme === 'light'")
    pg.select_option("select[aria-label='length']", "mm")
    threads = pg.locator("input[aria-label='threads']")
    threads.fill("0")
    threads.press("Enter")
    pg.wait_for_selector(".set-error >> text=threads is at least 1")
    threads.fill("3")
    threads.press("Enter")
    pg.wait_for_function("!document.querySelector('.set-error')")
    exe = pg.locator("input[aria-label='mallard 3D']")
    exe.fill(str(root / "missing-mallard3d"))
    exe.press("Enter")
    pg.wait_for_selector(".set-error >> text=no such file")
    saved = tomllib.loads((root / "config" / "settings.toml").read_text())
    assert saved["appearance"] == {"theme": "light"} and saved["units"] == {"length": "mm"}
    assert saved["runs"]["threads"] == 3 and "mallard" not in saved
    pg.locator(".segmented .seg", has_text="System").click()
    eventually(lambda: "appearance" not in tomllib.loads((root / "config" / "settings.toml").read_text()))
    assert pg.evaluate("document.documentElement.dataset.theme") == "light"  # headless Chrome is light


COVERED = """() => { const n = document.querySelector('.doc-notice'); const r = n.getBoundingClientRect();
  return [...document.querySelectorAll('button, input, select, .tab, .tree-row')].filter(e => {
    const b = e.getBoundingClientRect(); if (!b.width || !e.offsetParent || n.contains(e)) return false;
    const x = b.left + b.width / 2, y = b.top + b.height / 2;
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }).map(e => e.getAttribute('title') || e.getAttribute('aria-label') || e.innerText.trim()); }"""


@pytest.mark.parametrize("title", ["Case setup", "Runs", "Data"])
def test_a_standing_notice_covers_no_control(page: object, title: str) -> None:
    pg: Any = page
    pg.click(f".rail button[aria-label='{title}']")
    pg.evaluate("trame.state.set('doc_recovered', 'Recovered unsaved changes: save to keep them.')")
    pg.wait_for_selector(".doc-notice")
    assert pg.evaluate(COVERED) == []
    pg.locator(".doc-notice button[title='Dismiss']").click()
    pg.wait_for_selector(".doc-notice", state="detached")


def test_the_status_bar_reads_as_words(page: Any) -> None:
    """The build as hardware, the check's verdict and when, the file's state: no raw `check warning 0 s at`."""
    pg = page
    pg.click(".rail button[aria-label='Case setup']")
    bar = pg.locator(".statusbar")
    sync_api.expect(bar).to_contain_text(re.compile(r"1 warning\s*checked \d\d:\d\d:\d\d"))
    text = bar.inner_text()
    assert "CPU (" in text and "input.toml · saved" in text, text
    assert "check warning" not in text, text


OVERLAPS = """(pairs) => pairs.map(([a, b]) => {
    const p = document.querySelector(a), q = document.querySelector(b);
    if (!p || !q || !p.offsetParent || !q.offsetParent) return [a, b, 'missing'];
    const r = p.getBoundingClientRect(), s = q.getBoundingClientRect();
    return r.left < s.right && s.left < r.right && r.top < s.bottom && s.top < r.bottom ? [a, b, 'overlap'] : null;
}).filter(Boolean)"""
WRAPPED_TABS = """() => [...document.querySelectorAll('.tabs .tab')].filter((t) => t.offsetParent).filter((t) => {
    const walk = document.createTreeWalker(t, NodeFilter.SHOW_TEXT);
    const lines = new Set();
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
        const range = document.createRange(); range.selectNodeContents(n);
        for (const r of range.getClientRects()) lines.add(Math.round(r.top));
    }
    return lines.size > 1;
}).map((t) => t.textContent.trim())"""


def test_a_small_window_keeps_tabs_on_one_line_and_view_overlays_apart(page: Any) -> None:
    pg = page
    pg.set_viewport_size({"width": 1100, "height": 700})
    try:
        pg.click(".rail button[aria-label='Case setup']")
        pg.get_by_role("tab", name="Case view").click()
        pg.locator(".tree-row", has_text="Boundaries").first.click()
        assert pg.evaluate(WRAPPED_TABS) == []
        pg.click(".rail button[aria-label='Results']")
        open_box_run(pg)
        pg.wait_for_selector(".results-view .colorbar")
        assert pg.evaluate(OVERLAPS, [[".results-view .colorbar", ".results-view .readout"]]) == []
    finally:
        pg.set_viewport_size({"width": 1440, "height": 900})


# Elements drawn past their box (into a neighbour, or clipped by it).
SPILLED = """(selectors) => selectors.flatMap((sel) => [...document.querySelectorAll(sel)].filter((e) => e.offsetParent)
    .filter((e) => {
        const box = e.closest(sel.includes('.stat') ? '.stat' : '.plot').getBoundingClientRect();
        const r = e.getBoundingClientRect();
        return e.scrollWidth > e.clientWidth + 1 || r.left < box.left - 1 || r.right > box.right + 1;
    }).map((e) => sel + ': ' + e.textContent.trim()))"""


def test_choosing_a_finished_runs_plot_quantity_redraws_that_plot(page: Any) -> None:
    """A finished run's plot kept its quantity: the choice redrew the run, which reset every plot to its default."""
    pg = page
    pg.click(".rail button[aria-label='Runs']")
    pg.locator(".panel:has(> .panel-head > span:text-is('Runs')) .run-row", has_text="input-1").click()
    pg.wait_for_function("(window.trame.state.get('m_chart_0').series || []).length > 0")
    chart = "window.trame.state.get('m_chart_0')"
    before = pg.evaluate(f"[{chart}.ylabel, {chart}.series.map(s => s.label)]")
    picker = pg.locator("select[aria-label='plot 1']")
    target = next(v for v in picker.locator("option").evaluate_all("os => os.map(o => o.value)") if v.endswith("/dt"))
    picker.select_option(target)
    pg.wait_for_function(f"{chart}.view.endsWith('|{target}')")
    after = pg.evaluate(f"[{chart}.ylabel, {chart}.series.map(s => s.label)]")
    assert after != before and after[0].startswith("dt"), (before, after)
    eventually(lambda: pg.locator("#m_chart_0 .u-legend, #m_chart_0").inner_text(), lambda t: "dt" in t)
    assert picker.input_value() == target


def test_a_small_window_keeps_run_figures_and_plot_heads_whole(page: Any) -> None:
    """At 1100x700 the run's figures ran into each other ("0.0083240.454"), the plot titles repeated their own
    select as "Pr...", the plot heads clipped the SVG export button, Stop fell off the run head, and the plots
    were drawn over the status bar."""
    pg = page
    pg.set_viewport_size({"width": 1100, "height": 700})
    try:
        pg.click(".rail button[aria-label='Runs']")
        pg.locator(".panel:has(> .panel-head > span:text-is('Runs')) .run-row", has_text="input-1").click()
        pg.wait_for_selector(".runs-plots select")
        pg.wait_for_timeout(300)
        spilled = [".stat .v", ".runs-plots .plot-head select", ".runs-plots .export .tb"]
        assert pg.evaluate(SPILLED, spilled) == []
        heads = pg.locator(".runs-plots .plot-head:visible")
        for i in range(heads.count()):
            sync_api.expect(heads.nth(i).get_by_role("button", name="SVG")).to_be_visible()
            box, head = heads.nth(i).get_by_role("button", name="SVG").bounding_box(), heads.nth(i).bounding_box()
            assert box["x"] + box["width"] <= head["x"] + head["width"] + 1
        center = pg.locator(".runs-center").bounding_box()
        for button in pg.locator(".run-head .btn").all():
            b = button.bounding_box()
            assert b["x"] + b["width"] <= center["x"] + center["width"], button.get_attribute("title")
        status = pg.locator(".statusbar").bounding_box()
        for chart in pg.locator(".runs-plots .plot").all():
            c = chart.bounding_box()
            assert c["y"] + c["height"] <= status["y"] + 1
    finally:
        pg.set_viewport_size({"width": 1440, "height": 900})


NATIVE_SELECTS = """() => [...document.querySelectorAll('select')].filter((s) => s.offsetParent)
    .filter((s) => getComputedStyle(s).appearance !== 'none').map((s) => s.getAttribute('aria-label') || s.className)"""


@pytest.mark.parametrize("title", ["Settings", "Runs", "Case setup"])
def test_every_select_draws_the_apps_chevron(page: Any, title: str) -> None:
    """WebKit draws a native select as a stepper glyph, unlike the rest of the app's selects."""
    pg = page
    pg.click(f".rail button[aria-label='{title}']")
    if title == "Runs":
        pg.locator(".run-row:visible", has_text="input-1").first.click()
        pg.wait_for_selector(".runs-plots select")
    if title == "Case setup":
        pg.locator(".tree-row", has_text="Boundaries").first.click()
        pg.wait_for_selector(".toolbar")
    assert pg.evaluate(NATIVE_SELECTS) == []
