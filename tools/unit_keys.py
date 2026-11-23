"""Writes `src/mallard_gui/unit_keys.py`: the dimension of every input key that has one, from the Units table of
Mallard's input reference (`docs/input.md`), resolved against Mallard's key catalogs. The catalog's own `dimension`
wins where it gives one; this table covers the keys it does not say yet.

Run from the repository root:

    source .venv/bin/activate && python tools/unit_keys.py [--mallard ~/local/Mallard] [--catalog KEYS.json ...]

and commit the regenerated module. The catalogs are `mallard list keys --format json` without an input, of a 2D
and a 3D build (default: run the `mallard2d` and `mallard3d` of `--build`, or `mallard` on PATH). The table's
shorthand reads as the reference writes it:

- `; ` separates groups and `, ` (or ` and `) the keys of a group; a bare name continues the table of the group's
  first full path (`run.dt`, `t_stop`: `run.t_stop`).
- `` `T` of `initialize`, `boundaries` ``: the key of each table named.
- `region lengths (`center`, ...)`: those keys of every region table (`regions.<name>`, `... .region`).
- A path that is not rooted at a top-level table (`wall_model.exchange_distance`) matches every catalog key that
  ends with it.

Keys whose dimension depends on another key (`statistics.phase.level` on its `variable`, `[[sources]]` rates on
`total`) are listed in UNKNOWN, with the reason, and get no unit selector. Keys of a fixed unit the reference
gives as plain numbers (angles in degrees, `chemistry.C_reg` in kmol/m^3, a temperature difference in K) are in
FIXED: their unit is shown, not chosen. Names the catalogs do not have are reported and left out.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "src" / "mallard_gui" / "unit_keys.py"
DEFAULT_MALLARD = Path.home() / "local" / "Mallard"

# The reference's SI unit of each dimension, as the GUI's unit parser writes it.
SI = {
    "s": "s",
    "m": "m",
    "K": "K",
    "Pa": "Pa",
    "kg/m^3": "kg/m^3",
    "m/s": "m/s",
    "m^2/s^2": "m^2/s^2",
    "m/s^2": "m/s^2",
    "1/s": "1/s",
    "1/m": "1/m",
    "Pa s": "Pa s",
    "W/m^2": "W/m^2",
    "kg/(m^2 s)": "kg/(m^2 s)",
    "kg/s": "kg/s",
}
SOURCE_UNITS = {"rho": "kg/(m^3 s)", "rhou": "N/m^3", "rhoE": "W/m^3", "rhoY": "kg/(m^3 s)"}
SOURCE_NAMES = {"kg/(m^3 s)": "mass source", "N/m^3": "momentum source", "W/m^3": "energy source"}


def parsed(unit: str) -> str | None:
    """A unit the GUI's parser reads, as written; None otherwise."""
    sys.path.insert(0, str(OUTPUT.parent.parent))
    from mallard_gui.units import UnitError, parse_unit

    try:
        parse_unit(unit)
    except UnitError:
        return None
    return unit


