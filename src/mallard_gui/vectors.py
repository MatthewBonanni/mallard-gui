"""Vector keys: points, directions and velocities, entered one box per component (x/y/z, u/v/w for velocities),
as many as the case's dimension. The catalog types only a few of them (arrays with a dimension), so the rest are
named here from Mallard's input reference."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

VELOCITY = ("u", "v", "w")
SPACE = ("x", "y", "z")

# Catalog paths of keys that are a vector of the case's dimension
POINTS = {
    "periodic.center",
    "periodic.translation",
    "probes.point",
    "probes.start",
    "probes.end",
    "probes.pivot",
    "probes.translate",
    "probes.moment_center",
    "probes.region.center",
    "probes.region.pivot",
    "probes.region.translate",
    "probes.region.regions.center",
    "probes.region.regions.pivot",
    "probes.region.regions.translate",
    "write_data.point",
    "regions.<name>.center",
    "regions.<name>.pivot",
    "regions.<name>.translate",
    "regions.<name>.repeat.pivot",
    "regions.<name>.repeat.translate",
    "initialize.regions.region.center",
    "initialize.regions.region.point",
    "initialize.regions.region.normal",
    "initialize.regions.region.pivot",
    "initialize.regions.region.translate",
    "sources.region.center",
    "sources.region.pivot",
    "sources.region.translate",
    "mesh.transform.translate",
    "boundaries.origin",
    "boundaries.toward",
    "source.gravity",
    "source.mass_flow",
    "initialize.turbulence.period",
    "initialize.regions.turbulence.period",
    "periodic.axis",
    "mesh.transform.rotate.axis",
    "particles.lattice.lo",
    "particles.lattice.hi",
    "particles.gravity",
    "particles.quantities.winding.point",
    "particles.quantities.winding.axis",
    "statistics.phase.probe",
}
VELOCITIES = {
    "initialize.u",
    "initialize.regions.u",
    "boundaries.u",
    "sponges.u",
    "boundaries.turbulence.convection_velocity",
    "source.rhou",
    "sources.rhou",
    "particles.velocity",
}
COUNTS = {"particles.lattice.n"}  # an integer per direction
POINT_LISTS = {"particles.points", "boundaries.corners"}  # points, one row of boxes each
# Components may be expressions of x, y, z, t
EXPRESSIONS = (VELOCITIES - {"particles.velocity"}) | {"boundaries.direction"}
NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


@dataclass(frozen=True)
class Vector:
    labels: tuple[str, ...]
    expressions: bool
    integer: bool = False


def vector_of(catalog_path: str, value: Any, dim: int, array_with_dimension: bool = False) -> Vector | None:
    """The vector a key takes, or None: not a vector key, or a scalar already written (a speed with `direction`)."""
    known = catalog_path in POINTS | VELOCITIES | COUNTS or catalog_path == "boundaries.direction"
    if not (known or array_with_dimension):
        return None
    if value is not None and not (isinstance(value, list) and len(value) in (2, 3)):
        return None
    if isinstance(value, list) and any(isinstance(v, (list, dict, bool)) for v in value):
        return None
    n = len(value) if isinstance(value, list) else dim  # what is written, else the case's dimension
    if n not in (2, 3):
        return None
    names = VELOCITY if catalog_path in VELOCITIES else SPACE
    return Vector(names[:n], catalog_path in EXPRESSIONS, catalog_path in COUNTS)


def point_list_of(catalog_path: str, value: Any, dim: int) -> tuple[str, ...] | None:
    """The components of each point of a list of points, or None: not such a key, or a value of another shape."""
    if catalog_path not in POINT_LISTS:
        return None
    if value is None or value == []:
        n = dim
    elif isinstance(value, list) and all(isinstance(p, list) for p in value) and len({len(p) for p in value}) == 1:
        n = len(value[0])
        if any(isinstance(c, (bool, list, dict)) for p in value for c in p):
            return None
    else:
        return None
    return SPACE[:n] if n in (2, 3) else None


def component(text: str, expressions: bool, integer: bool = False) -> tuple[Any, str]:
    """A component as typed: a number, else (where allowed) an expression kept as text; with what is wrong."""
    text = text.strip()
    if not text:
        return None, "needed"
    if integer:
        return (int(text), "") if re.fullmatch(r"[+-]?\d+", text) else (None, "an integer")
    if NUMBER.match(text):
        number = float(text)
        return (int(number) if number.is_integer() and "." not in text and "e" not in text.lower() else number), ""
    if expressions:
        return text, ""
    return None, "a number"
