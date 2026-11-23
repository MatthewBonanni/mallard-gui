"""Which `mallard` runs a case. Mallard is one executable per dimension (`mallard2d`, `mallard3d`) behind the
`mallard` front end, which picks by the case's mesh. Where the user names a dimension's executable in Settings
(a build tree's `build-2d/src/mallard`, say), a case of that dimension uses it, and the front end otherwise.

The case's dimension is the front end's rule: a mesh file's, from `mallard info MESH --format json`; for a
generated mesh, 3D when `[mesh]` sets `Nz` or `Lz`.

TODO(session): ask `mallard session` for the case's dimension instead of `mallard info` on its mesh."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# `mallard info` of a mesh file with one executable: its JSON, or None when that executable cannot read it.
Info = Callable[[Path, Path], dict[str, Any] | None]


def mallard_info(executable: Path, mesh: Path) -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            [str(executable), "info", str(mesh), "--format", "json"],
            cwd=mesh.parent,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def mesh_file(data: Mapping[str, Any], case_dir: Path) -> Path | None:
    mesh = data.get("mesh")
    if not isinstance(mesh, Mapping) or not isinstance(mesh.get("filename"), str):
        return None
    path = Path(mesh["filename"]).expanduser()
    return path if path.is_absolute() else case_dir / path


@dataclass
class Executables:
    """The front end and, where set, each dimension's own executable."""

    front: Path
    by_dim: dict[int, Path] = field(default_factory=dict)
    info: Info = mallard_info
    _cache: dict[tuple[str, int, int], int | None] = field(default_factory=dict, repr=False)

    def for_dim(self, dim: int | None) -> Path:
        return self.by_dim.get(dim, self.front) if dim else self.front

    def dimension(self, data: Mapping[str, Any], case_dir: Path) -> int | None:
        """The case's dimension, None when it cannot be told (no mesh, or no executable reads it)."""
        mesh = data.get("mesh")
        if not isinstance(mesh, Mapping):
            return None
        path = mesh_file(data, case_dir)
        if path is None:
            return 3 if ("Nz" in mesh or "Lz" in mesh) else 2
        try:
            st = path.stat()
        except OSError:
            return None
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key not in self._cache:
            self._cache[key] = self._read(path)
        return self._cache[key]

    def _read(self, path: Path) -> int | None:
        for exe in dict.fromkeys([self.front, *self.by_dim.values()]):
            data = self.info(exe, path)
            dim = data.get("dim", data.get("dimension")) if data else None
            if dim in (2, 3):
                return int(dim)
        return None
