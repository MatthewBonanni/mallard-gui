from __future__ import annotations

from mallard_gui.checklinks import referenced_keys

KEYS = {"run.cfl", "run.dt", "run.t_wall_stop", "boundaries.T", "les.model", "mesh.filename"}


def known(path: str) -> bool:
    return path in KEYS or any(k.startswith(path + ".") for k in KEYS)


def test_keys_named_by_mallards_messages() -> None:
    """The forms Mallard's diagnostics name keys in: a dotted key, an element's key, a section, a section's key."""
    assert referenced_keys(
        "CFL 1.5 at the initial state is above 1.2, the stability limit of SSPRK3: lower run.cfl (or run.dt)", known
    ) == ["run.cfl", "run.dt"]
    assert referenced_keys('bad.toml: line 31: boundaries[name = "farfield"].T: missing', known) == [
        'boundaries[name = "farfield"].T'
    ]
    assert referenced_keys("refine, or model the small scales with LES ([les])", known) == ["les"]
    assert referenced_keys("past [run] t_wall_stop", known) == ["run.t_wall_stop"]


def test_numbers_files_and_unknown_names_are_not_keys() -> None:
    message = "cells of 6.38 eta (see docs/guide/input_files.md#w-dns-eta, input.toml, run.bogus, [chemistry])"
    assert referenced_keys(message, known) == []
