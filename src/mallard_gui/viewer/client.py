"""The GUI's side of the viewer: a viewer process on this machine for local runs, or a viewer job on a remote
reached through the user's SSH master connection. Both speak the same protocol over their standard input and
output, so the page above cannot tell them apart, and nothing a remote viewer reads is written to this
machine's disk: frames arrive in memory. The local viewer is a process of its own, not a thread of the GUI:
VTK's window must be made on its process's main thread (macOS enforces it), and a crash in a reader stays
out of the GUI."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import selectors
import shlex
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from ..backends.ssh import RemoteError, SshConnection
from ..config import Remote
from .engine import ViewerError
from .protocol import NAME, VERSION

VIEWER_DEFAULTS = {"time": "01:00:00", "cpus": "2", "idle": "900", "launch": "auto"}
START_TIMEOUT = 600.0  # the queue wait of the viewer job
CALL_TIMEOUT = 120.0


class Viewer(ABC):
    where: str

    @abstractmethod
    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One request's response object."""

    def call(self, method: str, **params: Any) -> Any:
        response = self.request(method, params)
        if "error" in response:
            raise ViewerError(str(response["error"]))
        result = response.get("result")
        if isinstance(result, dict) and isinstance(result.get("image"), str):
            result["image"] = base64.b64decode(result["image"])
        return result

    def close(self) -> None:
        return None


def viewer_settings(remote: Remote) -> dict[str, str]:
    """The remote's viewer settings over the defaults; the viewer's Python is the uv environment beside the
    remote's Mallard install unless `viewer.python` names another."""
    s = {**VIEWER_DEFAULTS, **remote.viewer}
    if not s.get("python") and remote.installs:
        install = PurePosixPath(remote.installs[0].path)
        root = install.parent.parent if install.parent.name == "bin" else install.parent
        s["python"] = str(root / "viewer" / "bin" / "python")
    s.setdefault("partition", remote.viewer.get("partition") or remote.defaults.get("partition", ""))
    s.setdefault("account", remote.defaults.get("account", ""))
    return s


LAUNCHES = ("srun", "allocation", "node", "login")
# Tried in this order when `launch` is auto; the login node only when the user chooses it, each time.
AUTO_LAUNCHES = ("srun", "allocation", "node")
RENDER_WINDOWS = {"egl": "vtkEGLRenderWindow", "osmesa": "vtkOSOpenGLRenderWindow"}


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def render_backend(s: dict[str, str]) -> str:
    """How the viewer draws: EGL on a GPU node (`gpu = true`), else what `render` says (auto: VTK picks,
    OSMesa where there is no display)."""
    render = s.get("render", "auto")
    if render == "auto" and _truthy(s.get("gpu", "")):
        return "egl"
    if render not in ("auto", *RENDER_WINDOWS):
        raise ViewerError(f"viewer render {render!r}: auto, egl or osmesa")
    return render


def slurm_resources(s: dict[str, str]) -> list[str]:
    argv = [f"--cpus-per-task={s['cpus']}", f"--time={s['time']}"]
    if s.get("partition"):
        argv.append(f"--partition={s['partition']}")
    if s.get("account"):
        argv.append(f"--account={s['account']}")
    if _truthy(s.get("gpu", "")):
        argv.append(f"--gpus={s.get('gpus') or '1'}")
    return argv


def viewer_command(remote: Remote, root: str, launch: str = "", jobid: str = "", node: str = "") -> list[str]:
    """The viewer, its protocol on standard input and output, so no port is ever forwarded:
    - `srun`: one task of its own under Slurm on the viewer partition;
    - `allocation`: a step in an allocation the GUI holds (`srun --jobid --overlap`), where a site refuses
      interactive jobs but allows steps of batch ones;
    - `node`: through the login node to the allocation's compute node (`ssh NODE`), where steps are refused
      but compute nodes accept SSH from the login node;
    - `login`: on the login node itself, only when the user chooses it.
    Each exits after its idle timeout or when its input closes."""
    s = viewer_settings(remote)
    launch = launch or (s["launch"] if s["launch"] in LAUNCHES else "srun")
    if s["launch"] not in (*LAUNCHES, "auto"):
        raise ViewerError(f"remote {remote.name}: viewer launch {s['launch']!r}: auto, {', '.join(LAUNCHES)}")
    if not s.get("python"):
        raise ViewerError(f"remote {remote.name}: no viewer Python (set viewer.python, or record an install)")
    viewer = [s["python"], "-m", "mallard_gui.viewer", "--stdio", "--root", root, "--idle", s["idle"]]
    render = render_backend(s)
    if render != "auto":
        viewer = ["env", f"VTK_DEFAULT_OPENGL_WINDOW={RENDER_WINDOWS[render]}", *viewer]
    if launch == "login":
        return viewer
    if launch == "node":
        if not node:
            raise ViewerError("viewer on a compute node: no node allocated")
        return ["ssh", "-o", "BatchMode=yes", "-T", node, shlex.join(viewer)]
    if launch == "allocation":
        if not jobid:
            raise ViewerError("viewer in an allocation: no allocation held")
        return ["srun", f"--jobid={jobid}", "--overlap", "--ntasks=1", "--unbuffered", *viewer]
    return ["srun", f"--job-name={NAME}", "--ntasks=1", *slurm_resources(s), "--unbuffered", *viewer]


