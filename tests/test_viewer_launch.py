"""Reaching a viewer where forwarding or interactive jobs are blocked: the launch order (a job of its own, a
step in a held allocation, the allocation's node through the login node), the login node only when chosen,
batch images as the last resort, and the GPU path's resources and render window."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from mallard_gui.backends.ssh import SshConnection
from mallard_gui.config import Install, Remote
from mallard_gui.viewer.batch import BatchImages
from mallard_gui.viewer.client import (
    ViewerUnavailable,
    allocation_command,
    start_remote_viewer,
    viewer_command,
)
from mallard_gui.viewer.engine import ViewerError

from .runs import make_run
from .test_remotes import FAKE_SSH, fake_cluster, stub

PY = sys.executable


def remote(**viewer: str) -> Remote:
    return Remote("hpc", "hpc", installs=[Install("/opt/m/bin/mallard")], viewer={"python": PY, **viewer})


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_cluster(tmp_path)
    bin_dir = tmp_path / "bin"
    # sbatch: an allocation (`--wrap "sleep ..."`) only holds its place; a batch job runs at once, then is done.
    # squeue reports the allocation running on node71-node72 until it is cancelled or done.
    stub(
        bin_dir / "sbatch",
        "for a; do last=$a; done\n"
        'case "$last" in sleep*) ;; *) sh -c "$last" >/dev/null 2>&1; touch "$HOME/done" ;; esac\necho 77',
    )
    stub(bin_dir / "squeue", 'case "$*" in *-j\\ 77*) [ -f "$HOME/done" ] || echo "RUNNING|node7[1-2]" ;; esac')
    stub(bin_dir / "scancel", 'echo "$@" >> "$HOME/scancel.log"; touch "$HOME/done"')
    stub(
        bin_dir / "ssh",
        'while [ $# -gt 1 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done\n'
        'echo "$1" >> "$HOME/ssh-node.log"; shift; exec sh -c "$*"',
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_HOMES", str(tmp_path / "homes"))
    return tmp_path


def conn(cluster: Path) -> SshConnection:
    return SshConnection("hpc", control_dir=cluster / "ctl", ssh=FAKE_SSH)


def refuse_srun_steps(cluster: Path, also_overlap: bool = False) -> None:
    """A site that refuses interactive jobs (and, with also_overlap, steps of batch ones too)."""
    body = 'case "$*" in *--overlap*) %s ;; esac\necho "srun: error: interactive jobs are disabled" >&2; exit 1' % (
        'echo "srun: error: steps are disabled" >&2; exit 1'
        if also_overlap
        else 'while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac; done; exec "$@"'
    )
    stub(cluster / "bin" / "srun", body)


def test_launch_commands_and_the_gpu_path() -> None:
    r = remote(partition="viz", gpu="true")
    argv = viewer_command(r, "/scratch", "srun")
    assert "--gpus=1" in argv and "--partition=viz" in argv
    assert "VTK_DEFAULT_OPENGL_WINDOW=vtkEGLRenderWindow" in argv  # EGL on the GPU node
    assert viewer_command(r, "/", "allocation", jobid="77")[:4] == ["srun", "--jobid=77", "--overlap", "--ntasks=1"]
    node = viewer_command(r, "/", "node", node="node7")
    assert node[:5] == ["ssh", "-o", "BatchMode=yes", "-T", "node7"] and "mallard_gui.viewer" in node[5]
    assert viewer_command(r, "/", "login")[0] == "env"
    cpu = remote(render="osmesa")
    assert "VTK_DEFAULT_OPENGL_WINDOW=vtkOSOpenGLRenderWindow" in viewer_command(cpu, "/")
    assert "--gpus=1" not in viewer_command(cpu, "/")
    assert allocation_command(r)[:2] == ["sbatch", "--parsable"] and "--gpus=1" in allocation_command(r)
    with pytest.raises(ViewerError, match="render"):
        viewer_command(remote(render="opengl"), "/")
    with pytest.raises(ViewerError, match="launch"):
        viewer_command(remote(launch="tunnel"), "/")


def test_a_job_of_its_own_comes_first(cluster: Path, tmp_path: Path) -> None:
    run = make_run(tmp_path / "runs" / "box", steps=1)
    v = start_remote_viewer(conn(cluster), remote(), str(run.parent))
    try:
        assert v.launch == "srun" and not v.jobid
        assert v.call("open", run="box")["dim"] == 3
    finally:
        v.close()


def test_refused_interactive_jobs_fall_back_to_a_step_in_an_allocation(cluster: Path, tmp_path: Path) -> None:
    refuse_srun_steps(cluster)
    run = make_run(tmp_path / "runs" / "box", steps=1)
    v = start_remote_viewer(conn(cluster), remote(), str(run.parent))
    home = cluster / "homes" / "hpc"
    try:
        assert (v.launch, v.jobid) == ("allocation", "77")
        assert v.call("open", run="box")["dim"] == 3
    finally:
        v.close()
    assert (home / "scancel.log").read_text().split() == ["77"]  # the allocation ends with the viewer


def test_refused_steps_fall_back_to_the_node_through_the_login_node(cluster: Path, tmp_path: Path) -> None:
    refuse_srun_steps(cluster, also_overlap=True)
    run = make_run(tmp_path / "runs" / "box", steps=1)
    v = start_remote_viewer(conn(cluster), remote(), str(run.parent))
    try:
        assert v.launch == "node"
        assert (cluster / "homes" / "hpc" / "ssh-node.log").read_text().split() == ["node71"]
        assert v.call("open", run="box")["dim"] == 3
    finally:
        v.close()


def test_when_nothing_works_every_attempt_is_reported_and_the_login_node_is_a_choice(
    cluster: Path, tmp_path: Path
) -> None:
    refuse_srun_steps(cluster, also_overlap=True)
    stub(cluster / "bin" / "ssh", 'echo "ssh: connect to host node7 port 22: Connection refused" >&2; exit 255')
    run = make_run(tmp_path / "runs" / "box", steps=1)
    with pytest.raises(ViewerUnavailable) as e:
        start_remote_viewer(conn(cluster), remote(), str(run.parent), start_timeout=20)
    assert [a for a, _ in e.value.attempts] == ["srun", "allocation", "node"]
    assert "interactive jobs are disabled" in e.value.attempts[0][1]
    assert (cluster / "homes" / "hpc" / "scancel.log").read_text().split() == ["77"]
    v = start_remote_viewer(conn(cluster), remote(), str(run.parent), login=True)
    try:
        assert v.launch == "login" and v.call("open", run="box")["dim"] == 3
    finally:
        v.close()


def test_batch_images_render_next_to_the_data_and_come_back_in_memory(cluster: Path, tmp_path: Path) -> None:
    run = make_run(tmp_path / "runs" / "box", steps=2)
    images = BatchImages(conn(cluster), remote(), poll=0.2)
    png = images.render(
        str(run), {"series": "solut/box", "index": 1, "field": "U", "preset": "+X", "width": 320, "height": 200}
    )
    assert png.startswith(b"\x89PNG")
    assert list((run / "extract" / "images").glob("*.png"))
