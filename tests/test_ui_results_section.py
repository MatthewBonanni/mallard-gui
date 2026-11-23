"""The results view's Clip and Slice in Chrome and WebKit, over the viewer's images and drawn by the browser, on
a 3D and a 2D run: each changes the picture as its plane says, the plane's mark is drawn and moves with the
view, the origin typed and the side flipped apply, and turning it off draws the run whole again."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from mallard_gui.view.scene import THEMES

from .test_ui_view_camera import MODES, box_of, drag, drawn, open_run, picture, settled
from .test_ui_view_camera import pg as pg
from .test_ui_view_camera import world_url as world_url

pytestmark = pytest.mark.ui


def tool(pg: Any, name: str) -> None:
    pg.locator(".toolbar button:visible", has_text=name).first.click()


def typed(pg: Any, label: str, value: str) -> None:
    """Type into one of the section bar's number fields (under Numbers)."""
    if not pg.evaluate("trame.state.get('v_sec_more')"):
        pg.locator(".optbar button:visible", has_text="Numbers").click()
    field = pg.locator(f".optbar input[aria-label='{label}']:visible")
    field.click()
    field.press("ControlOrMeta+a")
    pg.keyboard.type(value)  # as a hand types: key by key, the page redrawing in between
    field.press("Tab")


def shown(pg: Any, mode: str, after: np.ndarray[Any, Any] | None = None) -> np.ndarray[Any, Any]:
    """The view's picture once it has settled; given `after`, once it is another picture than that one (the
    viewer draws a change a while after the page asks for it)."""
    for _ in range(40):
        settled(pg, mode)
        now = picture(pg)
        if after is None or now.shape != after.shape or np.abs(now - after).mean() > 0.01:
            return now
        pg.wait_for_timeout(250)
    raise AssertionError("the picture did not change")


def section_is(pg: Any, **want: Any) -> None:
    """The viewer has the section asked for: clip, slice, flipped, and the origin's x."""
    checks = " && ".join(
        f"Math.abs(s.origin[0] - {v}) < 1e-9" if k == "x" else f"s.{k} === {str(v).lower()}" for k, v in want.items()
    )
    try:
        pg.wait_for_function(
            f"(() => {{ const s = trame.state.get('v_info').section || {{}}; return {checks}; }})()", timeout=10_000
        )
    except Exception as e:
        keys = "['v_info', 'v_clip', 'v_slice', 'v_clip_origin', 'v_clip_normal', 'v_clip_flip', 'v_error', 'v_run']"
        now = pg.evaluate(f"{keys}.map(k => k === 'v_info' ? trame.state.get(k).section : trame.state.get(k))")
        raise AssertionError(f"the viewer's section is not {want}: {now}") from e


