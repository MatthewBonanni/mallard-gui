"""The parallel viewer job (design: Viewer, Rendering): the ranks proposed from the cells and their cost, the
Slurm allocation with pvserver and the driver as steps of it, ParaView's own MPI here, and every rank stopped
on close. With $MALLARD_GUI_PARAVIEW naming ParaView's binary directory, the real driver renders a run."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from mallard_gui.config import Install, Remote
from mallard_gui.project import RunRecord
from mallard_gui.ui.results_page import ResultsPage
from mallard_gui.viewer.engine import ViewerError
from mallard_gui.viewer.parallel import (
    DRIVER_FILES,
    ParallelPlan,
    allocation_command,
    driver_command,
    local_server_command,
    plan,
    propose_ranks,
    pvserver_command,
    start_local_parallel_viewer,
    start_parallel_viewer,
)

from .runs import make_run
from .test_remotes import stub
from .test_viewer_launch import cluster, conn

__all__ = ["cluster"]

# Stands in for pvdriver.py: the hello with the server's ranks, then each request echoed back.
FAKE_DRIVER = """\
import json, os, sys
args = sys.argv[1:]
open(os.path.join(os.environ.get("HOME", "."), "driver-args"), "w").write(" ".join(args))
print(json.dumps({"hello": "mallard-viewer", "version": 1, "pid": os.getpid(), "ranks": 3}), flush=True)
for line in sys.stdin:
    r = json.loads(line)
    print(json.dumps({"id": r["id"], "result": {"method": r["method"], **r["params"]}}), flush=True)
