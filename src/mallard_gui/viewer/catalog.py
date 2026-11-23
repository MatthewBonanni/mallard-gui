"""What a run directory holds to view: its snapshot series, from the run log's output records (each names
its writer), else from the series indexes (`.pvd`), classified by the run's own input; and a snapshot's
header, read without its data (fields, units, step, time and cell count), so budgets are checked before
anything of size O(cells) is read. Standard library only: this runs next to the data."""

from __future__ import annotations

import json
import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VIEWABLE = {"vtu": "vtu", "vtp": "vtp", "pvtu": "pvtu", "vtkhdf": "vtkhdf", "hdf": "vtkhdf"}
HEADER_BYTES = 256 * 1024
_SNAPSHOT = re.compile(rb"<!-- mallard-snapshot (\{.*?\}) -->", re.S)
_PIECE = re.compile(rb'<Piece\b[^>]*?NumberOfPoints="(\d+)"[^>]*?(?:NumberOfCells|NumberOfPolys|NumberOfLines)="(\d+)"')
_COUNTS = re.compile(rb'Number(?:OfCells|OfPolys|OfLines|OfStrips|OfVerts)="(\d+)"')
_INDEXED = re.compile(r"^(?P<stem>.*)_(?P<i>\d+)$")


@dataclass
class Snapshot:
    path: str  # relative to the run directory
    step: int | None = None
    t: float | None = None
    index: int = 0  # the time step within a single-file series (VTKHDF)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "step": self.step, "t": self.t, "index": self.index}


@dataclass
class Series:
    id: str  # the writer's prefix, relative: "solut/cylinder"
    kind: str  # volume | zone | region | probe | particles
    region: str = ""
    format: str = ""
    fields: list[str] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.id.rsplit("/", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "region": self.region,
            "format": self.format,
            "fields": list(self.fields),
            "snapshots": [s.to_dict() for s in self.snapshots],
        }


def _rel(path: str) -> str:
    p = posixpath.normpath(path.removeprefix("./"))
    if p.startswith(("../", "/")) or p == "..":
        raise ValueError(f"{path!r}: outside the run directory")
    return p


def _stem(writer: str) -> str:
    """`./solut/wall.pvd` -> `solut/wall`; `./solut/q.vtkhdf` -> `solut/q`."""
    p = _rel(writer)
    for ext in (".vtkhdf.series", ".pvd", ".vtkhdf", ".hdf", ".xmf", ".series"):
        if p.endswith(ext):
            return p[: -len(ext)]
    return p


def discover(run_dir: Path) -> list[Series]:
    """Every snapshot series of the run, in the order its writers first wrote."""
    log = run_dir / "mallard.jsonl"
    found = _from_log(log.read_bytes()) if log.is_file() else {}
    if not found:
        for pvd in sorted(run_dir.rglob("*.pvd")):
            sid = pvd.relative_to(run_dir).as_posix()[: -len(".pvd")]
            snaps = _read_pvd(pvd, run_dir)
            if snaps:
                fmt = VIEWABLE.get(snaps[0].path.rsplit(".", 1)[-1], "")
                found[sid] = Series(sid, "volume", format=fmt, snapshots=snaps)
    _dedupe(found)
    inp = _read_input(run_dir / "input.toml")
    classify(list(found.values()), inp)
    return list(found.values()) + particles(run_dir, inp) + derived(run_dir)


_PARTICLES_AT = re.compile(r"^# step (\d+), t = (\S+)")


def particles(run_dir: Path, inp: dict[str, Any]) -> list[Series]:
    """The particles' CSV file (`[particles] file`), written at the end of the run: one snapshot, at the step
    and time its header records, its columns (after `id` and the position) the fields."""
    file = (inp.get("particles") or {}).get("file") if isinstance(inp.get("particles"), dict) else None
    if not isinstance(file, str):
        return []
    try:
        rel = _rel(file)
    except ValueError:
        return []
    path = run_dir / rel
    if not path.is_file():
        return []
    snap, columns = Snapshot(rel), []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#"):
                m = _PARTICLES_AT.match(line)
                if m:
                    snap.step, snap.t = int(m[1]), float(m[2])
                continue
            columns = particle_columns(line)
            break
    fields = [c for c in columns if c not in ("id", "x", "y", "z")]
    return [Series(rel.rsplit(".", 1)[0], "particles", format="csv", fields=fields, snapshots=[snap])]


POST_DIRS = ("average", "extract", "derived")
# The particle file's columns as Mallard before #1466 named them
OLD_PARTICLE_COLUMNS = {"v_x": "V_X", "v_y": "V_Y", "v_z": "V_Z", "T_p": "T_P"}


def particle_columns(header: str) -> list[str]:
    """A particle file's column names, an older Mallard's lowercase state columns as the current one names them."""
    return [OLD_PARTICLE_COLUMNS.get(c, c) for c in header.strip().split(",")]


