"""Probe and force CSV files followed by byte offset, locally and over SSH, and their plots."""

from __future__ import annotations

import json
from pathlib import Path

from mallard_gui.backends.base import Job
from mallard_gui.backends.ssh import SlurmBackend, SshConnection
from mallard_gui.config import Install, Remote
from mallard_gui.csvseries import CsvFollower, CsvSeries, csv_files
from mallard_gui.runlog import RunLogFollower
from mallard_gui.ui.monitor_plots import default_plots, plot_sources, profile_spec, signals_of

FAKE_SSH = str(Path(__file__).parent / "fixtures" / "fake-ssh")
PROVENANCE = '# mallard-run {"schema":"mallard-run","version":3}\n'

POINT = (
    "step,t,point,x,y,U_Y,P\n"
    "# units: 1,s,1,m,m,m s-1,kg m-1 s-2\n" + PROVENANCE + '# mallard-probe-sampling {"probe":"wake"}\n'
    "0,0,0,2.5,0,0.1,0.7\n"
    "0,0,1,3.5,0,0.2,0.8\n"
    "6,0.1,0,2.5,0,0.3,0.7\n"
    "6,0.1,1,3.5,0,0.4,0.8\n"
)
# A continuation appends its own header lines and comments before its rows.
CONTINUED = "step,t,point,x,y,U_Y,P\n# units: 1,s,1,m,m,m s-1,kg m-1 s-2\n" + PROVENANCE + "12,0.2,0,2.5,0,0.5,0.7\n"
WALL = (
    "step,t,face,x,y,s,nx,ny,area,CP,CF\n"
    "# units: 1,s,1,m,m,m,1,1,m,1,1\n"
    "0,0,0,0.5,0,0.0,1,0,0.1,1.0,0.01\n"
    "0,0,1,0.4,0.3,0.5,1,0,0.1,0.9,0.02\n"
    "20,1,0,0.5,0,0.0,1,0,0.1,0.8,0.03\n"
    "20,1,1,0.4,0.3,0.5,1,0,0.1,0.7,0.04\n"
)
INPUT = """
[[probes]]
name = "wake"
file = "./solut/probe_wake.csv"
[[probes]]
name = "arc"
file = "./solut/probe_arc.pvd"
reductions_file = "./solut/probe_arc_reductions.csv"
[[probes]]
name = "wall"
file = "./solut/probe_wall.csv"
[[forces]]
file = "./solut/forces_cylinder.csv"
"""


def feed(text: str, s: CsvSeries | None = None) -> CsvSeries:
    s = s or CsvSeries("x.csv")
    for line in text.splitlines():
        s.add_line(line)
    return s


def test_point_probe_series_per_point_with_units_across_a_continuation() -> None:
    s = feed(CONTINUED, feed(POINT))
    assert s.variables == ["U_Y", "P"]
    assert s.unit("U_Y") == "m s-1" and s.unit("t") == "s"
    assert s.points() == [0, 1]
    assert s.series[("U_Y", 0)].recent() == ([0.0, 0.1, 0.2], [0.1, 0.3, 0.5])
    assert s.series[("U_Y", 1)].recent() == ([0.0, 0.1], [0.2, 0.4])


def test_a_probes_sample_geometry_is_no_variable_in_either_spelling() -> None:
    new = feed("step,t,face,x,y,N_X,N_Y,AREA,COUNT,MEAN_P")  # Mallard's columns
    old = feed("step,t,face,x,y,nx,ny,area,P")  # before #1467
    assert (new.variables, old.variables) == (["MEAN_P"], ["P"])


def test_boundary_probe_keeps_its_latest_sample_as_a_profile() -> None:
    s = feed(WALL)
    assert s.profile
    assert s.t == 1.0
    assert s.column("s") == [0.0, 0.5]
    assert s.column("CP") == [0.8, 0.7]


def test_follower_waits_for_a_whole_line_and_restarts_on_truncation() -> None:
    data = bytearray(POINT[: POINT.index("6,0.1,0") + 5].encode())  # ends inside a row
    f = CsvFollower("x.csv", lambda off: b"\0TRUNCATED" if off > len(data) else bytes(data[off:]))
    f.poll()
    assert f.data.series[("U_Y", 0)].recent() == ([0.0], [0.1])
    data += POINT.encode()[len(data) :]
    f.poll()
    assert f.data.series[("U_Y", 0)].recent() == ([0.0, 0.1], [0.1, 0.3])
    data[:] = WALL.encode()[:10]  # a fresh run in the same place
    f.poll()
    assert f.data.columns == [] and f.offset == 10


def test_csv_files_named_by_the_input() -> None:
    assert csv_files(INPUT) == [
        "./solut/probe_wake.csv",
        "./solut/probe_arc_reductions.csv",
        "./solut/probe_wall.csv",
        "./solut/forces_cylinder.csv",
    ]
    assert csv_files("not = [toml") == []


def write_run(run: Path) -> None:
    (run / "solut").mkdir(parents=True)
    (run / "input.toml").write_text(INPUT)
    start = {"type": "start", "schema": "mallard-run-log", "version": 2, "run_id": "r1", "units": {}}
    (run / "mallard.jsonl").write_text(json.dumps(start) + "\n")
    (run / "solut" / "probe_wake.csv").write_text(POINT)
    (run / "solut" / "probe_wall.csv").write_text(WALL)


def test_run_follower_reads_the_csv_files_locally(tmp_path: Path) -> None:
    write_run(tmp_path / "run")
    f = RunLogFollower(tmp_path / "run" / "mallard.jsonl")
    f.poll()
    assert sorted(f.state.csv) == ["./solut/probe_wake.csv", "./solut/probe_wall.csv"]
    with (tmp_path / "run" / "solut" / "probe_wake.csv").open("a") as out:
        out.write(CONTINUED)
    f.poll()
    assert f.state.csv["./solut/probe_wake.csv"].series[("U_Y", 0)].t == [0.0, 0.1, 0.2]


def test_run_follower_reads_the_csv_files_over_ssh(tmp_path: Path) -> None:
    run = tmp_path / "run"
    write_run(run)
    remote = Remote("hpc", "hpc", installs=[Install("/opt/mallard")])
    backend = SlurmBackend(remote, SshConnection("hpc", control_dir=tmp_path / "ctl", ssh=FAKE_SSH))
    job = Job("1", str(run), "slurm")
    f = RunLogFollower(Path("mallard.jsonl"), read_file=lambda p, off: backend.read_file(job, p, off))
    f.poll()
    assert f.state.csv["./solut/probe_wall.csv"].column("CF") == [0.03, 0.04]
    assert backend.read_file(job, "solut/none.csv", 0) == b""


def test_plots_default_to_the_csv_files(tmp_path: Path) -> None:
    write_run(tmp_path / "run")
    f = RunLogFollower(tmp_path / "run" / "mallard.jsonl")
    f.poll()
    sources = plot_sources(f.state)
    assert sources["csv:./solut/probe_wall.csv"] == "probe_wall · profile at t 1"
    defaults = default_plots(list(sources), {"csv:./solut/probe_wall.csv"})
    assert defaults[:3] == ["csv:./solut/probe_wake.csv", "", "csv:./solut/probe_wall.csv"]
    profile = profile_spec(f.state.csv["./solut/probe_wall.csv"])
    assert profile.xlabel == "s (m)" and "CP" in [c.label for c in profile.curves]
    series = signals_of(f.state, "csv:./solut/probe_wake.csv")
    assert {(s.label, s.unit) for s in series} >= {("U_Y [1]", "m s-1"), ("P [0]", "kg m-1 s-2")}
