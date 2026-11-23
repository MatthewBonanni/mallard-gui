"""Opening another project from a browser: a server serves one project, so another `mallard-gui` starts on it and
the page moves to its URL. (The desktop shell restarts its own server instead.) Revealing a file in the file
manager, for the browser too: the server runs on the user's machine."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

STARTUP = 120.0


class OpenError(RuntimeError):
    pass


def server_command(path: Path, mallard: Path, threads: int, url_file: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "mallard_gui",
        str(path),
        "--no-browser",
        "--url-file",
        str(url_file),
        "--mallard",
        str(mallard),
        "--threads",
        str(threads),
    ]


def start_server(path: Path, mallard: Path, threads: int, timeout: float = STARTUP) -> str:
    """Start a `mallard-gui` on `path` (a project directory, or an input in one), detached from this one, and
    return its URL once it serves."""
    work = Path(tempfile.mkdtemp(prefix="mallard-gui-open-"))
    url_file, log = work / "url", work / "server.log"
    try:
        with log.open("w") as err:
            proc = subprocess.Popen(
                server_command(path, mallard, threads, url_file),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
                start_new_session=True,
            )
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if url_file.is_file() and (url := url_file.read_text(encoding="utf-8").strip()):
                return url
            if proc.poll() is not None:
                tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-5:]
                raise OpenError(f"mallard-gui {path} stopped: " + (" ".join(tail) or f"exit {proc.returncode}"))
            time.sleep(0.1)
        proc.terminate()
        raise OpenError(f"mallard-gui {path} did not start in {timeout:.0f} s")
    finally:
        shutil.rmtree(work, ignore_errors=True)  # the URL holds the new session's token


def reveal_command(path: Path) -> Sequence[str]:
    if sys.platform == "darwin":
        return ["open", "-R", str(path)]
    if sys.platform == "win32":
        return ["explorer", f"/select,{path}"]
    return ["xdg-open", str(path.parent)]


def reveal(path: Path) -> None:
    quiet = subprocess.DEVNULL
    subprocess.Popen(reveal_command(path), stdin=quiet, stdout=quiet, stderr=quiet)
