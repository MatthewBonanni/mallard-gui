"""Force coefficients from a `[[forces]]` file: Cd, Cl (and the side force in 3D) of the pressure and viscous
forces Mallard writes, with reference quantities given in units.

C = F · e / (q S), q = ρ U² / 2, with S the reference length in 2D (Mallard's forces are per unit depth there)
and the reference area in 3D. Drag is along the free stream at the angle of attack α in the x-y plane, lift
normal to it in that plane, the side force along z. The Strouhal number of a peak is f L / U.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .units import UnitError, parse_quantity

# The dimension of each reference, as units.py writes exponents (m, kg, s, K, mol).
DIMS = {
    "rho": ((-3, 1, 0, 0, 0, 0), "kg/m^3"),
    "velocity": ((1, 0, -1, 0, 0, 0), "m/s"),
    "length": ((1, 0, 0, 0, 0, 0), "m"),
    "area": ((2, 0, 0, 0, 0, 0), "m^2"),
    "alpha": ((0, 0, 0, 0, 0, 0), "deg"),
}
LABELS = {"rho": "ρ∞", "velocity": "U∞", "length": "L", "area": "S", "alpha": "α"}


@dataclass
class Reference:
    rho: float = 1.0
    velocity: float = 1.0
    length: float = 1.0
    area: float = 1.0  # 3D
    alpha: float = 0.0  # degrees

    @property
    def q(self) -> float:
        return 0.5 * self.rho * self.velocity**2

    def surface(self, dim: int) -> float:
        return self.length if dim == 2 else self.area


def parse_reference(texts: Mapping[str, str], nondimensional: bool) -> tuple[Reference, dict[str, str]]:
    """Each reference from its text (`"1.2 kg/m^3"`, `"30 m/s"`, `"2 deg"`); a number alone is in SI (degrees
    for α). Returns the reference and an error per field that does not parse."""
    ref = Reference()
    errors: dict[str, str] = {}
    for key, (dim, si) in DIMS.items():
        text = str(texts.get(key, "")).strip()
        if not text:
            continue
        if key == "alpha":
            try:
                ref.alpha = float(text.removesuffix("deg").removesuffix("°").strip())
            except ValueError:
                errors[key] = "an angle in degrees"
            continue
        try:
            q = parse_quantity(text)
        except UnitError as e:
            errors[key] = str(e)
            continue
        if q is None:
            errors[key] = f"a number, with a unit of {si}"
            continue
        if q.unit.text and nondimensional:
            errors[key] = "a nondimensional case takes plain numbers"
            continue
        if q.unit.text and q.unit.dim != dim:
            errors[key] = f"{q.unit.text} is not a unit of {si}"
            continue
        value = float(q.si())
        if value <= 0:
            errors[key] = "must be positive"
            continue
        setattr(ref, key, value)
    return ref, errors


def default_reference(resolved: Mapping[str, Any]) -> dict[str, str]:
    """Reference texts from the input's `[output] reference = {rho, u, p}` (Mallard's for CP and CF): ρ, |u|
    and the angle of u in the x-y plane; the length is the user's."""
    ref = (resolved.get("output") or {}).get("reference") or {}
    out = {"rho": "", "velocity": "", "length": "1", "area": "1", "alpha": "0"}
    rho, u = ref.get("rho"), ref.get("u")
    if isinstance(rho, (int, float, str)):
        out["rho"] = str(rho)
    if isinstance(u, list) and all(isinstance(c, (int, float)) for c in u) and u:
        out["velocity"] = repr(math.hypot(*u))
        out["alpha"] = f"{math.degrees(math.atan2(u[1], u[0])) if len(u) > 1 else 0.0:.6g}"
    return out


_FORCE = re.compile(r"^F_?([xyzXYZ])_([A-Za-z]+)$")  # F_X_PRESSURE (Mallard #1467), Fx_pressure before


def force_part(column: str) -> tuple[str, str] | None:
    """A forces file column's axis and part (`("x", "pressure")`), in either spelling; None for other columns."""
    m = _FORCE.match(column)
    return (m[1].lower(), m[2].lower()) if m else None


def force_columns(columns: Sequence[str]) -> dict[str, list[str]]:
    """The force components of a forces file by axis: `{"x": ["F_X_PRESSURE", "F_X_VISCOUS"], ...}`."""
    out: dict[str, list[str]] = {}
    for c in columns:
        if (fp := force_part(c)) is not None:
            out.setdefault(fp[0], []).append(c)
    return out


def coefficients(
    t: Sequence[float], forces: Mapping[str, Sequence[float]], ref: Reference, dim: int
) -> dict[str, list[float]]:
    """Cd, Cl and, in 3D, Cs over time, and the pressure and viscous parts of Cd: `{name: values}`."""
    a = math.radians(ref.alpha)
    drag = (math.cos(a), math.sin(a), 0.0)
    lift = (-math.sin(a), math.cos(a), 0.0)
    scale = 1.0 / (ref.q * ref.surface(dim))
    n = len(t)

    def component(axis: str, part: str = "") -> list[float]:
        cols = [k for k in forces if (fp := force_part(k)) and fp[0] == axis and (not part or fp[1] == part.strip("_"))]
        return [sum(forces[k][i] for k in cols) for i in range(n)]

    f = {ax: component(ax) for ax in "xyz"}
    out = {
        "Cd": [scale * (drag[0] * f["x"][i] + drag[1] * f["y"][i]) for i in range(n)],
        "Cl": [scale * (lift[0] * f["x"][i] + lift[1] * f["y"][i]) for i in range(n)],
    }
    if dim == 3:
        out["Cs"] = [scale * f["z"][i] for i in range(n)]
    for part in ("pressure", "viscous"):
        fx, fy = component("x", part), component("y", part)
        if any(fx) or any(fy):
            out[f"Cd_{part}"] = [scale * (drag[0] * fx[i] + drag[1] * fy[i]) for i in range(n)]
    return out
