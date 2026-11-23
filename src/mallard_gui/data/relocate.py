"""Continuing a run elsewhere and moving a run to another remote.

Continuing elsewhere moves the case's inputs and the run's newest restart, never the snapshots, and checks the
restart at the destination before anything else moves or runs: the destination's `mallard info RESTART` must
read the file and find in it what the run's home finds (format version, dimension, precision, cells, fields,
species). The rank count does not matter: a restart reads bitwise the same on any number of ranks.

Moving a run copies all of it (the restart first, so a destination that cannot read it stops the move before
the snapshots) and then changes its home in `mallard-runs/<run>.toml`, the only committed change. The old home
is left as it was; cleaning it is a separate, explicit step listing what it deletes.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..backends import backend_for
from ..backends.base import Backend, Job, RunSpec
from ..backends.k8s import k8s_settings
from ..backends.staging import input_files
from ..config import Remote
from ..mallard import Exec, MallardError, file_info, local_exec
from ..project import RunRecord, sha256_file
from .locations import LOCAL, LocalLocation, Location, ShellLocation, TransferError, sha256_of
from .manager import DataManager
from .manifest import OBJECT, FileEntry
from .transfer import Endpoint, Plan, Report

# What a restart must hold the same wherever it is read, from `mallard info --format json`.
FACTS = ("format", "version", "dim", "precision", "cells", "fields", "species", "step", "t")


class RelocateError(RuntimeError):
    pass


@dataclass
class RestartCheck:
    path: str
    destination: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "destination": self.destination,
            "ok": self.ok,
            "problems": self.problems,
            "notes": self.notes,
            "facts": self.facts,
        }


def restart_facts(info: dict[str, Any]) -> dict[str, Any]:
    snaps = info.get("snapshots") or [{}]
    return {
        "format": info.get("format"),
        "version": info.get("version"),
        "dim": info.get("dim"),
        "precision": info.get("precision"),
        "cells": (info.get("mesh") or {}).get("cells"),
        "fields": [f.get("name") for f in info.get("fields", [])],
        "species": list(info.get("species") or []),
        "step": snaps[-1].get("step"),
        "t": snaps[-1].get("t"),
    }


def compare_restart(path: str, destination: str, home: dict[str, Any], there: dict[str, Any]) -> RestartCheck:
    """The destination's reading of a restart against its home's: they must agree on every fact."""
    a, b = restart_facts(home), restart_facts(there)
    problems = []
    if b["format"] != "restart":
        problems.append(f"{destination} reads it as {b['format']!r}, not a restart file")
    for key in FACTS:
        if a[key] != b[key] and key != "format":
            problems.append(f"{key}: {_short(b[key])} at {destination}, {_short(a[key])} where it ran")
    notes = [str(n) for n in there.get("notes", [])]
    # TODO(#1318): `mallard info RESTART` says whether this build can continue from it (precision, species of
    # its mechanism); until then reading it with the same facts is the check.
    return RestartCheck(path, destination, not problems, problems, notes, b)


def _short(value: Any) -> str:
    if isinstance(value, list):
        return f"{len(value)} ({', '.join(map(str, value[:4]))}{', ...' if len(value) > 4 else ''})"
    return str(value)


def location_exec(location: Location) -> Exec:
    if isinstance(location, LocalLocation):
        return local_exec
    assert isinstance(location, ShellLocation)
    shell = location.conn

    def run(argv: Sequence[str]) -> tuple[int, bytes, bytes]:
        r = shell.run(argv, check=False)
        return r.returncode, r.stdout, r.stderr

    return run


def mallard_command(remote: Remote | None, local_mallard: Path) -> list[str]:
    """How `mallard` is invoked at a location: here, a remote's recorded install (after its module line), or
    the image's `mallard` in a Kubernetes helper pod."""
    if remote is None:
        return [str(local_mallard)]
    if remote.backend == "k8s":
        return [k8s_settings(remote).get("mallard", "mallard")]
    if not remote.installs:
        raise RelocateError(f"remote {remote.name}: no Mallard install recorded")
    install = remote.installs[0]
    if install.module:
        return ["sh", "-c", f'{install.module} >/dev/null 2>&1; exec "$0" "$@"', install.path]
    return [install.path]


@dataclass
class Continuation:
    record: RunRecord
    job: Job
    backend: Backend
    check: RestartCheck


