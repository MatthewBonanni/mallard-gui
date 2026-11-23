"""Places files live: this machine, or a remote reached over SSH or through a Kubernetes helper pod.

A location answers in one round trip whatever touches many files (listing, hashing), so nothing is a
per-file request over SSH, and streams bytes by offset for transfers.

TODO(#1317): `mallard files RUN_DIR --hash` on the location replaces the listing and hashing scripts
below, which need only a POSIX shell, `find`, `wc`, `head`, `tail` and `sha256sum` or `shasum`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import posixpath
import shlex
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Protocol

from ..backends.ssh import RemoteError, Result, SshConnection

BLOCK = 1 << 20
LOCAL = "local"

_LIST = r"""
cd "$1" 2>/dev/null || exit 0
find . -type f | while IFS= read -r f; do
  s=$(wc -c < "$f") && printf '%s\t%s\n' $s "${f#./}"
done
"""

# stdin: lines "LENGTH<TAB>PATH" (LENGTH "-" for the whole file); stdout: "SHA256<TAB>PATH", "-" if absent.
_HASH = r"""
if command -v sha256sum >/dev/null 2>&1; then h() { sha256sum | cut -d' ' -f1; }
else h() { shasum -a 256 | cut -d' ' -f1; }; fi
tab=$(printf '\t')
while IFS="$tab" read -r n f; do
  if [ ! -f "$f" ]; then printf -- '-\t%s\n' "$f"; continue; fi
  if [ "$n" = "-" ]; then d=$(h < "$f"); else d=$(head -c "$n" "$f" | h); fi
  printf '%s\t%s\n' "$d" "$f"
done
"""

_SIZES = r"""
for f in "$@"; do
  if [ -f "$f" ]; then s=$(wc -c < "$f"); printf '%s\t%s\n' $s "$f"; else printf -- '-\t%s\n' "$f"; fi
done
"""

# Bytes [offset, offset + length) of a file: tail and head read only what is sent.
_READ = 'tail -c +"$2" "$1" | head -c "$3"'
_APPEND = 'mkdir -p "$(dirname "$1")" && cat >> "$1"'


class TransferError(RuntimeError):
    pass


class Source(Protocol):
    def read(self, n: int) -> bytes: ...

    def close(self) -> None: ...


class Sink(Protocol):
    def write(self, data: bytes) -> None: ...

    def close(self) -> None:
        """Finish the write; raises TransferError if the bytes did not all land."""

    def abort(self) -> None: ...


def check_path(path: str) -> str:
    if "\n" in path or "\t" in path:
        raise ValueError(f"{path!r}: tabs and newlines in paths are not supported")
    return path


class Location(ABC):
    """Files under absolute POSIX paths at one place, `local` or a remote's name and id."""

    name: str
    id: str
    kind: str

    def label(self, path: str) -> str:
        return f"{self.name}:{path}"

    @abstractmethod
    def list_files(self, root: str) -> dict[str, int]:
        """Every file under `root`, by path relative to it, with its size; empty if `root` does not exist."""

    @abstractmethod
    def hash_files(self, items: Sequence[tuple[str, int | None]]) -> dict[str, str | None]:
        """The SHA-256 of each file (of its first N bytes when N is given); None for a missing file."""

    @abstractmethod
    def sizes(self, paths: Sequence[str]) -> dict[str, int | None]: ...

    @abstractmethod
    def read_bytes(self, path: str, offset: int = 0) -> bytes:
        """A small file's bytes from `offset` on (a run log, a CSV); empty if it does not exist."""

    @abstractmethod
    def reader(self, path: str, offset: int, length: int) -> Source: ...

    @abstractmethod
    def appender(self, path: str) -> Sink: ...

    @abstractmethod
    def rename(self, src: str, dst: str) -> None: ...

    @abstractmethod
    def remove(self, paths: Sequence[str]) -> None: ...


