"""Choosing a path, for every command that needs one: `choose(kind, directory, name, then)`.

The page decides how when it is asked, not the server: the desktop shell's native dialog (the system's open and
save panels) whenever `window.mallardShell` exists, else a picker of the files on the machine that runs
mallard-gui. A browser's own file dialog gives a file's contents but never its path, and every choice here is a
path that the server opens or writes, so a browser has no native dialog to offer.
"""

from __future__ import annotations

import itertools
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import client, html

if TYPE_CHECKING:
    from .app import App

OPEN, SAVE = "open", "save"
MESH_EXTENSIONS = ("msh", "cas", "cgns", "h5", "hdf5")


@dataclass(frozen=True)
class Kind:
    mode: str
    title: str
    filters: tuple[tuple[str, tuple[str, ...]], ...] = ()
    project_dirs: bool = False  # a directory holding an input is a choice too (the server's picker)
    executable: bool = False  # the server's picker lists executable files (no extension to filter by)
    any_file: bool = False  # the server's picker lists every file

    def accepts(self, path: Path) -> bool:
        if self.executable:
            return os.access(path, os.X_OK)
        return not self.filters or any(path.suffix.lower().lstrip(".") in exts for _, exts in self.filters)


KINDS = {
    "open_input": Kind(OPEN, "Open a Mallard input", (("Mallard input", ("toml",)),), project_dirs=True),
    "save_input": Kind(SAVE, "Save the input as", (("Mallard input", ("toml",)),)),
    "new_project": Kind(SAVE, "New Project: a new directory for the case"),
    "mesh": Kind(OPEN, "Choose the mesh", (("Mesh", MESH_EXTENSIONS),)),
    "mechanism": Kind(OPEN, "Choose the reaction mechanism", (("Cantera mechanism", ("yaml", "yml")),)),
    "reference": Kind(OPEN, "Choose reference data", (("CSV", ("csv", "txt", "dat")),)),
    "image": Kind(SAVE, "Save the view as an image", (("PNG image", ("png",)),)),
    "executable": Kind(OPEN, "Choose a mallard executable", executable=True),
    "file": Kind(OPEN, "Choose a file", any_file=True),
}


def project_relative(root: Path, path: Path) -> str:
    """How an input names a file: relative to the project when inside it, so the project can move."""
    path, root = path.resolve(), root.resolve()
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def kind_for(name: str) -> str:
    """The kind of file a name is by its extension (a mesh, a table), whose dialog filters for it; else any file."""
    ext = Path(name).suffix.lower().lstrip(".")
    for kind, k in KINDS.items():
        if ext and k.mode == OPEN and any(ext in exts for _, exts in k.filters):
            return kind
    return "file"


def request(seq: int, kind: str, directory: Path, name: str, title: str = "") -> dict[str, Any]:
    """What the page receives: everything the native dialog needs."""
    k = KINDS[kind]
    return {
        "seq": seq,
        "kind": kind,
        "mode": k.mode,
        "title": title or k.title,
        "directory": str(directory),
        "name": name,
        "filters": [{"name": n, "extensions": list(exts)} for n, exts in k.filters],
    }


