"""Installs on a remote, end to end through the fake ssh: a local directory is the release (archives,
their SHA-256 and the manifest, as file:// URLs), another is the remote's home. Archives, Apptainer and
Spack (as a batch job) install side by side, verify, upgrade and are removed."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from mallard_gui.backends.ssh import SshConnection
from mallard_gui.config import Install, Remote, RemoteStore
from mallard_gui.installs import (
    MANIFEST,
    InstallError,
    ReleaseManifest,
    fetch_manifest,
    make_default,
    plan,
    remove,
    run_install,
    upgrade_target,
    verify,
    verify_command,
)
from mallard_gui.remotes import parse_probe, probe

from .test_remotes import FAKE_SSH, fake_cluster, stub

VERDICT = {
    "schema": "mallard-install-verify",
    "version": 1,
    "passed": True,
    "seconds": 4.5,
    "checks": [
        {"name": "environment", "dim": 2, "passed": True, "seconds": 0.3},
        {"name": "run", "dim": 2, "passed": True, "seconds": 2.1, "template": "sod"},
        {"name": "run", "dim": 3, "passed": True, "seconds": 2.1, "template": "sod_3d"},
    ],
}


def mallard_stub(version: str, verdict: dict[str, object] | None = VERDICT) -> str:
    """A mallard that answers --version and, from 0.12 on, install verify (else as older builds do)."""
    if verdict is None:
        verify = 'echo "mallard: unknown command install verify (see mallard --help)" >&2; exit 2'
    else:
        verify = f"echo '{json.dumps(verdict)}'; exit {0 if verdict.get('passed') else 1}"
    return (
        f'case "$1" in --version) echo "Mallard v{version}" ;; '
        f"install) {verify} ;; "
        """check) echo '{"version": 1, "diagnostics": []}' ;; esac"""
    )


def make_archive(release: Path, version: str, arch: str = "x86_64", variant: str = "cpu", **kw: object) -> Path:
    top = f"mallard-{version}-linux-{arch}-{variant}"
    staging = release / "staging" / top
    stub(staging / "bin" / "mallard", mallard_stub(version, **kw))  # type: ignore[arg-type]
    (staging / "share" / "mallard").mkdir(parents=True, exist_ok=True)
    out = release / f"v{version}" / f"{top}.tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        tar.add(staging, arcname=top)
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.parent / f"{out.name}.sha256").write_text(f"{sha}  {out.name}\n")
    return out


def write_manifest(release: Path, version: str, archives: list[Path], glibc: str = "2.28") -> None:
    artifacts: list[dict[str, object]] = [
        {
            "kind": "archive",
            "name": a.name,
            "variant": a.name.removesuffix(".tar.gz").rsplit("-", 1)[1],
            "platform": "linux-" + a.name.split("-linux-")[1].rsplit("-", 1)[0],
            "url": a.as_uri(),
            "sha256": hashlib.sha256(a.read_bytes()).hexdigest(),
            "size": a.stat().st_size,
            "glibc": glibc,
        }
        for a in archives
    ]
    artifacts.append(
        {
            "kind": "image",
            "name": f"ghcr.io/matthewbonanni/mallard:{version}",
            "variant": "cpu",
            "digest": "sha256:" + "ab" * 32,
        }
    )
    data = {"schema": "mallard-release-manifest", "version": 1, "mallard": version, "artifacts": artifacts}
    (release / f"v{version}" / MANIFEST).write_text(json.dumps(data))


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_cluster(tmp_path)
    (tmp_path / "bin" / "mallard").unlink()
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_HOMES", str(tmp_path / "homes"))
    release = tmp_path / "release"
    monkeypatch.setenv("MALLARD_GUI_RELEASES", release.as_uri())
    for v in ("0.12.0", "0.13.0"):
        write_manifest(release, v, [make_archive(release, v)])
    return tmp_path


def conn(tmp_path: Path, host: str = "hpc") -> SshConnection:
    return SshConnection(host, control_dir=tmp_path / "ctl", ssh=FAKE_SSH)


def home(cluster: Path) -> Path:
    return cluster / "homes" / "hpc"


def test_archive_installs_beside_others_checked_and_verified(cluster: Path) -> None:
    c = conn(cluster)
    p = probe(c)
    assert p.home == str(home(cluster))
    manifest = fetch_manifest("0.12.0-31-gabc")
    assert manifest.version == "0.12.0" and manifest.source.endswith(MANIFEST)
    pl = plan(p, manifest)
    assert pl.method == "archive" and pl.directory == f"{home(cluster)}/mallard/0.12.0-cpu"
    assert subprocess.run(["sh", "-n"], input=pl.script, text=True, check=False).returncode == 0
    lines: list[str] = []
    out = run_install(c, pl, lines.append)
    assert out.ok, out.log
    assert out.install is not None
    assert out.install.path == f"{pl.directory}/bin/mallard"
    assert (out.install.version, out.install.method, out.install.sha256) == ("0.12.0", "archive", pl.sha256)
    assert any("OK" in line for line in lines)  # sha256sum -c
    assert "Mallard v0.12.0" in out.log
    record = (home(cluster) / "mallard" / "0.12.0-cpu" / "install.toml").read_text()
    assert f'sha256 = "{pl.sha256}"' in record and 'method = "archive"' in record
    assert not (home(cluster) / "mallard" / "0.12.0-cpu.partial").exists()

    again = run_install(c, pl)
    assert not again.ok and "never overwritten" in again.error

    remote = Remote("hpc", "hpc", installs=[out.install], defaults={"partition": "cpu"})
    v = verify(c, remote, out.install)
    assert v.passed, v.summary
    assert v.summary == "passed: 3 checks in 4.5 s"
    assert out.install.verified.startswith("passed ")
    assert verify_command(remote, out.install)[:2] == ["srun", "--ntasks=1"]
    assert "--partition=cpu" in verify_command(remote, out.install)

    store = RemoteStore(cluster / "remotes.toml")
    store.add(remote)
    store.save()
    kept = RemoteStore(cluster / "remotes.toml").remotes["hpc"].installs[0]
    assert (kept.sha256, kept.verified, kept.verify_summary) == (pl.sha256, out.install.verified, v.summary)


def test_a_download_that_does_not_match_its_sha256_never_lands(cluster: Path) -> None:
    release = cluster / "release"
    manifest = fetch_manifest("0.12.0", release.as_uri())
    manifest.artifacts[0].sha256 = "0" * 64
    out = run_install(conn(cluster), plan(probe(conn(cluster)), manifest))
    assert not out.ok
    assert "FAILED" in out.log
    assert not (home(cluster) / "mallard" / "0.12.0-cpu").exists()


def test_upgrade_installs_beside_verifies_then_becomes_default(cluster: Path) -> None:
    c = conn(cluster)
    p = probe(c)
    old = run_install(c, plan(p, fetch_manifest("0.12.0"))).install
    assert old is not None
    remote = Remote("hpc", "hpc", installs=[old])
    assert upgrade_target(remote, "0.12.0") == ""
    assert upgrade_target(remote, "0.11.0") == ""
    assert upgrade_target(remote, "0.13.0-4-gdeadbee") == "0.13.0"
    new = run_install(c, plan(p, fetch_manifest("0.13.0"))).install
    assert new is not None
    remote.installs.append(new)
    assert verify(c, remote, new).passed
    make_default(remote, new.path)
    assert [i.version for i in remote.installs] == ["0.13.0", "0.12.0"]
    assert (home(cluster) / "mallard" / "0.12.0-cpu" / "bin" / "mallard").exists()
    remove(c, old)
    assert not (home(cluster) / "mallard" / "0.12.0-cpu").exists()
    with pytest.raises(InstallError, match="not an install directory"):
        remove(c, Install("/usr/bin/mallard"))


def test_verify_reports_failed_checks_and_older_builds(cluster: Path) -> None:
    c = conn(cluster)
    remote = Remote("hpc", "hpc")
    failing = dict(VERDICT, passed=False, checks=[{"name": "run", "dim": 3, "passed": False}])
    for version, verdict in (("0.14.0", failing), ("0.9.0", None)):
        release = cluster / "release"
        write_manifest(release, version, [make_archive(release, version, verdict=verdict)])
        out = run_install(c, plan(probe(c), fetch_manifest(version)))
        install = out.install
        assert install is not None, out.log
        v = verify(c, remote, install)
        if verdict is None:
            assert v.passed and "predates install verify" in v.summary
        else:
            assert not v.passed and v.summary == "failed: run 3D"
            assert install.verified.startswith("failed ")


def test_plan_order_and_reasons(cluster: Path) -> None:
    manifest = fetch_manifest("0.12.0")
    arm = parse_probe("os\tLinux\narch\taarch64\nhome\t/home/u\nhas\tcurl\nhas\tapptainer\nhas\tspack\nhas\tsbatch\n")
    pl = plan(arm, manifest)
    assert pl.method == "apptainer" and pl.directory == "/home/u/mallard/0.12.0-cpu-apptainer"
    assert pl.skipped == ["archive: release 0.12.0 has no cpu archive for aarch64"]
    assert f"docker://ghcr.io/matthewbonanni/mallard@sha256:{'ab' * 32}" in pl.script
    assert plan(arm, manifest, method="spack").job
    old = parse_probe("os\tLinux\narch\tx86_64\nglibc\tldd (GNU libc) 2.17\nhas\tcurl\n")
    with pytest.raises(InstallError, match=r"needs glibc 2\.28; the login node has 2\.17.*no apptainer.*no spack"):
        plan(old, manifest)
    with pytest.raises(InstallError, match="Linux only"):
        plan(parse_probe("os\tDarwin\n"), manifest)
    for script in (pl.script, plan(arm, manifest, method="spack").script):
        assert subprocess.run(["sh", "-n"], input=script, text=True, check=False).returncode == 0


def test_a_release_without_a_manifest_uses_its_archives_sha256_files(cluster: Path) -> None:
    release = cluster / "release"
    make_archive(release, "0.11.0")
    manifest = fetch_manifest("0.11.0")
    assert manifest.source == ""
    a = manifest.find("archive", "cpu", "linux-x86_64")
    assert a is not None and len(a.sha256) == 64
    with pytest.raises(InstallError, match="not a mallard-release-manifest"):
        ReleaseManifest.from_json({"schema": "other"})


def test_apptainer_pulls_by_digest_and_wraps_the_image(cluster: Path) -> None:
    stub(
        cluster / "bin" / "apptainer",
        'case "$1" in pull) echo image > "$2" ;; '
        'exec) shift; while [ "${1#-}" != "$1" ]; do shift; done; shift; shift; echo "Mallard v0.12.0 $*" ;; esac',
    )
    c = conn(cluster)
    p = probe(c)
    pl = plan(p, fetch_manifest("0.12.0"), method="apptainer")
    out = run_install(c, pl)
    assert out.ok, out.log
    d = home(cluster) / "mallard" / "0.12.0-cpu-apptainer"
    assert (d / "mallard.sif").read_text() == "image\n"
    assert "Mallard v0.12.0 --version" in out.log
    assert 'digest = "sha256:' in (d / "install.toml").read_text()


def test_spack_builds_as_a_batch_job_followed_by_its_log(cluster: Path) -> None:
    prefix = cluster / "spack-opt" / "mallard"
    stub(prefix / "bin" / "mallard", mallard_stub("0.12.0"))
    stub(
        cluster / "bin" / "spack",
        f'case "$*" in *location*) echo {prefix} ;; *install*) echo "==> Installing mallard" ;; esac',
    )
    # sbatch runs the job at once, writing its log where --output says; squeue then finds it gone.
    stub(
        cluster / "bin" / "sbatch",
        'for a; do case "$a" in --output=*) log=${a#--output=} ;; esac; s=$a; done\nbash "$s" > "$log" 2>&1; echo 777',
    )
    stub(cluster / "bin" / "squeue", "exit 0")
    c = conn(cluster)
    pl = plan(probe(c), fetch_manifest("0.12.0"), method="spack")
    assert pl.job
    lines: list[str] = []
    out = run_install(c, pl, lines.append, poll_s=0)
    assert out.ok, out.log
    assert lines[0].startswith("submitted the build as job 777")
    assert "==> Installing mallard" in out.log
    wrapper = home(cluster) / "mallard" / "0.12.0-cpu-spack" / "bin" / "mallard"
    assert subprocess.run([str(wrapper), "--version"], capture_output=True, text=True).stdout == "Mallard v0.12.0\n"
