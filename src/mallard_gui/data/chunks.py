"""Large objects moved by chunk: Mallard identifies a file over one 64 MiB chunk by its chunk tree (#1329,
PR #1336): `chunks`, the SHA-256 of each `chunk_bytes` chunk in order, and `sha256_tree`, the SHA-256 of
their 32-byte digests. Each chunk verifies on its own, so a transfer runs several streams at once, each a
chunk read at its offset and written at the same offset of the destination's `.partial`, checked where it
landed; an interruption costs at most the chunks in flight, and a resumed transfer skips every chunk whose
hash already matches."""

from __future__ import annotations

import hashlib
import subprocess
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .locations import LocalLocation, Location, SshLocation, TransferError, check_path

STREAMS = 8
HASH_THREADS = 8
TREE_PREFIX = "tree:"  # a FileEntry's sha256 for a file known by its chunk tree

# stdin: lines "OFFSET<TAB>LENGTH"; stdout: one SHA-256 per line ("-" for a range past the file's end).
_CHUNK_HASH = r"""
if command -v sha256sum >/dev/null 2>&1; then h() { sha256sum | cut -d' ' -f1; }
else h() { shasum -a 256 | cut -d' ' -f1; }; fi
size=$(wc -c < "$1" 2>/dev/null || echo 0)
tab=$(printf '\t')
d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT
i=0
while IFS="$tab" read -r o n; do
  if [ $((o + n)) -gt "$size" ]; then echo - > "$d/$i"
  else (tail -c +$((o + 1)) "$1" | head -c "$n" | h > "$d/$i") & fi
  i=$((i + 1))
  [ $((i % 8)) -eq 0 ] && wait
done
wait
j=0
while [ "$j" -lt "$i" ]; do cat "$d/$j"; j=$((j + 1)); done
"""
# Standard input written at byte OFFSET of a file (created if absent), the rest of it untouched.
_WRITE_AT = 'mkdir -p "$(dirname "$1")" && touch "$1" && dd of="$1" bs="$2" seek="$3" conv=notrunc 2>/dev/null'


@dataclass
class ChunkTree:
    chunk_bytes: int
    chunks: list[str]
    root: str

    def verify(self) -> bool:
        return tree_root(self.chunks) == self.root

    def spans(self, size: int) -> list[tuple[int, int, int]]:
        """(index, offset, length) of every chunk of a file of `size` bytes, the last possibly shorter."""
        out = []
        for i in range(len(self.chunks)):
            off = i * self.chunk_bytes
            out.append((i, off, min(self.chunk_bytes, size - off)))
        return out


