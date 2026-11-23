"""Changing a running run's settings: the keys this build's `mallard ctl set` takes, spelled for the run's own
input (`write_data.<prefix or index>.interval` -> `write_data.flow.interval`), sent over the run's control
socket from its node or as a SET file from anywhere its directory is visible. The run log is the reply:
`set` events for applied settings, `W-SET-REFUSED` warnings for refused ones.

Every call to Mallard goes through `Mallard`; this module only decides which channel and what to send.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mallard import Capabilities, Mallard, write_set_file

SOCKET = "mallard.sock"
# The run polls control files every this many steps when the GUI starts it (off by default in Mallard).
CONTROL_FILE_INTERVAL = 10

_PLACEHOLDER = re.compile(r"<([^>]+)>")
# Which key names an element for each placeholder of `ctl set`'s keys.
_NAMING = {"prefix or index": "prefix", "prefix_or_index": "prefix", "name": "name", "zone": "zone"}


def settable_keys(patterns: Sequence[str], data: Mapping[str, Any]) -> list[str]:
    """The settable keys of a run, with each `<...>` replaced by the run's elements (a writer's prefix as written, else
    its index; a probe's name; a force's zone). Arrays the input lacks give no keys."""
    out = []
    for pattern in patterns:
        m = _PLACEHOLDER.search(pattern)
        if m is None:
            if "." not in pattern or pattern.split(".")[0] in data:
                out.append(pattern)
            continue
        array = pattern[: m.start()].rstrip(".")
        naming = _NAMING.get(m[1], m[1])
        elements = data.get(array)
        for i, el in enumerate(elements if isinstance(elements, list) else []):
            ident = el.get(naming) if isinstance(el, Mapping) else None
            if ident is None and naming != "prefix":
                continue
            ident = str(ident if ident is not None else i)
            out.append(pattern[: m.start()] + ident + pattern[m.end() :])
    return out


def setting_of(capabilities: Capabilities, key: str) -> dict[str, Any]:
    """What `initialize` says of a run's settable key (`write_data.flow.interval`): its type, units and help, by
    the pattern it is spelled from; empty for builds that report none."""
    for pattern, info in capabilities.settings.items():
        if re.fullmatch("[^.]+".join(re.escape(part) for part in _PLACEHOLDER.split(pattern)[::2]), key):
            return info
    return {}


def current_value(data: Mapping[str, Any], key: str) -> Any:
    """The value a settable key has in the run's input (`run.<key>` for the run's own limits), or None."""
    parts = key.split(".")
    if len(parts) == 1:
        return (data.get("run") or {}).get(key, (data.get("output") or {}).get(key))
    array, ident, leaf = parts[0], ".".join(parts[1:-1]), parts[-1]
    if not ident:
        return (data.get(array) or {}).get(leaf) if isinstance(data.get(array), Mapping) else None
    for i, el in enumerate(data.get(array) or []):
        if not isinstance(el, Mapping):
            continue
        names = {str(el.get(n, "")) for n in ("prefix", "name", "zone")} | {str(i)}
        if ident in names:
            return el.get(leaf)
    return None


@dataclass
class Reply:
    ok: bool
    channel: str  # "mallard ctl" | "SET file" | ""
    text: str  # Mallard's reply as written, or what is missing


class LiveControl:
    def __init__(self, mallard: Mallard, capabilities: Capabilities) -> None:
        self.mallard = mallard
        self.capabilities = capabilities

    def channel(self, run_dir: Path) -> str:
        """The socket when this machine runs rank 0 (it is there), else a SET file when the build polls one."""
        caps = self.capabilities
        if (run_dir / SOCKET).exists() and (caps.source != "initialize" or "set" in caps.commands):
            return "mallard ctl"
        return "SET file" if self.capabilities.set_file else ""

    def apply(self, run_dir: Path, settings: Mapping[str, str], allowed: Sequence[str]) -> Reply:
        refused = [k for k in settings if k not in allowed]
        if refused:
            return Reply(False, "", f"not settable while running: {', '.join(refused)}")
        channel = self.channel(run_dir)
        if channel == "mallard ctl":
            out = self.mallard.ctl_set(run_dir, settings)
            return Reply(out.returncode == 0, channel, (out.stdout + out.stderr).strip())
        if channel == "SET file":
            write_set_file(run_dir, settings)
            return Reply(True, channel, "written; the run log answers within control_file_interval steps")
        return Reply(False, "", "no control socket here, and this Mallard has no SET file (PR #1334)")


def set_replies(events: Sequence[Mapping[str, Any]], warnings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The run log's answers to settings: applied ones (`set` events) and refused SET-file lines."""
    out = [
        {"ok": True, "step": e.get("step"), "setting": e.get("setting") or e.get("text", ""), "source": e.get("source")}
        for e in events
        if e.get("kind") == "set"
    ]
    out += [
        {"ok": False, "step": w.get("step"), "setting": w.get("message", ""), "source": "SET file"}
        for w in warnings
        if w.get("code") == "W-SET-REFUSED"
    ]
    return out
