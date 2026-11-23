"""The input a run directory gets: the case's input, with what a launched run needs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..input_doc import InputDocument, format_path

CONTROL_INTERVAL = 10


def run_input(input_file: Path, absolute_paths: bool) -> str:
    """The text of the run directory's `input.toml`.

    TODO(#1316, PR #1334): `--set run.control_file_interval=N` would leave the input untouched; until then the
    run's copy turns control files on, so STOP and CHECKPOINT work, when the input leaves them off.
    TODO(#1256): with file roles in the catalog, only file keys would be rewritten; until then any
    relative string naming an existing file or directory beside the input is made absolute, so it
    still resolves from the run directory (outputs, which do not exist yet, stay relative).
    """
    doc = InputDocument(input_file, input_file.read_text(encoding="utf-8"))
    if not doc.get("run.control_file_interval"):
        doc.set("run.control_file_interval", CONTROL_INTERVAL)
    if absolute_paths:
        base = input_file.parent.resolve()
        for path, value in list(_strings(doc.data)):
            if value and not Path(value).is_absolute() and (base / value).exists() and "\n" not in value:
                doc.set(path, str((base / value).resolve()))
    return doc.text


def input_files(input_file: Path) -> list[str]:
    """The files an input names relative to its directory and that exist there (its mesh, data tables), as a
    remote run directory needs them beside its input.

    TODO(#1256): file roles from the catalog instead of every string naming an existing file."""
    doc = InputDocument(input_file, input_file.read_text(encoding="utf-8"))
    base = input_file.parent.resolve()
    out = []
    for _, value in _strings(doc.data):
        if value and "\n" not in value and not Path(value).is_absolute() and (base / value).is_file():
            rel = Path(value).as_posix().removeprefix("./")
            if rel not in out and not rel.startswith("../"):
                out.append(rel)
    return out


def _strings(node: Any, prefix: list[str | int] | None = None) -> list[tuple[str, str]]:
    prefix = prefix or []
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += _strings(v, [*prefix, k])
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, (dict, list)):
                out += _strings(v, [*prefix, i])
    elif isinstance(node, str) and prefix:
        out.append((format_path(prefix), node))
    return out
