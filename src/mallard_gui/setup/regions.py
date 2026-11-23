"""Regions and probes of a case, as the input holds them, and the edits of the Regions and Probes sections:
add, promote an inline region to `[regions]`, use a region in another feature, remove.

Every region the case uses is found by walking the input, not from a list of features: a `region` key (or
an item of a `regions` list) anywhere but the top-level `[regions]` table is a use, and a `[[probes]]`
element with a geometry is a probe.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..input_doc import InputDocument, Resolved, format_path
from .geometry import PROBE_TYPES

# Starting tables for "Add region" and "Add probe", sized to the domain. TODO(#1256): from the catalog's
# variants once it has them; these are the examples of Mallard's geometry guide.
REGION_TYPES_2D = ("rectangle", "disk", "annulus", "polygon", "half_space", "ellipsoid", "union", "difference")
REGION_TYPES_3D = ("box", "sphere", "cylinder", "cone", "ellipsoid", "torus", "half_space", "disk", "rectangle")
PROBE_TYPES_2D = ("point", "points", "line", "polyline", "circle", "arc", "cut_plane")
PROBE_TYPES_3D = (*PROBE_TYPES_2D,)
_LABEL_KEYS = ("name", "prefix", "zone")


@dataclass
class Entry:
    """A region or probe in the input: its key path (indices resolved), a label, its value (a table or a
    region's name), and the feature that holds it."""

    path: str
    label: str
    value: Any
    owner: str
    named: bool = False  # in [regions]


def _label(data: Mapping[str, Any], segments: Resolved) -> str:
    """`write_data[prefix = "./solut/near_wake"].region` -> `near_wake`, `sources[0].region` -> `sources 1`."""
    node: Any = data
    label = ""
    for seg in segments:
        node = node[seg]
        if isinstance(seg, int) and isinstance(node, Mapping):
            found = next((str(node[k]) for k in _LABEL_KEYS if k in node), "")
            label = found.rstrip("/").rsplit("/", 1)[-1] if found else label
    if not label:
        head = [s for s in segments if isinstance(s, str)]
        idx = [s for s in segments if isinstance(s, int)]
        label = " ".join([head[0], *(str(i + 1) for i in idx)]) if head else "region"
    tail = segments[-1]
    if isinstance(tail, int) and len(segments) > 2:  # an item of a `regions` list
        label += f" {tail + 1}"
    return label


def selector_path(data: Mapping[str, Any], segments: Resolved) -> str:
    """The path as Mallard names case-view blocks: array elements by `name`, `prefix` or `zone`."""
    out: list[Any] = []
    node: Any = data
    for i, seg in enumerate(segments):
        node = node[seg]
        if isinstance(seg, int) and i > 0 and isinstance(node, Mapping) and out and isinstance(out[-1], str):
            key = next((k for k in _LABEL_KEYS if k in node), None)
            out.append((key, str(node[key])) if key else seg)
        else:
            out.append(seg)
    return format_path(out)


def _walk(node: Any, prefix: list[str | int]) -> Iterator[tuple[list[str | int], Any]]:
    if isinstance(node, Mapping):
        for k, v in node.items():
            yield [*prefix, k], v
            yield from _walk(v, [*prefix, k])
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield [*prefix, i], v
            yield from _walk(v, [*prefix, i])


def regions(doc: InputDocument) -> list[Entry]:
    """Every region the input defines or uses inline: the named ones of `[regions]` first."""
    data = doc.data
    out: list[Entry] = []
    named = data.get("regions")
    if isinstance(named, Mapping):
        for name, value in named.items():
            out.append(Entry(f"regions.{name}", name, value, "regions", named=True))
    for segments, value in _walk(data, []):
        if not segments or segments[0] == "regions":
            continue
        key = segments[-1]
        parent_key = segments[-2] if len(segments) > 1 else None
        is_use = key == "region" or (isinstance(key, int) and parent_key == "regions")
        if not is_use or not isinstance(value, Mapping):
            continue
        owner = format_path(segments[:-1] if key == "region" else segments[:-2])
        out.append(Entry(format_path(segments), _label(data, segments), value, owner))
    return out


def uses(doc: InputDocument) -> dict[str, list[str]]:
    """Each region name's users: the features whose `region` (or a part of a boolean) names it."""
    out: dict[str, list[str]] = {}
    for segments, value in _walk(doc.data, []):
        if not isinstance(value, str) or not segments:
            continue
        key = segments[-1]
        parent = segments[-2] if len(segments) > 1 else None
        if key in ("region", "a", "b", "of") or (isinstance(key, int) and parent == "regions"):
            if segments[0] == "boundaries":
                continue
            out.setdefault(value, []).append(format_path(segments))
    return out


def probes(doc: InputDocument) -> list[Entry]:
    """`[[probes]]` elements with a geometry of their own (not a boundary or region probe), and the points of
    other features that sample one (`[statistics.phase] probe`)."""
    out = []
    items = doc.data.get("probes")
    if isinstance(items, list):
        for i, el in enumerate(items):
            if not isinstance(el, Mapping) or "region" in el or "boundary" in el or "boundary_type" in el:
                continue
            kind = str(el.get("type", ""))
            if kind in PROBE_TYPES or "point" in el or ("start" in el and "end" in el):
                out.append(Entry(f"probes[{i}]", str(el.get("name", f"probe {i + 1}")), el, f"probes[{i}]"))
    return out


def features(doc: InputDocument, catalog: Iterable[str]) -> list[str]:
    """The elements a region can be given to (`Use in`): those of arrays whose `region` key the catalog has."""
    takes = {k.removesuffix(".region") for k in catalog if k.endswith(".region") and k.count(".") == 1}
    out = []
    for array, items in doc.data.items():
        if array in takes and isinstance(items, list):
            out += [f"{array}[{i}]" for i, el in enumerate(items) if isinstance(el, Mapping)]
    return out


def free_name(existing: Sequence[str], base: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", base) or "region"
    if base not in existing:
        return base
    k = 2
    while f"{base}_{k}" in existing:
        k += 1
    return f"{base}_{k}"


def template(kind: str, dim: int, bounds: Sequence[float]) -> dict[str, Any]:
    """A starting table of a region or probe type, in the middle of the domain, a fifth of its size."""
    lo, hi = bounds[0::2], bounds[1::2]
    c = [_r((lo[i] + hi[i]) / 2) for i in range(dim)]
    ext = [hi[i] - lo[i] for i in range(dim)]
    s = _r(max(min(e for e in ext if e > 0) / 5, 1e-6)) if any(e > 0 for e in ext) else 1.0
    normal = [1.0] + [0.0] * (dim - 1)

    def at(*offset: float) -> list[float]:
        return [_r(c[i] + offset[i] * s) for i in range(dim)]

    tables: dict[str, dict[str, Any]] = {
        "rectangle": {"center": c, "size": [2 * s, s]}
        if dim == 2
        else {"center": c, "normal": [0.0, 0.0, 1.0], "size": [2 * s, s]},
        "box": {"center": c, "size": [2 * s, s, s]},
        "disk": {"center": c, "radius": s} if dim == 2 else {"center": c, "normal": normal, "radius": s},
        "sphere": {"center": c, "radius": s},
        "annulus": {"center": c, "radius": s, "inner_radius": _r(s / 2)},
        "ellipsoid": {"center": c, "semi_axes": [_r(s * f) for f in (1.0, 0.6, 0.4)[:dim]]},
        "cylinder": {"center": c, "axis": [0.0, 0.0, 1.0], "radius": _r(s / 2), "length": 2 * s},
        "cone": {"base": c, "axis": [0.0, 0.0, 1.0], "length": 2 * s, "base_radius": s, "top_radius": _r(s / 3)},
        "torus": {"center": c, "axis": [0.0, 0.0, 1.0], "major_radius": s, "minor_radius": _r(s / 4)},
        "half_space": {"point": c, "normal": normal},
        "polygon": {"vertices": [at(-1, -1, 0), at(1, -1, 0), at(0, 1, 0)]},
        "union": {
            "regions": [
                {"type": "disk" if dim == 2 else "sphere", "center": at(-0.5, 0, 0), "radius": s},
                {"type": "disk" if dim == 2 else "sphere", "center": at(0.5, 0, 0), "radius": s},
            ]
        },
        "difference": {
            "a": {"type": "disk" if dim == 2 else "sphere", "center": c, "radius": s},
            "b": {"type": "disk" if dim == 2 else "sphere", "center": at(0.5, 0, 0), "radius": _r(s / 2)},
        },
        "point": {"point": c},
        "points": {"points": [at(-1, 0, 0), c, at(1, 0, 0)]},
        "line": {"start": at(-1, 0, 0), "end": at(1, 0, 0), "n_points": 51},
        "polyline": {"vertices": [at(-1, -1, 0), at(0, 1, 0), at(1, -1, 0)], "n_points": 21},
        "circle": {"center": c, "radius": s, "n_points": 64}
        if dim == 2
        else {"center": c, "normal": [0.0, 0.0, 1.0], "radius": s, "n_points": 64},
        "arc": {"center": c, "radius": s, "angles": [-45.0, 45.0], "n_points": 31},
        "cut_plane": {"point": c, "normal": normal},
    }
    if kind not in tables:
        raise ValueError(f"no starting table for type {kind!r}")
    return {"type": kind, **tables[kind]}


def _r(x: float) -> float:
    return float(f"{x:.4g}")


# Edits


def add_region(doc: InputDocument, kind: str, dim: int, bounds: Sequence[float], name: str = "") -> str:
    """A new named region of `[regions]`; returns its path."""
    existing = list((doc.data.get("regions") or {}).keys())
    name = free_name(existing, name or kind)
    doc.set(f"regions.{name}", template(kind, dim, bounds))
    return f"regions.{name}"


def promote(doc: InputDocument, path: str, name: str = "") -> str:
    """Move an inline region to `[regions]` under a name and reference it by that name; returns the new path."""
    value = doc.get(path)
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}: not an inline region")
    data = doc.data
    existing = list((data.get("regions") or {}).keys())
    segments = doc.resolve(path)
    name = free_name(existing, name or _label(data, segments))
    doc.set(f"regions.{name}", dict(value))
    doc.set(path, name)
    return f"regions.{name}"


def use_in(doc: InputDocument, region: str, element: str) -> None:
    """Give a named region to a feature element as its `region`."""
    doc.set(f"{element}.region", region)


def add_probe(doc: InputDocument, kind: str, dim: int, bounds: Sequence[float], fields: Sequence[str] = ("P",)) -> int:
    existing = [str(e.get("name")) for e in doc.data.get("probes") or [] if isinstance(e, Mapping)]
    name = free_name(existing, kind)
    table = template(kind, dim, bounds)
    return doc.append_element("probes", {"name": name, "fields": list(fields), **table})


def remove_region(doc: InputDocument, path: str) -> None:
    """Remove a named region (the check then names any feature still using it)."""
    doc.delete(path)
