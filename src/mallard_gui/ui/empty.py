"""A page's or pane's empty state: an icon, what belongs here, how to get it, and the one action that does."""

from __future__ import annotations

from typing import Any

from trame.widgets import html


def empty_state(
    icon: str,
    title: str,
    text: str,
    v_if: str,
    action: tuple[str, str, Any] | None = None,
    compact: bool = False,
) -> None:
    """Centered in its container while `v_if` holds. `action` is (label, icon, click), click as trame takes it."""
    with html.Div(classes="empty-state compact" if compact else "empty-state", v_if=v_if, role="status"):
        html.Span(v_html=f"icons.{icon}", classes="es-icon")
        html.Div(title, classes="es-title")
        html.Div(text, classes="es-text")
        if action:
            label, glyph, click = action
            with html.Button(classes="btn", click=click):
                html.Span(v_html=f"icons.{glyph}")
                html.Span(label)
