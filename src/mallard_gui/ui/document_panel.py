"""The input as a document: Save, Save As, Revert to Saved, Undo and Redo, Open, Open Recent, New Project, Close and
Quit, with Save / Don't Save / Cancel before unsaved edits are left behind and before a run.

The same commands come from the desktop shell's menu (through `window.mallardMenu`), from keyboard shortcuts in a
browser and from the command palette. Paths are chosen through `PathChooser` (paths.py), in the shell's native
panels; another project opens in the shell's restarted server, else in a new server the page moves to.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import client, html

from .. import __version__, recent, switch
from ..project import copy_plan, new_project
from .fmt import bytes_text

if TYPE_CHECKING:
    from .app import App

DOCS = "https://github.com/MatthewBonanni/mallard/tree/main/docs"
SITE = "https://github.com/MatthewBonanni/mallard"
GUARDED = ("open", "open_recent", "new_project", "close", "quit")
# How long a browser page has to move to another server, or to show that this one closed, before it stops.
STOP_AFTER = 1.5

# The rail's pages, in its order: Cmd/Ctrl+1... goes to each (desktop/src-tauri/src/menu.rs, document.js).
PAGES = (
    ("case", "Case setup"),
    ("runs", "Runs"),
    ("data", "Data"),
    ("results", "Results"),
    ("analysis", "Analysis"),
    ("compare", "Compare runs"),
    ("remotes", "Remotes"),
)

# The palette's document commands: id, label, detail.
COMMANDS = (
    ("save", "Save", "write the input  ⌘S"),
    ("save_as", "Save As…", "a new input file  ⇧⌘S"),
    ("revert", "Revert to Saved", "the input as on disk"),
    ("undo", "Undo", "the last edit  ⌘Z"),
    ("redo", "Redo", "⇧⌘Z"),
    ("open", "Open…", "another project or input  ⌘O"),
    ("new_project", "New Project…", "a directory with a new input"),
    ("close", "Close Project", "this project"),
    ("reveal", "Reveal in Finder", "the input file"),
    ("import_mesh", "Import Mesh…", "the case's mesh file  ⇧⌘I"),
    ("paraview", "Open in ParaView", "the results view's series and view  ⇧⌘P"),
    ("docs", "Mallard Documentation", "the user guide"),
)


class DocumentPanel:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self.session = app.session
        self._seq = itertools.count(1)
        self._replies: dict[int, tuple[str, Any]] = {}  # shell calls waiting for an answer: purpose, argument
        self._pending: tuple[str, Any] | None = None  # what the open prompt decides
        self._discard = False  # Don't Save was chosen: the recovery file goes once the document is left
        s = self.state
        s.doc_shell = False
        s.doc_about = {}
        s.doc_goto = {}
        s.doc_reveal = {}
        s.doc_prompt = {"open": False}
        s.doc_shell_call = {}
        s.doc_navigate = ""
        s.doc_closed = ""
        s.doc_error = ""
        s.doc_busy = ""
        s.doc_recent = []
        s.doc_info = {}
        doc = self.session.doc
        s.doc_recovered = (
            f"Recovered unsaved changes from {time.strftime('%Y-%m-%d %H:%M', time.localtime(doc.recovered))}: "
            "save to keep them, or revert to the saved file."
            if doc.recovered
            else ""
        )
        server = app.server
        server.trigger("doc_command")(self.command)
        server.trigger("doc_client")(self.set_client)
        server.trigger("doc_answer")(self.answer)
        server.trigger("doc_shell_reply")(self.shell_reply)
        self.state.change("mesh_edges")(lambda **_: self.sync())
        self.sync()

    # What the page shows of the document

    def sync(self) -> None:
        """After anything that changes the document or its file: title, edited marker, undo state."""
        s, ses, doc = self.state, self.session, self.session.doc
        name = ses.input_path.name
        s.input_name = name
        s.doc_edited = doc.dirty
        s.doc_can_undo = doc.can_undo
        s.doc_can_redo = doc.can_redo
        s.trame__title = f"{'• ' if doc.dirty else ''}{name} - {ses.project.name} - Mallard"
        s.doc_recent = [
            {"path": str(p), "label": f"{p.parent.name}/{p.name}"} for p in recent.load() if p != ses.input_path
        ]
        s.doc_info = {
            "title": f"{name} — {ses.project.name}",
            "path": str(ses.input_path),
            "edited": doc.dirty,
            "canUndo": doc.can_undo,
            "canRedo": doc.can_redo,
            "mesh": bool(s.mesh_edges),
        }
        if not doc.dirty:
            s.doc_recovered = ""

    def set_client(self, shell: bool) -> None:
        self.state.doc_shell = bool(shell)

    def _edited(self) -> None:
        self.app._refresh_case()
        self.app._schedule_check()

    # Commands

    def command(self, name: str, arg: Any = None) -> None:
        self.state.doc_error = ""
        if name == "save":
            self.save()
        elif name == "save_as":
            self._ask_path("save_as")
        elif name == "revert":
            self.session.doc.revert()
            self._edited()
        elif name == "undo":
            if self.session.doc.undo():
                self._edited()
        elif name == "redo":
            if self.session.doc.redo():
                self._edited()
        elif name in GUARDED:
            self.guard(name, arg)
        elif name == "reveal":
            self.reveal()
        elif name == "import_mesh":
            self.app.set_page("case")
            self.app.setup.choose_mesh()
        elif name == "paraview":
            self.open_in_paraview()
        elif name == "use_mesh":  # a mesh file dropped on the desktop app's window
            self.app.set_page("case")
            self.app.setup.set_mesh(Path(str(arg)))
        elif name == "docs":
            self._shell("openUrl", [DOCS])
        elif name == "show_mesh":
            self.app.toggle_view("mesh")
            self.sync()
        elif name == "fit":
            self.app.fit()
        elif name == "theme":
            self.app.toggle_theme()
        elif name == "settings":
            self.app.set_page("settings")
        elif name.startswith("page_") and name.removeprefix("page_") in dict(PAGES):
            self.app.set_page(name.removeprefix("page_"))
        elif name == "about":
            self.about()
        elif name == "clear_recent":
            recent.clear()
            self.sync()
        else:
            raise ValueError(f"unknown document command {name!r}")

    def about(self) -> None:
        """The GUI's version, and apart from it the solver's: the GUI is not Mallard."""
        from ..mallard import MallardError

        exe = self.session.mallard.executable
        try:
            solver = f"mallard {self.session.mallard.version()} at {exe}"
        except (MallardError, OSError) as e:
            solver = f"not found ({exe}: {e})"
        self.state.doc_about = {"open": True, "gui": f"Mallard GUI {__version__}", "solver": f"Solver: {solver}"}

    def save(self) -> bool:
        try:
            self.session.save()
        except OSError as e:
            self._error(f"Could not save {self.session.input_path.name}: {e.strerror or e}")
            return False
        self.sync()
        self.app._schedule_check()  # from the file now, rather than the recovery file
        return True

    def open_in_paraview(self) -> None:
        """The results view's series in ParaView; from elsewhere, the Results page, saying what to open first."""
        s = self.state
        if s.page == "results" and (s.v_info or {}).get("count"):
            self.app.exports.paraview()
            return
        if not s.runs:
            self._error("Open in ParaView opens a run's results: run the case first.")
            return
        self.app.set_page("results")
        self.app.health.toast("Open in ParaView", "choose a run's snapshot series here, then Open in ParaView")

    def reveal(self) -> None:
        path = self.session.input_path
        if self.state.doc_shell:
            self._shell("reveal", [str(path)])
        else:
            switch.reveal(path)

    def _error(self, text: str) -> None:
        self.state.doc_error = text

    # Leaving the document: Save / Don't Save / Cancel first

    def guard(self, action: str, arg: Any = None) -> None:
        """Carry out `action` once unsaved edits are saved or let go, or not at all if the user cancels."""
        if not self.session.doc.dirty:
            self._proceed(action, arg)
            return
        name = self.session.input_path.name
        verb = {
            "open": "opening another project",
            "open_recent": "opening another project",
            "new_project": "making a new project",
            "close": "closing",
            "quit": "quitting",
        }[action]
        self._prompt(
            (action, arg),
            f"Save changes to {name} before {verb}?",
            "Your changes are lost if you don't save them.",
            [("discard", "Don't Save", False), ("cancel", "Cancel", False), ("save", "Save", True)],
        )

    def guard_run(self, start: Any) -> bool:
        """Whether a run can start now; with unsaved edits, asks to save first and starts it after."""
        if not self.session.doc.dirty:
            return True
        name = self.session.input_path.name
        self._prompt(
            ("run", start),
            f"Save {name} before running?",
            f"A run starts from {name} as saved on disk; it has unsaved changes.",
            [("cancel", "Cancel", False), ("save", "Save and Run", True)],
        )
        return False

    def _prompt(self, pending: tuple[str, Any], title: str, body: str, buttons: list[tuple[str, str, bool]]) -> None:
        self._pending = pending
        self.state.doc_prompt = {
            "open": True,
            "title": title,
            "body": body,
            "buttons": [{"id": b, "label": label, "primary": primary} for b, label, primary in buttons],
            "error": "",
        }

    def answer(self, choice: str) -> None:
        pending, self._pending = self._pending, None
        self.state.doc_prompt = {"open": False}
        if pending is None or choice == "cancel":
            return
        action, arg = pending
        if action == "save_as_elsewhere":
            self._save_as(Path(arg), copy=choice == "copy")
            return
        if choice == "save":
            if not self.save():
                return
        elif choice == "discard":
            self._discard = True
        if action == "run":
            arg()
        else:
            self._proceed(action, arg)

    def _proceed(self, action: str, arg: Any) -> None:
        if action == "open":
            self._ask_path("open")
        elif action == "open_recent":
            self._leave_to(Path(str(arg)))
        elif action == "new_project":
            self._ask_path("new_project")
        elif action in ("close", "quit"):
            self._leave(action)

    def _leave(self, action: str) -> None:
        if self._discard:
            self.session.doc.discard_recovery()
        if self.state.doc_shell:
            self._shell("closeProject" if action == "close" else "quit", [])
            return
        self.state.doc_closed = f"{self.session.input_path.name} is closed. You can close this tab."
        self._stop_soon()

    def _leave_to(self, path: Path) -> None:
        """Open another project (a directory, or an input in one) in place of this one."""
        if self.state.doc_shell:
            if self._discard:
                self.session.doc.discard_recovery()
            self._shell("openProject", [str(path)], purpose="open_project")
            return
        self.state.doc_error = ""
        self.state.doc_busy = f"Opening {path}…"
        future = asyncio.ensure_future(self._open_in_browser(path))
        self.app._tasks.add(future)
        future.add_done_callback(self.app._tasks.discard)

    async def _open_in_browser(self, path: Path) -> None:
        ses = self.session
        try:
            url = await asyncio.to_thread(switch.start_server, path, ses.mallard.executable, ses.mallard.threads)
        except switch.OpenError as e:
            with self.state:
                self.state.doc_busy = ""
                self._error(str(e))
            return
        if self._discard:
            ses.doc.discard_recovery()
        with self.state:
            self.state.doc_busy = ""
            self.state.doc_navigate = url
        self._stop_soon()

    def _stop_soon(self) -> None:
        async def stop() -> None:
            await asyncio.sleep(STOP_AFTER)
            await self.app.server.stop()

        future = asyncio.ensure_future(stop())
        self.app._tasks.add(future)
        future.add_done_callback(self.app._tasks.discard)

    # Choosing a path

    def _ask_path(self, purpose: str) -> None:
        ses, paths = self.session, self.app.paths
        if purpose == "save_as":
            paths.choose("save_input", ses.input_path.parent, ses.input_path.name, self.save_as)
        elif purpose == "open":
            paths.choose("open_input", ses.input_path.parent, "", self._leave_to)
        else:
            paths.choose("new_project", ses.project.root.parent, "new-case", self._new_project)

    def _new_project(self, target: Path) -> None:
        try:
            created = new_project(target)
        except OSError as e:
            self._error(f"Could not make {target}: {e}")
            return
        self._leave_to(created)

    def save_as(self, path: Path) -> None:
        ses = self.session
        if path.suffix != ".toml":
            path = path.with_name(path.name + ".toml")
        if path.resolve().parent == ses.project.root:
            self._save_as(path, copy=False)
            return
        files = copy_plan(ses.project, path.parent)
        size = sum((ses.project.root / f).stat().st_size for f in files)
        self._prompt(
            ("save_as_elsewhere", str(path)),
            f"Copy the project to {path.parent}?",
            f"{path.name} is outside {ses.project.name}, so the paths it gives relative to the project (the mesh) "
            f"would not hold. Copy the project's {len(files)} file{'s' if len(files) != 1 else ''} "
            f"({bytes_text(size)}, without its runs) beside it?",
            [("cancel", "Cancel", False), ("input", "Input Only", False), ("copy", "Copy Project", True)],
        )

    def _save_as(self, path: Path, copy: bool) -> None:
        try:
            here = self.session.save_as(path, copy_project=copy)
        except OSError as e:
            self._error(f"Could not save {path}: {e.strerror or e}")
            return
        self.sync()
        if here:
            self.app._schedule_check()
        else:
            self._leave_to(path.resolve())

    def goto_line(self, line: int | None, column: int | None = None) -> None:
        """The text tab with the caret at a diagnostic's 1-based line and column (the line's start without one)."""
        if not line:
            return
        self.state.tab = "toml"
        self.state.doc_goto = {"line": int(line), "column": int(column or 1), "seq": next(self._seq)}

    def reveal_lines(self, spans: list[tuple[int, int]]) -> None:
        """The text scrolled to these 1-based line ranges, highlighted for a moment; applied when the text tab is
        next shown if it is not now, without switching to it or taking the focus."""
        if spans:
            self.state.doc_reveal = {"spans": [list(sp) for sp in spans], "seq": next(self._seq)}

    # The desktop shell's bridge, called by the page

    def _shell(self, cmd: str, args: list[Any], purpose: str = "") -> None:
        seq = next(self._seq)
        if purpose:
            self._replies[seq] = (purpose, None)
        self.state.doc_shell_call = {"seq": seq, "cmd": cmd, "args": args}

    def shell_reply(self, seq: int, result: Any = None, error: str = "") -> None:
        purpose, _ = self._replies.pop(int(seq), ("", None))
        if error:
            if purpose == "open_project" and self.session.doc.dirty:
                self.session.doc.keep_recovery()
            self._error(error)

    # Layout

    def layout_client(self) -> None:
        client.ClientTriggers(mounted=(self.set_client, "[!!window.mallardShell]"))
        client.ClientStateChange(value="doc_shell_call", change="window.mallardDocument.shellCall(doc_shell_call)")
        client.ClientStateChange(value="doc_navigate", change="window.mallardDocument.navigate(doc_navigate)")
        client.ClientStateChange(value="doc_goto", change="window.mallardDocument.gotoLine(doc_goto)")
        client.ClientStateChange(value="doc_reveal", change="window.mallardToml.reveal(doc_reveal)")
        client.ClientStateChange(
            value="doc_about", change="doc_about.open && window.mallardDocument.focus('.doc-about .btn.primary')"
        )
        client.ClientStateChange(
            value="doc_prompt", change="doc_prompt.open && window.mallardDocument.focus('.doc-prompt .btn.primary')"
        )
        client.ClientStateChange(
            value="doc_info",
            trigger_on_create=True,
            change="window.mallardShell && window.mallardShell.setDocument(doc_info)",
        )

    def layout(self) -> None:
        self._prompt_layout()
        self._about_layout()
        with html.Div(classes="doc-notice", v_if="doc_recovered || doc_error || doc_busy", role="status"):
            html.Span("{{ doc_error || doc_busy || doc_recovered }}", classes=("doc_error ? 'err' : ''",))
            html.Button(
                "Revert to Saved",
                classes="btn",
                v_if="doc_recovered && !doc_error && !doc_busy",
                click=(self.command, "['revert']"),
            )
            html.Button(
                "×",
                classes="icon-btn",
                title="Dismiss",
                v_if="!doc_busy",
                click="doc_error = ''; doc_recovered = ''",
            )
        with html.Div(classes="doc-closed", v_if="doc_closed"):
            html.Img(src="mallard_gui/logo@2x.png", alt="Mallard", classes="brand-mark")
            html.Div("{{ doc_closed }}")

    def _about_layout(self) -> None:
        with html.Div(classes="modal-scrim doc-about", v_if="doc_about.open", click_self="doc_about = {}"):
            with html.Div(classes="modal doc-dialog", role="dialog", aria_label="About Mallard GUI"):
                with html.Div(classes="doc-dialog-body"):
                    html.Img(src="mallard_gui/logo@2x.png", alt="", classes="doc-dialog-icon")
                    with html.Div():
                        html.Div("{{ doc_about.gui }}", classes="doc-dialog-title about-gui")
                        html.Div("Case setup, runs and analysis for the Mallard flow solver", classes="muted")
                        html.Div("{{ doc_about.solver }}", classes="mono about-solver", title=("doc_about.solver",))
                        html.Div("Matthew Bonanni · Apache License 2.0 · Icons: Lucide (ISC License)", classes="muted")
                        html.Button(
                            SITE.removeprefix("https://"),
                            classes="linkbtn about-site",
                            click=(self._shell, f"['openUrl', ['{SITE}']]"),
                        )
                with html.Div(classes="actions doc-dialog-actions"):
                    html.Button("OK", classes="btn primary", click="doc_about = {}")

    def _prompt_layout(self) -> None:
        with html.Div(classes="modal-scrim doc-prompt", v_if="doc_prompt.open"):
            with html.Div(
                classes="modal doc-dialog",
                role="alertdialog",
                aria_label=("doc_prompt.title",),
            ):
                with html.Div(classes="doc-dialog-body"):
                    html.Img(src="mallard_gui/logo@2x.png", alt="", classes="doc-dialog-icon")
                    with html.Div():
                        html.Div("{{ doc_prompt.title }}", classes="doc-dialog-title")
                        html.Div("{{ doc_prompt.body }}", classes="muted")
                with html.Div(classes="actions doc-dialog-actions"):
                    html.Button(
                        "{{ b.label }}",
                        v_for="b in doc_prompt.buttons",
                        key="b.id",
                        classes=("b.primary ? 'btn primary' : 'btn'",),
                        click=(self.answer, "[b.id]"),
                    )
