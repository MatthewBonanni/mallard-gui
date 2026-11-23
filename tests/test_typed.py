"""A typed table (probe, boundary, region shape, mesh, ...) shows only the keys its type reads, as Mallard reports
them for the input, also where the input has several kinds at one catalog path."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, ClassVar

from mallard_gui.mallard import KeyInfo, Mallard, parse_catalog
from mallard_gui.session import Session, catalog_path
from mallard_gui.typed import instances

from .conftest import FakeMallard, fixture_json

LINE_PROBE = """
[[probes]]
name = "wake_line"
type = "line"
start = [1.0, 0.0]
end = [6.0, 0.0]
n_points = 51
fields = ["P"]
"""


def labels(s: Session, node: str, group: str) -> set[str]:
    g = next(g for g in s.properties(node)["groups"] if g["id"] == group)
    return {r["label"] for r in g["rows"]}


def with_line_probe(project: Path) -> Path:
    path = project / "input.toml"
    path.write_text(path.read_text().replace("\n[statistics]\n", LINE_PROBE + "\n[statistics]\n", 1))
    return project


def test_each_probe_shows_the_keys_its_type_reads(real_mallard: Mallard, cylinder_project: Path) -> None:
    s = Session(with_line_probe(cylinder_project), real_mallard)
    s.refresh_catalog()
    s.check()
    point, arc, wall, line = (labels(s, f"el:probes[{i}]", f"probes[{i}]") for i in range(4))
    assert {"start", "end", "n_points"} <= line
    assert not {"radius", "center", "angles", "point"} & line
    assert {"center", "radius", "angles", "n_points"} <= arc and not {"start", "end"} & arc
    assert "point" in point and "radius" not in point  # untyped: `point` or `start`/`end` choose
    assert not {"radius", "angles", "center"} & wall
    assert "region" not in line  # the alternative to a type: not read beside one
    # Singletons read what their type reads: a mesh from a file has no generated-mesh sizes
    mesh = labels(s, "section:mesh", "mesh")
    assert "filename" in mesh and not {"Lx", "Nx"} & mesh
    s.close()


def test_a_key_the_entry_type_does_not_read_is_flagged_with_reset(
    real_mallard: Mallard, cylinder_project: Path
) -> None:
    project = with_line_probe(cylinder_project)
    path = project / "input.toml"
    path.write_text(path.read_text().replace("n_points = 51\n", "n_points = 51\nradius = 2.0\n", 1))
    s = Session(project, real_mallard)
    s.refresh_catalog()
    s.check()
    g = next(g for g in s.properties("el:probes[3]")["groups"] if g["id"] == "probes[3]")
    radius = next(r for r in g["rows"] if r["label"] == "radius")
    assert radius["set"] and radius["error"] and not radius["readonly"]
    s.close()


class KindsMallard(FakeMallard):
    """Reports, like Mallard, only the keys each kind of probe and boundary reads: the case catalog of an input
    with one kind at a path lists that kind's keys, of several kinds their union."""

    PROBES: ClassVar[dict[str, set[str]]] = {
        "arc": {"center", "radius", "angles", "n_points"},
        "line": {"start", "end", "n_points"},
        "": {"point"},  # no type, `point`: a point probe; with `boundary`, a boundary probe
    }
    BOUNDARIES: ClassVar[dict[str, set[str]]] = {"wall_adiabatic": set(), "farfield": {"p", "T", "u"}}
    COMMON: ClassVar[set[str]] = {
        "name",
        "type",
        "fields",
        "file",
        "format",
        "sampling",
        "time_interval",
        "reductions",
        "boundary",
    }

    def list_keys(self, input_file: Path | None = None) -> tuple[dict[str, KeyInfo] | None, list[Any]]:
        catalog, diags = super().list_keys(input_file)
        if input_file is None or catalog is None:
            return catalog, diags
        data = tomllib.loads(input_file.read_text())
        probes = set().union(*(self.PROBES[str(p.get("type", ""))] for p in instances(data, "probes")))
        walls = set().union(*(self.BOUNDARIES[str(b["type"])] for b in instances(data, "boundaries")))
        keep = {f"probes.{k}" for k in probes | self.COMMON} | {f"boundaries.{k}" for k in walls | {"name", "type"}}
        data_keys = fixture_json("cylinder_keys.json")["keys"]
        return {
            k: v for k, v in catalog.items() if not k.startswith(("probes.", "boundaries.")) or k in keep
        } | parse_catalog({"keys": {k: data_keys[k] for k in keep if k in data_keys}}), diags


def test_every_typed_entry_shows_no_key_its_kind_does_not_read(cylinder_project: Path) -> None:
    s = Session(with_line_probe(cylinder_project), KindsMallard())  # type: ignore[arg-type]
    s.refresh_catalog()
    s.check()
    for array, kinds in (("probes", KindsMallard.PROBES), ("boundaries", KindsMallard.BOUNDARIES)):
        for i, entry in enumerate(s.doc.data[array]):
            shown = labels(s, f"el:{array}[{i}]", f"{array}[{i}]")
            own = kinds[str(entry.get("type", ""))]
            others = set().union(*kinds.values()) - own
            assert own <= shown, (array, i)
            assert not (others - set(entry)) & shown, (array, i, (others - set(entry)) & shown)
    # Every typed path the input has: no row the case's catalog does not list for it
    for cp in s.typed_paths():
        for table in instances(s.doc.data, cp):
            if cp in ("probes", "boundaries"):
                continue
            path = next(p for p in _paths(s.doc.data, cp) if s.doc.get(p) is table)
            read = {k.rpartition(".")[2] for k in s.catalog if k.rpartition(".")[0] == cp}
            read |= {"composition"} if {"X", "Y"} <= read else set()  # X and Y share one editor
            top = path.split(".")[0]
            for g in s.properties(f"el:{top}" if "[" in top else f"section:{top}")["groups"]:
                if g["id"] == path:
                    assert {r["label"] for r in g["rows"] if not r["set"]} <= read, cp
    s.close()


def _paths(data: Any, cp: str) -> list[str]:
    out: list[str] = []

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            if prefix and catalog_path(prefix) == cp:
                out.append(prefix)
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{prefix}[{i}]")

    walk(data, "")
    return out
