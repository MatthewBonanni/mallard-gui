from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from mallard_gui.config import Install, Remote, RemoteStore, config_dir
from mallard_gui.project import Project, RunRecord, sha256_file


def test_project_created_with_detected_inputs(cylinder_project: Path) -> None:
    (cylinder_project / "notes.toml").write_text('title = "not an input"\n')
    project = Project.open(cylinder_project)
    assert project.inputs == ["input.toml"]
    data = tomllib.loads((cylinder_project / "mallard-project.toml").read_text())
    assert (data["schema"], data["version"]) == ("mallard-project", 1)
    assert list(data)[:2] == ["schema", "version"]
    assert Project.open(cylinder_project).inputs == ["input.toml"]


def test_project_open_without_create_writes_nothing(cylinder_project: Path) -> None:
    assert Project.open(cylinder_project, create=False).inputs == ["input.toml"]
    assert not (cylinder_project / "mallard-project.toml").exists()


def test_project_rejects_other_schema_and_newer_version(tmp_path: Path) -> None:
    (tmp_path / "mallard-project.toml").write_text('schema = "other"\nversion = 1\n')
    with pytest.raises(ValueError, match="schema"):
        Project.open(tmp_path)
    (tmp_path / "mallard-project.toml").write_text('schema = "mallard-project"\nversion = 9\n')
    with pytest.raises(ValueError, match="newer"):
        Project.open(tmp_path)


def test_run_records_round_trip_and_names(cylinder_project: Path) -> None:
    project = Project.open(cylinder_project)
    assert project.new_run_name("input") == "input-1"
    record = RunRecord(
        name="input-1",
        home={"remote": "local", "directory": "runs/input-1"},
        input="input.toml",
        input_sha256=sha256_file(cylinder_project / "input.toml"),
        mallard="0.10.0",
        job="123",
        outcome="stopped",
        last_restart={"path": "restart/r.h5", "sha256": "ab"},
    )
    path = project.save_run(record)
    assert path == cylinder_project / "mallard-runs" / "input-1.toml"
    assert project.run("input-1") == record
    assert [r.name for r in project.runs()] == ["input-1"]
    assert project.new_run_name("input") == "input-2"
    data = tomllib.loads(path.read_text())
    assert data["schema"] == "mallard-run-record"
    assert "t_stop" not in path.read_text()


def test_run_record_rejects_path_names() -> None:
    with pytest.raises(ValueError):
        RunRecord(name="../x", home={}, input="input.toml", input_sha256="", mallard="")


def test_remotes_round_trip(tmp_path: Path) -> None:
    store = RemoteStore(tmp_path / "remotes.toml")
    remote = Remote(
        "cluster",
        "hpc",
        installs=[Install("/opt/mallard/bin/mallard", module="module load mallard")],
        defaults={"partition": "gpu"},
    )
    store.add(remote)
    with pytest.raises(ValueError):
        store.add(Remote("cluster", "other"))
    store.save()
    again = RemoteStore(tmp_path / "remotes.toml")
    assert again.remotes["cluster"] == remote
    assert again.by_id(remote.id) == remote
    text = (tmp_path / "remotes.toml").read_text()
    assert text.index("schema") < text.index("[remotes")


def test_remote_validation_and_newer_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Remote("x", "host", backend="nope")
    with pytest.raises(ValueError):
        Remote("x", "", backend="slurm")
    (tmp_path / "remotes.toml").write_text('schema = "mallard-gui-remotes"\nversion = 2\n')
    with pytest.raises(ValueError):
        RemoteStore(tmp_path / "remotes.toml")


def test_config_dir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MALLARD_GUI_CONFIG_DIR", str(tmp_path))
    assert config_dir() == tmp_path
