from __future__ import annotations

import math

import pytest

from mallard_gui.setup.geometry import Handle, apply_handle, length, plane_axes, preview, vector


def bounds_of(p: object) -> tuple[float, ...]:
    return tuple(p.poly.GetBounds())  # type: ignore[attr-defined]


def test_lengths_take_numbers_and_length_quantities_only() -> None:
    assert length(2) == 2.0
    assert length("2 mm") == pytest.approx(0.002)
    assert length("3 in") == pytest.approx(0.0762)
    assert length("2 * x") is None
    assert length("3 K") is None
    assert vector([1, "5 cm"]) == (1.0, 0.05, 0.0)


def test_rectangle_2d_is_its_outline_with_center_and_size_handles() -> None:
    p = preview({"type": "rectangle", "center": [3.0, 0.0], "size": [6.0, 3.0]}, 2)
    assert p.dimension == 2
    assert bounds_of(p)[:4] == (0.0, 6.0, -1.5, 1.5)
    assert [(h.key, h.role) for h in p.handles] == [("center", "point"), ("size", "size")]
    assert p.handles[1].position == (6.0, 1.5, 0.0)


def test_sphere_3d_and_circle_2d_share_center_and_radius_handles() -> None:
    p = preview({"type": "sphere", "center": [0, 0, 1], "radius": 0.5}, 3)
    assert p.dimension == 3
    assert bounds_of(p)[4:] == pytest.approx((0.5, 1.5))
    q = preview({"type": "sphere", "center": [0, 0], "radius": 2, "filled": False}, 2)
    assert q.dimension == 1
    assert bounds_of(q)[:2] == pytest.approx((-2, 2))


def test_line_probe_samples_n_points_from_start_to_end() -> None:
    p = preview({"name": "x", "type": "line", "start": [0, 0], "end": [1, 0], "n_points": 5}, 2)
    assert p.dimension == 1
    assert [s[0] for s in p.samples] == [0.0, 0.25, 0.5, 0.75, 1.0]


def test_point_probe_without_type_is_a_point() -> None:
    p = preview({"name": "wake_point", "point": [2.5, 0.0]}, 2)
    assert p.dimension == 0 and p.samples == [(2.5, 0.0, 0.0)]
    assert p.handles[0].key == "point"


def test_arc_samples_run_from_the_first_angle_to_the_second() -> None:
    p = preview({"type": "arc", "center": [0, 0], "radius": 5, "angles": [-45, 45], "n_points": 3}, 2)
    assert p.samples[0] == pytest.approx((5 * math.cos(math.radians(-45)), -5 * math.sin(math.radians(45)), 0))
    assert p.samples[1] == pytest.approx((5, 0, 0))


def test_circle_3d_lies_in_the_plane_normal_to_normal() -> None:
    p = preview({"type": "circle", "center": [0, 0, 0], "normal": [1, 0, 0], "radius": 1, "n_points": 8}, 3)
    xmin, xmax, *_ = bounds_of(p)
    assert xmin == xmax == 0.0
    assert len(p.samples) == 8
    e1, _ = plane_axes((1.0, 0.0, 0.0))
    assert p.samples[0] == pytest.approx(e1)  # the first sample along the default reference


def test_cut_plane_2d_is_clipped_to_its_bounds() -> None:
    p = preview(
        {"type": "cut_plane", "point": [1.0, 0.0], "normal": [1.0, 0.0], "bounds": [[0.9, -2.0], [1.1, 2.0]]}, 2
    )
    assert p.dimension == 1
    assert bounds_of(p)[:4] == pytest.approx((1.0, 1.0, -2.0, 2.0))


def test_a_bounded_cut_planes_handle_moves_the_whole_piece() -> None:
    """Moving only `point` left the piece in its old bounds while the handle went elsewhere."""
    table = {"type": "cut_plane", "point": [1.0, 0.0], "normal": [1.0, 0.0], "bounds": [[0.9, -2.0], [1.1, 2.0]]}
    handle = preview(table, 2).handles[0]
    moved = {**table, **apply_handle(table, handle, (4.0, 1.0, 0.0), 2)}
    assert moved["point"] == [4.0, 1.0] and moved["bounds"] == [[3.9, -1.0], [4.1, 3.0]]
    assert bounds_of(preview(moved, 2))[:4] == pytest.approx((4.0, 4.0, -1.0, 3.0))  # the piece is under the handle


def test_named_regions_and_booleans_resolve_and_mesh_regions_are_left_to_their_zone() -> None:
    named = {"a": {"type": "rectangle", "center": [0, 0], "size": [1, 1]}}
    union = preview({"type": "union", "regions": ["a", {"type": "disk", "center": [2, 0], "radius": 0.5}]}, 2, named)
    assert bounds_of(union)[:2] == pytest.approx((-0.5, 2.5))
    assert union.handles == []
    zone = preview("cylinder", 2, named)
    assert zone.mesh_region == "cylinder" and zone.poly.GetNumberOfPoints() == 0


def test_expressions_are_left_to_the_check_with_a_note() -> None:
    p = preview({"type": "sphere", "center": [0, 0, 0], "radius": "2 * t"}, 3)
    assert p.poly.GetNumberOfPoints() == 0
    assert "radius" in p.note and "mallard check" in p.note


def test_transforms_move_the_shape_and_drop_the_handles() -> None:
    p = preview(
        {"type": "rectangle", "center": [0, 0], "size": [2, 1], "rotate": 90, "pivot": [0, 0], "translate": [5, 0]}, 2
    )
    assert bounds_of(p)[:4] == pytest.approx((4.5, 5.5, -1, 1))
    assert p.handles == [] and "transformed" in p.note


def test_handles_set_their_keys() -> None:
    table = {"type": "polygon", "vertices": [[0, 0], [1, 0], [0, 1]]}
    assert apply_handle(table, Handle("center", "point", (0, 0, 0)), (1.23456789, 2, 0), 2) == {
        "center": [1.23457, 2.0]
    }
    assert apply_handle(table, Handle("radius", "radius", (1, 0, 0), anchor=(0, 0, 0)), (3, 4, 0), 2) == {"radius": 5.0}
    assert apply_handle(table, Handle("size", "size", (1, 1, 0), anchor=(1, 1, 0)), (2, 3, 0), 2) == {
        "size": [2.0, 4.0]
    }
    assert apply_handle(table, Handle("vertices", "vertex", (1, 0, 0), index=1), (2, 0, 0), 2) == {
        "vertices": [[0, 0], [2.0, 0.0], [0, 1]]
    }


def test_stl_regions_are_read_in_their_units(tmp_path: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path))
    (root / "tri.stl").write_text(
        "solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1000 0 0\nvertex 0 1000 0\n"
        "endloop\nendfacet\nendsolid t\n"
    )
    p = preview({"type": "stl", "file": "tri.stl", "units": "mm"}, 3, root=root)
    assert bounds_of(p)[1] == pytest.approx(1.0)