class LocalLocation(Location):
    kind = "local"

    def __init__(self) -> None:
        self.name = LOCAL
        self.id = LOCAL

    def list_files(self, root: str) -> dict[str, int]:
        base = Path(root)
        if not base.is_dir():
            return {}
        out = {}
        for dirpath, _, names in os.walk(base):
            for n in names:
                p = Path(dirpath) / n
                if p.is_file():
                    out[p.relative_to(base).as_posix()] = p.stat().st_size
        return out

    def hash_files(self, items: Sequence[tuple[str, int | None]]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for path, length in items:
            out[path] = sha256_of(Path(path), length) if Path(path).is_file() else None
        return out

    def sizes(self, paths: Sequence[str]) -> dict[str, int | None]:
        return {p: Path(p).stat().st_size if Path(p).is_file() else None for p in paths}

    def read_bytes(self, path: str, offset: int = 0) -> bytes:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                return f.read()
        except FileNotFoundError:
            return b""

    def reader(self, path: str, offset: int, length: int) -> Source:
        f = open(path, "rb")  # noqa: SIM115  closed by the reader
        f.seek(offset)
        return _LimitedFile(f, length)

    def appender(self, path: str) -> Sink:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return _FileSink(open(path, "ab"))

    def rename(self, src: str, dst: str) -> None:
        os.replace(src, dst)

    def remove(self, paths: Sequence[str]) -> None:
        for p in paths:
            Path(p).unlink(missing_ok=True)


class Shell(Protocol):
    """Runs commands where a location's files are: `ssh` to a login node, or `kubectl exec` in a helper pod."""

    def run(self, argv: Sequence[str], stdin: bytes | None = None, check: bool = True) -> Result: ...

    def popen(
        self, argv: Sequence[str], stdin: int | None = None, stdout: int | None = None, forward_agent: bool = False
    ) -> subprocess.Popen[bytes]: ...


class ShellLocation(Location):
    """Files reached through a shell: every operation is a POSIX shell script run there."""

    kind = "shell"

    def __init__(self, name: str, remote_id: str, conn: Shell) -> None:
        self.name = name
        self.id = remote_id
        self.conn = conn

    def _sh(self, script: str, *args: str, stdin: bytes | None = None) -> str:
        return self.conn.run(["sh", "-c", script, "sh", *args], stdin=stdin).text

    def list_files(self, root: str) -> dict[str, int]:
        out = {}
        for line in self._sh(_LIST, check_path(root)).splitlines():
            size, _, path = line.partition("\t")
            if size.isdigit() and path:
                out[path] = int(size)
        return out

    def hash_files(self, items: Sequence[tuple[str, int | None]]) -> dict[str, str | None]:
        if not items:
            return {}
        lines = "".join(f"{'-' if n is None else n}\t{check_path(p)}\n" for p, n in items)
        out: dict[str, str | None] = {p: None for p, _ in items}
        for line in self._sh(_HASH, stdin=lines.encode()).splitlines():
            digest, _, path = line.partition("\t")
            if path in out:
                out[path] = None if digest == "-" else digest
        return out

    def sizes(self, paths: Sequence[str]) -> dict[str, int | None]:
        if not paths:
            return {}
        out: dict[str, int | None] = dict.fromkeys(paths)
        for line in self._sh(_SIZES, *map(check_path, paths)).splitlines():
            size, _, path = line.partition("\t")
            if path in out and size.isdigit():
                out[path] = int(size)
        return out

    def read_bytes(self, path: str, offset: int = 0) -> bytes:
        script = 'test -f "$1" && tail -c +"$2" "$1" || true'
        return self.conn.run(["sh", "-c", script, "sh", check_path(path), str(offset + 1)]).stdout

    def reader(self, path: str, offset: int, length: int) -> Source:
        proc = self.conn.popen(
            ["sh", "-c", _READ, "sh", check_path(path), str(offset + 1), str(length)], stdout=subprocess.PIPE
        )
        return _ProcSource(proc, self.label(path))

    def appender(self, path: str) -> Sink:
        proc = self.conn.popen(["sh", "-c", _APPEND, "sh", check_path(path)], stdin=subprocess.PIPE)
        return _ProcSink(proc, self.label(path))

    def rename(self, src: str, dst: str) -> None:
        self.conn.run(["mv", "-f", check_path(src), check_path(dst)])

    def remove(self, paths: Sequence[str]) -> None:
        if paths:
            self.conn.run(["rm", "-f", "--", *map(check_path, paths)])


class SshLocation(ShellLocation):
    kind = "ssh"
    conn: SshConnection

    def __init__(self, name: str, remote_id: str, conn: SshConnection) -> None:
        super().__init__(name, remote_id, conn)

    def direct_command(self, src: str, offset: int, length: int, dst_host: str, dst_path: str) -> list[str]:
        """The command run here that pipes a byte range straight into a file on another remote."""
        append = shlex.join(["sh", "-c", _APPEND, "sh", check_path(dst_path)])
        remote = shlex.join(["ssh", "-o", "BatchMode=yes", dst_host, append])
        return ["sh", "-c", f"{_READ} | {remote}", "sh", check_path(src), str(offset + 1), str(length)]

    def pull_command(self, src_host: str, src: str, offset: int, length: int, dst_path: str) -> list[str]:
        """The command run here that pulls a byte range of a file on another remote into a file here."""
        read = shlex.join(["sh", "-c", _READ, "sh", check_path(src), str(offset + 1), str(length)])
        remote = shlex.join(["ssh", "-o", "BatchMode=yes", src_host, read])
        return ["sh", "-c", f"{remote} | {{ {_APPEND}; }}", "sh", check_path(dst_path)]

    def shared_command(self, src: str, offset: int, length: int, dst_path: str) -> list[str]:
        """The command run here that copies a byte range between two paths of this remote's filesystems (another
        remote's storage mounted here too): no network at all."""
        return [
            "sh",
            "-c",
            f'tail -c +"$2" "$1" | head -c "$3" | {{ {_APPEND.replace("$1", "$4")}; }}',
            "sh",
            check_path(src),
            str(offset + 1),
            str(length),
            check_path(dst_path),
        ]

    def has_file(self, path: str) -> bool:
        return self.conn.run(["test", "-f", check_path(path)], check=False).returncode == 0

    def can_reach(self, host: str) -> bool:
        """Whether this remote reaches `host` by SSH with the forwarded agent, as a direct route needs."""
        result = self.conn.runner(
            [*self.conn.ssh_args(forward_agent=True), shlex.join(["ssh", "-o", "BatchMode=yes", host, "true"])], None
        )
        return result.returncode == 0


def sha256_of(path: Path, length: int | None = None) -> str:
    h = hashlib.sha256()
    left = length
    with path.open("rb") as f:
        while left is None or left > 0:
            chunk = f.read(BLOCK if left is None else min(BLOCK, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def join(root: str, rel: str) -> str:
    return posixpath.normpath(posixpath.join(root, rel))


class _LimitedFile:
    def __init__(self, f: IO[bytes], length: int) -> None:
        self.f, self.left = f, length

    def read(self, n: int) -> bytes:
        if self.left <= 0:
            return b""
        data = self.f.read(min(n, self.left))
        self.left -= len(data)
        return data

    def close(self) -> None:
        self.f.close()


class _FileSink:
    def __init__(self, f: IO[bytes]) -> None:
        self.f = f

    def write(self, data: bytes) -> None:
        self.f.write(data)

    def close(self) -> None:
        self.f.close()

    def abort(self) -> None:
        self.f.close()


class _ProcSource:
    def __init__(self, proc: subprocess.Popen[bytes], label: str) -> None:
        self.proc, self.label = proc, label

    def read(self, n: int) -> bytes:
        assert self.proc.stdout is not None
        return self.proc.stdout.read(n)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()
        if self.proc.stderr:
            self.proc.stderr.close()


class _ProcSink:
    def __init__(self, proc: subprocess.Popen[bytes], label: str) -> None:
        self.proc, self.label = proc, label

    def write(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(data)
        except BrokenPipeError as e:
            raise TransferError(f"{self.label}: {self._stderr()}") from e

    def _stderr(self) -> str:
        self.proc.wait()
        err = self.proc.stderr.read().decode("utf-8", "replace").strip() if self.proc.stderr else ""
        return err or f"exit {self.proc.returncode}"

    def close(self) -> None:
        assert self.proc.stdin is not None
        with contextlib.suppress(BrokenPipeError):
            self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise TransferError(f"{self.label}: {self._stderr()}")
        if self.proc.stderr:
            self.proc.stderr.close()

    def abort(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for f in (self.proc.stdin, self.proc.stderr):
            if f:
                with contextlib.suppress(BrokenPipeError):
                    f.close()


__all__ = [
    "LOCAL",
    "LocalLocation",
    "Location",
    "RemoteError",
    "Shell",
    "ShellLocation",
    "SshLocation",
    "TransferError",
    "sha256_of",
]
