"""Style-preserving editing of a case's `input.toml`: values change in place, comments,
order and layout survive.

Edits patch the text by the span of the key they change. tomlkit formats new values and
measures the raw text of old ones; its documents are not dumped back, since tomlkit (0.15)
regroups the elements of an array of tables written in two places of the file, as Mallard's
examples do (`[[write_data]]` before and after `[statistics]`).

Paths are Mallard's key paths: `run.cfl`, `boundaries[1].type`, and the selectors its
messages and case-view blocks use, `boundaries[name = "farfield"].T`.
"""

from __future__ import annotations

import contextlib
import copy
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from .tomlio import write_atomic

Segment = str | int | tuple[str, str]
Resolved = list[str | int]

_SEGMENT = re.compile(
    r"""(?P<key>[A-Za-z0-9_\-]+)
        | \[\s*(?P<index>\d+)\s*\]
        | \[\s*(?P<skey>[A-Za-z0-9_]+)\s*=\s*"(?P<sval>(?:[^"\\]|\\.)*)"\s*\]
        | \.(?P<dotkey>[A-Za-z0-9_\-]+)""",
    re.VERBOSE,
)


def parse_path(path: str) -> list[Segment]:
    """`a.b[2].c` -> ['a', 'b', 2, 'c']; `b[name = "x"]` -> ['b', ('name', 'x')]."""
    segments: list[Segment] = []
    pos = 0
    while pos < len(path):
        m = _SEGMENT.match(path, pos)
        if not m:
            raise ValueError(f"cannot read key path {path!r} at {path[pos:]!r}")
        if m["key"] is not None and not segments:
            segments.append(m["key"])
        elif m["dotkey"] is not None and segments:
            segments.append(m["dotkey"])
        elif m["index"] is not None:
            segments.append(int(m["index"]))
        elif m["skey"] is not None:
            segments.append((m["skey"], m["sval"].replace('\\"', '"')))
        else:
            raise ValueError(f"cannot read key path {path!r} at {path[pos:]!r}")
        pos = m.end()
    if not segments:
        raise ValueError("empty key path")
    return segments


def format_path(segments: list[Segment] | Resolved) -> str:
    out = ""
    for seg in segments:
        if isinstance(seg, int):
            out += f"[{seg}]"
        elif isinstance(seg, tuple):
            out += f'[{seg[0]} = "{seg[1]}"]'
        else:
            out += ("." if out else "") + seg
    return out


def parse_value(text: str, type_: str | None = None) -> Any:
    """A value typed in a field: TOML syntax (`[1, 2]`, `true`, `"a"`), or a bare string."""
    text = text.strip()
    if type_ == "string" and not text.startswith(('"', "'")):
        return text
    try:
        value = tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        if type_ in ("number", "integer", "bool", "array", "table"):
            raise ValueError(f"{text!r} is not a valid {type_}") from None
        return text
    if type_ == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{text!r} is not an integer")
    if type_ == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise ValueError(f"{text!r} is not a number")
    if type_ == "bool" and not isinstance(value, bool):
        raise ValueError(f"{text!r} is not true or false")
    return value


def format_value(value: Any) -> str:
    """A value as TOML, for the file and for form fields."""
    return str(_inline(value).as_string()).strip()


def _inline(value: Any) -> Any:
    if isinstance(value, dict):
        table = tomlkit.inline_table()
        for k, v in value.items():
            table.append(k, _inline(v))
        return table
    if isinstance(value, list):
        array = tomlkit.array()
        for v in value:
            array.append(_inline(v))
        return array
    return tomlkit.item(value)


def display_value(value: Any) -> str:
    """A value for a form field: strings bare, everything else as TOML."""
    return value if isinstance(value, str) else format_value(value)


@dataclass
class Element:
    """An array-of-tables element, as the case tree shows it."""

    array: str
    index: int
    label: str
    kind: str


@dataclass
class _Entry:
    """A header or a key written in the file, with the lines it spans (0-based, inclusive)."""

    path: Resolved
    first: int
    last: int
    header: bool
    value_col: int = 0  # where the value starts on the first line


_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?$")
_KEY = re.compile(r"^(\s*)((?:[A-Za-z0-9_\-]+|\"[^\"]*\")(?:\s*\.\s*(?:[A-Za-z0-9_\-]+|\"[^\"]*\"))*)\s*=\s*")


def _split_key(raw: str) -> list[str]:
    return [p.strip().strip('"') for p in re.findall(r'"[^"]*"|[^.]+', raw)]


