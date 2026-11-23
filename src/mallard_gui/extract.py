"""Lines and wall distributions of a snapshot, extracted by Mallard (`mallard post extract --region`), never
re-sampled here: the line's point values are the run's reconstruction, a wall's samples its boundary
probe's (P, CP, CF, Y_PLUS, ... at the face centroids, ordered along the wall with their arc length `s` in 2D).

The extraction is a post job where the run is (`postjobs`); its probe file is read back and drawn against
distance along the line or arc length, with the axes in the units the user picks.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .csvseries import CsvSeries
from .postjobs import _name
from .units import CHOICES, Unit, UnitError, parse_unit

# Snapshots `post extract` reads: restart files and snapshots of the whole mesh in mesh order.
EXTRACTABLE = ("restart", "vtkhdf", "hdf5", "xdmf", "xmf", "h5")
WALL_FIELDS = ("P", "CP", "CF", "Y_PLUS", "TAU_W", "Q_W")


def sources(outputs: Sequence[dict[str, Any]], end: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The run's files `post extract` can read, newest first: its restart files and whole-mesh VTKHDF or
    XDMF snapshots (VTU and VTP hold cells in an order of their own)."""
    out: dict[str, dict[str, Any]] = {}
    for o in outputs:
        kind = str(o.get("kind") or o.get("format") or "")
        path = str(o.get("path") or "")
        if path and (kind in EXTRACTABLE or path.endswith((".restart", ".vtkhdf", ".xmf"))):
            out[path] = {"path": path, "step": o.get("step"), "t": o.get("t"), "kind": kind or path.rsplit(".")[-1]}
    last = (end or {}).get("last_restart") or {}
    if last.get("path") and last["path"] not in out:
        out[last["path"]] = {"path": last["path"], "step": last.get("step"), "t": last.get("t"), "kind": "restart"}
    return sorted(out.values(), key=lambda s: (s["t"] is None, -(s["t"] or 0.0)))


def _vector(p: Sequence[float]) -> str:
    return "[" + ", ".join(repr(float(c)) for c in p) + "]"


def line_region(p0: Sequence[float], p1: Sequence[float], n: int) -> str:
    return f'{{type = "line", start = {_vector(p0)}, end = {_vector(p1)}, n_points = {int(n)}}}'


def line_args(
    file: str, step: int | None, p0: Sequence[float], p1: Sequence[float], n: int, fields: Sequence[str], name: str
) -> list[str]:
    """Point values of the reconstruction at `n` points from p0 to p1: `extract/probe_NAME.csv`."""
    if n < 2:
        raise ValueError("a line needs at least 2 points")
    if len(p0) != len(p1) or math.dist(p0, p1) == 0:
        raise ValueError("a line needs two different end points")
    args = ["post", "extract", file, "--region", line_region(p0, p1, n), "--sampling", "point"]
    return _finish(args, step, fields, name)


def wall_input(input_text: str, name: str, zone: str, fields: Sequence[str]) -> str:
    """The run's input with one more `[[probes]]`: a boundary probe of the zone, its samples at the face
    centroids. TODO(#1361): `post extract --region ZONE` as a boundary probe would need no copy."""
    if not zone:
        raise ValueError("pick a wall")
    listed = ", ".join(f'"{f}"' for f in fields) or '"P"'
    return (
        f"{input_text.rstrip()}\n\n# mallard-gui: a wall distribution, extracted by mallard post extract\n"
        f'[[probes]]\nname = "{_name(name)}"\nboundary = "{zone}"\nfields = [{listed}]\n'
        f'sampling = "point"\nfile = "probe_{name}.csv"\n'
    )


def wall_args(file: str, step: int | None, input_path: str, name: str) -> list[str]:
    """The boundary probe of `wall_input`, extracted: `extract/probe_NAME.csv`, with `s` along the wall in 2D.
    `-i` names the input: the resolved one in the run block fails for boundary probes (#1360)."""
    args = ["post", "extract", file, "-i", input_path, "--probe", _name(name)]
    if step is not None:
        args += ["--steps", str(int(step))]
    return [*args, "-o", "extract", "--format", "json"]


