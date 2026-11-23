"""Presets as Mallard resolves them: which preset is in effect, the keys each one fills and with what, the keys the
input sets over it, and what switching to another would change.

Nothing here knows a preset's contents. Every value comes from `mallard input resolve` (the input as a run uses it)
and the preset row of `mallard check`'s report; a preset key is any catalog key named `preset` with values. The keys
a preset fills are those whose resolved value differs between presets, with the run logs' exact lists (their run
block's `preset.settings`) added where the project has run that preset.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from .input_doc import display_value

Resolver = Callable[[str], dict[str, Any] | None]  # an input's text -> its resolved input; None: no such command


class SetupError(Exception):
    """The input does not set up: Mallard's own diagnostic."""


WORKERS = 3  # resolves at once, on a shared machine


@dataclass
class PresetLine:
    name: str
    default: bool
    overrides: dict[str, str]  # key -> the value as the report writes it


def parse_preset_line(line: str | None) -> PresetLine | None:
    """`les (the input sets numerics.riemann_solver = "RHLL", run.cfl = 1)` -> les, not default, two overrides."""
    if not line:
        return None
    m = re.match(r"(\S+)(?:\s*\((.*)\))?\s*$", line.strip())
    if not m or m.group(1) in ("none", "-"):
        return None
    note = m.group(2) or ""
    sets = note.split("the input sets", 1)[1] if "the input sets" in note else ""
    overrides = dict(re.findall(r"([A-Za-z_][\w.]*) = (\"[^\"]*\"|[^,]+)", sets))
    return PresetLine(m.group(1), "default" in note.split(";")[0], {k: v.strip() for k, v in overrides.items()})


