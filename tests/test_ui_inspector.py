"""The inspector in a real browser: a section the input leaves out lists every key; editing an unset key writes it
and Reset removes it; Add on a section's array; one disclosure control everywhere; and nothing clipped at the
inspector's narrowest. Needs Google Chrome (Playwright's channel)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .test_choices import mixture
from .test_inspector import PARTICLE_FORCES, RIEMANN_MODELS, UNSET_KEYS
from .test_ui import PAGES, open_app, pw  # noqa: F401  (fixtures)
from .test_vectors import PARTICLE_KEYS
from .ui.world import eventually, make_world, serve

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")
SRC = Path(__file__).parent.parent / "src" / "mallard_gui"
INSPECTOR_MIN = 280  # px: the right pane's narrowest (style.css --inspector-min)


@pytest.fixture(scope="module")
def page(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    with serve(*make_world(tmp_path_factory.mktemp("inspector"))) as url:
        browser, pg = open_app(pw, "chromium", url)
        yield pg
        browser.close()


def select(pg: Any, label: str) -> None:
    pg.click(".rail button[aria-label='Case setup']")
    pg.locator(".case-tree .tree-row", has=pg.locator(f".tlabel:text-is('{label}')")).first.click()
    pg.wait_for_selector(f".prop-title .name:text-is('{label}')")


def toml(pg: Any) -> str:
    return str(pg.evaluate("() => window.trame.state.get('toml_text')"))


def turn_on(pg: Any) -> None:
    """The selected section's feature, off without its table: its keys show once it is on."""
    pg.locator(".feature-off button", has_text="Turn on").click()
    pg.wait_for_selector(".group-head .fswitch[aria-checked='true']")


def test_a_feature_turned_on_lists_every_key_and_edits_write_it(page: Any) -> None:
    pg = page
    select(pg, "LES")
    turn_on(pg)
    group = pg.locator(".pgroup", has=pg.locator(".gtitle:text-is('[les]')"))
    labels = group.locator(".plabel").all_inner_texts()
    assert {lbl.replace("​", "") for lbl in labels} >= {"C", "model", "filter_width", "dynamic"}
    assert group.locator(".prow.unset").count() == len(labels)
    field = group.locator("input[aria-label='les.C']")
    assert field.get_attribute("placeholder") == "0.07"
    field.fill("0.1")
    field.press("Enter")
    eventually(lambda: toml(pg), lambda t: "[les]\nC = 0.1" in t)
    row = pg.locator(".prow.set", has=pg.locator("input[aria-label='les.C']"))
    row.locator("button[aria-label='Reset les.C']").click()
    eventually(lambda: toml(pg), lambda t: "C = 0.1" not in t)
    pg.locator(".group-head .fswitch").first.click()
    eventually(lambda: toml(pg), lambda t: "[les]" not in t)


def test_hovering_a_key_shows_its_catalog_entry(page: Any) -> None:
    pg = page
    select(pg, "LES")
    turn_on(pg)
    pg.locator(".prow", has=pg.locator(".dd[aria-label='les.model']")).hover()
    help_ = pg.locator(".key-help")
    eventually(lambda: help_.inner_text(), lambda t: "les.model" in t and "smagorinsky" in t)


def test_add_on_a_section_writes_an_entry_and_selects_it(page: Any) -> None:
    pg = page
    select(pg, "Sponges")
    pg.locator(".group-head button[aria-label='Add sponges']").click()
    eventually(lambda: toml(pg), lambda t: "[[sponges]]" in t)
    pg.wait_for_selector(".prop-title .name:text-is('sponges[0]')")
    select(pg, "Sponges")
    pg.locator("button[title='Delete sponges[0]']").click()
    eventually(lambda: toml(pg), lambda t: "[[sponges]]" not in t)
    pg.locator(".prop-notice button", has_text="Undo").click()
    eventually(lambda: toml(pg), lambda t: "[[sponges]]" in t)


def test_an_added_trigger_is_deleted_from_its_own_page_and_undone(page: Any) -> None:
    pg = page
    select(pg, "Triggers")
    pg.locator(".group-head button[aria-label='Add triggers']").click()
    eventually(lambda: toml(pg), lambda t: "[[triggers]]" in t)
    pg.wait_for_selector(".prop-title .name:text-is('triggers[0]')")
    pg.locator(".group-head button[title='Delete triggers[0]']").click()
    eventually(lambda: toml(pg), lambda t: "[[triggers]]" not in t)
    pg.locator(".prop-notice button", has_text="Undo").click()
    eventually(lambda: toml(pg), lambda t: "[[triggers]]" in t)
    select(pg, "Triggers")
    pg.locator("button[title='Delete triggers[0]']").click()
    eventually(lambda: toml(pg), lambda t: "[[triggers]]" not in t)