def accent_columns(pg: Any, img: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    """The columns (CSS px) where the plane's mark, in the accent color, is drawn top to bottom."""
    accent = 255 * np.array(THEMES[pg.evaluate("trame.state.get('theme')")]["accent"])
    hits = (np.abs(img - accent).sum(axis=2) < 60).sum(axis=0)
    cols: np.ndarray[Any, Any] = np.nonzero(hits > 40)[0] / 2
    return cols


def face_on(pg: Any, run: str, mode: str) -> tuple[np.ndarray[Any, Any], tuple[float, float, float, float]]:
    """The run seen along -z (x to the right), no section: its picture and the box it is drawn in."""
    open_run(pg, run, mode)
    if pg.evaluate("trame.state.get('v_clip_flip') || !trame.state.get('v_sec_mark')"):  # set in the section's bar
        if not pg.evaluate("trame.state.get('v_clip') || trame.state.get('v_slice')"):
            tool(pg, "Clip")
        if pg.evaluate("trame.state.get('v_clip_flip')"):
            pg.locator(".optbar button:visible", has_text="Flip").click()
        if not pg.evaluate("trame.state.get('v_sec_mark')"):
            pg.locator(".optbar button:visible", has_text="Plane").click()
            pg.wait_for_function("trame.state.get('v_sec_mark') === true")
    for name, key in (("Clip", "v_clip"), ("Slice", "v_slice"), ("X-ray", "v_xray")):
        if pg.evaluate(f"trame.state.get('{key}')"):
            tool(pg, name)
    if bool(pg.evaluate("trame.state.get('v_parallel')")) != (run == "box-1"):
        tool(pg, "Ortho")  # the box without perspective: its near and far faces pan and are cut as one
    if run == "box-1":
        pg.locator(".toolbar button:visible", has_text="−Z").click()
    section_is(pg, clip=False, slice=False, flipped=False)
    pg.wait_for_timeout(600)
    whole = shown(pg, mode)
    return whole, box_of(drawn(whole))


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("run", ["box-1", "plane-1"])
def test_clip_cuts_at_the_plane_keeps_its_side_and_goes(pg: Any, run: str, mode: str) -> None:
    whole, (left, top, right, bottom) = face_on(pg, run, mode)
    width = right - left
    tool(pg, "Clip")
    if run == "box-1":
        pg.locator(".optbar button:visible", has_text="X").first.click()
    typed(pg, "origin x", "0.5")
    section_is(pg, clip=True, x=0.5)
    # A 2D run is cut as it opens, by a plane across x: the half its normal points to stays.
    half = shown(pg, mode, whole)
    cut = box_of(drawn(half))
    assert cut[0] == pytest.approx(left + 0.5 * width, abs=0.02 * width) and cut[2] == pytest.approx(right, abs=2)
    typed(pg, "origin x", "0.3")
    section_is(pg, x=0.3)
    more = shown(pg, mode, half)
    cut = box_of(drawn(more))
    assert cut[0] == pytest.approx(left + 0.3 * width, abs=0.02 * width) and cut[2] == pytest.approx(right, abs=2)
    mark = accent_columns(pg, more)
    assert len(mark) and mark.mean() == pytest.approx(cut[0], abs=3), "the plane's mark is not at the cut"
    # The mark and the cut move with the view.
    pg.keyboard.down("Shift")
    drag(pg, 40, 0, at=(0.8, 0.5))
    pg.keyboard.up("Shift")
    panned = shown(pg, mode, more)
    assert accent_columns(pg, panned).mean() == pytest.approx(mark.mean() + 40, abs=4)
    assert box_of(drawn(panned))[0] == pytest.approx(cut[0] + 40, abs=4)
    pg.keyboard.down("Shift")
    drag(pg, -40, 0, at=(0.8, 0.5))
    pg.keyboard.up("Shift")
    back = shown(pg, mode, panned)
    pg.locator(".optbar button:visible", has_text="Flip").click()
    section_is(pg, flipped=True)
    flipped = shown(pg, mode, back)
    other = box_of(drawn(flipped))
    assert other[0] == pytest.approx(left, abs=3) and other[2] == pytest.approx(left + 0.3 * width, abs=0.02 * width)
    tool(pg, "Clip")
    section_is(pg, clip=False)
    again = shown(pg, mode, flipped)
    assert box_of(drawn(again)) == pytest.approx((left, top, right, bottom), abs=3)
    assert len(accent_columns(pg, again)) == 0
    assert np.abs(again - whole).mean() < 1.0, "the run is not drawn whole again"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("run", ["box-1", "plane-1"])
def test_slice_is_seen_at_the_plane_and_goes(pg: Any, run: str, mode: str) -> None:
    whole, (left, _, right, _) = face_on(pg, run, mode)
    width = right - left
    tool(pg, "Slice")
    if run == "box-1":
        pg.locator(".optbar button:visible", has_text="X").first.click()
    typed(pg, "origin x", "0.4")
    section_is(pg, slice=True, x=0.4)
    sliced = shown(pg, mode, whole)
    assert pg.evaluate("trame.state.get('v_info').section.slice_cells") > 0
    # Seen: the picture changed where the run is drawn, and the plane is marked at x = 0.4.
    inside = drawn(whole)
    assert np.abs(sliced - whole)[inside].mean() > (8.0 if run == "box-1" else 0.05)
    mark = accent_columns(pg, sliced)
    assert len(mark) and mark.mean() == pytest.approx(left + 0.4 * width, abs=0.02 * width + 2)
    tool(pg, "Slice")
    section_is(pg, slice=False)
    again = shown(pg, mode, sliced)
    assert np.abs(again - whole).mean() < 1.0 and len(accent_columns(pg, again)) == 0


def test_text_typed_into_a_field_survives_the_pages_redraws(pg: Any) -> None:
    """The page redraws whenever any state changes and set each field back to its state's value: an origin
    typed while a frame or an answer of the viewer arrived was replaced by the old one, and never entered."""
    face_on(pg, "plane-1", "images")
    tool(pg, "Clip")
    section_is(pg, clip=True)
    typed(pg, "origin x", "0.5")
    section_is(pg, x=0.5)
    field = pg.locator(".optbar input[aria-label='origin x']:visible")
    field.click()
    field.press("ControlOrMeta+a")
    for key in "0.25":
        pg.keyboard.type(key)
        pg.evaluate("trame.state.set('v_busy', 'redraw ' + Math.random())")  # any state: the page redraws
        pg.wait_for_timeout(120)
    assert field.input_value() == "0.25"
    field.press("Tab")
    section_is(pg, x=0.25)
    pg.evaluate("trame.state.set('v_busy', '')")
    # Left with nothing entered (no change), a field shows the state's value again.
    tool(pg, "Clip")
    section_is(pg, clip=False)


@pytest.mark.parametrize("mode", MODES)
def test_the_sections_bar_is_in_the_view_and_its_slider_moves_the_cut(pg: Any, mode: str) -> None:
    """The section's settings were rows appended to the right pane's end. They are a bar along the top of the
    view, under its toolbar, while Clip or Slice is on: presets, the side, a slider across the data, the mark."""
    whole, (left, _, right, _) = face_on(pg, "plane-1", mode)
    width = right - left
    assert pg.locator(".optbar:visible").count() == 0
    assert "SECTION" not in pg.locator(".panel.right:visible").inner_text()
    tool(pg, "Clip")
    section_is(pg, clip=True)
    bar = pg.locator(".optbar:visible")
    bar.wait_for()
    assert "SECTION" in bar.inner_text() and "SECTION" not in pg.locator(".panel.right:visible").inner_text()
    # Along the top of the view, right under its toolbar, over nothing else the view shows there.
    tools, view = pg.locator(".toolbar:visible").bounding_box(), pg.locator(".results-view").first.bounding_box()
    at = bar.bounding_box()
    assert at["y"] == pytest.approx(tools["y"] + tools["height"], abs=2) and at["y"] == pytest.approx(view["y"], abs=2)
    assert at["width"] == pytest.approx(view["width"], abs=2) and at["height"] < 70
    covered = pg.evaluate(
        """view => { const b = document.querySelector(view + ' .optbar').getBoundingClientRect();
        return [...document.querySelectorAll(view + ' :is(.legend, .triad, .ana-panel, .su-banner, .view-notice)')]
          .filter(e => e.offsetParent && e.getBoundingClientRect().top < b.bottom - 1).length; }""",
        ".results-view",
    )
    assert covered == 0
    half = shown(pg, mode, whole)
    slider = pg.locator(".optbar input[aria-label='section position']").bounding_box()
    to = 0.3 if pg.evaluate("trame.state.get('v_sec').at") > 0.5 else 0.7  # away from where the plane is
    pg.mouse.click(slider["x"] + to * slider["width"], slider["y"] + slider["height"] / 2)
    try:
        pg.wait_for_function(f"Math.abs(trame.state.get('v_sec').at - {to}) < 0.08", timeout=10_000)
    except Exception as e:
        got = pg.evaluate(
            "[trame.state.get('v_sec'), trame.state.get('v_clip_origin'), trame.state.get('v_info').section]"
        )
        raise AssertionError(f"the slider at {to} left the plane at {got}") from e
    x = pg.evaluate("trame.state.get('v_sec').at")
    moved = shown(pg, mode, half)
    assert box_of(drawn(moved))[0] == pytest.approx(left + x * width, abs=0.02 * width)
    assert float(pg.locator(".optbar input[aria-label='section position value']").input_value()) == pytest.approx(x)
    assert len(accent_columns(pg, moved))
    pg.locator(".optbar button:visible", has_text="Plane").click()  # the mark off: the cut stays
    section_is(pg, mark=False)
    unmarked = shown(pg, mode, moved)
    assert len(accent_columns(pg, unmarked)) == 0
    assert box_of(drawn(unmarked))[0] == pytest.approx(left + x * width, abs=0.02 * width)
    pg.locator(".optbar button:visible", has_text="Plane").click()
    section_is(pg, mark=True)
    pg.locator(".optbar button[title^='Turn']:visible").click()
    section_is(pg, clip=False)
    assert pg.locator(".optbar:visible").count() == 0
    assert np.abs(shown(pg, mode, unmarked) - whole).mean() < 1.0
