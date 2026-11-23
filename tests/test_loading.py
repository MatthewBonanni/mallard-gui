"""The startup screen: the desktop shell's splash (desktop/splash/index.html is assets/loading.html) and the GUI page's
cover until its first frame. The shell's own side (progress file, failure, Retry) is tested in main.rs."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mallard_gui import loading
from mallard_gui.tokens import load_tokens

ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "src" / "mallard_gui" / "assets"
SPLASH = ROOT / "desktop" / "splash"
ORIGIN = "http://splash.test"


def test_the_splash_is_the_guis_own_loading_page() -> None:
    assert (SPLASH / "index.html").resolve() == (ASSETS / "loading.html").resolve()
    assert (SPLASH / "mallard_gui" / "favicon.png").resolve() == (ASSETS / "favicon.png").resolve()


def test_the_screens_colors_are_the_design_tokens() -> None:
    text = (ASSETS / "loading.html").read_text(encoding="utf-8")
    light, dark = re.findall(r"#mallard-loading \{\s*(--ml-bg:.*?;)\s*(?:position|\})", text, re.S)[:2]
    tokens = load_tokens()["color"]
    names = {"bg": "bg", "surface": "surface", "border": "border", "text": "text", "muted": "text-muted",
             "accent": "accent", "danger": "danger"}  # fmt: skip
    for block, theme in ((light, "light"), (dark, "dark")):
        for ours, token in names.items():
            assert f"--ml-{ours}: {tokens[theme][token]};" in block, (theme, ours)
        assert re.search(rf"html \{{[^}}]*background: {tokens[theme]['bg']}", text), theme


def test_the_gui_page_carries_the_cover_at_its_last_stage() -> None:
    index = '<html><head><title>t</title></head><body class="x"><div id="app"></div></body></html>'
    html = loading.page(index, "cyl</script>")
    head, body = html.split("</head>")
    assert 'id="mallard-loading-style"' in head
    assert body.startswith('<body class="x">\n<div id="mallard-loading"')
    assert '{"stage": "render", "project": "cyl\\u003c/script>"}' in body
    assert body.index("mallard-loading") < body.index('id="app"')


def test_cli_reports_its_steps_and_serves_the_covered_page(tmp_path: Path, cylinder_project: Path) -> None:
    mallard = tmp_path / "mallard"
    mallard.write_text("#!/bin/sh\n[ \"$1\" = --version ] && echo 'Mallard v0.11.0' || exit 1\n")
    mallard.chmod(0o755)
    url_file, progress = tmp_path / "url", tmp_path / "progress"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys, mallard_gui.cli as c; sys.exit(c.main())", str(cylinder_project),
         "--mallard", str(mallard), "--no-browser", "--url-file", str(url_file)],
        env={**os.environ, "MALLARD_GUI_CONFIG_DIR": str(tmp_path / "config"), loading.PROGRESS_ENV: str(progress)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        end = time.monotonic() + 60
        while not (url_file.exists() and url_file.read_text()) and time.monotonic() < end and proc.poll() is None:
            time.sleep(0.05)
        assert progress.read_text().split() == ["mallard", "libraries", "case", "interface"]
        url = url_file.read_text(encoding="utf-8")
        with urllib.request.urlopen(url, timeout=10) as reply:
            page = reply.read().decode()
        assert 'id="mallard-loading"' in page and f'"project": "{cylinder_project.name}"' in page
        assert "vue.global.js" in page  # still trame's own page
        base = url.split("?")[0]
        with urllib.request.urlopen(base + "loading.tpl", timeout=10) as reply:
            assert reply.read() == b""  # no trame spinner under the cover
        with urllib.request.urlopen(base + "mallard_gui/style.css", timeout=10) as reply:
            assert reply.headers["Cache-Control"] == "no-cache"  # a reinstalled GUI's assets, not the cached ones
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# The screen in WebKit, the desktop app's engine, as the shell drives it


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.webkit.launch(headless=True)
        except sync_api.Error:
            pytest.skip("Playwright's WebKit is not installed")
        yield b
        b.close()


def open_splash(browser: Any, path: str = "index.html", **options: Any) -> tuple[Any, list[str]]:
    """The bundled splash on its own origin; `asked` lists what its buttons asked the shell for."""
    pg = browser.new_page(**options)
    asked: list[str] = []

    def serve(route: Any) -> None:
        url = route.request.url.split("#")[0].split("?")[0]
        if "/__mallard/" in url:
            asked.append(url.rsplit("/", 1)[1])
            route.abort()
        else:
            route.fulfill(path=str(SPLASH / url.removeprefix(f"{ORIGIN}/")))

    pg.route(f"{ORIGIN}/**", serve)
    pg.goto(f"{ORIGIN}/{path}")
    return pg, asked


def rows(pg: Any) -> list[tuple[str, str, str]]:
    return [
        tuple(r)
        for r in pg.eval_on_selector_all(
            ".ml-stages li",
            "ls => ls.map(l => [l.dataset.state, l.querySelector('.ml-detail').textContent,"
            " l.querySelector('.ml-time').textContent])",
        )
    ]


LOG = "/tmp/mallard-desktop-1/server.log"


@pytest.mark.ui
def test_stage_updates_reach_the_screen(browser: Any) -> None:
    pg, _ = open_splash(browser)
    pg.evaluate(
        "s => window.mallardSplash.update(s)",
        {"mode": "loading", "project": "cyl", "path": "/c/cyl", "stage": "server", "detail": "Loading VTK and trame",
         "log": LOG},
    )  # fmt: skip
    assert pg.text_content(".ml-title") == "cyl"
    assert rows(pg)[0] == ("active", "Loading VTK and trame", "")
    pg.evaluate("s => window.mallardSplash.update(s)", {"mode": "loading", "project": "cyl", "stage": "interface",
                                                         "times": [1834, 190], "log": LOG})  # fmt: skip
    assert rows(pg) == [("done", "", "1.8 s"), ("done", "", "0.19 s"), ("active", "", ""), ("pending", "", "")]
    assert pg.eval_on_selector(".ml-progress", "p => [p.value, p.max, p.hidden]") == [2, 4, False]
    assert pg.is_hidden(".ml-failure") and pg.is_hidden(".ml-slow")
    pg.close()


@pytest.mark.ui
def test_a_failure_shows_why_the_log_and_retry(browser: Any) -> None:
    pg, asked = open_splash(browser)
    tail = "Traceback (most recent call last):\nmallard-gui: input.toml: line 12: expected '='"
    pg.evaluate(
        "s => window.mallardSplash.update(s)",
        {"mode": "failed", "project": "cyl", "stage": "case", "times": [1834],
         "reason": "mallard-gui: input.toml: line 12: expected '='", "log": LOG, "tail": tail},
    )  # fmt: skip
    assert pg.text_content(".ml-title") == "Mallard couldn’t start"
    assert rows(pg)[:2] == [("done", "", "1.8 s"), ("failed", "", "")]
    assert pg.text_content(".ml-reason") == "mallard-gui: input.toml: line 12: expected '='"
    assert pg.text_content(".ml-log code") == LOG
    assert pg.text_content(".ml-failure pre") == tail
    assert pg.is_hidden(".ml-progress")
    pg.click(".ml-failure [data-action=log]")
    pg.click("text=Retry")
    pg.keyboard.press("Enter")  # Retry is the default button
    end = time.monotonic() + 5
    while len(asked) < 3 and time.monotonic() < end:
        pg.wait_for_timeout(20)
    assert asked == ["log", "retry", "retry"]
    # Without a log (the server never started), only Retry.
    pg.evaluate("s => window.mallardSplash.update(s)", {"mode": "failed", "reason": "mallard-gui was not found"})
    assert pg.is_hidden(".ml-failure [data-action=log]") and pg.is_hidden(".ml-log")
    assert pg.is_visible("text=Retry")
    pg.close()


@pytest.mark.ui
def test_no_project_shows_what_to_do(browser: Any) -> None:
    pg, _ = open_splash(browser)
    message = "File > Open… or Open Recent opens one."
    pg.evaluate("s => window.mallardSplash.update(s)", {"mode": "idle", "message": message})
    assert pg.text_content(".ml-title") == "No project open"
    assert pg.is_visible("text=File > Open… or Open Recent opens one.")
    assert pg.is_hidden(".ml-stages") and pg.is_hidden(".ml-progress")
    pg.close()


@pytest.mark.ui
def test_motion_stops_when_the_system_reduces_it(browser: Any) -> None:
    style = (
        "[document.querySelector('li[data-state=active] .ml-active'), document.querySelector('.ml-mark')]"
        ".map((e, i) => i ? getComputedStyle(e, '::after').display : getComputedStyle(e).animationName)"
    )
    pg, _ = open_splash(browser)
    assert pg.evaluate(style) == ["ml-pulse", "block"]
    pg.close()
    pg, _ = open_splash(browser, reduced_motion="reduce")
    assert pg.evaluate(style) == ["none", "none"]
    pg.close()


@pytest.mark.ui
def test_the_gui_cover_shows_the_shells_times_then_fades_out(browser: Any) -> None:
    pg, _ = open_splash(browser, path="index.html#startup=1834,190,410")
    assert pg.evaluate("location.hash") == ""  # dropped before trame reads the URL
    pg.evaluate("s => window.mallardSplash.update(s)", {"stage": "render", "project": "cyl",
                                                         "times": [1834, 190, 410]})  # fmt: skip
    assert [r[0] for r in rows(pg)] == ["done", "done", "done", "active"]
    pg.evaluate("window.mallardSplash.done()")
    pg.wait_for_selector("#mallard-loading", state="detached", timeout=5000)
    assert pg.evaluate("document.getElementById('mallard-loading-style')") is None
    pg.close()
