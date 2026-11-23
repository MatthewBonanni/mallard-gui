"""The startup screen on the GUI's page: trame's index.html with Mallard's cover in place of trame's spinner, until
the app has drawn its first frame. The cover is `assets/loading.html`, also the desktop shell's splash, so the
window looks the same as the shell hands it to the page."""

from __future__ import annotations

import contextlib
import json
import os
import re
from importlib import resources
from pathlib import Path
from typing import Any

PROGRESS_ENV = "MALLARD_GUI_PROGRESS_FILE"
STATE = '<script type="application/json" id="mallard-loading-state">{}</script>'


def _part(text: str, name: str) -> str:
    match = re.search(rf"<!-- mallard-loading:{name} -->(.*?)<!-- /mallard-loading:{name} -->", text, re.S)
    if match is None:
        raise ValueError(f"loading.html: no mallard-loading:{name} part")
    return match.group(1)


def cover(project: str) -> tuple[str, str]:
    """The cover's style (for the head) and its markup and script (for the body), at the last stage."""
    text = resources.files("mallard_gui.assets").joinpath("loading.html").read_text(encoding="utf-8")
    state = json.dumps({"stage": "render", "project": project}).replace("<", "\\u003c")
    body = _part(text, "body")
    if STATE not in body:
        raise ValueError("loading.html: no mallard-loading-state script")
    return _part(text, "head"), body.replace(STATE, STATE.replace("{}", state))


def page(index: str, project: str) -> str:
    """trame's index.html with the cover, drawn with the page's first paint."""
    head, body = cover(project)
    index = index.replace("</head>", f"{head}</head>", 1)
    return re.sub(r"<body[^>]*>", lambda m: m.group(0) + body, index, count=1)


def install(server: Any, project: str) -> None:
    """Serve the covered index.html, and an empty loading.tpl for trame's own spinner; every reply revalidates."""
    from aiohttp import web
    from trame_client.module import vue3

    html = page((Path(vue3.www) / "index.html").read_text(encoding="utf-8"), project)
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    async def index(_: web.Request) -> web.Response:
        return web.Response(text=html, content_type="text/html", headers=headers)

    async def no_spinner(_: web.Request) -> web.Response:
        return web.Response(text="", content_type="text/plain", headers=headers)

    async def revalidate(_: web.Request, response: web.StreamResponse) -> None:
        # Without it WebKit (the desktop app) caches the GUI's scripts and styles heuristically and draws a
        # reinstalled GUI with stale ones; no-cache still answers an unchanged file with 304.
        response.headers.setdefault("Cache-Control", "no-cache")

    def bind(wslink_server: Any) -> None:
        wslink_server.app.on_response_prepare.append(revalidate)
        # Routes added here precede wslink's static ones, which it adds as the server starts.
        router = wslink_server.app.router
        router.add_get("/", index)
        router.add_get("/index.html", index)
        router.add_get("/loading.tpl", no_spinner)

    server.controller.on_server_bind.add(bind)


class Progress:
    """The startup steps the desktop shell shows on its splash, each a line appended to the file it names in
    $MALLARD_GUI_PROGRESS_FILE (dropped from the environment, so mallard's own processes never see it)."""

    def __init__(self) -> None:
        self.path = os.environ.pop(PROGRESS_ENV, "")

    def __call__(self, step: str) -> None:
        if self.path:
            with contextlib.suppress(OSError), open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{step}\n")
