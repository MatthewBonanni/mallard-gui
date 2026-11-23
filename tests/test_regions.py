from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mallard_gui.input_doc import InputDocument
from mallard_gui.setup import regions as rg

from .conftest import FIXTURES

BOUNDS = (-25.0, 25.0, -25.0, 25.0, 0.0, 0.0)


@pytest.fixture
def doc(tmp_path: Path) -> InputDocument:
    path = tmp_path / "input.toml"
    path.write_text((FIXTURES / "cylinder_input.toml").read_text(encoding="utf-8"), encoding="utf-8")
    return InputDocument(path)


def test_inline_regions_are_found_wherever_a_region_is_used(doc: InputDocument) -> None:
    found = {e.path: e.label for e in rg.regions(doc)}
    assert found["write_data[2].region"] == "near_wake"
    assert found["write_data[3].regions[0]"] == "wake 1"
    assert len([p for p in found if p.startswith("write_data[3].regions")]) == 3
    assert all(not p.startswith("probes") for p in found)  # a probe's geometry is not a region


def test_selector_paths_are_the_case_view_block_names(doc: InputDocument) -> None:
    assert rg.selector_path(doc.data, ["write_data", 2, "region"]) == 'write_data[prefix = "./solut/near_wake"].region'


def test_probes_with_a_geometry_are_listed_and_boundary_probes_are_not(doc: InputDocument) -> None:
    assert [e.label for e in rg.probes(doc)] == ["wake_point", "wake_arc"]


def test_promote_moves_the_table_to_regions_and_references_it(doc: InputDocument) -> None:
    path = rg.promote(doc, "write_data[2].region")
    assert path == "regions.near_wake"
    data = tomllib.loads(doc.text)
    assert data["write_data"][2]["region"] == "near_wake"
    assert data["regions"]["near_wake"] == {"type": "rectangle", "center": [3.0, 0.0], "size": [6.0, 3.0]}
    assert "# Often, and small" in doc.text  # the rest of the file is kept
    assert rg.uses(doc)["near_wake"] == ["write_data[2].region"]
    assert rg.regions(doc)[0].named


def test_add_region_names_it_freely_and_sizes_it_to_the_domain(doc: InputDocument) -> None:
    assert rg.add_region(doc, "rectangle", 2, BOUNDS) == "regions.rectangle"
    assert rg.add_region(doc, "rectangle", 2, BOUNDS) == "regions.rectangle_2"
    table = tomllib.loads(doc.text)["regions"]["rectangle"]
    assert table == {"type": "rectangle", "center": [0.0, 0.0], "size": [20.0, 10.0]}


def test_use_in_gives_a_feature_the_region(doc: InputDocument) -> None:
    rg.add_region(doc, "disk", 2, BOUNDS, name="heater")
    rg.use_in(doc, "heater", "write_data[0]")
    assert tomllib.loads(doc.text)["write_data"][0]["region"] == "heater"


def test_add_probe_appends_an_element_with_a_geometry(doc: InputDocument) -> None:
    index = rg.add_probe(doc, "line", 2, BOUNDS)
    el = tomllib.loads(doc.text)["probes"][index]
    assert el["type"] == "line" and el["name"] == "line" and el["n_points"] == 51
    assert len(rg.probes(doc)) == 3


def test_templates_exist_for_every_offered_type() -> None:
    for kind in {*rg.REGION_TYPES_2D, *rg.PROBE_TYPES_2D}:
        assert rg.template(kind, 2, BOUNDS)["type"] == kind
    for kind in {*rg.REGION_TYPES_3D, *rg.PROBE_TYPES_3D}:
        assert rg.template(kind, 3, (0, 1, 0, 1, 0, 1))["type"] == kind


def test_use_in_offers_the_elements_of_arrays_whose_region_key_the_catalog_has(doc: InputDocument) -> None:
    assert rg.features(doc, ["write_data.region", "probes.region", "probes.fields", "regions"]) == [
        *(f"write_data[{i}]" for i in range(5)),
        *(f"probes[{i}]" for i in range(3)),
    ]
