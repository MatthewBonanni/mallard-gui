"""Where runs run: one interface, a backend per kind of place."""

from ..config import Remote
from .base import Backend, Job, JobState, RunSpec
from .local import LocalBackend
from .ssh import SlurmBackend, SshConnection

__all__ = ["Backend", "Job", "JobState", "LocalBackend", "RunSpec", "SlurmBackend", "SshConnection", "backend_for"]


def backend_for(remote: Remote) -> Backend:
    """The backend of a remote, by its `backend`."""
    from .batch import LsfBackend, PbsBackend
    from .k8s import K8sBackend

    kinds: dict[str, type[Backend]] = {
        "slurm": SlurmBackend,
        "pbs": PbsBackend,
        "lsf": LsfBackend,
        "k8s": K8sBackend,
    }
    if remote.backend not in kinds:
        raise ValueError(f"remote {remote.name}: backend {remote.backend} runs nothing")
    return kinds[remote.backend](remote)  # type: ignore[call-arg]