def _value_extent(lines: list[str], first: int, col: int) -> int:
    """The last line of the value starting at lines[first][col:]."""
    for last in range(first, len(lines)):
        chunk = "\n".join([lines[first][col:], *lines[first + 1 : last + 1]])
        try:
            tomllib.loads("v = " + chunk)
            return last
        except tomllib.TOMLDecodeError:
            continue
    raise ValueError(f"line {first + 1}: cannot find the end of the value")


def _scan(lines: list[str]) -> list[_Entry]:
    entries: list[_Entry] = []
    table: Resolved = []
    counts: dict[tuple[str | int, ...], int] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        m = _HEADER.match(line)
        if m and not _KEY.match(line):
            names: Resolved = list(_split_key(m.group(2)))
            # Headers nested under an array of tables belong to its last element.
            resolved: Resolved = []
            for name in names:
                resolved.append(name)
                if tuple(resolved) in counts and (m.group(1) == "[" or len(resolved) < len(names)):
                    resolved.append(counts[tuple(resolved)])
            if m.group(1) == "[[":
                key = tuple(resolved)
                counts[key] = counts.get(key, -1) + 1
                for k in [k for k in counts if len(k) > len(key) and k[: len(key)] == key]:
                    del counts[k]
                resolved.append(counts[key])
            table = resolved
            entries.append(_Entry(list(table), i, i, header=True))
            i += 1
            continue
        km = _KEY.match(line)
        if km:
            last = _value_extent(lines, i, km.end())
            entries.append(_Entry([*table, *_split_key(km.group(2))], i, last, header=False, value_col=km.end()))
            i = last + 1
            continue
        i += 1
    return entries


# Undo steps kept per document; inputs are a few kilobytes, so each step is the whole text.
HISTORY = 200


def recovery_path(path: Path) -> Path:
    """Where a document's unsaved text is kept: hidden beside it, so `mallard check` resolves its relative paths."""
    return path.with_name(f".{path.stem}.unsaved{path.suffix}")


