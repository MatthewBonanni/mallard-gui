"""Inflow velocity vectors on the boundaries that let flow in, drawn on their faces in the case view.

The vectors are Mallard's own where the build gives them: `case.boundary_values` evaluates each
`[[boundaries]]` entry at its faces, so a profile, a data table or a moving frame is drawn as the run will
prescribe it, and the velocity a mass-flow or stagnation inlet makes of the initial state comes marked as an
estimate. For builds without that request the velocity is read from the entry itself, for what needs no
evaluation: a vector of numbers, or a speed along `direction` or the inward normal; a vector of expressions of
x, y, z is sampled at the case view's faces; mass-flux and stagnation inlets get an estimate from the initial
state, labeled as such; data profiles get their direction only.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..mallard import sample_expressions
from ..units import UnitError, parse_quantity
from .geometry import Vec, unit, vector

# The conditions whose faces take flow in (Mallard's boundary_conditions guide: inlets, the far field, a
# prescribed exterior state).
INFLOW_TYPES = ("inlet_state", "inlet_stagnation", "inlet_mass_flow", "farfield", "dirichlet")
VELOCITY = (1, 0, -1, 0, 0, 0)
AUTO_ARROWS = {2: 24, 3: 150}


@dataclass
class Faces:
    """A zone's faces: centers, outward unit normals, measures (areas; lengths in 2D)."""

    centers: list[Vec]
    normals: list[Vec]
    measures: list[float]


@dataclass
class Values:
    """One `[[boundaries]]` entry's values at its faces (`case.boundary_values`): centers, inward unit normals and,
    where the entry sets one, the velocity it prescribes (or estimates)."""

    index: int
    zone: str
    condition: str
    centers: list[Vec]
    normals: list[Vec]
    velocity: list[Vec] | None
    units: str = ""  # the velocity's
    estimated: bool = False


def boundary_values(result: Mapping[str, Any], root: Path) -> dict[int, Values]:
    """The entries of a `mallard-boundary-values` result by their index in `[[boundaries]]`, each read from its
    CSV file (relative paths from `root`, the session's working directory). Unreadable files are left out."""
    out: dict[int, Values] = {}
    for entry in result.get("entries") or []:
        try:
            path = Path(str(entry["file"]))
            with (path if path.is_absolute() else root / path).open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            index = int(entry["index"])
        except (KeyError, OSError, ValueError, TypeError):
            continue
        columns = {str(c.get("name")): c for c in entry.get("columns") or [] if isinstance(c, Mapping)}
        velocity = _vectors(rows, columns, "u_")
        column = columns.get("u_x") or {}
        out[index] = Values(
            index,
            str(entry.get("zone", "")),
            str(entry.get("condition", "")),
            _vectors(rows, columns, "") or [],
            _vectors(rows, columns, "n_") or [],
            velocity,
            str(column.get("units") or ""),
            any(bool(columns.get(f"u_{a}", {}).get("estimated")) for a in "xyz"),
        )
    return out


def _vectors(rows: Sequence[Mapping[str, str]], columns: Mapping[str, Any], prefix: str) -> list[Vec] | None:
    """The vectors of columns `<prefix>x`, `<prefix>y` and `<prefix>z` (absent ones zero, as in 2D)."""
    x, y, z = (f"{prefix}{a}" for a in "xyz")
    if x not in columns:
        return None
    try:
        return [(float(r[x]), float(r.get(y) or 0), float(r.get(z) or 0)) for r in rows]
    except (KeyError, ValueError):
        return None


@dataclass
class Inflow:
    zone: str
    points: list[Vec] = field(default_factory=list)
    vectors: list[Vec] = field(default_factory=list)
    source: str = ""  # what the vectors are: "uniform", "speed along the inward normal", ...
    estimate: bool = False  # the speed is an estimate
    known: bool = True  # False: direction only, unit length
    note: str = ""

    @property
    def max_speed(self) -> float:
        return max((math.sqrt(sum(c * c for c in v)) for v in self.vectors), default=0.0)


