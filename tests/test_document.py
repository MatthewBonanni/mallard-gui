"""The input as a document: edits stay in memory until Save, which is atomic; undo and redo; Save As; Revert;
the recovery file of unsaved edits, restored by the next session; Save / Don't Save / Cancel before the
document is left or a run starts; Open Recent."""

from __future__ import annotations

import asyncio
import itertools
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from trame.app import get_server

from mallard_gui import recent, switch
from mallard_gui.input_doc import InputDocument
from mallard_gui.session import Session, UnsavedChanges
from mallard_gui.ui.app import App

from .conftest import FakeMallard

_names = itertools.count()


class RecordingMallard(FakeMallard):
    def __init__(self) -> None:
        super().__init__()
        self.checked: list[str] = []

    def check(self, input_file: Path, view_dir: Path | None = None, extra: Any = (), catalog: Any = None) -> Any:
        self.checked.append(input_file.read_text(encoding="utf-8"))
        return super().check(input_file, view_dir, extra, catalog)


# The document


def test_edits_stay_in_memory_and_save_writes_them(cylinder_project: Path) -> None:
    path = cylinder_project / "input.toml"
    before = path.read_text()
    doc = InputDocument(path)
    doc.set("run.cfl", 0.5)
    doc.commit()
    assert doc.dirty and path.read_text() == before
    assert doc.recovery_path.read_text() == doc.text  # crash-safe while unsaved
    doc.save()
    assert not doc.dirty and InputDocument(path).get("run.cfl") == 0.5
    assert not doc.recovery_path.exists()
    assert "# Flow past a circular cylinder" in path.read_text()  # the writer keeps comments


def test_undo_and_redo_step_through_commits_and_a_new_edit_drops_redo(cylinder_project: Path) -> None:
    doc = InputDocument(cylinder_project / "input.toml")
    original = doc.text
    for cfl in (0.5, 0.6):
        doc.set("run.cfl", cfl)
        doc.commit()
    assert doc.undo() and doc.get("run.cfl") == 0.5
    assert doc.undo() and doc.text == original and not doc.dirty
    assert not doc.recovery_path.exists()  # back to the saved text: nothing to recover
    assert not doc.undo()
    assert doc.redo() and doc.get("run.cfl") == 0.5 and doc.recovery_path.exists()
    doc.set("run.cfl", 0.9)
    doc.commit()
    assert not doc.can_redo


def test_undo_after_save_goes_back_past_it_and_marks_the_document_edited(cylinder_project: Path) -> None:
    doc = InputDocument(cylinder_project / "input.toml")
    doc.set("run.cfl", 0.5)
    doc.save()
    assert doc.undo() and doc.dirty


