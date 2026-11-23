"""The command palette (Cmd/Ctrl K): one search over the case's nodes, every input key Mallard knows and the
actions of the app, ranked so that typing a few letters of any of them finds it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass
class Item:
    id: str  # node:<tree id> | key:<path> | action:<name>[:<value>]
    label: str
    detail: str = ""
    kind: str = ""  # node | key | action
    rank: int = 0  # ties: lower first (keys the input sets before the others)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "detail": self.detail, "kind": self.kind}


def score(query: str, text: str) -> float | None:
    """How well `query` matches `text`: a prefix best, then a word start, a substring, then letters in order
    (closer together is better); several words must each be in the text. None when they do not match."""
    q, t = query.lower().strip(), text.lower()
    if not q:
        return 0.0
    words = q.split()
    if len(words) > 1:  # every word somewhere in the text, as a substring
        each = [score(w, t) for w in words]
        if any(x is None or x < 50 for x in each):
            return None
        return min(x for x in each if x is not None) - 1
    i = t.find(q)
    if i == 0:
        return 100.0 - len(t) / 100
    if i > 0:
        word_start = not t[i - 1].isalnum()
        return (80.0 if word_start else 60.0) - i / 10 - len(t) / 100
    pos, gaps, last = 0, 0, -1
    for c in q:
        found = t.find(c, pos)
        if found < 0:
            return None
        if last >= 0:
            gaps += found - last - 1
        last, pos = found, found + 1
    return 40.0 - gaps - len(t) / 100


def search(query: str, items: Iterable[Item], limit: int = 30) -> list[Item]:
    scored = []
    for item in items:
        s = score(query, item.label)
        d = score(query, item.detail) if item.detail else None
        best = max((x for x in (s, None if d is None else d - 30) if x is not None), default=None)
        if best is not None:
            scored.append((-best, item.rank, item.label, item))
    scored.sort(key=lambda x: x[:3])
    return [x[3] for x in scored[:limit]]


def items(
    tree: Sequence[Mapping[str, Any]],
    keys: Iterable[tuple[str, bool, str]],
    actions: Iterable[tuple[str, str, str]],
) -> list[Item]:
    """Palette entries: tree nodes (id, label, value), keys (path, set, type) and actions (id, label, detail)."""
    out = [Item(f"action:{a}", label, detail, "action") for a, label, detail in actions]
    out += [Item(f"node:{n['id']}", str(n["label"]), str(n.get("value") or ""), "node") for n in tree if n.get("label")]
    out += [
        Item(f"key:{path}", path, "set" if is_set else type_, "key", 0 if is_set else 1) for path, is_set, type_ in keys
    ]
    return out
