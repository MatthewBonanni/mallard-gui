"""The user's settings, `settings.toml` in the user config directory (`$MALLARD_GUI_SETTINGS` overrides the
path; the desktop shell sets it so its Open Recent menu reads the same file): theme, units, run defaults,
notifications, the mallard executables, and the recently opened inputs.

    schema = "mallard-gui-settings"
    version = 1
    recent = ["/home/me/cyl/input.toml"]   # newest first

    [appearance]
    theme = "system"        # system, light or dark
    [units]
    length = "mm"           # the unit a field without a value starts in, per quantity
    [runs]
    threads = 2             # of local mallard commands and runs
    remote = "hpc"          # where Data proposes to copy runs
    [notifications]
    enabled = true          # Notify, for runs without their own setting
    level = "warning"       # warning or critical: the least severe alert notified
    finished = true         # a notification when a run ends
    [mallard]
    path = ""               # the front end; empty: --mallard, $MALLARD, beside the GUI, PATH
    path_2d = ""            # mallard2d, for 2D cases; empty: the front end
    path_3d = ""
    [layout]
    case-left = 320         # a pane's size in px, by splitter (panes.js); absent: the page's default
    case-left-collapsed = true

Unknown keys are kept, so an older GUI does not drop what a newer one wrote."""

from __future__ import annotations

import contextlib
import copy
import os
import re
from pathlib import Path
from typing import Any

from .config import config_dir
from .tomlio import dumps_sorted, read_toml, write_atomic
from .units import CHOICES, QUANTITIES

SCHEMA = "mallard-gui-settings"
VERSION = 1
RECENT_LIMIT = 10
THEMES = ("system", "light", "dark")
LEVELS = ("warning", "critical")
PANE = re.compile(r"[a-z][a-z0-9-]{0,47}")
PANE_MAX = 10_000
DEFAULTS: dict[str, dict[str, Any]] = {
    "appearance": {"theme": "system"},
    "units": {},
    "runs": {"threads": 2, "remote": ""},
    "notifications": {"enabled": True, "level": "warning", "finished": True},
    "mallard": {"path": "", "path_2d": "", "path_3d": ""},
}


def settings_file() -> Path:
    override = os.environ.get("MALLARD_GUI_SETTINGS")
    return Path(override) if override else config_dir() / "settings.toml"


def legacy_recent_file() -> Path:
    """The recent inputs before settings.toml: one path per line."""
    override = os.environ.get("MALLARD_GUI_RECENT")
    return Path(override) if override else config_dir() / "recent-projects"


class SettingsError(ValueError):
    pass


