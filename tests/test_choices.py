"""What a key's value can be beyond its catalog type: files, names defined elsewhere, compositions. Session-level:
the inspector's rows for them and the composition edits, on the cylinder made a mixture."""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from trame.app import get_server

from mallard_gui import choices
from mallard_gui.mallard import Species, parse_species
from mallard_gui.session import Session
from mallard_gui.ui.app import App

from .conftest import H2O2_SPECIES, FakeMallard

MIXTURE = '[physics]\ntype = "navier_stokes"\ngas = "mixture"\nmechanism = "h2o2.yaml"\n'
W = {"H2": 2.016, "O2": 31.998, "AR": 39.948, "N2": 28.014}


def mixture(project: Path, initialize: str = "X = { H2 = 2.0, O2 = 1.0, AR = 7.0 }") -> None:
    text = (project / "input.toml").read_text()
    head, _, rest = text.partition("[physics]")
    rest = rest.split("\n[", 1)[1]
    text = f"{head}{MIXTURE}\n[{rest}".replace("[initialize]\n", f"[initialize]\n{initialize}\n", 1)
    (project / "input.toml").write_text(text)


class WithMasses(FakeMallard):
    """A build whose `list species` also gives molar masses."""

    def list_species(self, input_file: Path) -> Species | None:
        return Species(list(H2O2_SPECIES), {**dict.fromkeys(H2O2_SPECIES, 1.0), **W})


def session(project: Path, mallard: FakeMallard | None = None) -> Session:
    s = Session(project, mallard or FakeMallard())  # type: ignore[arg-type]
    s.refresh_catalog()
    s.check()
    return s


def row(s: Session, node: str, group: str, label: str) -> dict[str, Any]:
    g = next(g for g in s.properties(node)["groups"] if g["id"] == group)
    return next(r for r in g["rows"] if r["label"] == label)


# Files


def test_file_keys_come_from_catalog_metadata_not_a_list() -> None:
    assert choices.is_file_key("flame_table", "path", None, None)  # a catalog type saying so
    assert choices.is_file_key("file", "string", None, None)
    assert choices.is_file_key("reductions_file", "string", '"probe_reductions.csv"', None)
    assert choices.is_file_key("filename", "string", '"mesh.msh"', None)
    assert choices.is_file_key("mechanism", "string", None, "../../mechanisms/h2o2.yaml")
    assert choices.is_file_key("flame_table", "string", None, "flame.csv")
    assert not choices.is_file_key("mechanism", "string", None, None)  # unset, no default: the catalog is silent
    for value in ("ohmech", "x < 0.06 ? 1.5e3 : 2.0", "1.5e3", "./solut/cylinder", "high_order", "a.b c"):
        assert not choices.is_file_key("phase", "string", None, value), value
    assert not choices.is_file_key("file", "number", None, 3.0)


