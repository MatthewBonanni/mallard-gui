"""The Analysis page in Chromium and WebKit: histories overlaid and zoomed with a crosshair readout and an
export of every sample, a probe's spectrum with its Strouhal number and convergence, force coefficients with
reference quantities in units, and a wall distribution and a line extracted by (a stand-in for) `mallard post
extract`, drawn with units picked on the axes."""

from __future__ import annotations

import json
import math
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

from mallard_gui.project import Project, RunRecord

from .conftest import FIXTURES
from .test_ui import free_port

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")

F = 0.033  # the probe's frequency: St = F * 1 m / 0.2 m/s = 0.165
DT = 0.1
N = 2000
INPUT = """\
[physics]
type = "navier_stokes"

[[boundaries]]
name = "cylinder"
type = "wall_adiabatic"

[[boundaries]]
name = "farfield"
type = "farfield"

[output]
reference = {rho = 1.2, u = [0.2, 0.0], p = 1.0}

[[probes]]
name = "wake_point"
point = [2.5, 0.0]
file = "./solut/probe_wake_point.csv"

[[forces]]
zone = "cylinder"
file = "./solut/forces_cylinder.csv"
"""


def make_signal_run(run: Path) -> None:
    (run / "solut").mkdir(parents=True)
    (run / "input.toml").write_text(INPUT, encoding="utf-8")
    q = 0.5 * 1.2 * 0.2**2
    with (run / "solut" / "probe_wake_point.csv").open("w") as f:
        f.write("step,t,point,x,y,U_Y,P\n# units: 1,s,1,m,m,m s-1,Pa\n")
        for i in range(N):
            t = DT * i
            f.write(f"{i},{t!r},0,2.5,0,{0.3 * math.sin(2 * math.pi * F * t)!r},{1 + 0.01 * math.cos(t)!r}\n")
    with (run / "solut" / "forces_cylinder.csv").open("w") as f:
        f.write("step,t,Fx_pressure,Fy_pressure,Fx_viscous,Fy_viscous\n# units: 1,s,N,N,N,N\n")
        for i in range(N):
            t = DT * i
            fy = 0.33 * q * math.sin(2 * math.pi * F * t)
            f.write(f"{i},{t!r},{q * 1.0!r},{fy!r},{q * 0.3!r},0.0\n")
    log = [
        {
            "type": "start",
            "schema": "mallard-run-log",
            "version": 2,
            "run_id": "sig",
            "units": {"t": "s", "dt": "s"},
            "provenance": {"build": {"dim": 2}, "input": {"text": INPUT, "resolved": INPUT}},
        }
    ]
    log += [{"type": "progress", "step": i, "t": float(i), "dt": 0.1, "cfl": 0.8 + 0.01 * i} for i in range(0, 200, 10)]
    log.append({"type": "output", "kind": "restart", "path": "./solut/restart_000100.restart", "step": 100, "t": 20.0})
    log.append({"type": "end", "outcome": "finished", "step": N, "t": DT * N})
    (run / "mallard.jsonl").write_text("".join(json.dumps(r) + "\n" for r in log), encoding="utf-8")


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def browser_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory, browser_name: str) -> Path:
    root = tmp_path_factory.mktemp(f"analysis-{browser_name}")  # a server per browser: it keeps the page state
    project = root / "case"
    project.mkdir()
    shutil.copy(FIXTURES / "cylinder_input.toml", project / "input.toml")
    shutil.copy(FIXTURES / "cylinder.msh", project / "cylinder.msh")
    make_signal_run(project / "runs" / "sig-1")
    Project.open(project).save_run(
        RunRecord("sig-1", home={"remote": "local", "directory": "runs/sig-1"}, outcome="finished")
    )
    return project


