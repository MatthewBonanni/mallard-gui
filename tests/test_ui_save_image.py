"""The camera button of the case view and of the results view (images and browser-drawn) saves the view as a PNG
at the path the save dialog gives: of the pixels the view is drawn with, in the theme shown, with the picture
the view shows. The button that frames a zone has another icon."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from playwright import sync_api

from mallard_gui.ui.icons import ICONS
from mallard_gui.view.scene import THEMES

from .test_ui_view_camera import drag, open_run
from .test_ui_view_camera import pg as pg
from .test_ui_view_camera import world_url as world_url

pytestmark = pytest.mark.ui

# The desktop shell's save dialog, answering with the path the test set.
SHELL = "window.mallardShell = {choosePath: (req) => { window.__asked = req; return Promise.resolve(window.__path); }}"


@pytest.fixture
def shell(pg: Any) -> Iterator[None]:
    pg.evaluate(SHELL)
    yield
    pg.evaluate("delete window.mallardShell")


def themed(pg: Any, theme: str) -> None:
    if pg.evaluate("trame.state.get('theme')") != theme:
        pg.locator("button[title='Light or dark theme']").click()
        pg.wait_for_function(f"trame.state.get('theme') === '{theme}'")
        pg.wait_for_timeout(600)


def saved(pg: Any, path: Path, view: str) -> np.ndarray[Any, Any]:
    """Click the view's camera button; the PNG written, as (H, W, 3)."""
    path.unlink(missing_ok=True)
    pg.evaluate("p => { window.__path = p; window.__asked = null; }", str(path))
    button = pg.locator(".toolbar button[title='Save the view as a PNG image']:visible")
    sync_api.expect(button).to_have_count(1)
    button.click()
    pg.wait_for_function("window.__asked !== null")
    asked = pg.evaluate("window.__asked")
    assert asked["mode"] == "save" and asked["name"].endswith(".png")
    assert asked["filters"] == [{"name": "PNG image", "extensions": ["png"]}]
    for _ in range(100):
        if path.exists() and path.stat().st_size:
            break
        pg.wait_for_timeout(100)
    image = Image.open(path)
    assert image.format == "PNG"
    box = pg.locator(view).first.bounding_box()
    assert image.size == (round(2 * box["width"]), round(2 * box["height"]))  # the view's pixels, at ratio 2
    return np.asarray(image.convert("RGB")).astype(float)


def background(theme: str) -> np.ndarray[Any, Any]:
    return 255 * np.array(THEMES[theme]["background"])


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("mode", ["images", "geometry"])
def test_the_camera_button_saves_the_results_view(pg: Any, shell: None, tmp_path: Path, mode: str, theme: str) -> None:
    open_run(pg, "box-1", mode)
    themed(pg, theme)
    drag(pg, 90, 50)  # an orbit: the file is of the view as it is, not as it opened
    pg.wait_for_timeout(700)
    shown = pg.locator(".results-view").first.bounding_box()
    page_shot = pg.screenshot(clip=shown)
    image = saved(pg, tmp_path / "view.png", ".results-view")
    assert np.abs(image[3, 3] - background(theme)).sum() < 12  # the theme's background at its corner
    drawn = np.abs(image - background(theme)).sum(axis=2) > 40
    page_pixels = np.asarray(Image.open(io.BytesIO(page_shot)).convert("RGB")).astype(float)
    on_page = np.abs(page_pixels - page_pixels[3, 3]).sum(axis=2) > 40
    h, w = drawn.shape
    # The same picture as the page's: the box drawn over the same middle of the view (the page lays its
    # legend, colorbar and readout over the corners, the file its time and colorbar).
    mid = (slice(h // 4, 3 * h // 4), slice(w // 4, 3 * w // 4))
    assert (drawn[mid] == on_page[mid]).mean() > 0.97
    assert 0.1 < drawn[mid].mean() < 1.0
    themed(pg, "light")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_camera_button_saves_the_case_view(pg: Any, shell: None, tmp_path: Path, theme: str) -> None:
    pg.click(".rail button[aria-label='Case setup']")
    pg.wait_for_selector(".viewport:not(.results-view) canvas", state="visible", timeout=30_000)
    pg.wait_for_function("!!window.mallardZoom.camera('caseview')", timeout=30_000)
    themed(pg, theme)
    pg.mouse.move(700, 500)
    pg.mouse.wheel(0, -300)  # zoomed: the file is of the view as it is
    pg.wait_for_timeout(500)
    image = saved(pg, tmp_path / "case.png", ".viewport:not(.results-view)")
    assert np.abs(image[3, 3] - background(theme)).sum() < 12
    drawn = np.abs(image - background(theme)).sum(axis=2) > 40
    assert 0.02 < drawn.mean() < 0.9  # the mesh and the zones, drawn
    pg.keyboard.press("f")
    themed(pg, "light")


def test_framing_a_zone_is_not_the_camera_button(pg: Any) -> None:
    """The camera icon framed the selection and saved nothing."""
    pg.click(".rail button[aria-label='Case setup']")
    frame = pg.locator(".toolbar button[aria-label='Frame the selected zone']:visible")
    camera = pg.locator(".toolbar button[title='Save the view as a PNG image']:visible")
    sync_api.expect(frame).to_have_count(1)
    sync_api.expect(camera).to_have_count(1)
    assert ICONS["frame"] != ICONS["camera"]
    assert pg.evaluate("e => e.querySelector('path').getAttribute('d')", frame.element_handle()) != pg.evaluate(
        "e => e.querySelector('path').getAttribute('d')", camera.element_handle()
    )
