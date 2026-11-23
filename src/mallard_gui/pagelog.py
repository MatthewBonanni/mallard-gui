"""What went wrong in the page, on disk: `page.log` beside settings.toml (the desktop app's data directory when
it sets one, else ~/.config/mallard-gui). The page (assets/diagnostics.js) reports uncaught errors, lost WebGL
contexts and the case view's mode as it sees it; the server adds its own events. One JSON line per event, the
previous log kept as page.log.1 once this one passes a megabyte."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .settings import settings_file

LIMIT = 1_000_000
DETAIL = 4000


def log_file() -> Path:
    return settings_file().parent / "page.log"


class PageLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or log_file()

    def write(self, kind: str, detail: Any = None, source: str = "page") -> None:
        """Append one event; a log that cannot be written never stops the app."""
        text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        line = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "pid": os.getpid(),
            "source": source,
            "kind": str(kind)[:80],
            "detail": text[:DETAIL],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > LIMIT:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass
