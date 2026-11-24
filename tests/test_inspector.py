"""The inspector's catalog side without the UI: every key a table takes, set or not; writing an unset key into its
table and resetting it; entries added, duplicated and deleted (with undo); and presets as Mallard resolves them."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from mallard_gui.mallard import KeyInfo, Mallard, parse_catalog, site_url
from mallard_gui.presets import SetupError, compute, parse_preset_line
from mallard_gui.session import Session, catalog_path

from .conftest import FakeMallard, fixture_json


def session(project: Path, mallard: FakeMallard | None = None) -> Session:
    s = Session(project, mallard or FakeMallard())  # type: ignore[arg-type]
    s.refresh_catalog()
    s.check()
    return s


def groups(s: Session, node: str) -> dict[str, dict[str, Any]]:
    return {g["id"]: g for g in s.properties(node)["groups"]}


def rows(s: Session, node: str, group: str) -> dict[str, dict[str, Any]]:
    return {r["label"]: r for r in groups(s, node)[group]["rows"]}


def every_row(s: Session, node: str) -> dict[str, dict[str, Any]]:
    """The labeled rows of a page, in its alternatives' groups too."""
    return {r["label"]: r for g in groups(s, node).values() for r in g["rows"] if r["label"]}


class ExtraCatalog(FakeMallard):
    """A build whose general catalog has keys the captured one does not."""

    def __init__(self, extra: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.extra_keys = extra

    def list_keys(self, input_file: Path | None = None) -> tuple[dict[str, KeyInfo] | None, list[Any]]:
        if input_file is not None:
            return super().list_keys(input_file)
        data = fixture_json("general_keys.json")
        data["keys"].update(self.extra_keys)
        return parse_catalog(data), []


def test_a_section_the_input_leaves_out_lists_every_key_with_its_default(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    assert "les" not in s.doc.data
    les = rows(s, "section:les", "les")
    assert set(les) == {"C", "Pr_t", "Sc_t", "dynamic", "filter_width", "gradients", "model"}
    assert not any(r["set"] for r in les.values())
    # The case is 2D: the catalog's per-dimension default, as a placeholder
    assert (les["model"]["placeholder"], les["model"]["widget"]) == ("wale", "select")
    assert les["C"]["placeholder"] == "0.07"
    assert les["model"]["help"]["values"] == "sigma, smagorinsky, vreman, wale"
    # Its tables too, collapsed while they set nothing, with their own keys
    nested = groups(s, "section:les")["les.combustion"]
    assert nested["collapsed"] and nested["depth"] == 1
    assert "T_burnt" in {r["label"] for r in nested["rows"]}
    assert "les" not in s.doc.data  # showing them writes nothing


def test_setting_an_unset_key_writes_it_into_its_table(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    s.set_value("les.model", "smagorinsky")
    s.set_value("les.combustion.n_res", "3")
    s.set_value("numerics.face_reconstruction.bound_preserving", "true")
    text = s.doc.text
    assert '[les]\nmodel = "smagorinsky"\ncombustion.n_res = 3' in text
    assert "order = 3\nbound_preserving = true" in text
    assert text.startswith("# Flow past a circular cylinder")  # comments kept
    data = tomllib.loads(text)
    assert data["les"] == {"model": "smagorinsky", "combustion": {"n_res": 3}}
    assert rows(s, "section:les", "les")["model"]["set"]


def test_reset_removes_a_key_so_its_default_applies(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    s.set_value("les.model", "vreman")
    s.reset_key("les.model")
    assert "model" not in s.doc.data["les"]
    row = rows(s, "section:les", "les")["model"]
    assert not row["set"] and row["placeholder"] == "wale"


def test_keys_another_dimension_reads_are_marked(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    write_data = rows(s, "el:write_data[0]", "write_data[0]")
    assert write_data["fields"]["other_dim"] == ""
    assert rows(s, "el:write_data[0]", "write_data[0]#unread")["boundary"]["other_dim"] == "3D only"


def test_entries_of_arrays_of_tables_are_added_duplicated_and_deleted(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    for array in ("sponges", "sources"):
        before = s.doc.text
        new = s.add_entry(array)
        assert new == f"{array}[0]" and f"[[{array}]]" in s.doc.text
        assert s.node_of_key(new) == f"el:{new}"
        assert "strength" in rows(s, f"el:{new}", new) or "rho" in rows(s, f"el:{new}", new)
        copy = s.duplicate_entry(new)
        assert copy == f"{array}[1]" and len(s.doc.data[array]) == 2
        s.delete_entry(new)
        assert len(s.doc.data[array]) == 1
        assert s.doc.undo() and len(s.doc.data[array]) == 2
        s.delete_entry(f"{array}[1]")
        s.delete_entry(f"{array}[0]")
        assert array not in s.doc.data
        assert tomllib.loads(s.doc.text) == tomllib.loads(before)


def test_a_duplicate_gets_its_own_name(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    new = s.duplicate_entry("probes[0]")
    assert s.doc.get(f"{new}.name") == s.doc.get("probes[0].name") + "_copy"
    assert s.doc.data["probes"][1]["name"] == "wake_point_copy"


def test_a_section_shows_its_arrays_with_add_and_each_entry_with_duplicate_and_delete(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    write_data = groups(s, "section:output")["write_data"]
    assert write_data["add"]["path"] == "write_data"
    assert [r["entry"] for r in write_data["rows"]] == [f"write_data[{i}]" for i in range(5)]
    assert groups(s, "section:sponges")["sponges"]["note"] == "none yet"


def test_arrays_inside_a_table_add_entries_in_place(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    head = groups(s, "section:initialize")["initialize.regions"]
    assert head["add"]["path"] == "initialize.regions"
    new = s.add_entry("initialize.regions")
    assert new == "initialize.regions[0]"
    assert tomllib.loads(s.doc.text)["initialize"]["regions"] == [{}]
    assert s.doc.text.index("[[initialize.regions]]") < s.doc.text.index("[[boundaries]]")
    entry = groups(s, "section:initialize")["initialize.regions[0]"]
    assert entry["entry"] == new and "region" in {
        g["id"].rsplit(".", 1)[-1] for g in s.properties("section:initialize")["groups"]
    }


def test_the_catalog_alone_decides_keys_and_entry_types(cylinder_project: Path) -> None:
    """A key and a repeatable entry the GUI has never heard of appear, with Add offering the entry's types."""
    extra = {
        "les.backscatter": {"type": "bool", "default": "false", "dims": [2, 3]},
        "injectors": {"type": "array", "dims": [2, 3]},
        "injectors.name": {"type": "string", "required": True, "dims": [2, 3]},
        "injectors.type": {"type": "string", "values": ["cone", "point"], "dims": [2, 3]},
        "injectors.rate": {"type": "number", "required": True, "dims": [2, 3]},
    }
    s = session(cylinder_project, ExtraCatalog(extra))
    assert rows(s, "section:les", "les")["backscatter"]["placeholder"] == "false"
    assert "section:injectors" in {n["id"] for n in s.tree()}
    add = groups(s, "section:injectors")["injectors"]["add"]
    assert add == {"path": "injectors", "types": ["cone", "point"]}
    new = s.add_entry("injectors", "cone")
    # The type chosen and a unique name; the required rate is left for the user, flagged until set
    assert s.doc.data["injectors"] == [{"type": "cone", "name": "injectors_1"}]
    rate = rows(s, f"el:{new}", new)["rate"]
    assert rate["required"] and not rate["set"]


def test_without_mallards_catalog_the_panel_says_why(cylinder_project: Path) -> None:
    class NoCatalog(FakeMallard):
        def list_keys(self, input_file: Path | None = None) -> tuple[dict[str, KeyInfo] | None, list[Any]]:
            if input_file is None:
                from mallard_gui.mallard import Diagnostic

                return None, [Diagnostic("error", "E-LIST", "the key catalog docs/input-keys is in none of ...")]
            return super().list_keys(input_file)

    s = session(cylinder_project, NoCatalog())
    assert "docs/input-keys" in s.general_error


# Presets


def test_the_preset_line_names_the_preset_and_the_keys_set_over_it() -> None:
    line = parse_preset_line('les (the input sets numerics.riemann_solver = "RHLL", run.cfl = 1)')
    assert line is not None and (line.name, line.default) == ("les", False)
    assert line.overrides == {"numerics.riemann_solver": '"RHLL"', "run.cfl": "1"}
    default = parse_preset_line("high_order (the default since 0.10.0, first order before: ...)")
    assert default is not None and default.default and default.overrides == {}
    assert parse_preset_line(None) is None


def preset_input(project: Path, numerics: str, cfl: bool = False) -> None:
    path = project / "input.toml"
    text = path.read_text()
    start, end = text.index("[numerics]"), text.index("[physics]")
    text = text[:start] + numerics + "\n" + text[end:]
    if not cfl:
        text = text.replace("cfl = 1.0\n", "")
    path.write_text(text)


def test_presets_come_from_mallards_resolution(cylinder_project: Path) -> None:
    preset_input(cylinder_project, '[numerics]\npreset = "les"\nriemann_solver = "RHLL"\n')
    s = session(cylinder_project)
    view = s.refresh_presets()["numerics.preset"]
    assert (view.active, view.explicit) == ("les", True)
    assert view.overrides == {"numerics.riemann_solver": "RHLL"}
    # What each preset fills is what the resolutions differ in
    assert view.fills["les"]["numerics.time_integrator"] == "SSPRK54"
    assert view.fills["les"]["run.cfl"] == 2.0
    assert view.fills["second_order"]["numerics.riemann_solver"] == "RHLL"
    assert view.effective["numerics.convective_flux"] == "hybrid"
    assert view.to_dict()["status"] == "preset les, 1 override"
    rows_ = rows(s, "section:numerics", "numerics")
    assert rows_["riemann_solver"]["origin"] == "override"
    assert (rows_["convective_flux"]["origin"], rows_["convective_flux"]["placeholder"]) == ("preset", "hybrid")
    assert every_row(s, "section:run")["cfl"]["origin"] == "preset"


def test_a_preset_value_mallard_changes_shows_without_a_gui_change(cylinder_project: Path) -> None:
    class NewerPresets(FakeMallard):
        def resolve(self, input_file: Path) -> dict[str, Any] | None:
            out = super().resolve(input_file)
            if out is not None and out["numerics"].get("preset") == "les":
                out["numerics"]["time_integrator"] = "RK4"
            return out

    preset_input(cylinder_project, '[numerics]\npreset = "les"\n')
    s = session(cylinder_project, NewerPresets())
    view = s.refresh_presets()["numerics.preset"]
    assert view.fills["les"]["numerics.time_integrator"] == "RK4"
    assert "RK4" in view.summary("les")


def test_switching_presets_says_what_changes_and_keeps_or_drops_overrides(cylinder_project: Path) -> None:
    preset_input(cylinder_project, '[numerics]\npreset = "les"\nriemann_solver = "HLL"\n')
    s = session(cylinder_project)
    s.refresh_presets()
    keep, error = s.preset_switch("numerics.preset", "second_order", keep=True)
    assert error is None
    changed = {c["key"]: (c["old"], c["new"]) for c in keep}
    assert changed["numerics.time_integrator"] == ("SSPRK54", "SSPRK3")
    assert changed["run.cfl"] == ("2.0", "0.5")
    assert "numerics.riemann_solver" not in changed  # kept: the input's HLL
    drop, _ = s.preset_switch("numerics.preset", "second_order", keep=False)
    assert ("numerics.riemann_solver", "HLL", "RHLL") in {(c["key"], c["old"], c["new"]) for c in drop}
    s.apply_preset("numerics.preset", "second_order", keep=False)
    assert s.doc.data["numerics"] == {"preset": "second_order"}
    view = s.refresh_presets()["numerics.preset"]
    assert (view.active, view.overrides) == ("second_order", {})


def test_choosing_a_preset_over_set_keys_offers_to_drop_them(cylinder_project: Path) -> None:
    preset_input(cylinder_project, '[numerics]\nriemann_solver = "HLL"\n')
    s = session(cylinder_project)
    view = s.refresh_presets()["numerics.preset"]
    assert view.active in (None, "high_order") and "numerics.riemann_solver" in view.overrides_for("les")
    s.apply_preset("numerics.preset", "les", keep=False)
    assert s.doc.data["numerics"] == {"preset": "les"}


def test_reverting_an_override_hands_the_key_back_to_the_preset(cylinder_project: Path) -> None:
    preset_input(cylinder_project, '[numerics]\npreset = "les"\ntime_integrator = "RK4"\n')
    s = session(cylinder_project)
    s.refresh_presets()
    s.reset_key("numerics.time_integrator")
    view = s.refresh_presets()["numerics.preset"]
    assert view.overrides == {} and view.effective["numerics.time_integrator"] == "SSPRK54"


def test_dropping_overrides_also_drops_keys_the_new_preset_leaves_unused() -> None:
    def resolve(text: str) -> dict[str, Any]:
        uses_order = 'preset = "b"' not in text
        return {"t": {"preset": "a", "x": 1, **({"order": 3} if uses_order else {})}}

    view = compute('[t]\npreset = "a"\norder = 5\n', "t.preset", ["a", "b"], resolve, lambda _: None)
    assert view.overrides_for("b") == ["t.order"]


def all_rows(s: Session) -> list[dict[str, Any]]:
    sections = [n["id"] for n in s.tree() if n["depth"] == 0]
    return [r for node in sections for g in s.properties(node)["groups"] for r in g["rows"]]


def test_only_keys_the_cases_catalog_requires_are_starred(cylinder_project: Path) -> None:
    """The general catalog marks `initialize.file` required (a restart reads it); this case does not read it."""
    s = session(cylinder_project)
    assert s.general["initialize.file"].required and "initialize.file" not in s.catalog
    assert "file" not in rows(s, "section:initialize", "initialize")  # an analytical initialize does not read it
    assert not groups(s, "section:les")["les.combustion"]["rows"][0]["required"]  # a table the case leaves out
    assert not any(r["required"] for r in groups(s, "section:les")["les.combustion"]["rows"])
    starred = {catalog_path(r["path"]) for r in all_rows(s) if r["required"]}
    assert starred, "the case's own required keys are still starred"
    for key in starred:
        info = s.catalog.get(key)
        assert info is not None and info.required, key


def test_every_entry_that_can_be_added_can_be_deleted_where_it_is_shown(cylinder_project: Path) -> None:
    """After Add, the app shows the new entry's node: that page has its Duplicate and Delete, and Delete undoes."""
    s = session(cylinder_project)
    original = s.doc.text
    adds = {
        (g["add"]["path"], t)
        for n in s.tree()
        if n["depth"] == 0
        for g in s.properties(n["id"])["groups"]
        if g["add"]
        for t in (g["add"]["types"] or [""])[:1]
    }
    assert {"triggers", "sponges", "initialize.regions", "regions"} <= {a for a, _ in adds}
    for array, type_ in sorted(adds):
        new = s.add_entry(array, type_)
        page = s.properties(s.node_of_key(new) or "")
        shown = [g["entry"] for g in page["groups"]] + [r["entry"] for g in page["groups"] for r in g["rows"]]
        assert new in shown, (array, page["id"])
        if new.startswith("regions."):
            region = s.properties(f"region:{new}")
            assert region["groups"][0]["entry"] == new
        s.delete_entry(new)
        assert not s.doc.has(new)
        assert s.doc.undo() and s.doc.has(new)
        s.set_text(original)


RESOLVE_STDERR = """warning W-FORMAT-VTU: [[write_data]] writes VTU files: set format = "vtu" to silence this.
  docs/guide/running.md#w-format-vtu
error E-INPUT: {name}: line 156: triggers[0].when: missing; this key is required here. Add when = "...".
keys that are unknown or unused with these settings (mallard check lists them all):
  line 20: initialize.rho (did you mean output.reference.rho?)
  docs/guide/input_files.md#e-input
"""


def test_when_the_input_does_not_set_up_the_preset_bar_shows_mallards_error(cylinder_project: Path) -> None:
    """The cylinder demo's unsaved text had a [[triggers]] entry without `when`: the bar said only that resolve
    "could not set this case up". It names Mallard's error, in the input as the user knows it."""
    script = cylinder_project / "fake-mallard"
    stderr = cylinder_project / "stderr.txt"
    script.write_text(f'#!/bin/sh\nsed "s/{{name}}/$3/" {stderr} >&2\nexit 1\n')
    script.chmod(0o755)
    stderr.write_text(RESOLVE_STDERR)
    real = Mallard(script)

    class Failing(FakeMallard):
        def resolve(self, input_file: Path) -> dict[str, Any] | None:
            return real.resolve(input_file)

    s = session(cylinder_project, Failing())
    view = s.refresh_presets()["numerics.preset"]
    assert view.error == (
        "mallard input resolve: E-INPUT: input.toml: line 156: triggers[0].when: missing; this key is required "
        'here. Add when = "...". (values shown are the input\'s)'
    )
    assert "scratch" not in view.to_dict()["error"]
    try:
        real.resolve(cylinder_project / "input.toml")
    except SetupError as e:
        assert str(e).startswith("E-INPUT: input.toml: line 156")
    else:
        raise AssertionError("no SetupError")


def test_a_2d_case_offers_no_key_only_3d_reads(cylinder_project: Path) -> None:
    """`write_data.boundary` and `boundary_type` are read in 3D only (the catalog's dims): mallard2d check rejects
    them as unused, so a 2D write_data entry does not offer them; they are listed, not editable, folded away."""
    s = session(cylinder_project)
    assert s.case_dim == 2 and s.general["write_data.boundary"].dims == [3]
    page = groups(s, "el:write_data[0]")
    own = {r["label"]: r for r in page["write_data[0]"]["rows"]}
    assert "boundary" not in own and "boundary_type" not in own
    unread = page["write_data[0]#unread"]
    assert unread["collapsed"] and unread["title"] == "not read in 2D"
    assert {r["label"]: (r["readonly"], r["value"]) for r in unread["rows"]}["boundary"] == (True, "3D only")
    walked = 0
    for n in s.tree():
        if n["depth"] != 0 and not n["id"].startswith("el:"):
            continue
        for g in s.properties(n["id"])["groups"]:
            for r in g["rows"]:
                if r["path"] and ":" not in r["path"] and not s.applies(r["path"]):
                    assert r["readonly"], (n["id"], r["path"])
                    walked += 1
            assert not g["add"] or s.applies(g["add"]["path"]), g["id"]
    assert walked > 20
    # 2D reads forces.zone although no 2D catalog input does (dims [3]): the case's catalog read it typed
    assert s.general["forces.zone"].dims == [3] and not rows(s, "el:forces[0]", "forces[0]")["zone"]["readonly"]


def test_a_3d_only_key_the_2d_input_sets_is_flagged_with_reset(cylinder_project: Path) -> None:
    text = (cylinder_project / "input.toml").read_text()
    text = text.replace('prefix = "./solut/cylinder"\n', 'prefix = "./solut/cylinder"\nboundary = "cylinder"\n', 1)
    (cylinder_project / "input.toml").write_text(text)
    s = session(cylinder_project)
    row = rows(s, "el:write_data[0]", "write_data[0]")["boundary"]
    assert row["readonly"] and row["set"] and row["severity"] == "warning"
    assert "does not read boundary" in row["error"]
    s.reset_key("write_data[0].boundary")
    assert "boundary" not in s.doc.data["write_data"][0]


def test_an_unset_key_without_a_default_reads_not_set_not_a_value(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    run = every_row(s, "section:run")
    # The catalog's -1 sentinel is no default: the run has no wall-time limit
    wall = run["t_wall_stop"]
    assert (wall["placeholder"], wall["unset"]) == ("not set: disabled", "not set: disabled")
    assert "-1" not in run["t_wall_stop"]["help"]["default"]
    # No default and no description: no claim about what Mallard does (it derives dt from cfl)
    assert (run["dt"]["placeholder"], run["dt"]["unset"]) == ("not set", "not set")
    assert "disabled" not in run["dt"]["help"]["default"]
    assert (run["n_steps"]["placeholder"], run["n_steps"]["unset"]) == ("0", "")


def test_an_unset_key_its_description_calls_automatic_reads_automatic(cylinder_project: Path) -> None:
    dt = fixture_json("general_keys.json")["keys"]["run.dt"]
    auto = {"run.dt": {**dt, "description": "Time step; chosen from cfl if omitted"}}
    run = every_row(session(cylinder_project, ExtraCatalog(auto)), "section:run")
    assert (run["dt"]["placeholder"], run["dt"]["unset"]) == ("not set: automatic", "not set: automatic")


# Run limits as Mallard's catalog states them since #1400: no default, and what leaving them out means
UNSET_KEYS = {
    "run.t_wall_stop": {"type": "number", "dimension": "time (s)", "dims": [2, 3], "unset": "no wall-clock limit"},
    "run.n_steps": {"type": "integer", "dims": [2, 3], "unset": "no step limit"},
}


def test_an_unset_key_reads_what_the_catalog_says_leaving_it_out_means(cylinder_project: Path) -> None:
    fake = FakeMallard()
    fake.key_entries = UNSET_KEYS
    run = every_row(session(cylinder_project, fake), "section:run")
    assert run["t_wall_stop"]["placeholder"] == "not set: no wall-clock limit"
    assert run["t_wall_stop"]["help"]["default"] == "none: no wall-clock limit"
    assert run["n_steps"]["placeholder"] == "not set: no step limit"
    assert (run["step_queue"]["placeholder"], run["step_queue"]["unset"]) == ("3", "")


def test_a_catalog_stating_unset_drops_the_guesses_for_older_catalogs(cylinder_project: Path) -> None:
    dt = {
        "type": "number",
        "dimension": "time (s)",
        "dims": [2, 3],
        "description": "Time step; chosen from cfl if omitted",
    }
    fake = FakeMallard()
    fake.key_entries = {**UNSET_KEYS, "run.dt": dt}
    run = every_row(session(cylinder_project, fake), "section:run")
    assert (run["dt"]["placeholder"], run["dt"]["unset"]) == ("not set", "not set")


def test_keys_mallard_takes_one_of_are_groups_under_a_selector(cylinder_project: Path) -> None:
    s = session(cylinder_project)
    g = groups(s, "section:run")
    assert "dt" not in rows(s, "section:run", "run") and "cfl" not in rows(s, "section:run", "run")
    selector = g["run#oneof0"]["rows"][0]["oneof"]
    assert selector["labels"] == ["CFL number", "Fixed time step"] and selector["active"] == 0
    cfl, dt = g["run#oneof0.0"], g["run#oneof0.1"]
    assert not cfl["collapsed"] and [r["label"] for r in cfl["rows"]] == ["cfl"]
    assert dt["collapsed"] and dt["note"] == "not used"
    # Switching away from the one in use asks first: it would clear cfl
    assert not s.pick_alternative("run", 0, 1)
    assert "cfl" in s.doc.data["run"]
    # Setting a key of the unused one makes it the one in use, and offers to clear the other
    s.set_value("run.dt", "0.001")
    selector = groups(s, "section:run")["run#oneof0"]["rows"][0]["oneof"]
    assert selector["active"] == 1 and selector["conflict"] == ["run.cfl"]
    assert s.pick_alternative("run", 0, 1, clear=True)
    assert s.doc.data["run"]["dt"] == 0.001 and "cfl" not in s.doc.data["run"]
    assert not groups(s, "section:run")["run#oneof0"]["rows"][0]["oneof"]["conflict"]


def test_an_edit_sets_the_case_up_once_and_a_value_edit_reuses_the_preset_row(cylinder_project: Path) -> None:
    """Each setup reads the whole mesh: on a large case one takes minutes, so an edit must not start three."""
    preset_input(cylinder_project, '[numerics]\npreset = "les"\nriemann_solver = "RHLL"\n')
    m = FakeMallard()
    s = session(cylinder_project, m)
    s.refresh_presets()
    m.setups.clear()
    m.key_entries = {"numerics.riemann_solver": {"type": "string", "description": "from the check's setup"}}
    s.set_value("numerics.riemann_solver", "HLL")
    s.refresh_presets()
    s.check()
    assert m.setups == ["check"]  # no `list keys` of the input, no Preset row: the row's keys are the same
    assert s.info("numerics.riemann_solver").description == "from the check's setup"  # type: ignore[union-attr]
    assert s.presets["numerics.preset"].overrides == {"numerics.riemann_solver": "HLL"}
    s.set_value("numerics.time_integrator", "RK4")  # a key set anew: the row may change
    view = s.refresh_presets()["numerics.preset"]
    assert m.setups == ["check", "preset line"]
    assert view.overrides == {"numerics.riemann_solver": "HLL", "numerics.time_integrator": "RK4"}


# Values naming models from the literature, with the theory section of each (Mallard's catalog `models`)
RIEMANN_MODELS = {
    "numerics.riemann_solver": {
        "type": "string",
        "default": '"HLLC"',
        "values": ["HLL", "HLLC", "RHLL", "Roe", "Rusanov"],
        "models": {
            "HLLC": {"docs": "docs/theory/fluxes.md#hllc", "cite": "Toro, Spruce & Speares 1994"},
            "Roe": {"docs": "docs/theory/fluxes.md#roe"},
            "Rusanov": {"docs": "https://elsewhere.example/#rusanov"},
        },
    }
}


def test_a_choice_of_models_links_each_to_its_theory_section_on_the_website(cylinder_project: Path) -> None:
    fake = FakeMallard()
    fake.key_entries = RIEMANN_MODELS
    row = every_row(session(cylinder_project, fake), "section:numerics")["riemann_solver"]
    assert row["models"]["HLLC"]["url"] == "https://matthewbonanni.github.io/mallard/docs/theory/fluxes/#hllc"
    assert row["models"]["HLLC"]["tip"].startswith("HLLC: Toro, Spruce & Speares 1994\n")
    assert row["models"]["Roe"] == {
        "url": "https://matthewbonanni.github.io/mallard/docs/theory/fluxes/#roe",
        "tip": "Roe\nIts formulation in the theory manual (theory/fluxes), on the Mallard website",
        "cite": "",
    }
    assert "Rusanov" not in row["models"]  # only docs paths of the repository


def test_site_urls_follow_mkdocs_directory_urls() -> None:
    assert site_url("docs/theory/index.md#notation") == "https://matthewbonanni.github.io/mallard/docs/theory/#notation"
    assert site_url("docs/guide/les.md") == "https://matthewbonanni.github.io/mallard/docs/guide/les/"


def test_a_table_leads_with_its_name_and_type(cylinder_project: Path) -> None:
    """The keys were alphabetical: a boundary's type sat between p and u, face_reconstruction's under 15 others."""
    s = session(cylinder_project)
    boundary = [r["label"] for r in groups(s, "el:boundaries[1]")["boundaries[1]"]["rows"]]
    assert boundary[:2] == ["name", "type"]
    reconstruction = groups(s, "section:numerics")["numerics.face_reconstruction"]["rows"]
    assert reconstruction[0]["label"] == "type"
