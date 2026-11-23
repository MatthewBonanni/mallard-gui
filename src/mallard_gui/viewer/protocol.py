"""The viewer's protocol: one JSON object per line each way. A request `{"id", "method", "params"}` gets
`{"id", "result"}` or `{"id", "error"}`; frames travel base64-encoded inside the result. The same code
serves the in-process engine of a local run and the viewer job of a remote one, over the SSH channel's
standard input and output (the transport the design's fallback 3 names, which works wherever
`srun --overlap` does), so both paths run the same requests."""

from __future__ import annotations

import base64
import json
import os
import selectors
import socket
import sys
import time
from typing import IO, Any, cast

from .engine import ResultScene, ViewerError

NAME = "mallard-viewer"
VERSION = 1
# The engine methods a client may call, and nothing else.
METHODS = (
    "open",
    "show",
    "set_field",
    "set_particles",
    "set_section",
    "camera",
    "frame",
    "screenshot",
    "pick_point",
    "probe",
    "line",
    "set_markers",
    "info",
    "set_theme",
    "set_xray",
    "set_visible",
    "isolate",
    "show_all",
    "rescan",
    "geometry",
    "get_camera",
    "set_camera",
    "export_surface",
    "render_info",
    "build_lod",
    "overlay",
    "stats",
    "frame_field",
)


def hello() -> dict[str, Any]:
    return {"hello": NAME, "version": VERSION, "pid": os.getpid(), "host": socket.gethostname()}


def dispatch(engine: ResultScene, request: dict[str, Any]) -> dict[str, Any]:
    rid = request.get("id")
    method = request.get("method")
    if method not in METHODS:
        return {"id": rid, "error": f"unknown method {method!r}"}
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return {"id": rid, "error": "params: an object"}
    try:
        result = getattr(engine, str(method))(**params)
    except (ViewerError, ValueError, KeyError, TypeError, IndexError, OSError) as e:
        return {"id": rid, "error": f"{type(e).__name__}: {e}"}
    if isinstance(result, dict) and isinstance(result.get("image"), bytes):
        result = {**result, "image": base64.b64encode(result["image"]).decode("ascii")}
    return {"id": rid, "result": result}


def serve(engine: ResultScene, inp: IO[bytes], out: IO[bytes], idle: float) -> str:
    """Answer requests until the client closes its end or `idle` seconds pass without one; returns why."""
    out.write(json.dumps(hello()).encode() + b"\n")
    out.flush()
    sel = selectors.DefaultSelector()
    sel.register(inp, selectors.EVENT_READ)
    buffer = b""
    last = time.monotonic()
    while True:
        while b"\n" not in buffer:
            left = idle - (time.monotonic() - last)
            if left <= 0:
                return "idle"
            if not sel.select(timeout=left):
                continue
            chunk = os.read(inp.fileno(), 1 << 16)
            if not chunk:
                return "closed"
            buffer += chunk
        line, _, buffer = buffer.partition(b"\n")
        last = time.monotonic()
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            response: dict[str, Any] = {"id": None, "error": f"bad request: {e}"}
        else:
            response = (
                dispatch(engine, request) if isinstance(request, dict) else {"id": None, "error": "not an object"}
            )
        out.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        out.flush()


def main_stdio(root: str, idle: float) -> int:
    """`python -m mallard_gui.viewer --stdio`: the protocol on standard input and output. Anything else
    printing to standard output (VTK's warnings) goes to standard error instead."""
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    from pathlib import Path

    engine = ResultScene(Path(root))
    why = serve(engine, sys.stdin.buffer, proto, idle)
    print(f"{NAME}: exiting ({why})", file=sys.stderr)
    return 0


def main_listen(root: str, idle: float, port: int) -> int:
    """`python -m mallard_gui.viewer --listen PORT`: the protocol on a TCP port of the loopback interface only,
    for a viewer pod reached through `kubectl port-forward` (which connects inside the pod). One client at a
    time; the viewer exits after `idle` seconds without a request, connected or not."""
    from pathlib import Path

    engine = ResultScene(Path(root))
    server = socket.create_server(("127.0.0.1", port))
    server.settimeout(1.0)
    print(f"{NAME}: listening on 127.0.0.1:{port}", file=sys.stderr, flush=True)
    last = time.monotonic()
    why = "idle"
    while time.monotonic() - last < idle:
        try:
            conn, _ = server.accept()
        except TimeoutError:
            continue
        with conn:
            conn.settimeout(None)
            with conn.makefile("rb", buffering=0) as inp, conn.makefile("wb") as out:
                try:
                    why = serve(engine, cast(IO[bytes], inp), out, idle)
                except (BrokenPipeError, ConnectionResetError):
                    why = "closed"
        last = time.monotonic()
        if why == "idle":
            break
    server.close()
    print(f"{NAME}: exiting (idle)", file=sys.stderr)
    return 0
