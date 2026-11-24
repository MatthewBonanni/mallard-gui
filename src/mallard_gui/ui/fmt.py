"""Numbers, sizes and durations as the UI shows them: comma digit groups, units always.

Only text to read: values written to the input or shown in fields typed back stay ungrouped (TOML has no
digit separators but `_`)."""

from __future__ import annotations

import datetime
from typing import Any

from ..grouping import grouped

__all__ = ["bytes_text", "duration", "grouped", "local_time", "number"]


def number(v: Any) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return grouped(v)
    if v == 0:
        return "0"
    if abs(v) >= 1e5 or abs(v) < 1e-3:
        return f"{v:.3e}"
    return f"{v:.4g}"


def duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "–"
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60} min"
    if s >= 60:
        return f"{s // 60} min {s % 60} s"
    if 0 < seconds < 1:
        return f"{seconds:.2g} s"
    if seconds < 10 and seconds != s:
        return f"{seconds:.1f} s"
    return f"{s} s"


def bytes_text(n: Any) -> str:
    if not isinstance(n, (int, float)):
        return ""
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000:
            return f"{n:.3g} {unit}"
        n /= 1000
    return f"{n:.3g} PB"


def local_time(iso: Any) -> str:
    """A timestamp Mallard writes in UTC (`2026-11-24T00:25:15Z`) in this machine's time zone, to the minute."""
    try:
        t = datetime.datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso or "") or "–"
    return t.astimezone().strftime("%Y-%m-%d %H:%M %Z").rstrip()