class Settings:
    """settings.toml, read once and written on every change. A file that does not parse is never overwritten:
    the GUI runs on the defaults and says why (`error`)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_file()
        self.error = ""
        self.data: dict[str, Any] = {}
        try:
            self.data = read_toml(self.path) if self.path.exists() else {}
        except (OSError, ValueError) as e:
            self.error = f"{self.path}: {e}; using the defaults until it is fixed"
        version = self.data.get("version", VERSION)
        if isinstance(version, int) and version > VERSION:
            self.error = f"{self.path}: version {version} is newer than this GUI reads ({VERSION})"
            self.data = {}
        self._migrate_recent()

    # Values

    def section(self, name: str) -> dict[str, Any]:
        stored = self.data.get(name)
        return {**DEFAULTS[name], **(stored if isinstance(stored, dict) else {})}

    def get(self, name: str, key: str) -> Any:
        return self.section(name).get(key)

    def as_dict(self) -> dict[str, Any]:
        return {name: self.section(name) for name in DEFAULTS}

    def set(self, name: str, key: str, value: Any) -> None:
        """Set one value, checked, and save. Raises SettingsError with the message to show."""
        value = validate(name, key, value)
        table = self.data.setdefault(name, {})
        if not isinstance(table, dict):
            table = self.data[name] = {}
        if (value in ("", None) and name in ("units", "mallard")) or value == DEFAULTS[name].get(key, object()):
            table.pop(key, None)
        else:
            table[key] = value
        self.save()

    @property
    def theme(self) -> str:
        theme = str(self.get("appearance", "theme"))
        return theme if theme in THEMES else "system"

    @property
    def threads(self) -> int:
        t = self.get("runs", "threads")
        return t if isinstance(t, int) and t > 0 else 2

    def preferred_units(self) -> dict[tuple[int, int, int, int, int, int], str]:
        """The unit each quantity's empty fields start in, by dimension."""
        out = {}
        for name, unit in self.section("units").items():
            dim = QUANTITIES.get(name)
            if dim is not None and unit in CHOICES.get(dim, ()):
                out[dim] = str(unit)
        return out

    def executables(self) -> dict[str, str]:
        """The mallard paths set: "front", "2", "3"; empty when not set."""
        m = self.section("mallard")
        return {"front": str(m["path"]), "2": str(m["path_2d"]), "3": str(m["path_3d"])}

    # Pane sizes

    def layout(self) -> dict[str, int | bool]:
        """The panes' sizes and collapsed panes, entries this GUI cannot use left out."""
        stored = self.data.get("layout")
        return pane_layout(stored) if isinstance(stored, dict) else {}

    def set_layout(self, layout: dict[str, Any]) -> None:
        self.data["layout"] = pane_layout(layout)
        self.save()

    # Recent inputs

    def recent(self) -> list[Path]:
        """The recent inputs that still exist, newest first."""
        entries = self.data.get("recent", [])
        if not isinstance(entries, list):
            return []
        return [Path(p) for p in entries if isinstance(p, str) and Path(p).is_file()]

    def add_recent(self, path: Path) -> None:
        path = path.resolve()
        self.data["recent"] = [str(p) for p in [path, *(p for p in self.recent() if p != path)][:RECENT_LIMIT]]
        with contextlib.suppress(OSError):  # a read-only config directory loses the list, not the session
            self.save()

    def clear_recent(self) -> None:
        self.data.pop("recent", None)
        with contextlib.suppress(OSError):
            self.save()

    def _migrate_recent(self) -> None:
        """Move the old recent-projects file into settings.toml, once."""
        legacy = legacy_recent_file()
        if self.error or "recent" in self.data or not legacy.is_file():
            return
        try:
            lines = legacy.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return
        self.data["recent"] = [line.strip() for line in lines if line.strip()][:RECENT_LIMIT]
        try:
            self.save()
        except OSError:
            return
        legacy.unlink(missing_ok=True)

    def save(self) -> None:
        if self.error:
            raise OSError(self.error)
        data = copy.deepcopy(self.data) | {"schema": SCHEMA, "version": VERSION}
        for name in (*DEFAULTS, "layout"):
            if name in data and not data[name]:
                del data[name]
        write_atomic(self.path, dumps_sorted(data, header=("Written by mallard-gui: the user's settings.",)))


def validate(name: str, key: str, value: Any) -> Any:
    if name not in DEFAULTS or (name != "units" and key not in DEFAULTS[name]):
        raise SettingsError(f"no setting {name}.{key}")
    if name == "appearance":
        if value not in THEMES:
            raise SettingsError(f"theme is one of {', '.join(THEMES)}")
        return value
    if name == "units":
        dim = QUANTITIES.get(key)
        if dim is None:
            raise SettingsError(f"no quantity {key!r}")
        if value and value not in CHOICES.get(dim, ()):
            raise SettingsError(f"{value} is not a unit of {key}")
        return value
    if name == "runs" and key == "threads":
        try:
            threads = int(value)
        except (TypeError, ValueError):
            raise SettingsError("threads is a whole number") from None
        if threads < 1:
            raise SettingsError("threads is at least 1")
        return threads
    if name == "notifications":
        if key == "level":
            if value not in LEVELS:
                raise SettingsError(f"level is one of {', '.join(LEVELS)}")
            return value
        return bool(value)
    if name == "mallard":
        text = str(value or "").strip()
        if text and not Path(text).expanduser().is_file():
            raise SettingsError(f"{text}: no such file")
        return str(Path(text).expanduser()) if text else ""
    return str(value or "")


def pane_layout(layout: dict[str, Any]) -> dict[str, int | bool]:
    """`name = px` for a pane's size and `name-collapsed = true` for a collapsed one; anything else dropped."""
    out: dict[str, int | bool] = {}
    for key, value in layout.items():
        if not isinstance(key, str) or not PANE.fullmatch(key) or isinstance(value, bool) != key.endswith("-collapsed"):
            continue
        if isinstance(value, bool):
            if value:
                out[key] = True
        elif isinstance(value, int | float) and 0 < value <= PANE_MAX:
            out[key] = round(value)
    return out
