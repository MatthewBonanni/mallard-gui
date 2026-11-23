"""The one disclosure control: a header row that toggles what follows it, its chevron in a fixed 16 px gutter
turning a quarter when open. The case tree's headings, the property groups, the check report's raw text and every
other expand/collapse use it, so they look and act alike (tests/test_ui_disclosure.py finds any that does not)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from trame.widgets import html

CHEVRON = "icons.next"


def chevron() -> None:
    """The disclosure chevron alone, for a row whose own toggle is the chevron (a tree item that selects on a
    click): the same glyph, gutter and turn."""
    html.Span(v_html=CHEVRON, classes="chev", aria_hidden="true")


@contextmanager
def disclosure(open_: str, toggle: Any, classes: str = "", **attrs: Any) -> Iterator[None]:
    """A header row, open while the expression `open_` holds; a click anywhere on it, Enter or Space runs
    `toggle` (a server call or a client expression). The `with` body is the row's content after the chevron."""
    with html.Div(
        classes=f"disc {classes}".strip(),
        role="button",
        tabindex="0",
        __properties=[("aria_expanded", "aria-expanded")],
        aria_expanded=(f"String(!!({open_}))",),
        click=toggle,
        keydown_enter=toggle,
        __events=[("keydown_space", "keydown.space.prevent")],
        keydown_space=toggle,
        **attrs,
    ):
        chevron()
        yield
