"""The gas model as one choice of Mallard's models (`mallard list models`): switching sets the model's keys, asks
for those it requires and removes those it does not read, in one undo step; no mapping of the GUI's own."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mallard_gui.session import Session

from .conftest import FakeMallard


def session(project: Path, models: bool = True) -> Session:
    fake = FakeMallard()
    fake.models_fixture = "gas_models.json" if models else ""
    s = Session(project, fake)  # type: ignore[arg-type]
    s.check()
    return s


def physics(s: Session) -> list[dict[str, Any]]:
    return list(s.properties("section:physics")["groups"])


def test_physics_leads_with_mallards_gas_models_in_place_of_the_gas_key(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    first = physics(s)[0]
    assert first["title"] == "GAS MODEL" and first["note"] == "Perfect gas"
    options = first["models"]["options"]
    assert [o["name"] for o in options] == ["perfect", "frozen", "finite_rate", "flamelet"]
    assert first["models"]["current"] == "perfect"
    reacting = next(o for o in options if o["name"] == "finite_rate")
    assert reacting["missing"] == ["physics.mechanism"]
    assert reacting["changes"] == ['physics.gas = "mixture"', "chemistry.enabled = true", "remove physics.units"]
    assert reacting["docs"].endswith("docs/guide/thermochemistry/#finite-rate-chemistry")
    rows = [r["path"] for g in physics(s) for r in g["rows"]]
    assert "physics.gas" not in rows


def test_switching_sets_requires_and_removes_mallards_keys_in_one_step(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    with pytest.raises(ValueError, match=r"physics\.mechanism"):
        s.set_gas_model("finite_rate")
    s.set_gas_model("finite_rate", {"physics.mechanism": "h2o2.yaml"})
    assert s.doc.get("physics.gas") == "mixture" and s.doc.get("physics.mechanism") == "h2o2.yaml"
    assert s.doc.get("chemistry.enabled") is True
    s.check()
    assert physics(s)[0]["models"]["current"] == "finite_rate"
    s.set_gas_model("flamelet", {"physics.table": "fpv.h5"})
    assert s.doc.get("physics.gas") == "flamelet" and s.doc.get("physics.table") == "fpv.h5"
    assert not s.doc.has("physics.mechanism") and not s.doc.has("chemistry")
    s.doc.undo()
    assert s.doc.get("physics.gas") == "mixture" and s.doc.has("chemistry")


def test_a_build_without_the_listing_keeps_the_gas_key(cylinder_project: Path) -> None:
    s = session(cylinder_project, models=False)
    groups = physics(s)
    assert groups[0]["title"] != "GAS MODEL"
    assert "physics.gas" in [r["path"] for g in groups for r in g["rows"]]
