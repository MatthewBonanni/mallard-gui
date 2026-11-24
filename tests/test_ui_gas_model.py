"""The gas model in a real browser: Mallard's models as one radio choice at the head of Physics, a switch asking
for the file the model requires; and a restart of another gas previewed, accepted and converted before launch."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from .test_ui import open_app, pw  # noqa: F401  (fixtures)
from .test_ui_restart_pick import open_page, select_run, stopped_run
from .ui.world import eventually, make_world, serve

pytestmark = pytest.mark.ui
pytest.importorskip("playwright.sync_api")


def toml(pg: Any) -> str:
    return str(pg.evaluate("() => window.trame.state.get('toml_text')"))


@pytest.fixture(scope="module")
def page(pw: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:  # noqa: F811
    project, env = make_world(tmp_path_factory.mktemp("gas-model"), runs=False)
    (project / "fpv.h5").write_bytes(b"table")
    with serve(project, {**env, "MALLARD_GUI_TEST_MODELS": "gas_models.json"}) as url:
        browser, pg = open_app(pw, "chromium", url)
        yield pg
        browser.close()


def test_physics_offers_mallards_gas_models_and_a_switch_asks_for_the_table(page: Any) -> None:
    pg = page
    pg.click(".rail button[aria-label='Case setup']")
    pg.locator(".case-tree .tree-row", has=pg.locator(".tlabel:text-is('Physics')")).first.click()
    group = pg.get_by_role("radiogroup", name="Gas model")
    group.wait_for()
    models = group.get_by_role("radio")
    assert [m.locator(".gm-title").inner_text() for m in models.all()] == [
        "Perfect gas",
        "Frozen mixture",
        "Finite-rate chemistry",
        "Flamelet table (FPV)",
    ]
    assert models.first.get_attribute("aria-checked") == "true"
    flamelet = models.nth(3)
    assert "physics.table" in flamelet.inner_text()  # what it asks for, before it is picked
    assert pg.locator("[aria-label='physics.gas']").count() == 0

    flamelet.click()
    picker = pg.locator(".path-picker")
    picker.wait_for()
    assert "choose physics.table" in picker.inner_text()
    picker.locator(".doc-entry", has_text="fpv.h5").click()
    eventually(lambda: toml(pg), lambda t: 'gas = "flamelet"' in t and 'table = "fpv.h5"' in t)
    assert "units" not in toml(pg).split("[physics]")[1].split("[")[0]
    eventually(lambda: flamelet.get_attribute("aria-checked"), lambda v: v == "true")


@pytest.fixture(scope="module")
def runs_world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    project, env = make_world(tmp_path_factory.mktemp("convert"))
    stopped_run(project, "gri30-1", "cccc3333", None)
    return project, {
        **env,
        "MALLARD_GUI_TEST_RESTART_CHECK": "mixture_check_restart_mechanism.json",
        "MALLARD_GUI_TEST_CONVERTED_CHECK": "mixture_check_restart_converted.json",
        "MALLARD_GUI_TEST_CONVERT": "restart_convert_dry.json",
    }


def test_a_restart_of_another_mechanism_is_converted_once_the_rule_is_accepted(
    pw: Any,  # noqa: F811
    runs_world: tuple[Path, dict[str, str]],
) -> None:
    project, env = runs_world
    with serve(project, env) as url:
        browser, pg = open_page(pw, url)
        try:
            select_run(pg, "gri30-1")
            pg.locator(".run-head button", has_text="Restart from").click()
            conversion = pg.locator(".restart-pick .conversion")
            conversion.wait_for()
            assert conversion.locator(".pill").inner_text() == "not exact"
            text = conversion.inner_text()
            assert "mixture · gri30.yaml" in text and "mixture · ch4_bfer.yaml" in text
            assert "OH (max Y 0.001187)" in text and "species dropped without mass\n46" in text
            assert "-2.96 %" in text  # Mallard's energy change, as a percentage
            assert "8 cells" in conversion.locator(".conversion-reasons").inner_text()
            launch = pg.locator(".restart-pick button", has_text="Launch from")
            convert = conversion.get_by_role("button", name="Convert")
            assert launch.is_disabled() and convert.is_disabled()

            conversion.get_by_role("checkbox").check()
            convert.click()
            pg.locator(".restart-pick .conversion", has_text="Converted: gri30-1-restart_000020.restart").wait_for()
            assert (project / "converted" / "gri30-1-restart_000020.restart").is_file()
            eventually(lambda: launch.is_enabled(), bool)
            rows = pg.locator(".restart-pick .restart-changes .events-row.change")
            assert "incompatible" not in " ".join(rows.all_inner_texts())
        finally:
            browser.close()