def test_applying_a_preset_resolves_it_again_without_reselecting(page: Any) -> None:
    pg = page
    select(pg, "Numerics")
    status = pg.locator(".preset-status")
    eventually(lambda: status.inner_text(), lambda t: "resolving" not in t)
    assert pg.locator(".preset-bar .preset-opt, .preset-bar .preset-options").count() == 0  # no list of presets
    pg.locator(".preset-row .dd").click()
    items = pg.locator(".dd-pop .dd-item")
    assert items.count() == 5 and "SSPRK54" in items.filter(has_text="les").locator(".dd-note").inner_text()
    pg.keyboard.type("les")
    pg.keyboard.press("Enter")
    pg.locator(".preset-pending .btn.primary").click()
    eventually(lambda: toml(pg), lambda t: 'preset = "les"' in t)
    eventually(lambda: status.inner_text(), lambda t: t.startswith("preset les"))


def test_switching_presets_names_what_each_choice_does_to_the_users_changes(page: Any) -> None:
    pg = page
    select(pg, "Numerics")
    status = pg.locator(".preset-status")
    eventually(lambda: status.inner_text(), lambda t: "resolving" not in t)
    pg.locator(".dd[aria-label='numerics.riemann_solver']").click()
    pg.keyboard.type("HLL")
    pg.locator(".dd-pop .dd-item", has_text=re.compile(r"^HLL$")).click()
    eventually(lambda: toml(pg), lambda t: 'riemann_solver = "HLL"' in t)
    pg.locator(".preset-row .dd").click()
    pg.keyboard.type("second_order")
    pg.keyboard.press("Enter")
    pending = pg.locator(".preset-pending")
    pending.locator(".actions .btn.primary").wait_for()
    mine = pending.locator(".preset-diff.mine .preset-chg").all_inner_texts()
    assert any(m.startswith("numerics.riemann_solver") and "HLL →" in m for m in mine)
    n = len(mine)  # the case also sets the time integrator and reconstruction itself
    assert pending.locator(".actions .btn.primary").inner_text() == f"Use second_order, keep my {n} changes"
    assert pending.locator(".actions .btn:not(.primary)", has_text="replace").inner_text() == (
        f"Use second_order, replace my {n} changes"
    )
    pending.locator(".actions .btn.primary").click()
    eventually(lambda: toml(pg), lambda t: 'preset = "second_order"' in t and 'riemann_solver = "HLL"' in t)
    pg.locator(".prop-notice button", has_text="Undo").click()
    eventually(lambda: toml(pg), lambda t: 'preset = "second_order"' not in t)


def test_a_dropdown_filters_as_soon_as_it_opens_and_enter_picks(page: Any) -> None:
    """Click, then type at once (the search field has the focus): the list filters; arrows move, Enter picks."""
    pg = page
    select(pg, "LES")
    if pg.locator(".feature-off").count():
        turn_on(pg)
    dd = pg.locator(".dd[aria-label='les.model']")
    dd.click()
    pop = pg.locator(".dd-pop")
    assert pop.locator(".dd-item").all_inner_texts() == ["sigma", "smagorinsky", "vreman", "wale"]
    pg.keyboard.type("ma")
    assert pop.locator(".dd-item").all_inner_texts() == ["sigma", "smagorinsky", "vreman"]
    pg.keyboard.type("g")
    assert pop.locator(".dd-item").all_inner_texts() == ["smagorinsky"]
    pg.keyboard.press("Enter")
    eventually(lambda: toml(pg), lambda t: 'model = "smagorinsky"' in t)
    assert pop.count() == 0
    eventually(lambda: dd.locator(".dd-text").inner_text(), lambda t: t == "smagorinsky")  # the value, not dimmed
    assert "dim" not in (dd.locator(".dd-text").get_attribute("class") or "")
    dd.click()
    pg.keyboard.press("Escape")
    assert pop.count() == 0 and pg.evaluate("() => document.activeElement.getAttribute('aria-label')") == "les.model"
    pg.keyboard.press("ArrowDown")  # the focused dropdown opens on a key, the value marked
    assert pop.locator(".dd-item.on").inner_text().endswith("smagorinsky")
    pg.keyboard.press("ArrowDown")
    pg.keyboard.press("Enter")
    eventually(lambda: toml(pg), lambda t: 'model = "vreman"' in t)
    pg.locator("button[aria-label='Reset les.model']").click()
    eventually(lambda: toml(pg), lambda t: "model =" not in t.split("[les]")[-1].split("\n[")[0])


