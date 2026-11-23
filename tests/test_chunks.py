"""Objects recorded by their chunk tree (#1329): parallel chunk transfers, verified and resumed per chunk."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mallard_gui.data.cache import CacheIndex
from mallard_gui.data.chunks import ChunkTree, copy_chunks, tree_at, tree_of_file, tree_root
from mallard_gui.data.locations import LocalLocation, TransferError
from mallard_gui.data.manifest import OBJECT, build_manifest
from mallard_gui.data.transfer import Endpoint, TransferEngine

from .test_data import sha, ssh_location


@pytest.fixture
def ssh_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    return tmp_path / "ssh.log"


@pytest.fixture
def index(tmp_path: Path) -> CacheIndex:
    return CacheIndex(tmp_path / "cache" / "index.sqlite")


CHUNK = 4096
BIG = "solut/big_000010.vtu"


def write_chunked_run(root: Path) -> bytes:
    """A run whose one snapshot Mallard recorded by its chunk tree, as a file over one chunk is."""
    data = os.urandom(CHUNK * 9 + 123)
    (root / "solut").mkdir(parents=True)
    (root / BIG).write_bytes(data)
    tree = tree_of_file(root / BIG, CHUNK)
    record = {
        "type": "output",
        "step": 10,
        "t": 0.1,
        "kind": "vtu",
        "path": f"./{BIG}",
        "writer": "./solut/big",
        "files": [
            {
                "path": f"./{BIG}",
                "bytes": len(data),
                "sha256_tree": tree.root,
                "chunk_bytes": CHUNK,
                "chunks": tree.chunks,
            }
        ],
    }
    lines = [{"type": "start", "run_id": "r1"}, record, {"type": "end", "outcome": "stopped"}]
    (root / "mallard.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    return data


def test_tree_root_hashes_the_concatenated_digests(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"a" * 10 + b"b" * 10 + b"c" * 3)
    tree = tree_of_file(tmp_path / "f", 10)
    assert tree.chunks == [sha(b"a" * 10), sha(b"b" * 10), sha(b"c" * 3)]
    assert tree.root == sha(b"".join(bytes.fromhex(c) for c in tree.chunks)) == tree_root(tree.chunks)
    assert tree.spans(23) == [(0, 0, 10), (1, 10, 10), (2, 20, 3)]


@pytest.mark.parametrize("where", ["local", "ssh"])
def test_manifest_takes_the_tree_and_the_root_is_found_where_the_file_is(
    tmp_path: Path,
    ssh_log: Path,
    where: str,
) -> None:
    run = tmp_path / "run"
    write_chunked_run(run)
    loc = LocalLocation() if where == "local" else ssh_location("hpc", tmp_path)
    e = build_manifest(loc, str(run)).files[BIG]
    tree = tree_of_file(run / BIG, CHUNK)
    assert (e.kind, e.recorded, e.sha256, e.chunk_bytes, e.chunks) == (
        OBJECT,
        True,
        f"tree:{tree.root}",
        CHUNK,
        tree.chunks,
    )
    assert tree_at(loc, str(run / BIG), CHUNK) == tree.root
    assert tree_at(loc, str(run / "missing"), CHUNK) is None


@pytest.mark.parametrize("direction", ["up", "down"])
def test_chunked_object_moves_in_parallel_then_is_present(
    tmp_path: Path,
    ssh_log: Path,
    index: CacheIndex,
    direction: str,
) -> None:
    run = tmp_path / "run"
    data = write_chunked_run(run)
    hpc = ssh_location("hpc", tmp_path)
    here, there = Endpoint(LocalLocation(), str(run)), Endpoint(hpc, str(tmp_path / "hpc"))
    src, dst = (
        (here, there)
        if direction == "up"
        else (Endpoint(hpc, str(run)), Endpoint(LocalLocation(), str(tmp_path / "copy")))
    )
    engine = TransferEngine(index, streams=4)
    entry = build_manifest(src.location, src.root).files[BIG]
    report = engine.run(engine.plan([entry], [src], dst, "stream"))
    assert not report.errors and report.moved == [BIG] and report.bytes == len(data)
    landed = Path(dst.path(BIG))
    assert landed.read_bytes() == data and not Path(dst.path(BIG) + ".partial").exists()
    if direction == "up":
        assert sum("dd of=" in line for line in ssh_log.read_text().splitlines()) == 10  # one write per chunk
    fresh = TransferEngine(CacheIndex(tmp_path / "other.sqlite"))
    assert fresh.plan([entry], [src], dst, "stream").items[0].skip == "present"  # by its chunks, not the index


def test_a_partial_keeps_its_good_chunks_wherever_they_are(tmp_path: Path, ssh_log: Path, index: CacheIndex) -> None:
    run = tmp_path / "run"
    data = write_chunked_run(run)
    src, dst = Endpoint(LocalLocation(), str(run)), Endpoint(ssh_location("hpc", tmp_path), str(tmp_path / "hpc"))
    partial = Path(dst.path(BIG) + ".partial")
    partial.parent.mkdir(parents=True)
    torn = bytearray(data)
    torn[0:CHUNK] = b"\0" * CHUNK  # chunk 0 wrong, 1..5 right, the rest not written
    partial.write_bytes(bytes(torn[: 6 * CHUNK]))
    engine = TransferEngine(index, streams=3)
    entry = build_manifest(src.location, src.root).files[BIG]
    plan = engine.plan([entry], [src], dst, "stream")
    assert plan.items[0].offset == 5 * CHUNK
    report = engine.run(plan)
    assert not report.errors and report.bytes == len(data) - 5 * CHUNK
    assert Path(dst.path(BIG)).read_bytes() == data


def test_a_chunk_list_that_does_not_make_the_tree_is_refused(tmp_path: Path) -> None:
    run = tmp_path / "run"
    data = write_chunked_run(run)
    tree = tree_of_file(run / BIG, CHUNK)
    loc = LocalLocation()
    bad = ChunkTree(CHUNK, [tree.chunks[1], tree.chunks[0], *tree.chunks[2:]], tree.root)
    with pytest.raises(TransferError, match="does not hash"):
        copy_chunks(loc, str(run / BIG), loc, str(tmp_path / "out"), len(data), bad)
    lying = tree_of_file(run / BIG, CHUNK)
    lying.chunks[3] = sha(b"other")
    lying.root = tree_root(lying.chunks)
    with pytest.raises(TransferError, match="chunk 3"):
        copy_chunks(loc, str(run / BIG), loc, str(tmp_path / "out"), len(data), lying)
