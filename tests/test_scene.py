from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from vtkmodules.vtkCommonCore import vtkFloatArray
from vtkmodules.vtkFiltersSources import vtkCubeSource, vtkSphereSource
from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter
from vtkmodules.vtkRenderingCore import vtkActor, vtkProperty

from mallard_gui.macos import activation_policy
from mallard_gui.view.scene import CaseScene, input_path_of, label_of, snap_normal

from .conftest import FIXTURES, fixture_json
from .ui.serve import address


@pytest.fixture
def scene2d() -> CaseScene:
    s = CaseScene(offscreen=True)
    s.window.SetSize(400, 300)
    s.load(FIXTURES / "cylinder_view" / "case.vtm", fixture_json("cylinder_check.json"))
    return s


def test_2d_blocks_classified(scene2d: CaseScene) -> None:
    kinds = {name: b.kind for name, b in scene2d.blocks.items()}
    assert kinds["domain"] == "mesh"
    assert kinds["cylinder"] == kinds["farfield"] == "zone"
    assert kinds['probes[name = "wake_point"]'] == "probe"
    # The case view writes '/' as '_'; the block keeps the input's name.
    region = 'write_data[prefix = "./solut/near_wake"].region'
    assert kinds[region] == "region"
    assert scene2d.blocks[region].label == "near_wake"
    assert scene2d.blocks["farfield"].input_path == 'boundaries[name = "farfield"]'


def test_label_and_input_path() -> None:
    assert label_of('probes[name = "wall"]') == "wall"
    assert input_path_of("inlet", "zone", set()) is None
    assert input_path_of("fluid", "region", set()) == "regions.fluid"
    assert snap_normal((0.1, 0.0, 1.0)) == (0.0, 0.0, 1.0)
    assert snap_normal((1.0, 1.0, 0.0)) != (1.0, 0.0, 0.0)


def test_2d_pick_respects_visibility(scene2d: CaseScene) -> None:
    for _ in range(4):
        scene2d.renderer.GetActiveCamera().Zoom(3.0)
    # The wall probe lies on the cylinder's wall, which passes through (0.5, 0).
    x, y = _display(scene2d, (0.5, 0.0, 0.0))
    hit = scene2d.pick(x, y)
    assert hit is not None and hit["name"] == 'probes[name = "wall"]'
    assert hit["point"] == pytest.approx([0.5, 0.0, 0.0], abs=0.01)
    scene2d.set_visible('probes[name = "wall"]', False)
    hit = scene2d.pick(x, y)
    assert hit is not None and hit["name"] == "cylinder"


def test_isolate_and_show_all(scene2d: CaseScene) -> None:
    scene2d.isolate(["cylinder"])
    visible = {n for n, b in scene2d.blocks.items() if b.visible}
    assert visible == {"cylinder", "domain"}
    scene2d.show_all()
    assert all(b.visible for b in scene2d.blocks.values())


def _display(s: CaseScene, p: tuple[float, float, float]) -> tuple[float, float]:
    s.window.Render()
    s.renderer.SetWorldPoint(*p, 1.0)
    s.renderer.WorldToDisplay()
    x, y, _ = s.renderer.GetDisplayPoint()
    return x, y


def _write_poly(source: Any, path: Path) -> None:
    source.Update()
    w = vtkXMLPolyDataWriter()
    w.SetFileName(str(path))
    w.SetInputData(source.GetOutput())
    w.Write()


@pytest.fixture
def scene3d(tmp_path: Path) -> CaseScene:
    """A unit box (zone "box") around a sphere of radius 0.2 (zone "ball"), like a body in a domain."""
    cube = vtkCubeSource()
    cube.SetBounds(-1, 1, -1, 1, -1, 1)
    sphere = vtkSphereSource()
    sphere.SetRadius(0.2)
    sphere.SetThetaResolution(32)
    sphere.SetPhiResolution(32)
    _write_poly(cube, tmp_path / "box.vtp")
    _write_poly(sphere, tmp_path / "ball.vtp")
    (tmp_path / "case.vtm").write_text(
        '<?xml version="1.0"?>\n<VTKFile type="vtkMultiBlockDataSet" version="1.0"><vtkMultiBlockDataSet>'
        '<DataSet index="0" name="box" file="box.vtp"/><DataSet index="1" name="ball" file="ball.vtp"/>'
        "</vtkMultiBlockDataSet></VTKFile>\n"
    )
    report = {"dim": 3, "boundaries": [{"zone": "box"}, {"zone": "ball"}], "regions": []}
    s = CaseScene(offscreen=True)
    s.window.SetSize(300, 300)
    s.load(tmp_path / "case.vtm", report)
    s.view_preset("-Z")  # from +z, looking down
    return s


