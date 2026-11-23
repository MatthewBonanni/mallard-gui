from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from mallard_gui.backends.base import Backend, Job, JobState, RunSpec
from mallard_gui.config import Install, Remote
from mallard_gui.data.manager import DataManager
from mallard_gui.data.relocate import RelocateError, Relocator, compare_restart, mallard_command, restart_facts

from .conftest import FIXTURES
from .test_data import cache, manager, shim  # noqa: F401  (fixtures)

INFO = json.loads((FIXTURES / "restart_info.json").read_text())


def fake_mallard(tmp_path: Path, name: str, **changes: Any) -> str:
    """A `mallard info` whose answer is restart_info.json with `changes`."""
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    shutil.copy(FIXTURES / "fake-mallard-info", path)
    path.with_name(name + ".json").write_text(json.dumps({**INFO, **changes}))
    return str(path)


class StubBackend(Backend):
    kind = "stub"

    def __init__(self) -> None:
        self.specs: list[RunSpec] = []

    def submit(self, spec: RunSpec) -> Job:
        self.specs.append(spec)
        return Job("77", spec.run_dir, self.kind)

    def state(self, job: Job) -> JobState:
        return JobState.RUNNING

    def read_file(self, job: Job, path: str, offset: int) -> bytes:
        return b""

    def control(self, job: Job, request: str) -> None:
        pass

    def cancel(self, job: Job) -> None:
        pass


@pytest.fixture
def relocator(manager: DataManager, tmp_path: Path) -> Relocator:  # noqa: F811
    manager.remotes.remotes["hpc"].installs = [Install(fake_mallard(tmp_path, "mallard-hpc"))]
    return Relocator(manager, Path(fake_mallard(tmp_path, "mallard-here")))


def test_restart_facts_and_comparison() -> None:
    facts = restart_facts(INFO)
    assert facts["dim"] == 2 and facts["precision"] == "double" and facts["cells"] == 1536 and facts["step"] == 180
    assert facts["fields"] == ["RHO", "RHOU_X", "RHOU_Y", "RHOE"] and facts["species"] == []
    assert compare_restart("/r", "hpc", INFO, INFO).ok
    check = compare_restart("/r", "hpc", INFO, {**INFO, "dim": 3, "precision": "float"})
    assert not check.ok and check.problems == [
        "dim: 3 at hpc, 2 where it ran",
        "precision: float at hpc, double where it ran",
    ]
    assert "not a restart" in compare_restart("/r", "hpc", INFO, {**INFO, "format": "vtkhdf"}).problems[0]


def test_mallard_command_per_location(tmp_path: Path) -> None:
    assert mallard_command(None, Path("/here/mallard")) == ["/here/mallard"]
    plain = Remote("a", "a", installs=[Install("/opt/m/bin/mallard")])
    assert mallard_command(plain, Path("x")) == ["/opt/m/bin/mallard"]
    mod = Remote("a", "a", installs=[Install("/opt/m/bin/mallard", module="module load mallard/0.10")])
    assert (
        mallard_command(mod, Path("x"))[-1] == "/opt/m/bin/mallard"
        and "module load" in mallard_command(mod, Path("x"))[2]
    )
    k = Remote("k", "ctx", backend="k8s", installs=[Install("img")], k8s={"namespace": "n", "pvc": "p"})
    assert mallard_command(k, Path("x")) == ["mallard"]
    with pytest.raises(RelocateError):
        mallard_command(Remote("b", "b"), Path("x"))


def test_check_moves_only_the_restart_and_reads_it_there(relocator: Relocator, tmp_path: Path) -> None:
    record = relocator.data.project.run("input-1")
    check, there = relocator.check_restart(record, "hpc")
    assert check.ok and there == str(tmp_path / "hpc" / "runs" / "input-1" / "restart_000010.restart")
    copied = sorted(p.name for p in (tmp_path / "hpc" / "runs" / "input-1").rglob("*") if p.is_file())
    assert copied == ["restart_000010.restart"]


def test_a_destination_that_reads_it_otherwise_fails_the_check(relocator: Relocator, tmp_path: Path) -> None:
    relocator.data.remotes.remotes["hpc"].installs = [Install(fake_mallard(tmp_path, "mallard-3d", dim=3))]
    check, _ = relocator.check_restart(relocator.data.project.run("input-1"), "hpc")
    assert not check.ok and check.problems == ["dim: 3 at hpc, 2 where it ran"]
    relocator.data.remotes.remotes["hpc"].installs = [Install(str(tmp_path / "bin" / "missing-mallard"))]
    check, _ = relocator.check_restart(relocator.data.project.run("input-1"), "hpc")
    assert not check.ok and "cannot read it" in check.problems[0]