"""


def remote(**viewer: str) -> Remote:
    return Remote("hpc", "hpc", installs=[Install("/opt/m/bin/mallard")], viewer=viewer)


def paraview_stubs(bindir: Path, tmp: Path) -> None:
    """pvpython runs the fake driver; pvserver and mpiexec record how they were called and wait."""
    (tmp / "fake_driver.py").write_text(FAKE_DRIVER)
    stub(bindir / "pvpython", f'[ "$1" = --force-offscreen-rendering ] && shift\nshift\nexec {sys.executable} '
         f'{tmp / "fake_driver.py"} "$@"')  # fmt: skip
    stub(bindir / "pvserver", f'echo "pvserver $*" >> {tmp / "server.log"}\nexec sleep 300')
    # mpiexec -n N CMD...: N ranks of CMD, in the background, as MPI's launcher does.
    stub(
        bindir / "mpiexec",
        f'n=$2; shift 2\necho "mpiexec -n $n" >> {tmp / "server.log"}\n'
        f'i=0; while [ $i -lt $n ]; do "$@" & echo $! >> {tmp / "ranks.pid"}; i=$((i+1)); done\nwait',
    )


def alive(pid: int) -> bool:
    """Running: a zombie no init reaps (a container's) has stopped."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not stat.exists() or stat.read_text().rpartition(")")[2].split()[0] != "Z"


def test_ranks_come_from_the_cells_within_the_remotes_limit_with_their_cost() -> None:
    assert propose_ranks(1) == 1
    assert propose_ranks(1_000_000_000) == 14  # about one rank per 75 M cells
    p = plan(remote(cpus="4", time="1-02:30:00", max_ranks="8"), 1_000_000_000)
    assert (p.ranks, p.cpus, p.hours) == (8, 4, 26.5)
    assert p.text == "8 ranks x 4 CPUs for up to 26.5 h: at most 848 core-hours"
    assert plan(None, 10).text.startswith("1 rank x ")


def test_pvserver_and_the_driver_are_steps_of_one_allocation() -> None:
    r = remote(cpus="4", time="02:00:00", partition="viz", paraview="/opt/pv/bin", idle="600")
    alloc = allocation_command(r, 8)
    assert "--ntasks=8" in alloc and "--cpus-per-task=4" in alloc and "--partition=viz" in alloc
    assert alloc[-2:] == ["--wrap", "sleep 2147483"]
    server = pvserver_command(r, "77", 8, 20077)
    assert server[:5] == ["srun", "--jobid=77", "--overlap", "--ntasks=8", "/opt/pv/bin/pvserver"]
    assert "--server-port=20077" in server
    driver = driver_command(r, "77", "node71", 20077, "/", ".d")
    assert "--ntasks=1" in driver and "--nodelist=node71" in driver and "/opt/pv/bin/pvpython" in driver
    assert driver[driver.index("--server") + 1] == "node71:20077"
    assert local_server_command(Path("/pv"), 1, 9)[0] == "/pv/pvserver"
    assert local_server_command(Path("/pv"), 4, 9)[:3] == ["/pv/mpiexec", "-n", "4"]


def test_a_remote_parallel_viewer_installs_its_driver_and_releases_the_allocation_on_close(cluster: Path) -> None:
    paraview_stubs(cluster / "pv", cluster)
    r = remote(paraview=str(cluster / "pv"), python=sys.executable)
    viewer = start_parallel_viewer(conn(cluster), r, "/", 2, ".d", start_timeout=20)
    home = cluster / "homes" / "hpc"
    assert sorted(p.name for p in (home / ".d").iterdir()) == sorted(DRIVER_FILES)
    assert viewer.ranks == 3 and viewer.where == "hpc (2 ranks)"
    assert viewer.call("info") == {"method": "info"}
    assert "--server node71:" in (home / "driver-args").read_text()
    viewer.close()
    assert "77" in (home / "scancel.log").read_text()
    assert viewer.server.poll() is not None


@pytest.mark.parametrize("ranks", [1, 3])
def test_a_local_parallel_viewer_stops_every_rank_on_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ranks: int
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # the fake driver notes its arguments in its home: not the user's
    paraview_stubs(tmp_path / "pv", tmp_path)
    viewer = start_local_parallel_viewer(str(tmp_path), ranks, tmp_path / "pv", start_timeout=20)
    end = time.monotonic() + 10
    while (tmp_path / "server.log").read_text().count("pvserver") < ranks and time.monotonic() < end:
        time.sleep(0.05)
    log = (tmp_path / "server.log").read_text()
    assert log.count("pvserver --force-offscreen-rendering --server-port=") == ranks
    assert ("mpiexec -n 3" in log) == (ranks == 3)
    pids = [int(p) for p in (tmp_path / "ranks.pid").read_text().split()] if ranks > 1 else [viewer.server.pid]
    assert all(alive(p) for p in pids)
    viewer.close()
    end = time.monotonic() + 10
    while any(alive(p) for p in pids) and time.monotonic() < end:
        time.sleep(0.05)
    assert not any(alive(p) for p in pids)


@pytest.mark.skipif(not os.environ.get("MALLARD_GUI_PARAVIEW"), reason="set MALLARD_GUI_PARAVIEW to ParaView's bin")
def test_paraview_renders_a_run_composited_from_its_ranks(tmp_path: Path) -> None:
    run = make_run(tmp_path / "run", steps=2, n=8)
    ranks = int(os.environ.get("MALLARD_GUI_PARAVIEW_RANKS", "2"))
    viewer = start_local_parallel_viewer(str(tmp_path), ranks, Path(os.environ["MALLARD_GUI_PARAVIEW"]))
    try:
        assert viewer.ranks == ranks
        opened = viewer.call("open", run=str(run))
        volume = next(s for s in opened["series"] if s["kind"] == "volume")
        info = viewer.call("show", series=volume["id"], index=1)
        assert info["ranks"] == ranks and info["cells"] == 8**3 and info["count"] == 2
        assert info["field"] == "P" and info["data_range"] == pytest.approx([1 + 1 / 16, 2 - 1 / 16])
        viewer.call("set_theme", theme="light")
        before = viewer.call("frame", width=320, height=200)
        assert (before["width"], before["height"], before["mime"]) == (320, 200, "image/png")
        viewer.call("camera", op="orbit", dx=0.25, dy=0.1)
        after = viewer.call("frame", width=320, height=200)
        assert base64.b64decode(after["image"]) != base64.b64decode(before["image"])
        cam = viewer.call("get_camera")
        assert viewer.call("set_camera", **cam)["position"] == pytest.approx(cam["position"])
    finally:
        viewer.close()


class _State(dict[str, Any]):
    def __getattr__(self, k: str) -> Any:
        if k == "change":  # trame's decorator for state watchers
            return lambda *_: lambda f: f
        return self[k]

    def __setattr__(self, k: str, v: Any) -> None:
        self[k] = v

    def __enter__(self) -> _State:
        return self

    def __exit__(self, *_: object) -> None:
        pass


class _Session:
    def __init__(self) -> None:
        self.started: list[int] = []
        self.project = MagicMock(data={}, run=lambda name: RunRecord(name=name, home={}))

    def parallel_plan(self, record: RunRecord, cells: int) -> ParallelPlan:
        return plan(None, cells)

    def parallel_viewer(self, record: RunRecord, ranks: int, on_wait: object = None) -> tuple[object, str]:
        self.started.append(ranks)
        raise ViewerError("no pvserver here")


def test_the_results_page_offers_a_volume_over_budget_whole_and_starts_the_job_planned() -> None:
    session = _Session()
    app = MagicMock(state=_State(), session=session, _tasks=set())
    page = ResultsPage(app)
    app.state.v_run = "big-1"
    asyncio.run(page._offer_whole(200_000_000))
    assert app.state.v_whole == {"ranks": 3, "text": plan(None, 200_000_000).text}
    asyncio.run(page._open("big-1", ranks=app.state.v_whole["ranks"]))
    assert session.started == [3] and app.state.v_error == "no pvserver here"
    assert app.state.v_whole == {}
    asyncio.run(page._offer_whole(0))
    assert app.state.v_whole == {}


class _Driverless:
    """Answers as the parallel viewer's driver does for a method it lacks (pvdriver.serve)."""

    def call(self, method: str, **_: Any) -> Any:
        raise ViewerError(f"the parallel viewer has no {method!r}")


def test_compare_and_statistics_say_when_the_viewer_lacks_them() -> None:
    app = MagicMock(state=_State(), session=_Session(), _tasks=set())
    page = ResultsPage(app)
    page.viewer = _Driverless()  # type: ignore[assignment]
    app.state.v_info = {"field": "P", "fields": [{"name": "P"}, {"name": "U", "components": 3}], "colormap": "Fast"}
    asyncio.run(page._frame2(64, 48, False))
    assert app.state.v_split_note == "this viewer draws one field at a time" and app.state.v_frame2 == ""
    asyncio.run(page._stats(app.state.v_info))
    assert app.state.v_stats["error"] == "this viewer does not compute statistics"


# A driver that answers like the real one's camera and writes every request it gets to its install's log.
LOGGING_DRIVER = """\
import json, os, sys
log = open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "requests.log"), "a")
camera = {"position": [0, 0, 1], "focal_point": [0, 0, 0], "view_up": [0, 1, 0]}
print(json.dumps({"hello": "mallard-viewer", "version": 1, "pid": os.getpid(), "ranks": 1}), flush=True)
for line in sys.stdin:
    r = json.loads(line)
    log.write(json.dumps([r["method"], r["params"]]) + "\\n"); log.flush()
    if r["method"] == "set_camera": camera = r["params"]
    if r["method"] == "camera": camera = {**camera, "position": [3, 4, 5]}
    result = camera if r["method"] in ("get_camera", "set_camera") else {"method": r["method"], **r["params"]}
    print(json.dumps({"id": r["id"], "result": result}), flush=True)
"""


def fake_paraview(apps: Path, version: str) -> Path:
    """A ParaView app of `version` in `apps`: its pvserver waits, its pvpython runs the logging driver."""
    bindir = apps / f"ParaView-{version}.app" / "Contents" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "driver.py").write_text(LOGGING_DRIVER)
    stub(bindir / "pvpython", f'[ "$1" = --force-offscreen-rendering ] && shift\nshift\nexec {sys.executable} '
         f'{bindir / "driver.py"} "$@"')  # fmt: skip
    stub(bindir / "pvserver", "exec sleep 300")
    return bindir


