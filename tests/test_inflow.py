from __future__ import annotations

import math

import pytest

from mallard_gui.setup.inflow import (
    Faces,
    Inflow,
    boundary_values,
    faces_of,
    inflow,
    initial_state,
    legend,
    speed,
    subsample,
)
from mallard_gui.view.scene import read_vtm

from .conftest import FIXTURES, fixture_json

# A 2D inlet across x = 0, faces along y, normals pointing out of the domain (-x)
INLET = Faces(
    centers=[(0.0, (k + 0.5) / 10, 0.0) for k in range(10)],
    normals=[(-1.0, 0.0, 0.0)] * 10,
    measures=[0.1] * 10,
)
DATA = {"initialize": {"rho": 1.2, "p": 1.0e5}, "physics": {"gamma": 1.4}}


def test_speeds_take_numbers_and_velocity_quantities() -> None:
    assert speed(3) == 3.0
    assert speed("36 km/h") == pytest.approx(10.0)
    assert speed("2 * y") is None


def test_uniform_vector_of_inlet_state() -> None:
    f = inflow("in", {"type": "inlet_state", "u": [10.0, 1.0], "p": 1e5, "T": 300}, INLET, DATA, 2, density=5)
    assert f is not None and f.source == "uniform" and f.known
    assert len(f.points) == 5 and set(f.vectors) == {(10.0, 1.0, 0.0)}


def test_speed_without_direction_is_along_the_inward_normal() -> None:
    f = inflow("in", {"type": "inlet_state", "u": "5 m/s"}, INLET, DATA, 2)
    assert f is not None and f.vectors[0] == (5.0, 0.0, 0.0)
    g = inflow("in", {"type": "inlet_state", "u": 5.0, "direction": [1.0, 1.0]}, INLET, DATA, 2)
    assert g is not None and g.vectors[0] == pytest.approx((5 / math.sqrt(2), 5 / math.sqrt(2), 0))


def test_profiles_are_directions_only_until_mallard_gives_values() -> None:
    f = inflow("in", {"type": "inlet_state", "u": "20 * (1 - r^2)"}, INLET, DATA, 2)
    assert f is not None and not f.known and f.source == "profile"
    assert f.vectors[0] == (1.0, 0.0, 0.0)


def test_vector_profiles_of_x_y_z_are_sampled_at_the_faces() -> None:
    u = ["40 * 4 * y * (1 - y)", "0"]
    f = inflow("in", {"type": "inlet_state", "u": u}, INLET, DATA, 2, density=10)
    assert f is not None and f.known and f.source == "profile, sampled at the faces"
    assert f.vectors[0] == pytest.approx((160 * 0.05 * 0.95, 0.0, 0.0))
    assert f.max_speed == pytest.approx(160 * 0.45 * 0.55)
    local = inflow("in", {"type": "inlet_state", "u": ["20 * (1 - v^2)", "0"]}, INLET, DATA, 2)
    assert local is not None and not local.known  # boundary coordinates: Mallard's to evaluate


def test_mallards_values_at_the_faces_win_over_the_input() -> None:
    values = boundary_values(fixture_json("boundary_values/boundary_values.json"), FIXTURES / "boundary_values")
    left = values[0]
    assert (left.zone, left.condition, left.units, left.estimated) == ("left", "inlet_state", "m/s", False)
    f = inflow("left", {"type": "inlet_state", "u": "anything"}, INLET, DATA, 2, density=100, values=left)
    assert f is not None and f.known and f.source == "from mallard, at each face" and not f.estimate
    assert len(f.points) == 20 and f.points[0] == pytest.approx((0.0, 0.0025, 0.0))
    assert f.max_speed == pytest.approx(99.75)  # the parabola Mallard evaluated, not one this GUI sampled


def test_mass_flow_and_stagnation_estimates_are_marked_and_only_inflow_is_drawn() -> None:
    values = boundary_values(fixture_json("boundary_values/boundary_values.json"), FIXTURES / "boundary_values")
    bottom = inflow("bottom", {"type": "inlet_mass_flow"}, INLET, DATA, 2, density=100, values=values[1])
    assert bottom is not None and bottom.estimate and bottom.source.startswith("estimate from mallard")
    assert bottom.vectors[0] == pytest.approx((0.0, 16.18564844478563, 0.0))
    top = inflow("top", {"type": "inlet_stagnation"}, INLET, DATA, 2, values=values[3])
    assert top is not None and top.points == [] and top.note  # Mallard's estimate lets nothing in here


