"""The property inspector's catalog side: every key a table takes (set or not) in groups that disclose its tables,
arrays of tables and named entries; Add (with the entry's types), Duplicate and Delete (with Undo) for entries; Reset
for set keys; a key's catalog entry in the key help; and the presets of a section, resolved by Mallard, with what
switching would change."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trame.widgets import html

from ..fields import FieldCatalog
from ..input_doc import format_path, parse_path
from ..mallard import SITE, MallardError
from ..session import SECTIONS, catalog_path
from .copy import message
from .disclosure import disclosure
from .dropdown import dropdown
from .paths import kind_for, project_relative

if TYPE_CHECKING:
    from .app import App

GROUP_OPEN = "(g.collapsed === prop_toggled.includes(g.id))"
PARENTS_OPEN = "g.parents.every(a => a.collapsed === prop_toggled.includes(a.id))"


def _toggle(var: str, item: str) -> str:
    return f"{var} = {var}.includes({item}) ? {var}.filter(x => x !== {item}) : [...{var}, {item}]"


class Inspector:
    def __init__(self, app: App) -> None:
        self.app = app
        self.state = app.state
        self.session = app.session
        self._seq = itertools.count(1)
        self._preset_task: asyncio.Future[None] | None = None
        s = self.state
        s.prop_toggled = []  # groups opened or closed against their default
        s.key_help = None
        s.add_menu = {"open": False, "items": [], "x": 0, "y": 0}
        s.undo_notice = {}
        s.preset_pending = {}
        s.vec_err = {}  # a vector's components' problems, by key, until they are valid
        s.pts_err = {}  # the same for a list of points: by key, a list per point
        s.comp_ask = {}  # a switch between X and Y that would clear the values, waiting for the user
        s.oneof_ask = {}  # a switch to another alternative that would clear the keys of the one in use
        s.fpick = {"open": False}  # the field picker: its list's path, entries as picked, what they select
        s.fpick_groups = []
        s.fpick_q = ""
        s.feature_confirm = ""  # a feature with settings of the user's, asked before it is turned off
        server = app.server
        for name, fn in (
            ("insp_add", self.add),
            ("insp_feature", self.feature),
            ("insp_feature_mech", self.feature_mechanism),
            ("insp_duplicate", self.duplicate),
            ("insp_delete", self.delete),
            ("insp_reset", self.reset),
            ("insp_reveal", self.reveal),
            ("insp_undo", self.undo),
            ("preset_choose", self.preset_choose),
            ("preset_apply", self.preset_apply),
            ("insp_file", self.choose_file),
            ("comp_value", self.comp_value),
            ("comp_add", self.comp_add),
            ("comp_rename", self.comp_rename),
            ("comp_normalize", self.comp_normalize),
            ("comp_basis", self.comp_basis),
            ("comp_clear", self.comp_clear),
            ("insp_cancel", self.cancel),
            ("oneof_pick", self.oneof_pick),
            ("oneof_clear", self.oneof_clear),
            ("fields_open", self.fields_open),
            ("fields_toggle", self.fields_toggle),
            ("fields_drop", self.fields_drop),
            ("fields_apply", self.fields_apply),
            ("fields_remove", self.fields_remove),
        ):
            server.trigger(name)(fn)

    # What the panel shows

    def props(self, node_id: str) -> dict[str, Any]:
        ses = self.session
        props: dict[str, Any] = ses.properties(node_id) if node_id else {"title": "", "groups": []}
        stack: list[dict[str, Any]] = []
        props["groups"] = [g for g in props["groups"] if not g.get("hidden")]
        for g in props["groups"]:
            while stack and stack[-1]["depth"] >= g["depth"]:
                stack.pop()
            g["parents"] = [{"id": p["id"], "collapsed": p["collapsed"]} for p in stack]
            g["add"] = {**g["add"], "items": self.menu_items(g["add"])} if (g.get("add") or {}).get("path") else {}
            stack.append(g)
        props["catalog_error"] = (
            f"Mallard's key catalog is unavailable, so only keys the input sets are listed: {ses.general_error}"
            if ses.general_error
            else ""
        )
        props["preset"] = self._preset_props(node_id)
        return props

    def _preset_props(self, node_id: str) -> dict[str, Any]:
        kind, _, sid = node_id.partition(":")
        if kind != "section":
            return {}
        tables = next((t for s, _, t in SECTIONS if s == sid), (sid,))
        keys = [k for k in self.session.preset_keys() if k.split(".", 1)[0] in tables]
        if not keys:
            return {}
        view = self.session.presets.get(keys[0]) if self.session.presets_fresh else None
        if view is None:
            return {"key": keys[0], "loading": True, "active": str(self.session.doc.get(keys[0]) or "")}
        return view.to_dict()

    def menu_items(self, add: dict[str, Any]) -> list[dict[str, str]]:
        path = add.get("path", "")
        types = add.get("types") or []
        label = path.rsplit(".", 1)[-1]
        if not types:
            return [{"path": path, "type": "", "label": f"new {label} entry"}]
        return [{"path": path, "type": t, "label": t} for t in types]

    def section_adds(self, sid: str) -> list[dict[str, str]]:
        """What the tree's + on a section adds: each array or named table of the section, by type."""
        tables = next((t for s, _, t in SECTIONS if s == sid), (sid,))
        items: list[dict[str, str]] = []
        for t in tables:
            if t == "boundaries" or not (self.session.is_array(t) or self.session.is_named(t)):
                continue
            spec = self.session._add_spec(t)
            if not spec["path"]:
                continue
            for item in self.menu_items(spec):
                items.append({**item, "label": f"[[{t}]] {item['type']}".strip() if len(tables) > 1 else item["label"]})
        return items

    # Entries

    def add(self, path: str, type_: str = "") -> None:
        app = self.app
        self.state.add_menu = {"open": False, "items": [], "x": 0, "y": 0}
        if path in ("regions", "probes"):  # placed in the domain by the setup editor
            app.setup.act("add_region" if path == "regions" else "add_probe", type_)
            return
        try:
            new = self.session.add_entry(path, type_)
        except (KeyError, ValueError) as e:
            self.state.props = {**self.state.props, "edit_error": str(e)}
            return
        self._edited(new)

    def duplicate(self, path: str) -> None:
        try:
            new = self.session.duplicate_entry(path)
        except (KeyError, ValueError) as e:
            self.state.props = {**self.state.props, "edit_error": str(e)}
            return
        self._edited(new)

    def delete(self, path: str) -> None:
        try:
            self.session.delete_entry(path)
        except (KeyError, ValueError) as e:
            self.state.props = {**self.state.props, "edit_error": str(e)}
            return
        self.state.undo_notice = {"text": f"Deleted {path}", "seq": next(self._seq)}
        node = self.session.node_of_key(format_path(parse_path(path)[:-1]))
        self._edited(None, node)

    def feature(self, path: str, on: bool, content: bool = False) -> None:
        """A feature's switch: on at once; off at once while it holds nothing of the user's, else after the
        confirmation in its group."""
        if not on and content and self.state.feature_confirm != path:
            self.state.feature_confirm = path
            return
        self.state.feature_confirm = ""
        try:
            self.session.set_feature(path, on)
        except (KeyError, ValueError) as e:
            self.state.props = {**self.state.props, "edit_error": str(e)}
            return
        if not on:
            self.state.undo_notice = {
                "text": f"Turned off {path}: its settings left input.toml",
                "seq": next(self._seq),
            }
        self._edited(None)

    def feature_mechanism(self, path: str) -> None:
        """Chemistry on a case without a mechanism: the mechanism file first, then the mixture and the table."""
        root = self.session.input_path.parent

        def chosen(p: Path) -> None:
            try:
                self.session.set_feature(path, True, project_relative(root, p))
            except (KeyError, ValueError) as e:
                self.state.props = {**self.state.props, "edit_error": str(e)}
                return
            self._edited(None)

        self.app.paths.choose("mechanism", root, "", chosen, title="Choose the reaction mechanism")

    def undo(self) -> None:
        self.state.undo_notice = {}
        self.app.document.command("undo")

    def _edited(self, new: str | None, node: str | None = None) -> None:
        app, ses = self.app, self.session
        app._refresh_case()
        app._schedule_check()
        target = node or (ses.node_of_key(new) if new else None)
        if target and any(n["id"] == target for n in self.state.tree):
            app.select(target)
        if new:
            app.document.reveal_lines(ses.doc.spans_of([new]))

    # Keys

    def reset(self, path: str) -> None:
        self.session.reset_key(path)
        self.app._refresh_case()
        self.app._schedule_check()

    def reveal(self, path: str) -> None:
        """input.toml scrolled to a set key's line, highlighted for a moment."""
        if self.session.doc.has(path):
            self.app.document.reveal_lines(self.session.doc.spans_of([path]))

    # Files

    def choose_file(self, path: str) -> None:
        """The file dialog for a key naming a file, in the current file's directory; the choice is stored relative
        to the case's directory when it is inside it."""
        ses = self.session
        root = ses.input_path.parent
        current = str(ses.doc.get(path) or "")
        info = ses.info(path)
        start = (root / current).parent if current else root
        self.app.paths.choose(
            kind_for(current or (info.default_value if info else "") or ""),
            start if start.is_dir() else root,
            Path(current).name,
            lambda p: self.app.set_key(path, project_relative(root, p)),
            title=f"Choose {catalog_path(path)}",
        )

    # Compositions

    def _comp(self, edit: Callable[..., Any], *args: Any) -> None:
        try:
            edit(*args)
        except (KeyError, ValueError) as e:
            self.state.props = {**self.state.props, "edit_error": str(e)}
            return
        self.app._refresh_case()
        self.app._schedule_check()

    def comp_value(self, table: str, species: str, text: str) -> None:
        self._comp(self.session.set_fraction, table, species, str(text))

    def comp_add(self, table: str, species: str) -> None:
        self._comp(self.session.add_species, table, species)

    def comp_rename(self, table: str, old: str, new: str) -> None:
        self._comp(self.session.rename_species, table, old, new)

    def comp_normalize(self, table: str) -> None:
        self._comp(self.session.normalize_composition, table)

    def comp_basis(self, table: str, basis: str) -> None:
        """Mass or mole fractions: converted where Mallard gives the molar masses, else asking to clear."""
        if self.session.switch_basis(table, basis):
            self.state.comp_ask = {}
            self.app._refresh_case()
            self.app._schedule_check()
        else:
            self.state.comp_ask = {"table": table, "basis": basis}

    def cancel(self, name: str) -> None:
        """A question's Cancel, answered here rather than in the page: the page's own write could reach this
        process after the question is asked again, and leave it unasked."""
        if name in ("comp_ask", "oneof_ask", "preset_pending"):
            self.state[name] = {}

    def comp_clear(self) -> None:
        ask = self.state.comp_ask or {}
        self.state.comp_ask = {}
        if ask.get("table"):
            self._comp(self.session.switch_basis, ask["table"], ask["basis"], True)

    # Field lists (`write_data.fields`): picked from the case's fields, written as the array of the entries

    def _picked(self, cat: FieldCatalog, entries: list[str]) -> dict[str, Any]:
        problems = [{"name": e, "problem": p} for e in entries if (p := cat.problem(e))]
        return {"entries": entries, "sel": dict.fromkeys(cat.selected(entries), True), "problems": problems}

    def fields_open(self, path: str) -> None:
        cat = self.session.field_catalog(path)
        if cat is None:
            return
        self.state.fpick_q = ""
        self.state.fpick_groups = cat.groups()
        self.state.fpick = {"open": True, "path": path, **self._picked(cat, self.session.field_entries(path))}

    def _restage(self, edit: Callable[[FieldCatalog, list[str]], list[str]]) -> None:
        fp = self.state.fpick
        cat = self.session.field_catalog(fp.get("path", ""))
        if cat is not None and fp.get("open"):
            self.state.fpick = {**fp, **self._picked(cat, edit(cat, list(fp["entries"])))}

    def fields_toggle(self, names: list[str], on: bool) -> None:
        self._restage(lambda cat, entries: cat.toggle(entries, [str(n) for n in names], bool(on)))

    def fields_drop(self, entry: str) -> None:
        self._restage(lambda _, entries: [e for e in entries if e != entry])

    def fields_apply(self) -> None:
        fp, self.state.fpick = self.state.fpick, {"open": False}
        if fp.get("path") and fp["entries"] != self.session.field_entries(fp["path"]):
            self._comp(self.session.set_fields, fp["path"], fp["entries"])

    def fields_remove(self, path: str, entry: str) -> None:
        self._comp(self.session.set_fields, path, [e for e in self.session.field_entries(path) if e != entry])

    def fields_cell(self) -> None:
        """A field list's entries as chips (a problem in red, with why), and the picker."""
        with html.Div(v_if="r.widget === 'fields'", classes="fchips"):
            with html.Span(
                v_for="e in r.fields",
                key="e.name",
                classes=("['fchip', {bad: !!e.problem}]",),
                title=("e.problem || e.covers",),
            ):
                html.Span("{{ e.name }}", classes="fchip-name")
                html.Button(
                    v_html="icons.x",
                    classes="fchip-x",
                    title=("'Remove ' + e.name",),
                    aria_label=("'Remove ' + e.name + ' from ' + r.path",),
                    click=(self.fields_remove, "[r.path, e.name]"),
                )
            html.Button(
                "{{ r.fields.length ? 'Edit' : 'Choose fields' }}",
                classes="linkbtn fields-open",
                title=("'Choose the fields of ' + r.path",),
                aria_label=("'Choose ' + r.path",),
                click=(self.fields_open, "[r.path]"),
            )

    def fields_dialog(self) -> None:
        """The field picker: the case's fields by Mallard's groups, filtered, each with what it is and its unit;
        a group's box selects all it shows; entries that write nothing listed first, with why."""
        match = "(f.name + ' ' + f.description).toLowerCase().includes(fpick_q.trim().toLowerCase())"
        groups = f"fpick_groups.map(g => ({{...g, shown: g.fields.filter(f => {match})}})).filter(g => g.shown.length)"
        all_on = "g.shown.every(f => fpick.sel[f.name])"
        with html.Div(classes="modal-scrim fpick", v_if="fpick.open"):
            with html.Div(
                classes="modal fpick-modal",
                role="dialog",
                aria_label=("'Fields of ' + fpick.path",),
                keydown_esc="fpick = {open: false}",
            ):
                with html.Div(classes="modal-head"):
                    html.Span("Output fields")
                    html.Span("{{ fpick.path }}", classes="mono muted")
                    html.Div(classes="grow")
                    html.Span("{{ Object.keys(fpick.sel).length }} selected", classes="muted fpick-count")
                with html.Div(classes="fpick-bar"):
                    html.Span(v_html="icons.search", classes="fpick-ico")
                    html.Input(
                        classes="fpick-search",
                        value=("fpick_q",),
                        placeholder="Filter by name or description",
                        aria_label="Filter fields",
                        spellcheck="false",
                        input="fpick_q = $event.target.value",
                    )
                with html.Div(v_if="fpick.problems.length", classes="fpick-problems", role="list"):
                    with html.Div(v_for="p in fpick.problems", key="p.name", classes="fpick-problem", role="listitem"):
                        html.Span("{{ p.name }}", classes="mono fpick-name")
                        html.Span("{{ p.problem }}", classes="grow fpick-why")
                        html.Button(
                            "Remove",
                            classes="linkbtn",
                            aria_label=("'Remove ' + p.name",),
                            click=(self.fields_drop, "[p.name]"),
                        )
                with html.Div(classes="modal-body fpick-body"):
                    with html.Div(v_for=f"g in {groups}", key="g.group", classes="fpick-group"):
                        with html.Label(classes="fpick-ghead"):
                            html.Input(
                                type="checkbox",
                                checked=(all_on,),
                                indeterminate=(f"!{all_on} && g.shown.some(f => fpick.sel[f.name])",),
                                __properties=["indeterminate"],
                                aria_label=("'All ' + g.label + ' fields'",),
                                change=(self.fields_toggle, "[g.shown.map(f => f.name), $event.target.checked]"),
                            )
                            html.Span("{{ g.label }}", classes="fpick-gname")
                            html.Span(
                                "{{ g.shown.filter(f => fpick.sel[f.name]).length }} of {{ g.shown.length }}",
                                classes="muted fpick-gcount",
                            )
                        with html.Label(
                            v_for="f in g.shown",
                            key="f.name",
                            classes=("['fpick-row', {on: !!fpick.sel[f.name]}]",),
                        ):
                            html.Input(
                                type="checkbox",
                                checked=("!!fpick.sel[f.name]",),
                                aria_label=("f.name",),
                                change=(self.fields_toggle, "[[f.name], $event.target.checked]"),
                            )
                            html.Span("{{ f.name }}", classes="mono fpick-name")
                            with html.Span(classes="fpick-desc", title=("f.description",)):
                                html.Span("{{ f.description }}")
                                html.Span("on {{ f.needs }}", v_if="f.needs", classes="muted fpick-needs")
                            html.Span("{{ f.units }}", classes="mono muted fpick-unit")
                    html.Div(
                        "no field matches “{{ fpick_q }}”",
                        v_if=f"!({groups}).length",
                        classes="muted prop-empty",
                    )
                with html.Div(classes="actions doc-dialog-actions"):
                    html.Span(
                        "fields = [{{ fpick.entries.map(e => JSON.stringify(e)).join(', ') }}]",
                        classes="mono muted grow fpick-toml",
                        title=("fpick.entries.join(', ')",),
                    )
                    html.Button("Cancel", classes="btn", click="fpick = {open: false}")
                    html.Button("Apply", classes="btn primary", click=self.fields_apply)

    # Alternatives (keys Mallard takes one of)

    def oneof_pick(self, table: str, n: int, branch: int) -> None:
        if self.session.pick_alternative(table, int(n), int(branch)):
            self.state.oneof_ask = {}
            self.state.props = self.props(self.state.selected)
        else:
            self.state.oneof_ask = {"table": table, "n": int(n), "branch": int(branch)}

    def oneof_clear(self, table: str, n: int, branch: int) -> None:
        """Use a branch, clearing the keys the others set (asked first, or offered for a conflict)."""
        self.state.oneof_ask = {}
        self.session.pick_alternative(table, int(n), int(branch), clear=True)
        self.app._refresh_case()
        self.app._schedule_check()

    def oneof_body(self) -> None:
        """A selector of alternatives: which one is in use, switching (asking before it clears keys), and a
        conflict where an unused one sets keys too."""
        with html.Div(v_if="r.widget === 'oneof'", classes="oneof"):
            with html.Div(
                classes="segmented", role="radiogroup", aria_label=("'one of: ' + r.oneof.labels.join(', ')",)
            ):
                html.Button(
                    "{{ label }}",
                    v_for="(label, k) in r.oneof.labels",
                    key="label",
                    role="radio",
                    classes=("['seg', {on: r.oneof.branches[k] === r.oneof.active}]",),
                    __properties=[("aria_checked", "aria-checked")],
                    aria_checked=("String(r.oneof.branches[k] === r.oneof.active)",),
                    title=("'Use ' + label + ' (Mallard takes one of these)'",),
                    click=(self.oneof_pick, "[r.oneof.table, r.oneof.n, r.oneof.branches[k]]"),
                )
            with html.Div(
                v_if="oneof_ask.table === r.oneof.table && oneof_ask.n === r.oneof.n",
                classes="comp-ask oneof-ask",
                role="alertdialog",
            ):
                html.Span(
                    "Use {{ r.oneof.labels[r.oneof.branches.indexOf(oneof_ask.branch)] }}: clear "
                    "{{ r.oneof.clear.map(p => p.split('.').pop()).join(', ') }}?"
                )
                with html.Div(classes="actions"):
                    html.Button("Cancel", classes="btn", click="trigger('insp_cancel', ['oneof_ask'])")
                    html.Button(
                        "Clear and switch",
                        classes="btn primary",
                        click=(self.oneof_clear, "[oneof_ask.table, oneof_ask.n, oneof_ask.branch]"),
                    )
            with html.Div(v_if="r.oneof.conflict.length", classes="comp-ask oneof-conflict", role="alert"):
                html.Span(
                    "Mallard takes one of these: {{ r.oneof.conflict.map(p => p.split('.').pop()).join(', ') }} "
                    "{{ r.oneof.conflict.length === 1 ? 'is' : 'are' }} set too."
                )
                with html.Div(classes="actions"):
                    html.Button(
                        "Clear {{ r.oneof.conflict.map(p => p.split('.').pop()).join(', ') }}",
                        classes="btn primary",
                        click=(self.oneof_clear, "[r.oneof.table, r.oneof.n, r.oneof.active]"),
                    )

    # Presets

    def schedule_presets(self) -> None:
        if self._preset_task and not self._preset_task.done():
            return
        if not self.session.wants_presets(self.state.selected):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # before the server runs: the first selection schedules it
            return
        self._preset_task = asyncio.ensure_future(self._refresh_presets())

    async def _refresh_presets(self) -> None:
        try:
            await asyncio.to_thread(self.session.refresh_presets)
        except (MallardError, OSError) as e:
            with self.state:
                self.state.props = {**self.state.props, "preset": {"error": f"presets: {e}"}}
            return
        with self.state:
            self.state.props = self.props(self.state.selected)
        if self.session.wants_presets(self.state.selected):  # edited meanwhile
            self._preset_task = asyncio.ensure_future(self._refresh_presets())

    async def preset_choose(self, key: str, name: str) -> None:
        """What switching to preset `name` changes, keeping and dropping the overrides, for the user to choose."""
        s = self.state
        if not name or name == (s.props.get("preset") or {}).get("active"):
            s.preset_pending = {}
            return
        s.preset_pending = {"key": key, "name": name, "loading": True}
        keep, drop = await asyncio.gather(
            asyncio.to_thread(self.session.preset_switch, key, name, True),
            asyncio.to_thread(self.session.preset_switch, key, name, False),
        )
        kept = {c["key"] for c in keep[0]}
        with s:
            s.preset_pending = {
                "key": key,
                "name": name,
                "keep": keep[0],
                "mine": [c for c in drop[0] if c["key"] not in kept],  # what only replacing the user's changes does
                "error": keep[1] or drop[1] or "",
            }

    def preset_apply(self, keep: bool) -> None:
        pending = self.state.preset_pending or {}
        self.state.preset_pending = {}
        if not pending.get("name"):
            return
        self.session.apply_preset(pending["key"], pending["name"], bool(keep))
        self.state.undo_notice = {"text": f"Now using {pending['name']}", "seq": next(self._seq)}
        self.app._refresh_case()
        self.app._schedule_check()
        self.app.document.reveal_lines(self.session.doc.spans_of([pending["key"]]))

    # Layout

    def layout(self) -> None:
        """The panel's scrolling body: notices, the preset bar, the groups."""
        html.Div("{{ props.catalog_error }}", v_if="props.catalog_error", classes="prop-notice warn")
        with html.Div(v_if="undo_notice.text", classes="prop-notice"):
            html.Span("{{ undo_notice.text }}", classes="grow")
            html.Button("Undo", classes="linkbtn", click=self.undo)
        self._preset_bar()
        with html.Div(v_for="g in props.groups", key="g.id", v_show=PARENTS_OPEN, classes="pgroup"):
            with disclosure(
                GROUP_OPEN,
                _toggle("prop_toggled", "g.id"),
                classes="group-head",
                style=("{paddingLeft: (4 + 16 * g.depth) + 'px'}",),
            ):
                html.Span("{{ g.title }}", classes="gtitle", title=("g.id",))
                html.Span("{{ g.note || g.rows.length }}", classes="note", v_if="!g.feature.path || g.feature.on")
                with html.Button(
                    v_if="g.feature.path",
                    role="switch",
                    classes=("['fswitch', {on: g.feature.on}]",),
                    __properties=[("aria_checked", "aria-checked")],
                    aria_checked=("String(g.feature.on)",),
                    aria_label=("g.feature.name",),
                    title=(
                        "g.feature.name + (g.feature.on ? ' is on' : ' is off') + (g.feature.key ? "
                        "': on while [' + g.feature.path + '] is in the input and ' + g.feature.key + ' is not false'"
                        " : ': on while the input has [' + g.feature.path + ']')",
                    ),
                    __events=[("click_stop", "click.stop")],
                    click_stop=(
                        "g.feature.on ? trigger('insp_feature', [g.feature.path, false, g.feature.content]) : "
                        "g.feature.mechanism ? trigger('insp_feature_mech', [g.feature.path]) : "
                        "trigger('insp_feature', [g.feature.path, true])"
                    ),
                ):
                    html.Span(classes="knob")
                    html.Span("{{ g.feature.on ? 'On' : 'Off' }}", classes="flabel")
                html.Button(
                    v_html="icons.plus",
                    v_if="g.add && g.add.path",
                    classes="icon-btn gact",
                    title=("'Add an entry to ' + g.add.path",),
                    aria_label=("'Add ' + g.add.path",),
                    __events=[("click_stop", "click.stop")],
                    click_stop=self._open_menu("g.add.items"),
                )
                html.Button(
                    v_html="icons.copy",
                    v_if="g.entry",
                    classes="icon-btn gact",
                    title=("'Duplicate ' + g.entry",),
                    __events=[("click_stop", "click.stop")],
                    click_stop=(self.duplicate, "[g.entry]"),
                )
                html.Button(
                    v_html="icons.trash",
                    v_if="g.entry",
                    classes="icon-btn gact",
                    title=("'Delete ' + g.entry",),
                    __events=[("click_stop", "click.stop")],
                    click_stop=(self.delete, "[g.entry]"),
                )
            with html.Div(
                v_if="g.feature.path && (!g.feature.on || feature_confirm === g.feature.path)",
                classes="feature-off",
                style=("{marginLeft: (16 * g.depth) + 'px'}",),
            ):
                with html.Template(v_if="feature_confirm === g.feature.path"):
                    html.Div(
                        "Turn off {{ g.feature.name }}? Its settings leave {{ input_name }}; Undo brings them back.",
                        classes="fo-text",
                    )
                    with html.Div(classes="fo-actions"):
                        html.Button("Keep it on", classes="btn", click="feature_confirm = ''", aria_label="keep it on")
                        html.Button(
                            "Turn off",
                            classes="btn danger",
                            click=(self.feature, "[g.feature.path, false, true]"),
                        )
                with html.Template(v_else=""):
                    html.Div("{{ g.feature.blurb }} Off.", classes="fo-text")
                    html.Div(
                        "It needs a reacting mixture: Turn on asks for the mechanism (a Cantera YAML file) and sets "
                        "Physics' gas to mixture.",
                        classes="fo-text",
                        v_if="g.feature.mechanism",
                    )
                    with html.Div(classes="fo-actions"):
                        html.Button(
                            "Turn on",
                            classes="btn primary",
                            click=(
                                "g.feature.mechanism ? trigger('insp_feature_mech', [g.feature.path]) : "
                                "trigger('insp_feature', [g.feature.path, true])"
                            ),
                        )
            with html.Div(
                v_if=GROUP_OPEN + " && (!g.feature.path || g.feature.on)",
                classes="pgroup-body",
                style=("{'--indent': (16 * g.depth) + 'px'}",),
            ):
                with html.Div(v_if="!g.rows.length && g.add && g.add.path", classes="prop-empty list-empty"):
                    html.Span("None yet: the case has no {{ g.add.path }} until you add one.")
                    html.Button(
                        "Add",
                        classes="btn",
                        aria_label=("'Add the first of ' + g.add.path",),
                        click=self._open_menu("g.add.items"),
                    )
                self.app._prop_rows()
        self._key_help_and_menu()

    def _open_menu(self, items: str) -> str:
        return (
            f"{items}.length === 1 && !{items}[0].type ? trigger('insp_add', [{items}[0].path, '']) : "
            f"add_menu = {{open: true, items: {items}, x: $event.clientX, y: $event.clientY}}"
        )

    def vector(self, path: str, texts: list[str], unit: str = "") -> None:
        """A vector's components as typed (numbers in `unit`): written once all are valid, else each problem shown
        at its box."""
        errors = self.session.set_vector(path, [str(t) for t in texts], str(unit or ""))
        self.state.vec_err = {path: errors, "texts": [str(t) for t in texts]} if any(errors) else {}
        if not any(errors):
            self._edited(None)

    def vector_cell(self) -> None:
        """A vector key's boxes, one per component, inside its row's value cell."""
        with html.Div(v_if="!r.readonly && r.widget === 'vector'", classes="kvec"):
            with html.Label(
                v_for="(lab, i) in r.vector.labels",
                key="lab",
                classes=("['vcomp', {bad: (vec_err[r.path] || [])[i]}]",),
                title=("(vec_err[r.path] || [])[i] ? lab + ': ' + vec_err[r.path][i] : lab",),
            ):
                html.Span("{{ lab }}", classes="vlab")
                html.Input(
                    value=("vec_err[r.path] ? vec_err.texts[i] : r.vector.values[i]",),
                    placeholder=("r.vector.placeholders[i]",),
                    aria_label=("r.path + ' ' + lab",),
                    spellcheck="false",
                    change=(
                        self.vector,
                        "[r.path, [...$event.target.closest('.kvec').querySelectorAll('input')].map(e => e.value),"
                        " r.vector.unit]",
                    ),
                    keydown_enter="$event.target.blur()",
                )
            dropdown(
                "r.vector.unit",
                "r.vector.units",
                (self.show_unit, "[r.path, $event.detail]"),
                ("r.path + ' unit'",),
                classes="qunit",
                v_if="r.vector.units && r.vector.units.length",
            )

    def show_unit(self, path: str, unit: str) -> None:
        """The unit a dimensional key's boxes show; the input keeps its SI value."""
        try:
            self.session.show_unit(path, str(unit))
        except (ValueError, KeyError) as e:
            self.state.props = {**self.state.props, "edit_error": str(e)}
            return
        self.state.props = self.props(self.state.selected)

    def points(self, path: str, rows: list[list[str]], unit: str = "") -> None:
        """A list of points as typed (numbers in `unit`): written once every component is a number, else each
        problem at its box."""
        texts = [[str(t) for t in r] for r in rows]
        errors = self.session.set_points(path, texts, str(unit or ""))
        kept = [r for r in texts if any(t.strip() for t in r)]
        self.state.pts_err = {path: errors, "texts": kept} if any(any(e) for e in errors) else {}
        if not any(any(e) for e in errors):
            self._edited(None)

    def points_body(self) -> None:
        """A list of points: a row of boxes per point, a blank row to add one, a button to remove each; full width
        under its row."""
        rows = "(pts_err[r.path] ? pts_err.texts : r.points.rows)"
        collect = (
            "[...$event.target.closest('.kpts').querySelectorAll('.kvec')]"
            ".map(v => [...v.querySelectorAll('input')].map(e => e.value))"
        )
        with html.Div(v_if="!r.readonly && r.widget === 'points'", classes="kpts"):
            with html.Div(v_for=f"(pt, j) in [...{rows}, null]", key="j", classes="kpt"):
                with html.Div(classes="kvec"):
                    with html.Label(
                        v_for="(lab, i) in r.points.labels",
                        key="lab",
                        classes=("['vcomp', {bad: ((pts_err[r.path] || [])[j] || [])[i]}]",),
                        title=("((pts_err[r.path] || [])[j] || [])[i] ? lab + ': ' + pts_err[r.path][j][i] : lab",),
                    ):
                        html.Span("{{ lab }}", classes="vlab")
                        html.Input(
                            value=("pt ? pt[i] : ''",),
                            placeholder=("pt ? '' : (i ? '' : 'add a point')",),
                            aria_label=("r.path + ' ' + (j + 1) + ' ' + lab",),
                            spellcheck="false",
                            change=(self.points, f"[r.path, {collect}, r.points.unit]"),
                            keydown_enter="$event.target.blur()",
                        )
                html.Button(
                    v_html="icons.x",
                    classes="icon-btn",
                    style=("{visibility: pt ? 'visible' : 'hidden'}",),  # the blank row's boxes line up too
                    title="Remove this point",
                    aria_label=("'remove point ' + (j + 1)",),
                    click=(self.points, f"[r.path, {collect}.filter((_, k) => k !== j), r.points.unit]"),
                )
            message(
                "(pts_err[r.path] || []).map((e, j) => e.some(Boolean) ? 'point ' + (j + 1) + ': ' + "
                "r.points.labels.map((l, i) => e[i] ? l + ' ' + e[i] : '').filter(Boolean).join(', ') : '')"
                ".filter(Boolean).join('; ')",
                "perr",
                v_if="(pts_err[r.path] || []).some(e => e.some(Boolean))",
            )

    def vector_error(self) -> None:
        """A vector's components' problems, under its row."""
        message(
            "r.vector.labels.map((l, i) => (vec_err[r.path] || [])[i] ? l + ': ' + vec_err[r.path][i] : '')"
            ".filter(Boolean).join(', ')",
            "perr",
            v_if="r.widget === 'vector' && (vec_err[r.path] || []).some(Boolean)",
        )

    def set_choice(self, path: str, choice: str, on: bool) -> None:
        self._comp(self.session.set_choice, path, choice, bool(on))

    def checks_cell(self) -> None:
        """A set of choices' summary, in its row's value cell: the values on, else what leaving it out means."""
        html.Div(
            "{{ r.checks.filter(c => c.on).map(c => c.value).join(', ') || 'none' + (r.set ? '' : ' (default)') }}",
            v_if="r.widget === 'checks'",
            classes=("['ro checks-summary', {dim: !r.checks.some(c => c.on)}]",),
        )

    def checks_body(self) -> None:
        """A set of choices, full width under its row: a box per accepted value with what it does and, quietly
        after its name, how much it costs (the catalog's `value_notes`)."""
        note = "r.notes[c.value]"
        with html.Div(
            v_if="!r.readonly && r.widget === 'checks'", classes="checks", role="group", aria_label=("r.path",)
        ):
            with html.Label(
                v_for="c in r.checks",
                key="c.value",
                classes=("['check', {on: c.on, unknown: c.unknown}]",),
            ):
                html.Input(
                    type="checkbox",
                    checked=("c.on",),
                    aria_label=("r.path + ' ' + c.value",),
                    change=(self.set_choice, "[r.path, c.value, $event.target.checked]"),
                )
                with html.Span(classes="check-text"):
                    with html.Span(classes="check-head"):
                        html.Span("{{ c.value }}", classes="check-name")
                        html.Span(
                            "{{ " + note + ".cost }}",
                            v_if=f"{note} && {note}.cost",
                            classes=(f"['cost', {note}.cost]",),
                        )
                        html.Span("not accepted by this build", v_if="c.unknown", classes="check-why")
                    html.Span("{{ " + note + ".summary }}", v_if=f"{note} && {note}.summary", classes="check-summary")
                    html.Span("{{ " + note + ".why }}", v_if=f"{note} && {note}.why", classes="check-why")

    def model_button(self) -> None:
        """A choice of models from the literature: (i) opens the theory section of the one chosen (else the default)
        on the website."""
        model = "r.models[r.value || r.placeholder]"
        html.Button(
            v_html="icons.info",
            v_if=f"!r.readonly && r.widget === 'select' && {model}",
            classes="icon-btn pinfo",
            title=(f"{model}.tip",),
            aria_label=("'Theory of ' + r.path + ' = ' + (r.value || r.placeholder)",),
            click=(self.open_theory, f"[{model}.url]"),
        )

    def open_theory(self, url: str) -> None:
        if url.startswith(SITE):
            self.app.document._shell("openUrl", [url])

    def file_button(self) -> None:
        """A key naming a file: the file dialog, after its field."""
        html.Button(
            v_html="icons.folder",
            v_if="!r.readonly && r.file",
            classes="icon-btn pfile",
            title=("'Choose a file for ' + r.path",),
            aria_label=("'Choose a file for ' + r.path",),
            click=(self.choose_file, "[r.path]"),
        )

    def composition_head(self) -> None:
        """A composition's summary, in its row's value cell."""
        html.Div(
            "{{ r.comp.entries.length }} species · {{ r.comp.basis }}",
            v_if="r.widget === 'composition'",
            classes=("['ro comp-summary', {dim: !r.comp.entries.length}]",),
        )

    def composition_body(self) -> None:
        """A composition's species, each with its fraction, and Add species; full width under its row."""
        with html.Div(v_if="r.widget === 'composition'", classes="comp-body"):
            with html.Div(classes="comp-head"):
                with html.Div(classes="segmented comp-basis", role="group", aria_label=("r.comp.table + ' basis'",)):
                    for basis, label, tip in (("X", "Mole (X)", "mole fractions"), ("Y", "Mass (Y)", "mass fractions")):
                        html.Button(
                            label,
                            classes=(f"['seg', {{on: r.comp.basis === '{basis}'}}]",),
                            title=f"Give the composition as {tip} ({basis})",
                            aria_pressed=(f"String(r.comp.basis === '{basis}')",),
                            __properties=[("aria_pressed", "aria-pressed")],
                            click=(self.comp_basis, f"[r.comp.table, '{basis}']"),
                        )
                html.Span(
                    "sum {{ r.comp.sum || (r.comp.entries.length ? 'expr' : '0') }}",
                    classes=("['comp-sum mono', {off: r.comp.sum && !r.comp.normal}]",),
                    title=(
                        "r.comp.sum ? 'The fractions sum to ' + r.comp.sum + (r.comp.normal ? '' : "
                        "' (Mallard normalizes them; Normalize writes them so)') : "
                        "'A fraction is an expression: Mallard normalizes them where it evaluates them'",
                    ),
                )
                html.Button(
                    "Normalize",
                    classes="linkbtn comp-norm",
                    disabled=("!r.comp.sum || r.comp.normal || r.comp.sum === '0'",),
                    title="Scale the fractions so they sum to 1",
                    click=(self.comp_normalize, "[r.comp.table]"),
                )
            with html.Div(v_if="comp_ask.table === r.comp.table", classes="comp-ask", role="alertdialog"):
                html.Span(
                    "Mallard does not give this mechanism's molar masses, so the fractions cannot be converted to "
                    "{{ comp_ask.basis === 'Y' ? 'mass' : 'mole' }} fractions. Clear them and switch?"
                )
                with html.Div(classes="actions"):
                    html.Button("Cancel", classes="btn", click="trigger('insp_cancel', ['comp_ask'])")
                    html.Button("Clear and switch", classes="btn primary", click=self.comp_clear)
            with html.Div(v_for="e in r.comp.entries", key="e.species", classes=("['comp-entry', {bad: e.unknown}]",)):
                dropdown(
                    "e.species",
                    "r.comp.species",
                    (self.comp_rename, "[r.comp.table, e.species, $event.detail]"),
                    ("r.comp.table + '.' + r.comp.basis + ' species ' + e.species",),
                    free="!r.comp.species.length && !!r.comp.species_error",
                    classes="comp-species",
                    title=("e.unknown ? 'The mechanism has no species ' + e.species : e.species",),
                )
                html.Input(
                    value=("e.value",),
                    aria_label=("r.comp.table + '.' + r.comp.basis + '.' + e.species",),
                    classes="comp-value mono",
                    spellcheck="false",
                    title=("e.value",),
                    change=(self.comp_value, "[r.comp.table, e.species, $event.target.value]"),
                    keydown_enter="$event.target.blur()",
                )
                html.Button(
                    v_html="icons.x",
                    classes="icon-btn pact",
                    title=("'Remove ' + e.species",),
                    aria_label=("'Remove ' + e.species",),
                    click=(self.comp_value, "[r.comp.table, e.species, '']"),
                )
            with html.Div(classes="comp-foot"):
                dropdown(
                    "''",
                    "r.comp.species",
                    (self.comp_add, "[r.comp.table, $event.detail]"),
                    ("'Add species to ' + r.comp.table + '.' + r.comp.basis",),
                    placeholder="'+ Add species'",
                    free="!!r.comp.species_error",
                    classes="comp-add",
                )
                html.Span(
                    "species unknown: {{ r.comp.species_error }}",
                    v_if="r.comp.species_error",
                    classes="muted comp-note",
                    title=("r.comp.species_error",),
                )

    def row_extras(self) -> None:
        """A row's tags and buttons after its value: where its value comes from, Reset, Duplicate and Delete."""
        html.Span("3D only", classes="ptag dim", v_if="r.other_dim", title=("r.other_dim",))
        html.Span(
            "{{ r.origin === 'override' ? 'override' : 'preset' }}",
            v_if="r.origin",
            classes=("['ptag', r.origin]",),
            title=(
                "r.origin === 'override' ? 'set over the preset (' + (r.help.preset || '') + ')' : "
                "'from preset ' + (r.help.preset || '')",
            ),
        )
        html.Button(
            v_html="icons.reset",
            v_if="r.set && r.path && (!r.readonly || r.other_dim)",
            classes="icon-btn pact",
            title=(
                "r.origin === 'override' ? 'Revert to the preset (' + (r.help.preset || '') + ')' : "
                "'Reset to the default (removes ' + r.label + ' from the input)'",
            ),
            aria_label=("'Reset ' + r.path",),
            click=(self.reset, "[r.path]"),
        )
        html.Button(
            v_html="icons.copy",
            v_if="r.entry",
            classes="icon-btn pact",
            title=("'Duplicate ' + r.entry",),
            click=(self.duplicate, "[r.entry]"),
        )
        html.Button(
            v_html="icons.trash",
            v_if="r.entry",
            classes="icon-btn pact",
            title=("'Delete ' + r.entry",),
            click=(self.delete, "[r.entry]"),
        )

    def _preset_bar(self) -> None:
        with html.Div(v_if="props.preset && props.preset.key", classes="preset-bar"):
            with html.Div(classes="preset-row"):
                html.Span("PRESET", classes="preset-label")
                dropdown(
                    "props.preset.active",
                    "(props.preset.options || []).map(o => ({value: o.name, label: o.name, note: o.summary || ''}))",
                    (self.preset_choose, "[props.preset.key, $event.detail]"),
                    "preset",
                    placeholder="'none'",
                    classes="preset-dd",
                    disabled=("!!props.preset.loading",),
                )
            message("props.preset.error", "preset-status err", v_if="props.preset.error && !props.preset.loading")
            html.Div(
                "{{ props.preset.loading ? 'resolving presets with mallard input resolve…' : props.preset.status }}",
                classes="preset-status",
                v_else=True,
            )
            with html.Div(v_if="(props.preset.overrides || []).length", classes="preset-overrides"):
                with html.Div(v_for="o in props.preset.overrides", key="o.key", classes="preset-ov"):
                    html.Span("{{ o.key }}", classes="mono k", title=("o.key",))
                    html.Span("{{ o.value }}", classes="mono")
                    html.Span("preset: {{ o.preset }}", classes="muted")
                    html.Button(
                        "Revert",
                        classes="linkbtn",
                        title=("'Revert ' + o.key + ' to the preset (' + o.preset + ')'",),
                        click=(self.reset, "[o.key]"),
                    )
            with html.Div(v_if="preset_pending.name", classes="preset-pending", role="dialog"):
                html.Div("Switch to {{ preset_pending.name }}?", classes="preset-pending-title")
                html.Div("comparing with mallard input resolve…", v_if="preset_pending.loading", classes="muted")
                message("preset_pending.error", "err", v_if="preset_pending.error")
                with html.Template(v_if="!preset_pending.loading"):
                    with html.Div(classes="preset-diff"):
                        html.Div(
                            "{{ preset_pending.name }} sets {{ preset_pending.keep.length }} "
                            "key{{ preset_pending.keep.length === 1 ? '' : 's' }} either way:",
                            classes="muted",
                        )
                        with html.Div(v_for="c in preset_pending.keep", key="c.key", classes="preset-chg"):
                            html.Span("{{ c.key }}", classes="mono k", title=("c.key",))
                            html.Span("{{ c.old }} → {{ c.new }}", classes="mono")
                    with html.Div(classes="preset-diff mine", v_if="preset_pending.mine.length"):
                        html.Div(
                            "Your {{ preset_pending.mine.length }} "
                            "change{{ preset_pending.mine.length === 1 ? '' : 's' }} to its keys "
                            "(yours → the preset's):",
                            classes="muted",
                        )
                        with html.Div(v_for="c in preset_pending.mine", key="c.key", classes="preset-chg"):
                            html.Span("{{ c.key }}", classes="mono k", title=("c.key",))
                            html.Span("{{ c.old }} → {{ c.new }}", classes="mono")
                    with html.Div(classes="actions"):
                        html.Button("Cancel", classes="btn", click="trigger('insp_cancel', ['preset_pending'])")
                        html.Button(
                            "Use {{ preset_pending.name }}, replace my {{ preset_pending.mine.length }} "
                            "change{{ preset_pending.mine.length === 1 ? '' : 's' }}",
                            classes="btn",
                            v_if="preset_pending.mine.length",
                            click=(self.preset_apply, "[false]"),
                        )
                        html.Button(
                            "{{ preset_pending.mine.length ? 'Use ' + preset_pending.name + ', keep my ' + "
                            "preset_pending.mine.length + ' change' + (preset_pending.mine.length === 1 ? '' : 's') : "
                            "'Use ' + preset_pending.name }}",
                            classes="btn primary",
                            click=(self.preset_apply, "[true]"),
                        )

    def key_help(self) -> None:
        """The hovered or focused key's catalog entry, under the groups."""
        with html.Div(classes="key-help", v_if="key_help && key_help.path", aria_live="polite"):
            html.Div("{{ key_help.path }}", classes="kh-path mono")
            html.Div(
                "{{ [key_help.type, key_help.units, key_help.default && ('default ' + key_help.default), "
                "key_help.dims].filter(Boolean).join(' · ') }}",
                classes="kh-meta mono",
            )
            html.Div("one of: {{ key_help.values }}", v_if="key_help.values", classes="kh-meta mono")
            html.Div("{{ key_help.description }}", v_if="key_help.description", classes="kh-desc")
            html.Div("applies {{ key_help.when }}", v_if="key_help.when", classes="kh-desc muted")
            html.Div("preset {{ key_help.preset }}", v_if="key_help.preset", classes="kh-meta mono")

    def _key_help_and_menu(self) -> None:
        with html.Div(classes="add-scrim", v_if="add_menu.open", click="add_menu = {...add_menu, open: false}"):
            with html.Div(
                classes="add-menu",
                role="menu",
                style=("{left: Math.min(add_menu.x, window.innerWidth - 240) + 'px', top: add_menu.y + 'px'}",),
                __events=[("click_stop", "click.stop")],
            ):
                html.Div(
                    "{{ i.label }}",
                    v_for="i in add_menu.items",
                    key="i.path + i.type",
                    classes="add-item",
                    role="menuitem",
                    tabindex="0",
                    click=(self.add, "[i.path, i.type]"),
                    keydown_enter=(self.add, "[i.path, i.type]"),
                )
