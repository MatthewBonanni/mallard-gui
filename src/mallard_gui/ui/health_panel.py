"""Run health in the run monitor: the alerts of the selected run, in-app toasts, desktop notifications (shown
by the page, see `notify`) and the per-run auto-action (stop with a restart, through the run's control file).

The app calls `observe` after every poll of a live run and `show` when it shows a run; everything else
(state, layout, settings) is this module's.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import client, html

from .. import notify
from ..config import cache_dir
from ..health import CRITICAL, INFO, WARNING, Alert, HealthMonitor
from ..runlog import RunState
from ..tomlio import dumps_sorted, read_toml, write_atomic
from .copy import COPY_PROPS, DOCS_BASE, copy_button

if TYPE_CHECKING:
    from .app import App

TOAST_SECONDS = 8.0
MAX_TOASTS = 4
AUTO_ACTIONS = (
    ("off", "Off"),
    ("critical", "Stop with a restart on a critical alert"),
    ("warning", "Stop with a restart on any alert"),
)
_RANK = {INFO: 0, WARNING: 1, CRITICAL: 2}


class HealthSettings:
    """Per-run notification and auto-action settings, per machine (the GUI acts on them only while it runs),
    in `<cache>/health.toml` keyed by the run's directory. A run without its own takes the last ones set in
    its project, so a setting chosen once holds for the runs that follow."""

    DEFAULT: dict[str, Any] = {"auto": "off", "notify": True}  # noqa: RUF012

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_dir() / "health.toml"
        self.default = dict(self.DEFAULT)  # Settings' Notify, for runs without their own
        try:
            self.data: dict[str, Any] = dict(read_toml(self.path).get("runs", {}))
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> dict[str, Any]:
        project = str(Path(key).parent.parent)
        return {**self.default, **self.data.get(key, self.data.get(project, {}))}

    def set(self, key: str, **values: Any) -> dict[str, Any]:
        entry = self.get(key) | values
        self.data[key] = self.data[str(Path(key).parent.parent)] = entry
        write_atomic(self.path, dumps_sorted({"runs": self.data}))
        return entry


class HealthPanel:
    def __init__(self, app: App, settings: HealthSettings | None = None) -> None:
        self.app = app
        self.state = app.state
        self.settings = settings or HealthSettings()
        self.monitors: dict[str, HealthMonitor] = {}
        self._acted: set[str] = set()
        self._ids = itertools.count(1)
        self.level = "warning"  # Settings: the least severe alert notified
        self.notify_end = True  # Settings: a notification when a run ends
        s = self.state
        s.h_alerts = []
        s.h_worst = ""
        s.h_toasts = []
        s.h_native = {}
        s.h_settings = dict(HealthSettings.DEFAULT)
        s.h_actions = [{"id": k, "label": v} for k, v in AUTO_ACTIONS]
        s.h_route = notify.NONE
        s.h_perm = ""
        app.server.enable_module({"styles": ["mallard_gui/health.css"]})

    def _key(self, name: str) -> str:
        record = self.app.session.project.run(name)
        return str(self.app.session.project.root / record.home.get("directory", f"runs/{name}"))

    def monitor(self, name: str) -> HealthMonitor:
        if name not in self.monitors:
            self.monitors[name] = HealthMonitor()
        return self.monitors[name]

    # Called by the app

    def observe(self, name: str, st: RunState, running: bool) -> list[Alert]:
        """After a poll of a live run: new alerts become toasts, notifications and, if set, a stop."""
        new = self.monitor(name).update(st, time.monotonic(), running)
        if not new:
            return new
        settings = self.settings.get(self._key(name))
        for alert in new:
            self.toast(f"{name}: {alert.title}", alert.text, alert.severity)
            if settings["notify"] and self._notified(alert):
                self.notify(f"Mallard · {name}", alert.text or alert.title, alert.title)
        self._auto(name, new, settings, running)
        return new

    def _notified(self, alert: Alert) -> bool:
        if alert.kind == "end":
            return self.notify_end
        return _RANK[alert.severity] >= _RANK[CRITICAL if self.level == "critical" else WARNING]

    def show(self, name: str, st: RunState) -> None:
        """The alerts of the run shown; a run not followed live is read through once, without notifying."""
        live = name in self.app.session.active and self.app.session.active[name].state.active
        mon = self.monitor(name)
        if not live:
            mon.update(st, time.monotonic(), running=False)
        s = self.state
        s.h_alerts = [
            a.to_dict() | {"where": _where(a)} for a in sorted(mon.alerts, key=lambda a: -_RANK.get(a.severity, 0))
        ]
        s.h_worst = mon.worst if any(a.severity != INFO for a in mon.alerts) else ""
        s.h_settings = self.settings.get(self._key(name))

    # Actions

    def _auto(self, name: str, new: list[Alert], settings: dict[str, Any], running: bool) -> None:
        threshold = {"critical": CRITICAL, "warning": WARNING}.get(str(settings["auto"]))
        if threshold is None or not running or name in self._acted:
            return
        trigger = next((a for a in new if _RANK.get(a.severity, 0) >= _RANK[threshold] and a.kind != "end"), None)
        if trigger is None:
            return
        self._acted.add(name)
        self.app.session.stop_run(name)  # the STOP control file: a restart and the final output, then exit
        text = f"stop requested after “{trigger.title}”: a restart and the final output, then exit"
        if name == self.state.run_sel:
            self.state.run = {**self.state.run, "requested": f"auto: {text}"}
        self.toast(f"{name}: auto-action", text, WARNING)
        if settings["notify"]:
            self.notify(f"Mallard · {name}", text, "Stopping with a restart")

    def set_auto(self, value: str) -> None:
        name = self.state.run_sel
        if name and value in dict(AUTO_ACTIONS):
            self.state.h_settings = self.settings.set(self._key(name), auto=value)
            self._acted.discard(name)

    def set_notify(self, value: bool) -> None:
        name = self.state.run_sel
        if name:
            self.state.h_settings = self.settings.set(self._key(name), notify=bool(value))

    def set_channel(self, detected: str) -> None:
        self.state.h_route, self.state.h_perm = notify.parse(detected)

    def notify(self, title: str, body: str, subtitle: str = "") -> None:
        """Queues a notification for the page to show; with no channel the toast is all there is."""
        if self.state.h_route != notify.NONE:
            body = f"{subtitle}\n{body}".strip()
            self.state.h_native = {"id": next(self._ids), "title": title, "body": body, "icon": notify.ICON}

    def toast(self, title: str, text: str, severity: str = INFO, path: str = "") -> None:
        tid = next(self._ids)
        toasts = [*self.state.h_toasts, {"id": tid, "title": title, "text": text, "severity": severity, "path": path}]
        self.state.h_toasts = toasts[-MAX_TOASTS:]
        with contextlib.suppress(RuntimeError):  # without a loop (tests) a toast stays until dismissed
            asyncio.get_running_loop().call_later(TOAST_SECONDS, self._expire, tid)

    def _expire(self, tid: int) -> None:
        with self.state:
            self.dismiss(tid)

    def dismiss(self, tid: int) -> None:
        self.state.h_toasts = [t for t in self.state.h_toasts if t["id"] != int(tid)]

    def reveal(self, path: str) -> None:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Layout

    def layout_client(self) -> None:
        """Once, at the top of the page: the notification channel it has, and the delivery of each one."""
        client.ClientTriggers(mounted=(self.set_channel, f"[{notify.DETECT}]"))
        client.ClientStateChange(value="h_native", change=notify.DELIVER)

    def layout_monitor(self) -> None:
        """The run's health, between its progress and its numbers."""
        with html.Div(classes=("['health', h_worst]",), v_if="run_sel"):
            with html.Div(classes="health-head"):
                html.Span(v_html="icons.bell")
                html.Span("Health", classes="health-title")
                html.Span(
                    "{{ h_alerts.filter(a => a.severity !== 'info').length ? "
                    "h_alerts.filter(a => a.severity !== 'info').length + ' alert' + "
                    "(h_alerts.filter(a => a.severity !== 'info').length === 1 ? '' : 's') : 'no warnings' }}",
                    classes="muted health-count",
                )
                html.Div(classes="grow")
                with html.Label(
                    classes="chip",
                    title=(
                        "h_route === 'none' ? 'No desktop notifications here: alerts show as toasts' : "
                        "h_perm === 'denied' ? 'Notifications are blocked for this page in the browser settings' : "
                        "'Desktop notifications for this run\\'s alerts and its end'",
                    ),
                ):
                    html.Input(
                        type="checkbox",
                        checked=(
                            "h_settings.notify && (h_route === 'shell' || h_route === 'tauri' || "
                            "(h_route === 'web' && h_perm === 'granted'))",
                        ),
                        disabled=("h_route === 'none' || h_perm === 'denied'",),
                        click=notify.ASK,
                        change=(self.set_notify, "[$event.target.checked]"),
                        aria_label="notify",
                    )
                    html.Span("Notify")
                html.Span("on alert", classes="muted", style="font-size: 11px")
                with html.Select(
                    aria_label="auto-action",
                    change=(self.set_auto, "[$event.target.value]"),
                    title="Auto-action, while this GUI runs: stop through the run's STOP control file",
                ):
                    html.Option(
                        "{{ a.label }}",
                        v_for="a in h_actions",
                        key="a.id",
                        value=("a.id",),
                        selected=("a.id === h_settings.auto",),
                    )
            with html.Div(classes="health-list", v_if="h_alerts.length"):
                with html.Div(
                    v_for="a in h_alerts",
                    key="a.key",
                    classes=("['alert', a.severity]",),
                    tabindex="0",
                    __properties=COPY_PROPS,
                    data_copy=(
                        "[a.severity + (a.code ? ' ' + a.code : '') + ': ' + a.title, a.text, a.where && 'at: ' + "
                        f"a.where, a.docs && 'docs: {DOCS_BASE}' + a.docs].filter(Boolean).join('\\n')",
                    ),
                ):
                    html.Span(
                        v_html="a.severity === 'critical' ? icons.error : a.severity === 'warning' ? "
                        "icons.warning : icons.ok",
                        classes="alert-icon",
                    )
                    html.Span("{{ a.title }}", classes="alert-title")
                    html.Span("{{ a.text }}", classes="alert-text", title=("a.text",))
                    html.Span(
                        "{{ a.source === 'mallard' ? (a.code || 'run log') : 'GUI check' }}",
                        classes="alert-source mono",
                        title=(
                            "a.source === 'mallard' ? 'From the run log (mallard.jsonl)' + "
                            "(a.docs ? ': ' + a.docs : '') : 'A heuristic of this GUI on the run log numbers'",
                        ),
                    )
                    html.Span("{{ a.where }}", classes="alert-where mono")
                    copy_button()

    def layout_toasts(self) -> None:
        with html.Div(classes="toasts", aria_live="polite"):
            with html.Div(
                v_for="t in h_toasts",
                key="t.id",
                classes=("['toast', t.severity]",),
                tabindex="0",
                __properties=COPY_PROPS,
                data_copy=("t.title + (t.text ? '\\n' + t.text : '')",),
            ):
                with html.Div(classes="toast-body"):
                    html.Div("{{ t.title }}", classes="toast-title")
                    html.Div("{{ t.text }}", classes="toast-text", title=("t.text",))
                with html.Div(classes="toast-actions"):
                    copy_button()
                    html.Button(
                        "Reveal",
                        classes="tb",
                        v_if="t.path",
                        click=(self.reveal, "[t.path]"),
                        title=("'Show ' + t.path + ' in the Finder'",),
                    )
                    html.Button(
                        v_html="icons.close", classes="icon-btn", click=(self.dismiss, "[t.id]"), title="Dismiss"
                    )


def _where(a: Alert) -> str:
    parts = []
    if a.step is not None:
        parts.append(f"step {a.step}")
    if a.t is not None:
        parts.append(f"t {a.t:.4g}")
    return " · ".join(parts)