class InputDocument:
    """The text of an input as the user is editing it. Edits change the text in memory; `commit` closes one
    (an undo step, and the text in a recovery file beside the input while it differs from the file), `save`
    writes the file atomically. `restore_recovery` takes back the text of a session that ended unsaved."""

    def __init__(self, path: Path, text: str | None = None) -> None:
        self.path = path
        self.text = ""
        self.data: dict[str, Any] = {}
        self.saved_text = ""
        self.recovered = 0.0  # the restored recovery file's time, else 0
        self._undo: list[str] = []
        self._redo: list[str] = []
        if text is None:
            self.reload()
        else:
            self._set(text)
        self._committed = self.text

    @property
    def dirty(self) -> bool:
        return self.text != self.saved_text

    @property
    def recovery_path(self) -> Path:
        return recovery_path(self.path)

    def reload(self) -> None:
        """Take the file as it is on disk, forgetting the history."""
        self._set(self.path.read_text(encoding="utf-8"))
        self.saved_text = self._committed = self.text
        self._undo.clear()
        self._redo.clear()

    def set_text(self, text: str) -> None:
        """Replace the whole text (the side-by-side TOML editor); raises on a syntax error."""
        self._set(text)

    # The document

    @property
    def can_undo(self) -> bool:
        return bool(self._undo) or self.text != self._committed

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def commit(self) -> None:
        """Close an edit: an undo step, and the unsaved text in the recovery file."""
        if self.text == self._committed:
            return
        self._undo.append(self._committed)
        del self._undo[:-HISTORY]
        self._redo.clear()
        self._committed = self.text
        self.keep_recovery()

    def undo(self) -> bool:
        self.commit()
        if not self._undo:
            return False
        self._redo.append(self.text)
        self._set(self._undo.pop())
        self._committed = self.text
        self.keep_recovery()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.text)
        self._set(self._redo.pop())
        self._committed = self.text
        self.keep_recovery()
        return True

    def save(self, path: Path | None = None) -> None:
        """Write the text to the file (or to `path`, which the document then is) through a temporary file renamed
        over it, so a crash leaves the old file or the new one; the recovery file goes."""
        self.commit()
        target = path or self.path
        write_atomic(target, self.text)
        if target != self.path:
            self.discard_recovery()
            self.path = target
        self.saved_text = self.text
        self.discard_recovery()

    def revert(self) -> None:
        """Back to the file as saved, as one undo step."""
        self._set(self.path.read_text(encoding="utf-8"))
        self.saved_text = self.text
        self.commit()
        self.keep_recovery()

    def restore_recovery(self) -> bool:
        """Take the unsaved text a previous session left in the recovery file, as an undoable edit; a recovery
        file equal to the input goes, one that does not parse stays for the user to look at."""
        path = self.recovery_path
        try:
            text = path.read_text(encoding="utf-8")
            tomllib.loads(text)
            when = path.stat().st_mtime
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return False
        if text == self.saved_text:
            path.unlink(missing_ok=True)
            return False
        self._set(text)
        self.commit()
        self.recovered = when
        return True

    def discard_recovery(self) -> None:
        self.recovery_path.unlink(missing_ok=True)

    def keep_recovery(self) -> None:
        """The unsaved text in the recovery file, or no recovery file when there is nothing unsaved."""
        if self.dirty:
            with contextlib.suppress(OSError):  # a read-only case still edits; it is only not crash-safe
                write_atomic(self.recovery_path, self.text)
        else:
            self.discard_recovery()

    def _set(self, text: str) -> None:
        self.data = tomllib.loads(text)
        self.text = text

    # Reading

    def resolve(self, path: str | list[Segment]) -> Resolved:
        """Replace selectors by indices: `boundaries[name = "x"]` -> `boundaries[1]`.

        Keys that do not exist yet are kept, so a path to a new key resolves."""
        segments = parse_path(path) if isinstance(path, str) else path
        out: Resolved = []
        node: Any = self.data
        for seg in segments:
            if isinstance(seg, tuple):
                if not isinstance(node, list):
                    raise KeyError(f"{format_path(segments)}: {format_path(out) or 'the input'} is not an array")
                key, wanted = seg
                matches = [i for i, el in enumerate(node) if isinstance(el, dict) and str(el.get(key)) == wanted]
                if not matches:
                    raise KeyError(f"{format_path(segments)}: no element with {key} = {wanted!r}")
                seg = matches[0]
            if isinstance(seg, int):
                if not isinstance(node, list) or seg >= len(node):
                    raise KeyError(f"{format_path(segments)}: no element [{seg}]")
                node = node[seg]
            else:
                node = node.get(seg) if isinstance(node, dict) else None
            out.append(seg)
        return out

    def get(self, path: str, default: Any = None) -> Any:
        try:
            segments = self.resolve(path)
        except (KeyError, ValueError):
            return default
        return _dig(self.data, segments, default)

    def has(self, path: str) -> bool:
        sentinel = object()
        return self.get(path, sentinel) is not sentinel

    def elements(self, array: str) -> list[Element]:
        items = self.data.get(array)
        if not isinstance(items, list):
            return []
        out = []
        for i, el in enumerate(items):
            if isinstance(el, dict):
                label = next((str(el[k]) for k in ("name", "zone", "prefix") if k in el), f"{array}[{i}]")
                out.append(Element(array, i, label, str(el.get("type", ""))))
        return out

    def line_of(self, path: str) -> int | None:
        """The 1-based line of the key at `path`, else of the nearest enclosing key or table header."""
        try:
            segments = self.resolve(path)
        except (KeyError, ValueError):
            return None
        best: tuple[int, int] | None = None
        for entry in _scan(self.text.splitlines()):
            n = len(entry.path)
            if entry.path == segments[:n] and (best is None or n > best[0]):
                best = (n, entry.first + 1)
        return best[1] if best else None

    def spans_of(self, paths: list[str]) -> list[tuple[int, int]]:
        """The 1-based inclusive line ranges written for `paths`: a table's header and body (its subtables too),
        an element's, or a key's value; else the nearest enclosing key's. Another table's lines split a range."""
        entries = _scan(self.text.splitlines())
        chosen: set[int] = set()
        for path in paths:
            try:
                target = self.resolve(path)
            except (KeyError, ValueError):
                continue
            hits = {i for i, e in enumerate(entries) if e.path[: len(target)] == target}
            if not hits:
                enclosing = [(len(e.path), i) for i, e in enumerate(entries) if target[: len(e.path)] == e.path]
                hits = {max(enclosing)[1]} if enclosing else set()
            chosen |= hits
        spans: list[tuple[int, int]] = []
        extending = False
        for i, e in enumerate(entries):
            if i not in chosen:
                extending = False
            elif extending:
                spans[-1] = (spans[-1][0], e.last + 1)
            else:
                spans.append((e.first + 1, e.last + 1))
                extending = True
        return spans

    def path_at_line(self, line: int) -> Resolved | None:
        """The path of the key written across a 1-based line, else of the table the line is in."""
        found: Resolved | None = None
        for e in _scan(self.text.splitlines()):
            if e.first > line - 1:
                break
            if e.header or e.last >= line - 1:
                found = e.path
        return found

    # Editing

    def set(self, path: str, value: Any) -> None:
        """Set a value in place; the line's comment and everything else in the file are kept."""
        segments = self.resolve(path)
        lines = self.text.splitlines()
        entries = _scan(lines)
        key_entries = [e for e in entries if not e.header]
        for entry in key_entries:
            n = len(entry.path)
            if entry.path == segments[:n]:
                if n < len(segments):  # inside an inline table or array: rewrite that value
                    inner = copy.deepcopy(_dig(self.data, entry.path))
                    _assign(inner, segments[n:], value)
                    value = inner
                self._replace_value(lines, entry, value)
                return
        self._insert(lines, entries, segments, value)

    def delete(self, path: str) -> None:
        segments = self.resolve(path)
        lines = self.text.splitlines()
        for entry in _scan(lines):
            if not entry.header and entry.path == segments:
                del lines[entry.first : entry.last + 1]
                self._commit(lines)
                return
        raise KeyError(f"{format_path(segments)} is not written in the file")

    def add_table(self, path: str) -> None:
        """An empty table, as its own `[header]` at the end of the file; inside an array's entry, inline."""
        segments = self.resolve(path)
        if self.has(path):
            return
        if any(isinstance(s, int) for s in segments):
            self.set(path, {})
            return
        lines = self.text.rstrip("\n").splitlines()
        self._commit([*lines, *([""] if lines else []), f"[{format_path(segments)}]"])

    def delete_table(self, path: str) -> None:
        """A table and everything in it: its `[header]` block and the blocks of its sub-tables, or its inline
        value; the rest of the file as it was."""
        segments = self.resolve(path)
        lines = self.text.splitlines()
        entries = _scan(lines)
        drop: set[int] = set()
        for k, e in enumerate(entries):
            if e.path[: len(segments)] != segments:
                continue
            if not e.header:
                drop.update(range(e.first, e.last + 1))
                continue
            end = next((f.first for f in entries[k + 1 :] if f.header), len(lines))
            while end > e.first + 1 and not lines[end - 1].strip():
                end -= 1
            drop.update(range(e.first, end))
        if not drop:
            raise KeyError(f"{format_path(segments)} is not written in the file")
        kept = [line for i, line in enumerate(lines) if i not in drop]
        out: list[str] = []
        for line in kept:  # no run of blank lines where a block was
            if line.strip() or (out and out[-1].strip()):
                out.append(line)
        self._commit(out[:-1] if out and not out[-1].strip() else out)

    def append_element(self, array: str, values: dict[str, Any]) -> int:
        """Append an element to an array of tables (`[[boundaries]]`) after its last one."""
        lines = self.text.splitlines()
        entries = _scan(lines)
        block = [f"[[{array}]]", *(f"{k} = {format_value(v)}" for k, v in values.items())]
        owned = [e for e in entries if e.path[:1] == [array]]
        if owned:
            at = max(e.last for e in owned) + 1
            lines[at:at] = ["", *block]
        else:
            lines += ["", *block]
        self._commit(lines)
        return len(self.data[array]) - 1

    def _header_entries(self, segments: Resolved) -> list[_Entry]:
        """The headers (and keys) written for an array element's table, its subtables too."""
        return [e for e in _scan(self.text.splitlines()) if e.path[: len(segments)] == segments]

    def add_entry(self, array: str, values: dict[str, Any]) -> str:
        """Append an entry to an array of tables wherever it is: as a `[[a.b]]` table after the array's last one
        when the array is written that way (or not yet), else into the array's inline value. Returns its path."""
        segments = self.resolve(array)
        current = _dig(self.data, segments)
        written_inline = isinstance(current, list) and not any(
            e.header and e.path[: len(segments)] == segments for e in _scan(self.text.splitlines())
        )
        if all(isinstance(s, str) for s in segments) and not written_inline:
            name = ".".join(str(s) for s in segments)
            lines = self.text.splitlines()
            entries = _scan(lines)
            block = [f"[[{name}]]", *(f"{k} = {format_value(v)}" for k, v in values.items())]
            owned = [e for e in entries if e.path[: len(segments)] == segments]
            if not owned and len(segments) > 1:  # after the enclosing table's own keys and subtables
                owned = [e for e in entries if e.path[: len(segments) - 1] == segments[:-1]]
            if owned:
                at = max(e.last for e in owned) + 1
                lines[at:at] = ["", *block]
            else:
                lines += ["", *block]
            self._commit(lines)
        else:
            self.set(array, [*(current or []), values])
        return f"{format_path(segments)}[{len(_dig(self.data, segments)) - 1}]"

    def delete_entry(self, path: str) -> None:
        """Remove an array element (`sponges[1]`, its subtables too) or a named entry (`regions.box`)."""
        segments = self.resolve(path)
        lines = self.text.splitlines()
        owned = [e for e in _scan(lines) if e.path[: len(segments)] == segments]
        if owned and owned[0].header:
            first, last = owned[0].first, max(e.last for e in owned)
            if first > 0 and not lines[first - 1].strip():
                first -= 1
            del lines[first : last + 1]
            self._commit(lines)
        elif isinstance(segments[-1], int):
            parent = format_path(segments[:-1])
            items = list(_dig(self.data, segments[:-1]))
            del items[segments[-1]]
            self.set(parent, items)
        else:
            self.delete(path)

    def duplicate_entry(self, path: str) -> str:
        """A copy of an array element right after it (its subtables too), or of a named entry under a new name;
        a `name` or `prefix` it has gets `_copy`, so the copy is not taken for the same entry. Returns its path."""
        segments = self.resolve(path)
        value = copy.deepcopy(_dig(self.data, segments))
        if not isinstance(segments[-1], int):
            names = _dig(self.data, segments[:-1]) or {}
            new = f"{segments[-1]}_copy"
            while new in names:
                new += "_copy"
            self.set(format_path([*segments[:-1], new]), value)
            return format_path([*segments[:-1], new])
        lines = self.text.splitlines()
        owned = [e for e in _scan(lines) if e.path[: len(segments)] == segments]
        index = segments[-1] + 1
        copy_path = format_path([*segments[:-1], index])
        if owned and owned[0].header:
            first, last = owned[0].first, max(e.last for e in owned)
            lines[last + 1 : last + 1] = ["", *lines[first : last + 1]]
            self._commit(lines)
        else:
            items = list(_dig(self.data, segments[:-1]))
            items.insert(index, value)
            self.set(format_path(segments[:-1]), items)
        for key in ("name", "prefix"):
            if isinstance(value, dict) and isinstance(value.get(key), str):
                self.set(f"{copy_path}.{key}", value[key] + "_copy")
        return copy_path

    def _replace_value(self, lines: list[str], entry: _Entry, value: Any) -> None:
        chunk = "\n".join([lines[entry.first][entry.value_col :], *lines[entry.first + 1 : entry.last + 1]])
        item = tomlkit.parse("v = " + chunk).item("v")
        raw = item.as_string() if isinstance(item, tomlkit.items.Item) else chunk
        if not chunk.startswith(raw):
            raw = chunk.split("#", 1)[0].rstrip()
        rest = chunk[len(raw) :]
        new = lines[entry.first][: entry.value_col] + format_value(value) + rest
        lines[entry.first : entry.last + 1] = new.split("\n")
        self._commit(lines)

    def _insert(self, lines: list[str], entries: list[_Entry], segments: Resolved, value: Any) -> None:
        # The deepest table written as a header (or an array element) that holds the key.
        headers = [e for e in entries if e.header and e.path == segments[: len(e.path)] and len(e.path) < len(segments)]
        if headers:
            depth = max(len(e.path) for e in headers)
            table = segments[:depth]
            keys = segments[depth:]
            if any(isinstance(k, int) for k in keys):
                raise KeyError(f"{format_path(segments)}: no such array element")
            # After the last key of the table's last section, before the next header.
            start = [e for e in entries if e.header and e.path == table][-1].first
            next_header = next((e.first for e in entries if e.header and e.first > start), len(lines))
            owned = [e.last for e in entries if not e.header and start < e.first < next_header]
            at = (max(owned) if owned else start) + 1
            lines[at:at] = [f"{'.'.join(str(k) for k in keys)} = {format_value(value)}"]
        else:
            if any(isinstance(k, int) for k in segments):
                raise KeyError(f"{format_path(segments)}: no such array element")
            if len(segments) == 1:
                first_header = next((e.first for e in entries if e.header), len(lines))
                lines[first_header:first_header] = [f"{segments[0]} = {format_value(value)}", ""]
            else:
                lines += ["", f"[{segments[0]}]", f"{'.'.join(str(k) for k in segments[1:])} = {format_value(value)}"]
        self._commit(lines)

    def _commit(self, lines: list[str]) -> None:
        text = "\n".join(lines) + ("\n" if self.text.endswith("\n") or not self.text else "")
        self._set(text)


def _dig(node: Any, segments: Resolved, default: Any = None) -> Any:
    for seg in segments:
        try:
            node = node[seg]
        except (KeyError, IndexError, TypeError):
            return default
    return node


def _assign(node: Any, segments: Resolved, value: Any) -> None:
    for seg in segments[:-1]:
        if isinstance(node, dict) and seg not in node:
            node[seg] = {}
        node = node[seg]
    node[segments[-1]] = value