def table_rows(text: str) -> list[tuple[str, str]]:
    """The (dimension, keys) rows of the `| Dimension | Keys |` table."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("| Dimension | Keys |"))
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append((cells[0], cells[1]))
    return rows


def after_table(text: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("| Dimension | Keys |"))
    end = next(i for i in range(start + 2, len(lines)) if not lines[i].startswith("|"))
    return " ".join(lines[end : end + 6])


def split_top(text: str, seps: Iterable[str]) -> list[str]:
    """`text` split at any of `seps` outside parentheses."""
    out, depth, cur, i = [], 0, "", 0
    seps = sorted(seps, key=len, reverse=True)
    while i < len(text):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if depth == 0:
            sep = next((s for s in seps if text.startswith(s, i)), None)
            if sep:
                out.append(cur)
                cur, i = "", i + len(sep)
                continue
        cur += c
        i += 1
    out.append(cur)
    return [p.strip() for p in out if p.strip()]


def names(item: str) -> list[str]:
    return re.findall(r"`([^`]+)`", item)


class Resolver:
    def __init__(self, catalog: set[str]) -> None:
        self.catalog = catalog
        self.roots = {k.split(".", 1)[0] for k in catalog}
        self.missing: list[str] = []

    def rooted(self, path: str) -> bool:
        return path.split(".", 1)[0] in self.roots

    def resolve(self, path: str) -> list[str]:
        """The catalog paths `path` names. A rooted path the catalogs lack stays as written: the general catalogs
        list only what a default entry reads (a `radius` probe's keys only in a case with one)."""
        if path in self.catalog:
            return [path]
        if self.rooted(path):
            found = sorted(k for k in self.catalog if re.sub(r"\.<[^>]+>", "", k) == path)
            if not found:
                self.missing.append(path)
            return found or [path]
        found = sorted(k for k in self.catalog if k.endswith("." + path))
        if not found:
            self.missing.append(path)
        return found


def is_region_table(parent: str) -> bool:
    last = parent.rsplit(".", 1)[-1]
    return last in ("region", "regions") or bool(re.fullmatch(r"regions\.<[^>]+>", parent))


def keys_of(cell: str, r: Resolver) -> list[str]:
    out: list[str] = []
    for group in split_top(cell, ["; "]):
        if group.startswith("region lengths"):
            leaves = set(names(group))
            out += sorted(
                k for k in r.catalog if k.rpartition(".")[2] in leaves and is_region_table(k.rpartition(".")[0])
            )
            continue
        m = re.match(r"^`([^`]+)` of (.*)$", group)
        if m:
            key = m[1]
            for item in split_top(m[2], [", ", " and "]):
                bare = re.sub(r"\([^)]*\)", "", item).strip()
                word = re.fullmatch(r"`[^`]+` (\w+)", bare)  # "`dirichlet` boundaries"
                tables = [word[1]] if word else names(bare)
                for table in tables:
                    out += r.resolve(f"{table}.{key}")
            continue
        prefixes: list[str] = []  # the group's tables, outermost first: a bare name continues the deepest that has it
        for item in split_top(group, [", ", " and "]):
            item = re.sub(r"\([^)]*\)", "", item).strip()
            found = names(item)
            if not found:
                continue
            name = found[0]
            if "." in name and (not prefixes or r.rooted(name)):
                prefixes = [name.rsplit(".", 1)[0]]
                out += r.resolve(name)
                continue
            if not prefixes:
                out += r.resolve(name)
                continue
            known = [f"{p}.{name}" for p in reversed(prefixes) if f"{p}.{name}" in r.catalog]
            out += r.resolve(known[0] if known else f"{prefixes[0]}.{name}")
            if "." in name:
                prefixes.append(f"{prefixes[0]}.{name.rsplit('.', 1)[0]}")
    return out


def build(text: str, catalog: set[str]) -> tuple[dict[str, str], dict[str, str], dict[str, str], list[str]]:
    r = Resolver(catalog)
    dims: dict[str, str] = {}
    fixed: dict[str, str] = {}
    unknown: dict[str, str] = {}
    for head, cell in table_rows(text):
        m = re.match(r"^(.*?) \((.*)\)$", head)
        name, unit = (m[1], m[2]) if m else (head, "")
        lname = name.lower()
        if lname.startswith("sources"):
            for key, u in SOURCE_UNITS.items():
                for k in r.resolve(f"source.{key}"):
                    dims[k] = f"{SOURCE_NAMES.get(u, 'species source')} ({u})"
                for k in r.resolve(f"sources.{key}"):
                    unknown[k] = f"{u} per unit volume, or the integrated rate with `total = true`"
            continue
        keys = keys_of(cell, r)
        plain = re.match(r"^plain numbers in (.*)$", unit)
        if plain or "difference" in lname:
            shown = {"degrees": "deg"}.get(plain[1], plain[1]) if plain else unit.split(";")[0].strip()
            for k in keys:
                fixed[k] = shown
            continue
        si = SI.get(unit.split(";")[0].strip()) or parsed(unit.split(";")[0].strip())
        if si is None:
            raise SystemExit(f"unit_keys: no SI unit for the row {head!r}")
        for k in keys:
            dims[k] = f"{lname} ({si})"
    tail = after_table(text)
    for m in re.finditer(r"`([\w.]+)` has the dimension of its\s+`(\w+)`", tail):
        for k in r.resolve(m[1]):
            unknown[k] = f"the dimension of its `{m[2]}`"
    for m in re.finditer(r"`([\w.]+)` those of the variables of `(\w+)`", tail):
        for k in r.resolve(m[1]):
            unknown[k] = f"the dimensions of the variables of its `{m[2]}`"
    for k in unknown:
        dims.pop(k, None)
    return dims, fixed, unknown, sorted(set(r.missing))


def catalogs(paths: list[Path], build_dir: Path | None) -> set[str]:
    keys: set[str] = set()
    sources: list[str] = []
    if paths:
        sources = [p.read_text(encoding="utf-8") for p in paths]
    else:
        exes = [build_dir / n for n in ("mallard2d", "mallard3d")] if build_dir else []
        exes = [e for e in exes if e.exists()] or [Path(p) for p in [shutil.which("mallard")] if p]
        for exe in exes:
            out = subprocess.run(
                [str(exe), "list", "keys", "--format", "json"], capture_output=True, text=True, check=True
            )
            sources.append(out.stdout)
    for s in sources:
        data = json.loads(s)
        keys |= set(data.get("keys", data))
    if not keys:
        raise SystemExit("unit_keys: no key catalog (pass --catalog or --build)")
    return keys


def write(dims: dict[str, str], fixed: dict[str, str], unknown: dict[str, str], source: str) -> str:
    def block(name: str, doc: str, d: dict[str, str]) -> str:
        body = "".join(f"    {k!r}: {v!r},\n" for k, v in sorted(d.items()))
        return f"# {doc}\n{name}: dict[str, str] = {{\n{body}}}\n"

    return (
        f'"""Generated by `tools/unit_keys.py` from {source}: do not edit."""\n\n'
        + block("DIMENSIONS", "Catalog path: its dimension, as the catalog's `dimension` writes one.", dims)
        + "\n"
        + block("FIXED", "Catalog path: the unit of a key the reference gives as a plain number.", fixed)
        + "\n"
        + block("UNKNOWN", "Catalog path: why its unit is not known from the key alone.", unknown)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mallard", type=Path, default=DEFAULT_MALLARD, help="a Mallard checkout (its docs/input.md)")
    ap.add_argument("--catalog", type=Path, action="append", default=[], help="a `list keys --format json` output")
    ap.add_argument("--build", type=Path, help="a directory with mallard2d and mallard3d")
    ap.add_argument("--output", type=Path, default=OUTPUT)
    args = ap.parse_args(argv)
    text = (args.mallard / "docs" / "input.md").read_text(encoding="utf-8")
    dims, fixed, unknown, missing = build(text, catalogs(args.catalog, args.build))
    sha = subprocess.run(
        ["git", "-C", str(args.mallard), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    args.output.write_text(write(dims, fixed, unknown, f"Mallard's docs/input.md ({sha or 'unknown commit'})"))
    print(f"{len(dims)} keys with a dimension, {len(fixed)} of a fixed unit, {len(unknown)} unknown", file=sys.stderr)
    for name in missing:
        print(f"not in the general catalogs (kept if rooted): {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