class Relocator:
    """Continue elsewhere and move, on the data manager's locations and transfer engine. Each call is long
    (transfers): the UI runs it on a worker thread; `progress` reports bytes done, total and the current file."""

    def __init__(
        self,
        data: DataManager,
        local_mallard: Path,
        backend: Callable[[Remote], Backend] = backend_for,
        local_backend: Backend | None = None,
    ) -> None:
        self.data = data
        self.local_mallard = local_mallard
        self.backend = backend
        self.local_backend = local_backend

    def _remote(self, name: str) -> Remote | None:
        return None if name == LOCAL else self.data.remotes.remotes[name]

    def _run(self, plan: Plan, progress: Callable[[int, int, str], None] | None) -> Report:
        report = self.data.engine.run(plan, progress)
        if report.errors:
            raise RelocateError(f"transfer to {plan.destination.label}: {report.errors[0]}")
        return report

    def newest_restart(self, record: RunRecord) -> FileEntry:
        self.data.refresh(record, names=[])
        chosen = self.data.select(record, "restart", path=str(record.last_restart.get("path", "")).removeprefix("./"))
        chosen = chosen or self.data.select(record, "restart")
        if not chosen:
            raise RelocateError(f"run {record.name}: no restart file to continue from")
        return chosen[0]

    def check_restart(
        self, record: RunRecord, dst: str, progress: Callable[[int, int, str], None] | None = None
    ) -> tuple[RestartCheck, str]:
        """Bring the run's newest restart to `dst` (resumable, verified by SHA-256) and have the destination's
        Mallard read it. Returns the check and the restart's path there."""
        entry = self.newest_restart(record)
        plan = self.data.plan(record, [entry], dst)
        self._run(plan, progress)
        home = self.data.home(record)
        there = plan.destination.path(entry.path)
        try:
            home_info = file_info(
                location_exec(home.location),
                mallard_command(self._remote(home.location.name), self.local_mallard),
                home.path(entry.path),
            )
        except MallardError as e:
            raise RelocateError(f"{home.location.label(home.path(entry.path))}: {e}") from e
        try:
            dst_info = file_info(
                location_exec(plan.destination.location),
                mallard_command(self._remote(dst), self.local_mallard),
                there,
            )
        except MallardError as e:
            return RestartCheck(there, dst, False, [f"{dst} cannot read it: {str(e).splitlines()[0]}"]), there
        return compare_restart(there, dst, home_info, dst_info), there

    def stage_inputs(
        self, input_file: Path, dst: Endpoint, progress: Callable[[int, int, str], None] | None = None
    ) -> None:
        """The files the case's input names beside it (its mesh, data tables) into the new run directory, by
        content: objects the destination already holds are skipped."""
        base = input_file.parent.resolve()
        entries = []
        for rel in input_files(input_file):
            p = base / rel
            entries.append(FileEntry(rel, OBJECT, p.stat().st_size, sha256_of(p), category="input"))
        if not entries:
            return
        source = Endpoint(LocalLocation(), str(base))
        plan = self.data.engine.plan(entries, [source], dst, "stream" if dst.location.kind != "local" else "local")
        self._run(plan, progress)

    def continue_elsewhere(
        self,
        record: RunRecord,
        dst: str,
        input_file: Path,
        name: str,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> Continuation:
        """A new run of the case at `dst` from `record`'s newest restart, checked there first."""
        check, restart = self.check_restart(record, dst, progress)
        if not check.ok:
            raise RelocateError(
                f"{dst} cannot continue from {posixpath.basename(restart)}: {'; '.join(check.problems)}"
            )
        remote = self._remote(dst)
        if remote is None:
            run_dir = str(self.data.project.root / "runs" / name)
            directory = f"runs/{name}"
            backend = self.local_backend
            if backend is None:
                raise RelocateError("no local backend")
        else:
            root = self.data.copy_root(RunRecord(name), dst)
            if root is None:
                raise RelocateError(f"{dst}: no run root (set run_root for {dst} in Remotes)")
            run_dir = directory = root
            backend = self.backend(remote)
        endpoint = Endpoint(self.data.location(dst), run_dir)
        if remote is not None:
            self.stage_inputs(input_file, endpoint, progress)
        job = backend.submit(RunSpec(name, input_file, run_dir, restart=restart))
        home = {"remote": dst, "directory": directory}
        if remote is not None:
            home["remote_id"] = remote.id
        new = RunRecord(
            name=name,
            home=home,
            input=input_file.name,
            input_sha256=sha256_file(input_file),
            mallard=_install_version(remote),
            job=job.id,
        )
        self.data.project.save_run(new)
        return Continuation(new, job, backend, check)

    def move(
        self, record: RunRecord, dst: str, progress: Callable[[int, int, str], None] | None = None
    ) -> tuple[RunRecord, RestartCheck | None, list[str]]:
        """Copy the whole run to `dst` (its restart first, checked there) and make `dst` its home. Returns the
        new record, the restart check, and the old home's files, which stay until removed on request."""
        old_home = self.data.home(record)
        if old_home.location.name == dst:
            raise RelocateError(f"run {record.name} is on {dst} already")
        if not record.outcome:
            raise RelocateError(f"run {record.name} has not ended: move it once it has")
        self.data.refresh(record, names=[dst])
        check = None
        if self.data.select(record, "restart"):
            check, _ = self.check_restart(record, dst, progress)
            if not check.ok:
                raise RelocateError(f"{dst} cannot read the run's restart: {'; '.join(check.problems)}")
        plan = self.data.plan(record, self.data.select(record, "run"), dst)
        self._run(plan, progress)
        left = self.data.plan(record, self.data.select(record, "run"), dst)
        if left.moving:
            raise RelocateError(f"{len(left.moving)} files did not reach {dst}: the home stays where it was")
        root = plan.destination.root
        remote = self._remote(dst)
        if remote is None:
            directory = (
                str(Path(root).relative_to(self.data.project.root)) if _under(root, self.data.project.root) else root
            )
            record.home = {"remote": LOCAL, "directory": directory}
        else:
            record.home = {"remote": dst, "remote_id": remote.id, "directory": root}
        self.data.project.save_run(record)
        old = sorted(old_home.path(e.path) for e in self.data.manifests[record.name].entries())
        self.data.manifests.pop(record.name, None)
        return record, check, old

    def remove_old_home(self, location: str, paths: Sequence[str]) -> None:
        """Delete exactly these files of a run's former home, the list `move` returned and the user confirmed."""
        loc = self.data.location(location)
        try:
            loc.remove(list(paths))
        except (OSError, TransferError) as e:
            raise RelocateError(str(e)) from e


def _under(path: str, root: Path) -> bool:
    try:
        Path(path).relative_to(root)
    except ValueError:
        return False
    return True


def _install_version(remote: Remote | None) -> str:
    if remote is None or not remote.installs:
        return ""
    return remote.installs[0].version or remote.installs[0].path


__all__ = ["Continuation", "RelocateError", "Relocator", "RestartCheck", "compare_restart", "restart_facts"]