def test_a_failed_save_leaves_the_file_and_the_edits(cylinder_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = cylinder_project / "input.toml"
    before = path.read_text()
    doc = InputDocument(path)
    doc.set("run.cfl", 0.5)

    def fail(*_: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        doc.save()
    assert path.read_text() == before
    assert doc.dirty
    assert not [p for p in cylinder_project.iterdir() if p.name.endswith(".tmp")]


def test_save_keeps_permissions_and_symbolic_links(cylinder_project: Path) -> None:
    real = cylinder_project / "input.toml"
    real.chmod(0o640)
    link = cylinder_project / "linked.toml"
    link.symlink_to(real)
    doc = InputDocument(link)
    doc.set("run.cfl", 0.5)
    doc.save()
    assert link.is_symlink() and InputDocument(real).get("run.cfl") == 0.5
    assert stat.S_IMODE(real.stat().st_mode) == 0o640


def test_revert_is_one_undoable_step(cylinder_project: Path) -> None:
    doc = InputDocument(cylinder_project / "input.toml")
    doc.set("run.cfl", 0.5)
    doc.commit()
    doc.revert()
    assert not doc.dirty and not doc.recovery_path.exists()
    assert doc.undo() and doc.get("run.cfl") == 0.5


def test_the_next_session_restores_unsaved_edits_and_undo_returns_to_the_file(cylinder_project: Path) -> None:
    path = cylinder_project / "input.toml"
    doc = InputDocument(path)
    doc.set("run.cfl", 0.5)
    doc.commit()  # and the process dies here
    again = InputDocument(path)
    assert again.restore_recovery()
    assert again.dirty and again.get("run.cfl") == 0.5 and again.recovered
    assert again.undo() and again.text == path.read_text()


def test_a_recovery_file_equal_to_the_input_goes_and_a_broken_one_stays(cylinder_project: Path) -> None:
    doc = InputDocument(cylinder_project / "input.toml")
    doc.recovery_path.write_text(doc.text)
    assert not doc.restore_recovery() and not doc.recovery_path.exists()
    doc.recovery_path.write_text("[run\n")
    assert not doc.restore_recovery() and doc.recovery_path.exists()


# The session


def test_check_reads_the_unsaved_edits_and_runs_need_them_saved(cylinder_project: Path) -> None:
    m = RecordingMallard()
    s = Session(cylinder_project, m)  # type: ignore[arg-type]
    s.set_value("run.t_stop", "321")
    s.check()
    assert s.checked_path.parent == cylinder_project  # relative paths (the mesh) resolve as from the input
    assert "t_stop = 321" in m.checked[-1]
    with pytest.raises(UnsavedChanges):
        s.start_run()
    s.save()
    assert s.checked_path == s.input_path


def test_save_as_in_the_project_becomes_one_of_its_inputs(cylinder_project: Path) -> None:
    s = Session(cylinder_project, FakeMallard())  # type: ignore[arg-type]
    s.set_value("run.t_stop", "5")
    assert s.save_as(cylinder_project / "coarse.toml")
    assert s.input_path.name == "coarse.toml" and not s.doc.dirty
    assert recent.load()[0] == (cylinder_project / "coarse.toml").resolve()
    assert "t_stop = 5" not in (cylinder_project / "input.toml").read_text()
    assert "coarse.toml" in Session(cylinder_project, FakeMallard()).project.inputs  # type: ignore[arg-type]


def test_save_as_elsewhere_copies_the_project_but_not_its_runs(cylinder_project: Path, tmp_path_factory: Any) -> None:
    (cylinder_project / "runs" / "input-1").mkdir(parents=True)
    (cylinder_project / "runs" / "input-1" / "mallard.jsonl").write_text("{}\n")
    s = Session(cylinder_project, FakeMallard())  # type: ignore[arg-type]
    s.set_value("run.t_stop", "5")
    s.doc.commit()
    dst = tmp_path_factory.mktemp("copy") / "variant.toml"
    assert not s.save_as(dst, copy_project=True)
    assert (dst.parent / "cylinder.msh").is_file()
    assert not (dst.parent / "runs").exists()
    assert not any(p.name.startswith(".") for p in dst.parent.iterdir())  # no recovery files copied
    assert "t_stop = 5" in dst.read_text()
    assert not s.doc.recovery_path.exists()


def test_recent_inputs_are_newest_first_without_duplicates_or_missing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MALLARD_GUI_SETTINGS", str(tmp_path / "shell" / "settings.toml"))
    files = []
    for i in range(recent.LIMIT + 2):
        f = tmp_path / f"case{i}" / "input.toml"
        f.parent.mkdir()
        f.write_text("")
        files.append(f)
        recent.add(f)
    recent.add(files[3])
    files[5].unlink()
    got = recent.load()
    assert got[0] == files[3].resolve() and len(set(got)) == len(got) and files[5].resolve() not in got
    assert len(got) == recent.LIMIT - 1
    assert (tmp_path / "shell" / "settings.toml").is_file()  # the shell's file, not the config directory's


# The app: Save / Don't Save / Cancel


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    for task in asyncio.all_tasks(loop):
        task.cancel()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def app(cylinder_project: Path, loop: asyncio.AbstractEventLoop) -> App:
    server = get_server(f"doc-{next(_names)}", client_type="vue3")
    a = App(Session(cylinder_project, FakeMallard()), server)  # type: ignore[arg-type]
    a.ctrl.view_update = lambda: None
    return a


def edit(app: App) -> None:
    app.set_key("run.t_stop", "77")


def test_edits_mark_the_title_and_save_clears_it(app: App) -> None:
    edit(app)
    assert app.state.doc_edited and app.state.trame__title.startswith("• input.toml")
    assert app.state.doc_info["edited"] and app.state.doc_can_undo
    app.document.command("save")
    assert not app.state.doc_edited and not app.state.trame__title.startswith("•")
    assert "t_stop = 77" in app.session.input_path.read_text()


def test_closing_with_unsaved_edits_asks_and_cancel_keeps_them(app: App) -> None:
    edit(app)
    app.document.command("close")
    assert app.state.doc_prompt["open"]
    assert [b["label"] for b in app.state.doc_prompt["buttons"]] == ["Don't Save", "Cancel", "Save"]
    app.document.answer("cancel")
    assert not app.state.doc_closed and app.session.doc.dirty and app.session.doc.recovery_path.exists()


def test_dont_save_closes_and_drops_the_recovery_file(app: App, loop: asyncio.AbstractEventLoop) -> None:
    edit(app)
    before = app.session.input_path.read_text()
    app.document.command("quit")
    app.document.answer("discard")
    assert app.state.doc_closed
    assert not app.session.doc.recovery_path.exists() and app.session.input_path.read_text() == before


def test_save_then_close_in_the_shell_asks_the_shell(app: App) -> None:
    app.document.set_client(True)
    edit(app)
    app.document.command("close")
    app.document.answer("save")
    assert "t_stop = 77" in app.session.input_path.read_text()
    assert app.state.doc_shell_call["cmd"] == "closeProject"


def test_a_run_with_unsaved_edits_saves_first_or_does_not_start(app: App, loop: asyncio.AbstractEventLoop) -> None:
    started: list[str] = []
    app.session.start_run = lambda *a, **k: started.append(app.session.input_path.read_text())  # type: ignore[assignment,method-assign]
    app._follow = lambda run: None  # type: ignore[method-assign]
    loop.run_until_complete(app.run_check())
    edit(app)
    app.start_run()
    assert not started and app.state.doc_prompt["open"]
    app.document.answer("cancel")
    assert not started
    app.start_run()
    app.document.answer("save")
    assert started and "t_stop = 77" in started[0]


def test_open_in_a_browser_moves_the_page_to_a_new_server(
    app: App, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: Any
) -> None:
    other = tmp_path_factory.mktemp("other") / "input.toml"
    other.write_text("[mesh]\n")
    opened: list[Path] = []

    def start_server(path: Path, *_: Any) -> str:
        opened.append(path)
        return "http://127.0.0.1:1/?secret=x"

    monkeypatch.setattr(switch, "start_server", start_server)
    monkeypatch.setattr(app.server, "stop", lambda: asyncio.sleep(0))
    edit(app)
    app.document.command("open_recent", str(other))
    app.document.answer("save")
    loop.run_until_complete(asyncio.sleep(0.05))
    assert opened == [other] and app.state.doc_navigate.startswith("http://127.0.0.1:1/")


def test_save_as_asks_the_page_and_never_opens_the_servers_picker_itself(app: App) -> None:
    edit(app)
    app.document.command("save_as")  # no shell reported: the page still decides
    req = app.state.path_request
    assert (req["mode"], req["name"], req["directory"]) == ("save", "input.toml", str(app.session.project.root))
    assert req["filters"] == [{"name": "Mallard input", "extensions": ["toml"]}]
    assert not app.state.path_picker["open"]
    app.paths.reply(req["seq"], str(app.session.project.root / "native"))
    assert app.session.input_path.name == "native.toml" and not app.state.doc_edited
    assert not app.state.path_picker["open"]


def test_a_cancelled_or_failed_native_dialog_changes_nothing(app: App) -> None:
    app.document.command("open")
    app.paths.reply(app.state.path_request["seq"], None)
    app.document.command("save_as")
    app.paths.reply(app.state.path_request["seq"], None, "denied")
    assert app.session.input_path.name == "input.toml" and "denied" in app.state.doc_error
    assert not app.state.path_picker["open"]


def test_without_a_shell_the_servers_picker_saves_as_a_new_input(app: App) -> None:
    edit(app)
    app.document.command("save_as")
    app.paths.use_server_picker(app.state.path_request["seq"])
    picker = app.state.path_picker
    assert picker["open"] and {"name": "input.toml", "kind": "file"} in picker["entries"]
    assert {"name": "cylinder.msh", "kind": "file"} not in picker["entries"]  # the kind's filter
    app.paths.picker_ok("refined")
    assert app.session.input_path.name == "refined.toml" and not app.state.doc_edited
    assert "t_stop = 77" in (app.session.project.root / "refined.toml").read_text()


def test_the_servers_picker_asks_before_replacing_another_file(app: App) -> None:
    other = app.session.project.root / "other.toml"
    other.write_text("[mesh]\n")
    edit(app)
    app.document.command("save_as")
    app.paths.use_server_picker(app.state.path_request["seq"])
    app.paths.picker_ok("other.toml")
    assert app.state.path_picker["open"] and "exists" in app.state.path_picker["error"]
    assert other.read_text() == "[mesh]\n"
    app.paths.picker_ok("other.toml")
    assert "t_stop = 77" in other.read_text()


def test_the_mesh_is_chosen_with_mesh_filters_and_stored_relative_to_the_project(app: App) -> None:
    meshes = app.session.project.root / "meshes"
    meshes.mkdir()
    (meshes / "fine.cgns").write_text("")
    app.setup.choose_mesh()
    req = app.state.path_request
    assert req["mode"] == "open" and "cgns" in req["filters"][0]["extensions"]
    app.paths.reply(req["seq"], str(meshes / "fine.cgns"))
    assert app.session.doc.get("mesh.filename") == "meshes/fine.cgns"


def test_import_mesh_from_the_file_menu_asks_for_a_mesh_on_the_case_page(app: App) -> None:
    app.set_page("runs")
    app.document.command("import_mesh")
    req = app.state.path_request
    assert app.state.page == "case" and req["mode"] == "open" and "msh" in req["filters"][0]["extensions"]


def test_open_in_paraview_without_results_open_says_what_to_open(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(app.exports, "paraview", lambda: opened.append(True))
    app.document.command("paraview")
    assert "run the case first" in app.state.doc_error and app.state.page == "case"
    app.state.runs = [{"name": "input-1"}]
    app.document.command("paraview")
    assert app.state.page == "results" and app.state.h_toasts[-1]["title"] == "Open in ParaView" and not opened
    app.state.v_info = {"count": 3}
    app.document.command("paraview")
    assert opened == [True]


def test_a_mesh_dropped_on_the_window_becomes_the_cases_mesh(app: App) -> None:
    mesh = app.session.project.root / "meshes" / "fine.msh"
    mesh.parent.mkdir()
    mesh.write_text("")
    app.set_page("runs")
    app.document.command("use_mesh", str(mesh))
    assert app.state.page == "case" and app.session.doc.get("mesh.filename") == "meshes/fine.msh"


def test_about_names_the_gui_and_the_solver_apart(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    from mallard_gui import __version__
    from mallard_gui.mallard import MallardError

    app.document.command("about")
    about = app.state.doc_about
    assert about["gui"] == f"Mallard GUI {__version__}"
    assert about["solver"] == f"Solver: mallard {app.session.mallard.version()} at /nonexistent/mallard"

    def gone() -> str:
        raise MallardError("no such file")

    monkeypatch.setattr(app.session.mallard, "version", gone)
    app.document.command("about")
    assert app.state.doc_about["solver"].startswith("Solver: not found")
