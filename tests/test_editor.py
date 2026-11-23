from __future__ import annotations

from pathlib import Path

from mallard_gui.session import Session
from mallard_gui.setup.editor import SetupEditor
from mallard_gui.setup.inflow import Faces

from .test_session import session

BOUNDS = (-25.0, 25.0, -25.0, 25.0, 0.0, 0.0)


def editor(project: Path) -> tuple[Session, SetupEditor]:
    s = session(project)
    return s, SetupEditor(s)


def test_inline_regions_and_geometric_probes_are_previewed(cylinder_project: Path) -> None:
    _, e = editor(cylinder_project)
    nodes = {p[0]: p[1] for p in e.previews(BOUNDS)}
    assert nodes["region:write_data[2].region"] == "region"
    assert "region:write_data[3].regions[2]" in nodes
    assert nodes["el:probes[1]"] == "probe"
    assert "el:probes[2]" not in nodes  # a boundary probe has no shape of its own
    assert e.shape_of("el:write_data[2]") is not None
    assert e.shape_of("el:write_data[2]").path == "write_data[2].region"  # type: ignore[union-attr]


def test_dragging_previews_and_only_release_edits(cylinder_project: Path) -> None:
    s, e = editor(cylinder_project)
    node = "region:write_data[2].region"
    pv = next(p for n, _, p in e.previews(BOUNDS) if n == node)
    centre = next(h for h in pv.handles if h.role == "point")
    before = (cylinder_project / "input.toml").read_text()
    moved = e.drag(node, centre, (4.0, 1.0, 0.0), BOUNDS)
    assert moved is not None and s.doc.text == before and not s.doc.dirty
    assert e.working(node)["center"] == [4.0, 1.0]
    assert e.release(node, centre, (4.0, 1.0, 0.0)) == ["write_data[2].region.center"]
    assert "center = [4.0, 1.0]" in s.doc.text and "# Often, and small" in s.doc.text
    assert (cylinder_project / "input.toml").read_text() == before  # until Save
    assert s.doc.can_undo
    assert s.doc.get("write_data[2].region.size") == [6.0, 3.0]


def test_a_cut_plane_dragged_in_steps_writes_its_piece_where_the_handle_ends(cylinder_project: Path) -> None:
    s, e = editor(cylinder_project)
    node, _, pv = next(t for t in e.previews(BOUNDS) if t[2].handles and t[2].handles[0].role == "move")
    handle = pv.handles[0]
    for x in (2.0, 3.0, 4.0):  # each move hands back the preview, whose handle is anchored where it now is
        moved = e.drag(node, handle, (x, 0.5, 0.0), BOUNDS)
        assert moved is not None
        handle = moved.handles[0]
    paths = e.release(node, handle, (4.0, 0.5, 0.0))
    region = s.doc.get(paths[0].rsplit(".", 1)[0])
    assert region["point"] == [4.0, 0.5] and region["bounds"] == [[3.9, -1.5], [4.1, 2.5]]


def test_picked_points_set_boundary_coordinates_and_show_as_markers(cylinder_project: Path) -> None:
    s, e = editor(cylinder_project)
    path = 'boundaries[name = "farfield"].origin'
    e.pick(path, (0.0, 25.0000001, 0.0))
    assert s.doc.get(path) == [0.0, 25.0]
    assert e.markers("farfield") == [("origin", (0.0, 25.0, 0.0))]


def test_added_regions_are_named_previewed_and_usable(cylinder_project: Path) -> None:
    s, e = editor(cylinder_project)
    node = e.act("section:regions", "add_region", "rectangle", BOUNDS)
    assert node is not None and node in {n["id"] for n in s.tree()}
    assert any(n == node for n, _, _ in e.previews(BOUNDS))
    use = next(a for a in e.actions(node) if a["id"] == "use_in")
    assert "write_data[0]" in use["options"]


def test_farfield_inflow_is_its_uniform_velocity(cylinder_project: Path) -> None:
    _, e = editor(cylinder_project)
    assert e.inflow_zones() == ["farfield"]
    faces = Faces([(25.0, 0.0, 0.0), (-25.0, 0.0, 0.0)], [(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)], [1.0, 1.0])
    (flow,) = e.inflows(["farfield"], {"farfield": faces}, None)
    assert flow.points == [(-25.0, 0.0, 0.0)]  # only where the flow comes in