def speed(value: Any) -> float | None:
    """A velocity component or speed: a number, or a quantity with a velocity unit (`"10 m/s"`)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            q = parse_quantity(value)
        except UnitError:
            return None
        if q is not None and (not q.unit.text or q.unit.dim == VELOCITY) and not q.unit.offset:
            return float(q.si())
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def initial_state(data: Mapping[str, Any]) -> tuple[float, float, float] | None:
    """(rho, p, gamma) of the initial state when it is uniform numbers, else of the reference state."""
    init = data.get("initialize") if isinstance(data.get("initialize"), Mapping) else {}
    phys = data.get("physics") if isinstance(data.get("physics"), Mapping) else {}
    assert isinstance(init, Mapping) and isinstance(phys, Mapping)
    gamma = _number(phys.get("gamma")) or 1.4
    rho = _number(init.get("rho")) or _number(_float_text(init.get("rho"))) or _number(phys.get("rho_ref"))
    p = _number(init.get("p")) or _number(_float_text(init.get("p"))) or _number(phys.get("p_ref"))
    return (rho, p, gamma) if rho and p else None


def _float_text(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def subsample(n: int, dim: int, density: int | None) -> list[int]:
    """Indices of the faces to draw: about `density` evenly along the zone (automatic: a few dozen in 2D)."""
    target = density if density and density > 0 else AUTO_ARROWS.get(dim, 100)
    stride = max(1, math.ceil(n / target))
    return list(range(stride // 2, n, stride)) if n else []


def from_values(zone: str, values: Values, dim: int, density: int | None = None) -> Inflow:
    """The inflow Mallard evaluated at the faces: its prescribed velocity, or its estimate of a mass-flow or
    stagnation inlet's; only where it points into the domain (a far field lets flow out elsewhere)."""
    assert values.velocity is not None
    inward = zip(values.velocity, values.normals, strict=False)
    picks = [i for i, (v, n) in enumerate(inward) if v[0] * n[0] + v[1] * n[1] + v[2] * n[2] > 0]
    keep = [picks[k] for k in subsample(len(picks), dim, density)]
    out = Inflow(zone, [values.centers[i] for i in keep], [values.velocity[i] for i in keep])
    out.estimate = values.estimated
    if values.estimated:
        out.source = "estimate from mallard: the inflow its flux makes of the initial state"
    else:
        out.source = "from mallard, at each face"
    if len(picks) < len(values.velocity):
        out.source += " (inflow part)"
    if not picks:
        out.source += ": none at the initial time"
        out.note = "no face lets flow in at the initial time"
    return out


def inflow(
    zone: str,
    entry: Mapping[str, Any],
    faces: Faces,
    data: Mapping[str, Any],
    dim: int,
    density: int | None = None,
    values: Values | None = None,
) -> Inflow | None:
    """The inflow vectors of a zone's `[[boundaries]]` entry, None when its condition lets no flow in: Mallard's
    `values` at its faces where the build gives them, else from the entry."""
    kind = str(entry.get("type", ""))
    if kind not in INFLOW_TYPES:
        return None
    if values is not None and values.velocity is not None and values.centers:
        return from_values(zone, values, dim, density)
    out = Inflow(zone)
    picks = subsample(len(faces.centers), dim, density)
    inward = [tuple(-c for c in faces.normals[i]) for i in picks]
    out.points = [faces.centers[i] for i in picks]
    turbulent = "turbulence" in entry
    direction = vector(entry.get("direction"), dim) if "direction" in entry else None
    if "direction" in entry and direction is None:
        out.note = "direction: an expression"

    def along(magnitude: float) -> list[Vec]:
        if direction is not None:
            d = unit(direction)
            return [(d[0] * magnitude, d[1] * magnitude, d[2] * magnitude) for _ in picks]
        return [(n[0] * magnitude, n[1] * magnitude, n[2] * magnitude) for n in inward]

    if kind in ("inlet_state", "farfield", "dirichlet"):
        u = entry.get("u")
        vec = _velocity_vector(u, dim)
        mag = speed(u) if not isinstance(u, list) else None
        if vec is not None:
            out.vectors = [vec for _ in picks]
            out.source = "uniform"
        elif mag is not None:
            out.vectors = along(mag)
            out.source = "speed along direction" if direction else "speed along the inward normal"
        elif isinstance(u, list) and (sampled := sample_expressions(u, out.points, dim)) is not None:
            out.vectors = sampled
            out.source = "profile, sampled at the faces"
        else:
            out.vectors, out.known = along(1.0), False
            out.source = "profile" if u is not None or "data" in entry else "no velocity given"
            out.note = out.note or "values from mallard: direction only"
        if turbulent:
            out.source = f"mean of synthetic inflow ({out.source})"
        if kind == "farfield" and out.known:
            keep = [k for k, n in enumerate(inward) if sum(a * b for a, b in zip(out.vectors[k], n, strict=True)) > 0]
            out.points = [out.points[k] for k in keep]
            out.vectors = [out.vectors[k] for k in keep]
            out.source += " (inflow part)"
        return out

    state = initial_state(data)
    if kind == "inlet_mass_flow":
        flux_vec = _velocity_vector(entry.get("mass_flux"), dim)
        flux = _number(entry.get("mass_flux"))
        mdot = _number(entry.get("mdot"))
        if flux is None and mdot is not None:
            area = sum(faces.measures)
            flux = mdot / area if area > 0 else None
        if state is not None and (flux is not None or flux_vec is not None):
            rho = state[0]
            if flux_vec is not None:
                out.vectors = [(flux_vec[0] / rho, flux_vec[1] / rho, flux_vec[2] / rho) for _ in picks]
            else:
                assert flux is not None
                out.vectors = along(flux / rho)
            out.source, out.estimate = "mass flux / initial density", True
            return out
        out.vectors, out.known = along(1.0), False
        out.source = "mass flow inlet"
        out.note = out.note or "speed unknown: no uniform mass flux or initial density"
        return out

    # inlet_stagnation: the speed of an isentropic expansion from p0 to the initial pressure
    p0 = _number(entry.get("p0"))
    if state is not None and p0 is not None and p0 > state[1]:
        rho, p, g = state
        v = math.sqrt(2 * g / (g - 1) * p / rho * ((p0 / p) ** ((g - 1) / g) - 1))
        out.vectors = along(v)
        out.source, out.estimate = "isentropic from p0 to the initial pressure", True
        return out
    out.vectors, out.known = along(1.0), False
    out.source = "stagnation inlet"
    out.note = out.note or "speed unknown: p0 is not above the initial pressure"
    return out


