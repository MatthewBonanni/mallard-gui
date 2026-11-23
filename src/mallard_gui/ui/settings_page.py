"""The Settings page: settings.toml (settings.py) edited in place, each change checked and saved at once, as a
platform's preferences window does: theme, units, run defaults, notifications, the mallard executables per
dimension, and the recent inputs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from .. import recent
from ..executables import Executables
from ..settings import LEVELS, THEMES, Settings, SettingsError
from ..units import CHOICES, QUANTITIES
from .copy import message

if TYPE_CHECKING:
    from .app import App

THEME_LABELS = {"system": "System", "light": "Light", "dark": "Dark"}
LEVEL_LABELS = {"warning": "Warnings and critical alerts", "critical": "Critical alerts only"}
DIM_LABELS = {"front": "mallard", "2": "2D", "3": "3D"}
EXE_KEYS = {"front": "path", "2": "path_2d", "3": "path_3d"}


def _home(path: str | Path) -> str:
    return str(path).replace(str(Path.home()), "~", 1)


class SettingsPage:
    def __init__(self, app: App, settings: Settings | None = None) -> None:
        self.app = app
        self.state = app.state
        self.settings = settings or Settings()
        s = self.state
        s.set_values = self.settings.as_dict()
        s.set_errors = {}
        s.set_file = _home(self.settings.path)
        s.set_file_error = self.settings.error
        s.set_quantities = [
            {"name": name, "units": list(CHOICES[dim])} for name, dim in QUANTITIES.items() if dim in CHOICES
        ]
        s.set_themes = [{"id": t, "label": THEME_LABELS[t]} for t in THEMES]
        s.set_levels = [{"id": lv, "label": LEVEL_LABELS[lv]} for lv in LEVELS]
        s.set_remotes = []
        s.set_exes = []
        s.set_recent = []
        self.apply_all()

    # Applying settings to the running GUI

    def apply_all(self) -> None:
        ses = self.app.session
        ses.preferred_units = self.settings.preferred_units()
        self._apply_executables()
        self.state.theme_pref = self.settings.theme
        self.apply_notifications()

    def apply_notifications(self) -> None:
        n = self.settings.section("notifications")
        health = getattr(self.app, "health", None)
        if health is not None:
            health.settings.default = {**health.settings.DEFAULT, "notify": bool(n["enabled"])}
            health.level, health.notify_end = str(n["level"]), bool(n["finished"])

    def _apply_executables(self) -> None:
        ses = self.app.session
        if ses.executables is None:
            return
        paths = self.settings.executables()
        front = Path(paths["front"]) if paths["front"] else ses.executables.front
        by_dim = {int(d): Path(paths[d]) for d in ("2", "3") if paths[d]}
        ses.executables = Executables(front, by_dim, ses.executables.info)

    def initial_theme(self, system_dark: bool) -> str:
        theme = self.settings.theme
        return ("dark" if system_dark else "light") if theme == "system" else theme

    # Actions

    def open(self) -> None:
        s, ses = self.state, self.app.session
        s.set_values = self.settings.as_dict()
        try:
            names = sorted(ses.data.remotes.remotes)
        except (OSError, ValueError):
            names = []
        s.set_remotes = [{"id": "", "label": "None"}] + [{"id": n, "label": n} for n in names]
        self._refresh_exes()
        s.set_recent = [{"path": str(p), "label": _home(p)} for p in recent.load()]

    def _refresh_exes(self) -> None:
        ses = self.app.session
        paths = self.settings.executables()
        in_use = str(ses.mallard.executable)
        used = str(ses.dim) if ses.dim and paths[str(ses.dim)] else "front"
        rows = []
        for key in ("front", "2", "3"):
            front = ses.executables.front if ses.executables else ses.mallard.executable
            default = str(front) if key == "front" else ""
            rows.append(
                {
                    "key": key,
                    "label": DIM_LABELS[key],
                    "path": paths[key],
                    "placeholder": _home(default) if default else "the front end",
                    "used": key == used,
                }
            )
        self.state.set_exes = rows
        self.state.set_case = (
            f"This case is {ses.dim}D: it runs {_home(in_use)}" if ses.dim else f"It runs {_home(in_use)}"
        )

    def change(self, section: str, key: str, value: Any) -> None:
        s = self.state
        field = f"{section}.{key}"
        errors = {k: v for k, v in s.set_errors.items() if k != field}
        try:
            self.settings.set(section, key, value)
        except SettingsError as e:
            s.set_errors = {**errors, field: str(e)}
            return
        except OSError as e:
            s.set_errors = {**errors, field: str(e)}
            return
        s.set_errors = errors
        s.set_values = self.settings.as_dict()
        self._side_effect(section, key)

    def _side_effect(self, section: str, key: str) -> None:
        app, ses = self.app, self.app.session
        if section == "appearance":
            self.state.theme_pref = self.settings.theme  # the page follows the system's (app.py) while "system"
            if self.settings.theme != "system":
                app.set_theme(self.settings.theme)
        elif section == "units":
            ses.preferred_units = self.settings.preferred_units()
            app._refresh_case()
        elif section == "notifications":
            self.apply_notifications()
        elif section == "runs" and key == "threads":
            ses.mallard.threads = ses.backend.threads = self.settings.threads
        elif section == "mallard":
            self._apply_executables()
            if ses.select_mallard():
                app._schedule_check()
            self._refresh_exes()

    def choose_executable(self, dim: str) -> None:
        current = self.settings.executables()[dim]
        if current:
            path = Path(current).expanduser()
        else:
            front = self.app.session.executables.front if self.app.session.executables else None
            path = front or Path.home() / "mallard"
        directory = path.parent if path.parent.is_dir() else Path.home()
        self.app.paths.choose(
            "executable", directory, path.name, lambda p: self.change("mallard", EXE_KEYS[dim], str(p))
        )

    def clear_recent(self) -> None:
        recent.clear()
        self.state.set_recent = []

    # Layout

    def layout(self) -> None:
        with html.Div(classes="settings scroll"):
            with html.Div(classes="settings-col"):
                html.H1("Settings", classes="settings-title")
                message("set_file_error", "settings-banner err", v_if="set_file_error", role="alert")
                self._section_appearance()
                self._section_units()
                self._section_runs()
                self._section_notifications()
                self._section_mallard()
                self._section_recent()
                with html.Div(classes="settings-foot mono muted"):
                    html.Span("Saved as you change them, in {{ set_file }}")

    def _error(self, field: str) -> None:
        html.Div(f"{{{{ set_errors['{field}'] }}}}", classes="set-error", v_if=f"set_errors['{field}']")

    def _section_appearance(self) -> None:
        with html.Section(classes="set-sec", aria_label="Appearance"):
            html.H2("Appearance", classes="set-h")
            with html.Div(classes="set-row"):
                html.Span("Theme", classes="set-label", id="set-theme-label")
                with html.Div(classes="segmented", role="radiogroup", aria_labelledby="set-theme-label"):
                    html.Button(
                        "{{ t.label }}",
                        v_for="t in set_themes",
                        key="t.id",
                        role="radio",
                        aria_checked=("String(set_values.appearance.theme === t.id)",),
                        classes=("['seg', {on: set_values.appearance.theme === t.id}]",),
                        click=(self.change, "['appearance', 'theme', t.id]"),
                    )
            with html.Div(classes="set-row"):
                html.Span("Panes", classes="set-label")
                html.Button(
                    "Reset sizes",
                    classes="btn",
                    disabled=("!Object.keys(pane_layout).length",),
                    click="pane_layout = {}",
                    style="justify-self: start",
                )
                html.Span("every page's panes at their default sizes, hidden ones shown", classes="set-hint")

    def _section_units(self) -> None:
        with html.Section(classes="set-sec", aria_label="Units"):
            html.H2("Units", classes="set-h")
            html.P(
                "The unit an empty field starts in. A value keeps the unit it is written in; typing a unit "
                "(1 cm) always sets it.",
                classes="set-note",
            )
            with html.Div(v_for="q in set_quantities", key="q.name", classes="set-row"):
                html.Label("{{ q.name }}", classes="set-label", for_=("'set-unit-' + q.name",))
                with html.Select(
                    id=("'set-unit-' + q.name",),
                    classes="set-select mono",
                    aria_label=("q.name",),
                    value=("set_values.units[q.name] || ''",),
                    change=(self.change, "['units', q.name, $event.target.value]"),
                ):
                    html.Option("{{ q.units[0] }} (SI)", value="")
                    html.Option("{{ u }}", v_for="u in q.units.slice(1)", key="u", value=("u",))
                html.Div(
                    "{{ set_errors['units.' + q.name] }}", classes="set-error", v_if="set_errors['units.' + q.name]"
                )

    def _section_runs(self) -> None:
        with html.Section(classes="set-sec", aria_label="Runs"):
            html.H2("Runs", classes="set-h")
            with html.Div(classes="set-row"):
                html.Label("Threads", classes="set-label", for_="set-threads")
                html.Input(
                    id="set-threads",
                    type="number",
                    min="1",
                    classes="set-input mono narrow",
                    aria_label="threads",
                    value=("set_values.runs.threads",),
                    change=(self.change, "['runs', 'threads', $event.target.value]"),
                )
                html.Span("of local mallard commands and runs", classes="set-hint")
                self._error("runs.threads")
            with html.Div(classes="set-row"):
                html.Label("Copy runs to", classes="set-label", for_="set-remote")
                with html.Select(
                    id="set-remote",
                    classes="set-select",
                    aria_label="default remote",
                    value=("set_values.runs.remote",),
                    change=(self.change, "['runs', 'remote', $event.target.value]"),
                ):
                    html.Option("{{ r.label }}", v_for="r in set_remotes", key="r.id", value=("r.id",))
                html.Span("the destination Data proposes", classes="set-hint")

    def _section_notifications(self) -> None:
        with html.Section(classes="set-sec", aria_label="Notifications"):
            html.H2("Notifications", classes="set-h")
            with html.Div(classes="set-row"):
                html.Span("Notify", classes="set-label")
                with html.Label(classes="set-check"):
                    html.Input(
                        type="checkbox",
                        aria_label="notify by default",
                        checked=("set_values.notifications.enabled",),
                        change=(self.change, "['notifications', 'enabled', $event.target.checked]"),
                    )
                    html.Span("Desktop notifications for runs that have no setting of their own")
            with html.Div(classes="set-row"):
                html.Label("Alerts", classes="set-label", for_="set-level")
                with html.Select(
                    id="set-level",
                    classes="set-select",
                    aria_label="alert level",
                    value=("set_values.notifications.level",),
                    change=(self.change, "['notifications', 'level', $event.target.value]"),
                ):
                    html.Option("{{ lv.label }}", v_for="lv in set_levels", key="lv.id", value=("lv.id",))
            with html.Div(classes="set-row"):
                html.Span("", classes="set-label")
                with html.Label(classes="set-check"):
                    html.Input(
                        type="checkbox",
                        aria_label="notify when a run ends",
                        checked=("set_values.notifications.finished",),
                        change=(self.change, "['notifications', 'finished', $event.target.checked]"),
                    )
                    html.Span("When a run ends")

    def _section_mallard(self) -> None:
        with html.Section(classes="set-sec", aria_label="Mallard"):
            html.H2("Mallard", classes="set-h")
            html.P(
                "The mallard front end runs mallard2d or mallard3d by the case's mesh. Name a dimension's own "
                "executable (a build tree's src/mallard) to run that dimension's cases with it.",
                classes="set-note",
            )
            with html.Div(v_for="x in set_exes", key="x.key", classes="set-row"):
                html.Label("{{ x.label }}", classes="set-label", for_=("'set-exe-' + x.key",))
                with html.Div(classes="set-path"):
                    html.Input(
                        id=("'set-exe-' + x.key",),
                        classes="set-input mono",
                        spellcheck="false",
                        aria_label=("'mallard ' + x.label",),
                        value=("x.path",),
                        placeholder=("x.placeholder",),
                        change=(
                            self.change,
                            "['mallard', x.key === 'front' ? 'path' : 'path_' + x.key + 'd', $event.target.value]",
                        ),
                    )
                    html.Button(
                        "Choose…",
                        classes="btn",
                        title=("'Choose the ' + (x.key === 'front' ? 'mallard front end' : x.label + ' executable')",),
                        aria_label=("'choose mallard ' + x.label",),
                        click=(self.choose_executable, "[x.key]"),
                    )
                    html.Span("in use", classes="pill", v_if="x.used")
                html.Div(
                    "{{ set_errors['mallard.' + (x.key === 'front' ? 'path' : 'path_' + x.key + 'd')] }}",
                    classes="set-error",
                    v_if="set_errors['mallard.' + (x.key === 'front' ? 'path' : 'path_' + x.key + 'd')]",
                )
            html.Div("{{ set_case }}", classes="set-note mono")

    def _section_recent(self) -> None:
        with html.Section(classes="set-sec", aria_label="Recent projects"):
            with html.Div(classes="set-h-row"):
                html.H2("Recent projects", classes="set-h")
                html.Button(
                    "Clear",
                    classes="btn small",
                    click=self.clear_recent,
                    disabled=("!set_recent.length",),
                )
            with html.Div(classes="set-recent"):
                html.Div(
                    "{{ r.label }}",
                    v_for="r in set_recent",
                    key="r.path",
                    classes="set-recent-row mono",
                    title=("r.path",),
                )
                html.Div("None yet.", classes="set-note", v_if="!set_recent.length")
