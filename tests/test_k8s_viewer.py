from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from mallard_gui.backends.k8s import HelperPod
from mallard_gui.viewer import k8s as k8s_viewer
from mallard_gui.viewer.engine import ViewerError
from mallard_gui.viewer.k8s import K8sViewer, SocketViewer, package_archive, platform_tag, viewer_pod

from .runs import make_run
from .test_k8s import applied, k8s_remote, kube  # noqa: F401  (fixture)
from .test_ui import free_port

JPEG = b"\xff\xd8"


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_run(tmp_path_factory.mktemp("k8sviewer") / "run")


@pytest.fixture
def listener(run: Path) -> Iterator[tuple[int, subprocess.Popen[bytes]]]:
    """`python -m mallard_gui.viewer --listen PORT`, as the viewer pod runs it."""
    port = free_port()
    argv = [sys.executable, "-m", "mallard_gui.viewer", "--listen", str(port), "--root", str(run.parent)]
    proc = subprocess.Popen([*argv, "--idle", "60"], stderr=subprocess.PIPE)
    yield port, proc
    if proc.poll() is None:
        proc.terminate()
    proc.wait(timeout=10)


def test_listen_serves_one_client_after_another_on_loopback(
    run: Path, listener: tuple[int, subprocess.Popen[bytes]]
) -> None:
    port, proc = listener
    for _ in range(2):
        v = SocketViewer("test")
        v.connect(port, timeout=60)
        assert v.info["hello"] == "mallard-viewer"
        assert v.call("open", run=run.name)["dim"] == 3
        v.call("show", series="solut/box", index=0)
        assert v.call("frame", width=64, height=48)["image"][:2] == JPEG
        v.close()
    assert proc.poll() is None


def test_listen_exits_when_idle(run: Path) -> None:
    port = free_port()
    argv = [sys.executable, "-m", "mallard_gui.viewer", "--listen", str(port), "--root", str(run.parent), "--idle", "1"]
    proc = subprocess.run(argv, capture_output=True, timeout=60, check=False)
    assert proc.returncode == 0 and b"exiting (idle)" in proc.stderr


def test_viewer_pod_manifest(tmp_path: Path) -> None:
    remote = k8s_remote(tmp_path)
    remote.viewer = {"python": "python3", "cpus": "1", "memory": "2Gi", "time": "00:30:00", "idle": "300"}
    m = viewer_pod(remote, "gui-test-viewer-kube", {"PYTHONPATH": "/p"})
    c = m["spec"]["containers"][0]
    assert c["command"] == ["python3", "-m", "mallard_gui.viewer", "--listen", "8765", "--root", "/", "--idle", "300"]
    assert c["image"] == remote.installs[0].path and c["env"] == [{"name": "PYTHONPATH", "value": "/p"}]
    assert m["spec"]["activeDeadlineSeconds"] == 1800 and "ports" not in c  # no Service, no listener outside the pod
    assert m["metadata"]["labels"]["mallard-gui/role"] == "viewer"


def test_platform_and_package_archive() -> None:
    assert platform_tag("x86_64\n") == "x86_64-manylinux_2_28" and platform_tag("arm64") == "aarch64-manylinux_2_28"
    with pytest.raises(ViewerError):
        platform_tag("riscv64")
    digest, data = package_archive()
    assert digest == package_archive()[0]
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = tar.getnames()
    assert "mallard_gui/viewer/protocol.py" in names and not any("__pycache__" in n for n in names)


def test_environment_is_placed_on_the_volume_once(tmp_path: Path, kube: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    remote = k8s_remote(tmp_path)
    remote.viewer = {"python": sys.executable, "opengl": "osmesa"}
    vtk = tmp_path / "vtk.tgz"
    with tarfile.open(vtk, "w:gz") as tar:
        info = tarfile.TarInfo("vtk.py")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"x=1\n"))
    calls: list[tuple[str, str, str]] = []

    def fake_archive(python: str, platform: str, version: str, uv: str | None = None) -> Path:
        calls.append((python, platform, version))
        return vtk

    monkeypatch.setattr(k8s_viewer, "vtk_archive", fake_archive)
    helper = HelperPod(remote, sleep=lambda s: None)
    env = k8s_viewer.prepare_environment(remote, helper)
    gui_dir, vtk_dir = env["PYTHONPATH"].split(":")
    assert Path(vtk_dir, "vtk.py").read_text() == "x=1\n" and Path(gui_dir, "mallard_gui", "__init__.py").is_file()
    assert vtk_dir.startswith(str(tmp_path / "vol" / ".mallard-gui" / "viewer"))
    assert env["VTK_DEFAULT_OPENGL_WINDOW"] == "vtkOSOpenGLRenderWindow" and env["HOME"] == "/tmp"
    log = (tmp_path / "kube.log").read_text()
    assert k8s_viewer.prepare_environment(remote, helper) == env
    assert (tmp_path / "kube.log").read_text().count("tar -xzf") == log.count("tar -xzf")  # not placed again


def test_k8s_viewer_starts_a_pod_forwards_and_deletes_it(
    tmp_path: Path,
    kube: Path,  # noqa: F811
    run: Path,
    listener: tuple[int, subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, _ = listener
    monkeypatch.setenv("FAKE_KUBE_FORWARD", str(port))
    remote = k8s_remote(tmp_path)
    helper = HelperPod(remote, sleep=lambda s: None)
    (kube / "gone-gui-test-viewer-kube").parent.mkdir(exist_ok=True)
    (kube / "gone-gui-test-viewer-kube").touch()
    t0 = time.monotonic()
    v = K8sViewer(remote, helper, start_timeout=60, prepare=lambda r, h: {"HOME": "/tmp"})
    try:
        assert time.monotonic() - t0 < 60
        assert applied(kube)[-1]["metadata"]["name"] == "gui-test-viewer-kube"
        assert v.alive and v.call("open", run=run.name)["dim"] == 3
        assert "--address 127.0.0.1" in v.command
    finally:
        v.close()
    assert (kube / "gone-gui-test-viewer-kube").exists() and not v.alive
    assert json.loads(json.dumps(v.info))["hello"] == "mallard-viewer"
