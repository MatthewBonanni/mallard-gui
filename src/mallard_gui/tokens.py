"""The GUI's design tokens, as CSS custom properties (`--m-*`): the one place its colors, type, spacing, radii
and elevation are defined. `assets/style.css` and the other sheets use these variables and no literal colors.

The website's shared tokens (`design/tokens.json` of the website repository, pinned by TOKENS_SOURCE) give the
brand: the accent, the status colors and the fonts. The GUI layers its own over them: a dense scale and a
ladder of surfaces (chrome, canvas, panel, head, raised) that a website does not need, in each theme.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

TOKENS_SOURCE = "MatthewBonanni/mallard-website@7f01b55882e0ed1e205c51f79bee9f44f2396d9a:design/tokens.json"

# Sizes in px, on a 4 px grid (2 and 6 for the tightest insets).
SCALE = {
    # type
    "micro": 10,  # caps labels, plot ticks
    "small": 11,  # meta, units, tags
    "body": 12,  # the default
    "emph": 13,  # names, dialog text
    "title": 15,  # dialog titles, run names
    "figure": 20,  # key figures (run stats, the estimate)
    "page": 22,  # a page's title
    # space
    "space-1": 2,
    "space-2": 4,
    "space-3": 6,
    "space-4": 8,
    "space-5": 12,
    "space-6": 16,
    "space-7": 24,
    "space-8": 32,
    "pad": 8,
    # controls and bands
    "control": 26,
    "control-sm": 22,
    "icon": 16,
    "row": 26,
    "tab": 34,
    "header": 40,
    "status": 26,
    "rail": 48,
    # radii
    "radius-sm": 3,
    "radius": 5,
    "radius-lg": 8,
}

# The surfaces, back to front: chrome (title bar, rail, status bar), canvas (behind views), bg (panels),
# surface (panel heads, tab strips, group bands), raised (menus, dialogs). Text colors keep WCAG AA (4.5:1) on
# every surface of their theme.
GUI_COLORS = {
    "light": {
        "chrome": "#e6eaef",
        "chrome-border": "#cdd4db",
        "bg": "#ffffff",
        "surface": "#f5f7f9",
        "surface-2": "#e9edf1",
        "canvas": "#f3f5f8",
        "raised": "#ffffff",
        "border": "#d5dbe1",
        "border-strong": "#b8c1ca",
        "divider": "#e9ecef",
        "field": "#ffffff",
        "field-border": "#c4ccd4",
        "hover": "#10161c0d",
        "selected": "#0b6b631c",
        "selected-strong": "#0b6b632e",
        "text-muted": "#4b5763",
        "scrim": "#0f161d59",
        "shadow-1": "0 1px 2px #10161c14",
        "shadow-2": "0 4px 16px #10161c1f, 0 1px 3px #10161c14",
        "shadow-3": "0 24px 64px #10161c33, 0 2px 8px #10161c1f",
        "axis-x": "#c62f3b",
        "axis-y": "#1f8a3c",
        "axis-z": "#2c63c9",
        "tk-comment": "#5c6773",
        "tk-table": "#0b6b63",
        "tk-key": "#1f4f99",
        "tk-string": "#17702f",
        "tk-number": "#a33b0c",
        "tk-bool": "#7032c8",
        "tk-date": "#875a00",
        "tk-punct": "#4e5a66",
    },
    "dark": {
        "chrome": "#080b0f",
        "chrome-border": "#1f2730",
        "bg": "#10151b",
        "surface": "#151b22",
        "surface-2": "#1c242d",
        "canvas": "#0b0f14",
        "raised": "#18202a",
        "border": "#242d37",
        "border-strong": "#364350",
        "divider": "#1b222a",
        "field": "#0c1116",
        "field-border": "#323e4a",
        "hover": "#ffffff0b",
        "selected": "#4fc4b622",
        "selected-strong": "#4fc4b638",
        "text-muted": "#9ba7b3",
        "scrim": "#00000080",
        "shadow-1": "0 1px 2px #00000059",
        "shadow-2": "0 6px 20px #00000073, 0 1px 3px #00000059",
        "shadow-3": "0 24px 64px #0000009e, 0 2px 8px #00000066",
        "axis-x": "#ff6b74",
        "axis-y": "#4fd27a",
        "axis-z": "#6ea4ff",
        "tk-comment": "#8b97a3",
        "tk-table": "#4fc4b6",
        "tk-key": "#82b8ff",
        "tk-string": "#7ee08a",
        "tk-number": "#ffa766",
        "tk-bool": "#d2a8ff",
        "tk-date": "#e3b341",
        "tk-punct": "#9aa6b2",
    },
}


def load_tokens() -> dict[str, Any]:
    with resources.files("mallard_gui.assets").joinpath("tokens.json").open("r", encoding="utf-8") as f:
        return dict(json.load(f))


def theme_colors(tokens: dict[str, Any], theme: str) -> dict[str, str]:
    """The website's colors for `theme`, with the GUI's surfaces over them."""
    return {**tokens["color"][theme], **GUI_COLORS[theme]}


def css_variables(tokens: dict[str, Any]) -> str:
    """`:root` variables for the light theme, dark under `[data-theme=dark]` and the system preference."""

    def block(theme: str) -> str:
        colors = theme_colors(tokens, theme)
        # tk-*: the input editor's token colors, named as its highlighter's classes
        return "".join(f"--{k}:{v};" if k.startswith("tk-") else f"--m-{k}:{v};" for k, v in colors.items())

    font = tokens["font"]
    common = (
        f"--m-font-sans:'{font['sans']['family']}',{font['sans']['fallback']};"
        f"--m-font-mono:'{font['mono']['family']}',{font['mono']['fallback']};"
    ) + "".join(f"--m-{k}:{v}px;" for k, v in SCALE.items())
    return (
        f":root{{{common}{block('light')}}}"
        f"@media (prefers-color-scheme: dark){{:root:not([data-theme=light]){{{block('dark')}}}}}"
        f":root[data-theme=dark]{{{block('dark')}}}"
        f":root[data-theme=light]{{{block('light')}}}"
    )
