from __future__ import annotations

from fractions import Fraction

import pytest

from mallard_gui.units import (
    UnitError,
    convert,
    dimension_of,
    entry,
    entry_convert,
    entry_text,
    field_of,
    parse_quantity,
    parse_unit,
    store,
)

PRESSURE = "pressure (kg m^-1 s^-2)"
LENGTH = "length (m)"
TEMPERATURE = "temperature (K)"


def test_units_parse_as_mallard_writes_them() -> None:
    assert parse_unit("kPa").factor == 1000
    assert parse_unit("m/s^2").dim == (1, 0, -2, 0, 0, 0)
    assert parse_unit("J/(mol K)").dim == (2, 1, -2, -1, -1, 0)
    assert parse_unit("1/s").dim == (0, 0, -1, 0, 0, 0)
    assert parse_unit("g/cm³").factor == 1000
    assert parse_unit("Pa s").dim == (-1, 1, -1, 0, 0, 0)
    with pytest.raises(UnitError):
        parse_unit("furlong")


def test_catalog_dimensions_read_their_si_unit() -> None:
    assert dimension_of(PRESSURE) == ("pressure", (-1, 1, -2, 0, 0, 0))
    assert dimension_of("dynamic viscosity (kg m^-1 s^-1)") == ("dynamic viscosity", (-1, 1, -1, 0, 0, 0))
    assert dimension_of(None) is None


def test_conversions_are_exact() -> None:
    assert convert("1", parse_unit("in"), parse_unit("cm")) == ("2.54", True)
    assert convert("1", parse_unit("atm"), parse_unit("kPa")) == ("101.325", True)
    assert convert("25", parse_unit("degC"), parse_unit("K")) == ("298.15", True)
    assert convert("0.1", parse_unit("m"), parse_unit("mm")) == ("100", True)
    text, exact = convert("1", parse_unit("degF"), parse_unit("K"))
    assert not exact and float(text) == float((Fraction(1) + Fraction("459.67")) * Fraction(5, 9))
    with pytest.raises(UnitError):
        convert("1", parse_unit("K"), parse_unit("Pa"))


def test_quantities_and_expressions_are_told_apart() -> None:
    q = parse_quantity("1 cm")
    assert q is not None and q.number == "1" and q.unit.text == "cm"
    assert parse_quantity("2 * x") is None
    assert parse_quantity("300 + 50 * y") is None
    plain = parse_quantity("1e5")
    assert plain is not None and plain.unit.text == ""


def test_the_input_holds_si_numbers_whatever_unit_they_were_typed_in() -> None:
    assert store("1 cm", "m", LENGTH, False) == (0.01, "cm")  # the unit typed wins over the selector
    assert store("2.5", "mm", LENGTH, False) == (0.0025, "mm")
    assert store("2.5", "m", LENGTH, False) == (2.5, "m")
    assert store("101325 Pa", "bar", PRESSURE, False) == (101325, "Pa")
    assert store("20", "°C", TEMPERATURE, False) == (293.15, "°C")
    assert store("68 degF", "K", TEMPERATURE, False) == (293.15, "degF")
    value, unit = store("1", "psi", PRESSURE, False)
    assert unit == "psi" and value == pytest.approx(6894.757293168361, rel=1e-15)
    assert store("300 + 50 * y", "K", TEMPERATURE, False) == ("300 + 50 * y", "")  # an expression, in SI


def test_an_si_number_shows_in_the_unit_chosen_for_its_key() -> None:
    f = field_of(0.01, LENGTH, False, None, "cm")
    assert f is not None and (f.number, f.unit) == ("1", "cm")
    t = field_of(293.15, TEMPERATURE, False, None, "°C")
    assert t is not None and (t.number, t.unit) == ("20", "°C")
    p = field_of(6894.757293168361, PRESSURE, False, None, "psi")
    assert p is not None and p.number == "1"  # to 15 digits where no decimal is exact
    other = field_of(0.01, LENGTH, False, None, "Pa")  # a unit of another dimension is not shown
    assert other is not None and other.unit == "m"


def test_incompatible_and_unknown_units_are_inline_errors() -> None:
    with pytest.raises(UnitError, match="not a unit of pressure"):
        store("300 K", "Pa", PRESSURE, False)
    with pytest.raises(UnitError, match="unknown unit"):
        store("3 parsecs", "m", LENGTH, False)


def test_nondimensional_cases_take_plain_numbers() -> None:
    assert store("0.714", "", PRESSURE, True) == (0.714, "")
    with pytest.raises(UnitError, match="nondimensional"):
        store("1 atm", "", PRESSURE, True)
    f = field_of(0.714, PRESSURE, True)
    assert f is not None and f.choices == [] and f.unit == ""


def test_fields_show_number_and_unit() -> None:
    f = field_of("5 mm", LENGTH, False)
    assert f is not None and (f.number, f.unit) == ("5", "mm") and "m" in f.choices
    g = field_of(101325.0, PRESSURE, False)
    assert g is not None and (g.number, g.unit) == ("101325.0", "Pa")
    assert field_of(1.4, None, False) is None
    e = field_of("2 * x", LENGTH, False)
    assert e is not None and e.expression


VELOCITY = (1, 0, -1, 0, 0, 0)
KELVIN = (0, 0, 0, 1, 0, 0)


def test_a_pages_own_quantity_splits_into_number_and_unit_box_and_back() -> None:
    f = entry("40 cm/s", VELOCITY)
    assert (f.number, f.unit) == ("40", "cm/s") and f.choices[0] == "m/s"
    assert (entry("0.2", VELOCITY).number, entry("0.2", VELOCITY).unit) == ("0.2", "m/s")  # plain: SI
    assert entry("", (1, 0, 0, 0, 0, 0), preferred={(1, 0, 0, 0, 0, 0): "mm"}).unit == "mm"
    assert entry("3 kg", VELOCITY).number == "3 kg"  # another dimension stays to be reported
    assert entry("2 atm", (-1, 1, -2, 0, 0, 0)).unit == "atm"
    assert (entry("1 m^2", (2, 0, 0, 0, 0, 0)).number, entry("1 cm^2", (2, 0, 0, 0, 0, 0)).unit) == ("1", "cm^2")
    assert entry("300 K", KELVIN, nondimensional=True).choices == []
    assert entry_text("0.2", "cm/s", VELOCITY) == "0.2 cm/s"  # the box's unit
    assert entry_text("0.2", "m/s", VELOCITY) == "0.2"  # SI: a plain number
    assert entry_text("1 km/h", "cm/s", VELOCITY) == "1 km/h"  # a typed unit wins over the box
    assert entry_convert("1", "in", "cm", (1, 0, 0, 0, 0, 0)) == "2.54 cm"
    assert entry_convert("27", "degC", "K", KELVIN) == "300.15"
    assert entry_convert("", "m/s", "km/h", VELOCITY) == ""
