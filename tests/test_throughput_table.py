from __future__ import annotations

import pytest

from mallard_gui import throughput_table as tt
from mallard_gui.throughput_table import Entry


def entry(device: str, dim: int, rec: str, phys: str, rate: float = 1e6, species: int = 0, threads: int = 0) -> Entry:
    return Entry(device, dim, rec, phys, species, rate, threads, "double", "HW", "case", 0, "2026-11-22", "src", "o")


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> list[Entry]:
    rows = [
        entry("a100", 3, "teno5", "ns", 8e6),
        entry("a100", 3, "muscl", "les", 5e7),
        entry("a100", 2, "teno6", "les", 3e7),
        entry("a100", 3, "teno3", "euler", 2e7),
        entry("a100", 3, "muscl", "reacting", 7e6, species=6),
        entry("a100", 3, "muscl", "reacting", 1e6, species=53),
        entry("cpu-apple-m", 3, "muscl", "ns", 5e5, threads=2),
    ]
    monkeypatch.setattr(tt, "TABLE", tuple(rows))
    return rows


def test_exact_key(table: list[Entry]) -> None:
    found = tt.nearest("a100", 3, "teno5", "ns")
    assert found is not None
    assert found[0] is table[0]
    assert "nearest" not in found[1] and "3D TENO-E 5, NS" in found[1]


def test_fallback_order(table: list[Entry]) -> None:
    def pick(*key: object) -> Entry:
        found = tt.nearest(*key)  # type: ignore[arg-type]
        assert found is not None and "nearest to" in found[1]
        return found[0]

    # Same dimension before the reconstruction: not the 2D TENO-E 6 LES
    assert pick("a100", 3, "teno6", "les") is table[0]
    # The reconstruction before the physics: TENO-E 5 NS over MUSCL LES
    assert pick("a100", 3, "teno5", "les") is table[0]
    # The nearest species count among reacting entries
    assert pick("a100", 3, "muscl", "reacting", 40) is table[5]
    # Reacting: the physics before the reconstruction, chemistry costing more than any order
    assert pick("a100", 3, "teno6", "reacting", 53) is table[5]
    # The same device class before anything else; another CPU class stands in for a CPU
    assert pick("cpu-x86", 3, "teno5", "ns") is table[6]


def test_tie_takes_the_slower_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    slow, fast = entry("a100", 3, "teno6", "ns", 4e6), entry("a100", 3, "teno3", "ns", 2e7)
    monkeypatch.setattr(tt, "TABLE", (fast, slow))
    found = tt.nearest("a100", 3, "teno5", "ns")
    assert found is not None and found[0] is slow


def test_cpu_rate_scales_by_threads() -> None:
    assert entry("cpu-apple-m", 2, "muscl", "euler", 1e6).rate(6) == 6e6
    assert entry("a100", 2, "muscl", "euler", 1e8).rate(6) == 1e8


def test_generated_table_is_well_formed() -> None:
    assert tt.TABLE
    keys = [(e.device_class, e.dim, e.reconstruction, e.physics, e.species) for e in tt.TABLE]
    assert len(set(keys)) == len(keys)
    for e in tt.TABLE:
        assert e.device_class in ("a100", "cpu-apple-m", "cpu-x86") and e.dim in (2, 3)
        assert e.reconstruction in tt.RECONSTRUCTIONS and e.physics in tt.PHYSICS
        assert e.cells_per_second_per_rank > 0 and e.source and e.date
        assert (e.threads > 0) == e.cpu