def test_no_choice_in_the_inspector_is_a_plain_select_or_a_typed_boolean(page: Any) -> None:
    """Every section, every group open: values chosen from a set use the one dropdown; booleans are checkboxes."""
    pg = page
    pg.click(".rail button[aria-label='Case setup']")
    sections = pg.locator(".case-tree .tree-row:not(.child) .tlabel").all_inner_texts()
    for label in sections:
        pg.locator(".case-tree .tree-row:not(.child)", has=pg.locator(f".tlabel:text-is('{label}')")).first.click()
        pg.wait_for_timeout(120)
        pg.evaluate(OPEN_ALL)
        pg.wait_for_timeout(120)
        assert pg.locator(".panel.right select:visible").count() == 0, label
        bools = pg.evaluate(BOOL_FIELDS)
        assert bools == [], (label, bools)


BOOL_FIELDS = """() => [...document.querySelectorAll('.panel.right .prow')]
  .filter(r => r.offsetParent !== null && (r.querySelector('.plabel') || {}).title?.endsWith('(bool)'))
  .filter(r => !r.querySelector('input[type=checkbox]'))
  .map(r => r.querySelector('.plabel').title)"""


def test_a_boolean_is_a_checkbox_its_default_dimmed_until_set(page: Any) -> None:
    pg = page
    select(pg, "Numerics")
    pg.evaluate(OPEN_ALL)
    box = pg.locator("input[type=checkbox][aria-label='numerics.face_reconstruction.bound_preserving']")
    label = pg.locator(".pbool", has=box)
    assert "dim" in (label.get_attribute("class") or "") and not box.is_checked()
    assert "(default)" in label.inner_text()
    box.check()
    eventually(lambda: toml(pg), lambda t: "bound_preserving = true" in t)
    eventually(lambda: label.get_attribute("class") or "", lambda c: "dim" not in c)
    pg.locator("button[aria-label='Reset numerics.face_reconstruction.bound_preserving']").click()
    eventually(lambda: toml(pg), lambda t: "bound_preserving" not in t)


# One disclosure control everywhere

DISCLOSURES = """() => [...document.querySelectorAll('[aria-expanded], details, summary')]
  .filter(e => e.offsetParent !== null)
  .map(e => {
    const chev = e.querySelector(':scope > .chev');
    const svg = chev && chev.querySelector('svg');
    const box = svg && svg.getBoundingClientRect();
    return {tag: e.tagName, cls: e.className, disc: e.classList.contains('disc'),
            tree: e.classList.contains('tree-row'), chev: !!chev,
            w: box ? Math.round(box.width) : 0, h: box ? Math.round(box.height) : 0,
            gutter: chev ? Math.round(chev.getBoundingClientRect().width) : 0};
  })"""
GLYPHS = re.compile("[▶▸▹►▼▾▿◀◂]")


@pytest.mark.parametrize("title", PAGES)
def test_every_disclosure_is_the_shared_control(page: Any, title: str) -> None:
    pg = page
    pg.click(f".rail button[aria-label='{title}']")
    pg.wait_for_timeout(500)
    if title == "Case setup":
        select(pg, "Numerics")  # nested groups open and closed
        pg.get_by_role("tab", name="Check report").click()
        pg.wait_for_timeout(300)
        pg.get_by_role("tab", name="Case view").click()
    found = pg.evaluate(DISCLOSURES)
    for d in found:
        assert d["tag"] not in ("DETAILS", "SUMMARY"), d
        if d["tree"] and not d["chev"]:
            continue  # a tree item without children
        assert d["disc"] or d["tree"], f"not the shared disclosure: {d}"
        assert (d["w"], d["h"], d["gutter"]) == (16, 16, 16), d
    text = pg.evaluate("() => document.body.innerText")
    assert not GLYPHS.search(text), GLYPHS.search(text)


def test_no_template_builds_its_own_disclosure() -> None:
    """New disclosures go through ui/disclosure.py: no <details>, no triangle glyphs, no aria-expanded elsewhere."""
    for path in (SRC / "ui").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "html.Details" not in text and "html.Summary" not in text, path
        assert not GLYPHS.search(text), path
        if path.name not in ("disclosure.py", "app.py"):
            assert "aria_expanded" not in text, path


# Nothing clipped at the inspector's narrowest


