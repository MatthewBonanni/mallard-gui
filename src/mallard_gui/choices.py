"""What a key's value can be, beyond its catalog type, from the catalog and the case: a file, a name defined
elsewhere (a zone, a region, a species), or a composition of species.

The catalog says none of this yet: it gives a string key no "path" type, no "names a zone" reference and no
"composition" form. Until it does, these follow Mallard's own naming conventions, which hold across its keys:
a key named `file`, `filename` or `*_file`, or whose default or value names a file, is a file; `zone`,
`boundary`, `region`, `balance` and `*_species` keys name zones, regions and species; and a table that takes
both `X` and `Y` takes a composition, given as exactly one of the two (`MixtureModel::mass_fractions`)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

FILE_TYPES = ("path", "file")  # a catalog type that says so, should Mallard add one
_FILE_LEAF = re.compile(r"^(file|filename|\w+_file)$")
_FILE_NAME = re.compile(r"^[^\s?:*<>|\"'(){}\[\]=]*[^/\s.]\.[A-Za-z][A-Za-z0-9_]{0,9}$")
REFERENCES = {"zone": "zones", "boundary": "zones", "region": "regions", "balance": "species", "species": "species"}
BASES = ("X", "Y")  # mole and mass fractions


def names_file(text: Any) -> bool:
    """Whether a value reads as a file name: no spaces or expression characters, an extension."""
    return isinstance(text, str) and bool(_FILE_NAME.match(text)) and not _is_number(text)


def is_file_key(leaf: str, type_: str | None, default: str | None, value: Any) -> bool:
    if type_ in FILE_TYPES:
        return True
    if type_ not in (None, "string") or isinstance(value, (dict, list, bool, int, float)):
        return False
    return bool(_FILE_LEAF.match(leaf)) or names_file(default) or names_file(value)


def reference(leaf: str) -> str | None:
    """The kind of name a key holds: "zones", "regions" or "species"; None for other keys."""
    if leaf in REFERENCES:
        return REFERENCES[leaf]
    return "species" if leaf.endswith("_species") else None


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and _is_number(value.strip()):
        return float(value)
    return None


def parse_fraction(text: str) -> float | str:
    """A fraction as typed: a number, else an expression Mallard evaluates in space (kept as a string)."""
    text = text.strip()
    n = number(text)
    if n is None:
        return text
    if not math.isfinite(n) or n < 0:
        raise ValueError(f"{text}: a fraction is a number not below 0")
    return n


def total(values: Mapping[str, Any]) -> float | None:
    """The sum of the fractions; None when one is an expression."""
    nums = [number(v) for v in values.values()]
    return None if any(n is None for n in nums) else sum(n for n in nums if n is not None)


def _tidy(x: float) -> float:
    return float(f"{x:.12g}")


def normalized(values: Mapping[str, Any]) -> dict[str, float]:
    s = total(values)
    if s is None:
        raise ValueError("a fraction is an expression: only numbers can be normalized")
    if s <= 0:
        raise ValueError("the fractions sum to 0: nothing to scale")
    return {k: _tidy(float(number(v) or 0.0) / s) for k, v in values.items()}


def converted(values: Mapping[str, Any], to: str, molar_masses: Mapping[str, float]) -> dict[str, float] | None:
    """Mole fractions to mass fractions (`to` "Y") or back (`to` "X") with the species' molar masses, normalized;
    None when a value is an expression or a species' molar mass is not known."""
    nums = {k: number(v) for k, v in values.items()}
    if any(n is None for n in nums.values()) or any(k not in molar_masses for k in nums):
        return None
    weighted = {k: (n or 0.0) * molar_masses[k] if to == "Y" else (n or 0.0) / molar_masses[k] for k, n in nums.items()}
    s = sum(weighted.values())
    if s <= 0:
        return None
    return {k: _tidy(w / s) for k, w in weighted.items()}


# TEMPORARY: catalogs before Mallard #1400 give no `unset` and write -1 for "off" (`run.t_wall_stop`, `run.t_stop`);
# remove DISABLED_SENTINELS and _AUTOMATIC once the GUI requires a Mallard with #1400.
DISABLED_SENTINELS = ("-1", "-1.0")
_AUTOMATIC = re.compile(r"\b(automatic|automatically|adaptive|chosen|computed|derived)\b|\bif omitted\b", re.I)


def unset_meaning(default: str | None, description: str, unset: str | None = None) -> str:
    """How an unset key without a default reads: the catalog's `unset` ("not set: no wall-clock limit"), else
    just not set; empty when the default is a value. `unset` None (a catalog before #1400): disabled for the
    sentinel, automatic where the description says Mallard picks the value."""
    if unset is not None:
        return f"not set: {unset}" if unset else ("" if default is not None else "not set")
    if default in DISABLED_SENTINELS:
        return "not set: disabled"
    if default is not None:
        return ""
    return "not set: automatic" if _AUTOMATIC.search(description) else "not set"


# TEMPORARY: Mallard's catalog says nothing of keys that exclude each other; these are its "give a or b, not
# both" checks (solver.cpp dt/cfl, data_writer.cpp and probes.cpp intervals, probes.cpp region, boundary.cpp
# mass_flux|mdot, synthetic_inflow.cpp, wall_exchange.cpp, initial_state.cpp at/region, mesh.cpp periodic), until
# the catalog lists them (`alternatives`). Per table: sets of branches, each a label and its keys.
Branches = tuple[tuple[str, tuple[str, ...]], ...]
STEPS_OR_TIME: Branches = (("Every N steps", ("interval",)), ("Every simulated time", ("time_interval",)))
ALTERNATIVES: dict[str, tuple[Branches, ...]] = {
    "run": ((("CFL number", ("cfl",)), ("Fixed time step", ("dt",))),),
    "write_data": (STEPS_OR_TIME, (("One region", ("region",)), ("Several regions", ("regions",)))),
    "probes": (
        STEPS_OR_TIME,
        (("Geometry", ("type", "point", "start", "end", "boundary", "boundary_type")), ("Region", ("region",))),
    ),
    "boundaries": ((("Mass flux", ("mass_flux",)), ("Mass flow rate", ("mdot",))),),
    "boundaries.turbulence": ((("Reynolds stresses", ("reynolds_stress",)), ("Profile file", ("profile",))),),
    "boundaries.wall_model": (
        (("Exchange distance", ("exchange_distance",)), ("Exchange cells", ("exchange_cells",))),
    ),
    "initialize.data": ((("Coordinates", ("at",)), ("Distance to a region", ("region",))),),
    "periodic": ((("Translation", ("translation",)), ("Rotation", ("angle", "center"))),),
}


def branch_of(table_catalog_path: str, leaf: str) -> tuple[int, int] | None:
    """Which set of alternatives and which branch of it a key is in."""
    for n, branches in enumerate(ALTERNATIVES.get(table_catalog_path, ())):
        for b, (_, keys) in enumerate(branches):
            if leaf in keys:
                return n, b
    return None
