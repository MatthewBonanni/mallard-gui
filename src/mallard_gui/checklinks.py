"""The input keys a check diagnostic's text names, for builds whose check report has no structured `key` (before
PR #1377), so their diagnostics still link to what they concern: dotted keys (`run.cfl`,
`boundaries[name = "inlet"].T`), sections in brackets (`[les]`) and a section followed by its key
(`[run] t_wall_stop`). Only names the key catalog knows count, so numbers, file names and prose never match."""

from __future__ import annotations

import re
from collections.abc import Callable

from .input_doc import parse_path

_DOTTED = re.compile(r"(?<![\w./-])([A-Za-z_]\w*(?:\[[^\]\n]*\])?(?:\.[A-Za-z_]\w*(?:\[[^\]\n]*\])?)+)(?![\w/(])")
_SECTION = re.compile(r"\[([A-Za-z_][\w.]*)\](?:\s+([A-Za-z_]\w*))?")


def _catalog_path(path: str) -> str:
    return ".".join(s for s in parse_path(path) if isinstance(s, str))


def referenced_keys(message: str, known: Callable[[str], bool]) -> list[str]:
    """Every key or section `message` names that `known` accepts (by its catalog path), in order of appearance
    and without repeats."""
    found: list[tuple[int, str]] = []
    for m in _DOTTED.finditer(message):
        path = m.group(1)
        try:
            if known(_catalog_path(path)):
                found.append((m.start(), path))
        except (ValueError, KeyError):
            continue
    for m in _SECTION.finditer(message):
        section, key = m.group(1), m.group(2)
        if key and known(f"{section}.{key}"):
            found.append((m.start(), f"{section}.{key}"))
        elif known(section):
            found.append((m.start(), section))
    out: list[str] = []
    for _, path in sorted(found):
        if path not in out:
            out.append(path)
    return out
