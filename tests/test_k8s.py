from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from mallard_gui.backends.base import Job, JobState, RunSpec
from mallard_gui.backends.k8s import (
    HelperPod,
    K8sBackend,
    KubeConnection,
    dns_name,
    job_state,
    node_selector,
    probe_k8s,
    seconds_of,
)
from mallard_gui.config import Install, Remote, RemoteStore
from mallard_gui.data.cache import CacheIndex
from mallard_gui.data.locations import ShellLocation
from mallard_gui.data.manager import DataManager
from mallard_gui.project import Project, RunRecord

from .conftest import FIXTURES
from .runs import make_run

FAKE_KUBECTL = str(FIXTURES / "fake-kubectl")


@pytest.fixture
def kube(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The fake kubectl's state directory; the volume is a local directory, its mount path a local path."""
    monkeypatch.setenv("MALLARD_GUI_KUBECTL", FAKE_KUBECTL)
    monkeypatch.setenv("FAKE_KUBE_STATE", str(tmp_path / "kube"))
    monkeypatch.setenv("FAKE_KUBE_LOG", str(tmp_path / "kube.log"))
    (tmp_path / "vol").mkdir()
    return tmp_path / "kube"


def k8s_remote(tmp_path: Path, **k8s: str) -> Remote:
    return Remote(
        "kube",
        "ctx",
        backend="k8s",
        installs=[Install("ghcr.io/matthewbonanni/mallard:0.10.0-cpu-amd64")],
        defaults={"memory": "1Gi"},
        storage={"run_root": str(tmp_path / "vol" / "runs")},
        k8s={
            "namespace": "ns",
            "pvc": "data",
            "mount": str(tmp_path / "vol"),
            "prefix": "gui-test",
            "node_selector": "nvidia.com/gpu.product=NVIDIA-A100-SXM4-80GB",
            "run_as_user": "1000",
            **k8s,
        },
    )


def applied(kube: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(kube.glob("applied-*.json"))]


def test_names_selectors_and_times() -> None:
    assert dns_name("Gui", "run", "Cyl_1.fine") == "gui-run-cyl-1-fine"
    long = dns_name("p", "x" * 80)
    assert len(long) == 63 and long != dns_name("p", "x" * 81)
    assert node_selector("a=b, c = d") == {"a": "b", "c": "d"}
    with pytest.raises(ValueError):
        node_selector("a")
    assert seconds_of("01:30:00") == 5400 and seconds_of("1-00:00:10") == 86410 and seconds_of("") is None


def test_settings_need_namespace_and_pvc(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"k8s\.pvc"):
        K8sBackend(Remote("k", "ctx", backend="k8s", installs=[Install("img")], k8s={"namespace": "ns"}))
    with pytest.raises(ValueError, match="image"):
        K8sBackend(Remote("k", "ctx", backend="k8s", k8s={"namespace": "ns", "pvc": "p"}))
    with pytest.raises(ValueError):
        KubeConnection("-oops", "ns")


def test_job_manifest(tmp_path: Path, kube: Path) -> None:
    b = K8sBackend(k8s_remote(tmp_path))
    spec = RunSpec("cyl 1", Path("in.toml"), "/data/runs/cyl", threads=4, restart="/data/runs/a/r.restart")
    spec.options = {"time": "00:10:00", "gpu": "1"}
    m = b.job_manifest(spec, "gui-test-cyl")
    pod = m["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert m["spec"]["backoffLimit"] == 0 and m["spec"]["activeDeadlineSeconds"] == 600
    assert c["command"] == [
        "mallard",
        "-i",
        "input.toml",
        "--kokkos-num-threads=4",
        "--restart",
        "/data/runs/a/r.restart",
    ]
    assert c["workingDir"] == "/data/runs/cyl"
    assert c["resources"]["requests"] == {"cpu": "4", "memory": "1Gi", "nvidia.com/gpu": "1"}
    assert c["securityContext"]["runAsUser"] == 1000 and c["securityContext"]["allowPrivilegeEscalation"] is False
    assert pod["nodeSelector"] == {"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"}
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "data"
    assert c["volumeMounts"][0]["mountPath"] == str(tmp_path / "vol")


def test_submit_writes_input_and_job_and_applies(tmp_path: Path, kube: Path, cylinder_project: Path) -> None:
    b = K8sBackend(k8s_remote(tmp_path))
    run_dir = tmp_path / "vol" / "runs" / "input-1"
    job = b.submit(RunSpec("input-1", cylinder_project / "input.toml", str(run_dir)))
    assert job.backend == "k8s" and job.id.startswith("gui-test-input-1-")
    assert tomllib.loads((run_dir / "input.toml").read_text())["run"]["control_file_interval"] == 10
    written = json.loads((run_dir / "job.json").read_text())
    assert applied(kube)[-1] == written and written["metadata"]["name"] == job.id
    with pytest.raises(ValueError, match="not under"):
        b.submit(RunSpec("x", cylinder_project / "input.toml", "/elsewhere/x"))


def test_read_control_cancel_and_logs(tmp_path: Path, kube: Path) -> None:
    b = K8sBackend(k8s_remote(tmp_path))
    run = tmp_path / "vol" / "runs" / "r"
    run.mkdir(parents=True)
    (run / "mallard.jsonl").write_bytes(b'{"type":"start"}\n')
    job = Job("gui-test-r-1234", str(run), "k8s")
    assert b.read_log(job, 0) == b'{"type":"start"}\n' and b.read_log(job, 1) == b'"type":"start"}\n'
    assert b.read_file(job, "missing.csv", 0) == b""
    b.stop(job)
    assert (run / "STOP").exists()
    b.cancel(job)
    assert "delete job gui-test-r-1234" in (tmp_path / "kube.log").read_text()
    assert "fake log" in b.logs(job)


@pytest.mark.parametrize(
    ("status", "pods", "state"),
    [
        ({"conditions": [{"type": "Complete", "status": "True"}]}, [], JobState.COMPLETED),
        ({"conditions": [{"type": "Failed", "status": "True"}]}, [], JobState.FAILED),
        ({"active": 1}, [{"status": {"phase": "Running"}}], JobState.RUNNING),
        ({"active": 1}, [{"status": {"phase": "Pending"}}], JobState.PENDING),
        ({}, [], JobState.PENDING),
    ],
)
def test_job_state(status: dict[str, Any], pods: list[dict[str, Any]], state: JobState) -> None:
    assert job_state({"status": status}, pods) == state


def test_state_and_queue_from_kubectl(tmp_path: Path, kube: Path) -> None:
    b = K8sBackend(k8s_remote(tmp_path))
    job = Job("gui-test-r-1", "/x", "k8s")
    assert b.state(job) == JobState.UNKNOWN
    kube.mkdir(exist_ok=True)
    item = {
        "metadata": {"name": "gui-test-r-1", "annotations": {"mallard-gui/run-name": "r"}},
        "status": {"succeeded": 1, "startTime": "2026-11-20T10:00:00Z", "completionTime": "2026-11-21T10:01:05Z"},
    }
    (kube / "job-gui-test-r-1.json").write_text(json.dumps(item))
    (kube / "jobs.json").write_text(json.dumps({"items": [item]}))
    assert b.state(job) == JobState.COMPLETED
    assert b.queue() == [{"id": "gui-test-r-1", "name": "r", "state": "COMPLETED", "time": "1-0:01:05", "reason": ""}]


def test_helper_pod_is_created_when_missing_and_replaced_when_gone(tmp_path: Path, kube: Path) -> None:
    remote = k8s_remote(tmp_path)
    helper = HelperPod(remote, sleep=lambda s: None)
    kube.mkdir(exist_ok=True)
    (kube / f"gone-{helper.name}").touch()
    assert helper.run(["echo", "hi"]).text == "hi\n"
    m = applied(kube)[-1]
    assert m["metadata"]["name"] == "gui-test-helper-kube" and m["spec"]["activeDeadlineSeconds"] == 14400
    assert m["spec"]["containers"][0]["command"][-1] == "120"
    # It exits idle between two commands: the next command starts another.
    (kube / f"gone-{helper.name}").touch()
    assert helper.run(["echo", "again"]).text == "again\n"
    assert len(applied(kube)) == 2
    helper.close()
    assert (kube / f"gone-{helper.name}").exists()


def test_probe(tmp_path: Path, kube: Path) -> None:
    p = probe_k8s(k8s_remote(tmp_path))
    assert p["ok"] and p["can"]["port-forward"] and p["gpus"] == {"NVIDIA-A100-SXM4-80GB": 1}


def test_data_manager_moves_a_run_onto_the_volume_and_back(tmp_path: Path, kube: Path) -> None:
    project_dir = tmp_path / "proj"
    make_run(project_dir / "runs" / "box-1", steps=3)
    project = Project.open(project_dir)
    record = RunRecord("box-1", home={"remote": "local", "directory": "runs/box-1"}, outcome="finished")
    project.save_run(record)
    store = RemoteStore(tmp_path / "remotes.toml")
    store.add(k8s_remote(tmp_path))
    dm = DataManager(project, store, CacheIndex(tmp_path / "index.sqlite"))
    assert isinstance(dm.location("kube"), ShellLocation) and "kube" in dm.location_names()
    plan = dm.plan(record, dm.select(record, "run"), "kube")
    assert plan.route == "stream" and plan.moving
    dm.start(plan)
    dm.wait(60)
    assert dm.transfer.report is not None and not dm.transfer.report.errors
    copied = tmp_path / "vol" / "runs" / "box-1"
    local = project_dir / "runs" / "box-1"
    assert (copied / "mallard.jsonl").read_bytes() == (local / "mallard.jsonl").read_bytes()
    again = dm.plan(record, dm.select(record, "run"), "kube")
    assert not again.moving
