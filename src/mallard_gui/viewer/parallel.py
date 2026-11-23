"""The parallel viewer job (design: Viewer, Rendering), for what must be rendered whole: a `pvserver` of N MPI
ranks reads the snapshot in parallel and composites each image with IceT, and a driver beside it
(pvdriver.py, run by `pvpython`) speaks the viewer protocol, so the GUI drives it like any viewer and only
images leave the job. N is proposed from the snapshot's cells, about one rank per 75 million (the design's 50
to 100), within the remote's `viewer.max_ranks`, and shown with its cost before the job starts.

On a remote: an allocation of N tasks, `pvserver` as a step of N tasks (rank 0 on the first node), and the
driver as a one-task step on that node, through the SSH channel like the single viewer. Here (a workstation
with ParaView): `mpiexec -n N pvserver` and the driver as local processes."""

from __future__ import annotations

import contextlib
import math
import os
import re
import shutil
import signal
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..backends.ssh import SshConnection
from ..config import Remote
from .client import START_TIMEOUT, StdioViewer, hold_allocation, slurm_resources, viewer_settings
from .engine import ViewerError

CELLS_PER_RANK = 75_000_000
MAX_RANKS = 64
JOB_NAME = "mallard-pvserver"
_HERE = Path(__file__).parent
DRIVER_FILES = ("pvdriver.py", "catalog.py", "colormaps.json")
DRIVER_DIR = ".mallard-gui/pvdriver"  # under the remote's home


@dataclass
class ParallelPlan:
    ranks: int
    cpus: int  # per rank
    hours: float  # the time limit

    @property
    def core_hours(self) -> float:
        return self.ranks * self.cpus * self.hours

    @property
    def text(self) -> str:
        return (
            f"{self.ranks} rank{'s' if self.ranks != 1 else ''} x {self.cpus} CPUs for up to "
            f"{self.hours:g} h: at most {self.core_hours:g} core-hours"
        )


def propose_ranks(cells: int, max_ranks: int = MAX_RANKS) -> int:
    return max(1, min(max_ranks, math.ceil(cells / CELLS_PER_RANK)))


def _hours(limit: str) -> float:
    """A Slurm time limit (`[D-]HH:MM:SS`, `MM:SS`, `MM`) in hours."""
    days, _, rest = limit.rpartition("-")
    parts = [float(p) for p in rest.split(":")]
    h, m, sec = ([0.0] * (3 - len(parts)) + parts) if len(parts) > 1 else (0.0, parts[0], 0.0)
    return (float(days or 0) * 24) + h + m / 60 + sec / 3600


def plan(remote: Remote | None, cells: int) -> ParallelPlan:
    s = viewer_settings(remote) if remote is not None else {"cpus": "1", "time": "01:00:00"}
    max_ranks = int(s.get("max_ranks") or MAX_RANKS)
    return ParallelPlan(propose_ranks(cells, max_ranks), int(s["cpus"]), round(_hours(s["time"]), 3))


def _bin(s: dict[str, str], name: str) -> str:
    """`viewer.paraview`: the directory of ParaView's binaries; else they are on PATH."""
    return str(Path(s["paraview"]) / name) if s.get("paraview") else name


def port_for(jobid: str) -> int:
    return 20000 + int(jobid) % 30000 if jobid.isdigit() else 11111


def allocation_command(remote: Remote, ranks: int) -> list[str]:
    s = viewer_settings(remote)
    return [
        "sbatch",
        "--parsable",
        f"--job-name={JOB_NAME}",
        f"--ntasks={ranks}",
        *slurm_resources(s),
        "--wrap",
        "sleep 2147483",
    ]


def pvserver_command(remote: Remote, jobid: str, ranks: int, port: int) -> list[str]:
    s = viewer_settings(remote)
    return [
        "srun",
        f"--jobid={jobid}",
        "--overlap",
        f"--ntasks={ranks}",
        _bin(s, "pvserver"),
        "--force-offscreen-rendering",
        f"--server-port={port}",
    ]


