"""The last fallback when no live viewer can be reached (the design's "no live view"): images rendered by a
batch job next to the data and shown from their files. A view is a JSON description (run, series, snapshot,
field, colormap, range, zones, camera, size); the job renders each to a PNG beside it under the run's
`extract/images/`, and the GUI reads the PNG back over SSH into memory."""

from __future__ import annotations

import json
import secrets
import shlex
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ..backends.ssh import SshConnection
from ..config import Remote
from .client import slurm_resources, viewer_settings
from .engine import ResultScene, ViewerError

IMAGES = "extract/images"


def render_views(spec: dict[str, Any], root: Path) -> list[str]:
    """Render every view of `spec`; returns the PNG paths written."""
    scene = ResultScene(root)
    out = []
    for view in spec["views"]:
        scene.open(view["run"])
        scene.show(view["series"], int(view.get("index", 0)), view.get("zones"))
        scene.set_field(
            field=view.get("field", ""),
            component=view.get("component", ""),
            colormap=view.get("colormap", ""),
            range=view.get("range"),
            interpolation=view.get("interpolation", ""),
        )
        if view.get("theme"):
            scene.set_theme(view["theme"])
        if view.get("camera"):
            scene.set_camera(**view["camera"])
        elif view.get("preset"):
            scene.camera("preset", preset=view["preset"])
        scene.window.SetSize(int(view.get("width", 1600)), int(view.get("height", 1000)))
        target = Path(view["png"])
        partial = target.with_name(target.name + ".partial")
        partial.write_bytes(scene.render_image("png"))
        partial.replace(target)
        out.append(str(target))
    return out


def main_batch(spec_path: str, root: str) -> int:
    spec = json.loads(Path(spec_path).read_text())
    for path in render_views(spec, Path(root)):
        print(path)
    return 0


class BatchImages:
    """Views rendered by batch jobs on a remote. Slow (a queue wait per image) but needs only `sbatch`."""

    def __init__(self, conn: SshConnection, remote: Remote, poll: float = 3.0) -> None:
        self.conn = conn
        self.remote = remote
        self.poll = poll
        self.where = remote.name
        self.jobs: list[str] = []

    def command(self, spec_path: str) -> list[str]:
        s = viewer_settings(self.remote)
        if not s.get("python"):
            raise ViewerError(f"remote {self.remote.name}: no viewer Python for batch images")
        render = shlex.join([s["python"], "-m", "mallard_gui.viewer", "--batch", spec_path, "--root", "/"])
        return [
            "sbatch",
            "--parsable",
            "--job-name=mallard-images",
            "--ntasks=1",
            *slurm_resources(s),
            "--wrap",
            render,
        ]

    def render(self, run_dir: str, view: dict[str, Any], timeout: float = 3600.0) -> bytes:
        """One view's PNG: written by a batch job under the run's `extract/images/`, read back into memory."""
        token = secrets.token_hex(6)
        base = PurePosixPath(run_dir) / IMAGES
        png = str(base / f"{token}.png")
        spec_path = str(base / f"{token}.json")
        self.conn.run(["mkdir", "-p", str(base)])
        spec = {"views": [{**view, "run": run_dir, "png": png}]}
        self.conn.write_file(spec_path, json.dumps(spec).encode())
        jobid = self.conn.run(self.command(spec_path)).text.strip().split(";")[0]
        self.jobs.append(jobid)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.conn.run(["squeue", "-h", "-j", jobid, "-o", "%T"], check=False).text.strip()
            if not state:
                break
            time.sleep(self.poll)
        else:
            self.conn.run(["scancel", jobid], check=False)
            raise ViewerError(f"batch image job {jobid}: not done in {timeout:.0f} s")
        got = self.conn.run(["cat", png], check=False)
        if got.returncode != 0 or not got.stdout.startswith(b"\x89PNG"):
            raise ViewerError(f"batch image job {jobid}: no image ({got.stderr.decode(errors='replace')[-200:]})")
        return got.stdout

    def close(self) -> None:
        for jobid in self.jobs:
            self.conn.run(["scancel", jobid], check=False)
        self.jobs.clear()