# Labels, units, tags and headings are never clipped, tooltip or not; long values may be, with their full text in a
# tooltip.
CLIPPED = """() => { const NEVER = '.plabel, .unit, .ptag, .gtitle, .note, .qunit, .kh-path';
  return [...document.querySelectorAll('.panel.right *')]
  .filter(e => e.offsetParent !== null && e.children.length === 0 || ['INPUT', 'SELECT'].includes(e.tagName))
  .filter(e => e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflowX !== 'auto')
  .filter(e => e.matches(NEVER) || (!e.title && !(e.closest('[title]') && e.closest('[title]') !== document.body)))
  .map(e => e.className + ': ' + (e.value || e.innerText).slice(0, 40)) }"""


OPEN_ALL = "() => document.querySelectorAll('.pgroup .group-head[aria-expanded=false]').forEach(h => h.click())"


def test_no_section_clips_text_at_the_narrowest_inspector(page: Any) -> None:
    pg = page
    pg.click(".rail button[aria-label='Case setup']")
    columns = f"48px 275px minmax(0, 1fr) {INSPECTOR_MIN}px"
    pg.evaluate(f"() => document.querySelector('.body').style.gridTemplateColumns = '{columns}'")
    try:
        sections = pg.locator(".case-tree .tree-row:not(.child) .tlabel").all_inner_texts()
        assert len(sections) > 15
        for label in sections:
            pg.locator(".case-tree .tree-row:not(.child)", has=pg.locator(f".tlabel:text-is('{label}')")).first.click()
            pg.wait_for_timeout(150)
            pg.evaluate(OPEN_ALL)
            pg.wait_for_timeout(150)
            assert pg.evaluate(CLIPPED) == [], label
            width = pg.evaluate("() => document.querySelector('.panel.right').getBoundingClientRect().width")
            assert abs(width - INSPECTOR_MIN) < 2
    finally:
        pg.evaluate("() => document.querySelector('.body').style.gridTemplateColumns = ''")


# Files, names and compositions on a mixture


@pytest.fixture(scope="module")
def mix(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("mixture"), runs=False)
    mixture(project, "X = { H2 = 2.0, O2 = 1.0, AR = 7.0 }")
    text = (
        (project / "input.toml")
        .read_text()
        .replace(
            'name = "farfield"\ntype = "farfield"\n',
            'name = "farfield"\ntype = "farfield"\nX = { CH4 = 1.0, N2 = 3.76 }\n',
        )
    )
    (project / "input.toml").write_text(text)
    with serve(project, env) as url:
        browser, pg = open_app(pw, "chromium", url)
        yield pg
        browser.close()


def initialize(pg: Any) -> str:
    return toml(pg).split("[initialize]")[1].split("\n[")[0]


def comp_row(pg: Any) -> Any:
    return pg.locator(".prow.comp").first


def test_one_composition_editor_adds_picks_normalizes_and_flags(mix: Any) -> None:
    pg = mix
    select(pg, "Initial state")
    group = pg.locator(".pgroup", has=pg.locator(".gtitle:text-is('[initialize]')"))
    labels = {lbl.replace("​", "") for lbl in group.locator(".plabel").all_inner_texts()}
    assert "composition" in labels and not {"X", "Y"} & labels
    row = comp_row(pg)
    species = row.locator(".comp-entry .comp-species")
    assert species.all_inner_texts() == ["H2", "O2", "AR"]
    assert row.locator(".comp-sum").inner_text() == "sum 10"
    row.locator(".comp-add").click()  # Add species: search the mechanism's species at once
    pg.keyboard.type("n2")
    assert pg.locator(".dd-pop .dd-item").all_inner_texts() == ["N2"]
    pg.keyboard.press("Enter")
    eventually(lambda: toml(pg), lambda t: "N2 = 0.0" in t)
    field = pg.locator("input[aria-label='initialize.X.N2']")
    field.fill("10")
    field.press("Enter")
    eventually(lambda: toml(pg), lambda t: "N2 = 10.0" in t)
    pg.locator(".dd[aria-label='initialize.X species AR']").click()
    pg.keyboard.type("H2O")
    pg.keyboard.press("Enter")  # H2O ranks first: its label starts with the text
    eventually(lambda: toml(pg), lambda t: "H2O = 7.0" in t and "AR =" not in t)
    row.locator(".comp-norm").click()
    eventually(lambda: comp_row(pg).locator(".comp-sum").inner_text(), lambda t: t == "sum 1")
    assert "N2 = 0.5" in toml(pg)
    assert comp_row(pg).locator(".comp-norm").is_disabled()
    pg.click(".rail button[aria-label='Case setup']")
    pg.locator(".case-tree .tree-row", has=pg.locator(".tlabel:text-is('farfield')")).first.click()
    bad = comp_row(pg).locator(".comp-entry.bad")
    eventually(lambda: bad.count(), lambda n: n == 1)
    assert bad.locator(".comp-species").inner_text() == "CH4"
    assert "no species CH4" in comp_row(pg).locator(".perr").inner_text()


