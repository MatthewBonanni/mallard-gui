"""The results page's M5 controls in a real browser: geometry mode drawn by vtk.js and back to images, the
probe's spectrum, saved views written to the project file, and a surface extract that becomes a series."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from .test_ui import app_url, open_box_run, page, pw, root  # noqa: F401  (fixtures)

pytestmark = pytest.mark.ui


def results(pg: object) -> None:
    pg.click(".rail button[aria-label='Results']")  # type: ignore[attr-defined]
    open_box_run(pg)


def test_geometry_mode_draws_in_the_browser_and_back(page: object) -> None:  # noqa: F811
    pg = page
    results(pg)
    pg.get_by_role("button", name="geometry", exact=True).click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".results-view canvas", state="visible", timeout=20_000)  # type: ignore[attr-defined]
    pg.wait_for_selector("text=/triangles drawn in the browser/")  # type: ignore[attr-defined]
    assert pg.locator(".results-view img.frame").count() == 0  # type: ignore[attr-defined]
    pg.get_by_role("button", name="images", exact=True).click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".results-view img.frame", timeout=20_000)  # type: ignore[attr-defined]
    pg.get_by_role("button", name="auto", exact=True).click()  # type: ignore[attr-defined]


def test_a_view_preset_turns_the_browser_drawn_view(page: object) -> None:  # noqa: F811
    """vtk.js takes a scene's camera once: in geometry mode a preset moved the viewer's camera and the view
    drawn in the browser stayed as it was. The view looks along the preset's axis and its pixels change."""
    pg: Any = page
    results(pg)
    pg.get_by_role("button", name="geometry", exact=True).click()
    pg.wait_for_selector("text=/triangles drawn in the browser/")
    look = """(() => { const c = trame.refs.geoview.getCamera();
        const d = c.focalPoint.map((f, i) => f - c.position[i]), n = Math.hypot(...d); return d.map(v => v / n); })()"""
    box = pg.locator(".results-view").first.bounding_box()
    before = pg.screenshot(clip=box)
    for name, direction in (("+X", [1, 0, 0]), ("+Z", [0, 0, 1])):  # the way the view looks
        pg.locator(".toolbar button:visible", has_text=name).first.click()
        pg.wait_for_function(f"{look}.every((v, i) => Math.abs(v - {direction}[i]) < 1e-6)")
        pg.wait_for_timeout(500)
        now = pg.screenshot(clip=box)
        assert now != before, f"{name} left the view as it was"
        before = now
    pg.get_by_role("button", name="auto", exact=True).click()


def test_probe_spectrum_and_saved_view(page: object, root: Path) -> None:  # noqa: F811
    pg = page
    results(pg)
    # The client holds edited state back while it waits on a call (as under load): the click carries the point.
    pg.evaluate("trame.state.flush = async () => {}")  # type: ignore[attr-defined]
    for axis, v in zip("xyz", ("0.5", "0.5", "0.5"), strict=True):
        pg.fill(f"input[aria-label='point {axis}']", v)  # type: ignore[attr-defined]
        pg.press(f"input[aria-label='point {axis}']", "Tab")  # type: ignore[attr-defined]
    pg.locator(".panel.right .actions .btn.primary", has_text="Probe").filter(visible=True).click()  # type: ignore[attr-defined]
    pg.evaluate("delete trame.state.flush")  # type: ignore[attr-defined]
    pg.wait_for_selector(".results-plots .m-plot polyline", timeout=20_000)  # type: ignore[attr-defined]
    pg.get_by_role("button", name="spectrum").click()  # type: ignore[attr-defined]
    # three snapshots: too few for a spectrum, and it says so
    pg.wait_for_selector("text=/spectrum: 3 samples/")  # type: ignore[attr-defined]
    pg.get_by_role("button", name="time", exact=True).click()  # type: ignore[attr-defined]
    pg.fill("input[aria-label='view name']", "mid box")  # type: ignore[attr-defined]
    pg.get_by_role("button", name="Save", exact=True).click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".tree-row >> text=mid box")  # type: ignore[attr-defined]
    data = tomllib.loads((root / "case" / "mallard-project.toml").read_text())
    view = data["views"]["mid box"]
    assert view["run"] == "box-1" and view["probe"] == [0.5, 0.5, 0.5] and "position" in view["camera"]


def test_surface_extract_becomes_a_derived_series(page: object, root: Path) -> None:  # noqa: F811
    pg = page
    results(pg)
    pg.get_by_role("button", name="Surfaces", exact=True).click()  # type: ignore[attr-defined]
    pg.wait_for_selector(".tree-row >> text=surface_20", timeout=20_000)  # type: ignore[attr-defined]
    assert (root / "case" / "runs" / "box-1" / "extract" / "surface_20.vtp").is_file()


