"""Manifests of runs of many ranks and steps: the run log read incrementally, rank pieces grouped."""

from __future__ import annotations

import json
from pathlib import Path

from mallard_gui.data.locations import LocalLocation
from mallard_gui.data.manifest import build_manifest, groups

from .test_data import sha


class CountingLocation(LocalLocation):
    def __init__(self) -> None:
        super().__init__()
        self.read = 0

    def read_bytes(self, path: str, offset: int = 0) -> bytes:
        data = super().read_bytes(path, offset)
        self.read += len(data)
        return data


def write_step(root: Path, step: int, ranks: int) -> None:
    files = []
    for r in range(ranks):
        rel = f"solut/s_{step:06d}/s_{step:06d}_{r}.vtu"
        data = f"{step}-{r}".encode()
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
        files.append({"path": f"./{rel}", "bytes": len(data), "sha256": sha(data)})
    record = {"type": "output", "step": step, "kind": "vtu", "path": f"./solut/s_{step:06d}.pvtu", "files": files}
    with (root / "mallard.jsonl").open("a") as f:
        f.write(json.dumps(record) + "\n")


def test_refresh_reads_only_new_records_and_groups_rank_pieces(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    (run / "mallard.jsonl").write_text(json.dumps({"type": "start", "run_id": "r1"}) + "\n")
    loc = CountingLocation()
    for step in range(3):
        write_step(run, step, ranks=4)
    m = build_manifest(loc, str(run))
    first = loc.read
    assert first == (run / "mallard.jsonl").stat().st_size
    g = groups(m)
    assert sorted(g) == [f"solut/s_{s:06d}.pvtu" for s in range(3)]
    assert all(len(v) == 4 and all(e.recorded for e in v) for v in g.values())
    write_step(run, 3, ranks=4)
    m = build_manifest(loc, str(run))
    assert loc.read - first == len((run / "mallard.jsonl").read_bytes()) - first  # the new record only
    assert len(groups(m)["solut/s_000003.pvtu"]) == 4
    (run / "mallard.jsonl").write_text(json.dumps({"type": "start", "run_id": "r2"}) + "\n")  # a new run
    m = build_manifest(loc, str(run))
    assert m.run_id == "r2" and not groups(m)
