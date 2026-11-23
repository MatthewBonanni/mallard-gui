from __future__ import annotations

import json
from pathlib import Path

from mallard_gui.runlog import RunLogFollower, RunState

from .conftest import FIXTURES

LOG = (FIXTURES / "cylinder_run.jsonl").read_bytes()


def test_fixture_run_state() -> None:
    st = RunState()
    for line in LOG.splitlines():
        st.add(json.loads(line))
    assert st.status == "finished"
    assert st.run_id == "0abdf9d3-287d-428e-9d6f-5da82d1e8c27"
    assert not st.unsupported
    assert st.progress == 1.0
    dt = st.series["run/dt"]
    assert len(dt.t) == len(dt.values) > 1
    assert dt.t == sorted(dt.t)
    assert st.events[0]["kind"] == "start" and st.events[-1]["kind"] == "end"
    assert st.outputs


def test_units_nondimensional_are_blank() -> None:
    st = RunState()
    st.add(
        {
            "type": "start",
            "schema": "mallard-run-log",
            "version": 2,
            "units": {"dt": "s", "t": "1", "probes": {"p": "Pa"}},
        }
    )
    assert st.unit("run", "dt") == "s"
    assert st.unit("run", "t") == ""
    assert st.unit("probes", "p") == "Pa"
    st.add({"type": "progress", "t": 1.0, "dt": 0.1, "probes": {"a": {"p": 3.0}, "skip": "x"}})
    assert st.series["probes/a.p"].unit == "Pa"
    assert st.series["probes/a.p"].values == [3.0]


def test_newer_version_flagged_but_read() -> None:
    st = RunState()
    st.add({"type": "start", "schema": "mallard-run-log", "version": 99})
    st.add({"type": "progress", "t": 0.5, "dt": 0.01})
    assert "version 99" in st.unsupported
    assert st.series["run/dt"].values == [0.01]


def test_follower_holds_partial_line_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "mallard.jsonl"
    lines = LOG.splitlines(keepends=True)
    cut = len(lines[1]) // 2
    path.write_bytes(lines[0] + lines[1][:cut])
    f = RunLogFollower(path)
    assert [r["type"] for r in f.poll()] == ["start"]
    with path.open("ab") as out:
        out.write(lines[1][cut:] + lines[2])
    got = f.poll()
    assert got == [json.loads(lines[1]), json.loads(lines[2])]
    assert f.offset == len(lines[0]) + len(lines[1]) + len(lines[2])
    assert f.poll() == []


def test_follower_restarts_on_truncation(tmp_path: Path) -> None:
    path = tmp_path / "mallard.jsonl"
    path.write_bytes(LOG)
    f = RunLogFollower(path)
    f.poll()
    first = LOG.splitlines(keepends=True)[0]
    path.write_bytes(first)
    f.poll()
    assert f.state.records == 1
    assert f.state.status == "running"


def test_follower_with_remote_reader() -> None:
    reads: list[int] = []

    def read(offset: int) -> bytes:
        reads.append(offset)
        return LOG[offset:]

    f = RunLogFollower(Path("unused"), read=read)
    f.poll()
    f.poll()
    assert reads == [0, len(LOG)]
    assert f.state.status == "finished"