class PathChooser:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self._seq = itertools.count(1)
        self._pending: dict[int, tuple[dict[str, Any], Callable[[Path], None]]] = {}
        self._picking: tuple[int, str, Callable[[Path], None]] | None = None
        s = self.state
        s.path_request = {}
        s.path_picker = {"open": False}
        s.path_host = socket.gethostname().removesuffix(".local")
        server = app.server
        server.trigger("path_reply")(self.reply)
        server.trigger("path_server")(self.use_server_picker)
        server.trigger("path_picker_go")(self.picker_go)
        server.trigger("path_picker_ok")(self.picker_ok)

    def choose(self, kind: str, directory: Path, name: str, then: Callable[[Path], None], title: str = "") -> None:
        """Ask the page for a path of `kind`, starting in `directory` with `name`; `then` gets it unless cancelled."""
        seq = next(self._seq)
        req = request(seq, kind, directory, name, title)
        self._pending[seq] = (req, then)
        self.state.path_request = req

    def reply(self, seq: int, path: str | None = None, error: str = "") -> None:
        """The native dialog's answer: a path, or none when cancelled."""
        _, then = self._pending.pop(int(seq), ({}, None))
        if error:
            self.state.doc_error = f"The file dialog: {error}"
        elif then is not None and path:
            then(Path(path).expanduser())

    # The server's picker: no shell, so no native dialog

    def use_server_picker(self, seq: int) -> None:
        seq = int(seq)
        if seq not in self._pending:
            return
        req, then = self._pending.pop(seq)
        self._picking = (seq, req["kind"], then)
        self.state.path_picker = {**req, "open": True, "error": ""}
        self.picker_go(req["directory"] or str(Path.home()))

    def picker_go(self, directory: str) -> None:
        picker = dict(self.state.path_picker)
        kind = KINDS.get(str(picker.get("kind")))
        path = Path(directory).expanduser()
        if kind is None:
            return
        if not path.is_dir():
            self.state.path_picker = {**picker, "error": f"{path}: not a directory"}
            return
        path = path.resolve()
        try:
            children = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            self.state.path_picker = {**picker, "error": str(e)}
            return
        entries = []
        for child in children:
            if child.name.startswith("."):
                continue
            if child.is_dir():
                entries.append({"name": child.name, "kind": "dir"})
            elif (kind.filters or kind.executable or kind.any_file) and kind.accepts(child):
                entries.append({"name": child.name, "kind": "file"})
        self.state.path_picker = {
            **picker,
            "dir": str(path),
            "parent": str(path.parent) if path.parent != path else "",
            "entries": entries,
            "error": "",
        }

    def picker_ok(self, name: str = "") -> None:
        picker = dict(self.state.path_picker)
        if self._picking is None:
            self.state.path_picker = {"open": False}
            return
        _, kind_name, then = self._picking
        kind, directory = KINDS[kind_name], Path(str(picker.get("dir", "")))
        error = ""
        if kind.mode == OPEN:
            target = directory / name if name else directory
            if target.is_dir():
                if not (kind.project_dirs and any(p.suffix == ".toml" for p in target.iterdir())):
                    error = f"{target.name}: no input file here" if kind.project_dirs else "choose a file"
            elif not target.is_file():
                error = f"{name}: no such file"
        else:
            name = name.strip()
            target = directory / name
            if not name or os.sep in name:
                error = "a name, without directories"
            elif kind_name == "new_project" and target.exists():
                error = f"{name} exists: choose a new name"
            elif target.exists() and picker.get("replace") != str(target):
                self.state.path_picker = {**picker, "replace": str(target), "error": f"{name} exists: Save replaces it"}
                return
        if error:
            self.state.path_picker = {**picker, "error": error}
            return
        self._picking = None
        self.state.path_picker = {"open": False}
        then(target)

    def cancel(self) -> None:
        self._picking = None
        self.state.path_picker = {"open": False}

    # Layout

    def layout_client(self) -> None:
        client.ClientStateChange(value="path_request", change="window.mallardPaths.choose(path_request)")
        client.ClientStateChange(
            value="path_picker",
            change="path_picker.open && window.mallardDocument.focus('.path-picker input[aria-label=name]')",
        )

    def layout(self) -> None:
        with html.Div(classes="modal-scrim doc-picker path-picker", v_if="path_picker.open"):
            with html.Div(classes="modal", role="dialog", aria_label=("path_picker.title",)):
                with html.Div(classes="modal-head"):
                    html.Span("{{ path_picker.title }}")
                    html.Span(
                        "files on {{ path_host }}, the machine running mallard-gui",
                        classes="muted path-host",
                    )
                with html.Div(classes="doc-picker-path"):
                    html.Button(
                        "↑",
                        classes="icon-btn",
                        title="The directory above",
                        disabled=("!path_picker.parent",),
                        click=(self.picker_go, "[path_picker.parent]"),
                    )
                    html.Input(
                        value=("path_picker.dir",),
                        aria_label="directory",
                        classes="mono",
                        change=(self.picker_go, "[$event.target.value]"),
                    )
                with html.Div(classes="modal-body doc-picker-list"):
                    with html.Div(
                        v_for="e in path_picker.entries",
                        key="e.name",
                        classes=("['doc-entry', e.kind]",),
                        click=(
                            "e.kind === 'dir' ? trigger('path_picker_go', [path_picker.dir + '/' + e.name]) : "
                            "path_picker.mode === 'open' ? trigger('path_picker_ok', [e.name]) : "
                            "path_picker = {...path_picker, name: e.name}"
                        ),
                    ):
                        html.Span(v_html="e.kind === 'dir' ? icons.next : '·'", classes="chev")
                        html.Span("{{ e.name }}", classes="mono")
                    html.Div("nothing here", classes="muted doc-entry", v_if="!path_picker.entries?.length")
                with html.Div(classes="actions doc-dialog-actions"):
                    html.Input(
                        v_if="path_picker.mode === 'save'",
                        value=("path_picker.name",),
                        aria_label="name",
                        classes="mono grow",
                        input="path_picker = {...path_picker, name: $event.target.value}",
                        keydown_enter="trigger('path_picker_ok', [path_picker.name])",
                    )
                    html.Span("{{ path_picker.error }}", classes="err grow", v_if="path_picker.error")
                    html.Div(classes="grow", v_if="path_picker.mode !== 'save' && !path_picker.error")
                    html.Button("Cancel", classes="btn", click=self.cancel)
                    html.Button(
                        "{{ path_picker.mode === 'save' ? 'Save' : 'Choose' }}",
                        classes="btn primary",
                        click="trigger('path_picker_ok', [path_picker.mode === 'save' ? path_picker.name : ''])",
                    )
