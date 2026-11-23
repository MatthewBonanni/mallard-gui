"""The Run monitor's live plots: following costs no file reads, a zoom past what the histories hold reads its
window by byte range once, and a new source starts from the whole run again."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mallard_gui.data.locations import LocalLocation
from mallard_gui.history import CAP
from mallard_gui.runlog import RunLogFollower
from mallard_gui.signalsource import RunFiles
from mallard_gui.ui.monitor_plots import KEYS, MonitorPlots

N = 3 * CAP


class State(SimpleNamespace):
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)


def progress(step: int) -> str:
    return json.dumps({"type": "progress", "step": step, "t": 0.01 * step, "dt": 0.01, "cfl": 0.5 + 1e-5 * step})


@pytest.fixture
def run(tmp_path: Path) -> Path:
    start = {"type": "start", "schema": "mallard-run-log", "version": 2, "run_id": "r", "units": {"t": "s"}}
    lines = [json.dumps(start), *(progress(i) for i in range(N))]
    (tmp_path / "mallard.jsonl").write_text("\n".join(lines) + "\n")
    return tmp_path


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    got: list[tuple[int, int]] = []
    lines = RunFiles.lines

    def counted(self: RunFiles, name: str, start: int = 0, end: int = -1) -> Iterator[tuple[int, bytes]]:
        got.append((start, end))
        return lines(self, name, start, end)

    monkeypatch.setattr(RunFiles, "lines", counted)
    return got


def monitor(run: Path) -> MonitorPlots:
    state = State(plot_choice=["run/cfl", "run/dt", "", ""], plot_sources=[])
    exports = SimpleNamespace(remember=lambda *a: None)
    home = SimpleNamespace(location=LocalLocation(), root=str(run))
    session = SimpleNamespace(data=SimpleNamespace(home=lambda r: home), project=SimpleNamespace(run=lambda n: n))
    server = SimpleNamespace(trigger=lambda name: lambda f: f)
    return MonitorPlots(SimpleNamespace(state=state, server=server, session=session, exports=exports))  # type: ignore[arg-type]


def test_a_zoom_reads_its_window_once_while_the_run_goes_on(run: Path, reads: list[tuple[int, int]]) -> None:
    f = RunLogFollower(run / "mallard.jsonl")
    f.poll()
    m = monitor(run)
    m.show("r", f.state, live=True)
    assert reads == []  # following: the histories held
    m.on_range(KEYS[0], 10.0, 11.0)
    shown = m.state[KEYS[0]]["series"][0]["x"]
    assert len(shown) == 101 and reads and reads[0][1] > 0  # every sample, from a byte range
    with (run / "mallard.jsonl").open("a") as out:
        out.write("".join(progress(i) + "\n" for i in range(N, N + 50)))
    f.poll()
    m.show("r", f.state, live=True)
    assert len(reads) == 1  # the past window cannot change: not read again

    m.state.plot_choice = ["run/dt", "run/dt", "", ""]
    m.show("r", f.state, live=True)
    x = m.state[KEYS[0]]["series"][0]["x"]
    assert x[0] == 0 and x[-1] > 0.01 * N  # the whole run again
    assert m.state[KEYS[0]]["live"] and m.state[KEYS[0]]["range"] == "monitor_range"
