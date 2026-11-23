"""`mallard-gui [PROJECT]`: the web UI on 127.0.0.1, a random port and a per-session token."""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:
    from .executables import Executables
    from .settings import Settings

HOST = "127.0.0.1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mallard-gui", description="Mallard's graphical front end.")
    p.add_argument(
        "project",
        nargs="?",
        default=".",
        help="A directory holding a case's input.toml, or an input file in one (default: .)",
    )
    p.add_argument("--input", help="The case's input file in the project (default: its first input)")
    p.add_argument(
        "--mallard",
        help="The mallard front end to use (default: Settings' path, else $MALLARD, else mallard on PATH); Settings "
        "can name each dimension's own executable",
    )
    p.add_argument("--port", type=int, default=0, help="Port on 127.0.0.1 (default: a random free one)")
    p.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser")
    p.add_argument(
        "--url-file",
        help="Write the URL (it holds the session's token) to this file, readable by you alone, once the server is "
        "ready: for the desktop shell",
    )
    p.add_argument(
        "--new",
        action="store_true",
        help="Make PROJECT a new project first: a new or empty directory, with a starting input.toml",
    )
    p.add_argument(
        "--threads", type=int, help="Threads of local mallard commands and runs (default: Settings', else 2)"
    )
    p.add_argument("--version", action="version", version=f"mallard-gui {__version__}")
    return p.parse_args(argv)


def executables_of(settings: Settings, explicit: str | None) -> Executables:
    """The front end (--mallard, Settings, then find_mallard's order) and each dimension's executable Settings
    names. Without a front end, a dimension's executable stands in for it."""
    from .executables import Executables
    from .mallard import MallardError, find_mallard

    paths = settings.executables()
    by_dim = {int(d): Path(paths[d]) for d in ("2", "3") if paths[d]}
    try:
        front = find_mallard(explicit or paths["front"] or None)
    except MallardError:
        if not by_dim:
            raise
        front = next(iter(by_dim.values()))
    return Executables(front, by_dim)


def main(argv: list[str] | None = None) -> int:
    from .loading import Progress

    progress = Progress()
    args = parse_args(argv)
    progress("mallard")
    from .mallard import Mallard, MallardError
    from .settings import Settings

    settings = Settings()
    if settings.error:
        print(f"mallard-gui: {settings.error}", file=sys.stderr)
    try:
        executables = executables_of(settings, args.mallard)
        mallard = Mallard(executables.front, threads=args.threads or settings.threads)
        mallard.version()
    except MallardError as e:
        print(f"mallard-gui: {e}", file=sys.stderr)
        return 1
    project = Path(args.project).expanduser().resolve()
    if args.new:
        from .project import new_project

        try:
            new_project(project)
        except OSError as e:
            print(f"mallard-gui: {e}", file=sys.stderr)
            return 1
    if project.is_file() and not args.input:
        project, args.input = project.parent, project.name
    if not project.is_dir():
        print(f"mallard-gui: {project}: not a directory", file=sys.stderr)
        return 1

    progress("libraries")
    from trame.app import get_server

    from .macos import hide_from_dock

    hide_from_dock()

    from .session import Session
    from .ui.app import App

    progress("case")
    try:
        session = Session(project, mallard, args.input, executables)
    except (OSError, ValueError) as e:
        print(f"mallard-gui: {e}", file=sys.stderr)
        return 1
    # Other users of a shared machine can reach its localhost ports: the websocket needs a token,
    # given to the browser in the URL and to trame in a file only this user can read.
    token = secrets.token_urlsafe(24)
    key_dir = Path(tempfile.mkdtemp(prefix="mallard-gui-"))
    key_file = key_dir / "token"
    key_file.write_text(token, encoding="utf-8")
    os.chmod(key_file, 0o600)
    sys.argv = [sys.argv[0], "--authKeyFile", str(key_file)]

    progress("interface")
    server = get_server(client_type="vue3")
    App(session, server)

    def announce(**_: object) -> None:
        # trame's client takes the websocket's URL and the token from the page's query string.
        url = f"http://{HOST}:{server.port}/?sessionURL=ws://{HOST}:{server.port}/ws&secret={token}"
        print(f"mallard-gui: {url}", flush=True)
        if args.url_file:
            fd = os.open(args.url_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(url)
        if not args.no_browser:
            webbrowser.open(url)

    server.controller.on_server_ready.add(announce)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the cleanup below runs on `kill` too
    try:
        server.start(host=HOST, port=args.port, open_browser=False, show_connection_info=False, timeout=0)
    finally:
        session.close()  # viewer jobs and helper pods end now rather than at their idle timeouts
        key_file.unlink(missing_ok=True)
        key_dir.rmdir()
    return 0