def test_rows_naming_files_get_the_file_dialog(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    assert row(s, "section:mesh", "mesh", "filename")["file"]
    assert row(s, "section:numerics", "numerics.face_reconstruction", "cache_file")["file"]
    assert row(s, "el:forces[0]", "forces[0]", "file")["file"]
    assert not row(s, "el:write_data[0]", "write_data[0]", "prefix")["file"]
    assert not row(s, "section:physics", "physics", "type")["file"]


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


_n = iter(range(10_000))


@pytest.fixture
def app(cylinder_project: Path, loop: asyncio.AbstractEventLoop) -> App:
    a = App(Session(cylinder_project, FakeMallard()), get_server(f"choices-{next(_n)}", client_type="vue3"))  # type: ignore[arg-type]
    a.ctrl.view_update = lambda: None
    return a


def test_a_file_chosen_is_stored_relative_to_the_case_when_inside_it(app: App, tmp_path_factory: Any) -> None:
    root = app.session.input_path.parent
    (root / "tables").mkdir()
    (root / "tables" / "probes.csv").write_text("x,y\n")
    app.inspector.choose_file("forces[0].file")
    req = app.state.path_request
    assert req["mode"] == "open" and req["title"] == "Choose forces.file"
    assert req["directory"] == str(root)
    assert req["filters"][0]["extensions"] == ["csv", "txt", "dat"]  # by the file's extension
    app.paths.reply(req["seq"], str(root / "tables" / "probes.csv"))
    assert app.session.doc.get("forces[0].file") == "tables/probes.csv"
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "restart.h5"
    elsewhere.write_text("")
    app.inspector.choose_file("initialize.file")
    req = app.state.path_request
    assert req["filters"] == [] and req["directory"] == str(root)  # any file
    app.paths.reply(req["seq"], str(elsewhere))
    assert app.session.doc.get("initialize.file") == str(elsewhere.resolve())


def test_the_servers_picker_lists_every_file_for_a_file_key(app: App) -> None:
    root = app.session.input_path.parent
    (root / "flame.table").write_text("")
    app.inspector.choose_file("initialize.file")
    app.paths.use_server_picker(app.state.path_request["seq"])
    names = {e["name"] for e in app.state.path_picker["entries"]}
    assert {"flame.table", "cylinder.msh", "input.toml"} <= names
    app.paths.picker_ok("flame.table")
    assert app.session.doc.get("initialize.file") == "flame.table"


# Names defined elsewhere


def test_keys_naming_zones_regions_and_species_offer_those_names(cylinder_project: Path) -> None:
    mixture(cylinder_project)
    s = session(cylinder_project)
    zone = row(s, "el:forces[0]", "forces[0]", "zone")
    assert (zone["widget"], zone["free"]) == ("select", True)
    assert {"cylinder", "farfield"} <= set(zone["options"])
    balance = row(s, "section:initialize", "initialize", "balance")
    assert (balance["widget"], balance["options"], balance["free"]) == ("select", list(H2O2_SPECIES), False)


def test_a_single_gas_offers_no_species_and_balance_stays_a_field(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    assert s.species is not None and s.species.names == []
    assert row(s, "section:initialize", "initialize", "balance")["widget"] == "text"


def test_the_species_list_is_mallards_and_a_failing_mechanism_says_why(cylinder_project: Path) -> None:
    assert parse_species({"species": ["H2", "O2"], "molar_masses": [2.016, 31.998]}).molar_masses == {
        "H2": 2.016,
        "O2": 31.998,
    }
    assert parse_species({"species": ["H2", "O2"]}).molar_masses == {}

    class Broken(FakeMallard):
        def list_species(self, input_file: Path) -> Species | None:
            from mallard_gui.presets import SetupError

            raise SetupError("E-INPUT: input.toml: line 5: physics.mechanism: cannot open the mechanism file")

    mixture(cylinder_project)
    s = session(cylinder_project, Broken())
    comp = row(s, "section:initialize", "initialize", "composition")["comp"]
    assert comp["species"] == [] and "cannot open the mechanism file" in comp["species_error"]


# Compositions


def test_x_and_y_are_one_composition_editor(cylinder_project: Path) -> None:
    mixture(cylinder_project)
    s = session(cylinder_project)
    g = next(g for g in s.properties("section:initialize")["groups"] if g["id"] == "initialize")
    labels = [r["label"] for r in g["rows"]]
    assert "composition" in labels and "X" not in labels and "Y" not in labels
    assert not any(gr["id"].startswith("initialize.X") for gr in s.properties("section:initialize")["groups"])
    comp = row(s, "section:initialize", "initialize", "composition")
    assert comp["set"] and comp["path"] == "initialize.X"
    c = comp["comp"]
    assert c["basis"] == "X" and [e["species"] for e in c["entries"]] == ["H2", "O2", "AR"]
    assert (c["sum"], c["normal"]) == ("10", False)
    assert "N2" in c["species"] and "H2" not in c["species"]  # what Add species offers


def test_unknown_species_and_both_bases_are_flagged(cylinder_project: Path) -> None:
    mixture(cylinder_project, "X = { H2 = 1.0, CH4 = 1.0 }\nY = { O2 = 1.0 }")
    s = session(cylinder_project)
    comp = row(s, "section:initialize", "initialize", "composition")
    assert "exactly one of X and Y" in comp["error"] and comp["severity"] == "error"
    assert [e["unknown"] for e in comp["comp"]["entries"]] == [False, True]
    s.write_composition("initialize", "X", {"H2": 1.0, "CH4": 1.0})
    comp = row(s, "section:initialize", "initialize", "composition")
    assert comp["error"] == "the mechanism has no species CH4"


def test_editing_a_composition_adds_renames_removes_and_normalizes(cylinder_project: Path) -> None:
    mixture(cylinder_project)
    s = session(cylinder_project)
    s.add_species("initialize", "N2")
    s.set_fraction("initialize", "N2", "10")
    s.rename_species("initialize", "AR", "H2O")
    s.set_fraction("initialize", "O2", "")
    assert tomllib.loads(s.doc.text)["initialize"]["X"] == {"H2": 2.0, "H2O": 7.0, "N2": 10.0}
    with pytest.raises(ValueError, match="already"):
        s.rename_species("initialize", "H2", "N2")
    with pytest.raises(ValueError, match="not below 0"):
        s.set_fraction("initialize", "H2", "-1")
    s.normalize_composition("initialize")
    x = tomllib.loads(s.doc.text)["initialize"]["X"]
    assert sum(x.values()) == pytest.approx(1.0) and x["N2"] == pytest.approx(10 / 19)
    assert row(s, "section:initialize", "initialize", "composition")["comp"]["normal"]
    assert s.doc.undo() and tomllib.loads(s.doc.text)["initialize"]["X"]["N2"] == 10.0  # one step each


def test_expressions_are_kept_and_cannot_be_normalized(cylinder_project: Path) -> None:
    mixture(cylinder_project, 'X = { H2 = "x < 0.5 ? 1 : 0", O2 = 1.0 }')
    s = session(cylinder_project)
    c = row(s, "section:initialize", "initialize", "composition")["comp"]
    assert c["sum"] == "" and [e["number"] for e in c["entries"]] == [False, True]
    with pytest.raises(ValueError, match="expression"):
        s.normalize_composition("initialize")


def test_switching_mole_to_mass_converts_with_mallards_molar_masses(cylinder_project: Path) -> None:
    mixture(cylinder_project, "X = { H2 = 2.0, O2 = 1.0, AR = 7.0 }")
    s = session(cylinder_project, WithMasses())
    assert row(s, "section:initialize", "initialize", "composition")["comp"]["convertible"]
    assert s.switch_basis("initialize", "Y")
    data = tomllib.loads(s.doc.text)["initialize"]
    assert "X" not in data
    m = 2 * 2.016 + 31.998 + 7 * 39.948
    assert data["Y"] == pytest.approx({"H2": 2 * 2.016 / m, "O2": 31.998 / m, "AR": 7 * 39.948 / m})
    assert s.switch_basis("initialize", "X")
    assert tomllib.loads(s.doc.text)["initialize"]["X"] == pytest.approx({"H2": 0.2, "O2": 0.1, "AR": 0.7})


def test_without_molar_masses_switching_asks_and_clears_only_when_told(cylinder_project: Path) -> None:
    mixture(cylinder_project)
    s = session(cylinder_project)
    assert not row(s, "section:initialize", "initialize", "composition")["comp"]["convertible"]
    before = s.doc.text
    assert not s.switch_basis("initialize", "Y")
    assert s.doc.text == before
    assert s.switch_basis("initialize", "Y", clear=True)
    assert "X =" not in s.doc.text and "Y =" not in s.doc.text
    comp = row(s, "section:initialize", "initialize", "composition")
    assert comp["comp"]["basis"] == "Y" and comp["comp"]["entries"] == []
    s.add_species("initialize", "O2")
    assert tomllib.loads(s.doc.text)["initialize"]["Y"] == {"O2": 0.0}
