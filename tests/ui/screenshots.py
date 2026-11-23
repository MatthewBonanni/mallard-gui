"""Screenshots of every page, panel, dialog and state, at 2880×1800 (1440×900 at device pixel ratio 2), in both
themes, in WebKit (the desktop app's engine) and Chromium, with crops of the chrome:

    python -m tests.ui.screenshots OUT_DIR [--prefix polish-before-] [--engines webkit,chromium]

Runs without a mallard build, on the browser tests' world (`tests/ui/world.py`)."""

from __future__ import annotations

import argparse
import contextlib
import re
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from .world import FAKE_NOTIFICATION, make_world, serve

CROPS = {
    "topbar": ".topbar",
    "rail": ".rail",
    "toolbar": ".toolbar:visible",
    "statusbar": ".statusbar",
    "dialog": ".modal:visible, .su-dialog:visible, [role=alertdialog]:visible",
    "palette": ".search-results",
    "notice": ".doc-notice:visible",
    "props": ".panel.right:visible",
}


def rail(pg: Page, title: str) -> None:
    pg.click(f".rail button[aria-label='{title}']")
    pg.wait_for_timeout(600)


def case_view(pg: Page) -> list[str]:
    rail(pg, "Case setup")
    pg.get_by_role("tab", name="Case view").click()
    return ["topbar", "rail", "toolbar", "statusbar", "props"]


def case_toml(pg: Page) -> list[str]:
    rail(pg, "Case setup")
    pg.get_by_role("tab", name="input.toml").click()
    return []


def case_report(pg: Page) -> list[str]:
    rail(pg, "Case setup")
    pg.get_by_role("tab", name="Check report").click()
    pg.wait_for_selector(".su-sec >> text=TIME STEP")
    return []


def case_located(pg: Page) -> list[str]:
    case_report(pg)
    pg.locator(".su-kv", has_text="first-cell y+, largest").locator(".locate").click()
    pg.wait_for_selector(".su-banner:visible")
    pg.wait_for_timeout(800)
    return ["toolbar"]


def case_analysis(pg: Page) -> list[str]:
    rail(pg, "Case setup")
    pg.get_by_role("tab", name="Case view").click()
    if not pg.locator(".ana-panel").count():
        pg.click("button.ana-toggle")
    pg.wait_for_selector(".ana-panel")
    pg.wait_for_timeout(1500)
    shot = [".ana-panel"]
    pg.click("button.ana-toggle")
    return shot


def launch(pg: Page) -> list[str]:
    rail(pg, "Case setup")
    pg.click(".runbtn")
    pg.wait_for_selector(".su-dialog input[aria-label='ranks']")
    pg.wait_for_selector("#su_chart_mem svg polyline")
    pg.wait_for_timeout(800)
    return ["dialog"]


def palette(pg: Page) -> list[str]:
    rail(pg, "Case setup")
    pg.keyboard.press("Meta+k")
    pg.keyboard.type("farf")
    pg.wait_for_selector(".search-results div.on")
    return ["palette", "topbar"]


def runs_finished(pg: Page) -> list[str]:
    rail(pg, "Runs")
    pg.locator(".run-row:visible", has_text="input-1").click()
    pg.wait_for_timeout(1000)
    return [".run-head", ".health"]


def runs_control(pg: Page) -> list[str]:
    rail(pg, "Runs")
    pg.get_by_role("button", name="Control", exact=True).click()
    return []


def data(pg: Page) -> list[str]:
    rail(pg, "Data")
    pg.locator(".run-row:visible", has_text="box-1").click()
    pg.wait_for_selector(".relocate")
    return [".relocate"]


def results(pg: Page) -> list[str]:
    rail(pg, "Results")
    pg.locator(".panel:has(> .panel-head > span:text-is('Results')) .run-row", has_text="box-1").click()
    pg.wait_for_selector(
        ".results-view img.frame[src^='data:image/jpeg'], .results-view canvas:visible", timeout=30_000
    )
    pg.wait_for_timeout(1500)
    return ["toolbar", ".timebar"]


def compare(pg: Page) -> list[str]:
    rail(pg, "Compare runs")
    pg.select_option("select[aria-label='run A']", "input-1")
    pg.select_option("select[aria-label='run B']", "box-1")
    pg.wait_for_selector(".cmp-table tr.removed")
    return [".cmp-bar"]