def test_switching_to_mass_fractions_without_molar_masses_asks_to_clear(mix: Any) -> None:
    pg = mix
    select(pg, "Initial state")
    row = comp_row(pg)
    row.locator(".comp-basis .seg", has_text="Mass (Y)").click()
    ask = row.locator(".comp-ask")
    ask.wait_for()
    assert "molar masses" in ask.inner_text() and "X = {" in initialize(pg)
    ask.locator("button", has_text="Cancel").click()
    row.locator(".comp-basis .seg", has_text="Mass (Y)").click()  # at once: asked again, whatever the timing
    ask.wait_for()
    assert "X = {" in initialize(pg)
    row.locator(".comp-ask button", has_text="Clear and switch").click()
    eventually(lambda: initialize(pg), lambda t: "X = {" not in t)
    eventually(lambda: comp_row(pg).locator(".comp-basis .seg.on").inner_text(), lambda t: t.strip() == "Mass (Y)")
    comp_row(pg).locator(".comp-add").click()
    pg.keyboard.type("O2")
    pg.keyboard.press("Enter")
    eventually(lambda: toml(pg), lambda t: "Y = {O2 = 0.0}" in t)


def test_a_key_naming_species_or_zones_is_a_dropdown_of_them(mix: Any) -> None:
    pg = mix
    select(pg, "Initial state")
    pg.locator(".dd[aria-label='initialize.balance']").click()
    items = pg.locator(".dd-pop .dd-item").all_inner_texts()
    assert items[:3] == ["H2", "H", "O"] and "N2" in items
    pg.keyboard.press("Escape")
    pg.click(".rail button[aria-label='Case setup']")
    pg.locator(".case-tree .tree-row", has=pg.locator(".tlabel:text-is('cylinder')")).last.click()
    pg.locator(".dd[aria-label='forces[0].zone']").click()
    assert {"cylinder", "farfield"} <= set(pg.locator(".dd-pop .dd-item").all_inner_texts())
    pg.keyboard.type("newzone")  # a zone the check may not know yet can be typed
    assert pg.locator(".dd-pop .dd-item.free").inner_text().startswith("newzone")
    pg.keyboard.press("Escape")


def test_a_file_key_opens_the_file_picker_and_stores_a_relative_path(mix: Any) -> None:
    pg = mix
    select(pg, "Numerics")
    key = "numerics.face_reconstruction.cache_file"
    pg.locator(f"button[aria-label='Choose a file for {key}']").click()
    picker = pg.locator(".path-picker")
    picker.wait_for()
    assert f"Choose {key}" in picker.inner_text()
    picker.locator(".doc-entry", has_text="cylinder.msh").click()
    eventually(lambda: toml(pg), lambda t: 'cache_file = "cylinder.msh"' in t)
    pg.locator(f"button[aria-label='Reset {key}']").click()
    eventually(lambda: toml(pg), lambda t: 'cache_file = "cylinder.msh"' not in t)


def test_alternatives_are_groups_under_a_selector_that_asks_before_clearing(page: Any) -> None:
    pg = page
    pg.evaluate("() => window.trame.state.set('prop_toggled', [])")  # groups as they start (OPEN_ALL opened them)
    select(pg, "Run")
    head = pg.locator(".pgroup", has=pg.locator(".gtitle:text-is('CFL number or Fixed time step')"))
    seg = head.locator(".oneof .seg")
    assert seg.all_inner_texts() == ["CFL number", "Fixed time step"]
    assert head.locator(".oneof .seg.on").inner_text() == "CFL number"
    unused = pg.locator(".pgroup", has=pg.locator(".gtitle:text-is('Fixed time step')"))
    assert unused.locator(".note").inner_text() == "not used"
    assert unused.locator(".group-head").get_attribute("aria-expanded") == "false"
    assert unused.locator("input[aria-label='run.dt']").count() == 0  # collapsed
    assert pg.locator("input[aria-label='run.cfl']").is_visible()
    seg.nth(1).click()
    ask = head.locator(".oneof-ask")
    ask.wait_for()
    assert "clear cfl" in ask.inner_text()
    ask.locator("button", has_text="Clear and switch").click()
    eventually(lambda: toml(pg), lambda t: "\ncfl =" not in t)
    eventually(lambda: head.locator(".oneof .seg.on").inner_text(), lambda t: t == "Fixed time step")
    dt = pg.locator("input[aria-label='run.dt']")
    dt.fill("0.001")
    dt.press("Enter")
    eventually(lambda: toml(pg), lambda t: "dt = 0.001" in t)


