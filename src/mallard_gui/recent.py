"""Recently opened inputs, newest first, kept in settings.toml (`recent`; see settings.py). The desktop shell's
Open Recent menu and the command palette list the same files. Each call rereads the file, so another GUI's
additions are seen."""

from __future__ import annotations

from pathlib import Path

from .settings import RECENT_LIMIT as LIMIT
from .settings import Settings

__all__ = ["LIMIT", "add", "clear", "load"]


def load() -> list[Path]:
    """The inputs that still exist."""
    return Settings().recent()


def add(path: Path) -> None:
    Settings().add_recent(path)


def clear() -> None:
    Settings().clear_recent()
