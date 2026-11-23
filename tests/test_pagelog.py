from __future__ import annotations

import json
from pathlib import Path

from mallard_gui import pagelog


def test_a_full_log_is_kept_as_the_previous_one(tmp_path: Path) -> None:
    """page.log never grows past its limit: the next event starts a new one, the old kept beside it."""
    log = pagelog.PageLog(tmp_path / "page.log")
    log.path.write_text("x" * (pagelog.LIMIT + 1))
    log.write("error", {"message": "after the limit"})
    assert (tmp_path / "page.log.1").stat().st_size == pagelog.LIMIT + 1
    (line,) = log.path.read_text().splitlines()
    assert json.loads(line)["kind"] == "error" and "after the limit" in json.loads(line)["detail"]
