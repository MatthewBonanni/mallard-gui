"""The keys a typed table reads (a probe, boundary, region shape, mesh, ...), which depend on its `type`.

Mallard's catalogs name keys without the entry (`probes.radius`), so where one input has several tables at one
catalog path of different kinds, the case catalog lists the union of what they read. What one of them reads comes
from the case catalog of a scratch input in which every table at that path is a copy of it (each keeping its own
name). TODO: per-type key sets in Mallard's catalog would make the scratch inputs unnecessary.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

# What tells the tables at one path apart (names, output files): each copy keeps its own.
IDENTITY = ("name", "prefix", "zone", "file", "reductions_file")


def instances(data: Any, catalog_path: str) -> Iterator[dict[str, Any]]:
    """Every table at a catalog path (`probes`, `write_data.region`, `regions.<name>`), in input order."""
    want = catalog_path.split(".")

    def walk(node: Any, depth: int) -> Iterator[dict[str, Any]]:
        if isinstance(node, list):
            for item in node:
                yield from walk(item, depth)
            return
        if not isinstance(node, dict):
            return
        if depth == len(want):
            yield node
            return
        part = want[depth]
        if part == "<name>":
            for v in node.values():
                yield from walk(v, depth + 1)
        elif part in node:
            yield from walk(node[part], depth + 1)

    yield from walk(data, 0)


def signature(table: dict[str, Any]) -> str:
    """What decides the keys a table reads: its type and which keys it sets."""
    return json.dumps({"type": table.get("type"), "keys": sorted(table)}, sort_keys=True, default=str)


def mixed(data: Any, catalog_path: str) -> bool:
    """Whether the tables at a path differ, so the case catalog's union is not what each one reads."""
    return len({signature(t) for t in instances(data, catalog_path)}) > 1


def uniform(data: dict[str, Any], catalog_path: str, table: dict[str, Any]) -> dict[str, Any]:
    """The input with every table at `catalog_path` a copy of `table` keeping its own identity keys; triggers,
    which may name what the copies change, are left out unless they are the tables."""
    out = copy.deepcopy(data)
    if not catalog_path.startswith("triggers"):
        out.pop("triggers", None)
    shared = {k: v for k, v in table.items() if k not in IDENTITY}
    for inst in list(instances(out, catalog_path)):
        own = {k: inst[k] for k in IDENTITY if k in inst}
        inst.clear()
        inst.update(copy.deepcopy(shared))
        inst.update(own)
    return out
