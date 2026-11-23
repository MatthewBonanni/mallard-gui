"""`mallard diff`, `post average` and `post extract` as jobs: their arguments, a local process and a Slurm step
on a remote through the fake ssh, the JSON reports, failures and cancelling."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mallard_gui.backends.ssh import SshConnection
from mallard_gui.config import Install, Remote
from mallard_gui.postjobs import (
    LocalLauncher,
    PostJob,
    PostJobs,
    SlurmLauncher,
    average_args,
    diff_args,
    extract_args,
    plane_region,
)

from .test_remotes import FAKE_SSH, fake_cluster, stub

REPORT = {
    "result": "error",
    "fields": [{"name": "P", "status": "error"}, {"name": "RHO", "status": "ok"}],
}


def fake_mallard(path: Path) -> Path:
    """Echoes its arguments and working directory to args.json; `diff` reports REPORT and exits 1."""
    stub(
        path,
        "printf '%s\\n' \"$@\" > args.txt; pwd > cwd.txt\n"
        'case "$1" in diff) echo \'' + json.dumps(REPORT) + "'; exit 1 ;;\n"
        'post) [ "$2" = average ] && { mkdir -p average; echo \'{"files": []}\'; exit 0; }\n'
        "      echo 'E-REGION: no region' >&2; exit 1 ;; esac",
    )
    return path


def test_arguments() -> None:
    assert diff_args("a.vtu", "b.vtu", ["P"], tol=1e-6) == [
        "diff",
        "a.vtu",
        "b.vtu",
        "--fields",
        "P",
        "--format",
        "json",
        "--tol",
        "1e-06",
    ]
    avg = average_args(["solut/flow.vtkhdf"], 1.0, 2.5, name="mean")
    assert avg[:3] == ["post", "average", "solut/flow.vtkhdf"] and avg[-2:] == ["--times", "1.0:2.5"]
    assert "-o" in avg and avg[avg.index("-o") + 1] == "average"
    region = plane_region([0.5, 0, 0], [1, 0, 0])
    assert region == '{type = "cut_plane", point = [0.5, 0.0, 0.0], normal = [1.0, 0.0, 0.0]}'
    ext = extract_args("solut/flow.vtkhdf", 300, region, ["P"], name="cut")
    assert ext[ext.index("--steps") + 1] == "300" and ext[ext.index("--snapshot") + 1] == "vtkhdf"
    with pytest.raises(ValueError, match="output name"):
        average_args(["f"], None, None, name="../x")


def test_local_diff_reports_beyond_tolerance(tmp_path: Path) -> None:
    jobs = PostJobs()
    run = tmp_path / "run"
    run.mkdir()
    got: list[PostJob] = []
    job = jobs.submit(
        PostJob("diff", "run", "local", diff_args("a.vtu", "b.vtu")),
        LocalLauncher(fake_mallard(tmp_path / "mallard")),
        str(run),
        done=got.append,
    )
    job = jobs.wait(job.id)
    assert (job.state, job.exit_status) == ("done", 1)
    assert job.to_dict()["summary"] == "beyond tolerance: P (of 2 fields)"
    assert got == [job]
    assert (run / "cwd.txt").read_text().strip() == str(run.resolve())
    assert (run / "args.txt").read_text().split()[-1] == "--kokkos-num-threads=2"


def test_a_failed_job_says_why(tmp_path: Path) -> None:
    jobs = PostJobs()
    job = jobs.submit(
        PostJob("extract", "run", "local", extract_args("f.vtkhdf", 0, "nowhere")),
        LocalLauncher(fake_mallard(tmp_path / "mallard")),
        str(tmp_path),
    )
    job = jobs.wait(job.id)
    assert job.state == "failed" and job.error == "E-REGION: no region"


def test_cancel_ends_the_process(tmp_path: Path) -> None:
    stub(tmp_path / "slow", "sleep 30")
    jobs = PostJobs()
    job = jobs.submit(
        PostJob("average", "run", "local", ["post", "average"]), LocalLauncher(tmp_path / "slow"), str(tmp_path)
    )
    while job.state == "pending":
        pass
    jobs.cancel(job.id)
    assert jobs.wait(job.id, 10).state == "cancelled"


def test_remote_average_runs_as_a_slurm_step_in_the_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cluster(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_HOMES", str(tmp_path / "homes"))
    mallard = fake_mallard(tmp_path / "opt" / "mallard")
    run = tmp_path / "scratch" / "run"
    run.mkdir(parents=True)
    remote = Remote("hpc", "hpc", installs=[Install(str(mallard))], viewer={"partition": "viz"})
    launcher = SlurmLauncher(SshConnection("hpc", control_dir=tmp_path / "ctl", ssh=FAKE_SSH), remote)
    jobs = PostJobs()
    job = jobs.wait(jobs.submit(PostJob("average", "run", "", average_args(["f.vtkhdf"], 0, 1)), launcher, str(run)).id)
    assert job.state == "done" and job.where == "hpc"
    assert "srun --job-name=mallard-post --ntasks=1" in job.command and "--partition=viz" in job.command
    assert (run / "average").is_dir()
