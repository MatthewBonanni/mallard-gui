"""Saved views, in the project file as `[views.<name>]`: what the results page shows (run, series, snapshot by
step, zone surfaces, field, component, colormap and range), the camera, the section tool, and the probe and
line with their plot. Committed with the project, so a view reopens on any machine holding the run."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from typing import Any

from .project import Project
from .viewer.colormaps import DEFAULT, canonical

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


@dataclass
class SavedView:
    name: str
    run: str
    series: str
    step: int | None = None
    t: float | None = None
    zones: list[str] = dc_field(default_factory=list)
    field: str = ""
    component: str = "magnitude"
    colormap: str = DEFAULT
    interpolation: str = "cells"  # cells | linear: how the field is drawn
    range: list[float] = dc_field(default_factory=list)  # empty: per snapshot
    camera: dict[str, Any] = dc_field(default_factory=dict)
    section: dict[str, Any] = dc_field(default_factory=dict)
    probe: list[float] = dc_field(default_factory=list)
    line: list[list[float]] = dc_field(default_factory=list)
    plot: str = "time"  # time | spectrum

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"view name {self.name!r}: letters, digits, spaces, '.', '_' and '-'")
        if self.plot not in ("time", "spectrum"):
            raise ValueError(f"view plot {self.plot!r}: time or spectrum")
        self.colormap = canonical(self.colormap)

    def to_dict(self) -> dict[str, Any]:
        """Without the name (the table's key) and without empty values, which TOML cannot hold as null."""
        return {k: v for k, v in asdict(self).items() if k != "name" and v not in (None, "", [], {})}

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> SavedView:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data and k != "name"}
        return cls(name=name, **known)


def views(project: Project) -> list[SavedView]:
    out = []
    for name, data in sorted((project.data.get("views") or {}).items()):
        try:
            out.append(SavedView.from_dict(name, dict(data)))
        except (TypeError, ValueError):
            continue  # a view this GUI cannot read is kept in the file, not shown
    return out


def save_view(project: Project, view: SavedView) -> None:
    table = dict(project.data.get("views") or {})
    table[view.name] = view.to_dict()
    project.data["views"] = table
    project.save()


def delete_view(project: Project, name: str) -> None:
    table = dict(project.data.get("views") or {})
    if table.pop(name, None) is not None:
        project.data["views"] = table
        project.save()
