"""Signals of a run read by byte range, force coefficients with units, and line and wall extraction through
`mallard post extract`."""

from __future__ import annotations

import io
import json
import math
import tomllib
from pathlib import Path

import pytest

from mallard_gui.coefficients import Reference, coefficients, default_reference, parse_reference
from mallard_gui.data.locations import LocalLocation
from mallard_gui.extract import (
    convert_values,
    line_args,
    probe_file,
    read_profile,
    sources,
    unit_choices,
    wall_args,
    wall_input,
)
from mallard_gui.history import History
from mallard_gui.runlog import RunLogFollower
from mallard_gui.signalsource import RunFiles, catalog, curve, export_csv, find, samples

FORCES_HEAD = "step,t,Fx_pressure,Fy_pressure,Fx_viscous,Fy_viscous\n# units: 1,s,N,N,N,N\n"
PROBE_HEAD = 'step,t,point,x,y,U_Y,P\n# units: 1,s,1,m,m,m s-1,Pa\n# mallard-run {"schema":"mallard-run"}\n'


def _run(tmp_path: Path, rows: int = 20_000) -> Path:
    """A run directory with a long probe file (more rows than a history's finest level keeps) and a run log."""
    run = tmp_path / "run"
    (run / "solut").mkdir(parents=True)
    with (run / "solut" / "probe_wake.csv").open("w") as f:
        f.write(PROBE_HEAD)
        for i in range(rows):
            t = 0.1 * i
            f.write(f"{i},{t!r},0,2.5,0,{math.sin(2 * math.pi * 0.2 * t)!r},{1 + 0.01 * i!r}\n")
    (run / "input.toml").write_text('[[probes]]\nname = "wake"\nfile = "./solut/probe_wake.csv"\n', encoding="utf-8")
    log = [{"type": "start", "schema": "mallard-run-log", "version": 2, "run_id": "r", "units": {"t": "s", "dt": "s"}}]
    log += [{"type": "progress", "step": i, "t": float(i), "dt": 1.0 + i, "cfl": 0.5} for i in range(30)]
    (run / "mallard.jsonl").write_text("".join(json.dumps(r) + "\n" for r in log), encoding="utf-8")
    return run


def _follow(run: Path) -> RunLogFollower:
    f = RunLogFollower(run / "mallard.jsonl")
    f.poll()
    return f


def test_catalog_lists_csv_columns_and_run_log_numbers(tmp_path: Path) -> None:
    st = _follow(_run(tmp_path, rows=100)).state
    ids = [s.id for s in catalog(st)]
    assert "csv:./solut/probe_wake.csv#U_Y#0" in ids and "log:run/dt" in ids
    sig = find(st, "csv:./solut/probe_wake.csv#U_Y#0")
    assert sig is not None and sig.unit == "m s-1" and sig.group == "probe_wake"


def test_a_window_past_what_memory_holds_is_read_by_byte_range(tmp_path: Path) -> None:
    run = _run(tmp_path)
    st = _follow(run).state
    files = RunFiles(LocalLocation(), str(run))
    sig = find(st, "csv:./solut/probe_wake.csv#U_Y#0")
    assert sig is not None
    t, y = samples(st, sig, files, 100.0, 110.0)
    assert len(t) == 101 and t[0] == pytest.approx(100.0) and y[50] == pytest.approx(math.sin(2 * math.pi * 0.2 * 105))
    x, _ = curve(st, sig, files, 100.0, 101.0, max_points=2000)
    assert len(x) == 11  # every sample of the zoomed window, though memory holds coarse bins there
    log = find(st, "log:run/dt")
    assert log is not None
    lt, ly = samples(st, log, files, 3.0, 5.0)
    assert lt == [3.0, 4.0, 5.0] and ly == [4.0, 5.0, 6.0]


def test_export_writes_every_sample_with_units(tmp_path: Path) -> None:
    run = _run(tmp_path, rows=5000)
    st = _follow(run).state
    files = RunFiles(LocalLocation(), str(run))
    sig = find(st, "csv:./solut/probe_wake.csv#P#0")
    assert sig is not None
    out = io.StringIO()
    assert export_csv(st, [sig], files, out) == 5000
    lines = out.getvalue().splitlines()
    assert lines[0] == "t,probe_wake.P" and lines[1] == "# units: s,Pa" and len(lines) == 5002


def test_history_means_are_bin_averages() -> None:
    h = History(cap=4, factor=2)
    for i in range(16):
        h.append(float(i), float(i))
    _, m, n = h.means(max_points=4)
    assert sum(a * b for a, b in zip(m, n, strict=True)) == pytest.approx(sum(range(16)))


