"""The case's Files tab: the input and every file its setup read (`case.files`: the mesh, mechanisms, tables,
probe points, a restart), each with its role, path as written and resolved, and size, missing files flagged.
It reloads when the tab opens and after each check while it is open; builds without the request say so.

The app places `tab_button` and `panel` among the case page's tabs; everything else is this module's."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from .fmt import bytes_text

if TYPE_CHECKING:
    from .app import App


def files_view(result: dict[str, Any] | None) -> dict[str, Any]:
    """What the panel shows of a `mallard-case-files` result (None: a build without `case.files`)."""
    if result is None:
        return {"status": "unsupported", "files": [], "summary": "", "note": ""}
    if "error" in result:
        return {"status": "error", "files": [], "summary": "", "note": str(result["error"])}
    files = [
        {
            "role": str(f.get("role", "")),
            "path": str(f.get("path", "")),
            "resolved": str(f.get("resolved", "")),
            "exists": bool(f.get("exists")),
            "size": bytes_text(f.get("bytes")) if f.get("exists") else "missing",
        }
        for f in result.get("files") or []
        if isinstance(f, dict)
    ]
    missing = sum(not f["exists"] for f in files)
    total = sum(int(f.get("bytes") or 0) for f in result.get("files") or [] if isinstance(f, dict))
    summary = f"{len(files)} file{'s' if len(files) != 1 else ''} · {bytes_text(total)}"
    if missing:
        summary += f" · {missing} missing"
    note = "" if result.get("complete", True) else "The setup stopped early: these are the files it read before."
    return {"status": "missing" if missing else "ok", "files": files, "summary": summary, "note": note}


class CaseFilesPanel:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self._task: asyncio.Future[None] | None = None
        self.state.case_files = {"status": "", "files": [], "summary": "", "note": ""}
        self.state.change("tab")(self._tab_changed)
        self.state.change("check")(self._check_changed)

    def _tab_changed(self, tab: str = "", **_: Any) -> None:
        if tab == "files":
            self.refresh()

    def _check_changed(self, check: dict[str, Any] | None = None, **_: Any) -> None:
        if self.state.tab == "files" and check and not check.get("running") and check.get("status"):
            self.refresh()

    def refresh(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self.state.case_files = {**self.state.case_files, "status": "loading"}
        self._task = asyncio.ensure_future(self._load())

    async def _load(self) -> None:
        result = await asyncio.to_thread(self.app.session.refresh_case_files)
        with self.state:
            self.state.case_files = files_view(result)

    def tab_button(self) -> None:
        html.Button(
            "Files",
            classes=("tab === 'files' ? 'tab on' : 'tab'",),
            role="tab",
            click="tab = 'files'",
            title="The input and every file its setup reads",
        )

    def panel(self) -> None:
        with html.Div(v_if="tab === 'files'", classes="textview case-files"):
            with html.Div(classes="textbar"):
                html.Span("{{ case_files.summary }}", classes="grow mono")
                html.Button("Reload", classes="btn", click=self.refresh)
            html.Div("Reading the files the case reads…", classes="notice", v_if="case_files.status === 'loading'")
            html.Div(
                "This Mallard does not list the files a case reads (case.files).",
                classes="notice",
                v_if="case_files.status === 'unsupported'",
            )
            html.Div("{{ case_files.note }}", classes="notice", v_if="case_files.note")
            with html.Div(classes="scroll", v_if="case_files.files.length"):
                with html.Div(
                    v_for="f in case_files.files",
                    key="f.role + f.resolved",
                    classes=("['cf-row', {missing: !f.exists}]",),
                    title=("f.resolved",),
                ):
                    html.Span("{{ f.role }}", classes="cf-role")
                    html.Span("{{ f.path }}", classes="cf-path mono selectable")
                    html.Span("{{ f.size }}", classes="cf-size")