def remotes_ssh(pg: Page) -> list[str]:
    rail(pg, "Remotes")
    pg.locator(".rm-row", has_text="hpc").click()
    pg.get_by_role("button", name="Test connection").click()
    pg.wait_for_selector("text=gpu (gpu:a100:4)", timeout=20_000)
    return []


def remotes_k8s(pg: Page) -> list[str]:
    rail(pg, "Remotes")
    pg.locator(".rm-row", has_text="lab").click()
    pg.wait_for_timeout(800)
    return []


def unsaved(pg: Page) -> list[str]:
    rail(pg, "Case setup")
    pg.locator(".tree-row", has_text="Run").first.click()
    field = pg.locator("input[aria-label='run.t_stop']")
    field.fill("170")
    field.press("Enter")
    pg.wait_for_function("document.title.startsWith('•')")
    pg.keyboard.press("Meta+k")
    pg.keyboard.type("close project")
    pg.wait_for_selector(".search-results div.on >> text=Close Project")
    pg.keyboard.press("Enter")
    pg.get_by_role("alertdialog").wait_for()
    return ["dialog", "topbar"]


def unsaved_done(pg: Page) -> None:
    pg.get_by_role("alertdialog").get_by_role("button", name="Cancel").click()
    pg.keyboard.press("Meta+z")


def settings(pg: Page) -> list[str]:
    if not pg.locator(".rail button[aria-label='Settings']:not([disabled])").count():
        raise LookupError("no Settings page")
    rail(pg, "Settings")
    return [".settings"]


def external_edit(pg: Page, project: Path) -> list[str]:
    rail(pg, "Case setup")
    pg.locator(".tree-row", has_text="Run").first.click()
    field = pg.locator("input[aria-label='run.t_stop']")
    field.fill("175")
    field.press("Enter")
    pg.wait_for_function("document.title.startsWith('•')")
    path = project / "input.toml"
    path.write_text(re.sub(r"(?m)^t_stop = .*$", "t_stop = 180.0", path.read_text()))
    pg.wait_for_selector(".ext-notice", timeout=10_000)
    return ["notice"]


def external_edit_done(pg: Page, project: Path) -> None:
    pg.get_by_role("button", name="Reload").click()
    path = project / "input.toml"
    path.write_text(re.sub(r"(?m)^t_stop = .*$", "t_stop = 160.0", path.read_text()))
    pg.wait_for_timeout(2500)


State = Callable[[Page], list[str]]
STATES: dict[str, State] = {
    "case-view": case_view,
    "case-toml": case_toml,
    "case-report": case_report,
    "case-located": case_located,
    "case-analysis": case_analysis,
    "launch-dialog": launch,
    "palette": palette,
    "runs-finished": runs_finished,
    "runs-control": runs_control,
    "data": data,
    "results": results,
    "compare": compare,
    "remotes-ssh": remotes_ssh,
    "remotes-k8s": remotes_k8s,
    "settings": settings,
}


def only_page(title: str) -> Callable[[Page], list[str]]:
    def prepare(pg: Page) -> list[str]:
        rail(pg, title)
        return []

    return prepare


EMPTY_STATES = {
    f"empty-{t.split()[0].lower()}": only_page(t) for t in ("Runs", "Results", "Data", "Analysis", "Compare runs")
}


def set_theme(pg: Page, theme: str) -> None:
    """Through the topbar's button, so the views rendered by the server change theme too."""
    for _ in range(2):
        if pg.evaluate("document.documentElement.dataset.theme") == theme:
            break
        pg.click("button[title='Light or dark theme']")
        pg.wait_for_timeout(800)
    pg.wait_for_timeout(1500)


def tidy(pg: Page) -> None:
    """Close what a state left open."""
    pg.keyboard.press("Escape")
    for selector in (
        ".su-dialog .actions .btn:has-text('Close')",
        "[role=alertdialog] .btn:has-text('Cancel')",
        ".su-banner .btn:has-text('Clear')",
    ):
        button = pg.locator(selector)
        if button.count() and button.first.is_visible():
            with contextlib.suppress(PlaywrightError):  # Escape may have closed it meanwhile
                button.first.click(timeout=2000)
    pg.wait_for_timeout(200)