@pytest.mark.parametrize("name", ["F_{A}_{P}", "F{a}_{p}"])  # Mallard's columns, and before #1467
def test_force_coefficients_with_reference_in_units(name: str) -> None:
    ref, errors = parse_reference(
        {"rho": "1.2 kg/m^3", "velocity": "36 km/h", "length": "50 cm", "alpha": "90"}, nondimensional=False
    )
    assert errors == {} and ref.velocity == pytest.approx(10.0) and ref.length == pytest.approx(0.5)
    t = [0.0, 1.0]
    values = {("x", "pressure"): [1.0, 2.0], ("y", "pressure"): [30.0, 30.0], ("x", "viscous"): [0.0, 0.0]}
    values[("y", "viscous")] = [0.0, 0.0]
    columns = {name.format(A=a.upper(), P=p.upper(), a=a, p=p): v for (a, p), v in values.items()}
    c = coefficients(t, columns, ref, 2)
    assert c["Cd_pressure"] == c["Cd"]
    q = 0.5 * 1.2 * 100
    assert c["Cd"][0] == pytest.approx(30.0 / (q * 0.5))  # α = 90°: drag along +y
    assert c["Cl"][1] == pytest.approx(-2.0 / (q * 0.5))
    _, bad = parse_reference({"rho": "3 m/s", "length": "-1"}, nondimensional=False)
    assert "not a unit" in bad["rho"] and "positive" in bad["length"]
    _, nd = parse_reference({"rho": "1 kg/m^3"}, nondimensional=True)
    assert "nondimensional" in nd["rho"]


def test_default_reference_comes_from_the_inputs_output_reference() -> None:
    d = default_reference({"output": {"reference": {"rho": 1.0, "u": [0.2, 0.0], "p": 0.71}}})
    assert d["rho"] == "1.0" and float(d["velocity"]) == pytest.approx(0.2) and float(d["alpha"]) == 0.0
    assert Reference(rho=2, velocity=3).q == pytest.approx(9.0)


def test_extraction_arguments_are_mallards_own() -> None:
    args = line_args("solut/restart_001000.restart", 1000, [0.5, 0.0], [10.0, 0.0], 100, ["U_X", "P"], "wake")
    assert args[:3] == ["post", "extract", "solut/restart_001000.restart"]
    assert args[4] == '{type = "line", start = [0.5, 0.0], end = [10.0, 0.0], n_points = 100}'
    assert args[-6:] == ["--name", "wake", "-o", "extract", "--format", "json"]
    with pytest.raises(ValueError):
        line_args("f", None, [0, 0], [0, 0], 10, [], "x")
    text = wall_input("[run]\nt_stop = 1.0\n", "wall_10", "cylinder", ["CP", "Y_PLUS"])
    probe = tomllib.loads(text)["probes"][0]
    assert probe == {
        "name": "wall_10",
        "boundary": "cylinder",
        "fields": ["CP", "Y_PLUS"],
        "sampling": "point",
        "file": "probe_wall_10.csv",
    }
    assert wall_args("r.restart", 10, "extract/wall_10.toml", "wall_10")[3:7] == [
        "-i",
        "extract/wall_10.toml",
        "--probe",
        "wall_10",
    ]
    with pytest.raises(ValueError):
        wall_args("f", None, "i.toml", "bad name!")


def test_sources_are_restarts_and_whole_mesh_snapshots() -> None:
    outs = [
        {"kind": "vtu", "path": "./solut/cylinder_000001.vtu", "t": 1},
        {"kind": "restart", "path": "./solut/restart_000100.restart", "step": 100, "t": 20.0},
        {"kind": "vtkhdf", "path": "./solut/flow.vtkhdf", "step": 200, "t": 40.0},
    ]
    got = sources(outs, {"last_restart": {"path": "./solut/restart_000300.restart", "step": 300, "t": 60.0}})
    assert [s["path"] for s in got] == [
        "./solut/restart_000300.restart",
        "./solut/flow.vtkhdf",
        "./solut/restart_000100.restart",
    ]
    assert probe_file(None, "wall") == "extract/probe_wall.csv"
    assert probe_file({"written": [{"path": "extract/solut/probe_wall.csv"}]}, "wall") == "extract/solut/probe_wall.csv"


def test_wall_profile_with_arc_length_and_unit_conversion() -> None:
    text = (
        "step,t,face,x,y,s,nx,ny,area,P,CP\n# units: 1,s,1,m,m,m,1,1,m,kg m-1 s-2,1\n"
        "10,1.0,0,0.5,0,0,1,0,0.1,100,0.5\n10,1.0,1,0.4,0.3,0.31,1,0,0.1,200,0.2\n"
        "20,2.0,0,0.5,0,0,1,0,0.1,110,0.4\n20,2.0,1,0.4,0.3,0.31,1,0,0.1,210,0.1\n"
    )
    p = read_profile(text)
    assert p.x_name == "s" and p.step == 20 and p.columns["P"] == [110, 210] and p.steps == [10, 20]
    assert read_profile(text, step=10).columns["CP"] == [0.5, 0.2]
    assert unit_choices("kg m-1 s-2")[:2] == ["kg m-1 s-2", "Pa"] and "kPa" in unit_choices("kg m-1 s-2")
    assert convert_values([1500.0], "kg m-1 s-2", "kPa") == [pytest.approx(1.5)]
    assert convert_values([0.25], "m", "mm") == [pytest.approx(250.0)]
    assert unit_choices("1") == []


def test_line_profile_is_distance_along_the_line() -> None:
    text = "step,t,point,x,y,U_X\n# units: 1,s,1,m,m,m s-1\n5,1,0,0,0,1\n5,1,1,3,4,2\n"
    p = read_profile(text)
    assert p.x_name == "distance" and p.x == [0.0, 5.0]
