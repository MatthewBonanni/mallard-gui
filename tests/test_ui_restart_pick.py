"""Restart from: any restart of the run, and Mallard's answer for each key the case changed since it was written,
before launch; launch stays off while a change is incompatible. The Outputs tab shows the run's lineage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mallard_gui.project import Project, RunRecord

from .test_ui import pw  # noqa: F401  (fixture)
from .ui.world import make_world, serve

pytestmark = pytest.mark.ui
pytest.importorskip("playwright.sync_api")


def stopped_run(project: Path, name: str, run_id: str, restart_of: dict[str, Any] | None) -> None:
    """A run that wrote restarts at steps 10 and 20 and stopped."""
    run_dir = project / "runs" / name
    (run_dir / "solut").mkdir(parents=True)
    records: list[dict[str, Any]] = [
        {"type": "start", "schema": "mallard-run-log", "version": 2, "run_id": run_id, "restart_of": restart_of}
    ]
    for step, t in ((10, 0.175), (20, 0.35)):
        path = f"./solut/restart_{step:06d}.restart"
        data = f"state {step}\n".encode()
        (run_dir / path).write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        records.append(
            {
                "type": "output",
                "step": step,
                "t": t,
                "kind": "restart",
                "path": path,
                "bytes": len(data),
                "files": [{"path": path, "bytes": len(data), "sha256": sha}],
            }
        )
    records.append({"type": "end", "outcome": "stopped", "step": 20, "last_restart": {"step": 20, "path": path}})
    (run_dir / "mallard.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    Project.open(project).save_run(
        RunRecord(
            name,
            run_id=run_id,
            home={"remote": "local", "directory": f"runs/{name}"},
            outcome="stopped",
            last_restart={"step": 20, "path": path, "sha256": sha},
        )
    )


def open_page(pw: Any, url: str) -> tuple[Any, Any]:  # noqa: F811
    browser = pw.chromium.launch(channel="chrome")
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.goto(url)
    pg.wait_for_selector(".tree-row", timeout=30_000)
    pg.click(".rail button[aria-label='Runs']")
    return browser, pg


def select_run(pg: Any, name: str) -> None:
    pg.locator(".panel:has(> .panel-head > span:text-is('Runs')) .run-row", has_text=name).click()
    pg.wait_for_selector(f".run-head .name:text-is('{name}')")


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    project, env = make_world(tmp_path_factory.mktemp("restart-pick"))
    stopped_run(project, "stop-1", "aaaa1111", None)
    of = {"run_id": "aaaa1111", "file": str(project / "runs/stop-1/solut/restart_000010.restart"), "step": 10}
    stopped_run(project, "stop-2", "bbbb2222", of)
    return project, env


@pytest.fixture(scope="module")
def page(pw: Any, world: tuple[Path, dict[str, str]]) -> Iterator[Any]:  # noqa: F811
    project, env = world
    with serve(project, {**env, "MALLARD_GUI_TEST_RESTART_CHECK": "cylinder_check_restart.json"}) as url:
        browser, pg = open_page(pw, url)
        yield pg
        browser.close()


def test_restart_from_lists_every_restart_and_mallards_answer_for_each_change(page: Any) -> None:
    pg = page
    select_run(pg, "stop-1")
    pg.locator(".run-head button", has_text="Restart from").click()
    pick = pg.locator(".restart-pick")
    options = pick.locator("[role=option]")
    options.first.wait_for()
    assert [o.inner_text().split("\n")[0] for o in options.all()] == ["20", "10"]
    assert options.first.get_attribute("aria-selected") == "true"
    rows = pick.locator(".restart-changes .events-row.change")
    rows.first.wait_for()
    shown = {r.locator("code").first.inner_text(): r.locator(".pill").inner_text() for r in rows.all()}
    assert shown == {
        "mesh.filename": "incompatible",
        "run.t_stop": "accepted",
        "statistics.phase.bins": "resets",
        "statistics.reset": "resets",
    }
    mesh = rows.filter(has_text="mesh.filename")
    assert '"cylinder.msh"' in mesh.inner_text() and '"cylinder2.msh"' in mesh.inner_text()
    assert "remap" in mesh.inner_text()  # Mallard's reason, whole
    assert "mesh.filename" in pick.locator(".notice", has_text="Launch is off").inner_text()
    launch = pick.locator("button", has_text="Launch from")
    assert launch.is_disabled()
    assert "mesh.filename" in (launch.get_attribute("title") or "")

    options.nth(1).click()
    pg.wait_for_function(
        "() => document.querySelector('.restart-pick .btn.primary').textContent.includes('restart_000010')"
    )


def test_outputs_offer_each_restart_and_show_the_lineage(page: Any) -> None:
    pg = page
    pg.locator(".restart-pick button", has_text="Cancel").click()
    select_run(pg, "stop-2")
    pg.get_by_role("button", name="Outputs", exact=True).click()
    lineage = pg.locator(".lineage")
    lineage.wait_for()
    assert "restart_000010.restart · step 10" in lineage.inner_text()
    assert "of stop-1" in lineage.inner_text()
    row = pg.locator(".events-row", has_text="restart_000010.restart").filter(has=pg.locator("button"))
    assert "0.175" in row.inner_text()
    row.get_by_role("button", name="Restart from ./solut/restart_000010.restart").click()
    selected = pg.locator(".restart-pick [role=option][aria-selected=true]")
    selected.wait_for()
    assert "restart_000010.restart" in selected.inner_text()


def test_a_build_that_classifies_no_key_says_so_and_leaves_launch_on(
    pw: Any,  # noqa: F811
    world: tuple[Path, dict[str, str]],
) -> None:
    project, env = world
    with serve(project, env) as url:
        browser, pg = open_page(pw, url)
        try:
            select_run(pg, "stop-1")
            pg.locator(".run-head button", has_text="Restart from").click()
            notice = pg.locator(".restart-pick .notice", has_text="does not say")
            notice.wait_for()
            assert pg.locator(".restart-pick button", has_text="Launch from").is_enabled()
        finally:
            browser.close()
