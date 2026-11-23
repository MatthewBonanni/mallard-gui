"""Digit groups for numbers shown as text, everywhere in the GUI (the viewer too, which has no trame)."""

from __future__ import annotations


def grouped(n: int) -> str:
    """A whole number with comma digit groups: 2,097,152. Never for values written to the input or typed back
    into a field: TOML has no comma separators."""
    return f"{n:,}"