# What leaving a key out means, from a catalog that states it (Mallard #1400)


@pytest.fixture(scope="module")
def unset_page(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("unset"), runs=False)
    with serve(project, {**env, "MALLARD_GUI_TEST_KEYS": json.dumps(UNSET_KEYS)}) as url:
        browser, pg = open_app(pw, "chromium", url)
        yield pg
        browser.close()


def test_an_unset_key_shows_what_leaving_it_out_means(unset_page: Any) -> None:
    pg = unset_page
    select(pg, "Run")
    wall = pg.locator("input[aria-label='run.t_wall_stop']")
    assert wall.get_attribute("placeholder") == "not set: no wall-clock limit"
    assert pg.locator("input[aria-label='run.n_steps']").get_attribute("placeholder") == "not set: no step limit"
    assert pg.locator("input[aria-label='run.step_queue']").get_attribute("placeholder") == "3"
    pg.locator(".prow", has=wall).hover()
    eventually(lambda: pg.locator(".key-help").inner_text(), lambda t: "default none: no wall-clock limit" in t)


NEAR_WAKE_FIELDS = 'fields = ["RHO", "U", "P", "VORTICITY", "CP", "Q_CRITERION"]'


def open_fields(pg: Any) -> Any:
    """The near wake's write_data row, its field list's chips and Choose."""
    pg.click(".rail button[aria-label='Case setup']")
    pg.locator(".case-tree .tree-row", has_text="write_data").nth(2).click()
    pg.wait_for_selector(".prow .fchip")
    return pg.locator(".prow", has=pg.locator("button[aria-label='Choose write_data[2].fields']"))


def test_a_field_list_is_picked_from_the_cases_fields_and_written_as_its_array(page: Any) -> None:
    pg = page
    row = open_fields(pg)
    assert row.locator(".fchip-name").all_inner_texts() == ["RHO", "U", "P", "VORTICITY", "CP", "Q_CRITERION"]
    row.locator("button[aria-label='Choose write_data[2].fields']").click()
    dialog = pg.locator(".fpick-modal")
    head = dialog.locator(".fpick-ghead", has_text="conservative")
    assert head.locator(".fpick-gcount").inner_text() == "1 of 4"
    assert head.locator("input").evaluate("i => i.indeterminate")
    assert dialog.locator(".fpick-row", has_text="RHO").first.locator(".fpick-unit").inner_text() == "kg m⁻³"
    pg.fill(".fpick-search", "vort")
    names = dialog.locator(".fpick-row .fpick-name")
    eventually(names.all_inner_texts, lambda n: n == ["LAMBDA2", "VORTICITY"])  # descriptions match too
    pg.fill(".fpick-search", "")
    head.locator("input").check()
    dialog.locator("input[aria-label='U_X']").uncheck()
    expect = '["RHO", "U_Y", "P", "VORTICITY", "CP", "Q_CRITERION", "RHOU_X", "RHOU_Y", "RHOE"]'
    eventually(lambda: dialog.locator(".fpick-toml").inner_text(), lambda t: t == f"fields = {expect}")
    assert NEAR_WAKE_FIELDS in toml(pg)  # nothing written before Apply
    dialog.locator("button:text-is('Apply')").click()
    eventually(lambda: toml(pg), lambda t: f"fields = {expect}" in t)


@pytest.fixture(scope="module")
def bad_fields(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("fields"), runs=False)
    text = (project / "input.toml").read_text()
    (project / "input.toml").write_text(text.replace(NEAR_WAKE_FIELDS, 'fields = ["RHO", "MU_T", "FOO"]'))
    with serve(project, env) as url:
        browser, pg = open_app(pw, "chromium", url)
        yield pg
        browser.close()


def test_a_field_the_case_does_not_write_is_flagged_with_why(bad_fields: Any) -> None:
    pg = bad_fields
    row = open_fields(pg)
    bad = row.locator(".fchip.bad")
    assert bad.all_inner_texts() == ["MU_T", "FOO"]
    assert bad.first.get_attribute("title") == "MU_T needs [les]"
    row.locator("button[aria-label='Choose write_data[2].fields']").click()
    dialog = pg.locator(".fpick-modal")
    dialog.locator(".fpick-problem").first.wait_for()
    assert dialog.locator(".fpick-problem .fpick-why").all_inner_texts() == [
        "MU_T needs [les]",
        "FOO is not a field Mallard writes",
    ]
    dialog.locator("button[aria-label='Remove FOO']").click()
    dialog.locator("button:text-is('Apply')").click()
    eventually(lambda: toml(pg), lambda t: 'fields = ["RHO", "MU_T"]' in t)