def test_mass_flow_inlet_estimates_the_speed_from_the_initial_density() -> None:
    f = inflow("in", {"type": "inlet_mass_flow", "mdot": 0.12, "T": 300}, INLET, DATA, 2)
    assert f is not None and f.estimate
    assert f.vectors[0] == pytest.approx((0.12 / 1.0 / 1.2, 0, 0))  # flux over the inlet's length 1


def test_stagnation_inlet_estimates_an_isentropic_speed() -> None:
    f = inflow("in", {"type": "inlet_stagnation", "p0": 1.1e5, "T0": 300}, INLET, DATA, 2)
    assert f is not None and f.estimate
    expected = math.sqrt(7 * 1e5 / 1.2 * (1.1 ** (0.4 / 1.4) - 1))
    assert f.vectors[0][0] == pytest.approx(expected)
    g = inflow("in", {"type": "inlet_stagnation", "p0": 0.9e5, "T0": 300}, INLET, DATA, 2)
    assert g is not None and not g.known


def test_synthetic_inflow_shows_its_mean() -> None:
    f = inflow("in", {"type": "inlet_state", "u": [10.0, 0.0], "turbulence": {"intensity": 0.05}}, INLET, DATA, 2)
    assert f is not None and f.source.startswith("mean of synthetic inflow")


def test_farfield_keeps_its_inflow_part_and_walls_have_none() -> None:
    faces = Faces([(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)], [(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)], [1.0, 1.0])
    f = inflow("far", {"type": "farfield", "u": [0.2, 0.0]}, faces, DATA, 2)
    assert f is not None and f.points == [(-1.0, 0.0, 0.0)]
    assert inflow("wall", {"type": "wall_adiabatic"}, faces, DATA, 2) is None


def test_subsampling_spreads_the_arrows_along_the_zone() -> None:
    assert subsample(100, 2, 10) == list(range(5, 100, 10))
    assert len(subsample(1000, 3, None)) <= 150


def test_initial_state_reads_uniform_numbers_or_the_reference() -> None:
    assert initial_state({"initialize": {"rho": "1.0", "p": "0.714"}}) == (1.0, 0.714, 1.4)
    assert initial_state({"physics": {"rho_ref": 1.0, "p_ref": 2.0, "gamma": 1.3}}) == (1.0, 2.0, 1.3)


def test_faces_of_a_case_view_zone_have_outward_normals() -> None:
    from mallard_gui.view.scene import _read_block

    path = dict(read_vtm(FIXTURES / "cylinder_view" / "case.vtm"))["farfield"]
    block = _read_block(path)
    from vtkmodules.vtkFiltersGeometry import vtkGeometryFilter

    g = vtkGeometryFilter()
    g.SetInputData(block)
    g.Update()
    faces = faces_of(g.GetOutput())
    assert len(faces.centers) == 64
    c, n = faces.centers[0], faces.normals[0]
    assert sum(a * b for a, b in zip(c, n, strict=True)) > 0  # out of the domain at the far field


def test_the_speed_scale_is_needed_only_when_the_inflows_speeds_differ() -> None:
    a = Inflow("a", [(0.0, 0.0, 0.0)], [(2.0, 0.0, 0.0)], "uniform")
    b = Inflow("b", [(0.0, 1.0, 0.0)], [(0.0, 2.0, 0.0)], "uniform")
    slow = Inflow("c", [(0.0, 2.0, 0.0)], [(1.0, 0.0, 0.0)], "uniform")
    shape = Inflow("d", [(0.0, 3.0, 0.0)], [(5.0, 0.0, 0.0)], "profile", known=False)
    assert not legend([a, b, shape], "m/s")["varies"]  # one speed in two directions; a direction only has none
    assert legend([a, slow], "m/s")["varies"]