def _finish(args: list[str], step: int | None, fields: Sequence[str], name: str) -> list[str]:
    if fields:
        args += ["--fields", ",".join(fields)]
    if step is not None:
        args += ["--steps", str(int(step))]
    return [*args, "--name", _name(name), "-o", "extract", "--format", "json"]


def probe_file(report: dict[str, Any] | None, name: str) -> str:
    """The probe CSV a `post extract --format json` report lists as written for NAME, else Mallard's default
    `extract/probe_NAME.csv`."""
    for f in (report or {}).get("written", []):
        path = str(f.get("path") or "") if isinstance(f, dict) else str(f)
        if path.endswith(f"probe_{name}.csv"):
            return path
    return f"extract/probe_{name}.csv"


def mallard_unit(text: str) -> Unit | None:
    """A CSV units-line entry (`kg m-1 s-2`, `m s-1`, `1`) as a unit, None for none or an unknown one."""
    text = text.strip()
    if not text or text == "1":
        return None
    try:
        return parse_unit(re.sub(r"([A-Za-z])(-?\d+)", r"\1^\2", text))
    except UnitError:
        return None


def unit_choices(text: str) -> list[str]:
    """The units an axis of this CSV unit can be shown in, the file's own first."""
    u = mallard_unit(text)
    if u is None:
        return []
    own = text.strip()
    return [own, *(c for c in CHOICES.get(u.dim, ()) if c != own)]


def convert_values(values: Sequence[float], src: str, dst: str) -> list[float]:
    """`values` in the CSV unit `src` shown in `dst` (same dimension), unchanged when either is none."""
    a, b = mallard_unit(src), (mallard_unit(dst) if dst != src else None)
    if a is None or b is None:
        return list(values)
    if a.dim != b.dim:
        raise UnitError(f"{dst} is not a unit of {src}")
    return [float(b.from_si(a.to_si(Fraction(v)))) for v in values]


@dataclass
class Profile:
    """One step of an extracted line or wall: the abscissa (distance along the line, or arc length) and each
    field's values, with the file's units."""

    x_name: str
    x: list[float]
    x_unit: str
    columns: dict[str, list[float]] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    step: float | None = None
    t: float | None = None
    steps: list[float] = field(default_factory=list)


def read_profile(text: str, path: str = "probe.csv", step: float | None = None) -> Profile:
    """A line or boundary probe file as a profile at `step` (default: the last in the file)."""
    rows_by_step: dict[float, list[dict[str, float]]] = {}
    c = CsvSeries(path)
    times: dict[float, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") or not c.columns:
            c.add_line(line)
            continue
        cells = line.split(",")
        if cells == c.columns or len(cells) != len(c.columns):
            continue
        try:
            row = dict(zip(c.columns, (float(v) for v in cells), strict=True))
        except ValueError:
            continue
        s = row.get("step", 0.0)
        rows_by_step.setdefault(s, []).append(row)
        times[s] = row.get("t", 0.0)
    if not rows_by_step:
        raise ValueError(f"{path}: no samples")
    steps = sorted(rows_by_step)
    chosen = step if step in rows_by_step else steps[-1]
    rows = rows_by_step[chosen]
    if "s" in c.columns:
        x_name, x = "s", [r["s"] for r in rows]
        x_unit = c.units.get("s", "m")
    else:
        x_name = "distance"
        coords = [k for k in ("x", "y", "z") if k in c.columns]
        first = [rows[0][k] for k in coords]
        x = [math.dist(first, [r[k] for k in coords]) for r in rows]
        x_unit = c.units.get("x", "m")
    columns = {n: [r[n] for r in rows] for n in c.variables}
    return Profile(x_name, x, x_unit, columns, {n: c.units.get(n, "1") for n in columns}, chosen, times[chosen], steps)
