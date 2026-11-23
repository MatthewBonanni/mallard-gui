"""The project: `mallard-project.toml` and `mallard-runs/<run>.toml`, committed, identity only.

Neither file holds a solver key: `input.toml` is the case's single source of truth.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tomlio import dumps_sorted, read_toml, write_atomic

PROJECT_FILE = "mallard-project.toml"
RUNS_DIR = "mallard-runs"
_RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class RunRecord:
    """A run's durable identity, as `mallard-runs/<name>.toml` records it."""

    SCHEMA = "mallard-run-record"
    VERSION = 1

    name: str
    run_id: str = ""
    home: dict[str, str] = field(default_factory=dict)
    input: str = ""
    input_sha256: str = ""
    mallard: str = ""
    job: str = ""
    outcome: str = ""
    last_snapshot: dict[str, Any] = field(default_factory=dict)
    last_restart: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _RUN_NAME.match(self.name):
            raise ValueError(f"run name {self.name!r}: letters, digits, '.', '_' and '-' only")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"schema": self.SCHEMA, "version": self.VERSION}
        for key in (
            "run_id",
            "home",
            "input",
            "input_sha256",
            "mallard",
            "job",
            "outcome",
            "last_snapshot",
            "last_restart",
        ):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> RunRecord:
        if data.get("schema") != cls.SCHEMA:
            raise ValueError(f"run {name}: schema {data.get('schema')!r} is not {cls.SCHEMA!r}")
        if int(data.get("version", 0)) > cls.VERSION:
            raise ValueError(f"run {name}: version {data['version']} is newer than this GUI reads")
        kwargs = {
            k: data[k]
            for k in (
                "run_id",
                "home",
                "input",
                "input_sha256",
                "mallard",
                "job",
                "outcome",
                "last_snapshot",
                "last_restart",
            )
            if k in data
        }
        return cls(name=name, **kwargs)


class Project:
    """A directory holding one or more case inputs, with the GUI's committed files beside them."""

    SCHEMA = "mallard-project"
    VERSION = 1

    def __init__(self, root: Path, data: dict[str, Any]) -> None:
        self.root = root
        self.data = data

    @classmethod
    def open(cls, root: Path, create: bool = True) -> Project:
        """Open the project at `root`, writing a project file listing its inputs if there is none."""
        root = root.resolve()
        path = root / PROJECT_FILE
        if path.exists():
            data = read_toml(path)
            if data.get("schema") != cls.SCHEMA:
                raise ValueError(f"{path}: schema {data.get('schema')!r} is not {cls.SCHEMA!r}")
            if int(data.get("version", 0)) > cls.VERSION:
                raise ValueError(f"{path}: version {data['version']} is newer than this GUI reads")
            return cls(root, data)
        inputs = sorted(p.name for p in root.glob("*.toml") if _looks_like_input(p))
        project = cls(root, {"schema": cls.SCHEMA, "version": cls.VERSION, "inputs": inputs})
        if create:
            project.save()
        return project

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.root.name)

    @property
    def inputs(self) -> list[str]:
        return list(self.data.get("inputs", []))

    @property
    def remotes(self) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("remotes", {}))

    def save(self) -> None:
        write_atomic(
            self.root / PROJECT_FILE,
            dumps_sorted(
                self.data,
                header=(
                    "Written by mallard-gui (docs/design/gui.md):",
                    "the project's inputs, remotes by name and saved views.",
                    "Solver settings live in the inputs, never here.",
                ),
            ),
        )

    # Runs

    @property
    def runs_dir(self) -> Path:
        return self.root / RUNS_DIR

    def runs(self) -> list[RunRecord]:
        if not self.runs_dir.is_dir():
            return []
        return [RunRecord.from_dict(p.stem, read_toml(p)) for p in sorted(self.runs_dir.glob("*.toml"))]

    def run(self, name: str) -> RunRecord:
        return RunRecord.from_dict(name, read_toml(self.runs_dir / f"{name}.toml"))

    def save_run(self, record: RunRecord) -> Path:
        path = self.runs_dir / f"{record.name}.toml"
        write_atomic(path, dumps_sorted(record.to_dict()))
        return path

    def new_run_name(self, stem: str) -> str:
        taken = {r.name for r in self.runs()}
        n = 1
        while f"{stem}-{n}" in taken:
            n += 1
        return f"{stem}-{n}"


# What a copy of a project leaves behind: its runs and what was made from them, and hidden files (the unsaved
# text of an input among them).
NOT_COPIED = ("runs", RUNS_DIR, "exports")
NEW_INPUT = """\
# A new Mallard case: set its mesh, physics and boundaries in the GUI's case setup, where mallard check says
# what is still missing. Every key is in Mallard's docs/input.md.

[mesh]

[physics]
"""


def copy_plan(project: Project, dst: Path) -> list[Path]:
    """The files a copy of the project to `dst` takes, relative to its root."""
    dst = dst.resolve()
    out = []
    for top, dirs, files in os.walk(project.root):
        here = Path(top)
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".")
            and not (here == project.root and d in NOT_COPIED)
            and (here / d).resolve() != dst
            and not (here / d).is_symlink()
        )
        for name in sorted(files):
            path = here / name
            if not name.startswith(".") and not path.is_symlink():
                out.append(path.relative_to(project.root))
    return out


def copy_project_files(project: Project, dst: Path) -> None:
    """Copy the project's files (not its runs) to `dst`, where a case's relative paths then hold as they did."""
    for rel in copy_plan(project, dst):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project.root / rel, target)


def new_project(directory: Path) -> Path:
    """A new project: `directory` (new, or empty) with a starting `input.toml`; returns the input's path."""
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError(f"{directory}: not empty")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "input.toml"
    path.write_text(NEW_INPUT, encoding="utf-8")
    return path


def _looks_like_input(path: Path) -> bool:
    if path.name == PROJECT_FILE or path.name.startswith("."):
        return False
    try:
        data = read_toml(path)
    except (OSError, ValueError):
        return False
    return "mesh" in data or "physics" in data