def flatten(table: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Dotted keys to values; arrays (of tables too) are values."""
    out: dict[str, Any] = {}
    for k, v in table.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


def edited(text: str, set_: Mapping[str, Any] = {}, remove: Iterable[str] = ()) -> str:
    """The input's text with dotted keys removed (a whole table too) and others set; for a scratch copy only."""
    doc = tomlkit.parse(text)
    for key in remove:
        *parents, leaf = key.split(".")
        node: Any = doc
        for p in parents:
            node = node.get(p) if hasattr(node, "get") else None
            if node is None:
                break
        if node is not None and hasattr(node, "get") and leaf in node:
            del node[leaf]
    for key, value in set_.items():
        *parents, leaf = key.split(".")
        node = doc
        for p in parents:
            if p not in node:
                node[p] = tomlkit.table()
            node = node[p]
        node[leaf] = value
    return tomlkit.dumps(doc)


def line_signature(text: str, key: str) -> tuple[Any, ...]:
    """What `mallard check`'s Preset row of an input depends on: the preset it names, its tables' `type`s (the
    reconstruction a preset fills or not) and which keys it sets, not their other values. Inputs with one
    signature have one row."""
    flat = flatten(tomlkit.parse(text).unwrap())
    table = key.rpartition(".")[0]
    types = sorted((k, repr(v)) for k, v in flat.items() if k.startswith(table + ".") and k.endswith(".type"))
    return tuple(sorted(flat)), repr(flat.get(key)), tuple(types)


def run_log_presets(
    logs: Iterable[Path], cache: dict[Path, tuple[tuple[int, int], tuple[str, list[str]] | None]] | None = None
) -> dict[str, list[str]]:
    """The keys each preset fills, as runs recorded them (`provenance.preset.settings`). With `cache`, a log is
    read again only once it changed."""
    found: dict[str, list[str]] = {}
    for log in logs:
        try:
            st = log.stat()
        except OSError:
            continue
        stamp = (st.st_size, st.st_mtime_ns)
        if cache is not None and log in cache and cache[log][0] == stamp:
            recorded = cache[log][1]
        else:
            recorded = _log_preset(log)
            if cache is not None:
                cache[log] = (stamp, recorded)
        if recorded is not None:
            found.setdefault(*recorded)
    return found


def _log_preset(log: Path) -> tuple[str, list[str]] | None:
    try:
        with log.open(encoding="utf-8") as f:
            head = f.read(1 << 20)
    except OSError:
        return None
    for line in head.splitlines():
        if '"preset"' not in line:
            continue
        try:
            preset = _find(json.loads(line), "preset")
        except json.JSONDecodeError:
            continue
        if isinstance(preset, dict) and preset.get("name") and isinstance(preset.get("settings"), list):
            return str(preset["name"]), [
                str(s["key"]) for s in preset["settings"] if isinstance(s, dict) and "key" in s
            ]
        return None
    return None


def _find(node: Any, name: str) -> Any:
    if isinstance(node, dict):
        if name in node:
            return node[name]
        for v in node.values():
            hit = _find(v, name)
            if hit is not None:
                return hit
    return None


@dataclass
class PresetView:
    key: str  # the preset key, `numerics.preset`
    names: list[str]
    active: str | None = None  # the preset in effect
    explicit: bool = False  # the input names it
    default: bool = False  # Mallard applies it by default
    fills: dict[str, dict[str, Any]] = field(default_factory=dict)  # preset -> key -> the value it gives
    effective: dict[str, Any] = field(default_factory=dict)  # the input's resolved keys
    overrides: dict[str, Any] = field(default_factory=dict)  # keys the input sets over the active preset
    unused: dict[str, set[str]] = field(default_factory=dict)  # preset -> keys other presets use and it does not
    set_keys: set[str] = field(default_factory=set)  # keys the input sets
    error: str = ""

    @property
    def table(self) -> str:
        return self.key.rsplit(".", 1)[0]

    def origin(self, key: str) -> str:
        """ "override" for a key the input sets over the preset, "preset" for one the preset fills, else ""."""
        if self.active is None:
            return ""
        if key in self.overrides:
            return "override"
        return "preset" if key in self.fills.get(self.active, {}) else ""

    def overrides_for(self, name: str) -> list[str]:
        """The keys the input sets over preset `name` once it is chosen: today's overrides, the keys it fills and the
        keys it leaves unused (an `order` its reconstruction has no use for)."""
        keys = dict.fromkeys([*self.overrides, *self.fills.get(name, {}), *sorted(self.unused.get(name, ()))])
        return [k for k in keys if k in self.set_keys and k != self.key]

    def summary(self, name: str) -> str:
        """What a preset chooses, in its own values: `TENO · 5 · HLLC · riemann · SSPRK3 · 1.0`."""
        fills = self.fills.get(name, {})
        return " · ".join(display_value(fills[k]) for k in sorted(fills, key=_order))

    def to_dict(self) -> dict[str, Any]:
        n = len(self.overrides)
        active = self.fills.get(self.active or "", {})
        return {
            "key": self.key,
            "active": self.active or "",
            "explicit": self.explicit,
            "default": self.default,
            "error": self.error,
            "status": (
                f"preset {self.active}"
                + (" (the default)" if self.default else "")
                + (f", {n} override{'s' if n != 1 else ''}" if n else "")
                if self.active
                else "no preset: every key as the input or its default sets it"
            ),
            "options": [
                {"name": p, "summary": self.summary(p), "keys": sorted(self.fills.get(p, {}), key=_order)}
                for p in self.names
            ],
            "overrides": [
                {
                    "key": k,
                    "value": display_value(v),
                    "preset": display_value(active[k]) if k in active else "–",
                }
                for k, v in self.overrides.items()
            ],
        }


def _order(key: str) -> tuple[int, str]:
    return (key.count("."), key)


def compute(
    text: str,
    key: str,
    names: list[str],
    resolve: Resolver,
    preset_line: Callable[[str], str | None],
    recorded: Mapping[str, list[str]] = {},
) -> PresetView:
    view = PresetView(key, list(names))
    table = view.table
    data = tomlkit.parse(text).unwrap()
    explicit = flatten(data).get(key)
    with ThreadPoolExecutor(WORKERS) as pool:
        line_job = pool.submit(preset_line, text)
        current_job = pool.submit(resolve, text)
        line = parse_preset_line(line_job.result())
        overrides_named = list(line.overrides) if line else []
        outside = {k for keys in recorded.values() for k in keys if not k.startswith(table + ".")}
        # Each preset alone: the input without the preset's table and without the keys it sets over the preset.
        bare = {
            name: pool.submit(resolve, edited(text, {key: name}, [table, *overrides_named, *outside])) for name in names
        }
        alone = {name: _or_none(job.result) for name, job in bare.items()}
        try:
            current = current_job.result()
        except SetupError as e:
            view.error = f"mallard input resolve: {e} (values shown are the input's)"
            return view
    if current is None:
        view.error = "this mallard has no input resolve: values shown are the input's"
        return view
    view.effective = flatten(current)
    flat = {n: flatten(r) for n, r in alone.items() if r is not None}
    for name, keys in flat.items():
        others = [o for n, o in flat.items() if n != name]
        fills = {k: v for k, v in keys.items() if k != key and any(k in o and o[k] != v for o in others)}
        for k in recorded.get(name, []):
            if k in keys:
                fills[k] = keys[k]
        view.fills[name] = fills
        view.unused[name] = {k for o in others for k in o if k not in keys and k.startswith(table + ".")}
    view.explicit = explicit is not None
    view.set_keys = set(flatten(data))
    if line:
        view.active, view.default = line.name, line.default
    elif explicit is not None:
        view.active = str(explicit)
    if view.active:
        input_keys = flatten(data)
        if line:
            view.overrides = {k: input_keys.get(k, v) for k, v in line.overrides.items()}
        else:
            view.overrides = {k: input_keys[k] for k in view.fills.get(view.active, {}) if k in input_keys}
    return view


def _or_none(result: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
    try:
        return result()
    except SetupError:
        return None


def switch_changes(
    text: str, view: PresetView, name: str, keep: bool, resolve: Resolver
) -> tuple[list[dict[str, str]], str | None]:
    """What switching to preset `name` changes in the resolved input, keeping or dropping the overrides: each key's
    value now and after; and an error when the switched input does not set up."""
    remove = [] if keep else view.overrides_for(name)
    how = f"with preset {name}, {'keeping' if keep else 'dropping'} the overrides"
    try:
        after = resolve(edited(text, {view.key: name}, remove))
    except SetupError as e:
        return [], f"{how}, the input does not set up: {e}"
    if after is None:
        return [], f"{how}, the input does not set up"
    new = flatten(after)
    changes = []
    for k in sorted(set(view.effective) | set(new), key=_order):
        if k == view.key:
            continue
        old, now = view.effective.get(k), new.get(k)
        if old != now:
            changes.append(
                {
                    "key": k,
                    "old": display_value(old) if old is not None else "–",
                    "new": display_value(now) if now is not None else "–",
                }
            )
    return changes, None