@pytest.fixture(scope="module")
def url(project: Path) -> Iterator[str]:
    root = project.parent
    stub = root / "paraview"
    stub.write_text('#!/bin/sh\necho "$@" >> "$0.calls"\n')  # never the user's ParaView
    stub.chmod(0o755)
    env = {
        **os.environ,
        "MALLARD_GUI_CONFIG_DIR": str(root / "config"),
        "MALLARD_GUI_CACHE_DIR": str(root / "cache"),
        "MALLARD_GUI_PARAVIEW": str(stub),
        "FAKE_MALLARD_EXE": str(FIXTURES / "fake-mallard-post"),
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
        pg.wait_for_selector(".rail button", timeout=30_000)
        pg.click(".rail button[aria-label='Analysis']")
        pg.wait_for_selector(".a-sig", timeout=20_000)
        yield pg
        browser.close()


def tab(pg: Page, name: str) -> None:
    pg.locator(".center .tabs button", has_text=name).click()


def rows(pg: Page) -> dict[str, str]:
    """The right panel's label/value rows."""
    got: dict[str, str] = pg.evaluate(
        "Object.fromEntries([...document.querySelectorAll('.panel.right .prow')].map(r => "
        "[r.querySelector('.plabel')?.innerText.trim(), r.querySelector('.pval')?.innerText.trim()]))"
    )
    return got


def test_histories_overlay_zoom_readout_and_export(page: Page, project: Path) -> None:
    pg = page
    tab(pg, "Histories")
    chart = "#a_chart_hist"
    pg.wait_for_selector(f"{chart} polyline", timeout=20_000)
    pg.locator(".a-sig", has_text="Fy_pressure").locator("input").check()
    pg.wait_for_function(f"document.querySelectorAll('{chart} polyline').length === 2")
    box = pg.locator(f"{chart} svg").bounding_box()
    assert box is not None
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    pg.mouse.move(cx, cy)
    readout = pg.locator(chart).get_attribute("data-readout")
    assert readout and readout.startswith("t ") and "U_Y" in readout and "Fy_pressure" in readout
    before = pg.evaluate("window.mallardChart.view('a_chart_hist').x1 - window.mallardChart.view('a_chart_hist').x0")
    pg.mouse.wheel(0, -600)
    after = pg.evaluate("window.mallardChart.view('a_chart_hist').x1 - window.mallardChart.view('a_chart_hist').x0")
    assert after < before / 2
    pg.dblclick(f"{chart} svg")
    pg.locator(".plot-head", has_text="signals").get_by_role("button", name="CSV").click()
    out = project / "exports" / "sig-1" / "histories.csv"
    deadline = time.monotonic() + 10
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    lines = out.read_text().splitlines()
    assert lines[1] == "# units: s,m s-1,N" and len(lines) == N + 2


def test_spectrum_strouhal_and_convergence(page: Page) -> None:
    pg = page
    tab(pg, "Signal")
    pg.locator(".a-sig", has_text="U_Y").first.click()
    pg.wait_for_selector("#a_chart_psd polyline", timeout=20_000)
    pg.wait_for_function(
        "[...document.querySelectorAll('.panel.right .prow')].some(r => r.innerText.startsWith('Strouhal'))"
    )
    r = rows(pg)
    assert float(r["Strouhal"]) == pytest.approx(0.165, abs=0.01)
    assert r["converged"] in ("yes", "no") and "±" in r["mean"]
    sync_api.expect(pg.locator("#a_chart_sig polygon")).to_have_count(1)  # the interval band
    pg.fill("input[aria-label='from t']", "100")
    pg.press("input[aria-label='from t']", "Enter")
    pg.wait_for_function(
        "[...document.querySelectorAll('.panel.right .prow')].some(r => r.innerText.includes('100 – 199.9'))"
    )


def test_a_signal_clicked_right_after_its_tab_is_that_tabs(page: Page) -> None:
    """A click on a signal before the page had drawn the tab just chosen (a busy machine: the tab is the
    server's to change) was the old tab's: it overlaid the signal on the histories and the Signal tab stayed
    without a spectrum, which test_spectrum_strouhal_and_convergence waited 20 s for."""
    pg = page
    tab(pg, "Histories")
    pg.wait_for_selector("#a_chart_hist polyline", timeout=20_000)
    pg.wait_for_selector(".a-sig input[type=checkbox]")
    overlaid = pg.evaluate("trame.state.get('a_hist_sel')")
    # In one task: the tab's button, then the signal's control as the page still draws it (a checkbox).
    pg.evaluate(
        """() => { const tabs = [...document.querySelectorAll('.center .tabs button')];
        tabs.find(b => b.innerText.trim() === 'Signal').click();
        [...document.querySelectorAll('.a-sig')].find(e => e.innerText.includes('P')
          && e.title.includes('probe')).querySelector('input').click(); }"""
    )
    pg.wait_for_function("trame.state.get('a_tab') === 'signal'")
    pg.wait_for_selector("#a_chart_psd polyline", timeout=20_000)
    picked = pg.evaluate("trame.state.get('a_sig')")
    assert "#P#" in picked and pg.evaluate("trame.state.get('a_hist_sel')") == overlaid
    sync_api.expect(pg.locator(".a-sig.on")).to_have_count(1)
    sync_api.expect(pg.locator(".a-sig.on")).to_contain_text("P")


def test_force_coefficients_follow_reference_units(page: Page) -> None:
    pg = page
    tab(pg, "Forces")
    pg.wait_for_selector(".a-table td.k", timeout=20_000)

    def cd() -> float:
        return float(pg.locator(".a-table tr", has_text="Cd").first.locator("td").nth(1).inner_text())

    assert cd() == pytest.approx(1.3, rel=1e-3)
    field = "input[aria-label='reference velocity']"
    pg.fill(field, "40 cm/s")
    pg.press(field, "Enter")
    pg.wait_for_function(
        "document.querySelector('.a-table tr:nth-child(2) td:nth-child(2)').innerText.startsWith('0.325')"
    )
    unit = pg.locator("select[aria-label='reference velocity unit']")
    sync_api.expect(pg.locator(field)).to_have_value("40")  # the typed unit went to the unit box
    sync_api.expect(unit).to_have_value("cm/s")
    pg.fill(field, "40 cm/s")  # again: the same value, still split
    pg.press(field, "Enter")
    sync_api.expect(pg.locator(field)).to_have_value("40")
    unit.select_option("m/s")
    sync_api.expect(pg.locator(field)).to_have_value("0.4")  # converted, so the coefficients stay
    assert cd() == pytest.approx(1.3 / 0.4**2 * 0.2**2, rel=1e-3)
    pg.fill(field, "3 kg")
    pg.press(field, "Enter")
    pg.wait_for_selector(".field-error >> text=/not a unit/")
    pg.fill(field, "0.2")
    pg.press(field, "Enter")
    pg.wait_for_function("!document.querySelector('.field-error')")
    pg.wait_for_function(  # a plain number is in the unit box's unit, m/s
        "document.querySelector('.a-table tr:nth-child(2) td:nth-child(2)').innerText.startsWith('1.3')"
    )


def test_wall_and_line_extracted_by_mallard(page: Page, project: Path) -> None:
    pg = page
    tab(pg, "Lines and walls")
    pg.get_by_role("button", name="Extract").click()
    pg.wait_for_selector("#a_chart_line polyline", timeout=20_000)
    written = (project / "runs" / "sig-1" / "extract" / "cylinder_100.toml").read_text()
    assert 'boundary = "cylinder"' in written  # the wall's boundary probe, added to the run's input
    assert "CP (nd)" in pg.locator("#a_chart_line svg").inner_html()
    pg.select_option("select[aria-label='profile field']", "P")
    pg.wait_for_selector("select[aria-label='field unit']")
    pg.select_option("select[aria-label='field unit']", "kPa")
    pg.select_option("select[aria-label='abscissa unit']", "mm")
    pg.wait_for_function("document.querySelector('#a_chart_line svg').innerHTML.includes('P (kPa)')")
    pg.wait_for_function("document.querySelector('#a_chart_line svg').innerHTML.includes('s (mm)')")
    pg.locator(".panel.right .tb", has_text="line").click()
    for which, (x, y) in (("from", ("0.5", "0")), ("to", ("10", "0"))):
        for axis, v in (("x", x), ("y", y)):
            sel = f"input[aria-label='line {which} {axis}']"
            pg.fill(sel, v)
            pg.press(sel, "Tab")
    pg.get_by_role("button", name="Extract").click()
    pg.wait_for_function("document.querySelector('#a_chart_line svg').innerHTML.includes('distance')")