def shoot(pg: Page, base: str, name: str, crops: list[str]) -> None:
    pg.mouse.move(1, pg.viewport_size["height"] - 1 if pg.viewport_size else 899)
    pg.wait_for_timeout(300)
    pg.screenshot(path=f"{base}{name}.png")
    for crop in crops:
        selector = CROPS.get(crop, crop)
        loc = pg.locator(selector).first
        if loc.count() and loc.is_visible():
            label = crop.strip(".").replace(" ", "-").replace(":", "")
            loc.screenshot(path=f"{base}{name}-crop-{label}.png")


def session(
    engine: str,
    url: str,
    prefix: str,
    states: dict[str, Callable[[Page], list[str]]],
    extra: Callable[[Page, str], None],
    size: tuple[int, int] = (1440, 900),
    console: list[str] | None = None,
) -> None:
    with sync_playwright() as p:
        browser = p.webkit.launch() if engine == "webkit" else p.chromium.launch(channel="chrome")
        pg = browser.new_page(viewport={"width": size[0], "height": size[1]}, device_scale_factor=2)
        if console is not None:
            log = console

            def on_console(m: Any) -> None:
                if m.type in ("error", "warning"):
                    log.append(f"{m.type}: {m.text}")

            pg.on("console", on_console)
            pg.on("pageerror", lambda e: log.append(f"pageerror: {e}"))
        pg.add_init_script(FAKE_NOTIFICATION)
        pg.goto(url)
        pg.wait_for_selector(".tree-row", timeout=30_000)
        pg.wait_for_timeout(2000)
        for theme in ("dark", "light"):
            set_theme(pg, theme)
            base = f"{prefix}{engine}-{theme}-"
            for name, prepare in states.items():
                try:
                    crops = prepare(pg)
                    shoot(pg, base, name, crops)
                except Exception:  # noqa: BLE001  (a missing state is reported, the rest still shot)
                    print(f"{engine} {theme} {name}: failed\n{traceback.format_exc(limit=2)}")
                tidy(pg)
            extra(pg, base)
        browser.close()


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("out", type=Path)
    a.add_argument("--prefix", default="")
    a.add_argument("--engines", default="webkit,chromium")
    a.add_argument("--only", default="", help="comma-separated state names")
    a.add_argument("--size", default="1440x900", help="viewport in CSS pixels, WxH")
    args = a.parse_args()
    size = tuple(int(v) for v in args.size.split("x"))
    assert len(size) == 2
    console: list[str] = []
    args.out.mkdir(parents=True, exist_ok=True)
    prefix = f"{args.out}/{args.prefix}"
    only = set(filter(None, args.only.split(",")))

    def pick(d: dict[str, State]) -> dict[str, State]:
        return {k: v for k, v in d.items() if not only or k in only}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "full").mkdir()
        (root / "empty").mkdir()
        project, env = make_world(root / "full")
        empty, empty_env = make_world(root / "empty", runs=False)

        def stateful(pg: Page, base: str) -> None:
            for name, (prepare, done) in {
                "unsaved-prompt": (unsaved, unsaved_done),
                "external-edit": (lambda p: external_edit(p, project), lambda p: external_edit_done(p, project)),
            }.items():
                if only and name not in only:
                    continue
                try:
                    shoot(pg, base, name, prepare(pg))
                    done(pg)
                except Exception:  # noqa: BLE001
                    print(f"{base} {name}: failed\n{traceback.format_exc(limit=2)}")
                tidy(pg)

        def check_errors(pg: Page, base: str) -> None:
            if only and "check-errors" not in only:
                return
            rail(pg, "Case setup")
            pg.wait_for_timeout(1500)
            shoot(pg, base, "check-errors", [".problems"])

        nothing: Callable[[Page, str], None] = lambda pg, base: None  # noqa: E731
        for engine in args.engines.split(","):
            start = time.monotonic()
            with serve(project, env) as url:
                session(engine, url, prefix, pick(STATES), stateful, size, console)
            with serve(empty, empty_env) as url:
                session(engine, url, prefix, pick(EMPTY_STATES), nothing, size, console)
            with serve(empty, empty_env, check="cylinder_check_error.json") as url:
                session(engine, url, prefix, {}, check_errors, size, console)
            print(f"{engine}: {time.monotonic() - start:.0f} s")
        if console:
            (args.out / f"{args.prefix}console.txt").write_text("\n".join(dict.fromkeys(console)) + "\n")


if __name__ == "__main__":
    main()