def test_3d_pick_through_the_cut(scene3d: CaseScene) -> None:
    x, y = _display(scene3d, (0.0, 0.0, 0.0))
    assert scene3d.pick(x, y)["name"] == "box"  # type: ignore[index]
    # Keep z < 0: the box's near face is cut away, the ball's lower half is the first surface.
    scene3d.set_clip(True, origin=(0, 0, 0), normal=(0, 0, 1), flipped=True)
    assert scene3d.pick(x, y)["name"] == "ball"  # type: ignore[index]
    scene3d.set_clip(False)
    assert scene3d.pick(x, y)["name"] == "box"  # type: ignore[index]


def test_3d_xray_picks_selected_zone_through_others(scene3d: CaseScene) -> None:
    x, y = _display(scene3d, (0.0, 0.0, 0.0))
    scene3d.select("ball")
    assert scene3d.pick(x, y)["name"] == "box"  # type: ignore[index]
    scene3d.set_xray(True, 0.2)
    assert scene3d.blocks["box"].actor.GetProperty().GetOpacity() == pytest.approx(0.2)
    assert scene3d.blocks["ball"].actor.GetProperty().GetOpacity() == 1.0
    assert scene3d.pick(x, y)["name"] == "ball"  # type: ignore[index]
    scene3d.select(None)
    assert scene3d.pick(x, y)["name"] == "box"  # type: ignore[index]  # all translucent: the nearest


def test_focus_frames_a_small_zone_keeping_the_view_direction(scene3d: CaseScene) -> None:
    def ball_px() -> float:
        return _display(scene3d, (0.2, 0.0, 0.0))[0] - _display(scene3d, (-0.2, 0.0, 0.0))[0]

    cam = scene3d.renderer.GetActiveCamera()
    before = ball_px()
    view = cam.GetDirectionOfProjection()
    assert scene3d.focus("ball")
    assert ball_px() > 3 * before
    assert ball_px() < 300  # with a margin, not cropped
    assert cam.GetDirectionOfProjection() == pytest.approx(view)
    assert _display(scene3d, (0.0, 0.0, 0.0)) == pytest.approx((150, 150), abs=1)
    assert not scene3d.focus("no such zone")


def test_3d_outline_added(scene3d: CaseScene) -> None:
    assert scene3d.blocks["domain"].kind == "mesh"
    assert scene3d.bounds[0] == pytest.approx(-1.0)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_an_offscreen_scene_shows_no_dock_icon() -> None:
    CaseScene(offscreen=True).window.Render()
    assert activation_policy() == 2  # NSApplicationActivationPolicyProhibited


def test_a_long_2d_domain_fits_the_view_when_reset() -> None:
    s = CaseScene(offscreen=True)
    s.window.SetSize(300, 300)
    s.dim, s.bounds = 2, (0.0, 0.4, 0.0, 0.1, 0.0, 0.0)
    s.reset_camera()
    xs = [_display(s, (x, y, 0.0)) for x in (0.0, 0.4) for y in (0.0, 0.1)]
    assert all(0 <= x <= 300 and 0 <= y <= 300 for x, y in xs)
    assert max(x for x, _ in xs) - min(x for x, _ in xs) > 0.8 * 300  # and fills it


def test_a_resized_view_is_framed_again_until_the_camera_moves(scene2d: CaseScene) -> None:
    scene2d.reset_camera()
    scale = scene2d.renderer.GetActiveCamera().GetDistance()
    scene2d.window.SetSize(300, 600)  # a narrow view: the domain needs more room across
    assert scene2d.renderer.GetActiveCamera().GetDistance() > scale
    scene2d.style.InvokeEvent("StartInteractionEvent")
    moved = scene2d.renderer.GetActiveCamera().GetDistance()
    scene2d.window.SetSize(800, 300)
    assert scene2d.renderer.GetActiveCamera().GetDistance() == moved


def test_a_freed_objects_name_is_not_given_to_the_object_at_its_address() -> None:
    """trame-vtk named the scene's objects to the browser by address, and the browser's scene keeps what it
    built under a name: a new actor where a freed property was (they share a heap size class) was updated as
    that property, the scene update threw, and the view stopped following the scene."""
    from trame_vtk.modules.vtk.serializers import actors as serializer

    for _ in range(5000):  # the heap hands a freed block out again at once, unless the thread changed cores
        old = vtkProperty()
        at, name = address(old), serializer.reference_id(old)
        del old
        new = vtkActor()
        if address(new) == at:
            break
    else:
        pytest.fail("the heap never gave a new actor a freed property's address")
    assert serializer.reference_id(new) != name