def allocation_command(remote: Remote) -> list[str]:
    """A batch job that only holds the viewer's allocation until its time limit or a cancel."""
    s = viewer_settings(remote)
    return ["sbatch", "--parsable", f"--job-name={NAME}", "--ntasks=1", *slurm_resources(s), "--wrap", "sleep 2147483"]


class ViewerUnavailable(ViewerError):
    """No launch reached a viewer: each attempt with why it failed; the login node and batch images remain."""

    def __init__(self, where: str, attempts: list[tuple[str, str]]) -> None:
        self.attempts = attempts
        tried = "; ".join(f"{launch}: {why}" for launch, why in attempts)
        super().__init__(f"no viewer on {where} ({tried})")


class StdioViewer(Viewer):
    """A viewer process speaking the protocol on its standard input and output."""

    def __init__(
        self,
        where: str,
        argv: list[str],
        proc: subprocess.Popen[bytes],
        start_timeout: float,
        call_timeout: float,
        on_wait: Callable[[float], None] | None,
    ) -> None:
        self.where = where
        self.argv = argv
        self.proc = proc
        self.call_timeout = call_timeout
        self._lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=40)
        self._buffer = b""
        self._next = 1
        self._drain = threading.Thread(target=self._read_stderr, daemon=True)
        self._drain.start()
        self.info = self._hello(start_timeout, on_wait)

    @property
    def command(self) -> str:
        return shlex.join(self.argv)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line.decode("utf-8", "replace").rstrip())

    def _readline(self, timeout: float, on_wait: Callable[[float], None] | None = None) -> bytes:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        start = time.monotonic()
        try:
            while b"\n" not in self._buffer:
                waited = time.monotonic() - start
                if waited > timeout:
                    raise ViewerError(f"viewer on {self.where}: no answer in {timeout:.0f} s{self._why()}")
                if not sel.select(timeout=min(1.0, timeout - waited)):
                    if on_wait:
                        on_wait(waited)
                    continue
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    self.proc.wait(timeout=5)
                    raise ViewerError(f"viewer on {self.where} exited ({self.proc.returncode}){self._why()}")
                self._buffer += chunk
        finally:
            sel.close()
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    def _why(self) -> str:
        time.sleep(0.05)
        tail = [line for line in self._stderr if line.strip()]
        return f": {tail[-1]}" if tail else ""

    def _hello(self, timeout: float, on_wait: Callable[[float], None] | None) -> dict[str, Any]:
        line = self._readline(timeout, on_wait)
        try:
            info: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as e:
            self.close()
            raise ViewerError(f"viewer on {self.where}: unexpected first line {line[:80]!r}") from e
        if info.get("hello") != NAME or int(info.get("version", 0)) != VERSION:
            self.close()
            raise ViewerError(
                f"viewer on {self.where}: protocol {info.get('hello')} {info.get('version')}, not {VERSION}"
            )
        return info

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.proc.poll() is not None:
                raise ViewerError(f"viewer on {self.where} has exited{self._why()}")
            rid = self._next
            self._next += 1
            assert self.proc.stdin is not None
            try:
                self.proc.stdin.write(json.dumps({"id": rid, "method": method, "params": params}).encode() + b"\n")
                self.proc.stdin.flush()
            except BrokenPipeError as e:
                raise ViewerError(f"viewer on {self.where} has exited{self._why()}") from e
            while True:
                response: dict[str, Any] = json.loads(self._readline(self.call_timeout))
                if response.get("id") == rid:
                    return response

    def close(self) -> None:
        """End the viewer: its input closes, and it exits (for a remote job, Slurm ends the step with it)."""
        if self.proc.stdin:
            with contextlib.suppress(BrokenPipeError, OSError):
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()


class LocalViewer(StdioViewer):
    """A viewer process on this machine, reading local runs in place."""

    def __init__(
        self, root: Path, idle: float = 900.0, start_timeout: float = 60.0, call_timeout: float = CALL_TIMEOUT
    ) -> None:
        argv = [sys.executable, "-m", "mallard_gui.viewer", "--stdio", "--root", str(root), "--idle", str(idle)]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        super().__init__("local", argv, proc, start_timeout, call_timeout, None)


