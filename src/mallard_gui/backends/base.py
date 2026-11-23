"""One interface for every place a case runs: local, SSH with Slurm (PBS and LSF later), Kubernetes later."""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

CONTROL_FILES = ("STOP", "CHECKPOINT")
RUN_LOG = "mallard.jsonl"


class JobState(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def active(self) -> bool:
        return self in (JobState.PENDING, JobState.RUNNING)


@dataclass
class RunSpec:
    """What to run: the case's input on the user's machine and where the run directory goes."""

    name: str
    input_file: Path
    run_dir: str
    ranks: int = 1
    threads: int = 2
    options: dict[str, str] = field(default_factory=dict)  # partition, account, time, ...
    # `--restart FILE`. With `resume`, the run continues in its own directory with its own input:
    # the run log, the CSV files and the snapshot series are appended to.
    restart: str = ""
    resume: bool = False
    settings: list[str] = field(default_factory=list)  # `--set KEY=VALUE`, before the first step


@dataclass
class Job:
    id: str
    run_dir: str
    backend: str


class Backend(ABC):
    kind: str

    @abstractmethod
    def submit(self, spec: RunSpec) -> Job:
        """Stage the input, write the job's files into the run directory and start it."""

    @abstractmethod
    def state(self, job: Job) -> JobState: ...

    @abstractmethod
    def read_file(self, job: Job, path: str, offset: int) -> bytes:
        """The bytes of a file in the run directory from `offset` on; empty if it does not exist yet."""

    def read_log(self, job: Job, offset: int) -> bytes:
        return self.read_file(job, RUN_LOG, offset)

    @abstractmethod
    def control(self, job: Job, request: str) -> None:
        """Write a control file (`STOP`, `CHECKPOINT`) into the run directory."""

    @abstractmethod
    def cancel(self, job: Job) -> None:
        """End the job at once, without the run's own stop (a restart is not written)."""

    def stop(self, job: Job) -> None:
        self.control(job, "STOP")

    def checkpoint(self, job: Job) -> None:
        self.control(job, "CHECKPOINT")


def check_request(request: str) -> str:
    if request not in CONTROL_FILES:
        raise ValueError(f"control request {request!r}: one of {', '.join(CONTROL_FILES)}")
    return request