def _velocity_vector(value: Any, dim: int) -> Vec | None:
    if not isinstance(value, list) or len(value) != dim:
        return None
    comps = [speed(v) for v in value]
    if any(c is None for c in comps):
        return None
    xs = [float(c) for c in comps if c is not None] + [0.0] * (3 - dim)
    return xs[0], xs[1], xs[2]


def faces_of(poly: Any) -> Faces:
    """A zone block's faces from its polydata: cell centers, its `normal` cell array (outward), its `area` or
    `length` array."""
    from vtkmodules.vtkFiltersCore import vtkCellCenters

    cc = vtkCellCenters()
    cc.SetInputData(poly)
    cc.Update()
    pts = cc.GetOutput().GetPoints()
    n = poly.GetNumberOfCells()
    cd = poly.GetCellData()
    centers = [tuple(pts.GetPoint(i)) for i in range(pts.GetNumberOfPoints())] if pts else []

    def rows(name: str, comps: int) -> list[Any] | None:
        arr = cd.GetArray(name)
        if arr is None or arr.GetNumberOfTuples() != n:
            return None
        if comps == 1:
            return [arr.GetTuple1(i) for i in range(n)]
        return [tuple(arr.GetTuple3(i)) for i in range(n)]

    normals = rows("normal", 3) or [(0.0, 0.0, 0.0)] * n
    measures = rows("area", 1) or rows("length", 1) or [1.0] * n
    return Faces(centers, normals, measures)


SAME_SPEED = 1e-6  # speeds closer than this, relative to the largest, are one speed


def legend(flows: Sequence[Inflow], unit_text: str) -> dict[str, Any]:
    """The legend: the largest speed drawn (the longest arrow), whether the speeds differ (else the arrows are of
    one color and need no scale), its unit, and what the arrows are."""
    known = [f for f in flows if f.known and f.vectors]
    speeds = [math.sqrt(sum(c * c for c in v)) for f in known for v in f.vectors]
    top = max(speeds, default=0.0)
    return {
        "max": top,
        "varies": top - min(speeds, default=0.0) > SAME_SPEED * top,
        "unit": unit_text,
        "estimate": any(f.estimate for f in known),
        "unknown": [f.zone for f in flows if not f.known],
        "sources": sorted({f"{f.zone}: {f.source}" for f in flows}),
    }