class RemoteViewer(StdioViewer):
    """A viewer job on a remote, its protocol carried over the SSH channel's standard input and output."""

    def __init__(
        self,
        conn: SshConnection,
        remote: Remote,
        root: str,
        start_timeout: float = START_TIMEOUT,
        call_timeout: float = CALL_TIMEOUT,
        on_wait: Callable[[float], None] | None = None,
        launch: str = "",
        jobid: str = "",
        node: str = "",
    ) -> None:
        self.conn = conn
        self.launch = launch or "srun"
        self.jobid = ""  # an allocation this viewer holds once it started, cancelled when it closes
        argv = viewer_command(remote, root, launch, jobid, node)
        proc = conn.popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        super().__init__(remote.name, argv, proc, start_timeout, call_timeout, on_wait)
        self.jobid = jobid

    def _release(self) -> None:
        if self.jobid:
            with contextlib.suppress(OSError, RuntimeError):
                self.conn.run(["scancel", self.jobid], check=False)
            self.jobid = ""

    def close(self) -> None:
        super().close()
        self._release()


def first_host(nodelist: str) -> str:
    """The first host of a Slurm node list: `nid[0012-0015,0020],gpu3` -> `nid0012`."""
    head = nodelist.strip()
    depth = 0
    for i, ch in enumerate(head):
        depth += (ch == "[") - (ch == "]")
        if ch == "," and depth == 0:
            head = head[:i]
            break
    prefix, bracket, rest = head.partition("[")
    if not bracket:
        return prefix
    first = rest.split(",")[0].split("-")[0].rstrip("]")
    return prefix + first + rest.partition("]")[2]


def hold_allocation(
    conn: SshConnection, remote: Remote, timeout: float, poll: float = 2.0, command: list[str] | None = None
) -> tuple[str, str]:
    """Submit the allocation job (the viewer's, unless `command` is another) and wait until it runs: its id
    and its first node."""
    out = conn.run(command or allocation_command(remote)).text.strip()
    jobid = out.split(";")[0].strip()
    if not jobid.isdigit():
        raise ViewerError(f"sbatch: unexpected answer {out[:80]!r}")
    end = time.monotonic() + timeout
    missing = 0
    while True:
        state = conn.run(["squeue", "-h", "-j", jobid, "-o", "%T|%N"], check=False).text.strip()
        status, _, nodes = state.partition("|")
        if status == "RUNNING" and nodes:
            return jobid, first_host(nodes)
        missing = missing + 1 if not status else 0
        if status in ("FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL") or missing >= 3:
            raise ViewerError(f"allocation {jobid}: {status or 'gone from the queue'}")
        if time.monotonic() > end:
            conn.run(["scancel", jobid], check=False)
            raise ViewerError(f"allocation {jobid}: still {status or 'pending'} after {timeout:.0f} s")
        time.sleep(poll)


def start_remote_viewer(
    conn: SshConnection,
    remote: Remote,
    root: str,
    on_wait: Callable[[float], None] | None = None,
    login: bool = False,
    start_timeout: float = START_TIMEOUT,
) -> RemoteViewer:
    """The viewer job by the first launch that works, in the design's order for blocked forwarding: a job of
    its own, a step in a held allocation, the allocation's node through the login node; the login node only
    with `login` (the user's explicit choice). Raises ViewerUnavailable listing every attempt."""
    s = viewer_settings(remote)
    if login:
        return RemoteViewer(conn, remote, root, start_timeout, on_wait=on_wait, launch="login")
    launches = AUTO_LAUNCHES if s["launch"] == "auto" else (s["launch"],)
    attempts: list[tuple[str, str]] = []
    held: tuple[str, str] | None = None
    for launch in launches:
        try:
            if launch in ("allocation", "node"):
                if held is None:
                    held = hold_allocation(conn, remote, start_timeout)
                jobid, node = held
                viewer = RemoteViewer(
                    conn, remote, root, start_timeout, on_wait=on_wait, launch=launch, jobid=jobid, node=node
                )
                held = None  # the viewer owns the allocation now
                return viewer
            return RemoteViewer(conn, remote, root, start_timeout, on_wait=on_wait, launch=launch)
        except (ViewerError, RemoteError, OSError) as e:
            attempts.append((launch, str(e).split(": ", 1)[-1][:200]))
    if held is not None:
        conn.run(["scancel", held[0]], check=False)
    raise ViewerUnavailable(remote.name, attempts)