def test_a_vector_is_a_box_per_component_and_a_bad_one_writes_nothing(page: Any) -> None:
    pg = page
    pg.click(".rail button[aria-label='Case setup']")
    pg.locator(".case-tree .tree-row", has=pg.locator(".tlabel:text-is('wake_point')")).first.click()
    pg.wait_for_selector(".prop-title .name:text-is('wake_point')")
    y = pg.locator("input[aria-label='probes[0].point y']")
    assert pg.locator("input[aria-label='probes[0].point x']").input_value() == "2.5" and y.input_value() == "0.0"
    y.fill("x + 1")  # a point takes numbers only
    y.press("Enter")
    pg.wait_for_selector(".vcomp.bad")
    assert "point = [2.5, 0.0]" in toml(pg) and y.input_value() == "x + 1"  # kept to correct
    y.fill("0.25")
    y.press("Enter")
    eventually(lambda: toml(pg), lambda t: "point = [2.5, 0.25]" in t)
    assert pg.locator(".vcomp.bad").count() == 0
    y.fill("0.0")
    y.press("Enter")
    eventually(lambda: toml(pg), lambda t: "point = [2.5, 0.0]" in t)


def test_an_empty_list_offers_add_and_no_switch(page: Any) -> None:
    pg = page
    select(pg, "Probes")
    for _ in range(3):
        n = toml(pg).count("[[probes]]")
        pg.locator("button[title='Delete probes[0]']").click()
        eventually(lambda: toml(pg), lambda t, n=n: t.count("[[probes]]") == n - 1)
    empty = pg.locator(".list-empty")
    empty.wait_for()
    assert "None yet" in empty.inner_text()
    assert pg.locator(".pgroup .fswitch").count() == 0
    empty.get_by_role("button", name="Add the first of probes").click()
    pg.locator(".add-menu .add-item").first.click()  # the kinds of probe
    eventually(lambda: toml(pg), lambda t: "[[probes]]" in t)


@pytest.fixture(scope="module")
def particles_page(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("particles"), runs=False)
    (project / "input.toml").write_text((project / "input.toml").read_text() + '\n[particles]\ntype = "solid"\n')
    keys = {**PARTICLE_KEYS, **PARTICLE_FORCES}
    with serve(project, {**env, "MALLARD_GUI_TEST_KEYS": json.dumps(keys)}) as url:
        browser, pg = open_app(pw, "chromium", url)
        yield pg
        browser.close()


def test_a_list_of_points_adds_a_row_per_point_and_removes_one(particles_page: Any) -> None:
    pg = particles_page
    select(pg, "Particles")
    box = "input[aria-label='particles.points {} {}']"
    pg.fill(box.format(1, "x"), "0.1")
    pg.fill(box.format(1, "y"), "0.2")
    pg.press(box.format(1, "y"), "Enter")
    eventually(lambda: toml(pg), lambda t: "points = [[0.1, 0.2]]" in t)
    pg.fill(box.format(2, "x"), "0.3")  # the blank row after the last point adds one
    pg.fill(box.format(2, "y"), "oops")
    pg.press(box.format(2, "y"), "Enter")
    pg.wait_for_selector(".kpts .vcomp.bad")
    assert "points = [[0.1, 0.2]]" in toml(pg)  # nothing written while a box is wrong
    pg.fill(box.format(2, "y"), "0.4")
    pg.press(box.format(2, "y"), "Enter")
    eventually(lambda: toml(pg), lambda t: "points = [[0.1, 0.2], [0.3, 0.4]]" in t)
    pg.click("button[aria-label='remove point 1']")
    eventually(lambda: toml(pg), lambda t: "points = [[0.3, 0.4]]" in t)


