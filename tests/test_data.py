"""Content addressing, the cache index and the transfer engine, against a local shim of ssh (no server)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from mallard_gui.backends.ssh import SshConnection
from mallard_gui.config import Remote, RemoteStore
from mallard_gui.data.cache import CacheIndex, Copy
from mallard_gui.data.locations import LocalLocation, SshLocation
from mallard_gui.data.manager import ABSENT, HOME, PARTIAL_STATE, PREFIX, PRESENT, DataManager
from mallard_gui.data.manifest import LIVE, OBJECT, PARTIAL, STREAM, build_manifest
from mallard_gui.data.transfer import Endpoint, TransferEngine
from mallard_gui.project import Project, RunRecord

FAKE_SSH = str(Path(__file__).parent / "fixtures" / "fake-ssh")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_run(root: Path, ended: bool = True, snapshot: bytes = b"S" * 70_000) -> dict[str, bytes]:
    """A run directory as Mallard leaves it: objects recorded with their SHA-256, streams, derived files."""
    files = {
        "solut/cyl_000000.vtu": snapshot,
        "solut/cyl_000010.vtu": b"T" * 5000,
        "restart_000010.restart": b"R" * 9000,
        "solut/series.vtkhdf": b"H" * 300,
        "solut/forces_cyl.csv": b"step,t,Fx\n# units: 1,s,N\n0,0,1.0\n10,0.1,1.5\n",
        "solut/cyl.pvd": b"<VTKFile/>",
        "input.toml": b"[run]\nt_stop = 1.0\n",
        "restart_000020.restart.partial": b"r" * 10,
        "mallard-gui.pid": b"123",
    }
    records: list[dict[str, object]] = [{"type": "start", "run_id": "r1"}]
    for step, rel in ((0, "solut/cyl_000000.vtu"), (10, "solut/cyl_000010.vtu"), (10, "restart_000010.restart")):
        kind = "restart" if rel.endswith(".restart") else "vtu"
        records.append(_output(step, rel, files[rel], kind))
    records.append(_output(0, "solut/series.vtkhdf", b"old", "vtkhdf"))
    records.append(_output(10, "solut/series.vtkhdf", files["solut/series.vtkhdf"], "vtkhdf"))
    if ended:
        records.append({"type": "end", "outcome": "stopped"})
    files["mallard.jsonl"] = "".join(json.dumps(r) + "\n" for r in records).encode()
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return files


def _output(step: int, rel: str, data: bytes, kind: str) -> dict[str, object]:
    return {
        "type": "output",
        "step": step,
        "t": step / 100,
        "kind": kind,
        "path": f"./{rel}",
        "writer": "./solut/cyl",
        "complete": True,
        "files": [{"path": f"./{rel}", "bytes": len(data), "sha256": sha(data)}],
    }


@pytest.fixture
def shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`ssh` on PATH is the shim, so a remote's own `ssh` (the direct route) reaches the other remote."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").symlink_to(FAKE_SSH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return log


def ssh_location(name: str, tmp_path: Path) -> SshLocation:
    conn = SshConnection(name, control_dir=tmp_path / "ctl", ssh=FAKE_SSH)
    return SshLocation(name, f"id-{name}", conn)


@pytest.fixture
def cache(tmp_path: Path) -> CacheIndex:
    return CacheIndex(tmp_path / "cache" / "index.sqlite")


# Manifest


@pytest.mark.parametrize("where", ["local", "ssh"])
def test_manifest_classifies_and_hashes(tmp_path: Path, shim: Path, where: str) -> None:
    run = tmp_path / "run"
    files = write_run(run)
    loc = LocalLocation() if where == "local" else ssh_location("hpc", tmp_path)
    m = build_manifest(loc, str(run))
    assert m.ended and m.run_id == "r1"
    kinds = {p: (e.kind, e.category) for p, e in m.files.items()}
    assert kinds["solut/cyl_000000.vtu"] == (OBJECT, "snapshot")
    assert kinds["restart_000010.restart"] == (OBJECT, "restart")
    assert kinds["solut/series.vtkhdf"] == (OBJECT, "series")  # final once its run ended
    assert kinds["solut/forces_cyl.csv"] == (STREAM, "probe")
    assert kinds["mallard.jsonl"] == (STREAM, "log")
    assert kinds["input.toml"] == (OBJECT, "input")
    assert kinds["restart_000020.restart.partial"][0] == PARTIAL
    assert "mallard-gui.pid" not in m.files
    for rel in ("solut/cyl_000000.vtu", "solut/forces_cyl.csv", "input.toml", "solut/cyl.pvd"):
        assert m.files[rel].sha256 == sha(files[rel])
    assert m.files["solut/cyl_000000.vtu"].recorded and not m.files["input.toml"].recorded
    assert m.files["solut/cyl_000010.vtu"].step == 10


def test_manifest_series_is_live_until_the_run_ends_and_a_changed_file_is_rehashed(tmp_path: Path) -> None:
    run = tmp_path / "run"
    write_run(run, ended=False)
    (run / "solut/cyl_000010.vtu").write_bytes(b"rewound")  # a rewind reused the path
    m = build_manifest(LocalLocation(), str(run))
    assert m.files["solut/series.vtkhdf"].kind == LIVE
    e = m.files["solut/cyl_000010.vtu"]
    assert (e.recorded, e.sha256, e.category) == (False, sha(b"rewound"), "other")


# Cache index


def test_cache_records_manifests_and_drops_contradicted_entries(tmp_path: Path, cache: CacheIndex) -> None:
    run = tmp_path / "run"
    files = write_run(run)
    m = build_manifest(LocalLocation(), str(run))
    cache.record_copy(Copy("local", str(run / "gone.vtu"), OBJECT, 3, "abc"))
    cache.record_copy(Copy("local", str(tmp_path / "other" / "x"), OBJECT, 3, "abc"))
    cache.record_manifest(m)
    under = cache.copies_under("local", str(run))
    assert under["solut/cyl_000000.vtu"].sha256 == sha(files["solut/cyl_000000.vtu"])
    assert "gone.vtu" not in under  # the location contradicts it
    assert cache.holders("abc")[0].path == str(tmp_path / "other" / "x")  # outside the root: kept
    assert cache.throughput("local>hpc:stream") is None
    cache.measured("local>hpc:stream", 1000, 2.0)
    cache.measured("local>hpc:stream", 3000, 2.0)
    assert cache.throughput("local>hpc:stream") == 1000.0


def test_cache_of_another_version_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite"
    c = CacheIndex(path)
    c.record_copy(Copy("local", "/a", OBJECT, 1, "h"))
    c._db.execute("UPDATE meta SET value = '0' WHERE key = 'version'")
    c.close()
    assert CacheIndex(path).copy_at("local", "/a") is None


# Transfers


def engine_run(cache: CacheIndex, src: Endpoint, dst: Endpoint, route: str, block: int = 1 << 20) -> TransferEngine:
    e = TransferEngine(cache, block=block)
    m = build_manifest(src.location, src.root)
    cache.record_manifest(m)
    plan = e.plan([x for x in m.entries() if x.kind != PARTIAL], [src], dst, route)
    report = e.run(plan)
    assert not report.errors and not report.cancelled
    return e


def same_files(a: Path, b: Path, rels: list[str]) -> None:
    for rel in rels:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def test_local_to_remote_then_everything_is_present(tmp_path: Path, shim: Path, cache: CacheIndex) -> None:
    run = tmp_path / "run"
    files = write_run(run)
    hpc = ssh_location("hpc", tmp_path)
    src, dst = Endpoint(LocalLocation(), str(run)), Endpoint(hpc, str(tmp_path / "hpc" / "run"))
    e = engine_run(cache, src, dst, "stream")
    moved = [r for r in files if not r.endswith((".partial", ".pid"))]
    same_files(run, tmp_path / "hpc" / "run", moved)
    assert not list((tmp_path / "hpc" / "run").rglob("*.partial"))
    assert cache.copy_at("id-hpc", dst.path("solut/cyl_000000.vtu")) is not None
    plan = e.plan(list(build_manifest(src.location, src.root).files.values()), [src], dst, "stream")
    assert {i.skip for i in plan.items} <= {"present", "unfinished write at the source"}
    assert plan.bytes == 0
    assert plan.throughput is not None and plan.seconds == 0


def test_remote_to_local(tmp_path: Path, shim: Path, cache: CacheIndex) -> None:
    run = tmp_path / "hpc" / "run"
    write_run(run)
    src, dst = Endpoint(ssh_location("hpc", tmp_path), str(run)), Endpoint(LocalLocation(), str(tmp_path / "copy"))
    engine_run(cache, src, dst, "stream")
    same_files(run, tmp_path / "copy", ["solut/cyl_000000.vtu", "mallard.jsonl", "restart_000010.restart"])


@pytest.mark.parametrize("route", ["funnel", "direct", "pull", "shared"])
def test_remote_to_remote(tmp_path: Path, shim: Path, cache: CacheIndex, route: str) -> None:
    run = tmp_path / "a" / "run"
    write_run(run)
    a, b = ssh_location("hpc-a", tmp_path), ssh_location("hpc-b", tmp_path)
    engine_run(cache, Endpoint(a, str(run)), Endpoint(b, str(tmp_path / "b" / "run")), route)
    same_files(run, tmp_path / "b" / "run", ["solut/cyl_000000.vtu", "solut/forces_cyl.csv"])
    lines = shim.read_text().splitlines()
    if route == "direct":
        # The source pipes into the destination itself: hpc-b is reached from hpc-a, not from here.
        assert any(line.startswith("hpc-a\t") and "ssh -o BatchMode=yes hpc-b" in line for line in lines)
    elif route == "pull":
        assert any(line.startswith("hpc-b\t") and "ssh -o BatchMode=yes hpc-a" in line for line in lines)
    elif route == "shared":
        # A copy on the destination from the source's path, which it sees: no ssh between the remotes.
        assert any(line.startswith("hpc-b\t") and "tail -c" in line for line in lines)
        assert not any("ssh -o BatchMode=yes" in line for line in lines)
    else:
        assert any(line.startswith("hpc-b\t") and "cat >>" in line for line in lines)


def test_direct_route_probe(tmp_path: Path, shim: Path) -> None:
    a = ssh_location("hpc-a", tmp_path)
    assert a.can_reach("hpc-b")
    assert not a.can_reach("unreachable-c")


def test_interrupted_transfer_resumes_from_its_partial(tmp_path: Path, shim: Path, cache: CacheIndex) -> None:
    run = tmp_path / "run"
    files = write_run(run)
    src, dst = Endpoint(LocalLocation(), str(run)), Endpoint(ssh_location("hpc", tmp_path), str(tmp_path / "hpc"))
    e = TransferEngine(cache, block=4096)
    snap = build_manifest(src.location, src.root).files["solut/cyl_000000.vtu"]
    cancel = threading.Event()

    def stop_after_some(done: int, total: int, current: str) -> None:
        if done >= 3 * 4096:
            cancel.set()

    report = e.run(e.plan([snap], [src], dst, "stream"), stop_after_some, cancel)
    assert report.cancelled and not report.moved
    partial = tmp_path / "hpc" / "solut" / "cyl_000000.vtu.partial"
    have = partial.stat().st_size
    assert 0 < have < snap.size
    assert files["solut/cyl_000000.vtu"].startswith(partial.read_bytes())
    pending = cache.pending("id-hpc")
    assert pending and pending[0].sha256 == snap.sha256
    plan = e.plan([snap], [src], dst, "stream")
    assert plan.items[0].offset == have and plan.bytes == snap.size - have
    report = e.run(plan)
    assert report.moved == ["solut/cyl_000000.vtu"] and report.bytes == snap.size - have
    assert (tmp_path / "hpc" / "solut" / "cyl_000000.vtu").read_bytes() == files["solut/cyl_000000.vtu"]
    assert not partial.exists() and not cache.pending("id-hpc")


def test_a_partial_finishes_from_another_source_holding_the_same_object(
    tmp_path: Path, shim: Path, cache: CacheIndex
) -> None:
    run, other = tmp_path / "run", tmp_path / "b" / "run"
    files = write_run(run)
    write_run(other)
    local, b = Endpoint(LocalLocation(), str(run)), Endpoint(ssh_location("hpc-b", tmp_path), str(other))
    dst = Endpoint(ssh_location("hpc-a", tmp_path), str(tmp_path / "a"))
    cache.record_manifest(build_manifest(b.location, b.root))
    data = files["solut/cyl_000000.vtu"]
    (tmp_path / "a" / "solut").mkdir(parents=True)
    (tmp_path / "a" / "solut" / "cyl_000000.vtu.partial").write_bytes(data[:1000])
    snap = build_manifest(local.location, local.root).files["solut/cyl_000000.vtu"]
    e = TransferEngine(cache)
    plan = e.plan([snap], [local, b], dst, "funnel")
    assert plan.items[0].source is b or plan.items[0].source is local
    plan = e.plan([snap], [b], dst, "funnel")
    assert plan.items[0].offset == 1000
    assert not e.run(plan).errors
    assert (tmp_path / "a" / "solut" / "cyl_000000.vtu").read_bytes() == data


def test_a_corrupt_partial_fails_verification_and_is_discarded(tmp_path: Path, shim: Path, cache: CacheIndex) -> None:
    run = tmp_path / "run"
    files = write_run(run)
    src, dst = Endpoint(LocalLocation(), str(run)), Endpoint(ssh_location("hpc", tmp_path), str(tmp_path / "hpc"))
    (tmp_path / "hpc").mkdir()
    (tmp_path / "hpc" / "restart_000010.restart.partial").write_bytes(b"X" * 100)
    e = TransferEngine(cache)
    restart = build_manifest(src.location, src.root).files["restart_000010.restart"]
    report = e.run(e.plan([restart], [src], dst, "stream"))
    assert report.errors and "SHA-256" in report.errors[0]
    assert not (tmp_path / "hpc" / "restart_000010.restart").exists()
    assert not (tmp_path / "hpc" / "restart_000010.restart.partial").exists()
    assert not e.run(e.plan([restart], [src], dst, "stream")).errors
    assert (tmp_path / "hpc" / "restart_000010.restart").read_bytes() == files["restart_000010.restart"]


def test_streams_append_their_missing_bytes_or_are_replaced(tmp_path: Path, shim: Path, cache: CacheIndex) -> None:
    run = tmp_path / "run"
    files = write_run(run)
    src, dst = Endpoint(LocalLocation(), str(run)), Endpoint(ssh_location("hpc", tmp_path), str(tmp_path / "hpc"))
    rel = "solut/forces_cyl.csv"
    copy = tmp_path / "hpc" / rel
    copy.parent.mkdir(parents=True)
    copy.write_bytes(files[rel][:20])
    with (run / rel).open("ab") as f:  # the run continued
        f.write(b"20,0.2,1.7\n")
    e = TransferEngine(cache)
    stream = build_manifest(src.location, src.root).files[rel]
    plan = e.plan([stream], [src], dst, "stream")
    assert (plan.items[0].mode, plan.items[0].offset) == ("append", 20)
    assert not e.run(plan).errors
    assert copy.read_bytes() == (run / rel).read_bytes()
    assert e.plan([stream], [src], dst, "stream").items[0].skip == "present"
    copy.write_bytes(b"step,t,Fy\n")  # a fresh start in that directory: another stream
    plan = e.plan([stream], [src], dst, "stream")
    assert (plan.items[0].mode, plan.items[0].offset) == ("object", 0)
    assert not e.run(plan).errors
    assert copy.read_bytes() == (run / rel).read_bytes()


# The data manager


@pytest.fixture
def manager(tmp_path: Path, cylinder_project: Path, cache: CacheIndex, shim: Path) -> DataManager:
    project = Project.open(cylinder_project)
    write_run(cylinder_project / "runs" / "input-1")
    project.save_run(RunRecord("input-1", home={"remote": "local", "directory": "runs/input-1"}))
    store = RemoteStore(tmp_path / "remotes.toml")
    store.add(Remote("hpc", "hpc", storage={"run_root": str(tmp_path / "hpc" / "runs")}))
    store.add(Remote("nowhere", "nowhere"))
    return DataManager(
        project, store, cache, connect=lambda r: SshConnection(r.host, control_dir=tmp_path, ssh=FAKE_SSH)
    )


def test_manager_states_selections_and_a_transfer(manager: DataManager, tmp_path: Path) -> None:
    record = manager.project.run("input-1")
    assert manager.location_names() == ["local", "hpc", "nowhere"]
    assert manager.copy_root(record, "nowhere") is None
    assert manager.copy_root(record, "hpc") == str(tmp_path / "hpc" / "runs" / "input-1")
    assert manager.refresh(record) == {}
    rows = {r["path"]: r for r in manager.states(record)}
    assert rows["input.toml"]["at"]["local"]["state"] == HOME
    assert rows["input.toml"]["at"]["hpc"]["state"] == ABSENT
    assert rows["input.toml"]["at"]["nowhere"]["state"] == ""
    assert [e.path for e in manager.select(record, "snapshot")] == ["solut/cyl_000010.vtu", "solut/series.vtkhdf"]
    assert [e.path for e in manager.select(record, "snapshots", t_min=0, t_max=0.05)] == ["solut/cyl_000000.vtu"]
    assert [e.path for e in manager.select(record, "restart")] == ["restart_000010.restart"]
    assert {e.path for e in manager.select(record, "probes")} == {"mallard.jsonl", "solut/forces_cyl.csv"}
    assert [e.path for e in manager.select(record, "inputs")] == ["input.toml"]
    with pytest.raises(ValueError):
        manager.plan(record, manager.select(record, "inputs"), "nowhere")

    plan = manager.plan(record, manager.select(record, "probes") + manager.select(record, "inputs"), "hpc")
    assert plan.route == "stream" and len(plan.moving) == 3
    manager.start(plan)
    manager.wait(30)
    assert manager.transfer.report is not None and not manager.transfer.report.errors
    assert manager.transfer.done == manager.transfer.total == plan.bytes
    rows = {r["path"]: r for r in manager.states(record)}
    assert rows["input.toml"]["at"]["hpc"]["state"] == PRESENT
    assert rows["restart_000010.restart"]["at"]["hpc"]["state"] == ABSENT
    # The run goes on: the copy of its log is now a prefix.
    with (manager.project.root / "runs" / "input-1" / "mallard.jsonl").open("ab") as f:
        f.write(b'{"type": "progress", "t": 0.2}\n')
    manager.refresh(record)
    assert {r["path"]: r for r in manager.states(record)}["mallard.jsonl"]["at"]["hpc"]["state"] == PREFIX


def test_manager_shows_a_partial_copy(manager: DataManager, tmp_path: Path) -> None:
    record = manager.project.run("input-1")
    manager.refresh(record)
    snap = manager.select(record, "snapshots", t_min=0, t_max=0)
    plan = manager.plan(record, snap, "hpc")
    manager.engine.block = 4096
    cancel = threading.Event()
    manager.engine.run(plan, lambda d, t, c: cancel.set() if d > 8192 else None, cancel)
    row = {r["path"]: r for r in manager.states(record)}["solut/cyl_000000.vtu"]
    assert row["at"]["hpc"]["state"] == PARTIAL_STATE and 0 < row["at"]["hpc"]["have"] < row["size"]


def test_manager_finds_a_renamed_remote_by_id(manager: DataManager) -> None:
    rid = manager.remotes.remotes["hpc"].id
    record = RunRecord("r", home={"remote": "old-name", "remote_id": rid, "directory": "/scratch/r"})
    assert manager.home(record).location.name == "hpc"
    with pytest.raises(KeyError, match="not one of your remotes"):
        manager.home(RunRecord("r", home={"remote": "elsewhere", "remote_id": "x", "directory": "/d"}))


def test_route_probe_results_are_kept_in_remotes_toml(manager: DataManager, tmp_path: Path) -> None:
    manager.remotes.add(Remote("hpc2", "hpc2", storage={"run_root": str(tmp_path / "hpc2")}))
    found = manager.probe_routes("hpc", "hpc2")
    # The fake ssh runs every host on this machine: each reaches the other, and sees its storage.
    assert found["hpc"]["ssh"] and found["hpc2"]["ssh"] and found["hpc2"]["sees"]
    assert not list((tmp_path / "hpc2").glob(".mallard-gui-probe-*"))  # the marker is cleaned up
    again = RemoteStore(tmp_path / "remotes.toml")
    entry = again.remotes["hpc"].reach[again.remotes["hpc2"].id]
    assert entry["ssh"] is True and entry["host"] == "hpc2" and entry["checked"]
    assert manager.choose_route("hpc", "hpc2") == "shared"
    manager.remotes.remotes["hpc2"].reach[manager.remotes.remotes["hpc"].id]["sees"] = False
    assert manager.choose_route("hpc", "hpc2") == "direct"
    manager.remotes.remotes["hpc"].reach[manager.remotes.remotes["hpc2"].id]["ssh"] = False
    assert manager.choose_route("hpc", "hpc2") == "pull"
    manager.remotes.remotes["hpc2"].reach[manager.remotes.remotes["hpc"].id]["ssh"] = False
    assert manager.choose_route("hpc", "hpc2") == "funnel"
    assert manager.choose_route("local", "hpc") == "stream"


def test_choose_route_probes_two_remotes_the_first_time(manager: DataManager, tmp_path: Path) -> None:
    manager.remotes.add(Remote("hpc2", "hpc2"))
    assert manager.reach("hpc", "hpc2") == {}
    assert manager.choose_route("hpc", "hpc2") == "shared"  # hpc2 sees hpc's storage root
    assert manager.reach("hpc2", "hpc")["ssh"] is True
    assert manager.choose_route("hpc2", "hpc") == "direct"  # hpc2 has no storage root for hpc to see
