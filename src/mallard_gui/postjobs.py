"""Post-processing as jobs where the run is: `mallard diff` of two snapshots, `mallard post average` over a
time window and `mallard post extract` of a region's snapshot (a slice of a volume too large to view whole).
Each reads O(cells) data, so on a remote it runs under Slurm (`srun`, with the viewer's partition and
account), never on the login node; here, as a process. Results are Mallard's own JSON (`--format json`);
the files they write land in the run's `average/` and `extract/`, where the viewer finds them as derived
series. The GUI parses no human-readable output."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backends.ssh import SshConnection
from .config import Remote
from .viewer.client import slurm_resources, viewer_settings

KINDS = ("diff", "average", "extract")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Exit statuses that still carry a report: diff's 1 is "a difference beyond tolerance". A diff field's
# status is "ok" (identical), "warning" (within tolerance) or "error" (beyond it).
REPORTING = {"diff": (0, 1), "average": (0,), "extract": (0,)}


def _name(name: str) -> str:
    if not _NAME.match(name):
        raise ValueError(f"output name {name!r}: letters, digits, '.', '_' and '-'")
    return name


def _fields(fields: Sequence[str]) -> list[str]:
    return ["--fields", ",".join(fields)] if fields else []


def diff_args(
    a: str, b: str, fields: Sequence[str] = (), tol: float | None = None, step: int | None = None
) -> list[str]:
    """Compare snapshot files A and B (paths relative to the run directory, or absolute)."""
    args = ["diff", a, b, *_fields(fields), "--format", "json"]
    if tol is not None:
        args += ["--tol", repr(float(tol))]
    if step is not None:
        args += ["--step", str(int(step))]
    return args


def average_args(
    files: Sequence[str], t0: float | None, t1: float | None, fields: Sequence[str] = (), name: str = "average"
) -> list[str]:
    """Time averages of the snapshots of `files` with t0 <= t <= t1, written to `average/NAME.vtkhdf`."""
    args = ["post", "average", *files, *_fields(fields), "--name", _name(name), "-o", "average", "--format", "json"]
    if t0 is not None and t1 is not None:
        args += ["--times", f"{float(t0)!r}:{float(t1)!r}"]
    return args


def plane_region(origin: Sequence[float], normal: Sequence[float]) -> str:
    """A cut plane as the inline TOML region `post extract --region` takes."""
    o = ", ".join(repr(float(c)) for c in origin)
    n = ", ".join(repr(float(c)) for c in normal)
    return f'{{type = "cut_plane", point = [{o}], normal = [{n}]}}'


def extract_args(
    file: str, step: int | None, region: str, fields: Sequence[str] = (), name: str = "extract"
) -> list[str]:
    """A snapshot of a region (a zone, a named region, or an inline table such as a cut plane) at one step,
    written to `extract/NAME.vtkhdf`: data the size of the region, made by Mallard in parallel."""
    args = ["post", "extract", file, "--region", region, "--snapshot", "vtkhdf", *_fields(fields)]
    if step is not None:
        args += ["--steps", str(int(step))]
    return [*args, "--name", _name(name), "-o", "extract", "--format", "json"]


@dataclass
class PostJob:
    kind: str
    run: str
    where: str
    args: list[str]
    id: str = ""
    state: str = "pending"  # pending | running | done | failed | cancelled
    exit_status: int | None = None
    report: dict[str, Any] | None = None
    error: str = ""
    command: str = ""
    started: float = 0.0
    seconds: float = 0.0
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "run": self.run,
            "where": self.where,
            "state": self.state,
            "exit_status": self.exit_status,
            "error": self.error,
            "command": self.command,
            "seconds": round(self.seconds, 1),
            "summary": summary(self),
        }


def summary(job: PostJob) -> str:
    """One line from the job's JSON report."""
    r = job.report or {}
    if job.kind == "diff" and r:
        fields = r.get("fields", [])
        beyond = [f["name"] for f in fields if f.get("status") == "error"]
        within = [f["name"] for f in fields if f.get("status") == "warning"]
        if beyond:
            return f"beyond tolerance: {', '.join(beyond)} (of {len(fields)} fields)"
        if within:
            return f"within tolerance: {', '.join(within)} differ (of {len(fields)} fields)"
        return f"identical: {len(fields)} fields bit for bit"
    if r:
        return "written"
    return job.error


