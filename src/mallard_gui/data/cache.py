"""The per-machine cache index (`~/.cache/mallard-gui/index.sqlite`): which copies exist where, interrupted
transfers, and the throughput measured per route.

Never committed and never authoritative: it is rebuilt from the locations' manifests, and an entry a
location contradicts is dropped.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..config import cache_dir
from .manifest import Manifest

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS copies (
    location TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,
    sha256 TEXT NOT NULL, checked REAL NOT NULL, PRIMARY KEY (location, path));
CREATE INDEX IF NOT EXISTS copies_sha ON copies (sha256);
CREATE TABLE IF NOT EXISTS transfers (
    location TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    done INTEGER NOT NULL, source TEXT NOT NULL, route TEXT NOT NULL, updated REAL NOT NULL,
    PRIMARY KEY (location, path));
CREATE TABLE IF NOT EXISTS routes (
    route TEXT PRIMARY KEY, bytes INTEGER NOT NULL, seconds REAL NOT NULL, updated REAL NOT NULL);
"""


@dataclass
class Copy:
    location: str
    path: str  # absolute at the location
    kind: str
    size: int
    sha256: str


@dataclass
class Pending:
    """An interrupted transfer: a `.partial` file of `done` bytes toward the object `sha256`."""

    location: str
    path: str
    sha256: str
    size: int
    done: int
    source: str
    route: str


class CacheIndex:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_dir() / "index.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.executescript(_DDL)
        with self._tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            if row is None:
                db.execute("INSERT INTO meta VALUES ('version', ?)", (str(SCHEMA_VERSION),))
            elif int(row[0]) != SCHEMA_VERSION:
                # Never authoritative: an index of another version is rebuilt, not migrated.
                for table in ("copies", "transfers", "routes"):
                    db.execute(f"DELETE FROM {table}")
                db.execute("UPDATE meta SET value = ? WHERE key = 'version'", (str(SCHEMA_VERSION),))

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        self._db.close()

    # Copies

    def record_manifest(self, manifest: Manifest) -> None:
        """Replace what is known under the manifest's root at its location by what the manifest says."""
        root = manifest.root.rstrip("/") + "/"
        now = time.time()
        with self._tx() as db:
            db.execute(
                "DELETE FROM copies WHERE location = ? AND substr(path, 1, ?) = ?",
                (manifest.location, len(root), root),
            )
            db.executemany(
                "INSERT INTO copies VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (manifest.location, root + e.path, e.kind, e.size, e.sha256, now)
                    for e in manifest.files.values()
                    if e.sha256
                ],
            )
            # A transfer whose destination holds the finished object is no longer pending.
            db.execute(
                "DELETE FROM transfers WHERE location = ? AND EXISTS (SELECT 1 FROM copies c WHERE "
                "c.location = transfers.location AND c.path = transfers.path AND c.sha256 = transfers.sha256)",
                (manifest.location,),
            )

    def record_copy(self, copy: Copy) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO copies VALUES (?, ?, ?, ?, ?, ?)",
                (copy.location, copy.path, copy.kind, copy.size, copy.sha256, time.time()),
            )

    def drop(self, location: str, path: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM copies WHERE location = ? AND path = ?", (location, path))

    def copy_at(self, location: str, path: str) -> Copy | None:
        row = self._db.execute(
            "SELECT location, path, kind, size, sha256 FROM copies WHERE location = ? AND path = ?", (location, path)
        ).fetchone()
        return Copy(*row) if row else None

    def copies_under(self, location: str, root: str) -> dict[str, Copy]:
        """Known copies under `root` at `location`, by path relative to it."""
        root = root.rstrip("/") + "/"
        rows = self._db.execute(
            "SELECT location, path, kind, size, sha256 FROM copies WHERE location = ? AND substr(path, 1, ?) = ?",
            (location, len(root), root),
        ).fetchall()
        return {r[1][len(root) :]: Copy(*r) for r in rows}

    def holders(self, sha256: str) -> list[Copy]:
        """Every known copy of an object, wherever it is."""
        rows = self._db.execute(
            "SELECT location, path, kind, size, sha256 FROM copies WHERE sha256 = ? ORDER BY location, path",
            (sha256,),
        ).fetchall()
        return [Copy(*r) for r in rows]

    # Interrupted transfers

    def begin(self, pending: Pending) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pending.location,
                    pending.path,
                    pending.sha256,
                    pending.size,
                    pending.done,
                    pending.source,
                    pending.route,
                    time.time(),
                ),
            )

    def progress(self, location: str, path: str, done: int) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE transfers SET done = ?, updated = ? WHERE location = ? AND path = ?",
                (done, time.time(), location, path),
            )

    def finish(self, location: str, path: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM transfers WHERE location = ? AND path = ?", (location, path))

    def pending(self, location: str | None = None) -> list[Pending]:
        sql = "SELECT location, path, sha256, size, done, source, route FROM transfers"
        rows = (
            self._db.execute(sql + " WHERE location = ?", (location,)).fetchall()
            if location
            else self._db.execute(sql).fetchall()
        )
        return [Pending(*r) for r in rows]

    # Throughput

    def measured(self, route: str, nbytes: int, seconds: float) -> None:
        """Add a transfer's bytes and time to its route's total, which estimates later transfers."""
        if nbytes <= 0 or seconds <= 0:
            return
        with self._tx() as db:
            db.execute(
                "INSERT INTO routes VALUES (?, ?, ?, ?) ON CONFLICT(route) DO UPDATE SET "
                "bytes = bytes + excluded.bytes, seconds = seconds + excluded.seconds, updated = excluded.updated",
                (route, nbytes, seconds, time.time()),
            )

    def throughput(self, route: str) -> float | None:
        """Bytes per second measured on a route, or None before its first transfer."""
        row = self._db.execute("SELECT bytes, seconds FROM routes WHERE route = ?", (route,)).fetchone()
        return row[0] / row[1] if row and row[1] > 0 else None
