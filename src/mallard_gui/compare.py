"""Two runs side by side: their inputs as Mallard resolved them (every default filled in), the keys that
differ, and their probe and force series on one plot, with reference or experimental data from a CSV file.
"""

from __future__ import annotations

import csv
import io
import tomllib
from dataclasses import dataclass
from typing import Any

from .plots import Curve
from .runlog import RunState


@dataclass
class KeyDiff:
    key: str
    a: str
    b: str

    @property
    def status(self) -> str:
        if self.a == self.b:
            return "same"
        if not self.a:
            return "added"
        if not self.b:
            return "removed"
        return "changed"

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "a": self.a, "b": self.b, "status": self.status}


def sectioned(rows: list[KeyDiff]) -> list[dict[str, Any]]:
    """The rows under a head for each top-level table, as an inspector groups them: each row names its key
    within the table (`leaf`), each head how many of its keys differ."""
    out: list[dict[str, Any]] = []
    head: dict[str, Any] = {}
    for r in rows:
        cut = min((i for i in (r.key.find("."), r.key.find("[")) if i > 0), default=len(r.key))
        section = r.key[:cut]
        if section != head.get("section"):
            head = {"key": "\u00a7" + section, "section": section, "changed": 0, "total": 0}
            out.append(head)
        head["total"] += 1
        head["changed"] += r.status != "same"
        out.append({**r.to_dict(), "leaf": r.key[cut:].lstrip(".") or r.key})
    return out


def resolved_input(st: RunState) -> tuple[dict[str, Any], str]:
    """The run's input as its start record gives it: resolved (defaults filled in) where the build records
    that, else as written. Returns the table and which one it is."""
    inp = ((st.start.get("provenance") or {}).get("input")) or {}
    for which in ("resolved", "text"):
        text = inp.get(which)
        if isinstance(text, str):
            try:
                return tomllib.loads(text), which
            except tomllib.TOMLDecodeError:
                continue
    return {}, ""


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    return repr(v)


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """`boundaries[name = "farfield"].p` for arrays of tables with names, else `probes[1].point`: keys that
    line up between two runs whose arrays are in another order."""
    out: dict[str, str] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{path}."))
        elif isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            ident = next(
                (
                    k
                    for k in ("name", "zone", "prefix")
                    if all(isinstance(x.get(k), str) for x in value) and len({x[k] for x in value}) == len(value)
                ),
                None,
            )
            for i, entry in enumerate(value):
                sub = f'{path}[{ident} = "{entry[ident]}"]' if ident else f"{path}[{i + 1}]"
                out.update(flatten(entry, f"{sub}."))
        else:
            out[path] = _value(value)
    return out


def diff_inputs(a: dict[str, Any], b: dict[str, Any], changed_only: bool = False) -> list[KeyDiff]:
    fa, fb = flatten(a), flatten(b)
    keys = sorted(set(fa) | set(fb), key=lambda k: (k.split(".", 1)[0], k))
    rows = [KeyDiff(k, fa.get(k, ""), fb.get(k, "")) for k in keys]
    return [r for r in rows if r.status != "same"] if changed_only else rows


def series_sources(st: RunState) -> dict[str, str]:
    """What a run has to overlay: each column of its probe, force and reduction files, else the run log's."""
    out: dict[str, str] = {}
    for path, c in st.csv.items():
        if c.profile:
            continue
        name = path.rsplit("/", 1)[-1].removesuffix(".csv")
        for var, point in sorted(c.series):
            out[f"csv:{path}:{var}:{point}"] = f"{name} · {var}" + (f" [{point}]" if len(c.points()) > 1 else "")
    for key, s in st.series.items():
        if s.group.startswith("forces_") or s.group in ("probes", "integrals"):
            out.setdefault(f"log:{key}", f"{s.group.removeprefix('forces_')} · {s.name}")
    return out


def series_of(st: RunState, source: str) -> tuple[list[float], list[float], str]:
    """t, values and unit of a source of `series_sources`."""
    if source.startswith("csv:"):
        _, rest = source.split(":", 1)
        path, var, point = rest.rsplit(":", 2)
        c = st.csv.get(path)
        if c is None or (var, int(point)) not in c.series:
            return [], [], ""
        t, v = c.series[(var, int(point))].recent()
        return list(t), list(v), c.unit(var)
    s = st.series.get(source.removeprefix("log:"))
    return (list(s.t), list(s.values), s.unit) if s is not None else ([], [], "")


def reference_csv(text: str) -> tuple[list[Curve], str]:
    """Reference or experimental data: the first column is x, every other column a curve. `#` lines are
    comments; a header row names the columns. Returns the curves and the x column's name."""
    rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].lstrip().startswith("#")]
    if not rows:
        return [], ""
    header: list[str]
    try:
        [float(c) for c in rows[0]]
        header = ["x", *(f"y{i}" for i in range(1, len(rows[0])))]
    except ValueError:
        header, rows = [c.strip() for c in rows[0]], rows[1:]
    columns: list[list[float]] = [[] for _ in header]
    xs: list[list[float]] = [[] for _ in header]
    for r in rows:
        try:
            x = float(r[0])
        except (ValueError, IndexError):
            continue
        for j in range(1, min(len(r), len(header))):
            try:
                y = float(r[j])
            except ValueError:
                continue
            xs[j].append(x)
            columns[j].append(y)
    return [Curve(header[j], xs[j], columns[j]) for j in range(1, len(header)) if columns[j]], header[0]
