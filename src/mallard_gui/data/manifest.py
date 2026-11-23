"""A run's files, each known by its content: immutable objects by SHA-256, append-only streams by length
and the SHA-256 of that prefix.

The run log's output records name the objects with their sizes and SHA-256; what it does not record is
hashed where it lives, in one round trip.
TODO(#1317): `mallard files RUN_DIR --format json` returns this manifest on the location itself.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import asdict, dataclass, field
from typing import Any

from .locations import Location, join

RUN_LOG = "mallard.jsonl"

# Kinds of file.
OBJECT = "object"  # written once: its SHA-256 names it
STREAM = "stream"  # only grows while the run continues: a copy is a prefix of it
LIVE = "live"  # rewritten in place until its run ends (a single-file series): viewable, not copyable
DERIVED = "derived"  # regenerated from objects and the run log (series indexes, the run report)
PARTIAL = "partial"  # a write that did not finish

_STREAM_SUFFIXES = (".csv", ".jsonl")
_DERIVED_SUFFIXES = (".pvd", ".series", ".xmf", ".html")
_SKIP = {"mallard-gui.pid", "mallard.sock", "STOP", "CHECKPOINT"}
_INPUTS = ("input.toml", "run.sbatch")


@dataclass
class FileEntry:
    path: str  # relative to the run directory
    kind: str
    size: int
    sha256: str = ""  # an object's; a stream's prefix of `size` bytes
    category: str = ""  # snapshot | restart | series | probe | log | input | output | other
    step: int | None = None
    t: float | None = None
    writer: str = ""
    recorded: bool = False  # by the run log, rather than hashed here
    group: str = ""  # a piece of a snapshot of several files (one per rank): the snapshot's path
    chunk_bytes: int = 0  # with `chunks`: an object known by its chunk tree, sha256 "tree:<root>"
    chunks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileEntry:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class Manifest:
    location: str  # the location's id
    root: str
    files: dict[str, FileEntry] = field(default_factory=dict)
    ended: bool = False
    run_id: str = ""

    def entries(self) -> list[FileEntry]:
        return sorted(self.files.values(), key=lambda e: (_ORDER.get(e.category, 9), e.step or 0, e.path))

    @property
    def bytes(self) -> int:
        return sum(e.size for e in self.files.values())


_ORDER = {"input": 0, "log": 1, "probe": 2, "restart": 3, "snapshot": 4, "series": 5, "output": 6, "other": 7}


class LogIndex:
    """What a run log's output records say, read from where the last read stopped: a refresh of a run of
    thousands of ranks and steps reads the records written since, not the whole log again."""

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.offset = 0
        self._partial = b""
        self.files: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, int] = {}
        self.ended = False
        self.run_id = ""

    @property
    def rewritten(self) -> set[str]:
        return {p for p, n in self._seen.items() if n > 1}

    def update(self, location: Location, path: str, size: int) -> None:
        if size < self.offset:  # a new log at that path
            self._reset()
        if size > self.offset:
            self.feed(location.read_bytes(path, self.offset))

    def feed(self, data: bytes) -> None:
        self.offset += len(data)
        lines = (self._partial + data).split(b"\n")
        self._partial = lines.pop()
        for line in lines:
            self._record(line)

    def _record(self, line: bytes) -> None:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(r, dict):
            return
        kind = r.get("type")
        if kind == "start":
            self.ended, self.run_id = False, str(r.get("run_id", ""))
        elif kind == "end":
            self.ended = True
        elif kind == "output" and r.get("complete", True):
            files = r.get("files") or []
            group = _norm(str(r.get("path", ""))) if len(files) > 1 else ""
            for f in files:
                path = _norm(str(f.get("path", "")))
                if not path:
                    continue
                self._seen[path] = self._seen.get(path, 0) + 1
                tree = f.get("sha256_tree")
                self.files[path] = {
                    "bytes": f.get("bytes"),
                    "sha256": f"tree:{tree}" if tree and not f.get("sha256") else f.get("sha256", ""),
                    "chunk_bytes": int(f.get("chunk_bytes") or 0) if tree else 0,
                    "chunks": [str(c) for c in f.get("chunks") or []] if tree else [],
                    "kind": r.get("kind", ""),
                    "step": r.get("step"),
                    "t": r.get("t"),
                    "writer": _norm(str(r.get("writer", ""))),
                    "group": group,
                }


def recorded_outputs(log: bytes) -> tuple[dict[str, dict[str, Any]], set[str], bool, str]:
    """From a run log: the last record of every file written, the paths written more than once (rewritten
    in place), whether the last run ended, and the last run's id."""
    index = LogIndex()
    index.feed(log if log.endswith(b"\n") else log + b"\n")
    return index.files, index.rewritten, index.ended, index.run_id


