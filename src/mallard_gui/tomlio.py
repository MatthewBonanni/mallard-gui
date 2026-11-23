"""Small helpers for the GUI's own TOML files: one item per line, sorted keys."""

from __future__ import annotations

import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomlkit


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def dumps_sorted(data: dict[str, Any], header: tuple[str, ...] = ()) -> str:
    """Serialize with `schema` and `version` first, then sorted keys, so files diff and merge cleanly."""
    doc = tomlkit.document()
    for line in header:
        doc.add(tomlkit.comment(line))
    first = [k for k in ("schema", "version") if k in data]
    rest = sorted(k for k in data if k not in first)
    scalars = [k for k in first + rest if not _is_table_like(data[k])]
    tables = [k for k in rest if _is_table_like(data[k])]
    for key in scalars:
        doc.add(key, _item(data[key]))
    for key in tables:
        doc.add(key, _item(data[key], top=True))
    return tomlkit.dumps(doc)


def write_atomic(path: Path, text: str) -> None:
    """Write through a temporary file renamed over `path`: a crash leaves the old file or the new one, never part
    of either. A symbolic link stays one (its target is replaced) and an existing file keeps its permissions."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644  # not 0o666 & ~umask: reading the umask sets it, for every thread of the process
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _is_table_like(value: Any) -> bool:
    return isinstance(value, dict) or (
        isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)
    )


def _item(value: Any, top: bool = False) -> Any:
    if isinstance(value, dict):
        if top:
            table = tomlkit.table()
            for k in sorted(value):
                table.add(k, _item(value[k]))
            return table
        inline = tomlkit.inline_table()
        for k in sorted(value):
            inline.append(k, _item(value[k]))
        return inline
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value) and top:
        aot = tomlkit.aot()
        for v in value:
            aot.append(_item(v, top=True))
        return aot
    if isinstance(value, list):
        return [_item(v) for v in value]
    return value
