"""Draggable splitters between a page's panes. panes.js resizes; a pane's size is the CSS variable
`--<name>` on `.body`, from the state `pane_layout`, which is settings.toml's [layout]."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from ..settings import SettingsError

if TYPE_CHECKING:
    from .app import App

# The width or height of a collapsed pane: the strip that brings it back.
STRIP = 12
# `.body`'s style: every stored size as its variable, a collapsed pane's as the strip.
BODY_STYLE = (
    "Object.fromEntries(Object.entries(pane_layout).map(([k, v]) => k.endsWith('-collapsed') ? "
    f"['--' + k.slice(0, -10), '{STRIP}px'] : ['--' + k, (pane_layout[k + '-collapsed'] ? {STRIP} : v) + 'px']))"
)


class Panes:
    def __init__(self, app: App) -> None:
        self.settings = app.settings_page.settings
        app.state.pane_layout = self.settings.layout()
        app.state.change("pane_layout")(self._changed)

    def _changed(self, pane_layout: dict[str, Any], **_: Any) -> None:
        with contextlib.suppress(OSError, SettingsError):  # a read-only or broken settings file: sizes for now
            self.settings.set_layout(pane_layout)


def splitter(
    name: str,
    label: str,
    *,
    axis: str = "x",
    edge: str = "end",
    minimum: int = 160,
    maximum: int = 900,
    keep: int = 360,
    collapsible: bool = True,
    sibling: bool = False,
    v_if: str | None = None,
) -> None:
    """A splitter on the `edge` ("end": right or bottom; "start": left or top) of the pane it sits in, or, with
    `sibling`, of the element before it (a scrolled list, which would scroll a splitter inside it). Sizes stay in
    minimum..maximum and leave the flexible neighbor at least `keep` px; a collapsible pane folds to a strip."""
    collapsed = f"pane_layout['{name}-collapsed']"
    props = [
        ("data_split", "data-split"),
        ("data_axis", "data-axis"),
        ("data_edge", "data-edge"),
        ("data_min", "data-min"),
        ("data_max", "data-max"),
        ("data_keep", "data-keep"),
        ("aria_orientation", "aria-orientation"),
    ]
    extra: dict[str, Any] = {}
    if sibling:
        props.append(("data_pane", "data-pane"))
        extra["data_pane"] = "prev"
    if collapsible:
        props.append(("data_collapsed", "data-collapsed"))
        extra["data_collapsed"] = (f"{collapsed} ? '' : null",)
    if v_if:
        extra["v_if"] = v_if
    with html.Div(
        classes="splitter",
        role="separator",
        tabindex="0",
        aria_label=f"Resize {label}",
        title="Drag or arrow keys to resize; double-click for the default size"
        + ("; Enter hides or shows it" if collapsible else ""),
        __properties=props,
        data_split=name,
        data_axis=axis,
        data_edge=edge,
        data_min=str(minimum),
        data_max=str(maximum),
        data_keep=str(keep),
        aria_orientation="vertical" if axis == "x" else "horizontal",
        **extra,
    ):
        if collapsible:
            html.Button(
                classes="splitter-toggle",
                tabindex="-1",
                aria_label=(f"({collapsed} ? 'Show ' : 'Hide ') + '{label}'",),
                title=(f"({collapsed} ? 'Show ' : 'Hide ') + '{label}'",),
            )


def list_style(name: str, cap: str) -> str:
    """A scrolled list sized by its splitter: its content's height up to `cap` until one is dragged."""
    return f"flex: none; height: var(--{name}, auto); max-height: var(--{name}, {cap})"


def list_splitter(name: str, label: str, v_if: str | None = None) -> None:
    """The splitter under a list sized by `list_style(name, ...)`."""
    splitter(name, label, axis="y", minimum=48, maximum=900, keep=80, collapsible=False, sibling=True, v_if=v_if)
