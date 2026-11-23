"""Move or continue, on the data page: check a run's restart at another place, continue the run there from it,
or move the whole run there; and, after a move, remove the old home's files once the user confirms the list."""

from __future__ import annotations

import asyncio
import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from ..backends.ssh import RemoteError
from ..data.locations import TransferError
from ..data.relocate import RelocateError, RestartCheck
from ..session import RestartError
from .fmt import number

if TYPE_CHECKING:
    from .app import App

ERRORS = (RelocateError, RestartError, RemoteError, TransferError, KeyError, ValueError, OSError)


@dataclass
class _Progress:
    done: int = 0
    total: int = 0
    current: str = ""

    def __call__(self, done: int, total: int, current: str) -> None:
        self.done, self.total, self.current = done, total, current


class RelocatePanel:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        s = self.state
        s.rl_busy = ""
        s.rl_text = ""
        s.rl_error = ""
        s.rl_check = {}
        s.rl_old = []
        s.rl_old_loc = ""
        s.rl_confirm = False
        self._old_paths: list[str] = []

    def reset(self) -> None:
        s = self.state
        s.rl_busy, s.rl_text, s.rl_error, s.rl_check = "", "", "", {}
        s.rl_old, s.rl_old_loc, s.rl_confirm = [], "", False

    def _schedule(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self.app._tasks.add(task)
        task.add_done_callback(self.app._tasks.discard)

    def check(self) -> None:
        self._start("check")

    def continue_there(self) -> None:
        self._start("continue")

    def move(self) -> None:
        self._start("move")

    def _start(self, what: str) -> None:
        s = self.state
        if s.rl_busy or not s.data_run or not s.data_dst:
            return
        s.rl_busy, s.rl_error, s.rl_text, s.rl_check, s.rl_confirm = what, "", "", {}, False
        self._schedule(self._work(what, s.data_run, s.data_dst))

    async def _work(self, what: str, run: str, dst: str) -> None:
        session = self.app.session
        progress = _Progress()
        follow = asyncio.ensure_future(self._follow(progress, what))
        text, error, old_loc = "", "", ""
        check: RestartCheck | None = None
        old: list[str] = []
        try:
            if what == "check":
                record = session.project.run(run)
                checked, _ = await asyncio.to_thread(session.relocator.check_restart, record, dst, progress)
                check = checked
                text = f"{dst} reads the restart" if checked.ok else f"{dst} cannot continue from it"
            elif what == "continue":
                active, check = await asyncio.to_thread(session.continue_elsewhere, run, dst, progress)
                text = f"{active.record.name} started on {dst} (job {active.job.id}) from the restart"
            else:
                old_loc = session.data.home(session.project.run(run)).location.name
                record, check, old = await asyncio.to_thread(session.move_run, run, dst, progress)
                text = f"{run} now lives on {dst}: {self._short(record.home.get('directory', ''))}"
        except ERRORS as e:
            error = str(e).splitlines()[0][:400]
        follow.cancel()
        with self.state:
            s = self.state
            s.rl_busy, s.rl_text, s.rl_error = "", text, error
            s.rl_check = _check_view(check) if check is not None else {}
            s.rl_old, s.rl_old_loc = [self._short(p) for p in old], old_loc
            self._old_paths = old
            s.runs = self.app._runs_list()
        self.app.data_page.refresh()

    async def _follow(self, progress: _Progress, what: str) -> None:
        verb = {"check": "bringing the restart", "continue": "staging", "move": "copying the run"}[what]
        while True:
            with self.state:
                if progress.total:
                    pct = round(100 * progress.done / progress.total)
                    self.state.rl_text = f"{verb}: {pct} % · {posixpath.basename(progress.current)}"
                else:
                    self.state.rl_text = f"{verb} ..."
            await asyncio.sleep(0.3)

    def ask_remove(self) -> None:
        self.state.rl_confirm = True

    def remove_old(self) -> None:
        s = self.state
        if not s.rl_confirm or not s.rl_old_loc:
            return
        try:
            self.app.session.relocator.remove_old_home(s.rl_old_loc, self._old_paths)
        except ERRORS as e:
            s.rl_error = str(e)
            return
        s.rl_text = f"removed {len(self._old_paths)} files of the old home on {s.rl_old_loc}"
        s.rl_old, s.rl_confirm, self._old_paths = [], False, []

    def _short(self, path: str) -> str:
        root = str(self.app.session.project.root)
        return path[len(root) + 1 :] if path.startswith(root + "/") else path

    # Layout

    def layout(self) -> None:
        with html.Div(classes="panel-head"):
            html.Span("Move or continue")
            html.Span("to {{ data_dst || '–' }}", classes="mono")
        html.Div(
            "Continue moves the inputs and the newest restart, checked there first; Move copies the whole run "
            "and makes the destination its home.",
            classes="notice",
            v_if="!rl_text && !rl_error && !rl_check.facts",
        )
        with html.Div(classes="actions"):
            with html.Button(
                classes="btn",
                click=self.check,
                disabled=("!!rl_busy || !data_dst || !data_run",),
                title="Bring the newest restart and have the destination's mallard read it",
            ):
                html.Span(v_html="icons.check")
                html.Span("Check restart")
            with html.Button(
                classes="btn",
                click=self.continue_there,
                disabled=("!!rl_busy || !data_dst || !data_run",),
                title="A new run of the case there, from this run's newest restart",
            ):
                html.Span(v_html="icons.play")
                html.Span("Continue")
            with html.Button(
                classes="btn",
                click=self.move,
                disabled=("!!rl_busy || !data_dst || !data_run",),
                title="Copy the whole run there and make it the run's home",
            ):
                html.Span(v_html="icons.transfer")
                html.Span("Move")
        html.Div("{{ rl_text }}", classes="mono rl-text", v_if="rl_text")
        html.Div("{{ rl_error }}", classes="mono rl-text err", v_if="rl_error")
        with html.Div(v_if="rl_check.facts"):
            with html.Div(classes="prow"):
                html.Div("restart", classes="plabel")
                html.Div("{{ rl_check.name }}", classes="pval ro mono", title=("rl_check.path",))
            with html.Div(classes="prow"):
                html.Div("read at", classes="plabel")
                html.Div(
                    "{{ rl_check.destination }} · {{ rl_check.ok ? 'compatible' : 'not compatible' }}",
                    classes=("rl_check.ok ? 'pval ro okc' : 'pval ro err'",),
                )
            with html.Div(v_for="f in rl_check.facts", key="f.k", classes="prow"):
                html.Div("{{ f.k }}", classes="plabel")
                html.Div("{{ f.v }}", classes="pval ro mono", title=("f.v",))
            html.Div("{{ p }}", v_for="p in rl_check.problems", key="p", classes="mono rl-text err")
            html.Div("{{ n }}", v_for="n in rl_check.notes", key="n", classes="mono rl-text muted")
        with html.Div(v_if="rl_old.length"):
            with html.Div(classes="panel-head"):
                html.Span("Old home")
                html.Span("{{ rl_old_loc }} · {{ rl_old.length }} files, kept", classes="mono")
            with html.Div(v_if="rl_confirm", classes="rl-list mono"):
                html.Div("{{ p }}", v_for="p in rl_old", key="p")
            with html.Div(classes="actions"):
                html.Div(classes="grow")
                with html.Button(classes="btn", click=self.ask_remove, v_if="!rl_confirm"):
                    html.Span(v_html="icons.trash")
                    html.Span("Remove old home ...")
                with html.Button(classes="btn danger", click=self.remove_old, v_if="rl_confirm"):
                    html.Span(v_html="icons.trash")
                    html.Span("Delete these files")


def _check_view(check: RestartCheck) -> dict[str, Any]:
    f = check.facts
    facts = []
    if f:
        facts = [
            {"k": "format", "v": f"restart version {f.get('version')}"},
            {"k": "case", "v": f"{f.get('dim')}D · {f.get('precision')} · {number(f.get('cells') or 0)} cells"},
            {"k": "fields", "v": _names(f.get("fields") or [])},
            {"k": "species", "v": _names(f.get("species") or [])},
            {"k": "step, t", "v": f"{number(f.get('step') or 0)} · {f.get('t')}"},
        ]
    return {
        "path": check.path,
        "name": posixpath.basename(check.path),
        "destination": check.destination,
        "ok": check.ok,
        "problems": check.problems,
        "notes": check.notes,
        "facts": facts or [{"k": "read", "v": "no"}],
    }


def _names(names: list[str]) -> str:
    if not names:
        return "none"
    head = ", ".join(names[:5])
    return f"{len(names)}: {head}, ..." if len(names) > 5 else head
