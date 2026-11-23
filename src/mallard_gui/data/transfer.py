"""The transfer engine: "put the object with SHA-256 H at this path of location L, from a location that has
it", and, for a stream, "append its bytes N onward".

Every object lands as `NAME.partial`, resumes from that file's length (from any source holding the same
hash), and is renamed only once its SHA-256, computed at the destination, matches. A stream is appended
in place after its prefix is checked against the source's, and its new prefix hash is checked after.

Routes: `local` (both ends on this machine), `stream` (this machine and a remote, through SSH or a
Kubernetes helper pod), `funnel` (remote to remote through this machine's memory, never its disk), and
between two SSH remotes once a probe found the way: `direct` (the source pushes into the destination over
SSH with the forwarded agent), `pull` (the destination pulls from the source the same way) and `shared`
(the source's storage is mounted at the destination too: a copy on the destination, no network).
An object Mallard recorded by its chunk tree (#1329) moves by chunk instead, several streams at once
through this machine (chunks.py), whatever the route; its `.partial` resumes chunk by chunk.
TODO: rsync `--partial --append-verify` over the master connection where both ends have it.
"""

from __future__ import annotations

import shlex
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .cache import CacheIndex, Copy, Pending
from .chunks import STREAMS, ChunkTree, chunk_hashes, copy_chunks
from .locations import BLOCK, Location, SshLocation, TransferError, join
from .manifest import DERIVED, LIVE, OBJECT, PARTIAL, STREAM, FileEntry

ROUTES = ("local", "stream", "funnel", "direct", "pull", "shared")
REMOTE_ROUTES = ("direct", "pull", "shared")  # run on one of two SSH remotes
PARTIAL_SUFFIX = ".partial"


class Cancelled(Exception):
    pass


@dataclass
class Endpoint:
    location: Location
    root: str

    def path(self, rel: str) -> str:
        return join(self.root, rel)

    @property
    def label(self) -> str:
        return self.location.label(self.root)


@dataclass
class Item:
    entry: FileEntry
    source: Endpoint
    dst_path: str
    mode: str = "object"  # object (via .partial) | append (a stream, in place)
    offset: int = 0  # bytes already at the destination: a partial's, or a stream copy's
    skip: str = ""  # why nothing moves: present, rewritten until its run ends, ...

    @property
    def remaining(self) -> int:
        return 0 if self.skip else self.entry.size - self.offset


@dataclass
class Plan:
    destination: Endpoint
    route: str
    items: list[Item]
    throughput: float | None = None  # bytes/s measured on this route before
    peer: str = ""  # direct: the destination's host as the source reaches it; pull: the source's, from the destination

    @property
    def moving(self) -> list[Item]:
        return [i for i in self.items if not i.skip]

    @property
    def skipped(self) -> list[Item]:
        return [i for i in self.items if i.skip]

    @property
    def bytes(self) -> int:
        return sum(i.remaining for i in self.items)

    @property
    def resumed(self) -> int:
        return sum(i.offset for i in self.moving)

    @property
    def bytes_skipped(self) -> int:
        return sum(i.entry.size for i in self.skipped)

    @property
    def seconds(self) -> float | None:
        return self.bytes / self.throughput if self.throughput else None


@dataclass
class Report:
    moved: list[str] = field(default_factory=list)
    bytes: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


Progress = Callable[[int, int, str], None]  # bytes done, bytes total, current file


def route_key(src: Location, dst: Location, route: str) -> str:
    return f"{src.id}>{dst.id}:{route}"


def default_route(src: Location, dst: Location, direct: bool = False, pull: bool = False, shared: bool = False) -> str:
    """The cheapest route: no network over a shared filesystem, one hop over two, two hops through here."""
    if src.kind == "local" and dst.kind == "local":
        return "local"
    if src.kind == "local" or dst.kind == "local":
        return "stream"
    if isinstance(src, SshLocation) and isinstance(dst, SshLocation):
        if shared:
            return "shared"
        if direct:
            return "direct"
        if pull:
            return "pull"
    return "funnel"


