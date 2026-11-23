"""Runs on this machine: `mallard` as a subprocess in the run directory."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from ..mallard import THREADS
from .base import RUN_LOG, Backend, Job, JobState, RunSpec, check_request
from .staging import run_input

PID_FILE = "mallard-gui.pid"
STDOUT = "stdout.txt"


class LocalBackend(Backend):
    kind = "local"

    def __init__(self, mallard: Path, threads: int = THREADS) -> None:
        self.mallard = mallard
        self.threads = threads
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    def submit(self, spec: RunSpec) -> Job:
        run_dir = Path(spec.run_dir)
        if spec.resume:
            if not (run_dir / "input.toml").is_file():
                raise FileNotFoundError(f"{run_dir}: no input.toml to continue")
        else:
            run_dir.mkdir(parents=True, exist_ok=False)
            (run_dir / "input.toml").write_text(run_input(spec.input_file, absolute_paths=True), encoding="utf-8")
        cmd = [str(self.mallard), "-i", "input.toml", f"--kokkos-num-threads={spec.threads or self.threads}"]
        if spec.restart:
            cmd += ["--restart", spec.restart]
        for setting in spec.settings:
            cmd += ["--set", setting]
        if spec.ranks > 1:
            cmd = ["mpirun", "-n", str(spec.ranks), *cmd]
        with (run_dir / STDOUT).open("ab" if spec.resume else "wb") as out:
            # Its own session: the run outlives the GUI, which only reads its files.
            proc = subprocess.Popen(
                cmd, cwd=run_dir, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
            )
        (run_dir / PID_FILE).write_text(str(proc.pid), encoding="utf-8")
        job = Job(id=str(proc.pid), run_dir=str(run_dir), backend=self.kind)
        self._procs[job.id] = proc
        return job

    def state(self, job: Job) -> JobState:
        proc = self._procs.get(job.id)
        if proc is not None:
            code = proc.poll()
            if code is None:
                return JobState.RUNNING
            return JobState.COMPLETED if code in (0, 3) else JobState.FAILED
        # A run started by an earlier session: its end record, else whether its process lives.
        end = _end_record(Path(job.run_dir) / RUN_LOG)
        if end is not None:
            return (
                JobState.COMPLETED
                if end.get("outcome") in ("finished", "stopped", "wall_time_limit")
                else JobState.FAILED
            )
        return JobState.RUNNING if _alive(int(job.id)) else JobState.UNKNOWN

    def read_file(self, job: Job, path: str, offset: int) -> bytes:
        try:
            with (Path(job.run_dir) / path).open("rb") as f:
                f.seek(offset)
                return f.read()
        except FileNotFoundError:
            return b""

    def control(self, job: Job, request: str) -> None:
        (Path(job.run_dir) / check_request(request)).touch()

    def cancel(self, job: Job) -> None:
        proc = self._procs.get(job.id)
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)

    def wait(self, job: Job, timeout: float | None = None) -> int | None:
        proc = self._procs.get(job.id)
        return proc.wait(timeout=timeout) if proc is not None else None


def _end_record(path: Path) -> dict[str, object] | None:
    try:
        lines = path.read_bytes().splitlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "end":
            return dict(record)
        if record.get("type") == "start":
            return None
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