class Launcher:
    """How a job's command runs where the run is."""

    where = "local"

    def command(self, mallard_args: Sequence[str], cwd: str, ranks: int) -> list[str]:
        raise NotImplementedError

    def start(self, argv: Sequence[str]) -> subprocess.Popen[bytes]:
        raise NotImplementedError


class LocalLauncher(Launcher):
    def __init__(self, mallard: Path, threads: int = 2) -> None:
        self.mallard = mallard
        self.threads = threads

    def command(self, mallard_args: Sequence[str], cwd: str, ranks: int) -> list[str]:
        self.cwd = cwd
        return [str(self.mallard), *mallard_args, f"--kokkos-num-threads={self.threads}"]

    def start(self, argv: Sequence[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            list(argv), cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )


class SlurmLauncher(Launcher):
    """`srun` from the login node, in the run directory, with the viewer's partition and account."""

    def __init__(self, conn: SshConnection, remote: Remote) -> None:
        if not remote.installs:
            raise ValueError(f"remote {remote.name}: no Mallard install recorded")
        self.conn = conn
        self.remote = remote
        self.where = remote.name

    def command(self, mallard_args: Sequence[str], cwd: str, ranks: int) -> list[str]:
        install = self.remote.installs[0]
        s = viewer_settings(self.remote)
        srun = ["srun", "--job-name=mallard-post", f"--ntasks={max(1, ranks)}", *slurm_resources(s)]
        run = shlex.join([*srun, install.path, *mallard_args, f"--kokkos-num-threads={s['cpus']}"])
        module = f"{install.module} && " if install.module else ""
        return ["sh", "-c", f"cd {shlex.quote(cwd)} && {module}{run}"]

    def start(self, argv: Sequence[str]) -> subprocess.Popen[bytes]:
        proc = self.conn.popen(list(argv), stdout=subprocess.PIPE)
        return proc


def _end(proc: subprocess.Popen[bytes]) -> None:
    """End a job's process and what it started (a local process group; over SSH, the client, whose channel
    closing ends the Slurm step)."""
    import os
    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()


class PostJobs:
    """The jobs of a session, run one thread each; cancelling ends the process (and with it, the Slurm step)."""

    def __init__(self) -> None:
        self.jobs: dict[str, PostJob] = {}
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()
        self._next = 1

    def submit(
        self,
        job: PostJob,
        launcher: Launcher,
        cwd: str,
        ranks: int = 1,
        done: Callable[[PostJob], None] | None = None,
    ) -> PostJob:
        if job.kind not in KINDS:
            raise ValueError(f"job kind {job.kind!r}: one of {', '.join(KINDS)}")
        with self._lock:
            job.id = f"{job.kind}-{self._next}"
            self._next += 1
            self.jobs[job.id] = job
        argv = launcher.command(job.args, cwd, ranks)
        job.command = shlex.join(argv)
        job.where = launcher.where
        thread = threading.Thread(target=self._run, args=(job, launcher, argv, done), daemon=True)
        thread.start()
        return job

    def _run(self, job: PostJob, launcher: Launcher, argv: list[str], done: Callable[[PostJob], None] | None) -> None:
        if job.state == "pending":
            job.state = "running"
        job.started = time.monotonic()
        try:
            proc = launcher.start(argv)
            self._procs[job.id] = proc
            if job.state == "cancelled":
                _end(proc)
            out, err = proc.communicate()
            job.exit_status = proc.returncode
            job.log = err.decode("utf-8", "replace").splitlines()[-20:]
            if job.state == "cancelled":
                return
            if proc.returncode in REPORTING[job.kind]:
                try:
                    job.report = json.loads(out)
                    job.state = "done"
                except json.JSONDecodeError:
                    job.state, job.error = "failed", "no JSON report on standard output"
            else:
                job.state = "failed"
                job.error = next((ln for ln in reversed(job.log) if ln.strip()), f"exit status {proc.returncode}")
        except OSError as e:
            job.state, job.error = "failed", str(e)
        finally:
            job.seconds = time.monotonic() - job.started
            self._procs.pop(job.id, None)
            if done is not None:
                done(job)

    def cancel(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job is None or job.state not in ("pending", "running"):
            return
        job.state = "cancelled"
        proc = self._procs.get(job_id)
        if proc is not None:
            _end(proc)

    def wait(self, job_id: str, timeout: float = 60.0) -> PostJob:
        end = time.monotonic() + timeout
        job = self.jobs[job_id]
        while job.state in ("pending", "running") and time.monotonic() < end:
            time.sleep(0.05)
        return job
