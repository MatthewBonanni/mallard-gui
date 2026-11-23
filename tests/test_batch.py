from __future__ import annotations

from pathlib import Path

import pytest

from mallard_gui.backends import backend_for
from mallard_gui.backends.base import Job, JobState, RunSpec
from mallard_gui.backends.batch import LsfBackend, PbsBackend, lsf_script, lsf_time, parse_qstat_f, pbs_script
from mallard_gui.backends.k8s import K8sBackend
from mallard_gui.backends.ssh import RemoteError, SlurmBackend, SshConnection
from mallard_gui.config import Install, Remote

from .test_backends import FakeSsh, ok


def remote_of(kind: str) -> Remote:
    return Remote(
        "c",
        "hpc",
        backend=kind,
        installs=[Install("/opt/mallard/bin/mallard", module="module load mallard")],
        defaults={"partition": "workq", "account": "proj", "time": "01:30:20"},
    )


def spec(**kw: object) -> RunSpec:
    s = RunSpec("cyl 1", Path("input.toml"), "/scratch/runs/cyl", ranks=4, threads=8, restart="/s/r.restart")
    s.options = {"partition": "workq", "account": "proj", "time": "01:30:20"}
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_backend_for_each_kind() -> None:
    assert type(backend_for(remote_of("slurm"))) is SlurmBackend
    assert type(backend_for(remote_of("pbs"))) is PbsBackend
    assert type(backend_for(remote_of("lsf"))) is LsfBackend
    k = Remote("k", "ctx", backend="k8s", installs=[Install("img")], k8s={"namespace": "n", "pvc": "p"})
    assert type(backend_for(k)) is K8sBackend
    with pytest.raises(ValueError):
        PbsBackend(remote_of("slurm"))


def test_pbs_script() -> None:
    lines = pbs_script(spec(), "/opt/mallard/bin/mallard", "module load mallard").splitlines()
    assert "#PBS -N cyl_1" in lines and "#PBS -l select=1:ncpus=32:mpiprocs=4:ompthreads=8" in lines
    assert "#PBS -l walltime=01:30:20" in lines and "#PBS -q workq" in lines and "#PBS -A proj" in lines
    assert "cd /scratch/runs/cyl" in lines
    assert (
        lines[-1] == "mpiexec -n 4 /opt/mallard/bin/mallard -i input.toml --kokkos-num-threads=8 --restart /s/r.restart"
    )
    single = pbs_script(spec(ranks=1, restart=""), "mallard").splitlines()
    assert single[-1] == "mallard -i input.toml --kokkos-num-threads=8"
    assert "#PBS -N m1-long-name-ov" in pbs_script(spec(name="1-long-name-over-15"), "m").splitlines()


def test_pbs_submit_state_queue_cancel(cylinder_project: Path, tmp_path: Path) -> None:
    fake = FakeSsh({"qsub": ok("1234.pbs01\n")})
    b = PbsBackend(remote_of("pbs"), SshConnection("hpc", runner=fake, control_dir=tmp_path))
    job = b.submit(RunSpec("input-1", cylinder_project / "input.toml", "/scratch/input-1"))
    assert job == Job("1234.pbs01", "/scratch/input-1", "pbs")
    assert "#PBS -A proj" in fake.files["/scratch/input-1/run.pbs"].decode()
    assert fake.remote_commands()[-1] == "qsub /scratch/input-1/run.pbs"
    for text, state in [
        ("Job Id: 1234\n    job_state = R\n", JobState.RUNNING),
        ("Job Id: 1234\n    job_state = Q\n", JobState.PENDING),
        ("    job_state = F\n    Exit_status = 0\n", JobState.COMPLETED),
        ("    job_state = F\n    Exit_status = 3\n", JobState.FAILED),
        ("    job_state = F\n    Exit_status = -1\n", JobState.CANCELLED),
        ("", JobState.UNKNOWN),
    ]:
        b.conn.runner = FakeSsh({"qstat": ok(text)})
        assert b.state(job) == state, text
    table = (
        "\npbs01:\n                                                            Req'd  Req'd   Elap\n"
        "Job ID          Username Queue    Jobname    SessID NDS TSK Memory Time  S Time\n"
        "--------------- -------- -------- ---------- ------ --- --- ------ ----- - -----\n"
        "1234.pbs01      me       workq    input-1     12345   1  32    --  01:30 R 00:02\n"
    )
    b.conn.runner = FakeSsh({"sh -c": ok(table)})
    assert b.queue() == [{"id": "1234.pbs01", "name": "input-1", "state": "RUNNING", "time": "00:02", "reason": ""}]
    fake = FakeSsh()
    b.conn.runner = fake
    b.cancel(job)
    assert fake.remote_commands()[-1] == "qdel 1234.pbs01"
    b.conn.runner = FakeSsh({"qsub": ok("qsub: Unknown queue")})
    with pytest.raises(RemoteError):
        b.submit(RunSpec("x", cylinder_project / "input.toml", "/scratch/x"))


def test_qstat_continuation_lines() -> None:
    attrs = parse_qstat_f("Job Id: 7\n    Job_Name = a\n    Variable_List = A=1,\n\tB=2\n    job_state = R\n")
    assert attrs == {"Job_Name": "a", "Variable_List": "A=1,B=2", "job_state": "R"}


def test_lsf_script_and_time() -> None:
    assert lsf_time("01:30:20") == "1:31" and lsf_time("1-00:00:00") == "24:00" and lsf_time("45:00") == "0:45"
    lines = lsf_script(spec(), "/opt/mallard/bin/mallard", "module load mallard").splitlines()
    assert "#BSUB -J 'cyl 1'" in lines and "#BSUB -n 4" in lines and '#BSUB -R "affinity[core(8)]"' in lines
    assert "#BSUB -cwd /scratch/runs/cyl" in lines and "#BSUB -W 1:31" in lines
    assert "#BSUB -q workq" in lines and "#BSUB -P proj" in lines
    assert lines[-1].startswith("mpirun -n 4 /opt/mallard/bin/mallard -i input.toml")


def test_lsf_submit_state_queue_cancel(cylinder_project: Path, tmp_path: Path) -> None:
    fake = FakeSsh({"sh -c": ok("Job <5678> is submitted to queue <workq>.\n")})
    b = LsfBackend(remote_of("lsf"), SshConnection("hpc", runner=fake, control_dir=tmp_path))
    job = b.submit(RunSpec("input-1", cylinder_project / "input.toml", "/scratch/input-1"))
    assert job == Job("5678", "/scratch/input-1", "lsf")
    assert "bsub <" in fake.remote_commands()[-1] and "/scratch/input-1/run.lsf" in fake.remote_commands()[-1]
    for text, state in [
        ("RUN -\n", JobState.RUNNING),
        ("PEND -\n", JobState.PENDING),
        ("DONE -\n", JobState.COMPLETED),
        ("EXIT 1\n", JobState.FAILED),
        ("EXIT 130\n", JobState.CANCELLED),
        ("", JobState.UNKNOWN),
    ]:
        b.conn.runner = FakeSsh({"bjobs": ok(text)})
        assert b.state(job) == state, text
    b.conn.runner = FakeSsh({"bjobs": ok("5678|input-1|PEND|0 second(s)|Job's resource requirements not satisfied\n")})
    assert b.queue() == [
        {
            "id": "5678",
            "name": "input-1",
            "state": "PENDING",
            "time": "0",
            "reason": "Job's resource requirements not satisfied",
        }
    ]
    fake = FakeSsh()
    b.conn.runner = fake
    b.cancel(job)
    assert fake.remote_commands()[-1] == "bkill 5678"
