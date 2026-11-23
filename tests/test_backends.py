from __future__ import annotations

import shlex
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest

from mallard_gui.backends import JobState, LocalBackend, RunSpec, SlurmBackend, SshConnection
from mallard_gui.backends.base import Job, check_request
from mallard_gui.backends.ssh import RemoteError, Result, slurm_script
from mallard_gui.backends.staging import run_input
from mallard_gui.config import Install, Remote
from mallard_gui.runlog import RunLogFollower


def test_run_input_turns_on_control_files_and_absolutizes_existing_paths(cylinder_project: Path) -> None:
    text = run_input(cylinder_project / "input.toml", absolute_paths=True)
    data = tomllib.loads(text)
    assert data["run"]["control_file_interval"] == 10
    assert data["mesh"]["filename"] == str((cylinder_project / "cylinder.msh").resolve())
    assert data["write_data"][0]["prefix"] == "./solut/cylinder"  # does not exist yet: stays relative
    assert "# Flow past a circular cylinder" in text
    remote = tomllib.loads(run_input(cylinder_project / "input.toml", absolute_paths=False))
    assert remote["mesh"]["filename"] == "cylinder.msh"


def test_run_input_keeps_a_set_control_interval(cylinder_project: Path) -> None:
    path = cylinder_project / "input.toml"
    path.write_text(path.read_text().replace("cfl = 1.0", "cfl = 1.0\ncontrol_file_interval = 3"))
    assert tomllib.loads(run_input(path, absolute_paths=False))["run"]["control_file_interval"] == 3


def test_check_request() -> None:
    assert check_request("STOP") == "STOP"
    with pytest.raises(ValueError):
        check_request("../STOP")


class FakeSsh:
    """Records ssh invocations and answers remote commands from a table."""

    def __init__(self, replies: dict[str, Result] | None = None) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.replies = replies or {}
        self.files: dict[str, bytes] = {}

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Result:
        self.calls.append((list(argv), stdin))
        if "-O" in argv:
            return Result(0, b"", b"")
        remote = shlex.split(argv[-1])
        if remote[:2] == ["sh", "-c"] and "cat >" in remote[2]:
            self.files[remote[4]] = stdin or b""
            return Result(0, b"", b"")
        for prefix, reply in self.replies.items():
            if argv[-1].startswith(prefix):
                return reply
        return Result(0, b"", b"")

    def remote_commands(self) -> list[str]:
        return [argv[-1] for argv, _ in self.calls]


def ok(text: str) -> Result:
    return Result(0, text.encode(), b"")


@pytest.fixture
def remote() -> Remote:
    return Remote(
        "cluster",
        "hpc",
        installs=[Install("/opt/mallard/bin/mallard", module="module load mallard/0.10")],
        defaults={"partition": "cpu", "account": "proj"},
    )


def backend(fake: FakeSsh, remote: Remote, tmp_path: Path) -> SlurmBackend:
    return SlurmBackend(remote, SshConnection("hpc", runner=fake, control_dir=tmp_path / "ssh"))


def test_ssh_uses_control_master_and_batch_mode(tmp_path: Path) -> None:
    fake = FakeSsh()
    conn = SshConnection("hpc", runner=fake, control_dir=tmp_path / "ssh", persist="5m")
    conn.run(["echo", "a b; rm -rf /"])
    argv, _ = fake.calls[0]
    assert argv[:-2] == [
        "ssh",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={tmp_path}/ssh/%C",
        "-o",
        "ControlPersist=5m",
        "-o",
        "BatchMode=yes",
    ]
    assert argv[-2] == "hpc"
    assert shlex.split(argv[-1]) == ["echo", "a b; rm -rf /"]
    conn.close()
    assert fake.calls[-1][0][-3:] == ["-O", "exit", "hpc"]
    with pytest.raises(ValueError):
        SshConnection("-oProxyCommand=x")


def test_ssh_error_raises(tmp_path: Path) -> None:
    fake = FakeSsh({"false": Result(1, b"", b"boom")})
    conn = SshConnection("hpc", runner=fake, control_dir=tmp_path)
    with pytest.raises(RemoteError, match="boom"):
        conn.run(["false"])
    assert conn.run(["false"], check=False).returncode == 1


def test_slurm_script(remote: Remote) -> None:
    spec = RunSpec(
        "cyl 1",
        Path("input.toml"),
        "/scratch/runs/cyl",
        ranks=4,
        threads=8,
        options={"partition": "gpu", "time": "01:00:00"},
    )
    script = slurm_script(spec, "/opt/mallard/bin/mallard", "module load mallard")
    lines = script.splitlines()
    assert lines[0] == "#!/bin/bash"
    assert "#SBATCH --job-name='cyl 1'" in lines
    assert "#SBATCH --ntasks=4" in lines and "#SBATCH --cpus-per-task=8" in lines
    assert "#SBATCH --partition=gpu" in lines and "#SBATCH --time=01:00:00" in lines
    assert lines.index("module load mallard") < next(i for i, x in enumerate(lines) if x.startswith("srun"))
    spec.settings = ["control_file_interval=10"]
    assert "--set control_file_interval=10" in slurm_script(spec, "mallard", "")
    assert "srun /opt/mallard/bin/mallard -i input.toml --kokkos-num-threads=8" in lines