def requests(bindir: Path) -> list[list[Any]]:
    import json

    return [json.loads(line) for line in (bindir / "requests.log").read_text().splitlines()]


@pytest.mark.parametrize("lost", ["install", "processes"])
def test_a_local_parallel_viewer_starts_again_on_the_paraview_found_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost: str
) -> None:
    """ParaView replaced under a running GUI (6.0.1 by 6.2.1): the viewer's processes died or their install was
    gone, and every request failed until the run was opened again. The next request starts pvserver and the
    driver from the ParaView found now, with the view the old ones had."""
    import shutil
    import signal

    from mallard_gui.viewer.parallel import LocalParallelViewer

    apps = tmp_path / "Applications"
    monkeypatch.setenv("MALLARD_GUI_APPLICATIONS", str(apps))
    monkeypatch.delenv("MALLARD_GUI_PARAVIEW", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    old = fake_paraview(apps, "6.0.1")
    viewer = LocalParallelViewer(str(tmp_path), 1)
    try:
        assert viewer.paraview == old
        viewer.call("open", run="runs/a")
        viewer.call("set_theme", theme="light")
        viewer.call("show", series="solut/box", index=3)
        viewer.call("set_field", field="P", colormap="Cool to Warm")
        viewer.call("camera", op="orbit", dx=0.1, dy=0.0)
        first = [viewer.proc.pid, viewer.server.pid]
        new = fake_paraview(apps, "6.2.1")
        if lost == "install":
            shutil.rmtree(apps / "ParaView-6.0.1.app")
        else:
            os.killpg(viewer.proc.pid, signal.SIGKILL)
            viewer.proc.wait(timeout=10)
        answer = viewer.call("frame", width=640, height=480)
        assert answer == {"method": "frame", "width": 640, "height": 480}  # answered, by the new one
        assert viewer.restarts == 1 and viewer.paraview == new and viewer.proc.pid not in first
        asked = [r for r in requests(new) if r[0] != "get_camera"]
        assert asked == [
            ["open", {"run": "runs/a"}],
            ["set_theme", {"theme": "light"}],
            ["show", {"series": "solut/box", "index": 3}],
            ["set_field", {"field": "P", "colormap": "Cool to Warm"}],
            ["set_camera", {"position": [3, 4, 5], "focal_point": [0, 0, 0], "view_up": [0, 1, 0]}],
            ["frame", {"width": 640, "height": 480}],
        ]
        end = time.monotonic() + 10
        while any(alive(p) for p in first) and time.monotonic() < end:
            time.sleep(0.05)
        assert not any(alive(p) for p in first)  # the old ones are stopped
        assert viewer.call("info") == {"method": "info"} and viewer.restarts == 1
    finally:
        viewer.close()


def test_a_local_parallel_viewer_says_when_no_paraview_is_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from mallard_gui.viewer.parallel import LocalParallelViewer

    apps = tmp_path / "Applications"
    monkeypatch.setenv("MALLARD_GUI_APPLICATIONS", str(apps))
    monkeypatch.delenv("MALLARD_GUI_PARAVIEW", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    fake_paraview(apps, "6.0.1")
    viewer = LocalParallelViewer(str(tmp_path), 1)
    shutil.rmtree(apps)
    with pytest.raises(ViewerError, match="no ParaView found"):
        viewer.call("frame", width=64, height=64)
    viewer.close()
