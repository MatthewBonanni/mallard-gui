"""The one dropdown for a finite set of values: a button showing the value (or, dimmed, what applies unset) that
opens the list with a search field focused (assets/dropdown.js). Every choice among values in the inspector uses
it, so they look and act alike."""

from __future__ import annotations

from typing import Any

from trame.widgets import html


def dropdown(
    value: str,
    options: str,
    pick: Any,
    label: Any,
    placeholder: str = "''",
    free: str = "false",
    classes: str = "",
    swatch: str = "",
    **attrs: Any,
) -> None:
    """A dropdown of the expression `options` (strings, or {value, label, note, swatch}) showing `value`, else
    `placeholder`; `pick` gets the chosen value as `$event.detail`. With `free`, text that names no option can
    be picked as typed. `swatch` is the expression of an SVG drawn before the value (a colormap's)."""
    with html.Button(
        type="button",
        classes=f"dd {classes}".strip(),
        aria_haspopup="listbox",
        aria_label=label,
        title=attrs.pop("title", (f"{value} || ({placeholder})",)),
        data_options=(f"JSON.stringify({options} || [])",),
        data_value=(f"{value} ?? ''",),
        data_free=(f"({free}) ? '1' : null",),
        __properties=[
            ("aria_haspopup", "aria-haspopup"),
            ("data_options", "data-options"),
            ("data_value", "data-value"),
            ("data_free", "data-free"),
        ],
        __events=[("dd_pick", "dd-pick")],
        dd_pick=pick,
        **attrs,
    ):
        if swatch:
            html.Span(v_html=swatch, classes="dd-swatch")
        html.Span(f"{{{{ {value} || ({placeholder}) }}}}", classes=(f"['dd-text', {{dim: !({value})}}]",))
        html.Span(v_html="icons.down", classes="dd-caret", aria_hidden="true")
