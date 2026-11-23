"""Edits to the open input by another program (an editor, git, a script). The file is looked at every second
(its size and modification time, then its text): without unsaved edits here it is reloaded, as an undo step;
with them, the page asks Reload / Keep Mine / Compare, Compare showing the two side by side as a diff."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import os
import tomllib
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from ..health import INFO

if TYPE_CHECKING:
    from .app import App

INTERVAL = 1.0
DIFF_LINES = 400


def diff_rows(mine: str, disk: str) -> list[dict[str, Any]]:
    """A unified diff of the text here against the file, as rows to show: kind (hunk, del, add, same) and text."""
    rows = []
    lines = difflib.unified_diff(mine.splitlines(), disk.splitlines(), "here (unsaved)", "on disk", lineterm="", n=2)
    for i, line in enumerate(lines):
        if i < 2:
            continue  # the ---/+++ header: the columns say it
        kind = {"@": "hunk", "-": "del", "+": "add"}.get(line[:1], "same")
        rows.append({"i": i, "kind": kind, "text": line})
        if len(rows) >= DIFF_LINES:
            rows.append({"i": i + 1, "kind": "hunk", "text": "… (more differences)"})
            break
    return rows


class FileWatch:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self._seen: tuple[str, int, int] | None = None
        self._disk = ""
        self._task: asyncio.Task[None] | None = None
        s = self.state
        s.ext = {"open": False, "name": "", "error": "", "gone": False}
        s.ext_compare = False
        s.ext_diff = []
        self.mark_seen()
        app.ctrl.on_server_ready.add(self._start)

    def _signature(self) -> tuple[str, int, int] | None:
        path = self.app.session.input_path
        try:
            st = os.stat(path)
        except OSError:
            return (str(path), -1, -1)
        return (str(path), st.st_mtime_ns, st.st_size)

    def mark_seen(self) -> None:
        self._seen = self._signature()

    def _start(self, **_: Any) -> None:
        self._task = asyncio.ensure_future(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(INTERVAL)
            with contextlib.suppress(Exception):  # a bad poll must not end the watch
                with self.state:
                    self.poll()

    def poll(self) -> None:
        """Look at the file; act when another program changed it."""
        sig = self._signature()
        if sig == self._seen:
            return
        self._seen = sig
        ses = self.app.session
        disk = ses.disk_change()
        if disk is None:
            if self.state.ext["open"]:
                self._close()  # changed back, or this session wrote it
            return
        self._disk = disk
        name = ses.input_path.name
        error = ""
        if disk:
            try:
                tomllib.loads(disk)
            except tomllib.TOMLDecodeError as e:
                error = f"it is not valid TOML: {e}"
        if not ses.doc.dirty and disk and not error:
            self.reload()
            self.app.health.toast(f"{name} changed on disk", "Reloaded. Undo brings back the previous text.", INFO)
            return
        self.state.ext = {"open": True, "name": name, "error": error, "gone": not disk}
        if self.state.ext_compare:
            self.state.ext_diff = diff_rows(ses.doc.text, disk)

    # Answers

    def reload(self) -> None:
        ses = self.app.session
        try:
            ses.reload_from_disk()
        except (OSError, ValueError) as e:
            self.state.ext = {**self.state.ext, "open": True, "error": f"cannot reload it: {e}"}
            return
        self.mark_seen()
        self._close()
        self.app.document.sync()
        self.app._refresh_case()
        self.app._schedule_check()

    def keep_mine(self) -> None:
        self.app.session.keep_mine(self._disk)
        self.mark_seen()
        self._close()
        self.app.document.sync()

    def compare(self) -> None:
        self.state.ext_diff = diff_rows(self.app.session.doc.text, self._disk)
        self.state.ext_compare = True

    def _close(self) -> None:
        self.state.ext = {"open": False, "name": "", "error": "", "gone": False}
        self.state.ext_compare = False
        self.state.ext_diff = []

    # Layout

    def layout(self) -> None:
        with html.Div(
            classes="doc-notice ext-notice",
            v_if="ext.open && !ext_compare",
            role="alertdialog",
            aria_label="The input changed on disk",
        ):
            html.Span(v_html="icons.warning", classes="ext-icon")
            with html.Div(classes="ext-text"):
                html.Div(
                    "{{ ext.name }} {{ ext.gone ? 'was deleted or moved' : 'changed on disk' }}",
                    classes="ext-title",
                )
                html.Div(
                    "{{ ext.error ? 'It changed on disk, but ' + ext.error : ext.gone ? "
                    "'Save writes your version again.' : 'You have unsaved changes here.' }}",
                    classes=("['ext-body', {err: ext.error}]",),
                )
            html.Button("Compare", classes="btn", click=self.compare, v_if="!ext.gone")
            html.Button("Keep Mine", classes="btn", click=self.keep_mine)
            html.Button("Reload", classes="btn primary", click=self.reload, v_if="!ext.gone && !ext.error")
        with html.Div(
            classes="modal-scrim",
            v_if="ext.open && ext_compare",
        ):
            with html.Div(classes="modal ext-compare", role="dialog", aria_label="Compare with the file on disk"):
                with html.Div(classes="modal-head"):
                    html.Span("{{ ext.name }}: your unsaved version and the file on disk")
                with html.Div(classes="ext-legend mono"):
                    html.Span("− here (unsaved)", classes="del")
                    html.Span("+ on disk", classes="add")
                with html.Div(classes="modal-body ext-diff mono"):
                    html.Div("{{ r.text }}", v_for="r in ext_diff", key="r.i", classes=("['ext-line', r.kind]",))
                with html.Div(classes="actions doc-dialog-actions"):
                    html.Button("Back", classes="btn", click="ext_compare = false")
                    html.Div(classes="grow")
                    html.Button("Keep Mine", classes="btn", click=self.keep_mine)
                    html.Button("Reload", classes="btn primary", click=self.reload, v_if="!ext.error")