def test_image_zoom_and_pan_preview_until_a_frame_shows_them(page: object) -> None:  # noqa: F811
    pg: Any = page
    results(pg)
    pg.get_by_role("button", name="images", exact=True).click()
    img = pg.wait_for_selector(".results-view img.frame", timeout=20_000)
    box = img.bounding_box()
    pg.mouse.move(box["x"] + 0.75 * box["width"], box["y"] + 0.3 * box["height"])
    transform = "document.querySelector('.results-view img.frame').style.transform"
    before = pg.evaluate("trame.state.get('v_frame')")
    # The server held back: what the page shows before any frame answers is the preview alone.
    pg.evaluate("window._trigger = trame.trigger; trame.trigger = () => Promise.resolve()")
    pg.mouse.wheel(0, -200)
    pg.wait_for_function(f"{transform}.includes('scale(1.')")
    pg.evaluate("trame.trigger = window._trigger")
    pg.mouse.wheel(0, -100)
    # Drawn through the last move, the frame needs no preview.
    pg.wait_for_function(
        f"{transform} === '' && trame.state.get('v_frame_seq') === window.mallardZoom.preview().seq", timeout=20_000
    )
    assert pg.evaluate("trame.state.get('v_frame')") != before  # the server's camera moved too
    x, y = box["x"] + 0.5 * box["width"], box["y"] + 0.5 * box["height"]
    pg.keyboard.down("Shift")
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.evaluate("trame.trigger = () => Promise.resolve()")
    pg.mouse.move(x + 40, y + 10, steps=4)
    pg.wait_for_function(f"{transform}.startsWith('translate(40px, 10px)')")
    pg.evaluate("trame.trigger = window._trigger")
    pg.mouse.up()
    pg.keyboard.up("Shift")
    pg.get_by_role("button", name="auto", exact=True).click()


def test_the_fields_statistics_probe_at_their_extremes_and_follow_the_slice(page: Any) -> None:  # noqa: F811
    pg = page
    results(pg)
    pg.wait_for_function("trame.state.get('v_info').field === 'P' && trame.state.get('v_info').index === 2")
    rows = pg.locator(".st-row")
    pg.wait_for_selector(".st-row .st-num")
    labels, values = rows.locator(".plabel").all_inner_texts(), rows.locator(".st-num").all_inner_texts()
    figures = dict(zip(labels, values, strict=True))
    assert figures["mean"] == "2.5 Pa" and figures["integral"] == "2.5"  # P = t + x on the unit box, t = 2
    assert pg.locator(".st-plot svg rect").count() == 32
    pg.locator("button[aria-label='Probe at the max']").click()
    pg.wait_for_function("trame.state.get('v_point')[0] === '0.9167'")
    pg.wait_for_function("(trame.state.get('v_probe_text') || '').startsWith('P at (0.9167')", timeout=20_000)
    pg.locator(".toolbar button:visible", has_text="Slice").first.click()
    pg.locator(".st-where button", has_text="Slice").click()
    pg.wait_for_selector(".st-summary:text-matches('area')")
    pg.locator(".toolbar button:visible", has_text="Slice").first.click()
    pg.wait_for_selector(".st-where", state="detached")


def test_compare_shows_a_second_field_beside_the_view(page: Any) -> None:  # noqa: F811
    pg = page
    results(pg)
    pg.wait_for_function("trame.state.get('v_info').field === 'P'")
    pg.locator(".toolbar button:visible", has_text="Compare").click()
    pg.wait_for_selector(".split-view img.frame", timeout=20_000)
    pg.wait_for_function("(trame.state.get('v_colorbar2').label || '').startsWith('U (magnitude)')")
    assert pg.locator(".results-view .cb-label").inner_text().startswith("P")
    main, other = pg.locator(".results-view").bounding_box(), pg.locator(".split-view").bounding_box()
    assert abs(main["width"] - other["width"]) <= 2 and other["x"] >= main["x"] + main["width"]
    pg.locator("select[aria-label='second component']").select_option("y")
    pg.wait_for_function("(trame.state.get('v_colorbar2').label || '').startsWith('U_Y')")
    pg.locator(".toolbar button:visible", has_text="Compare").click()
    pg.wait_for_selector(".split-view", state="detached")


def test_cell_values_or_interpolated_redraws_and_is_kept_with_a_saved_view(page: Any, root: Path) -> None:  # noqa: F811
    pg = page
    results(pg)
    pg.get_by_role("button", name="images", exact=True).click()
    frame = ".results-view img.frame"
    pg.wait_for_selector(f"{frame}[src^='data:image/jpeg']", timeout=20_000)
    cells = pg.get_by_role("button", name="Cell values", exact=True)
    linear = pg.get_by_role("button", name="Interpolated", exact=True)
    assert pg.locator(".prow:has(.interp) .plabel").inner_text() == "rendering"
    assert "blended linearly" in (linear.get_attribute("title") or "")
    assert cells.get_attribute("aria-pressed") == "true"
    assert "statistics always use the cell values" in pg.locator(".prow:has(.interp)").get_attribute("title")
    pg.wait_for_timeout(500)
    flat = pg.locator(frame).get_attribute("src")
    linear.click()
    pg.wait_for_function("document.querySelector(\".interp .seg.on\").innerText === 'Interpolated'")
    pg.wait_for_function(f"document.querySelector('{frame}').src !== {flat!r}", timeout=20_000)
    pg.fill("input[aria-label='view name']", "smooth")
    pg.get_by_role("button", name="Save", exact=True).click()
    pg.wait_for_selector(".tree-row >> text=smooth")
    assert (
        tomllib.loads((root / "case" / "mallard-project.toml").read_text())["views"]["smooth"]["interpolation"]
        == "linear"
    )
    cells.click()
    pg.wait_for_function("document.querySelector(\".interp .seg.on\").innerText === 'Cell values'")
