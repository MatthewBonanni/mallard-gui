"""Remotes: the store's edits, the connection probe and install discovery through the fake ssh, and the queue."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mallard_gui.backends.ssh import SshConnection, slurm_queue
from mallard_gui.config import Install, Remote, RemoteStore
from mallard_gui.remotes import parse_probe, probe, release_of

FAKE_SSH = str(Path(__file__).parent / "fixtures" / "fake-ssh")


def stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def fake_cluster(root: Path) -> None:
    """A Linux login node: Slurm's commands and a mallard on PATH, and an install under ~/mallard/."""
    bin_dir = root / "bin"
    stub(bin_dir / "uname", 'case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; *) echo Linux ;; esac')
    stub(bin_dir / "ldd", "echo 'ldd (GNU libc) 2.28'")
    stub(bin_dir / "sbatch", "exit 0")
    stub(bin_dir / "sinfo", r"printf 'cpu*\t(null)\t12\ngpu\tgpu:a100:4\t4\n'")
    stub(
        bin_dir / "squeue",
        r"printf '4242|cyl-re100|RUNNING|1:02:03|node12\n4243|sweep-3|PENDING|0:00|(Priority)\n'",
    )
    stub(bin_dir / "mallard", "echo 'Mallard v0.9.2'")
    # srun runs its command as the job step, here and now.
    stub(bin_dir / "srun", 'while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac; done\nexec "$@"')
    install = root / "homes" / "hpc" / "mallard" / "0.10.0-cpu"
    stub(install / "bin" / "mallard", "echo 'Mallard v0.10.0'")
    (install / "install.toml").write_text('version = "0.10.0"\n')


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_cluster(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_HOMES", str(tmp_path / "homes"))
    return tmp_path


def connection(host: str, tmp_path: Path) -> SshConnection:
    return SshConnection(host, control_dir=tmp_path / "ctl", ssh=FAKE_SSH)


def test_a_renamed_remote_keeps_its_id_and_place(tmp_path: Path) -> None:
    store = RemoteStore(tmp_path / "remotes.toml")
    store.add(Remote("a", "a"))
    store.add(Remote("hpc", "hpc", installs=[Install("/opt/m")]))
    store.add(Remote("z", "z"))
    rid = store.remotes["hpc"].id
    store.update("hpc", Remote("cluster", "login.hpc", installs=[Install("/opt/m")]))
    store.save()
    again = RemoteStore(tmp_path / "remotes.toml")
    assert list(again.remotes) == ["a", "cluster", "z"]
    assert again.remotes["cluster"].id == rid
    assert again.remotes["cluster"].host == "login.hpc"
    with pytest.raises(ValueError, match="exists"):
        again.update("cluster", Remote("a", "a"))
    again.remove("cluster")
    assert again.by_id(rid) is None


def test_probe_finds_scheduler_partitions_and_installs(cluster: Path) -> None:
    p = probe(connection("hpc", cluster), recorded=["/gone/bin/mallard"])
    assert p.ok, p.error
    assert (p.os, p.arch, p.glibc) == ("Linux", "x86_64", "2.28")
    assert p.scheduler == "slurm"
    assert p.partitions == [
        {"name": "cpu*", "gres": "", "nodes": "12"},
        {"name": "gpu", "gres": "gpu:a100:4", "nodes": "4"},
    ]
    found = {c.source: (c.path, c.version) for c in p.candidates}
    assert found["PATH"] == (str(cluster / "bin" / "mallard"), "0.9.2")
    home = cluster / "homes" / "hpc" / "mallard" / "0.10.0-cpu"
    assert found[str(home / "install.toml")] == (str(home / "bin" / "mallard"), "0.10.0")
    assert p.missing == ["/gone/bin/mallard"]


def test_probe_of_an_unreachable_host_reports_ssh_error(tmp_path: Path) -> None:
    p = probe(connection("unreachable-hpc", tmp_path))
    assert not p.ok
    assert "Connection refused" in p.error


def test_module_candidates_name_their_module_line() -> None:
    p = parse_probe(
        "os\tLinux\narch\taarch64\nglibc\tldd (GNU libc) 2.34\nhas\tmodule\n"
        "cand\tmodule:mallard/0.10.0-cuda\t/apps/mallard/bin/mallard\tMallard v0.10.0\n"
        "cand\tPATH\t/apps/mallard/bin/mallard\tMallard v0.10.0\n"
    )
    assert p.glibc == "2.34"
    assert len(p.candidates) == 1
    install = p.candidates[0].install()
    assert (install.path, install.module, install.version) == (
        "/apps/mallard/bin/mallard",
        "module load mallard/0.10.0-cuda",
        "0.10.0",
    )


def test_release_of_a_build() -> None:
    assert release_of("v0.11.0-rc1") == "0.11.0"
    assert release_of("0.10.0-147-g1fa6e0b05") == "0.10.0"


def test_queue_lists_the_users_jobs(cluster: Path) -> None:
    jobs = slurm_queue(connection("hpc", cluster))
    assert [(j["id"], j["state"], j["reason"]) for j in jobs] == [
        ("4242", "RUNNING", "node12"),
        ("4243", "PENDING", "(Priority)"),
    ]
