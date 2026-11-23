"""The results view's section tools, by what the viewer draws: the clip keeps the side its normal points to, in
images and in the geometry sent to the browser alike; a clipped volume shows the field on its cut face; a slice
is seen through the volume around it; a 2D run is cut by the plane it opens with, its slice a line drawn over
the field; the plane is marked in the scene; and turning each off draws the run whole again."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from mallard_gui.view.scene import THEMES
from mallard_gui.viewer.engine import ResultScene
from mallard_gui.viewer.geometry import decode_block

from .runs import make_run

W, H = 480, 360


@pytest.fixture(scope="module")
def box(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_run(tmp_path_factory.mktemp("section") / "box", steps=2)


@pytest.fixture(scope="module")
def plane(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_run(tmp_path_factory.mktemp("section") / "plane", steps=2, dim=2)


def scene(run: Path, preset: str = "") -> ResultScene:
    s = ResultScene(run.parent, theme="light")
    s.open(run.name)
    s.show("solut/box", 0)
    s.window.SetSize(W, H)
    if preset:
        s.camera("preset", preset=preset)
    return s


def image(s: ResultScene) -> np.ndarray[Any, Any]:
    """The frame the viewer renders (H, W, 3), row 0 at the top."""
    s.window.SetSize(W, H)
    pixels = np.asarray(Image.open(io.BytesIO(s.render_image("png"))).convert("RGB")).astype(float)
    assert pixels.shape[:2] == (H, W)
    return pixels


def drawn(img: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    mask: np.ndarray[Any, Any] = np.abs(img - 255.0).sum(axis=2) > 30
    return mask


def at(s: ResultScene, point: tuple[float, float, float]) -> tuple[int, int]:
    """The pixel (column, row from the top) a world point is drawn at."""
    s.renderer.SetWorldPoint(*point, 1.0)
    s.renderer.WorldToDisplay()
    x, y, _ = s.renderer.GetDisplayPoint()
    return round(x), H - 1 - round(y)


def yellow(pixel: np.ndarray[Any, Any]) -> float:
    """Cividis from low (blue, negative) to high (yellow, positive)."""
    return float(pixel[0] + pixel[1] - 2 * pixel[2])


def is_color(pixel: np.ndarray[Any, Any], color: tuple[float, float, float]) -> bool:
    return bool(np.abs(pixel - 255 * np.array(color)).sum() < 60)


def test_the_clip_keeps_the_side_its_normal_points_to_in_images_and_in_geometry(box: Path) -> None:
    """The geometry sent to the browser kept the other side: the two modes showed opposite halves."""
    s = scene(box, "+Z")  # along +z with y up: x rises to the left
    s.set_section(clip=True, origin=[0.3, 0.5, 0.5], normal=[1, 0, 0])
    img = image(s)
    kept, cut = at(s, (0.65, 0.5, 0.0)), at(s, (0.15, 0.5, 0.0))
    assert drawn(img)[kept[1], kept[0]] and not drawn(img)[cut[1], cut[0]]
    sent = {b["name"]: decode_block(b) for b in s.geometry()["blocks"]}
    xs = [sent["field"].GetPoint(i)[0] for i in range(sent["field"].GetNumberOfPoints())]
    assert min(xs) == pytest.approx(0.3) and max(xs) == pytest.approx(1.0)
    s.set_section(flipped=True)
    img = image(s)
    assert drawn(img)[cut[1], cut[0]] and not drawn(img)[kept[1], kept[0]]
    xs = [p[0] for p in map(decode_block(s.geometry()["blocks"][0]).GetPoint, range(8))]
    assert max(xs) <= 0.3 + 1e-6
    s.set_section(clip=False)
    assert drawn(image(s))[kept[1], kept[0]] and drawn(image(s))[cut[1], cut[0]]  # whole again


def test_a_clipped_volume_shows_the_field_on_its_cut_face(box: Path) -> None:
    """The volume's surface alone was clipped: a hollow shell, the far faces seen through the cut."""
    s = scene(box, "+X")  # looking along +x, at the cut face
    whole = image(s)
    s.set_section(clip=True, origin=[0.7, 0.5, 0.5], normal=[1, 0, 0])
    img = image(s)
    c = at(s, (0.7, 0.5, 0.5))
    # P = x at t = 0, over [0.08, 0.92]: the face at x = 0.7 is its upper third, not the x = 1 face's top
    # (through a hollow shell) nor the x = 0 face's bottom (unclipped).
    assert 40 < yellow(img[c[1], c[0]]) < yellow(np.array([255.0, 233.0, 69.0])) - 60
    assert yellow(whole[c[1], c[0]]) < -60
    sent = {b["name"] for b in s.geometry()["blocks"]}
    assert "slice" in sent  # the browser draws the face too
    assert s.section()["slice_cells"] == 0  # it is the clip's face, not a slice asked for