# Per location and run directory, kept between refreshes.
_LOGS: dict[tuple[str, str], LogIndex] = {}


def groups(manifest: Manifest) -> dict[str, list[FileEntry]]:
    """The pieces of each snapshot of several files, by the snapshot's path: shown and selected as one."""
    out: dict[str, list[FileEntry]] = {}
    for e in manifest.files.values():
        if e.group:
            out.setdefault(e.group, []).append(e)
    return out


def _norm(path: str) -> str:
    return posixpath.normpath(path) if path else ""


def classify(rel: str, size: int, record: dict[str, Any] | None, rewritten: bool, ended: bool) -> FileEntry | None:
    name = posixpath.basename(rel)
    if name in _SKIP:
        return None
    if name.endswith(".partial"):
        return FileEntry(rel, PARTIAL, size, category="other")
    if record is not None and record.get("bytes") == size and record.get("sha256"):
        category = "restart" if record["kind"] == "restart" else "snapshot"
        kind = OBJECT
        if rewritten:
            category = "series"
            kind = OBJECT if ended else LIVE
        return FileEntry(
            rel,
            kind,
            size,
            str(record["sha256"]),
            category,
            record.get("step"),
            record.get("t"),
            str(record.get("writer", "")),
            recorded=True,
            chunk_bytes=int(record.get("chunk_bytes") or 0),
            chunks=list(record.get("chunks") or []),
            group=str(record.get("group", "")),
        )
    if name == RUN_LOG or (name.startswith("stdout") and name.endswith(".txt")):
        return FileEntry(rel, STREAM, size, category="log")
    if name.endswith(_STREAM_SUFFIXES):
        return FileEntry(rel, STREAM, size, category="probe")
    if name.endswith(_DERIVED_SUFFIXES):
        return FileEntry(rel, DERIVED, size, category="output")
    if rel in _INPUTS:
        return FileEntry(rel, OBJECT, size, category="input")
    # Not recorded (an input, a file the run read, or one rewritten by a rewind): hashed as it is now.
    return FileEntry(rel, OBJECT, size, category="other")


def build_manifest(location: Location, root: str) -> Manifest:
    """The manifest of the run directory `root` at `location`: two round trips (list, hash), and a read of
    the run log's new records."""
    listing = location.list_files(root)
    log = _LOGS.setdefault((location.id, root), LogIndex())
    log.update(location, join(root, RUN_LOG), listing.get(RUN_LOG, 0))
    manifest = Manifest(location.id, root, ended=log.ended, run_id=log.run_id)
    records, rewritten = log.files, log.rewritten
    for rel, size in sorted(listing.items()):
        entry = classify(rel, size, records.get(rel), rel in rewritten, log.ended)
        if entry is not None:
            manifest.files[rel] = entry
    todo = [e for e in manifest.files.values() if not e.sha256 and e.kind != PARTIAL]
    hashes = location.hash_files([(join(root, e.path), e.size if e.kind == STREAM else None) for e in todo])
    for e in todo:
        e.sha256 = hashes.get(join(root, e.path)) or ""
    return manifest