def driver_command(remote: Remote, jobid: str, node: str, port: int, root: str, driver_dir: str) -> list[str]:
    s = viewer_settings(remote)
    return [
        "srun",
        f"--jobid={jobid}",
        "--overlap",
        "--ntasks=1",
        f"--nodelist={node}",
        "--unbuffered",
        _bin(s, "pvpython"),
        "--force-offscreen-rendering",
        f"{driver_dir}/pvdriver.py",
        "--server",
        f"{node}:{port}",
        "--root",
        root,
        "--idle",
        s["idle"],
    ]


def install_driver(conn: SshConnection, driver_dir: str) -> None:
    for name in DRIVER_FILES:
        conn.write_file(f"{driver_dir}/{name}", (_HERE / name).read_bytes())


class ParallelViewer(StdioViewer):
    """The driver of a running pvserver; closing it stops the server and releases its allocation."""

    def __init__(
        self,
        where: str,
        argv: list[str],
        proc: subprocess.Popen[bytes],
        server: subprocess.Popen[bytes],
        release: Callable[[], None] | None = None,
        start_timeout: float = START_TIMEOUT,
        call_timeout: float = 300.0,
        on_wait: Callable[[float], None] | None = None,
    ) -> None:
        self.server = server
        self._on_close = release
        try:
            super().__init__(where, argv, proc, start_timeout, call_timeout, on_wait)
        except BaseException:
            self._stop_server()
            raise
        self.ranks = int(self.info.get("ranks", 0))

    def _stop_server(self) -> None:
        if self.server.poll() is None:
            if os.getpgid(self.server.pid) == self.server.pid:  # its own session (mpiexec here): all its ranks
                os.killpg(self.server.pid, signal.SIGTERM)
            else:
                self.server.terminate()
            try:
                self.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait()
        if self._on_close is not None:
            self._on_close()
            self._on_close = None

    def close(self) -> None:
        super().close()
        self._stop_server()


def start_parallel_viewer(
    conn: SshConnection,
    remote: Remote,
    root: str,
    ranks: int,
    driver_dir: str,
    on_wait: Callable[[float], None] | None = None,
    start_timeout: float = START_TIMEOUT,
) -> ParallelViewer:
    install_driver(conn, driver_dir)
    jobid, node = hold_allocation(conn, remote, start_timeout, command=allocation_command(remote, ranks))

    def release() -> None:
        with contextlib.suppress(OSError, RuntimeError):
            conn.run(["scancel", jobid], check=False)

    port = port_for(jobid)
    try:
        server = conn.popen(pvserver_command(remote, jobid, ranks, port), stdout=subprocess.DEVNULL)
        argv = driver_command(remote, jobid, node, port, root, driver_dir)
        proc = conn.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    except BaseException:
        release()
        raise
    return ParallelViewer(f"{remote.name} ({ranks} ranks)", argv, proc, server, release, start_timeout, on_wait=on_wait)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_paraview(path: str = "") -> Path | None:
    """ParaView's binary directory as it is found now: `path`, else $MALLARD_GUI_PARAVIEW (the directory, an
    executable in it, or the .app), else pvserver on PATH, else the newest ParaView app in /Applications
    ($MALLARD_GUI_APPLICATIONS names another directory of apps)."""
    named = os.environ.get("MALLARD_GUI_PARAVIEW", "")
    candidates = [path, named, str(Path(named).parent) if named else "", str(Path(named) / "Contents" / "bin")]
    found = shutil.which("pvserver")
    candidates.append(str(Path(found).parent) if found else "")
    apps = Path(os.environ.get("MALLARD_GUI_APPLICATIONS") or "/Applications")
    candidates += [str(p / "Contents" / "bin") for p in sorted(apps.glob("ParaView*.app"), key=_version, reverse=True)]
    return next((Path(c) for c in candidates if c and (Path(c) / "pvserver").is_file()), None)


def _version(app: Path) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", app.name)) or (0,)


