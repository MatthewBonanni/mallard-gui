"""Probe, force and reduction CSV files of a run, followed by byte offset like the run log.

Layout: a header `step,t,...`, a `# units: ...` line, then rows; other `#` lines (provenance, probe
sampling) are skipped, also when a continuation appends them again. A file whose rows carry many faces
(a boundary probe) is a profile: only its latest sample is kept. Every other file is a time series,
one per column (and per point, for probes of several points).
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field

from .history import History

# Columns that place a value rather than measure it.
# Sample geometry, not signals: lowercase before Mallard #1467, uppercase after (`s` and coordinates stay).
GEOMETRY = {"step", "t", "point", "face", "x", "y", "z", "s", "nx", "ny", "nz", "area", "length"}
GEOMETRY |= {"N_X", "N_Y", "N_Z", "AREA", "LENGTH", "COUNT", "VOLUME"}
TRUNCATED = b"\0TRUNCATED"


@dataclass
class CsvSeries:
    path: str
    columns: list[str] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)
    # Time series: (column, point) -> its history. Profiles: the rows of the latest step.
    series: dict[tuple[str, int], History] = field(default_factory=dict)
    rows: list[list[float]] = field(default_factory=list)
    step: float | None = None
    t: float | None = None

    @property
    def profile(self) -> bool:
        return "face" in self.columns

    @property
    def variables(self) -> list[str]:
        return [c for c in self.columns if c not in GEOMETRY]

    def unit(self, column: str) -> str:
        u = self.units.get(column, "")
        return "" if u == "1" else u

    def add_line(self, line: str, offset: int = -1) -> None:
        """`offset`: the line's byte offset in the file, kept so a zoom can read that range again."""
        line = line.strip()
        if not line:
            return
        if line.startswith("#"):
            if line.startswith("# units:") and self.columns:
                units = [u.strip() for u in line.removeprefix("# units:").split(",")]
                self.units = dict(zip(self.columns, units, strict=False))
            return
        cells = line.split(",")
        if not self.columns or cells == self.columns:
            self.columns = [c.strip() for c in cells]
            return
        try:
            row = [float(c) for c in cells]
        except ValueError:
            return
        if len(row) != len(self.columns):
            return
        col = dict(zip(self.columns, row, strict=True))
        step, t = col.get("step"), col.get("t", 0.0)
        if self.profile:
            if step != self.step:
                self.rows = []
            self.rows.append(row)
        else:
            point = int(col.get("point", 0))
            for name in self.variables:
                h = self.series.get((name, point))
                if h is None:
                    h = self.series[(name, point)] = History()
                h.append(t, col[name], offset)
        self.step, self.t = step, t

    def reread(self, data: bytes, offset: int) -> CsvSeries:
        """The rows of a byte range of the file starting at `offset` (a zoom past the levels held), each
        series at full resolution."""
        out = CsvSeries(self.path, list(self.columns), dict(self.units))
        at = offset
        for line in data.split(b"\n"):
            out.add_line(line.decode("utf-8", "replace"), at)
            at += len(line) + 1
        return out

    def points(self) -> list[int]:
        return sorted({p for _, p in self.series})

    def column(self, name: str) -> list[float]:
        i = self.columns.index(name)
        return [r[i] for r in self.rows]


class CsvFollower:
    """New complete lines of a growing CSV file; a partial last line waits for its newline."""

    def __init__(self, path: str, read: Callable[[int], bytes]) -> None:
        self.read = read
        self.offset = 0
        self._partial = b""
        self.data = CsvSeries(path)

    def poll(self) -> bool:
        chunk = self.read(self.offset)
        if chunk == TRUNCATED:
            self.offset, self._partial, self.data = 0, b"", CsvSeries(self.data.path)
            chunk = self.read(0)
        if not chunk:
            return False
        at = self.offset - len(self._partial)
        self.offset += len(chunk)
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        for line in lines:
            self.data.add_line(line.decode("utf-8", "replace"), at)
            at += len(line) + 1
        return True


def csv_files(input_text: str) -> list[str]:
    """The CSV files a run's input writes: probes, their reductions, and forces, in input order."""
    try:
        data = tomllib.loads(input_text)
    except tomllib.TOMLDecodeError:
        return []
    out: list[str] = []
    for table in ("probes", "forces"):
        entries = data.get(table)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            for key in ("file", "reductions_file"):
                path = entry.get(key)
                if isinstance(path, str) and path.endswith(".csv") and path not in out:
                    out.append(path)
    return out


def label(path: str) -> str:
    """`./solut/probe_wake_point.csv` -> `probe_wake_point`."""
    return path.rsplit("/", 1)[-1].removesuffix(".csv")