def test_a_deleted_arrays_hash_is_forgotten() -> None:
    """trame-vtk keeps each array's hash by the array's name: with a name per object instead of per address,
    the hashes of deleted arrays would be kept for good, a few every scene update of a drag."""
    from trame_vtk.modules.vtk.serializers import helpers

    array = vtkFloatArray()
    array.SetNumberOfTuples(3)
    helpers.digest(array)
    name = helpers.reference_id(array)
    assert name in helpers.data_array_sha_mapping
    del array
    assert name not in helpers.data_array_sha_mapping


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("size", [(600, 300), (300, 500)])
def test_fit_frames_the_box_from_where_the_camera_looks(size: tuple[int, int], parallel: bool) -> None:
    """Fit moves and zooms only: the direction, the up and the view angle stay, every corner is in view, and
    the box reaches the margin on the side that limits it, centered on both."""
    s = CaseScene(offscreen=True)
    s.window.SetSize(*size)
    s.bounds = (0.0, 4.0, -1.0, 1.0, 2.0, 3.0)
    cam = s.renderer.GetActiveCamera()
    cam.SetFocalPoint(9.0, -7.0, 5.0)  # looking past the box, from an orbit of the user's
    cam.SetPosition(11.0, -4.0, 9.5)
    cam.SetViewUp(0.2, 0.9, 0.1)
    cam.SetParallelProjection(1 if parallel else 0)
    cam.OrthogonalizeViewUp()
    look, up, angle = cam.GetDirectionOfProjection(), cam.GetViewUp(), cam.GetViewAngle()
    s.fit(margin=1.25)
    assert cam.GetDirectionOfProjection() == pytest.approx(look, abs=1e-9)
    assert cam.GetViewUp() == pytest.approx(up, abs=1e-9) and cam.GetViewAngle() == angle
    w, h = size
    pts = [_display(s, (x, y, z)) for x in s.bounds[0:2] for y in s.bounds[2:4] for z in s.bounds[4:6]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert min(xs) + max(xs) == pytest.approx(w, abs=1.0) and min(ys) + max(ys) == pytest.approx(h, abs=1.0)
    fill = max((max(xs) - min(xs)) / w, (max(ys) - min(ys)) / h)
    assert fill == pytest.approx(1 / 1.25, abs=0.01)
    again = (*cam.GetPosition(), *cam.GetFocalPoint(), cam.GetParallelScale())
    s.fit(margin=1.25)
    assert (*cam.GetPosition(), *cam.GetFocalPoint(), cam.GetParallelScale()) == pytest.approx(again)


def test_the_other_projection_draws_the_focal_plane_at_the_same_size(scene2d: CaseScene) -> None:
    scene2d.reset_camera()
    left = _display(scene2d, (scene2d.bounds[0], 0.0, 0.0))
    scene2d.set_parallel(True)
    assert _display(scene2d, (scene2d.bounds[0], 0.0, 0.0)) == pytest.approx(left, abs=0.5)
    scene2d.set_parallel(False)
    assert _display(scene2d, (scene2d.bounds[0], 0.0, 0.0)) == pytest.approx(left, abs=0.5)


def test_a_renderer_left_empty_still_tells_the_browser_to_remove_its_actors() -> None:
    """trame-vtk says nothing of a renderer with no prop to draw, though it has noted the props as removed:
    the browser kept every actor of the scene before, for good."""
    from trame_vtk.modules.vtk.serializers import registry
    from trame_vtk.modules.vtk.serializers.initialize import initialize_serializers
    from trame_vtk.modules.vtk.serializers.synchronization_context import SynchronizationContext
    from vtkmodules.vtkRenderingCore import vtkPolyDataMapper, vtkRenderer

    from mallard_gui.view import scene_ids

    initialize_serializers()
    serialize = registry.SERIALIZERS["vtkOpenGLRenderer"]
    context = SynchronizationContext()
    renderer = vtkRenderer()
    sphere = vtkSphereSource()
    sphere.Update()
    mapper = vtkPolyDataMapper()
    mapper.SetInputData(sphere.GetOutput())
    actor = vtkActor()
    actor.SetMapper(mapper)
    renderer.AddActor(actor)
    name = scene_ids.reference_id(actor)

    def calls() -> list[list[Any]]:
        state = serialize(None, renderer, scene_ids.reference_id(renderer), context, 1)
        return [] if state is None else list(state["calls"])

    assert ["addViewProp", [f"instance:${{{name}}}"]] in calls()
    renderer.RemoveAllViewProps()
    assert ["removeViewProp", [f"instance:${{{name}}}"]] in calls()
    assert not [c for c in calls() if c[0] == "removeViewProp"]  # told once
