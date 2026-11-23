"""The style of Mallard's documentation figures (`tools/docs/figstyle.py` at MatthewBonanni/mallard@1fa6e0b05):
each figure drawn twice, for the light and the dark theme, as SVG with a transparent background and real text.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # no GUI backend: drawing never makes this process an application
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

Palette = dict[str, str]

LIGHT: Palette = {
    "name": "light",
    "bg": "#ffffff",
    "fg": "#1f1a1d",
    "muted": "#6b6166",
    "line": "#3b3338",
    "mesh": "#c9bfc4",
    "faint": "#ece6e9",
    "panel": "#f6f1f3",
    "teal": "#12655f",
    "flame": "#d9730d",
    "plum": "#7b3f6e",
    "blue": "#2f6db5",
    "red": "#c0392b",
    "green": "#3a7d2c",
    "gold": "#b58900",
}
DARK: Palette = {
    "name": "dark",
    "bg": "#171114",
    "fg": "#ece4e7",
    "muted": "#a99ea3",
    "line": "#cfc6ca",
    "mesh": "#4d4247",
    "faint": "#2a2126",
    "panel": "#211a1e",
    "teal": "#5fcfc1",
    "flame": "#f2a04d",
    "plum": "#d79bcb",
    "blue": "#7fb0ea",
    "red": "#f08070",
    "green": "#8fd17f",
    "gold": "#e3c45a",
}
THEMES = (LIGHT, DARK)
CYCLE = ("teal", "flame", "plum", "blue", "red", "green", "gold")


def rc(P: Palette) -> dict[str, Any]:
    return {
        "font.family": ["Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 13,
        "svg.fonttype": "none",
        "svg.hashsalt": "mallard",
        "text.color": P["fg"],
        "axes.labelcolor": P["fg"],
        "axes.edgecolor": P["muted"],
        "axes.facecolor": "none",
        "axes.titlesize": 14,
        "axes.labelsize": 13,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "xtick.color": P["muted"],
        "ytick.color": P["muted"],
        "xtick.labelcolor": P["fg"],
        "ytick.labelcolor": P["fg"],
        "xtick.labelsize": 12,
        "ytick.labelsize": 12,
        "legend.frameon": False,
        "legend.fontsize": 12,
        "figure.facecolor": "none",
        "savefig.facecolor": "none",
        "savefig.transparent": True,
        "lines.linewidth": 1.8,
        "hatch.color": P["muted"],
    }


def save_pair(stem: Path, draw: Callable[[Palette], Figure]) -> list[Path]:
    """`<stem>-light.svg` and `<stem>-dark.svg`, as the docs' figures are written."""
    stem.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for P in THEMES:
        with plt.rc_context(rc(P)):  # type: ignore[arg-type]
            fig = draw(P)
            path = stem.parent / f"{stem.name}-{P['name']}.svg"
            fig.savefig(path, format="svg", bbox_inches="tight", pad_inches=0.04, metadata={"Date": None}, dpi=150)
            plt.close(fig)
            out.append(path)
    return out