def local_server_command(bindir: Path, ranks: int, port: int) -> list[str]:
    """`mpiexec -n N pvserver`; one rank runs pvserver alone, as MPI needs no launcher for it."""
    server = [str(bindir / "pvserver"), "--force-offscreen-rendering", f"--server-port={port}"]
    return server if ranks == 1 else [str(bindir / "mpiexec"), "-n", str(ranks), *server]


def start_local_parallel_viewer(
    root: str, ranks: int, paraview: Path, idle: float = 900.0, start_timeout: float = 120.0
) -> ParallelViewer:
    """On this machine: pvserver of N ranks and the driver, from ParaView's binary directory."""
    port = free_port()
    env = {**os.environ, "PYTHONNOUSERSITE": "1"}
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    server = subprocess.Popen(
        local_server_command(paraview, ranks, port),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    argv = [
        str(paraview / "pvpython"),
        "--force-offscreen-rendering",
        str(_HERE / "pvdriver.py"),
        "--server",
        f"localhost:{port}",
        "--root",
        root,
        "--idle",
        str(idle),
        "--connect-timeout",
        str(start_timeout),
    ]
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True
    )
    return ParallelViewer(f"local ({ranks} rank{'s' if ranks != 1 else ''})", argv, proc, server, None, start_timeout)


# What of a viewer's state a new one is given to show the same view: the last call of each, in this order.
_REPLAYED = ("open", "set_theme", "show", "set_field", "set_camera")


class LocalParallelViewer(ParallelViewer):
    """The parallel viewer on this machine, which outlives its ParaView: when its processes have died or the
    install they run from is gone (ParaView replaced by another version under a running GUI), the next request
    starts pvserver and the driver again from the ParaView found now and gives them the view the old ones had
    (the run, the theme, the snapshot, the field and the camera), then is answered as if nothing happened."""

    def __init__(
        self,
        root: str,
        ranks: int,
        find: Callable[[], Path | None] = find_paraview,
        start: Callable[..., ParallelViewer] | None = None,
    ) -> None:
        self._root, self._ranks, self._find = root, ranks, find
        self._begin = start or start_local_parallel_viewer
        self._state: dict[str, dict[str, Any]] = {}
        self.restarts = 0
        self._launch()

    def _launch(self) -> None:
        paraview = self._find()
        if paraview is None:
            raise ViewerError(
                "no ParaView found: install it in /Applications, or set MALLARD_GUI_PARAVIEW to its binary directory"
            )
        self.paraview = paraview
        inner = self._begin(self._root, self._ranks, paraview)
        self._inner = inner
        self.where, self.info, self.ranks = inner.where, inner.info, inner.ranks
        self.proc, self.server = inner.proc, inner.server

    @property
    def alive(self) -> bool:
        """Its driver and its server run, from an install that is still there."""
        running = self._inner.proc.poll() is None and self._inner.server.poll() is None
        return running and (self.paraview / "pvserver").is_file()

    def _restart(self) -> None:
        with contextlib.suppress(Exception):
            self._inner.close()
        self._launch()
        self.restarts += 1
        for method in _REPLAYED:
            if method in self._state:
                self._inner.call(method, **self._state[method])

    def call(self, method: str, **params: Any) -> Any:
        if not self.alive:
            self._restart()
        try:
            result = self._inner.call(method, **params)
        except ViewerError:
            if self.alive:
                raise  # the request's own error
            self._restart()
            result = self._inner.call(method, **params)
        if method in _REPLAYED:
            self._state[method] = dict(params)
        elif method in ("camera", "frame"):  # the camera as moves left it, for the next viewer
            with contextlib.suppress(ViewerError):
                camera = self._inner.call("get_camera")
                if isinstance(camera, dict):
                    self._state["set_camera"] = camera
        if method == "open":
            self._state = {"open": dict(params), **{k: v for k, v in self._state.items() if k == "set_theme"}}
        return result

    def close(self) -> None:
        self._inner.close()