class TransferEngine:
    def __init__(self, cache: CacheIndex, block: int = BLOCK, streams: int = STREAMS) -> None:
        self.cache = cache
        self.block = block
        self.streams = streams

    # Planning

    def plan(
        self, entries: Sequence[FileEntry], sources: Sequence[Endpoint], dst: Endpoint, route: str, peer: str = ""
    ) -> Plan:
        """What would move: objects the destination holds are skipped, partials and stream prefixes resume.

        `sources` are endpoints holding the run's files, preferred first; an object comes from the first
        one the cache index says holds it, else from the first (the run's home, whose manifest this is)."""
        if not sources:
            raise ValueError("no source")
        home = sources[0]
        if route not in ROUTES:
            raise ValueError(f"route {route!r}: one of {', '.join(ROUTES)}")
        items = []
        for e in entries:
            src = home
            if e.kind == OBJECT:
                src = next((s for s in sources if self._holds(s, e)), home)
            item = Item(e, src, dst.path(e.path), mode="append" if e.kind == STREAM else "object")
            if e.kind == PARTIAL:
                item.skip = "unfinished write at the source"
            elif e.kind == LIVE:
                item.skip = "rewritten until its run ends"
            elif not e.sha256:
                item.skip = "not hashed"
            elif src.location.id == dst.location.id and src.path(e.path) == item.dst_path:
                item.skip = "present"
            items.append(item)
        for i in items:
            if route in REMOTE_ROUTES and not (
                isinstance(i.source.location, SshLocation) and isinstance(dst.location, SshLocation)
            ):
                raise ValueError(f"the {route} route runs between two SSH remotes")
        self._resume_points(items, dst)
        throughput = self.cache.throughput(route_key(home.location, dst.location, route))
        return Plan(dst, route, items, throughput, peer)

    def _holds(self, src: Endpoint, e: FileEntry) -> bool:
        copy = self.cache.copy_at(src.location.id, src.path(e.path))
        return copy is not None and copy.sha256 == e.sha256

    def _resume_points(self, items: list[Item], dst: Endpoint) -> None:
        """Where each item starts: one round trip for the destination's sizes, one per side for hashes."""
        live = [i for i in items if not i.skip]
        paths = [i.dst_path for i in live] + [i.dst_path + PARTIAL_SUFFIX for i in live if i.mode == "object"]
        sizes = dst.location.sizes(paths)
        dst_hash: list[tuple[str, int | None]] = []
        src_hash: dict[str, list[tuple[str, int | None]]] = {}
        for i in live:
            have = sizes.get(i.dst_path)
            if i.mode == "object" and i.entry.chunks:
                self._resume_chunked(i, dst, have, sizes.get(i.dst_path + PARTIAL_SUFFIX))
                continue
            if i.mode == "object":
                if have == i.entry.size:
                    known = self.cache.copy_at(dst.location.id, i.dst_path)
                    if known is not None and known.sha256 == i.entry.sha256:
                        i.skip = "present"
                        continue
                    dst_hash.append((i.dst_path, None))
                part = sizes.get(i.dst_path + PARTIAL_SUFFIX)
                i.offset = part if part is not None and part <= i.entry.size else 0
            elif have:
                # A stream copy: the source's prefix of the same length must match it.
                n = min(have, i.entry.size)
                dst_hash.append((i.dst_path, n))
                if n < i.entry.size:
                    src_hash.setdefault(i.source.location.id, []).append((i.source.path(i.entry.path), n))
                i.offset = n
        got = dst.location.hash_files(dst_hash)
        src_got: dict[str, str | None] = {}
        for i in live:
            if i.source.location.id in src_hash:
                src_got |= i.source.location.hash_files(src_hash.pop(i.source.location.id))
        for i in live:
            if i.skip:
                continue
            if i.mode == "object" and i.dst_path in got:
                if got[i.dst_path] == i.entry.sha256:
                    i.skip = "present"
                    self.cache.record_copy(Copy(dst.location.id, i.dst_path, OBJECT, i.entry.size, i.entry.sha256))
            elif i.mode == "append" and i.offset:
                expect = i.entry.sha256 if i.offset == i.entry.size else src_got.get(i.source.path(i.entry.path))
                if got.get(i.dst_path) != expect:
                    # Another stream at that path (a fresh start): replaced whole.
                    i.mode, i.offset = "object", 0
                elif i.offset == i.entry.size:
                    i.skip = "present"

    def _resume_chunked(self, i: Item, dst: Endpoint, have: int | None, part: int | None) -> None:
        """A chunked object is present when every chunk at its path matches; else its partial's matching
        chunks stay, wherever they are in the file."""
        e = i.entry
        known = self.cache.copy_at(dst.location.id, i.dst_path)
        if have == e.size and known is not None and known.sha256 == e.sha256:
            i.skip = "present"
            return
        spans = _tree(e).spans(e.size)
        if have == e.size and chunk_hashes(dst.location, i.dst_path, spans) == e.chunks:
            i.skip = "present"
            self.cache.record_copy(Copy(dst.location.id, i.dst_path, OBJECT, e.size, e.sha256))
            return
        if part:
            got = chunk_hashes(dst.location, i.dst_path + PARTIAL_SUFFIX, spans)
            i.offset = sum(n for (k, _, n), h in zip(spans, got, strict=True) if h == e.chunks[k])

    # Running

    def run(self, plan: Plan, progress: Progress | None = None, cancel: threading.Event | None = None) -> Report:
        report = Report()
        total = plan.bytes
        done = 0
        start = time.monotonic()
        dst = plan.destination.location
        for item in plan.moving:
            label = item.entry.path

            def on_bytes(n: int, label: str = label) -> None:
                nonlocal done
                done += n
                if progress:
                    progress(done, total, label)

            t0 = time.monotonic()
            try:
                moved = self._move(item, plan, on_bytes, cancel)
            except Cancelled:
                report.cancelled = True
                break
            except (TransferError, OSError, RuntimeError) as e:
                report.errors.append(f"{label}: {e}")
                continue
            report.moved.append(label)
            report.bytes += moved
            self.cache.measured(route_key(item.source.location, dst, plan.route), moved, time.monotonic() - t0)
        report.seconds = time.monotonic() - start
        return report

    def _move(self, item: Item, plan: Plan, on_bytes: Callable[[int], None], cancel: threading.Event | None) -> int:
        e, src, dst = item.entry, item.source, plan.destination.location
        target = item.dst_path + PARTIAL_SUFFIX if item.mode == "object" else item.dst_path
        length = e.size - item.offset
        self.cache.begin(Pending(dst.id, item.dst_path, e.sha256, e.size, item.offset, src.location.id, plan.route))
        if item.mode == "object" and e.chunks:
            return self._move_chunked(item, dst, target, on_bytes, cancel)
        if item.mode == "object" and item.offset == 0:
            dst.remove([target])
        self._copy(
            src.location,
            src.path(e.path),
            item.offset,
            length,
            dst,
            target,
            plan.route,
            on_bytes,
            cancel,
            item.dst_path,
            plan.peer,
        )
        got = dst.hash_files([(target, e.size)])[target]
        if got != e.sha256:
            if item.mode == "object":
                dst.remove([target])
            self.cache.finish(dst.id, item.dst_path)
            raise TransferError(f"SHA-256 at {dst.label(target)} is {got}, not {e.sha256}")
        if item.mode == "object":
            dst.rename(target, item.dst_path)
        kind = STREAM if e.kind == STREAM else OBJECT if e.kind != DERIVED else DERIVED
        self.cache.record_copy(Copy(dst.id, item.dst_path, kind, e.size, e.sha256))
        self.cache.finish(dst.id, item.dst_path)
        return length

    def _move_chunked(
        self, item: Item, dst: Location, target: str, on_bytes: Callable[[int], None], cancel: threading.Event | None
    ) -> int:
        e, src = item.entry, item.source
        try:
            tree = _tree(e)
            report = copy_chunks(
                src.location, src.path(e.path), dst, target, e.size, tree, self.streams, 1 << 20, on_bytes, cancel
            )
        except TransferError:
            self.cache.finish(dst.id, item.dst_path)
            raise
        if cancel is not None and cancel.is_set():
            raise Cancelled
        dst.rename(target, item.dst_path)
        self.cache.record_copy(Copy(dst.id, item.dst_path, OBJECT, e.size, e.sha256))
        self.cache.finish(dst.id, item.dst_path)
        return report.moved

    def _copy(
        self,
        src: Location,
        src_path: str,
        offset: int,
        length: int,
        dst: Location,
        target: str,
        route: str,
        on_bytes: Callable[[int], None],
        cancel: threading.Event | None,
        key: str,
        peer: str = "",
    ) -> None:
        if length <= 0:
            return
        if route in REMOTE_ROUTES:
            assert isinstance(src, SshLocation) and isinstance(dst, SshLocation)
            if route == "direct":
                runs_on, cmd = src, src.direct_command(src_path, offset, length, peer or dst.conn.host, target)
            elif route == "pull":
                runs_on, cmd = dst, dst.pull_command(peer or src.conn.host, src_path, offset, length, target)
            else:
                runs_on, cmd = dst, dst.shared_command(src_path, offset, length, target)
            argv = [*runs_on.conn.ssh_args(forward_agent=route != "shared"), shlex.join(cmd)]
            result = runs_on.conn.runner(argv, None)
            if result.returncode != 0:
                err = result.stderr.decode(errors="replace").strip()
                raise TransferError(f"{route} {src.name} to {dst.name}: {err or f'exit {result.returncode}'}")
            on_bytes(length)
            return
        reader = src.reader(src_path, offset, length)
        sink = dst.appender(target)
        moved = 0
        last = time.monotonic()
        try:
            while moved < length:
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                chunk = reader.read(min(self.block, length - moved))
                if not chunk:
                    break
                sink.write(chunk)
                moved += len(chunk)
                on_bytes(len(chunk))
                if time.monotonic() - last > 0.5:
                    self.cache.progress(dst.id, key, offset + moved)
                    last = time.monotonic()
        except Cancelled:
            # Let what was sent land, so the partial keeps it and the next run resumes after it.
            reader.close()
            sink.close()
            self.cache.progress(dst.id, key, offset + moved)
            raise
        except BaseException:
            sink.abort()
            reader.close()
            self.cache.progress(dst.id, key, offset + moved)
            raise
        reader.close()
        sink.close()
        self.cache.progress(dst.id, key, offset + moved)
        if moved != length:
            raise TransferError(f"{src.label(src_path)}: {moved} of {length} bytes read")


def _tree(e: FileEntry) -> ChunkTree:
    return ChunkTree(e.chunk_bytes, e.chunks, e.sha256.removeprefix("tree:"))
