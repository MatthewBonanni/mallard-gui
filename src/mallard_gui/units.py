"""Quantities with units in input fields, as Mallard reads them (its input guide's Units): a number, an
optional space, then a unit with SI prefixes, products, powers and quotients; `degC` and `degF` absolute.

The units, prefixes and exact factors are Mallard's own (`units.catalog`, loaded by `use_catalog`), and so are
the units a selector offers per dimension; the table below is for builds without that request.

A field shows the number and a unit selector. Typing `1 cm` sets both; changing the selector converts the
number exactly (rational arithmetic on the decimal text, so 1 in is 2.54 cm, not 2.5400000000000003). The
file keeps the user's unit as Mallard's quantity string (`"1 cm"`); the key's own SI unit is stored as a plain
number, Mallard's convention for SI. Nondimensional cases take plain numbers only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

# Dimension exponents: m, kg, s, K, mol, rad
Dim = tuple[int, int, int, int, int, int]
NONE: Dim = (0, 0, 0, 0, 0, 0)
_BASE = {
    "m": (1, 0, 0, 0, 0, 0),
    "kg": (0, 1, 0, 0, 0, 0),
    "s": (0, 0, 1, 0, 0, 0),
    "K": (0, 0, 0, 1, 0, 0),
    "mol": (0, 0, 0, 0, 1, 0),
}

PREFIXES = {
    "Y": Fraction(10**24), "Z": Fraction(10**21), "E": Fraction(10**18), "P": Fraction(10**15),
    "T": Fraction(10**12), "G": Fraction(10**9), "M": Fraction(10**6), "k": Fraction(10**3), "h": Fraction(100),
    "da": Fraction(10), "d": Fraction(1, 10), "c": Fraction(1, 100), "m": Fraction(1, 1000),
    "u": Fraction(1, 10**6), "µ": Fraction(1, 10**6), "n": Fraction(1, 10**9), "p": Fraction(1, 10**12),
    "f": Fraction(1, 10**15), "a": Fraction(1, 10**18), "z": Fraction(1, 10**21), "y": Fraction(1, 10**24),
}  # fmt: skip

_LBF = Fraction("4.4482216152605")
_IN = Fraction("0.0254")
# Units with their SI factor and dimension; those that take SI prefixes are marked.
UNITS: dict[str, tuple[Fraction, Dim, bool]] = {
    "m": (Fraction(1), (1, 0, 0, 0, 0, 0), True),
    "in": (_IN, (1, 0, 0, 0, 0, 0), False),
    "ft": (Fraction("0.3048"), (1, 0, 0, 0, 0, 0), False),
    "s": (Fraction(1), (0, 0, 1, 0, 0, 0), True),
    "min": (Fraction(60), (0, 0, 1, 0, 0, 0), False),
    "h": (Fraction(3600), (0, 0, 1, 0, 0, 0), False),
    "g": (Fraction(1, 1000), (0, 1, 0, 0, 0, 0), True),
    "lb": (Fraction("0.45359237"), (0, 1, 0, 0, 0, 0), False),
    "K": (Fraction(1), (0, 0, 0, 1, 0, 0), True),
    "degR": (Fraction(5, 9), (0, 0, 0, 1, 0, 0), False),
    "mol": (Fraction(1), (0, 0, 0, 0, 1, 0), True),
    "Pa": (Fraction(1), (-1, 1, -2, 0, 0, 0), True),
    "bar": (Fraction(100000), (-1, 1, -2, 0, 0, 0), True),
    "atm": (Fraction(101325), (-1, 1, -2, 0, 0, 0), False),
    "psi": (_LBF / (_IN * _IN), (-1, 1, -2, 0, 0, 0), False),
    "Torr": (Fraction(101325, 760), (-1, 1, -2, 0, 0, 0), False),
    "mmHg": (Fraction("133.322387415"), (-1, 1, -2, 0, 0, 0), False),
    "J": (Fraction(1), (2, 1, -2, 0, 0, 0), True),
    "cal": (Fraction("4.184"), (2, 1, -2, 0, 0, 0), True),
    "W": (Fraction(1), (2, 1, -3, 0, 0, 0), True),
    "N": (Fraction(1), (1, 1, -2, 0, 0, 0), True),
    "lbf": (_LBF, (1, 1, -2, 0, 0, 0), False),
    "Hz": (Fraction(1), (0, 0, -1, 0, 0, 0), True),
    "L": (Fraction(1, 1000), (3, 0, 0, 0, 0, 0), True),
}
# Absolute temperatures: K = (value + offset) * factor. They stand alone.
TEMPERATURES = {"degC": (Fraction("273.15"), Fraction(1)), "°C": (Fraction("273.15"), Fraction(1)),
                "degF": (Fraction("459.67"), Fraction(5, 9)), "°F": (Fraction("459.67"), Fraction(5, 9))}  # fmt: skip

# Units offered in the selector per dimension, the SI unit first.
CHOICES: dict[Dim, tuple[str, ...]] = {
    (1, 0, 0, 0, 0, 0): ("m", "cm", "mm", "µm", "km", "in", "ft"),
    (2, 0, 0, 0, 0, 0): ("m^2", "mm^2", "cm^2", "in^2", "ft^2"),
    (0, 0, 1, 0, 0, 0): ("s", "ms", "µs", "ns", "min", "h"),
    (0, 0, 0, 1, 0, 0): ("K", "°C", "°F", "degR"),
    (-1, 1, -2, 0, 0, 0): ("Pa", "kPa", "MPa", "bar", "mbar", "atm", "psi", "Torr"),
    (-3, 1, 0, 0, 0, 0): ("kg/m^3", "g/cm^3", "g/L", "lb/ft^3"),
    (1, 0, -1, 0, 0, 0): ("m/s", "km/h", "mm/s", "ft/s"),
    (2, 0, -2, 0, 0, 0): ("m^2/s^2", "ft^2/s^2"),
    (1, 0, -2, 0, 0, 0): ("m/s^2", "ft/s^2"),
    (2, 0, -2, -1, 0, 0): ("J/(kg K)", "kJ/(kg K)"),
    (0, 0, -1, 0, 0, 0): ("1/s", "Hz", "kHz"),
    (-1, 0, 0, 0, 0, 0): ("1/m", "1/mm"),
    (-1, 1, -1, 0, 0, 0): ("Pa s", "mPa s", "uPa s"),
    (0, 1, -3, 0, 0, 0): ("W/m^2", "kW/m^2", "MW/m^2"),
    (-2, 1, -1, 0, 0, 0): ("kg/(m^2 s)", "g/(m^2 s)"),
    (0, 1, -1, 0, 0, 0): ("kg/s", "g/s", "kg/h", "lb/s"),
    (2, 0, -1, 0, 0, 0): ("m^2/s",),
    (-3, 1, -1, 0, 0, 0): ("kg/(m^3 s)",),
    (-2, 1, -2, 0, 0, 0): ("N/m^3",),
    (-1, 1, -3, 0, 0, 0): ("W/m^3", "kW/m^3", "MW/m^3"),
}

_BUNDLED = (dict(UNITS), dict(TEMPERATURES), dict(PREFIXES), dict(CHOICES))
# The exponents of the unit catalog, in Dim's order.
_EXPONENTS = ("length", "mass", "time", "temperature", "amount", "angle")
# Where the units come from: "mallard" (its units.catalog) or "bundled" (the table above).
source = "bundled"


def _exact(text: str) -> Fraction:
    """A factor as the catalog writes it: a decimal, or a quotient of decimals or of pi and a decimal."""
    num, _, den = str(text).partition("/")
    a = Fraction(Decimal(repr(math.pi))) if num.strip() == "pi" else Fraction(Decimal(num.strip()))
    return a / Fraction(Decimal(den.strip())) if den else a


def _dim(exponents: Mapping[str, int]) -> Dim:
    d = tuple(int(exponents.get(k, 0)) for k in _EXPONENTS)
    return d  # type: ignore[return-value]


def use_catalog(catalog: Mapping[str, Any] | None) -> bool:
    """Take the units, prefixes and per-dimension choices from Mallard's `units.catalog`; None (a build without
    it) restores the bundled table. Returns whether the catalog is in use."""
    global source
    _install(*_BUNDLED)
    source = "bundled"
    if catalog is None or catalog.get("schema") != "mallard-unit-catalog":
        return False
    try:
        units: dict[str, tuple[Fraction, Dim, bool]] = {}
        temps: dict[str, tuple[Fraction, Fraction]] = {}
        for u in catalog["units"]:
            factor, dim = _exact(u["factor"]), _dim(u["exponents"])
            if u.get("offset") is not None:
                temps[str(u["symbol"])] = (_exact(u["offset"]), factor)
            else:
                units[str(u["symbol"])] = (factor, dim, bool(u["prefixable"]))
        prefixes = {str(p["symbol"]): _exact(p["factor"]) for p in catalog["prefixes"]}
        if not units:
            raise ValueError("units.catalog: no units")
        _install(units, temps, prefixes, _BUNDLED[3])
        CHOICES.update(_choices(catalog["dimensions"]))
    except (KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation):
        _install(*_BUNDLED)
        return False
    source = "mallard"
    return True


def _install(
    units: Mapping[str, tuple[Fraction, Dim, bool]],
    temps: Mapping[str, tuple[Fraction, Fraction]],
    prefixes: Mapping[str, Fraction],
    choices: Mapping[Dim, tuple[str, ...]],
) -> None:
    UNITS.clear()
    UNITS.update(units)
    TEMPERATURES.clear()
    TEMPERATURES.update(temps)
    PREFIXES.clear()
    PREFIXES.update(prefixes)
    CHOICES.clear()
    CHOICES.update(choices)


def _choices(dimensions: Sequence[Mapping[str, Any]]) -> dict[Dim, tuple[str, ...]]:
    """The units a selector offers per named dimension (the catalog's `examples`), the one of factor 1 first."""
    out: dict[Dim, tuple[str, ...]] = {}
    for d in dimensions:
        dim = _dim(d["exponents"])
        offered = [e.strip() for e in str(d.get("examples") or "").split(",") if e.strip()]
        if not offered or dim == NONE:
            continue
        si = next((e for e in offered if _unit_factor(e) == 1), None)
        if si is None:
            m = re.search(r"\(([^)]*)\)\s*$", str(d["dimension"]))
            si = m[1] if m else offered[0]
        out[dim] = (si, *[e for e in offered if e != si])
    return out


def _unit_factor(text: str) -> Fraction | None:
    if text in TEMPERATURES:
        return None
    try:
        return parse_unit(text).factor
    except UnitError:
        return None


_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_QUANTITY = re.compile(rf"^\s*({_NUMBER})\s*(.*?)\s*$")
_SUPERSCRIPTS = {"²": "^2", "³": "^3", "·": " "}


class UnitError(ValueError):
    pass


@dataclass(frozen=True)
class Unit:
    text: str
    factor: Fraction  # to SI
    dim: Dim
    offset: Fraction = Fraction(0)  # absolute temperatures: SI = (value + offset) * factor

    def to_si(self, value: Fraction) -> Fraction:
        return (value + self.offset) * self.factor

    def from_si(self, value: Fraction) -> Fraction:
        return value / self.factor - self.offset


def _symbol(name: str) -> tuple[Fraction, Dim]:
    if name in UNITS:
        f, d, _ = UNITS[name]
        return f, d
    if name == "kg":
        return Fraction(1), _BASE["kg"]
    for prefix in sorted(PREFIXES, key=len, reverse=True):
        if name.startswith(prefix) and name[len(prefix) :] in UNITS:
            f, d, prefixed = UNITS[name[len(prefix) :]]
            if prefixed:
                return PREFIXES[prefix] * f, d
    raise UnitError(f"unknown unit {name!r}")


def _tokens(text: str) -> list[str]:
    for a, b in _SUPERSCRIPTS.items():
        text = text.replace(a, b)
    text = text.replace("**", "^")
    return re.findall(r"\(|\)|/|\*|\^-?\d+|[A-Za-zµμ°]+|\d+", text)


def parse_unit(text: str) -> Unit:
    """A unit as Mallard writes them: `kPa`, `m/s^2`, `J/(mol K)`, `1/s`, `degC`."""
    text = text.strip()
    if text in TEMPERATURES:
        offset, factor = TEMPERATURES[text]
        return Unit(text, factor, _BASE["K"], offset)
    if not text:
        return Unit("", Fraction(1), NONE)
    toks = _tokens(text)
    pos = 0

    def group() -> tuple[Fraction, Dim]:
        nonlocal pos
        factor, dim = Fraction(1), NONE
        divide_next = False
        while pos < len(toks) and toks[pos] != ")":
            t = toks[pos]
            pos += 1
            if t == "*":
                continue
            if t == "/":
                divide_next = True
                continue
            if t == "(":
                f, d = group()
                if pos >= len(toks) or toks[pos] != ")":
                    raise UnitError(f"{text!r}: unbalanced parentheses")
                pos += 1
            elif t.isdigit() and t == "1":
                f, d = Fraction(1), NONE
            elif t.startswith("^"):
                raise UnitError(f"{text!r}: a power without a unit")
            else:
                f, d = _symbol(t)
            if pos < len(toks) and toks[pos].startswith("^"):
                n = int(toks[pos][1:])
                pos += 1
                f, d = f**n, tuple(x * n for x in d)  # type: ignore[assignment]
            if divide_next:
                f, d = 1 / f, tuple(-x for x in d)  # type: ignore[assignment]
                divide_next = False
            factor *= f
            dim = tuple(a + b for a, b in zip(dim, d, strict=True))  # type: ignore[assignment]
        return factor, dim

    factor, dim = group()
    if pos != len(toks):
        raise UnitError(f"{text!r}: unbalanced parentheses")
    return Unit(text, factor, dim)


def dimension_of(catalog_dimension: str | None) -> tuple[str, Dim] | None:
    """A key's dimension from the catalog's `pressure (kg m^-1 s^-2)`: its name and exponents."""
    if not catalog_dimension:
        return None
    m = re.match(r"^\s*(.*?)\s*\(([^)]*)\)\s*$", catalog_dimension)
    if not m:
        return None
    try:
        return m[1], parse_unit(m[2]).dim
    except UnitError:
        return None


@dataclass
class Quantity:
    number: str  # as typed, so conversions start from the exact decimal
    unit: Unit

    @property
    def value(self) -> Fraction:
        return Fraction(Decimal(self.number))

    def si(self) -> Fraction:
        return self.unit.to_si(self.value)


def parse_quantity(text: str) -> Quantity | None:
    """`"1 cm"` -> 1, cm; `"1e5"` -> no unit; None for anything else (an expression)."""
    m = _QUANTITY.match(text)
    if not m:
        return None
    try:
        Decimal(m[1])
    except InvalidOperation:
        return None
    rest = m[2]
    if rest and not re.match(r"^[A-Za-zµμ°(1/]", rest):
        return None
    try:
        unit = parse_unit(rest)
    except UnitError:
        if re.search(r"[-+*/^()<>?:,=]|\b(x|y|z|t|exp|sin|cos|sqrt)\b", rest):
            return None  # an expression such as "1 + x"
        raise
    return Quantity(m[1], unit)


def decimal_text(x: Fraction) -> tuple[str, bool]:
    """The shortest decimal text of `x` and whether it is exact (a terminating decimal); otherwise the
    shortest text that reads back as the same double."""
    d = x.denominator
    for p in (2, 5):
        while d % p == 0:
            d //= p
    if d == 1:
        dec = Decimal(x.numerator) / Decimal(x.denominator)
        text = format(dec.normalize(), "f") if abs(dec) < Decimal("1e16") else format(dec.normalize(), "E")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        if len(text.replace("-", "").replace(".", "").lstrip("0")) > 17:
            return repr(float(x)), False
        return text, True
    return repr(float(x)), False


def convert(number: str, src: Unit, dst: Unit) -> tuple[str, bool]:
    """`number` in `src` as a number in `dst`, exactly where a decimal can say it."""
    if src.dim != dst.dim:
        raise UnitError(f"{src.text or 'SI'} and {dst.text or 'SI'} are not of one dimension")
    return decimal_text(dst.from_si(src.to_si(Fraction(Decimal(number)))))


@dataclass
class Field:
    """What a dimensional field shows: the number, the unit selected, the units offered, and an error."""

    number: str
    unit: str
    choices: list[str]
    error: str = ""
    expression: bool = False


# Quantities whose unit a user can prefer (settings.toml's [units]), by name.
QUANTITIES: dict[str, Dim] = {
    "length": (1, 0, 0, 0, 0, 0),
    "time": (0, 0, 1, 0, 0, 0),
    "temperature": (0, 0, 0, 1, 0, 0),
    "pressure": (-1, 1, -2, 0, 0, 0),
    "density": (-3, 1, 0, 0, 0, 0),
    "velocity": (1, 0, -1, 0, 0, 0),
    "viscosity": (-1, 1, -1, 0, 0, 0),
    "mass flow": (0, 1, -1, 0, 0, 0),
}


def si_unit(dim: Dim) -> str:
    return CHOICES.get(dim, ("",))[0]


def field_of(
    value: object,
    catalog_dimension: str | None,
    nondimensional: bool,
    preferred: Mapping[Dim, str] | None = None,
    shown: str = "",
) -> Field | None:
    """The field of a key's value, or None when the key has no dimension (a plain field). A number (in SI, as the
    input holds it) shows in the unit `shown` (the one the user last chose for this key), else the `preferred`
    unit of its dimension, else SI; a value written with a unit keeps it."""
    found = dimension_of(catalog_dimension)
    if found is None:
        return None
    _, dim = found
    choices = list(CHOICES.get(dim, ()))
    if nondimensional:
        return Field("" if value is None else str(value), "", [], expression=isinstance(value, str))
    unit = _shown(shown, dim) or (preferred or {}).get(dim) or si_unit(dim)
    if unit not in choices:
        choices.append(unit)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Field(from_si(value, unit, dim), unit, choices)
    if isinstance(value, str):
        try:
            q = parse_quantity(value)
        except UnitError as e:
            return Field(value, "", choices, error=str(e), expression=True)
        if q is None:
            return Field(value, "", choices, expression=True)
        unit = q.unit.text or si_unit(dim)
        if unit not in choices:
            choices.append(unit)
        return Field(q.number, unit, choices)
    return Field("", unit, choices)


def _shown(unit: str, dim: Dim) -> str:
    try:
        return unit if unit and parse_unit(unit).dim == dim else ""
    except UnitError:
        return ""


def from_si(value: int | float, unit: str, dim: Dim) -> str:
    """An SI number as the field shows it in `unit`: exact where a decimal says it, else to 15 digits."""
    text = repr(value) if isinstance(value, float) else str(value)
    if not unit or unit == si_unit(dim):
        return text
    converted, exact = convert(text, parse_unit(si_unit(dim)), parse_unit(unit))
    return converted if exact else format(float(converted), ".15g")


def entry(text: str, dim: Dim, nondimensional: bool = False, preferred: Mapping[Dim, str] | None = None) -> Field:
    """The number and unit box of a page's own quantity (one not in the input), from its text as `entry_text`
    writes it: a plain number is in SI, an empty one starts in the `preferred` unit, a unit of another
    dimension stays in the number, to be reported."""
    text = text.strip()
    if nondimensional:
        return Field(text, "", [])
    choices = list(CHOICES.get(dim, ()))
    unit = ((preferred or {}).get(dim) if not text else None) or si_unit(dim)
    try:
        q = parse_quantity(text)
    except UnitError:
        q = None
    if q is None or not q.unit.text or q.unit.dim != dim:
        return Field(text, unit, choices)
    if q.unit.text not in choices:
        choices.append(q.unit.text)
    return Field(q.number, q.unit.text, choices)


def entry_text(number: str, unit: str, dim: Dim) -> str:
    """A page's own quantity from its number box and unit box: a unit typed in the number wins over the box."""
    number = number.strip()
    try:
        q = parse_quantity(number)
    except UnitError:
        return number
    if q is None or q.unit.text or not unit or unit == si_unit(dim):
        return number
    return f"{q.number} {unit}"


def entry_convert(number: str, old: str, new: str, dim: Dim) -> str:
    """A page's own quantity after its unit box changes from `old` to `new`: the number converted exactly."""
    try:
        q = parse_quantity(number.strip())
    except UnitError:
        q = None
    if q is None or q.unit.text or not old:
        return entry_text(number, new, dim)
    return entry_text(convert(q.number, parse_unit(old), parse_unit(new))[0], new, dim)


def store(text: str, unit: str, catalog_dimension: str | None, nondimensional: bool) -> tuple[object, str]:
    """The value to write for a field's text and selected unit, and the unit its number was in: a number in SI,
    the solver's units (`1 cm` and `1` in cm both write 0.01), or the text itself (an expression, in SI) with no
    unit. Raises UnitError with the message to show inline."""
    found = dimension_of(catalog_dimension)
    text = text.strip()
    if found is None:
        return text, ""
    name, dim = found
    q = parse_quantity(text)
    if q is None:
        return text, ""
    if q.unit.text:
        if nondimensional:
            raise UnitError("a nondimensional case takes plain numbers, without units")
        if q.unit.dim != dim:
            raise UnitError(
                f"{q.unit.text} is not a unit of {name}; use one of {', '.join(CHOICES.get(dim, ()) or ('SI',))}"
            )
        chosen = q.unit
    elif nondimensional or not unit:
        return _number(q.number), ""
    else:
        chosen = parse_unit(unit)
        if chosen.dim != dim:
            raise UnitError(f"{unit} is not a unit of {name}")
    if chosen.factor == 1 and chosen.offset == 0:
        return _number(q.number), chosen.text
    si, _ = decimal_text(chosen.to_si(q.value))
    return _number(si), chosen.text


def _number(text: str) -> int | float:
    d = Decimal(text)
    return int(d) if d == d.to_integral_value() and "." not in text and "e" not in text.lower() else float(text)
