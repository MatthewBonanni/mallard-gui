"""A layout built in one pass is the layout trame builds, rendered once instead of once per enclosing block."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from typing import Any

import pytest
from trame.app import get_server
from trame.ui.html import DivLayout
from trame.widgets import html
from trame_client.widgets.vue import HtmlElement as VueImpl

from mallard_gui.ui.app import one_render


def build(ctx: Callable[[], contextlib.AbstractContextManager[None]], name: str) -> tuple[str, int]:
    server = get_server(name, client_type="vue3")
    renders = 0
    render = VueImpl.render

    def counting(self: Any) -> str:
        nonlocal renders
        renders += 1
        return str(render(self))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(VueImpl, "render", counting)
        with ctx(), DivLayout(server) as layout:  # noqa: SIM117  the layout mirrors the DOM
            with html.Div(classes="a"), html.Div(v_if=("shown", True)), html.Div():
                html.Button("go", click=lambda: None, disabled=("busy", False))
        template = layout.html
    return re.sub(r"trigger__\d+", "trigger", template), renders


def test_one_render_builds_the_same_template_rendering_each_element_once() -> None:
    plain, plain_renders = build(contextlib.nullcontext, "plain")
    once, once_renders = build(one_render, "once")
    assert once == plain
    assert once_renders == 2 * 5  # the layout's flush and the check's own render, five elements each
    assert plain_renders > once_renders
