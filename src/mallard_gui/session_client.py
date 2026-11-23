"""The GUI's side of `mallard session` (Mallard's docs/design/session.md, #1340): JSON-RPC 2.0, one message per
line on the session's standard input and output, every result a versioned schema. Here: the client and the
query service for cuts and picks at full resolution (`mesh.cut`, `mesh.pick`), which the design makes the
session's job; bulk results are files next to the data, which the viewer reads there.

A cut is progressive: a decimated one first, then the full one, each cancellable (`$/cancel`) when the plane
moves again."""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import subprocess
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PROTOCOL = "2.0"
CLIENT = {"name": "mallard-gui", "version": "0.1.0"}
# The schemas and major versions this GUI reads; initialize refuses a session that cannot give them.
REQUIRE = {"mallard-capabilities": 1, "mallard-case": 1, "mallard-mesh-cut": 1, "mallard-mesh-pick": 1}
CANCELLED = -32800


class SessionError(RuntimeError):
    def __init__(self, message: str, code: int = 0, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass
class Cut:
    file: str
    triangles: int
    cells: int
    target: int  # 0: full resolution


@dataclass
class Pick:
    kind: str
    id: int
    zone: str
    center: list[float]
    measure: float | None
    quality: dict[str, float] = field(default_factory=dict)
    rank: int | None = None


def _check_schema(result: Any, schema: str) -> dict[str, Any]:
    if not isinstance(result, dict) or result.get("schema") != schema:
        got = result.get("schema") if isinstance(result, dict) else type(result).__name__
        raise SessionError(f"expected {schema}, got {got}")
    if int(result.get("version", 0)) > REQUIRE.get(schema, 1):
        raise SessionError(f"{schema} version {result['version']} is newer than this GUI reads")
    return result


def method_names(capabilities: Mapping[str, Any]) -> set[str]:
    """The requests a session takes: `initialize`'s `methods`, each `{name, command, result}` (or a bare name)."""
    out = set()
    for m in capabilities.get("methods") or []:
        name = m.get("name") if isinstance(m, Mapping) else m
        if isinstance(name, str):
            out.add(name)
    return out


class SessionClient:
    """A running session over a pipe (a local process, or `ssh host [srun ...] mallard session`)."""

    def __init__(self, proc: subprocess.Popen[bytes], timeout: float = 120.0) -> None:
        self.proc = proc
        self.timeout = timeout
        self._next = 1
        self._buffer = b""
        self._lock = threading.Lock()
        self.progress: Callable[[dict[str, Any]], None] | None = None
        self.capabilities: dict[str, Any] = {}

    @classmethod
    def start(
        cls,
        argv: Sequence[str],
        cwd: str | None = None,
        timeout: float = 120.0,
        env: Mapping[str, str] | None = None,
    ) -> SessionClient:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        client = cls(proc, timeout)
        try:
            client.initialize()
        except BaseException:
            client.kill()
            raise
        return client

    def initialize(self) -> dict[str, Any]:
        caps = _check_schema(self.request("initialize", {"client": CLIENT, "require": REQUIRE}), "mallard-capabilities")
        self.capabilities = caps
        return caps

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": PROTOCOL, **message}, separators=(",", ":")).encode() + b"\n")
        self.proc.stdin.flush()

    def _read(self, deadline: float) -> dict[str, Any]:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SessionError("the session did not answer in time")
                if sel.select(min(left, 1.0)):
                    chunk = os.read(fd, 1 << 16)
                    if not chunk:
                        raise SessionError(f"the session exited ({self.proc.poll()})")
                    self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        message: dict[str, Any] = json.loads(line)
        return message

    def send_request(self, method: str, params: dict[str, Any]) -> int:
        with self._lock:
            rid = self._next
            self._next += 1
            self._send({"id": rid, "method": method, "params": params})
        return rid

    def wait(self, rid: int, timeout: float | None = None) -> Any:
        """The result of request `rid`; notifications read meanwhile go to `progress`."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            message = self._read(deadline)
            if "id" not in message:
                if message.get("method") == "$/progress" and self.progress is not None:
                    self.progress(message.get("params") or {})
                continue
            if message["id"] != rid:
                continue  # an answer to a request already given up on
            if "error" in message:
                err = message["error"]
                raise SessionError(str(err.get("message", "error")), int(err.get("code", 0)), err.get("data"))
            return message.get("result")

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        return self.wait(self.send_request(method, params or {}), timeout)

    def cancel(self, rid: int) -> None:
        with self._lock:
            self._send({"method": "$/cancel", "params": {"id": rid}})

    def close(self) -> None:
        with contextlib.suppress(SessionError, OSError, ValueError):
            self.request("shutdown", timeout=10)
        if self.proc.stdin:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


class QueryService:
    """Cuts and picks at full resolution, from a session holding the case's mesh (and, optionally, a snapshot)."""

    def __init__(self, client: SessionClient, out_dir: str) -> None:
        methods = method_names(client.capabilities)
        missing = [m for m in ("mesh.cut", "mesh.pick") if methods and m not in methods]
        if missing:
            raise SessionError(f"the session has no {', '.join(missing)}")
        self.client = client
        self.out_dir = out_dir
        self.case: dict[str, Any] = {}

    def open(self, input_path: str) -> dict[str, Any]:
        self.case = _check_schema(self.client.request("case.open", {"input": input_path}), "mallard-case")
        return self.case

    def cut(
        self, origin: Sequence[float], normal: Sequence[float], targets: Sequence[int] = (200_000, 0)
    ) -> Generator[Cut, None, None]:
        """The cut through the volume on a plane: decimated to each target in turn, 0 meaning full
        resolution. Stop iterating to stop; the request in flight is cancelled."""
        plane = {"origin": [float(c) for c in origin], "normal": [float(c) for c in normal]}
        for target in targets:
            params: dict[str, Any] = {"plane": plane, "dir": self.out_dir}
            if target:
                params["target_triangles"] = int(target)
            rid = self.client.send_request("mesh.cut", params)
            try:
                result = _check_schema(self.client.wait(rid), "mallard-mesh-cut")
            except GeneratorExit:
                self.client.cancel(rid)
                raise
            yield Cut(str(result["file"]), int(result["triangles"]), int(result.get("cells", 0)), int(target))

    def pick(self, point: Sequence[float], kind: str = "cell") -> Pick:
        if kind not in ("cell", "face", "zone"):
            raise ValueError(f"pick kind {kind!r}: cell, face or zone")
        r = _check_schema(
            self.client.request("mesh.pick", {"point": [float(c) for c in point], "kind": kind}), "mallard-mesh-pick"
        )
        return Pick(
            kind,
            int(r["id"]),
            str(r.get("zone", "")),
            [float(c) for c in r.get("center", [])],
            r.get("volume", r.get("area")),
            dict(r.get("quality") or {}),
            r.get("rank"),
        )
