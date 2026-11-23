"""A field list's picker logic, on `mallard list fields` as the build gives it for the cylinder (2D, perfect gas)."""

from __future__ import annotations

from mallard_gui.fields import FieldCatalog, toml_array

from .conftest import fixture_json


def catalog() -> FieldCatalog:
    return FieldCatalog(fixture_json("cylinder_fields.json"), fixture_json("field_catalog.json"))


def test_clearing_a_name_a_shorthand_covers_keeps_the_rest_and_keeps_what_writes_nothing() -> None:
    c = catalog()
    entries = ["RHO", "U", "FOO", "P"]
    assert c.toggle(entries, ["U_X"], on=False) == ["RHO", "U_Y", "FOO", "P"]
    assert c.toggle(entries, ["U_Y", "T", "NOT_A_FIELD"], on=True) == ["RHO", "U", "FOO", "P", "T"]
    assert toml_array(c.toggle(entries, ["RHO", "FOO"], on=False)) == '["U", "FOO", "P"]'


def test_a_name_is_flagged_with_what_it_needs_and_faces_take_the_surface_fields() -> None:
    c = catalog()
    assert c.problem("MASS_FLUX") == ""  # a cut plane's or a boundary zone's write_data writes it
    assert c.problem("MU_T") == "MU_T needs [les]"
    assert c.problem("FOO") == "FOO is not a field Mallard writes"
    assert c.problem("Y_*").startswith("Y_* needs a mixture")  # not Y_PLUS, a surface field
    assert "TAU_W" in c.covered("TAU_W") and "DELTA_OVER_ETA" not in c.names  # no write_data writes it
    assert [g["label"] for g in c.groups()][:3] == ["conservative", "primitive", "derived"]
