"""The Run monitor's plots in Chromium and WebKit, on a run going on (a stand-in for `mallard` appending to its
run log): they follow the latest samples, a zoom pauses following and holds the view while samples arrive, a
fit resumes it; the crosshair reads the curves, log y toggles, and a residual starts on a log axis."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .conftest import FIXTURES
from .test_ui import free_port

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")

CFL = "#m_chart_0"  # the defaults for a run log without CSV files: CFL, forces, dt, health
HEALTH = "#m_chart_3"


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory, browser_name: str) -> Iterator[Path]:
    project = tmp_path_factory.mktemp(f"monitor-{browser_name}") / "case"
    project.mkdir()
    shutil.copy(FIXTURES / "cylinder_input.toml", project / "input.toml")
    shutil.copy(FIXTURES / "cylinder.msh", project / "cylinder.msh")
    yield project
    for run in (project / "runs").glob("*"):  # the stand-in exits on STOP, or after a minute
        (run / "STOP").touch()


@pytest.fixture(scope="module")
def url(project: Path) -> Iterator[str]:
    root = project.parent
    env = {
        **os.environ,
        "MALLARD_GUI_CONFIG_DIR": str(root / "config"),
        "MALLARD_GUI_CACHE_DIR": str(root / "cache"),
        "FAKE_MALLARD_EXE": str(FIXTURES / "fake-mallard-run"),
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
        yield f"http://127.0.0.1:{port}/"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def page(browser_name: str, url: str) -> Iterator[Page]:
    with sync_api.sync_playwright() as p:
        try:
            browser = getattr(p, browser_name).launch(headless=True)
        except sync_api.Error:
            pytest.skip(f"Playwright's {browser_name} is not installed (playwright install {browser_name})")
        pg = browser.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(url)
        pg.wait_for_selector(".tree-row", timeout=30_000)
        yield pg
        browser.close()


def view(pg: Page, chart: str, field: str) -> float:
    return float(pg.evaluate(f"window.mallardChart.view('{chart.lstrip('#')}').{field}"))


def grows(pg: Page, chart: str, than: float) -> None:
    pg.wait_for_function(f"window.mallardChart.view('{chart.lstrip('#')}').x1 > {than}", timeout=15_000)


def screenshots(pg: Page, browser_name: str, state: str) -> None:
    """Into MALLARD_GUI_SCREENSHOTS, when set: the monitor in both themes."""
    out = os.environ.get("MALLARD_GUI_SCREENSHOTS")
    if not out or browser_name != "chromium":
        return
    Path(out).mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        pg.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        pg.wait_for_timeout(200)
        pg.locator(".plots:not(.results-plots)").screenshot(path=str(Path(out) / f"monitor-{state}-{theme}.png"))


def test_live_plots_follow_pause_on_zoom_and_resume_on_fit(page: Page, browser_name: str) -> None:
    pg = page
    pg.click(".runbtn")
    pg.locator(".su-dialog .btn.primary").click()
    pg.wait_for_selector(f"{CFL}[data-follow='on'] polyline", timeout=30_000)
    pg.wait_for_selector(f"{HEALTH} polyline")
    assert pg.evaluate("window.mallardChart.view('m_chart_3').logy")  # a residual: a log axis
    grows(pg, CFL, view(pg, CFL, "x1"))  # following

    box = pg.locator(f"{CFL} svg").bounding_box()
    assert box is not None
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    pg.mouse.move(cx, cy)
    readout = pg.locator(CFL).get_attribute("data-readout")
    assert readout and readout.startswith("t ") and "cfl" in readout
    screenshots(pg, browser_name, "following")

    width = view(pg, CFL, "x1") - view(pg, CFL, "x0")
    pg.mouse.wheel(0, -600)
    pg.wait_for_selector(f"{CFL}[data-follow='paused']")
    zoomed = (view(pg, CFL, "x0"), view(pg, CFL, "x1"))
    assert zoomed[1] - zoomed[0] < width / 2
    pg.wait_for_timeout(2500)  # samples arrive; the zoomed view stays
    assert (view(pg, CFL, "x0"), view(pg, CFL, "x1")) == zoomed
    pg.wait_for_function("window.trame.state.get('m_chart_0').series[0].x.length > 0")
    screenshots(pg, browser_name, "paused")

    pg.locator(f"{CFL} .m-chart-tools button", has_text="log y").click()
    assert pg.evaluate("window.mallardChart.view('m_chart_0').logy")

    pg.dblclick(f"{CFL} svg")
    pg.wait_for_selector(f"{CFL}[data-follow='on']")
    grows(pg, CFL, view(pg, CFL, "x1"))

    pg.get_by_role("button", name="Stop", exact=True).click()
    pg.wait_for_selector(f"{CFL}[data-follow='']", timeout=20_000)  # the run ended: nothing to follow
