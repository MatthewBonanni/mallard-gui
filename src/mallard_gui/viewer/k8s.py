"""The viewer on Kubernetes: a viewer pod with the remote's volume mounted, listening on the pod's loopback
interface only, reached through `kubectl port-forward` bound to this machine's 127.0.0.1. Frames arrive in
memory; nothing is written to local disk.

The pod runs the viewer's image (`viewer.image`), by default the remote's Mallard image, which carries a
Python. Unless `viewer.bootstrap` is `no` (an image with mallard-gui and VTK installed), the viewer's Python
packages are placed on the volume once, under `<mount>/.mallard-gui/viewer/`, by the helper pod: VTK for the
pod's platform and Python (resolved here by uv, never fetched by the cluster) and this GUI's own package.
`viewer.libs` names a local `.tgz` of shared libraries for images without an offscreen OpenGL (OSMesa or EGL),
placed beside them and put on `LD_LIBRARY_PATH`; `viewer.opengl` (`osmesa`, `egl`) picks VTK's window.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mallard_gui

from ..backends.k8s import HelperPod, KubeConnection, dns_name, image_of, k8s_settings, labels, pod_spec, seconds_of
from ..backends.ssh import RemoteError
from ..config import Remote, cache_dir
from .client import CALL_TIMEOUT, START_TIMEOUT, Viewer, viewer_settings
from .engine import ViewerError
from .protocol import NAME, VERSION

PORT = 8765
_FORWARDING = re.compile(rb"Forwarding from 127\.0\.0\.1:(\d+)")
_WINDOWS = {"osmesa": "vtkOSOpenGLRenderWindow", "egl": "vtkEGLRenderWindow"}


class SocketViewer(Viewer):
    """The protocol over a TCP connection to this machine's loopback (the local end of a port-forward)."""

    def __init__(self, where: str, call_timeout: float = CALL_TIMEOUT) -> None:
        self.where = where
        self.call_timeout = call_timeout
        self.sock: socket.socket | None = None
        self._buffer = b""
        self._next = 1
        self._lock = threading.Lock()
        self.info: dict[str, Any] = {}

    def connect(self, port: int, timeout: float, on_wait: Callable[[float], None] | None = None) -> None:
        """Connect and read the viewer's hello, retrying while it starts (the port-forward accepts at once
        and drops the connection until the viewer listens)."""
        start = time.monotonic()
        last = ""
        while True:
            waited = time.monotonic() - start
            if waited > timeout:
                raise ViewerError(f"viewer on {self.where}: no answer in {timeout:.0f} s ({last})")
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
                self._buffer = b""
                line = self._readline(10.0)
                break
            except (OSError, ViewerError) as e:
                last = str(e)
                self._close_socket()
                if on_wait:
                    on_wait(waited)
                time.sleep(1.0)
        info: dict[str, Any] = json.loads(line)
        if info.get("hello") != NAME or int(info.get("version", 0)) != VERSION:
            raise ViewerError(f"viewer on {self.where}: protocol {info.get('hello')} {info.get('version')}")
        self.info = info

    def _readline(self, timeout: float) -> bytes:
        assert self.sock is not None
        self.sock.settimeout(timeout)
        while b"\n" not in self._buffer:
            try:
                chunk = self.sock.recv(1 << 20)
            except TimeoutError as e:
                raise ViewerError(f"viewer on {self.where}: no answer in {timeout:.0f} s") from e
            if not chunk:
                raise ViewerError(f"viewer on {self.where}: connection closed")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    @property
    def alive(self) -> bool:
        return self.sock is not None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.sock is None:
                raise ViewerError(f"viewer on {self.where} is closed")
            rid = self._next
            self._next += 1
            try:
                self.sock.sendall(json.dumps({"id": rid, "method": method, "params": params}).encode() + b"\n")
                while True:
                    response: dict[str, Any] = json.loads(self._readline(self.call_timeout))
                    if response.get("id") == rid:
                        return response
            except (OSError, ViewerError) as e:
                self._close_socket()
                raise ViewerError(f"viewer on {self.where}: {e}") from e

    def _close_socket(self) -> None:
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
        self.sock = None

    def close(self) -> None:
        self._close_socket()


def platform_tag(machine: str) -> str:
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(machine.strip())
    if arch is None:
        raise ViewerError(f"viewer pod: no VTK wheels for {machine.strip()!r}")
    return f"{arch}-manylinux_2_28"


def vtk_version() -> str:
    from vtkmodules.vtkCommonCore import vtkVersion

    return str(vtkVersion.GetVTKVersion())


