"""The output fields an entry can name, from Mallard's own lists: `mallard list fields input.toml` (the case's
fields, and the catalog's others with what they need) and `mallard list fields` (the build's catalog, for the
surface fields a region of faces writes). A field list's entries are names (`P`), vectors by their name
without the component (`U`) and prefixes ending in `*` (`Y_*`); the picker keeps them as written until a
name they cover is cleared."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

COMPONENTS = ("_X", "_Y", "_Z")
SURFACE = "surface probes"
FACES = "a region of faces (a boundary zone, a surface)"


@dataclass(frozen=True)
class Field:
    name: str
    units: str
    description: str
    group: str
    needs: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "units": pretty_units(self.units),
            "description": self.description,
            "needs": self.needs,
        }


def _field(d: Mapping[str, Any]) -> Field:
    return Field(
        str(d["name"]),
        str(d.get("units", "")),
        str(d.get("long_name", "")),
        str(d.get("group", "")),
        str(d.get("needs", "")),
    )


def _matches(pattern: str, name: str) -> bool:
    """A catalog pattern (`Y_<s>`, `COV_<A>_<B>`) against a name."""
    head = pattern.split("<", 1)[0]
    return name.startswith(head) if "<" in pattern else name == pattern


class FieldCatalog:
    """What one kind of entry (`write_data`) can write in this case, grouped as Mallard groups them."""

    def __init__(self, case: Mapping[str, Any], build: Mapping[str, Any] | None, target: str = "write_data") -> None:
        takes = [_field(d) for d in case.get("fields") or [] if target in (d.get("available") or ())]
        self.missing = [_field(d) for d in case.get("missing") or []]
        if target == "write_data" and build:
            # A region of faces (a boundary zone, a cut plane) writes the surface probes' fields too.
            for d in build.get("fields") or []:
                needs = str(d.get("needs", ""))
                if d.get("group") == SURFACE and "<" not in str(d.get("name")) and "a mixture" not in needs:
                    takes.append(replace_needs(_field(d), "a wall zone as region" if "wall" in needs else FACES))
        self.fields = takes
        self.names = {f.name: f for f in takes}

    def groups(self) -> list[dict[str, Any]]:
        out: dict[str, list[Field]] = {}
        for f in self.fields:
            out.setdefault(f.group, []).append(f)
        return [
            {"group": g, "label": "surface" if g == SURFACE else g, "fields": [f.to_dict() for f in fs]}
            for g, fs in out.items()
        ]

    def covered(self, entry: str) -> list[str]:
        """The names an entry selects, in catalog order."""
        if entry in self.names:
            return [entry]
        if entry.endswith("*"):
            return [n for n, f in self.names.items() if n.startswith(entry[:-1]) and f.group != SURFACE]
        return [entry + c for c in COMPONENTS if entry + c in self.names]

    def problem(self, entry: str) -> str:
        """Why an entry writes nothing in this case, or "" when it names fields."""
        if self.covered(entry):
            return ""
        for f in self.missing:
            if _matches(f.name, entry) or any(_matches(f.name, entry + c) for c in COMPONENTS):
                return f"{entry} needs {f.needs}" if f.needs else f"{entry} is not written by this case"
        if entry.endswith("*"):
            return f"{entry} matches no field of this case"
        return f"{entry} is not a field Mallard writes"

    def selected(self, entries: Sequence[str]) -> list[str]:
        out: list[str] = []
        for e in entries:
            out += [n for n in self.covered(e) if n not in out]
        return out

    def toggle(self, entries: Sequence[str], names: Sequence[str], on: bool) -> list[str]:
        """The entries after selecting or clearing `names`: a selected name not yet covered is appended; a
        cleared name drops its entry, and a vector or prefix that covered it becomes the names it still
        selects."""
        entries = list(entries)
        if on:
            have = set(self.selected(entries))
            return entries + [n for n in names if n not in have and n in self.names]
        gone = set(names)
        out: list[str] = []
        for e in entries:
            covered = self.covered(e)
            if not gone.intersection(covered):
                out.append(e)
            elif covered != [e]:
                out += [n for n in covered if n not in gone and n not in out]
        return out


SUPERSCRIPT = str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹")


def pretty_units(units: str) -> str:
    """Mallard's unit text with its exponents raised (`kg m-3` as kg m⁻³); "" for a dimensionless field."""
    if units == "1":
        return ""
    return re.sub(r"(?<=[A-Za-z])(-?\d+)\b", lambda m: m.group(1).translate(SUPERSCRIPT), units)


def replace_needs(f: Field, needs: str) -> Field:
    return Field(f.name, f.units, f.description, f.group, needs)


def toml_array(entries: Sequence[str]) -> str:
    return "[" + ", ".join(json.dumps(e) for e in entries) + "]"
