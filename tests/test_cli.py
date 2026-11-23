from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def test_url_file_holds_the_url_once_the_server_answers(tmp_path: Path, cylinder_project: Path) -> None:
    mallard = tmp_path / "mallard"
    mallard.write_text("#!/bin/sh\n[ \"$1\" = --version ] && echo 'Mallard v0.11.0' || exit 1\n")
    mallard.chmod(0o755)
    project = cylinder_project
    url_file = tmp_path / "url"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, mallard_gui.cli as c; sys.exit(c.main())",
            str(project),
            "--mallard",
            str(mallard),
            "--no-browser",
            "--url-file",
            str(url_file),
        ],
        env={**os.environ, "MALLARD_GUI_CONFIG_DIR": str(tmp_path / "config")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        end = time.monotonic() + 60
        while not url_file.exists() and time.monotonic() < end and proc.poll() is None:
            time.sleep(0.1)
        url = url_file.read_text(encoding="utf-8")
        assert stat.S_IMODE(url_file.stat().st_mode) == 0o600
        assert url.startswith("http://127.0.0.1:") and "&secret=" in url
        with urllib.request.urlopen(url, timeout=10) as reply:
            assert reply.status == 200
    finally:
        proc.terminate()
        proc.wait(timeout=10)
