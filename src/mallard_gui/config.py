"""Per-user configuration: global remotes and settings in the user config directory.

No secrets live here: SSH keys, agents and MFA stay with OpenSSH.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

from .tomlio import dumps_sorted, read_toml, write_atomic

APP = "mallard-gui"
BACKENDS = ("local", "slurm", "pbs", "lsf", "k8s")


def config_dir() -> Path:
    override = os.environ.get("MALLARD_GUI_CONFIG_DIR")
    if override:
        return Path(override)
    if os.name == "posix" and not os.environ.get("XDG_CONFIG_HOME"):
        # The design names ~/.config/mallard-gui on Unix-likes, macOS included.
        return Path.home() / ".config" / APP
    return Path(platformdirs.user_config_dir(APP))


def cache_dir() -> Path:
    override = os.environ.get("MALLARD_GUI_CACHE_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_cache_dir(APP))


@dataclass
class Install:
    path: str
    module: str = ""
    version: str = ""
    variant: str = ""
    # Installs the GUI made: how, from which artifact (and its SHA-256 or image digest), and when.
    method: str = ""
    artifact: str = ""
    sha256: str = ""
    installed: str = ""
    # The last `mallard install verify`: "passed YYYY-MM-DD" or "failed YYYY-MM-DD", and its summary.
    verified: str = ""
    verify_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v}


@dataclass
class Remote:
    name: str
    host: str
    backend: str = "slurm"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    installs: list[Install] = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)
    storage: dict[str, str] = field(default_factory=dict)
    # Cached probe results: which remotes this one reaches directly, by their ids.
    reach: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The viewer job: python, partition, time, cpus, idle (seconds), launch (srun, or login when chosen).
    viewer: dict[str, str] = field(default_factory=dict)
    # Kubernetes: namespace, pvc, mount, prefix, node_selector, run_as_user (backends/k8s.py); `host` is the context.
    k8s: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.backend not in BACKENDS:
            raise ValueError(f"remote {self.name!r}: unknown backend {self.backend!r} (one of {', '.join(BACKENDS)})")
        if not self.host and self.backend != "local":
            raise ValueError(f"remote {self.name!r}: needs a host (an alias of ~/.ssh/config, or a kube context)")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "host": self.host, "backend": self.backend}
        if self.installs:
            out["installs"] = [i.to_dict() for i in self.installs]
        if self.defaults:
            out["defaults"] = dict(self.defaults)
        if self.storage:
            out["storage"] = dict(self.storage)
        if self.reach:
            out["reach"] = {k: dict(v) for k, v in self.reach.items()}
        if self.viewer:
            out["viewer"] = dict(self.viewer)
        if self.k8s:
            out["k8s"] = dict(self.k8s)
        return out

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> Remote:
        return cls(
            name=name,
            host=str(data.get("host", "")),
            backend=str(data.get("backend", "slurm")),
            id=str(data.get("id") or uuid.uuid4()),
            installs=[Install(**i) for i in data.get("installs", [])],
            defaults={str(k): str(v) for k, v in data.get("defaults", {}).items()},
            storage={str(k): str(v) for k, v in data.get("storage", {}).items()},
            reach={str(k): dict(v) for k, v in data.get("reach", {}).items()},
            viewer={str(k): str(v) for k, v in data.get("viewer", {}).items()},
            k8s={str(k): str(v) for k, v in data.get("k8s", {}).items()},
        )


class RemoteStore:
    """`remotes.toml`: every remote by name, each with a stable id."""

    SCHEMA = "mallard-gui-remotes"
    VERSION = 1

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "remotes.toml"
        self.remotes: dict[str, Remote] = {}
        if self.path.exists():
            data = read_toml(self.path)
            version = data.get("version", self.VERSION)
            if version > self.VERSION:
                raise ValueError(f"{self.path}: version {version} is newer than this GUI reads ({self.VERSION})")
            for name, entry in data.get("remotes", {}).items():
                self.remotes[name] = Remote.from_dict(name, entry)

    def add(self, remote: Remote) -> None:
        if remote.name in self.remotes:
            raise ValueError(f"a remote named {remote.name!r} exists")
        self.remotes[remote.name] = remote

    def update(self, name: str, remote: Remote) -> None:
        """Replace the remote named `name`, renamed when `remote.name` differs; its id stays."""
        old = self.remotes[name]
        if remote.name != name and remote.name in self.remotes:
            raise ValueError(f"a remote named {remote.name!r} exists")
        remote.id = old.id
        self.remotes = {
            (remote.name if n == name else n): (remote if n == name else r) for n, r in self.remotes.items()
        }

    def remove(self, name: str) -> Remote:
        return self.remotes.pop(name)

    def by_id(self, remote_id: str) -> Remote | None:
        return next((r for r in self.remotes.values() if r.id == remote_id), None)

    def save(self) -> None:
        data = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "remotes": {name: r.to_dict() for name, r in self.remotes.items()},
        }
        write_atomic(self.path, dumps_sorted(data, header=("Written by mallard-gui: remotes, by name. No secrets.",)))
