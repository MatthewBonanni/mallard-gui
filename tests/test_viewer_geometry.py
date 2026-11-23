"""Geometry mode, saved cameras, surface extracts, derived series and the render backend report."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mallard_gui.viewer.engine import Budget, BudgetError, ResultScene
from mallard_gui.viewer.geometry import actor_of, decode_block
from mallard_gui.viewer.protocol import dispatch

from .runs import make_run


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_run(tmp_path_factory.mktemp("geometry") / "run", steps=3)


def scene(run: Path, **budget: int) -> ResultScene:
    s = ResultScene(run.parent, Budget(**budget) if budget else None)
    s.open(run.name)
    return s


def test_geometry_sends_the_visible_surfaces_with_mapped_colors(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 1, zones=["solut/inlet"])
    g = s.geometry()
    blocks = {b["name"]: b for b in g["blocks"]}
    assert set(blocks) == {"field", "zone:solut/inlet"}  # what the image draws: no domain box around zone surfaces
    field = blocks["field"]
    assert field["n_triangles"] == 6 * 36 * 2  # the box's faces, two triangles per quad
    poly = decode_block(field)
    assert poly.GetNumberOfPolys() == field["n_triangles"]
    rgb = poly.GetPointData().GetScalars()
    assert rgb is not None and rgb.GetNumberOfComponents() == 3
    # P = t + x: the colors change along x, the lowest and highest values at the colormap's ends.
    values = {tuple(rgb.GetTuple3(i)) for i in range(rgb.GetNumberOfTuples())}
    assert len(values) > 5
    s.show("solut/box", 1, zones=[])
    outline = next(b for b in s.geometry()["blocks"] if b["name"] == "domain")
    assert "rgb" not in outline and outline["n_triangles"] == 0  # the domain's box: lines
    assert actor_of(field).GetMapper().GetScalarVisibility()
    assert g["camera"]["position"] != g["camera"]["focal_point"]


def test_geometry_is_clipped_and_kept_within_its_budget(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    full = s.geometry()["triangles"]
    s.set_section(clip=True, origin=[0.5, 0.5, 0.5], normal=[1, 0, 0])
    assert s.geometry()["triangles"] < full
    s.set_section(clip=False)
    small = s.geometry(max_triangles=200)
    assert small["triangles"] <= 210
    tiny = scene(run, geometry_triangles=40)
    tiny.show("solut/box", 0)
    try:
        assert tiny.geometry()["triangles"] <= 42
    except BudgetError as e:
        assert "image mode" in str(e)


def test_camera_round_trips_for_saved_views(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    s.camera("preset", preset="+X")
    saved = s.get_camera()
    s.camera("orbit", dx=0.3, dy=0.1)
    assert s.get_camera()["position"] != pytest.approx(saved["position"])
    s.set_camera(**saved)
    assert s.get_camera()["position"] == pytest.approx(saved["position"])
    assert s.get_camera()["view_up"] == pytest.approx(saved["view_up"])


def test_export_surface_writes_a_decimated_object_next_to_the_data(tmp_path: Path) -> None:
    run = make_run(tmp_path / "run", steps=1, n=12)
    s = scene(run)
    s.show("solut/box", 0)
    out = s.export_surface("box_surface", triangles=300)
    assert out["path"] == "extract/box_surface.vtp"
    assert out["triangles"] <= 330
    assert (run / out["path"]).stat().st_size == out["bytes"]
    assert not list((run / "extract").glob("*.partial"))
    series = {x["id"]: x for x in s.rescan()["series"]}
    assert series["extract/box_surface"]["kind"] == "derived"
    s.show("extract/box_surface", 0)
    assert "P" in s.field_names()


def test_post_outputs_are_derived_series_with_their_steps(tmp_path: Path) -> None:
    run = make_run(tmp_path / "run", steps=2)
    (run / "average").mkdir()
    shutil.copy(run / "solut" / "box_000010.vtu", run / "average" / "average.vtu")
    s = scene(run)
    series = {x.id: x for x in s.series.values()}
    assert series["average/average"].kind == "derived"
    s.show("average/average", 0)
    assert s.info()["count"] == 1


def test_render_info_and_protocol(run: Path) -> None:
    s = scene(run)
    s.show("solut/box", 0)
    info = dispatch(s, {"id": 1, "method": "render_info"})["result"]
    assert info["window"].endswith("RenderWindow")
    assert isinstance(info["gpu"], bool)
    got = dispatch(s, {"id": 2, "method": "geometry", "params": {"max_triangles": 5000}})
    assert got["result"]["schema"] == "mallard-viewer-geometry"
