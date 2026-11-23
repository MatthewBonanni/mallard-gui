"""The remotes page's installs: the remote's Mallard installs side by side with their verification, and
one-click installs, verifications, upgrades and removals, each behind a confirmation that shows exactly
what will run where."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from trame.widgets import html

from ..backends.ssh import RemoteError
from ..config import Install, Remote
from ..installs import (
    METHODS,
    VARIANTS,
    InstallError,
    InstallPlan,
    Outcome,
    fetch_manifest,
    install_directory,
    make_default,
    plan,
    remove,
    run_install,
    upgrade_target,
    verify,
)
from ..remotes import probe, release_of

if TYPE_CHECKING:
    from .remotes_page import RemotesPage

T = TypeVar("T")


def _size(n: int) -> str:
    if not n:
        return "–"
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.3g} {unit}"
    return f"{n} bytes"


class InstallsPanel:
    def __init__(self, page: RemotesPage) -> None:
        self.page = page
        self.state = page.state
        self._plan: InstallPlan | None = None
        self._upgrade = False
        s = self.state
        s.in_rows = []
        s.in_target = ""
        s.in_variant = "cpu"
        s.in_method = ""
        s.in_plan = {}
        s.in_confirm = ""  # "install" or "remove": the dialog shown
        s.in_remove = ""
        s.in_busy = False
        s.in_phase = ""
        s.in_log = ""
        s.in_error = ""
        s.in_upgrade = ""

    @property
    def remote(self) -> Remote | None:
        return self.page.store.remotes.get(self.state.rm_sel)

    def _save(self) -> None:
        self.page.store.save()

    # Rows

    def show(self, remote: Remote | None) -> None:
        s = self.state
        if remote is None:
            s.in_rows, s.in_upgrade = [], ""
            return
        found = self.page._probes.get(remote.name)
        recorded = {i.path for i in remote.installs}
        missing = set(found.missing) if found else set()
        rows: list[dict[str, Any]] = [
            {
                "path": i.path,
                "module": i.module,
                "version": i.version or "–",
                "variant": i.variant,
                "method": i.method or "recorded",
                "source": "missing" if i.path in missing else (i.method or "recorded"),
                "verified": i.verified.split(" ")[0] if i.verified else "",
                "verified_on": i.verified.partition(" ")[2],
                "summary": i.verify_summary,
                "recorded": True,
                "default": n == 0,
                "removable": bool(i.method) and n != 0,
            }
            for n, i in enumerate(remote.installs)
        ]
        if found:
            rows += [
                {
                    "path": c.path,
                    "module": c.module,
                    "version": c.version or c.text or "–",
                    "variant": "",
                    "method": "",
                    "source": "install.toml" if c.source.endswith("/install.toml") else c.source,
                    "verified": "",
                    "verified_on": "",
                    "summary": "",
                    "recorded": False,
                    "default": False,
                    "removable": False,
                }
                for c in found.candidates
                if c.path not in recorded
            ]
        s.in_rows = rows
        if not s.in_target:
            s.in_target = release_of(self.page.app.state.version or "")
        s.in_upgrade = upgrade_target(remote, s.in_target)

    def use(self, path: str) -> None:
        """Record a discovered install; the first recorded one is the remote's default."""
        remote = self.remote
        found = self.page._probes.get(self.state.rm_sel)
        cand = next((c for c in found.candidates if c.path == path), None) if found else None
        if remote is None or cand is None:
            return
        remote.installs = [i for i in remote.installs if i.path != path] + [cand.install()]
        self._save()
        self.show(remote)

    def make_default(self, path: str) -> None:
        if remote := self.remote:
            make_default(remote, path)
            self._save()
            self.show(remote)

    def forget(self, path: str) -> None:
        if remote := self.remote:
            remote.installs = [i for i in remote.installs if i.path != path]
            self._save()
            self.show(remote)

    def set_target(self, version: str) -> None:
        self.state.in_target = version.strip()
        if remote := self.remote:
            self.state.in_upgrade = upgrade_target(remote, self.state.in_target)

    # Install, upgrade

    def prepare(self, form: dict[str, str] | None = None, upgrade: bool = False) -> None:
        """Choose what to install and open the confirmation; nothing runs on the remote yet."""
        s = self.state
        if not s.rm_sel or s.in_busy:
            return
        form = form or {}
        version = str(form.get("version") or s.in_target).strip()
        variant = str(form.get("variant") or s.in_variant)
        method = str(form.get("method") if "method" in form else s.in_method)
        if upgrade:
            version = s.in_upgrade or version
        s.in_target, s.in_variant, s.in_method = version, variant, method
        self._upgrade = upgrade
        self.page._schedule(self._prepare(s.rm_sel, version, variant, method))

    async def _prepare(self, name: str, version: str, variant: str, method: str) -> None:
        remote = self.page.store.remotes[name]
        with self.state:
            self.state.in_busy, self.state.in_error, self.state.in_phase = True, "", "Choosing what to install"
        error = ""
        try:
            if not version:
                raise InstallError("name the Mallard version to install")
            found = self.page._probes.get(name)
            if found is None or not found.ok:
                found = await asyncio.to_thread(probe, self.page._connect(remote), [i.path for i in remote.installs])
                self.page._probes[name] = found
                if not found.ok:
                    raise InstallError(found.error)
            manifest = await asyncio.to_thread(fetch_manifest, version)
            root = remote.storage.get("install_root", "")
            self._plan = plan(found, manifest, variant, method, root)
        except (InstallError, RemoteError, OSError, ValueError) as e:
            error = str(e)
        with self.state:
            self.state.in_busy, self.state.in_phase = False, ""
            if error:
                self.state.in_error = error
                return
            assert self._plan is not None
            p = self._plan
            self.state.in_plan = {
                **p.to_dict(),
                "remote": name,
                "size_text": _size(p.size),
                "check": ("SHA-256 " if p.method == "archive" else "digest ") + (p.sha256 or "–"),
                "upgrade": self._upgrade,
            }
            self.state.in_confirm = "install"

    def cancel(self) -> None:
        self.state.in_confirm = ""
        self.state.in_remove = ""

    def confirm(self) -> None:
        s = self.state
        kind, s.in_confirm = s.in_confirm, ""
        if kind == "install" and self._plan is not None and s.rm_sel:
            self.page._schedule(self._install(s.rm_sel, self._plan, self._upgrade))
        elif kind == "remove" and s.in_remove and s.rm_sel:
            self.page._schedule(self._remove(s.rm_sel, s.in_remove))

    async def _stream(self, phase: str, fn: Callable[[Callable[[str], None]], T]) -> T:
        """Run `fn(on_line)` in a thread, showing its output as it comes."""
        lines: list[str] = []
        with self.state:
            self.state.in_busy, self.state.in_phase, self.state.in_log, self.state.in_error = True, phase, "", ""
        task = asyncio.ensure_future(asyncio.to_thread(fn, lines.append))
        shown = 0
        while not task.done():
            await asyncio.wait([task], timeout=0.3)
            if len(lines) != shown:
                shown = len(lines)
                with self.state:
                    self.state.in_log = "\n".join(lines[-400:])
        with self.state:
            self.state.in_log = "\n".join(lines[-400:])
        return task.result()

    async def _install(self, name: str, p: InstallPlan, upgrade: bool) -> None:
        remote = self.page.store.remotes[name]
        conn = self.page._connect(remote)
        try:
            out: Outcome = await self._stream(
                f"Installing Mallard {p.version} ({p.method}) into {p.directory}",
                lambda on_line: run_install(conn, p, on_line),
            )
        except (RemoteError, OSError) as e:
            out = Outcome(False, error=str(e))
        if not out.ok or out.install is None:
            self._finish(f"install failed: {out.error}")
            return
        remote.installs = [i for i in remote.installs if i.path != out.install.path] + [out.install]
        self._save()
        with self.state:
            self.show(remote)
        await self._verify(name, out.install, then_default=upgrade)

    def verify(self, path: str) -> None:
        remote = self.remote
        install = next((i for i in remote.installs if i.path == path), None) if remote else None
        if install is not None and not self.state.in_busy:
            self.page._schedule(self._verify(self.state.rm_sel, install))

    async def _verify(self, name: str, install: Install, then_default: bool = False) -> None:
        remote = self.page.store.remotes[name]
        conn = self.page._connect(remote)
        with self.state:
            self.state.in_busy, self.state.in_phase = True, f"Verifying {install.path} (mallard install verify)"
        result = await asyncio.to_thread(verify, conn, remote, install)
        if then_default and result.passed:
            make_default(remote, install.path)
        self._save()
        message = f"{install_directory(install)}: {result.summary}"
        if then_default:
            message += "; now the default" if result.passed else "; the default is unchanged"
        with self.state:
            self.state.in_log = (self.state.in_log + "\n" if self.state.in_log else "") + message
            self.show(remote)
        self._finish("" if result.passed else f"verification failed: {result.summary}")

    def _finish(self, error: str) -> None:
        with self.state:
            self.state.in_busy, self.state.in_phase, self.state.in_error = False, "", error

    # Removal

    def ask_remove(self, path: str) -> None:
        if not self.state.in_busy:
            self.state.in_remove = path
            self.state.in_plan = {"remove_dir": path.removesuffix("/bin/mallard"), "remote": self.state.rm_sel}
            self.state.in_confirm = "remove"

    async def _remove(self, name: str, path: str) -> None:
        remote = self.page.store.remotes[name]
        install = next((i for i in remote.installs if i.path == path), None)
        if install is None:
            return
        with self.state:
            self.state.in_busy, self.state.in_phase = True, f"Removing {install_directory(install)}"
        error = ""
        try:
            await asyncio.to_thread(remove, self.page._connect(remote), install)
            remote.installs = [i for i in remote.installs if i.path != path]
            self._save()
        except (InstallError, RemoteError, OSError) as e:
            error = str(e)
        with self.state:
            self.state.in_log = f"removed {install_directory(install)}" if not error else self.state.in_log
            self.state.in_remove = ""
            self.show(remote)
        self._finish(error)

    # Layout

    def layout(self) -> None:
        with html.Div(classes="panel-head", v_if="!rm_new"):
            html.Span("Mallard installs")
            html.Span("side by side · the first is the default", classes="mono")
        with html.Table(classes="ftable installs", v_if="!rm_new && in_rows.length"):
            with html.Thead(), html.Tr():
                html.Th("path")
                html.Th("version")
                html.Th("how")
                html.Th("verified")
                html.Th("")
            with html.Tbody():
                with html.Tr(v_for="i in in_rows", key="i.path"):
                    with html.Td(classes="mono path", title=("i.path",)):
                        with html.Div(classes="in-path"):
                            html.Span(
                                "\u200e{{ i.path }}\u200e", classes="in-path-text"
                            )  # LRM: a path stays left to right
                            html.Span("default", classes="tag", v_if="i.default")
                        html.Div("{{ i.module }}", classes="muted", v_if="i.module", style="font-size: 10px")
                    html.Td("{{ i.version }}{{ i.variant ? ' ' + i.variant : '' }}", classes="mono")
                    html.Td("{{ i.source }}", classes=("['mono', i.source === 'missing' ? 'err' : 'muted']",))
                    with html.Td(classes="mono verified", title=("i.summary",)):
                        html.Span(
                            "{{ i.verified ? i.verified + ' ' + i.verified_on : (i.recorded ? 'not yet' : '') }}",
                            classes=("i.verified === 'passed' ? 'okc' : i.verified === 'failed' ? 'err' : 'muted'",),
                        )
                    with html.Td(classes="row-actions"):
                        html.Button(
                            "Use",
                            classes="tb",
                            v_if="!i.recorded",
                            click=(self.use, "[i.path]"),
                            title="Record this install for the remote",
                        )
                        html.Button(
                            "Verify",
                            classes="tb",
                            v_if="i.recorded && rm_backend !== 'k8s'",
                            disabled=("in_busy",),
                            click=(self.verify, "[i.path]"),
                            title="mallard install verify, as a short job: the environment and a quick run",
                        )
                        html.Button(
                            "Default",
                            classes="tb",
                            v_if="i.recorded && !i.default",
                            click=(self.make_default, "[i.path]"),
                        )
                        html.Button(
                            v_html="icons.trash",
                            classes="icon-btn",
                            v_if="i.removable",
                            disabled=("in_busy",),
                            click=(self.ask_remove, "[i.path]"),
                            title="Remove this install: deletes its directory on the remote",
                        )
                        html.Button(
                            v_html="icons.x",
                            classes="icon-btn",
                            v_if="i.recorded && !i.removable",
                            click=(self.forget, "[i.path]"),
                            title="Forget this install (nothing is deleted on the remote)",
                        )
        html.Div(
            "No install recorded. Test connection looks for mallard on PATH, in modules and under ~/mallard/*/; "
            "or install one below.",
            classes="notice",
            v_if="!rm_new && !in_rows.length && rm_backend !== 'k8s'",
        )
        # A Kubernetes remote's install is the image it names; installs and verification go over SSH
        with html.Div(classes="panel-head", v_if="!rm_new && rm_backend !== 'k8s'"):
            html.Span("Install Mallard")
            html.Span("archive, else Apptainer, else Spack", classes="mono")
        with html.Div(classes="install-form", v_if="!rm_new && rm_backend !== 'k8s'"):
            with html.Div(classes="prow"):
                html.Div("version", classes="plabel")
                with html.Div(classes="pval"):
                    html.Input(
                        v_model=("in_target",),
                        aria_label="install version",
                        placeholder="0.12.0",
                        classes="mono",
                        change=(self.set_target, "[$event.target.value]"),
                    )
            with html.Div(classes="prow"):
                html.Div("variant", classes="plabel")
                with html.Div(classes="pval"), html.Select(v_model=("in_variant",), aria_label="install variant"):
                    for v in VARIANTS:
                        html.Option(v, value=v)
            with html.Div(classes="prow"):
                html.Div("method", classes="plabel")
                with html.Div(classes="pval"), html.Select(v_model=("in_method",), aria_label="install method"):
                    html.Option("automatic", value="")
                    for m in METHODS:
                        html.Option(m, value=m)
            with html.Div(classes="actions"):
                html.Span("{{ in_phase }}", classes="muted phase", v_if="in_phase")
                html.Div(classes="grow")
                with html.Button(
                    classes="btn",
                    v_if="in_upgrade",
                    disabled=("in_busy",),
                    click=(self.prepare, "[{}, true]"),
                    title="Install the new version beside the default, verify it, then make it the default",
                ):
                    html.Span(v_html="icons.refresh")
                    html.Span("Upgrade to {{ in_upgrade }}")
                with html.Button(
                    classes="btn primary",
                    disabled=("in_busy || !in_target",),
                    click=(self.prepare, "[{version: in_target, variant: in_variant, method: in_method}]"),
                    title="Choose what to install and show it; nothing runs until you confirm",
                ):
                    html.Span(v_html="icons.download")
                    html.Span("Install ...")
            html.Div("{{ in_error }}", classes="prop-foot err", v_if="in_error")
            html.Pre("{{ in_log }}", classes="log install-log", v_if="in_log")
        self._dialog()

    def _dialog(self) -> None:
        with html.Div(classes="modal-scrim", v_if="in_confirm"):
            with html.Div(classes="modal", role="dialog", aria_modal="true", aria_label="Confirm"):
                with html.Div(v_if="in_confirm === 'install'", style="display: contents"):
                    with html.Div(classes="modal-head"):
                        html.Span(v_html="icons.download")
                        html.Span(
                            "{{ in_plan.upgrade ? 'Upgrade' : 'Install' }} Mallard {{ in_plan.version }} "
                            "on {{ in_plan.remote }}"
                        )
                    with html.Div(classes="modal-body"):
                        for label, expr in (
                            ("method", "in_plan.method + (in_plan.job ? ' (a batch job)' : '')"),
                            ("into", "in_plan.directory"),
                            ("artifact", "in_plan.artifact"),
                            ("checked by", "in_plan.check"),
                            ("download", "in_plan.size_text"),
                        ):
                            with html.Div(classes="prow"):
                                html.Div(label, classes="plabel")
                                html.Div(f"{{{{ {expr} }}}}", classes="pval ro", title=(expr,))
                        html.Div("{{ in_plan.note }}.", classes="notice")
                        html.Div(
                            "Passed over: {{ in_plan.skipped.join('; ') }}.",
                            classes="notice",
                            v_if="in_plan.skipped && in_plan.skipped.length",
                        )
                        html.Div(
                            "{{ in_plan.upgrade ? 'Then mallard install verify runs as a short job; the new install "
                            "becomes the default only if it passes. The old one stays.' : 'Then mallard install "
                            "verify runs as a short job. Installs never overwrite one another.' }}",
                            classes="notice",
                        )
                        html.Pre("{{ in_plan.script }}", classes="log modal-script")
                    with html.Div(classes="actions"):
                        html.Span("runs on {{ in_plan.remote }}'s login node", classes="muted", v_if="!in_plan.job")
                        html.Div(classes="grow")
                        with html.Button(classes="btn", click=self.cancel):
                            html.Span("Cancel")
                        with html.Button(classes="btn primary", click=self.confirm):
                            html.Span(v_html="icons.check")
                            html.Span("{{ in_plan.upgrade ? 'Upgrade' : 'Install' }}")
                with html.Div(v_if="in_confirm === 'remove'", style="display: contents"):
                    with html.Div(classes="modal-head"):
                        html.Span(v_html="icons.trash")
                        html.Span("Remove an install from {{ in_plan.remote }}")
                    with html.Div(classes="modal-body"):
                        html.Div(
                            "Deletes {{ in_plan.remove_dir }} on {{ in_plan.remote }}. Runs that used it keep "
                            "their record but cannot continue on it. This cannot be undone.",
                            classes="notice",
                        )
                    with html.Div(classes="actions"):
                        html.Div(classes="grow")
                        with html.Button(classes="btn", click=self.cancel):
                            html.Span("Cancel")
                        with html.Button(classes="btn danger", click=self.confirm):
                            html.Span(v_html="icons.trash")
                            html.Span("Remove")
