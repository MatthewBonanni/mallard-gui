"""Following a run log (`mallard.jsonl`) by byte offset: only what is new is read.

The series kept are whatever numbers the progress records carry (`monitors`, `integrals`,
`forces_<zone>`, `probes`), with the units the start record gives; no quantity is hard-coded.
The probe and force CSV files the run's input names are followed the same way, for every sample.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .csvseries import TRUNCATED, CsvFollower, CsvSeries, csv_files
from .history import History

SCHEMAS = ("mallard-run-log", "mallard-run")
MIN_VERSION, MAX_VERSION = 2, 3
_GROUPS = ("monitors", "integrals", "probes")
_SKIP = {"step", "t"}


@dataclass
class Series:
    name: str
    group: str
    unit: str = ""
    history: History = field(default_factory=History)

    @property
    def t(self) -> list[float]:
        return self.history.t

    @property
    def values(self) -> list[float]:
        return self.history.values


@dataclass
class RunState:
    run_id: str = ""
    version: int | None = None
    start: dict[str, Any] = field(default_factory=dict)
    last: dict[str, Any] = field(default_factory=dict)
    end: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    series: dict[str, Series] = field(default_factory=dict)
    units: dict[str, Any] = field(default_factory=dict)
    records: int = 0
    unsupported: str = ""
    csv: dict[str, CsvSeries] = field(default_factory=dict)
    starts: int = 0  # start records: a run continued from restarts has one per part
    divergence: dict[str, Any] | None = None  # the divergence report of a run that diverged

    @property
    def status(self) -> str:
        if self.end is not None:
            return str(self.end.get("outcome", "ended"))
        return "running" if self.start else "starting"

    @property
    def progress(self) -> float:
        return float(self.last.get("progress") or 0.0)

    def unit(self, group: str, name: str) -> str:
        units = self.units.get(group) if group != "run" else self.units
        if isinstance(units, dict):
            u = units.get(name)
            if isinstance(u, str) and u != "1":
                return u
        return ""

    def add(self, record: dict[str, Any], offset: int = -1) -> None:
        """`offset`: the record's byte offset in the run log, kept so a zoom can read that range again."""
        self.records += 1
        kind = record.get("type")
        if kind == "start":
            self._start(record)
        elif kind == "progress":
            self._progress(record, offset)
        elif kind == "end":
            self.end = record
            self.events.append(
                {
                    "kind": "end",
                    "step": record.get("step"),
                    "t": record.get("t"),
                    "text": record.get("reason") or record.get("outcome", ""),
                }
            )
        elif kind == "event":
            self.events.append(record)
        elif kind in ("warning", "error"):
            self.warnings.append(record)
            self.events.append({"kind": kind, "text": f"{record.get('code', '')} {record.get('message', '')}"})
        elif kind == "output":
            self.outputs.append(record)
        elif kind == "divergence":
            self.divergence = record

    def _start(self, record: dict[str, Any]) -> None:
        schema, version = record.get("schema"), record.get("version")
        if schema not in SCHEMAS or not isinstance(version, int) or not MIN_VERSION <= version <= MAX_VERSION:
            # A newer major: keep reading what is understood, and say so.
            self.unsupported = (
                f"run log {schema} version {version}; this GUI reads versions {MIN_VERSION}-{MAX_VERSION}"
            )
        self.starts += 1
        self.version = version if isinstance(version, int) else None
        self.run_id = str(record.get("run_id", ""))
        self.start = record
        self.end = self.divergence = None
        self.units = dict(record.get("units") or {})
        self.events.append(
            {
                "kind": "start",
                "step": (record.get("restart") or {}).get("step", 0),
                "t": None,
                "text": f"run {self.run_id[:8]}",
            }
        )

    def _progress(self, record: dict[str, Any], offset: int = -1) -> None:
        self.last = record
        t = record.get("t")
        if not isinstance(t, (int, float)):
            return
        for name in ("dt", "cfl", "cells_per_second"):
            self._append("run", name, t, record.get(name), offset)
        for group in _GROUPS:
            self._walk(group, record.get(group), t, offset)
        for key, value in record.items():
            if key.startswith("forces_"):
                self._walk(key, value, t, offset)

    def _walk(self, group: str, value: Any, t: float, offset: int, prefix: str = "") -> None:
        if not isinstance(value, dict):
            return
        for name, v in value.items():
            if name in _SKIP:
                continue
            if isinstance(v, dict):
                self._walk(group, v, t, offset, f"{prefix}{name}.")
            else:
                self._append(group, prefix + name, t, v, offset)

    def _append(self, group: str, name: str, t: float, value: Any, offset: int = -1) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        key = f"{group}/{name}"
        s = self.series.get(key)
        if s is None:
            s = self.series[key] = Series(name, group, self.unit(group, name.rsplit(".", 1)[-1]))
        s.history.append(float(t), float(value), offset)


class RunLogFollower:
    """Reads new complete lines of a growing file; a partial last line waits for its newline.

    `read_file(path, offset)` reads another file of the run directory (the input and the CSV files)."""

    def __init__(
        self,
        path: Path,
        read: Callable[[int], bytes] | None = None,
        read_file: Callable[[str, int], bytes] | None = None,
    ) -> None:
        self.path = path
        self.offset = 0
        self._partial = b""
        self.state = RunState()
        self._read_file = read_file or self._read_local
        self._read = read or (lambda offset: self._read_file(path.name, offset))
        self._csv: list[CsvFollower] | None = None

    def _read_local(self, name: str, offset: int) -> bytes:
        try:
            with (self.path.parent / name).open("rb") as f:
                size = f.seek(0, 2)
                if size < offset:  # truncated: a fresh start in the same directory
                    return TRUNCATED
                f.seek(offset)
                return f.read()
        except FileNotFoundError:
            return b""

    def poll(self) -> list[dict[str, Any]]:
        records = self._poll_log()
        self._poll_csv()
        return records

    def _poll_log(self) -> list[dict[str, Any]]:
        chunk = self._read(self.offset)
        if chunk == TRUNCATED:
            self.offset, self._partial, self.state, self._csv = 0, b"", RunState(), None
            chunk = self._read(0)
        if not chunk:
            return []
        at = self.offset - len(self._partial)
        self.offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        records = []
        for line in lines:
            for r in _decode([line]):
                self.state.add(r, at)
                records.append(r)
            at += len(line) + 1
        return records

    def _poll_csv(self) -> None:
        if self._csv is None:
            if not self.state.start:
                return  # the run has not read its input yet
            text = self._read_file("input.toml", 0)
            self._csv = [
                CsvFollower(p, functools.partial(self._read_file, p))
                for p in csv_files(text.decode("utf-8", "replace"))
            ]
        for f in self._csv:
            f.poll()
            if f.data.columns:
                self.state.csv[f.data.path] = f.data


def _decode(lines: Iterable[bytes]) -> Iterable[dict[str, Any]]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record
