"""Another program's edits to the open input: seen by content, reloaded as an undo step, or kept over."""

from __future__ import annotations

from pathlib import Path

from mallard_gui.session import Session
from mallard_gui.ui.file_watch import diff_rows

from .conftest import FakeMallard


def test_the_session_tells_another_programs_edit_from_its_own(cylinder_project: Path) -> None:
    s = Session(cylinder_project, FakeMallard())  # type: ignore[arg-type]
    path = cylinder_project / "input.toml"
    s.set_value("run.t_stop", "5")
    s.save()
    assert s.disk_change() is None  # its own save
    theirs = path.read_text().replace("cfl = 1.0", "cfl = 0.5")
    path.write_text(theirs)
    assert s.disk_change() == theirs
    s.reload_from_disk()
    assert s.doc.get("run.cfl") == 0.5 and not s.doc.dirty and s.disk_change() is None
    assert s.doc.undo() and s.doc.get("run.cfl") == 1.0  # Undo brings back what was here
    s.reload_from_disk()
    s.set_value("run.t_stop", "7")
    path.write_text(theirs.replace("cfl = 0.5", "cfl = 0.25"))
    s.keep_mine(s.disk_change() or "")
    assert s.doc.dirty and s.disk_change() is None and s.doc.get("run.t_stop") == 7
    s.save()
    assert "t_stop = 7" in path.read_text() and "cfl = 0.5" in path.read_text()
    path.unlink()
    assert s.disk_change() == ""
    s.close()


def test_the_comparison_shows_both_sides_changes() -> None:
    rows = diff_rows("a = 1\nb = 2\nc = 3\n", "a = 1\nb = 9\nc = 3\n")
    kinds = [(r["kind"], r["text"]) for r in rows]
    assert ("del", "-b = 2") in kinds and ("add", "+b = 9") in kinds and kinds[0][0] == "hunk"
