from __future__ import annotations

from mallard_gui.palette import Item, items, score, search


def test_prefixes_beat_word_starts_beat_substrings_beat_subsequences() -> None:
    p, w, s, q = (score("cfl", t) for t in ("cfl", "run.cfl", "runcfl", "c_f_l"))
    assert p is not None and w is not None and s is not None and q is not None
    assert p > w > s > q
    assert score("xyz", "run.cfl") is None
    assert score("add rect", "Add region: rectangle") is not None
    assert score("add rect", "Add probe: cut_plane") is None


def test_keys_nodes_and_actions_are_found_together() -> None:
    tree = [{"id": "zone:farfield", "label": "farfield", "value": "farfield"}]
    keys = [("run.t_stop", True, "number"), ("run.t_wall_stop", False, "number")]
    acts = [("check", "Check the case", "mallard check"), ("theme", "Toggle theme", "")]
    found = search("t_stop", items(tree, keys, acts))
    assert [i.id for i in found][:1] == ["key:run.t_stop"]
    assert search("far", items(tree, keys, acts))[0].id == "node:zone:farfield"
    assert search("chk", items(tree, keys, acts))[0].id == "action:check"


def test_set_keys_come_before_unset_ones_on_a_tie() -> None:
    a, b = Item("key:a.x", "a.x", kind="key", rank=1), Item("key:b.x", "b.x", kind="key", rank=0)
    assert [i.id for i in search("x", [a, b])] == ["key:b.x", "key:a.x"]