def derived(run_dir: Path) -> list[Series]:
    """What `mallard post average`, `post extract` and `post derive` (and the viewer's surface exports) wrote
    into their directories of the run: each file or `.pvd` collection a series of kind `derived`. A VTKHDF
    file lists one snapshot here; the engine reads how many steps it holds."""
    out: list[Series] = []
    for d in POST_DIRS:
        base = run_dir / d
        if not base.is_dir():
            continue
        members: set[str] = set()
        for pvd in sorted(base.glob("*.pvd")):
            snaps = _read_pvd(pvd, run_dir)
            members |= {s.path for s in snaps}
            if snaps:
                fmt = VIEWABLE.get(snaps[0].path.rsplit(".", 1)[-1], "")
                out.append(Series(f"{d}/{pvd.stem}", "derived", format=fmt, snapshots=snaps))
        for f in sorted(base.iterdir()):
            ext = f.name.rsplit(".", 1)[-1]
            rel = f.relative_to(run_dir).as_posix()
            if ext not in VIEWABLE or rel in members or f.name.endswith(".partial"):
                continue
            out.append(Series(f"{d}/{f.stem}", "derived", format=VIEWABLE[ext], snapshots=[Snapshot(rel)]))
    return out


def _dedupe(found: dict[str, Series]) -> None:
    for s in found.values():
        # A rewind or a continued run writes a step again: the newest record holds.
        by_step: dict[tuple[int | None, str, int], Snapshot] = {}
        for snap in s.snapshots:
            by_step[(snap.step, snap.path if snap.step is None else "", snap.index)] = snap
        s.snapshots = sorted(by_step.values(), key=lambda x: (x.step if x.step is not None else -1, x.t or 0.0))


def _from_log(log: bytes) -> dict[str, Series]:
    found: dict[str, Series] = {}
    for line in log.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "output" or not rec.get("writer"):
            continue
        fmt = VIEWABLE.get(str(rec.get("kind", "")))
        if fmt is None or not rec.get("path"):
            continue
        sid = _stem(str(rec["writer"]))
        s = found.setdefault(sid, Series(sid, "volume", format=fmt))
        snap = Snapshot(_rel(str(rec["path"])), rec.get("step"), rec.get("t"))
        if fmt == "vtkhdf":
            snap.index = sum(1 for x in s.snapshots if x.path == snap.path)
        s.snapshots.append(snap)
    return found


def series_from_log(log: bytes, input_text: str) -> list[Series]:
    """A run's series from its run log and input alone, for when nothing can read the run directory there
    (batch images): the same as `discover` without the series indexes and derived files."""
    found = _from_log(log)
    _dedupe(found)
    classify(list(found.values()), _toml(input_text))
    return list(found.values())


def _read_pvd(pvd: Path, run_dir: Path) -> list[Snapshot]:
    try:
        root = ET.parse(pvd).getroot()
    except ET.ParseError:
        return []
    out = []
    for ds in root.iter("DataSet"):
        file, t = ds.get("file"), ds.get("timestep")
        if not file:
            continue
        rel = (pvd.parent / file).resolve().relative_to(run_dir.resolve()).as_posix()
        out.append(Snapshot(rel, None, float(t) if t else None))
    for i, snap in enumerate(out):
        m = _INDEXED.match(snap.path.rsplit(".", 1)[0])
        snap.step = int(m["i"]) if m else i
    return out


def _read_input(path: Path) -> dict[str, Any]:
    try:
        return _toml(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _toml(text: str) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # the Python of some pvpython builds (the parallel viewer's driver)
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def classify(series: list[Series], inp: dict[str, Any]) -> None:
    """Which series are the volume, a boundary zone's surface, another region, or a probe's samples."""
    zones = {str(b.get("name")) for b in inp.get("boundaries", []) if isinstance(b, dict)}
    writers: dict[str, dict[str, Any]] = {}
    for w in inp.get("write_data", []):
        if isinstance(w, dict) and isinstance(w.get("prefix"), str):
            writers[_rel(w["prefix"])] = w
    for s in series:
        w = writers.get(s.id)
        if w is None:
            m = _INDEXED.match(s.id)
            w = writers.get(m["stem"]) if m else None
        if w is None:
            s.kind = "probe" if s.label.startswith("probe_") else "volume"
            continue
        fields = w.get("fields")
        s.fields = [str(f) for f in fields] if isinstance(fields, list) else []
        region = w.get("region")
        if isinstance(region, str) and region != "domain":
            s.kind, s.region = ("zone" if region in zones else "region"), region
        elif (region is not None and region != "domain") or w.get("regions") is not None:
            s.kind, s.region = "region", "inline"


def snapshot_header(path: Path) -> dict[str, Any]:
    """A snapshot's metadata from its first bytes: Mallard's header (fields, units, step, time) and the
    pieces' point and cell counts. Counts are None when the format keeps them elsewhere (VTKHDF)."""
    with path.open("rb") as f:
        head = f.read(HEADER_BYTES)
    out: dict[str, Any] = {"step": None, "time": None, "fields": [], "cells": None, "points": None}
    m = _SNAPSHOT.search(head)
    if m:
        try:
            meta = json.loads(m.group(1))
        except json.JSONDecodeError:
            meta = {}
        out["step"], out["time"] = meta.get("step"), meta.get("time")
        out["fields"] = [
            {"name": f.get("name", ""), "units": f.get("units", ""), "long_name": f.get("long_name", "")}
            for f in meta.get("fields", [])
            if isinstance(f, dict)
        ]
        out["time_unit"] = (meta.get("units") or {}).get("time", "")
    pieces = _PIECE.findall(head)
    if pieces:
        out["points"] = sum(int(p) for p, _ in pieces)
        # A polydata piece has several kinds of cells; a grid's piece one count.
        cells = 0
        for piece in re.finditer(rb"<Piece\b[^>]*>", head):
            cells += sum(int(c) for c in _COUNTS.findall(piece.group(0)))
        out["cells"] = cells
    return out