def test_a_set_of_choices_is_a_checkbox_per_value_with_its_cost_inline(particles_page: Any) -> None:
    pg = particles_page
    select(pg, "Particles")
    group = pg.locator(".checks[aria-label='particles.forces']")
    assert group.locator(".check").count() == 4
    history = group.locator(".check", has=pg.locator(".check-name:text-is('basset_history')"))
    assert history.locator(".cost").inner_text() == "expensive"
    assert history.locator(".check-why").inner_text() == "29 vectors per particle"
    assert history.locator(".check-summary").inner_text() == "History force"
    assert group.locator(".check", has_text="added_mass").locator(".cost").count() == 0  # not rated
    summary = pg.locator(".prow", has=pg.locator(".plabel", has_text="forces")).locator(".checks-summary")
    assert summary.inner_text() == "none (default)"
    pg.check("input[aria-label='particles.forces pressure_gradient']")
    eventually(lambda: toml(pg), lambda t: 'forces = ["pressure_gradient"]' in t)
    pg.check("input[aria-label='particles.forces basset_history']")
    eventually(lambda: toml(pg), lambda t: 'forces = ["pressure_gradient", "basset_history"]' in t)
    eventually(lambda: summary.inner_text(), lambda v: v == "basset_history, pressure_gradient")
    pg.uncheck("input[aria-label='particles.forces pressure_gradient']")
    pg.uncheck("input[aria-label='particles.forces basset_history']")
    eventually(lambda: toml(pg), lambda t: "\nforces = " not in t)
    pg.locator(".dd[aria-label='particles.type']").click()  # a single choice gives its cost in its dropdown
    assert pg.locator(".dd-pop .dd-item", has_text="solid").locator(".dd-note").inner_text() == "moderate"
    pg.keyboard.press("Escape")


@pytest.fixture(scope="module")
def si_page(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("si"), runs=False)
    path = project / "input.toml"
    path.write_text(path.read_text().replace('units = "nondimensional"\n', ""))
    with serve(project, env) as url:
        browser, pg = open_app(pw, "chromium", url)
        yield pg
        browser.close()


def pick(pg: Any, label: str, option: str) -> None:
    pg.locator(f".dd[aria-label='{label}']").click()
    pg.keyboard.type(option)
    pg.keyboard.press("Enter")


def test_a_unit_typed_or_picked_shows_while_the_input_holds_si(si_page: Any) -> None:
    pg = si_page
    select(pg, "Run")
    t_stop = pg.locator("input[aria-label='run.t_stop']")
    t_stop.fill("2 min")
    t_stop.press("Enter")
    eventually(lambda: toml(pg), lambda t: "t_stop = 120\n" in t)
    unit = pg.locator(".dd[aria-label='run.t_stop unit']")
    eventually(lambda: (t_stop.input_value(), unit.inner_text().strip()), lambda v: v == ("2", "min"))
    pick(pg, "run.t_stop unit", "ms")
    eventually(lambda: t_stop.input_value(), lambda v: v == "120000")
    assert "t_stop = 120\n" in toml(pg)  # the selector converts what shows, not the input
    pg.locator(".case-tree .tree-row", has=pg.locator(".tlabel:text-is('farfield')")).first.click()
    u = 'boundaries[name = "farfield"].u'
    pg.locator(f"input[aria-label='{u} u']").wait_for()
    assert pg.locator(f".dd[aria-label='{u} unit']").count() == 1  # one for every component
    pick(pg, f"{u} unit", "km/h")
    first = pg.locator(f"input[aria-label='{u} u']")
    eventually(lambda: first.input_value(), lambda v: v == "0.72")
    first.fill("72")
    first.press("Enter")
    eventually(lambda: toml(pg), lambda t: "u = [20, 0]" in t)


@pytest.fixture(scope="module")
def models_page(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("models"), runs=False)
    with serve(project, {**env, "MALLARD_GUI_TEST_KEYS": json.dumps(RIEMANN_MODELS)}) as url:
        browser, pg = open_app(pw, "chromium", url)
        yield pg
        browser.close()


def test_a_model_from_the_literature_has_an_info_button_opening_its_theory(models_page: Any) -> None:
    pg = models_page
    select(pg, "Numerics")
    pg.evaluate("() => { window.__opened = []; window.open = (u) => { window.__opened.push(u); return null; }; }")
    row = pg.locator(".prow", has=pg.locator(".dd[aria-label='numerics.riemann_solver']"))
    info = row.locator("button.pinfo")
    assert info.get_attribute("aria-label") == "Theory of numerics.riemann_solver = HLLC"
    assert (info.get_attribute("title") or "").startswith("HLLC: Toro, Spruce & Speares 1994")
    info.click()
    eventually(
        lambda: pg.evaluate("() => window.__opened"),
        lambda o: o == ["https://matthewbonanni.github.io/mallard/docs/theory/fluxes/#hllc"],
    )
    row.locator(".dd").click()
    item = pg.locator(".dd-pop .dd-item", has_text="HLLC")
    assert item.locator(".dd-note").inner_text() == "Toro, Spruce & Speares 1994"
    pg.locator(".dd-pop .dd-item", has_text="HLL").first.click()  # a solver the catalog gives no section
    eventually(lambda: toml(pg), lambda t: 'riemann_solver = "HLL"' in t)
    eventually(lambda: row.locator("button.pinfo").count(), lambda n: n == 0)
