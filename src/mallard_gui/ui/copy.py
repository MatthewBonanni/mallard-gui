"""The copy button of a message: copies the full text of the `data-copy` element it is in (assets/copy.js)."""

from __future__ import annotations

from typing import Any

from trame.widgets import html

COPY_PROPS = [("data_copy", "data-copy")]
DOCS_BASE = "https://github.com/MatthewBonanni/mallard/blob/main/"  # where a diagnostic's docs path points


def copy_button(**attrs: Any) -> html.Button:
    return html.Button(
        v_html="icons.copy",
        classes="icon-btn copy-btn",
        type="button",
        title=attrs.pop("title", "Copy the full message"),
        aria_label=attrs.pop("aria_label", "Copy"),
        **attrs,
    )


def message(text: str, classes: Any = "", **attrs: Any) -> None:
    """A message (the JS expression `text`) with its copy button: one action copies all of it."""
    with html.Div(
        classes=("['copyable', " + classes[0] + "]",) if isinstance(classes, tuple) else f"copyable {classes}",
        tabindex="0",
        __properties=COPY_PROPS,
        data_copy=(text,),
        **attrs,
    ):
        html.Span(f"{{{{ {text} }}}}", classes="copy-text")
        copy_button()