def package_archive() -> tuple[str, bytes]:
    """This GUI's own package as a .tgz, named by the SHA-256 of its files, so a changed GUI is placed anew."""
    root = Path(mallard_gui.__file__).parent
    files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
    digest = hashlib.sha256()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in files:
            rel = p.relative_to(root.parent).as_posix()
            data = p.read_bytes()
            digest.update(rel.encode() + b"\0" + data)
            info = tarfile.TarInfo(rel)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return digest.hexdigest()[:12], buf.getvalue()


def vtk_archive(python: str, platform: str, version: str, uv: str | None = None) -> Path:
    """VTK for the pod's Python and platform, resolved by uv on this machine and kept in the cache."""
    out = cache_dir() / "viewer-env" / f"vtk-{version}-py{python}-{platform}.tgz"
    if out.is_file():
        return out
    uv = uv or os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise ViewerError("the viewer pod's packages are resolved with uv, which is not on PATH")
    target = out.with_suffix(".d")
    cmd = [uv, "pip", "install", "--quiet", "--no-deps", "--target", str(target)]
    cmd += ["--python-platform", platform, "--python-version", python, f"vtk=={version}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=900)
    if proc.returncode != 0:
        raise ViewerError(f"uv pip install vtk=={version} for {platform}: {proc.stderr.strip()[-300:]}")
    partial = out.with_suffix(".partial")
    with tarfile.open(partial, "w:gz") as tar:
        for p in sorted(target.iterdir()):
            tar.add(p, arcname=p.name)
    partial.replace(out)
    shutil.rmtree(target, ignore_errors=True)
    return out


def _upload(helper: HelperPod, dest: str, data: Callable[[], bytes] | Path) -> None:
    """Extract a .tgz into `dest` on the volume through the helper, unless it is there already."""
    if helper.run(["test", "-f", f"{dest}/.ready"], check=False).returncode == 0:
        return
    script = 'mkdir -p "$1" && tar -xzf - -C "$1" && touch "$1/.ready"'
    proc = helper.popen(["sh", "-c", script, "sh", dest], stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        if isinstance(data, Path):
            with data.open("rb") as f:
                shutil.copyfileobj(f, proc.stdin, 1 << 20)
        else:
            proc.stdin.write(data())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode("utf-8", "replace").strip() if proc.stderr else ""
    if proc.wait() != 0:
        raise ViewerError(f"placing {dest}: {err or f'exit {proc.returncode}'}")


def prepare_environment(remote: Remote, helper: HelperPod) -> dict[str, str]:
    """Place the viewer's packages on the volume (once each) and return the pod's environment variables."""
    s = k8s_viewer_settings(remote)
    env: dict[str, str] = {"HOME": "/tmp", "PYTHONUNBUFFERED": "1"}
    if s.get("opengl"):
        if s["opengl"] not in _WINDOWS:
            raise ViewerError(f"remote {remote.name}: viewer.opengl {s['opengl']!r}: one of {', '.join(_WINDOWS)}")
        env["VTK_DEFAULT_OPENGL_WINDOW"] = _WINDOWS[s["opengl"]]
    if s.get("bootstrap", "yes") == "no":
        return env
    base = f"{k8s_settings(remote)['mount'].rstrip('/')}/.mallard-gui/viewer"
    probe = helper.run(
        ["sh", "-c", 'uname -m; "$1" -c "import sys; print(\'%d.%d\' % sys.version_info[:2])"', "sh", s["python"]]
    ).text.split()
    if len(probe) != 2:
        raise ViewerError(f"viewer pod: no Python {s['python']} in the image ({' '.join(probe)})")
    platform, python = platform_tag(probe[0]), probe[1]
    version = vtk_version()
    vtk_dir = f"{base}/vtk-{version}-py{python}-{platform}"
    _upload(helper, vtk_dir, vtk_archive(python, platform, version))
    digest, archive = package_archive()
    gui_dir = f"{base}/gui-{digest}"
    _upload(helper, gui_dir, lambda: archive)
    env["PYTHONPATH"] = f"{gui_dir}:{vtk_dir}"
    if s.get("libs"):
        libs = Path(s["libs"]).expanduser()
        h = hashlib.sha256(libs.read_bytes()).hexdigest()[:12]
        libs_dir = f"{base}/libs-{h}"
        _upload(helper, libs_dir, libs)
        env["LD_LIBRARY_PATH"] = libs_dir
        env.setdefault("VTK_DEFAULT_OPENGL_WINDOW", _WINDOWS["osmesa"])
    return env


def viewer_pod(remote: Remote, name: str, env: dict[str, str], root: str = "/") -> dict[str, Any]:
    s = k8s_viewer_settings(remote)
    image = s.get("image") or image_of(remote)
    python = s["python"]
    cpus = s.get("cpus", "2")
    memory = s.get("memory", "4Gi")
    container = {
        "name": "viewer",
        "image": image,
        "command": [python, "-m", "mallard_gui.viewer", "--listen", str(PORT), "--root", root, "--idle", s["idle"]],
        "env": [{"name": k, "value": v} for k, v in sorted(env.items())],
        "workingDir": "/tmp",
        "resources": {"requests": {"cpu": cpus, "memory": memory}, "limits": {"cpu": cpus, "memory": memory}},
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": labels(remote, "viewer")},
        "spec": pod_spec(remote, container, deadline=seconds_of(s.get("time", "")) or 3600),
    }


def k8s_viewer_settings(remote: Remote) -> dict[str, str]:
    """The viewer settings of a Kubernetes remote: its Python is the image's `python3` unless set."""
    s = viewer_settings(remote)
    if not remote.viewer.get("python"):
        s["python"] = "python3"
    return s


class K8sViewer(SocketViewer):
    """A viewer pod on the remote's volume, one per remote, reused for every run; deleted when the GUI closes
    it, else ending on its idle timeout or at its deadline."""

    def __init__(
        self,
        remote: Remote,
        helper: HelperPod,
        conn: KubeConnection | None = None,
        root: str = "/",
        start_timeout: float = START_TIMEOUT,
        call_timeout: float = CALL_TIMEOUT,
        on_wait: Callable[[float], None] | None = None,
        prepare: Callable[[Remote, HelperPod], dict[str, str]] = prepare_environment,
    ) -> None:
        super().__init__(remote.name, call_timeout)
        settings = k8s_settings(remote)
        self.remote = remote
        self.conn = conn or helper.conn
        self.name = dns_name(settings["prefix"], "viewer", remote.name)
        self.forward: subprocess.Popen[bytes] | None = None
        env = prepare(remote, helper)
        try:
            pod = self.conn.get("pod", self.name)
            if pod is not None and pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                self.conn.run(["delete", "pod", self.name, "--ignore-not-found", "--wait=true", "--timeout=60s"])
                pod = None
            if pod is None:
                self.conn.apply(viewer_pod(remote, self.name, env, root))
            from ..backends.k8s import wait_ready

            wait_ready(self.conn, self.name, start_timeout, on_wait)
            port = self._port_forward(30.0)
            self.connect(port, start_timeout, on_wait)
        except (RemoteError, ViewerError):
            self.close()
            raise

    @property
    def command(self) -> str:
        return f"kubectl port-forward pod/{self.name} :{PORT} --address 127.0.0.1"

    def _port_forward(self, timeout: float) -> int:
        """`kubectl port-forward` to the pod's port, bound to 127.0.0.1 here on a port the system picks."""
        self.forward = self.conn.popen(
            ["port-forward", f"pod/{self.name}", f":{PORT}", "--address", "127.0.0.1"], stdout=subprocess.PIPE
        )
        assert self.forward.stdout is not None
        end = time.monotonic() + timeout
        line = b""
        while time.monotonic() < end:
            line = self.forward.stdout.readline()
            m = _FORWARDING.search(line)
            if m:
                # Keep draining its output so the process never blocks on a full pipe.
                threading.Thread(target=self._drain, daemon=True).start()
                return int(m.group(1))
            if not line and self.forward.poll() is not None:
                break
        err = self.forward.stderr.read().decode("utf-8", "replace").strip() if self.forward.stderr else ""
        raise ViewerError(f"kubectl port-forward to {self.name}: {err or line.decode(errors='replace')}")

    def _drain(self) -> None:
        proc = self.forward
        if proc is not None and proc.stdout is not None:
            for _ in proc.stdout:
                pass

    @property
    def alive(self) -> bool:
        return self.sock is not None and self.forward is not None and self.forward.poll() is None

    def close(self) -> None:
        super().close()
        if self.forward is not None:
            if self.forward.poll() is None:
                self.forward.terminate()
                try:
                    self.forward.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.forward.kill()
                    self.forward.wait()
            self.forward = None
        self.conn.delete("pod", self.name)


__all__ = ["K8sViewer", "SocketViewer", "package_archive", "platform_tag", "prepare_environment", "viewer_pod"]
