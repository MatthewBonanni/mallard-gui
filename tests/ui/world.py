"""The browser tests' world: a project with a finished, a stopped and a mid-run run, a fake cluster and Kubernetes
context, and the app served on it (`tests.ui.serve`) without a mallard build."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from mallard_gui.config import Install, Remote, RemoteStore
from mallard_gui.project import Project, RunRecord

from ..conftest import FIXTURES
from ..runs import make_run
from ..test_installs import make_archive, write_manifest
from ..test_remotes import fake_cluster

KUBE_JOB = {
    "metadata": {"name": "mallard-gui-cyl-1-ab12", "annotations": {"mallard-gui/run-name": "cyl-1"}},
    "status": {"active": 1, "ready": 1, "startTime": "2026-11-20T17:00:00Z"},
}


def eventually[T](get: Callable[[], T], ok: Callable[[T], bool] = bool, timeout: float = 10.0) -> T:
    """`get()` once `ok` holds of it, polled: for what the server writes or draws after the page's last wait."""
    end = time.monotonic() + timeout
    while not ok(value := get()):
        if time.monotonic() > end:
            raise AssertionError(f"still {value!r} after {timeout} s")
        time.sleep(0.05)
    return value


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_world(root: Path, runs: bool = True) -> tuple[Path, dict[str, str]]:
    """The project and the environment its server runs in."""
    project = root / "case"
    project.mkdir()
    shutil.copy(FIXTURES / "cylinder_input.toml", project / "input.toml")
    shutil.copy(FIXTURES / "cylinder.msh", project / "cylinder.msh")
    if runs:
        run = project / "runs" / "input-1"
        run.mkdir(parents=True)
        shutil.copy(FIXTURES / "cylinder_run.jsonl", run / "mallard.jsonl")
        Project.open(project).save_run(
            RunRecord("input-1", home={"remote": "local", "directory": "runs/input-1"}, outcome="finished")
        )
        make_run(project / "runs" / "box-1", steps=3)
        Project.open(project).save_run(
            RunRecord("box-1", home={"remote": "local", "directory": "runs/box-1"}, outcome="finished")
        )
    fake_cluster(root)
    release = root / "release"
    write_manifest(release, "0.11.0", [make_archive(release, "0.11.0")])
    env = {
        **os.environ,
        "PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "MALLARD_GUI_CONFIG_DIR": str(root / "config"),
        "MALLARD_GUI_CACHE_DIR": str(root / "cache"),
        "MALLARD_GUI_SSH": str(FIXTURES / "fake-ssh"),
        "FAKE_SSH_HOMES": str(root / "homes"),
        "MALLARD_GUI_RELEASES": release.as_uri(),
        "MALLARD_GUI_KUBECTL": str(FIXTURES / "fake-kubectl"),
        "FAKE_KUBE_STATE": str(root / "kube"),
    }
    (root / "kube").mkdir()
    (root / "kube" / "jobs.json").write_text(json.dumps({"items": [KUBE_JOB]}))
    store = RemoteStore(root / "config" / "remotes.toml")
    store.add(Remote("hpc", "hpc", installs=[Install("/opt/mallard/bin/mallard")], storage={"run_root": "/scratch"}))
    store.add(
        Remote(
            "lab",
            "lab",
            backend="k8s",
            installs=[Install("ghcr.io/matthewbonanni/mallard:0.10.0-cpu-amd64")],
            storage={"run_root": "/data/runs"},
            defaults={"cpu": "2", "memory": "4Gi"},
            k8s={"namespace": "me", "pvc": "mallard-data", "node_selector": "nvidia.com/gpu.product=A100"},
        )
    )
    store.save()
    return project, env


@contextmanager
def serve(project: Path, env: dict[str, str], check: str = "") -> Iterator[str]:
    """The app on `project` at the URL yielded; `check` names another captured check report to answer with."""
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.ui.serve", str(project), str(port)],
        cwd=Path(__file__).parent.parent.parent,
        env={**env, "MALLARD_GUI_TEST_CHECK": check} if check else env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        end = time.monotonic() + 60
        while time.monotonic() < end:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# The tests never post a real notification: the Web Notifications API records them instead.
FAKE_NOTIFICATION = """
window.__notes = []; window.__asked = 0;
window.Notification = class {
  static permission = 'default';
  static requestPermission() { window.__asked++; this.permission = 'granted'; return Promise.resolve('granted'); }
  constructor(title, options) { window.__notes.push({title, ...options}); }
};
"""
