from __future__ import annotations

from pathlib import Path

from mallard_gui.mallard import capabilities_from_help, coordinate_points, sample_expressions, write_set_file

from .conftest import FIXTURES


def help_page(command: str) -> str:
    return (FIXTURES / f"{command}_help.txt").read_text()


def test_capabilities_list_gpus_and_every_settable_key() -> None:
    caps = capabilities_from_help(help_page("check"), help_page("ctl"))
    assert "a100-80" in caps.gpus and "h100-80" in caps.gpus
    assert caps.settable == [
        "n_steps",
        "t_stop",
        "t_wall_stop",
        "check_interval",
        "write_data.<prefix or index>.interval",
        "write_data.<prefix or index>.time_interval",
        "probes.<name>.interval",
        "probes.<name>.time_interval",
        "forces.<zone>.interval",
        "integrals.interval",
    ]
    assert not caps.set_file
    assert capabilities_from_help("", help_page("ctl") + " A SET file in run-dir ...").set_file


def test_coordinate_points_come_from_the_boundary_details() -> None:
    b = {"details": "local coordinates: four-sided patch, (u, v) = (0, 0) at (1, -2, 0.5), (u, v) = (1, 0) at (3, -2)"}
    assert coordinate_points(b) == [("(u, v) = (0, 0)", (1.0, -2.0, 0.5)), ("(u, v) = (1, 0)", (3.0, -2.0, 0.0))]


def test_the_set_file_appears_whole(tmp_path: Path) -> None:
    write_set_file(tmp_path, {"t_stop": "3.0", "write_data.flow.interval": "50"})
    assert (tmp_path / "SET").read_text() == "t_stop = 3.0\nwrite_data.flow.interval = 50\n"
    assert not (tmp_path / "SET.new").exists()


def test_expressions_are_sampled_only_when_they_are_plain_arithmetic() -> None:
    pts = [(0.5, 2.0, 0.0)]
    assert sample_expressions(["2 * x^2 + sin(0)", 1], pts, 2) == [(0.5, 1.0, 0.0)]
    assert sample_expressions(["__import__('os')", "0"], pts, 2) is None
    assert sample_expressions(["x.real", "0"], pts, 2) is None
    assert sample_expressions(["1 / (x - 0.5)", "0"], pts, 2) is None
    assert sample_expressions(["1"], pts, 2) is None
