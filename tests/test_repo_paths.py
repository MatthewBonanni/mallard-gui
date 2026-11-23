from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESERVED_CHARS = re.compile(r'[<>:"|?*\\\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10))}


def windows_problem(component: str) -> str | None:
    if RESERVED_CHARS.search(component):
        return "reserved character"
    if component.endswith((".", " ")):
        return "trailing dot or space"
    if component.split(".")[0].rstrip(" ").upper() in RESERVED_NAMES:
        return "reserved name"
    return None


def test_windows_problems() -> None:
    assert windows_problem('probes[name = "wake_arc"].vtu') == "reserved character"
    assert windows_problem("a:b") == "reserved character"
    assert windows_problem("dir.") == "trailing dot or space"
    assert windows_problem("nul.txt") == "reserved name"
    assert windows_problem("com1") == "reserved name"
    assert windows_problem("console.txt") is None
    assert windows_problem("probes[name = _wake_arc_].vtu") is None


def test_tracked_paths_check_out_on_windows() -> None:
    """Every tracked path checks out on Windows: the release workflow's Windows job clones the repo."""
    paths = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.split("\0")
    paths = [p for p in paths if p]
    assert paths
    bad = [f"{p}: {why}" for p in paths for c in p.split("/") if (why := windows_problem(c))]
    seen: dict[str, str] = {}
    for p in paths:
        parts = p.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            other = seen.setdefault(prefix.lower(), prefix)
            if other != prefix:
                bad.append(f"{p}: differs only by case from {other}")
    assert not bad, "\n".join(sorted(set(bad)))