def test_a_slice_is_seen_through_the_volume_around_it(box: Path) -> None:
    """The slice was drawn inside the opaque volume: turning Slice on changed nothing in the picture."""
    s = scene(box, "+X")
    whole = image(s)
    s.set_section(slice=True, origin=[0.7, 0.5, 0.5], normal=[1, 0, 0])
    img = image(s)
    c = at(s, (0.7, 0.5, 0.5))
    assert yellow(img[c[1], c[0]]) - yellow(whole[c[1], c[0]]) > 80  # the slice's higher P through the x = 0 face
    assert s.section()["slice_cells"] > 0
    s.set_section(slice=False)
    assert np.array_equal(image(s), whole)


def test_the_plane_is_marked_in_the_scene_while_it_cuts(box: Path) -> None:
    s = scene(box, "+Z")
    assert "section" not in s.blocks
    s.set_section(clip=True, origin=[0.3, 0.5, 0.5], normal=[1, 0, 0])
    accent = THEMES["light"]["accent"]
    img = image(s)
    edge = at(s, (0.3, 0.02, 0.0))  # the plane's outline on the box, beside the kept half
    near = img[edge[1] - 3 : edge[1] + 4, edge[0] - 3 : edge[0] + 4].reshape(-1, 3)
    assert any(is_color(p, accent) for p in near)
    assert "section" in {b["name"] for b in s.geometry()["blocks"]}
    s.set_section(clip=False)
    assert "section" not in s.blocks


def test_a_2d_run_opens_with_a_plane_that_cuts_it(plane: Path) -> None:
    """A 2D run opened with the plane of the page (normal z): Clip and Slice did nothing until a normal was typed."""
    s = scene(plane)
    whole = image(s)
    assert s.section()["normal"] == [1.0, 0.0, 0.0]
    s.set_section(clip=True)
    img = image(s)
    left, right = at(s, (0.2, 0.5, 0.0)), at(s, (0.8, 0.5, 0.0))
    assert drawn(img)[right[1], right[0]] and not drawn(img)[left[1], left[0]]
    s.set_section(normal=[0, 0, 1])  # no plane of a 2D run: the plane stays
    assert s.section()["normal"] == [1.0, 0.0, 0.0]
    s.set_section(clip=False)
    assert np.array_equal(image(s), whole)


def test_a_2d_slice_is_a_line_drawn_over_the_field(plane: Path) -> None:
    """The cut was drawn in the field's colors on the field itself: nothing to see."""
    s = scene(plane)
    whole = image(s)
    s.set_section(slice=True, origin=[0.4, 0.5, 0.0], normal=[1, 0, 0])
    img = image(s)
    c = at(s, (0.4, 0.5, 0.0))
    assert is_color(img[c[1], c[0]], THEMES["light"]["accent"])
    assert not is_color(whole[c[1], c[0]], THEMES["light"]["accent"])
    assert s.section()["slice_cells"] > 0
    s.set_section(slice=False)
    assert np.array_equal(image(s), whole)


def test_a_run_opens_whole_whatever_the_last_one_showed(box: Path) -> None:
    s = scene(box)
    s.set_section(clip=True, slice=True, flipped=True)
    s.set_xray(True)
    s.open(box.name)
    sec = s.section()
    assert not sec["clip"] and not sec["slice"] and not sec["flipped"] and not s.xray
