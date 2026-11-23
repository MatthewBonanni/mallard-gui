"""Runs on a cluster over SSH, scheduled by Slurm (PBS and LSF are adapters of this backend, in batch.py).

The connection is the user's own OpenSSH: the host is an alias of ~/.ssh/config, keys and agents
stay with OpenSSH, and every command shares one ControlMaster connection, so MFA is answered once.
Nothing is installed or left running on the cluster beyond the job itself.
"""

from __future__ import annotations

import dataclasses
import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..config import Remote, cache_dir
from .base import Backend, Job, JobState, RunSpec, check_request
from .staging import run_input


@dataclass
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", "replace")


Runner = Callable[[Sequence[str], bytes | None], Result]


def subprocess_runner(argv: Sequence[str], stdin: bytes | None) -> Result:
    proc = subprocess.run(list(argv), input=stdin, capture_output=True, timeout=120, check=False)
    return Result(proc.returncode, proc.stdout, proc.stderr)


class RemoteError(RuntimeError):
    pass


class SshConnection:
    """`ssh` to a host alias through one ControlMaster connection."""

    def __init__(
        self,
        host: str,
        runner: Runner = subprocess_runner,
        control_dir: Path | None = None,
        persist: str = "10m",
        ssh: str | None = None,
    ) -> None:
        if not host or host.startswith("-"):
            raise ValueError(f"host {host!r}: an alias of ~/.ssh/config")
        self.host = host
        self.runner = runner
        self.control_dir = control_dir or cache_dir() / "ssh"
        self.persist = persist
        # $MALLARD_GUI_SSH names another OpenSSH-compatible client (the tests' local shim).
        self.ssh = ssh or os.environ.get("MALLARD_GUI_SSH") or "ssh"

    def ssh_args(self, forward_agent: bool = False) -> list[str]:
        # %C hashes host, port and user, keeping the socket path short enough for Unix sockets.
        return [
            self.ssh,
            *(["-A"] if forward_agent else []),
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self.control_dir}/%C",
            "-o",
            f"ControlPersist={self.persist}",
            "-o",
            "BatchMode=yes",
            self.host,
        ]

    def run(self, argv: Sequence[str], stdin: bytes | None = None, check: bool = True) -> Result:
        """Run a command on the host; arguments are quoted for the remote shell."""
        self.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = self.runner([*self.ssh_args(), shlex.join(argv)], stdin)
        if check and result.returncode != 0:
            err = result.stderr.decode("utf-8", "replace").strip()
            raise RemoteError(f"{self.host}: {shlex.join(argv)}: exit {result.returncode}: {err}")
        return result

    def popen(
        self, argv: Sequence[str], stdin: int | None = None, stdout: int | None = None, forward_agent: bool = False
    ) -> subprocess.Popen[bytes]:
        """A streaming command on the host, for transfers: its standard input or output is a pipe."""
        self.control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return subprocess.Popen(
            [*self.ssh_args(forward_agent), shlex.join(argv)],
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def write_file(self, path: str, data: bytes) -> None:
        self.run(["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path], stdin=data)

    def close(self) -> None:
        self.runner([*self.ssh_args()[:-1], "-O", "exit", self.host], None)


_SLURM_STATES = {
    "PENDING": JobState.PENDING,
    "CONFIGURING": JobState.PENDING,
    "REQUEUED": JobState.PENDING,
    "RUNNING": JobState.RUNNING,
    "COMPLETING": JobState.RUNNING,
    "SUSPENDED": JobState.RUNNING,
    "COMPLETED": JobState.COMPLETED,
    "FAILED": JobState.FAILED,
    "TIMEOUT": JobState.FAILED,
    "NODE_FAIL": JobState.FAILED,
    "OUT_OF_MEMORY": JobState.FAILED,
    "BOOT_FAIL": JobState.FAILED,
    "DEADLINE": JobState.FAILED,
    "PREEMPTED": JobState.FAILED,
    "CANCELLED": JobState.CANCELLED,
}

JOB_SCRIPT = "run.sbatch"


def slurm_script(spec: RunSpec, mallard: str, module: str = "") -> str:
    """The job script, written into the run directory so it is visible and reusable without the GUI."""
    o = spec.options
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={shlex.quote(spec.name)}",
        f"#SBATCH --chdir={shlex.quote(spec.run_dir)}",
        f"#SBATCH --ntasks={spec.ranks}",
        f"#SBATCH --cpus-per-task={spec.threads}",
        "#SBATCH --output=stdout-%j.txt",
    ]
    for key in ("partition", "account", "time", "gres", "qos", "constraint"):
        if o.get(key):
            lines.append(f"#SBATCH --{key}={shlex.quote(o[key])}")
    lines += ["", "set -euo pipefail"]
    if module:
        lines.append(module)
    lines += [
        "export OMP_PROC_BIND=spread OMP_PLACES=threads",
        f"srun {shlex.quote(mallard)} -i input.toml --kokkos-num-threads={spec.threads}"
        + (f" --restart {shlex.quote(spec.restart)}" if spec.restart else "")
        + "".join(f" --set {shlex.quote(x)}" for x in spec.settings),
        "",
    ]
    return "\n".join(lines)


def slurm_queue(conn: SshConnection, user: str | None = None) -> list[dict[str, str]]:
    """The user's jobs: id, name, state, time, reason (squeue)."""
    argv = ["squeue", "-h", "-o", "%i|%j|%T|%M|%R"]
    argv += ["-u", user] if user else ["--me"]
    rows = []
    for line in conn.run(argv).text.splitlines():
        parts = line.split("|")
        if len(parts) == 5:
            rows.append(dict(zip(("id", "name", "state", "time", "reason"), parts, strict=True)))
    return rows


class SlurmBackend(Backend):
    kind = "slurm"

    job_script_name = JOB_SCRIPT

    def __init__(self, remote: Remote, connection: SshConnection | None = None) -> None:
        if remote.backend != self.kind:
            raise ValueError(f"remote {remote.name}: backend {remote.backend}, not {self.kind}")
        if not remote.installs:
            raise ValueError(f"remote {remote.name}: no Mallard install recorded")
        self.remote = remote
        self.conn = connection or SshConnection(remote.host)

    @property
    def install(self) -> tuple[str, str]:
        install = self.remote.installs[0]
        return install.path, install.module

    def submit(self, spec: RunSpec) -> Job:
        run_dir = PurePosixPath(spec.run_dir)
        if not run_dir.is_absolute():
            raise ValueError(f"run directory {spec.run_dir!r}: absolute on the remote")
        mallard, module = self.install
        options = {**self.remote.defaults, **spec.options}
        spec = dataclasses.replace(spec, options=options)
        # TODO: the transfer engine (M1 item 6) stages the files the input reads; until then the case's
        # mesh and data must already be on the remote at the input's paths.
        if not spec.resume:
            input_text = run_input(spec.input_file, absolute_paths=False)
            self.conn.write_file(str(run_dir / "input.toml"), input_text.encode())
        script = run_dir / self.job_script_name
        self.conn.write_file(str(script), self.job_script(spec, mallard, module).encode())
        return Job(id=self.submit_script(str(script)), run_dir=str(run_dir), backend=self.kind)

    def job_script(self, spec: RunSpec, mallard: str, module: str) -> str:
        return slurm_script(spec, mallard, module)

    def submit_script(self, path: str) -> str:
        """Submit the job script; the scheduler's job id."""
        out = self.conn.run(["sbatch", "--parsable", path]).text.strip()
        job_id = out.split(";", 1)[0]
        if not job_id.isdigit():
            raise RemoteError(f"sbatch: unexpected reply {out!r}")
        return job_id

    def state(self, job: Job) -> JobState:
        queued = self.conn.run(["squeue", "-h", "-j", job.id, "-o", "%T"], check=False).text.split()
        if queued:
            return _SLURM_STATES.get(queued[0], JobState.UNKNOWN)
        acct = self.conn.run(["sacct", "-n", "-X", "-P", "-j", job.id, "-o", "State"], check=False).text.split()
        if acct:
            # "CANCELLED by 1234" -> CANCELLED
            return _SLURM_STATES.get(acct[0].split()[0], JobState.UNKNOWN)
        return JobState.UNKNOWN

    def queue(self, user: str | None = None) -> list[dict[str, str]]:
        return slurm_queue(self.conn, user)

    def read_file(self, job: Job, path: str, offset: int) -> bytes:
        full = str(PurePosixPath(job.run_dir) / path)
        result = self.conn.run(["sh", "-c", 'test -f "$1" && tail -c +"$2" "$1" || true', "sh", full, str(offset + 1)])
        return result.stdout

    def control(self, job: Job, request: str) -> None:
        self.conn.run(["touch", str(PurePosixPath(job.run_dir) / check_request(request))])

    def cancel(self, job: Job) -> None:
        self.conn.run(["scancel", job.id])