def tree_root(chunks: Sequence[str]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(bytes.fromhex(c))
    return h.hexdigest()


def tree_of_file(path: Path, chunk_bytes: int) -> ChunkTree:
    """A local file's chunk tree, as Mallard computes it."""
    chunks = []
    with path.open("rb") as f:
        while chunk := f.read(chunk_bytes):
            chunks.append(hashlib.sha256(chunk).hexdigest())
    return ChunkTree(chunk_bytes, chunks, tree_root(chunks))


def chunk_hashes(location: Location, path: str, spans: Sequence[tuple[int, int, int]]) -> list[str | None]:
    """The SHA-256 of byte ranges of a file at a location, all in one process there."""
    if isinstance(location, LocalLocation):
        p = Path(path)
        size = p.stat().st_size if p.is_file() else -1

        def one(span: tuple[int, int, int]) -> str | None:
            _, off, n = span
            if off + n > size:
                return None
            h = hashlib.sha256()
            with p.open("rb") as f:
                f.seek(off)
                while n > 0 and (data := f.read(min(n, 1 << 22))):
                    h.update(data)
                    n -= len(data)
            return h.hexdigest()

        with ThreadPoolExecutor(max_workers=HASH_THREADS) as pool:  # hashlib releases the GIL
            return list(pool.map(one, spans))
    if isinstance(location, SshLocation):
        lines = "".join(f"{off}\t{n}\n" for _, off, n in spans)
        got = location.conn.run(["sh", "-c", _CHUNK_HASH, "sh", check_path(path)], stdin=lines.encode()).text
        digests = got.split()
        return [None if d == "-" else d for d in digests] + [None] * (len(spans) - len(digests))
    raise TransferError(f"{location.name}: no chunk hashing for this kind of location")


def tree_at(location: Location, path: str, chunk_bytes: int) -> str | None:
    """The chunk tree's root of a file at a location, None if it is missing."""
    size = location.sizes([path]).get(path)
    if size is None:
        return None
    spans = [(i, off, min(chunk_bytes, size - off)) for i, off in enumerate(range(0, size, chunk_bytes))]
    got = chunk_hashes(location, path, spans)
    return None if None in got else tree_root([h for h in got if h])


class _LocalWriter:
    def __init__(self, path: str, offset: int) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
        self.f = p.open("r+b")
        self.f.seek(offset)

    def write(self, data: bytes) -> None:
        self.f.write(data)

    def close(self) -> None:
        self.f.close()


class _SshWriter:
    def __init__(self, location: SshLocation, path: str, offset: int, block: int) -> None:
        self.proc = location.conn.popen(
            ["sh", "-c", _WRITE_AT, "sh", check_path(path), str(block), str(offset // block)], stdin=subprocess.PIPE
        )

    def write(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            err = self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else ""
            raise TransferError(f"writing a chunk: {err.strip()}")


def _writer(location: Location, path: str, offset: int, block: int) -> _LocalWriter | _SshWriter:
    if isinstance(location, LocalLocation):
        return _LocalWriter(path, offset)
    if isinstance(location, SshLocation):
        return _SshWriter(location, path, offset, block)
    raise TransferError(f"{location.name}: no positional writes for this kind of location")


@dataclass
class ChunkReport:
    moved: int = 0  # bytes
    chunks_moved: int = 0
    chunks_kept: int = 0  # already at the destination with the right hash
    retried: int = 0


def copy_chunks(
    src: Location,
    src_path: str,
    dst: Location,
    target: str,
    size: int,
    tree: ChunkTree,
    streams: int = STREAMS,
    block: int = 1 << 20,
    on_bytes: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
) -> ChunkReport:
    """Make `target` at `dst` hold the object, chunks moved by `streams` at once and each verified where it
    landed (one retry), skipping chunks already there. Raises TransferError if any chunk stays wrong."""
    if not tree.verify():
        raise TransferError("the chunk list does not hash to the recorded tree")
    spans = tree.spans(size)
    if len(spans) != -(-size // tree.chunk_bytes):
        raise TransferError(f"{len(spans)} chunks of {tree.chunk_bytes} bytes cannot make {size} bytes")
    if (dst.sizes([target]).get(target) or 0) > size:
        dst.remove([target])
    if tree.chunk_bytes % block:
        block = tree.chunk_bytes  # positional writes in whole blocks
    have = chunk_hashes(dst, target, spans)
    todo = [s for s, h in zip(spans, have, strict=True) if h != tree.chunks[s[0]]]
    report = ChunkReport(chunks_kept=len(spans) - len(todo))
    lock = threading.Lock()

    def move(span: tuple[int, int, int]) -> None:
        i, off, n = span
        for attempt in range(2):
            if cancel is not None and cancel.is_set():
                return
            reader = src.reader(src_path, off, n)
            writer = _writer(dst, target, off, block)
            moved = 0
            try:
                while moved < n:
                    data = reader.read(min(1 << 20, n - moved))
                    if not data:
                        break
                    writer.write(data)
                    moved += len(data)
                    if on_bytes:
                        with lock:
                            on_bytes(len(data))
            finally:
                reader.close()
                writer.close()
            if chunk_hashes(dst, target, [span])[0] == tree.chunks[i]:
                with lock:
                    report.moved += moved
                    report.chunks_moved += 1
                return
            with lock:
                report.retried += attempt == 0
        raise TransferError(f"chunk {i} at {dst.label(target)}: its SHA-256 does not match after a retry")

    with ThreadPoolExecutor(max_workers=max(1, streams)) as pool:
        for future in [pool.submit(move, s) for s in todo]:
            future.result()
    if cancel is not None and cancel.is_set():
        return report
    # Every chunk was verified where it landed, or found there before the copy: only the length is left.
    if dst.sizes([target]).get(target) != size:
        raise TransferError(f"{dst.label(target)}: not {size} bytes after the copy")
    return report
