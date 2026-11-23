"""The section tool's options, shared by the case view and the results viewer: rows along the top of the view
while the tool is on, with the plane's axis presets, the side kept, its position on a slider, its mark, and its
origin and normal as numbers; x-ray's opacity with it. Each page keeps its own state under a prefix."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trame.widgets import html


def section_bar(
    prefix: str,
    on_axis: Callable[..., Any],
    on_flip: tuple[Callable[..., Any], str],
    on_offset: Callable[..., Any],
    on_mark: tuple[Callable[..., Any], str],
    on_close: tuple[Callable[..., Any], str],
    shown: str,
    dim: str = "dim",
    what: str = "Clip",
) -> None:
    """The section tool's options, rows along the top of the view while the tool is on (`shown`): the plane's
    axis presets and the side kept, its position along its normal on a slider across the scene with the value
    beside it, whether the plane is drawn, and, opened by Numbers, its origin and normal as numbers. With x-ray
    on, its opacity. State under `prefix`: `clip_origin`, `clip_normal`, `clip_flip`, `snap`, `xray`,
    `xray_opacity`, and `sec` (the slider's `lo`, `hi` and `at`, the normal's `axis`), `sec_mark`, `sec_more`."""
    p = prefix
    sec = f"{p}sec"
    with html.Div(classes="optbar", v_if=f"{shown} || {p}xray", role="group", aria_label="View tool options"):
        with html.Div(classes="opt-group", v_if=shown):
            html.Span("SECTION", classes="opt-name")
            for a in "xyz":
                html.Button(
                    a.upper(),
                    classes=(f"{sec}.axis === '{a}' ? 'tb mono on' : 'tb mono'",),
                    click=(on_axis, f"['{a}']"),
                    v_if=f"{dim} === 3" if a == "z" else None,
                    title=f"The plane across {a}",
                )
            html.Button(
                "Flip",
                classes=(f"{p}clip_flip ? 'tb on' : 'tb'",),
                click=on_flip,
                title="Keep the other side of the plane",
            )
            html.Span(f"{{{{ {sec}.axis || 'at' }}}}", classes="opt-label mono")
            html.Input(
                type="range",
                classes="opt-slider",
                min="0",
                max="1000",
                value=(
                    f"{sec}.hi > {sec}.lo ? Math.round(1000 * ({sec}.at - {sec}.lo) / ({sec}.hi - {sec}.lo)) : 500",
                ),
                aria_label="section position",
                title="The plane's position along its normal, across the scene",
                input="Date.now() - ($event.target.dataset.t || 0) > 50 && ($event.target.dataset.t = Date.now(), "
                f"trigger('{p}sec_slide', [$event.target.value / 1000]))",
                change=f"trigger('{p}sec_slide', [$event.target.value / 1000])",
            )
            html.Input(
                classes="opt-value mono",
                value=(f"{sec}.text",),
                aria_label="section position value",
                title="The plane's position along its normal",
                change=(on_offset, "[$event.target.value]"),
                keydown_enter="$event.target.blur()",
            )
            html.Button(
                "Plane",
                classes=(f"{p}sec_mark ? 'tb on' : 'tb'",),
                click=on_mark,
                title="Draw the plane's outline and its normal in the view",
            )
            html.Button(
                "Numbers",
                classes=(f"{p}sec_more ? 'tb on' : 'tb'",),
                click=f"{p}sec_more = !{p}sec_more",
                title="The plane's origin and normal as numbers",
            )
            html.Button(v_html="icons.close", classes="icon-btn", click=on_close, title=f"Turn {what} off")
        with html.Div(classes="opt-group", v_if=f"({shown}) && {p}sec_more"):
            for label, var in (("origin", f"{p}clip_origin"), ("normal", f"{p}clip_normal")):
                html.Span(label, classes="opt-label")
                for i in range(3):
                    html.Input(
                        classes="opt-value mono",
                        v_if=f"{dim} > {i}" if i == 2 else None,
                        value=(f"{var}[{i}]",),
                        aria_label=f"{label} {'xyz'[i]}",
                        change=f"{var} = {var}.map((v, j) => j === {i} ? $event.target.value : v)",
                        keydown_enter="$event.target.blur()",
                    )
            with html.Label(classes="opt-check", title="A normal within 15° of an axis is that axis"):
                html.Input(type="checkbox", v_model=(f"{p}snap",))
                html.Span("snap to axes")
        with html.Div(classes="opt-group", v_if=f"{p}xray"):
            html.Span("X-RAY", classes="opt-name")
            html.Input(
                type="range",
                classes="opt-slider short",
                min="5",
                max="100",
                v_model=(f"{p}xray_opacity",),
                aria_label="x-ray opacity",
                title="How opaque the surfaces drawn through are",
            )
            html.Span(f"{{{{ {p}xray_opacity }}}} %", classes="opt-label mono")


def section_state(extent: tuple[float, float], offset: float, normal: Any) -> dict[str, Any]:
    """`sec` for the bar: the slider's ends and the plane's place along its normal, and the axis the normal is."""
    from .fmt import number

    lo, hi = extent
    n = [abs(float(c)) for c in normal]
    axis = next((a for a, c in zip("xyz", n, strict=False) if c > 1 - 1e-9), "")
    return {"lo": lo, "hi": hi, "at": offset, "text": number(offset), "axis": axis}


def note_row(label: str, text: str) -> None:
    with html.Div(classes="prow"):
        html.Div(label, classes="plabel")
        html.Div(text, classes="pval ro muted", style="font-family: var(--m-font-sans)")