def test_continue_elsewhere_stages_inputs_and_starts_from_the_restart_there(
    relocator: Relocator, tmp_path: Path
) -> None:
    stub = StubBackend()
    relocator.backend = lambda remote: stub
    record = relocator.data.project.run("input-1")
    input_file = relocator.data.project.root / "input.toml"
    c = relocator.continue_elsewhere(record, "hpc", input_file, "input-2")
    run_dir = tmp_path / "hpc" / "runs" / "input-2"
    spec = stub.specs[0]
    assert spec.run_dir == str(run_dir) and spec.restart == str(
        tmp_path / "hpc" / "runs" / "input-1" / "restart_000010.restart"
    )
    assert (run_dir / "cylinder.msh").read_bytes() == (relocator.data.project.root / "cylinder.msh").read_bytes()
    saved = relocator.data.project.run("input-2")
    assert saved.home["remote"] == "hpc" and saved.home["directory"] == str(run_dir) and saved.job == "77"
    assert c.check.ok


def test_continue_elsewhere_stops_before_running_when_the_check_fails(relocator: Relocator, tmp_path: Path) -> None:
    stub = StubBackend()
    relocator.backend = lambda remote: stub
    relocator.data.remotes.remotes["hpc"].installs = [Install(fake_mallard(tmp_path, "mallard-f", precision="float"))]
    record = relocator.data.project.run("input-1")
    with pytest.raises(RelocateError, match="precision"):
        relocator.continue_elsewhere(record, "hpc", relocator.data.project.root / "input.toml", "input-2")
    assert not stub.specs and not (tmp_path / "hpc" / "runs" / "input-2").exists()


def test_move_copies_everything_then_changes_the_home(relocator: Relocator, tmp_path: Path) -> None:
    project = relocator.data.project
    record = project.run("input-1")
    with pytest.raises(RelocateError, match="not ended"):
        relocator.move(record, "hpc")
    record.outcome = "stopped"
    project.save_run(record)
    moved, check, old = relocator.move(project.run("input-1"), "hpc")
    root = tmp_path / "hpc" / "runs" / "input-1"
    assert check is not None and check.ok
    assert project.run("input-1").home == {
        "remote": "hpc",
        "remote_id": moved.home["remote_id"],
        "directory": str(root),
    }
    assert (root / "solut" / "cyl_000000.vtu").is_file() and (root / "mallard.jsonl").is_file()
    home = project.root / "runs" / "input-1"
    assert str(home / "mallard.jsonl") in old and all(Path(p).exists() for p in old)  # the old home is left
    relocator.remove_old_home("local", old)
    assert not any(Path(p).exists() for p in old)
    with pytest.raises(RelocateError, match="already"):
        relocator.move(project.run("input-1"), "hpc")


def test_a_failed_check_leaves_the_home(relocator: Relocator, tmp_path: Path) -> None:
    project = relocator.data.project
    record = project.run("input-1")
    record.outcome = "stopped"
    project.save_run(record)
    relocator.data.remotes.remotes["hpc"].installs = [Install(fake_mallard(tmp_path, "mallard-x", version=99))]
    with pytest.raises(RelocateError, match="version"):
        relocator.move(record, "hpc")
    assert project.run("input-1").home["remote"] == "local"
    assert not (tmp_path / "hpc" / "runs" / "input-1" / "solut").exists()  # the snapshots never moved


def test_session_moves_a_run_and_reads_it_at_its_new_home(relocator: Relocator, tmp_path: Path) -> None:
    from mallard_gui.mallard import Mallard
    from mallard_gui.session import RestartError, Session

    project = relocator.data.project
    record = project.run("input-1")
    record.outcome = "stopped"
    project.save_run(record)
    session = Session(project.root, Mallard(relocator.local_mallard))
    session._data = relocator.data
    moved, check, old = session.move_run("input-1", "hpc")
    assert moved.home["remote"] == "hpc" and check is not None and old
    assert session.open_run(project.run("input-1")).run_id == "r1"  # its log, read over ssh at the new home
    run, check = session.continue_elsewhere("input-1", "local")
    assert check.ok and run.record.home == {"remote": "local", "directory": f"runs/{run.record.name}"}
    assert run.job.backend == "local" and run.backend is None
    session.backend.cancel(run.job)
    with pytest.raises(RestartError):
        session.move_run("input-1", "nowhere")