def test_slurm_submit_writes_files_and_parses_job_id(remote: Remote, cylinder_project: Path, tmp_path: Path) -> None:
    fake = FakeSsh({"sbatch": ok("4242;cluster\n")})
    b = backend(fake, remote, tmp_path)
    job = b.submit(
        RunSpec("input-1", cylinder_project / "input.toml", "/scratch/input-1", options={"partition": "debug"})
    )
    assert job == Job("4242", "/scratch/input-1", "slurm")
    assert tomllib.loads(fake.files["/scratch/input-1/input.toml"].decode())["run"]["control_file_interval"] == 10
    script = fake.files["/scratch/input-1/run.sbatch"].decode()
    assert "#SBATCH --partition=debug" in script and "#SBATCH --account=proj" in script
    assert fake.remote_commands()[-1] == "sbatch --parsable /scratch/input-1/run.sbatch"


def test_slurm_submit_rejects_relative_dir_and_bad_reply(
    remote: Remote, cylinder_project: Path, tmp_path: Path
) -> None:
    b = backend(FakeSsh({"sbatch": ok("Submitted batch job 1")}), remote, tmp_path)
    with pytest.raises(ValueError):
        b.submit(RunSpec("x", cylinder_project / "input.toml", "runs/x"))
    with pytest.raises(RemoteError):
        b.submit(RunSpec("x", cylinder_project / "input.toml", "/scratch/x"))


@pytest.mark.parametrize(
    ("squeue", "sacct", "state"),
    [
        ("RUNNING\n", "", JobState.RUNNING),
        ("PENDING\n", "", JobState.PENDING),
        ("", "COMPLETED\n", JobState.COMPLETED),
        ("", "CANCELLED by 1234\n", JobState.CANCELLED),
        ("", "TIMEOUT\n", JobState.FAILED),
        ("", "", JobState.UNKNOWN),
    ],
)
def test_slurm_state(remote: Remote, tmp_path: Path, squeue: str, sacct: str, state: JobState) -> None:
    b = backend(FakeSsh({"squeue": ok(squeue), "sacct": ok(sacct)}), remote, tmp_path)
    assert b.state(Job("7", "/scratch/x", "slurm")) == state


def test_slurm_queue(remote: Remote, tmp_path: Path) -> None:
    fake = FakeSsh({"squeue": ok("7|cyl|RUNNING|1:02|node01\n8|sph|PENDING|0:00|(Priority)\nbad line\n")})
    rows = backend(fake, remote, tmp_path).queue()
    assert rows == [
        {"id": "7", "name": "cyl", "state": "RUNNING", "time": "1:02", "reason": "node01"},
        {"id": "8", "name": "sph", "state": "PENDING", "time": "0:00", "reason": "(Priority)"},
    ]
    assert fake.remote_commands()[-1].endswith("--me")


def test_slurm_read_log_offset_and_control(remote: Remote, tmp_path: Path) -> None:
    log = b'{"type":"start"}\n{"type":"progress","t":1.0,"dt":0.1}\n'

    fake = FakeSsh()
    b = backend(fake, remote, tmp_path)

    def reply(argv: Sequence[str], stdin: bytes | None) -> Result:
        remote_argv = shlex.split(argv[-1])
        if remote_argv[:2] == ["sh", "-c"] and "tail -c" in remote_argv[2]:
            assert remote_argv[4] == "/scratch/x/mallard.jsonl"
            return Result(0, log[int(remote_argv[5]) - 1 :], b"")
        return fake(argv, stdin)

    b.conn.runner = reply
    job = Job("7", "/scratch/x", "slurm")
    f = RunLogFollower(Path("unused"), read=lambda off: b.read_log(job, off))
    f.poll()
    assert f.offset == len(log)
    assert f.state.series["run/dt"].values == [0.1]
    assert b.read_log(job, len(log)) == b""
    b.conn.runner = fake
    b.stop(job)
    b.cancel(job)
    assert fake.remote_commands()[-2:] == ["touch /scratch/x/STOP", "scancel 7"]


def test_slurm_backend_needs_install(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SlurmBackend(Remote("c", "hpc"))


def _wait_for(predicate, timeout: float = 120.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise TimeoutError
        time.sleep(0.2)


@pytest.mark.mallard
def test_local_run_stops_on_stop_file(real_mallard, cylinder_project: Path) -> None:  # type: ignore[no-untyped-def]
    path = cylinder_project / "input.toml"
    path.write_text(
        path.read_text()
        .replace("t_stop = 160.0", "t_stop = 100000.0")
        .replace("check_interval = 2000", "check_interval = 5")
    )
    b = LocalBackend(real_mallard.executable, threads=2)
    job = b.submit(RunSpec("input-1", path, str(cylinder_project / "runs" / "input-1")))
    try:
        f = RunLogFollower(Path(job.run_dir) / "mallard.jsonl", read=lambda off: b.read_log(job, off))

        def progressed() -> bool:
            f.poll()
            return bool(f.state.last)

        _wait_for(progressed)
        assert b.state(job) == JobState.RUNNING
        b.stop(job)
        assert b.wait(job, timeout=120) in (0, 3)
        f.poll()
        assert f.state.status == "stopped"
        assert b.state(job) == JobState.COMPLETED
        assert LocalBackend(real_mallard.executable).state(job) == JobState.COMPLETED
    finally:
        b.cancel(job)


def test_local_runs_take_settings_before_the_first_step(cylinder_project: Path, tmp_path: Path) -> None:
    fake = tmp_path / "mallard"
    fake.write_text('#!/bin/sh\necho "$@" > args.txt\n')
    fake.chmod(0o755)
    b = LocalBackend(fake)
    spec = RunSpec("r", cylinder_project / "input.toml", str(tmp_path / "r"), settings=["control_file_interval=10"])
    job = b.submit(spec)
    b.wait(job, timeout=30)
    assert "--set control_file_interval=10" in (tmp_path / "r" / "args.txt").read_text()
