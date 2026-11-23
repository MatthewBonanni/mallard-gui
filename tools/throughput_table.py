"""Writes `src/mallard_gui/throughput_table.py`: measured throughput per rank (per thread on CPUs) by device class,
dimension, reconstruction and physics, for the run dialog's estimate before anything of the project's own ran.

Run from the repository root:

    source .venv/bin/activate && python tools/throughput_table.py

and commit the regenerated module. Every source is optional: a missing one is reported and skipped. Defaults:

- `--runs`: run directories `<dir>/runs/*/mallard.jsonl` (with the campaign driver's `result.json` beside each) or
  `<dir>/*/mallard.jsonl`. Defaults to the overnight campaigns `ovn0110`, `ovn0110b`, `ovn529`, the `c978-mem`
  campaign, the A100 campaign `ctput` (every key on one A100, `overnight/c_tput.py`) and the local CPU runs
  `throughput-cpu` under `~/local/mallard-devtools/logs`. Each run's start record
  gives the build (dimension, precision, backend), the ranks, the device, the threads and the resolved input; its
  end record `cells_per_second` over `steps` (Mallard's stepping time only). Only single-rank runs count (a
  per-rank figure without communication), only `main` builds when the campaign names its builds, and only
  default model options (a dynamic LES model is skipped). The repetitions of one run are reduced to their median.
- `--mallard`: a Mallard checkout, for `benchmarks/perf/` (the performance set's inputs and its per-hardware
  baselines, dated by the last commit of `baselines.csv`), `docs/guide/performance.md` (its first table: 2D Euler
  in ns per step and cell on a CPU and an A100) and `mechanisms/` (species counts).
- `--dashboard`: the performance dashboard's data, a JSON file or `<git dir>:<ref>:<path>`; defaults to
  `~/local/mallard-website:origin/main:data/performance.json`. Its newest commit's results, dated by that commit.

For each key the newest measurement wins (ties: more cells, then the source's name), so a campaign run on a newer
build shadows an older baseline. The output depends only on the sources: rerunning without changes rewrites the
same file. Paths in the table are relative to `--base` (default `~/local`).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import re
import statistics
import subprocess
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

HOME_LOCAL = Path.home() / "local"
LOGS = HOME_LOCAL / "mallard-devtools" / "logs"
DEFAULT_RUNS = [LOGS / name for name in ("ovn0110", "ovn0110b", "ovn529", "c978-mem", "ctput", "throughput-cpu")]
DEFAULT_MALLARD = HOME_LOCAL / "Mallard"
DEFAULT_DASHBOARD = f"{HOME_LOCAL / 'mallard-website'}:origin/main:data/performance.json"
OUTPUT = Path(__file__).resolve().parent.parent / "src" / "mallard_gui" / "throughput_table.py"

RECONSTRUCTIONS = ("muscl", "teno3", "teno5", "teno6")
PHYSICS = ("euler", "ns", "les", "reacting")
FINISHED = ("finished", "wall_time_limit")  # n_steps or t_stop, or [run] t_wall_stop


@dataclass(frozen=True)
class Row:
    device_class: str
    dim: int
    reconstruction: str
    physics: str
    species: int
    cells_per_second_per_rank: float  # per thread on CPUs
    threads: int
    precision: str
    hardware: str
    case: str
    cells: int
    date: str
    source: str
    origin: str  # for the estimate's basis: the campaign, benchmark or document

    @property
    def key(self) -> tuple[str, int, str, str, int]:
        return (self.device_class, self.dim, self.reconstruction, self.physics, self.species)

    @property
    def rank(self) -> tuple[str, int, str]:
        return (self.date, self.cells, self.source)


# Classification


def device_class(backend: str, device: str) -> str | None:
    if "A100" in device:
        return "a100"
    if backend in ("Cuda", "HIP", "SYCL"):
        return None
    return "cpu-apple-m" if "Apple" in device else "cpu-x86"


def hardware_name(device: str) -> str:
    device = re.sub(r"\s*\(.*\)$", "", device).replace("NVIDIA ", "")
    return device.strip() or "unknown"


def reconstruction(numerics: dict[str, Any]) -> str | None:
    rec = numerics.get("face_reconstruction")
    if not isinstance(rec, dict):
        preset = numerics.get("preset", "high_order")
        return "teno5" if preset in ("high_order", "accurate") else None if preset == "first_order" else "muscl"
    kind = str(rec.get("type", "FO")).upper()
    if kind == "MUSCL":
        return "muscl"
    if kind == "TENO":
        name = f"teno{int(rec.get('order', 5))}"
        return name if name in RECONSTRUCTIONS else None
    return None


def physics(doc: dict[str, Any], mechanisms: Path | None) -> tuple[str, int] | None:
    phys = doc.get("physics", {})
    if phys.get("type") == "euler":
        return "euler", 0
    if phys.get("type") != "navier_stokes":
        return None
    if phys.get("gas") == "mixture":
        return "reacting", species_count(str(phys.get("mechanism", "")), str(phys.get("phase", "")), mechanisms)
    les = doc.get("les")
    if isinstance(les, dict):
        return "les", 0
    return "ns", 0


def default_options(doc: dict[str, Any]) -> bool:
    les = doc.get("les")
    return not (isinstance(les, dict) and les.get("dynamic"))


def species_count(mechanism: str, phase: str, mechanisms: Path | None) -> int:
    """Species of the mechanism's phase, from the file named or one of that name in Mallard's `mechanisms/`."""
    candidates = [Path(mechanism)]
    if mechanisms is not None:
        candidates.append(mechanisms / Path(mechanism).name)
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        phases = text.split("\nspecies:")[0]
        blocks = re.split(r"\n- name:\s*", phases)
        for block in blocks[1:]:
            name = block.split("\n", 1)[0].strip()
            m = re.search(r"\n\s+species:\s*\[([^\]]*)\]", block)
            if m and (not phase or name == phase):
                return len([s for s in m.group(1).split(",") if s.strip()])
    return 0


def mesh_cells(doc: dict[str, Any], dim: int) -> int:
    mesh = doc.get("mesh", {})
    if mesh.get("type") != "cartesian":
        return 0
    n = int(mesh.get("Nx", 0)) * int(mesh.get("Ny", 0))
    return n * int(mesh.get("Nz", 1)) if dim == 3 else n


def relative(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


# Run logs


def ends(log: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    start = end = None
    try:
        start = json.loads(lines[0]) if lines else None
    except ValueError:
        start = None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "end":
            end = record
            break
    return start, end


def run_logs(root: Path) -> list[Path]:
    return sorted(root.glob("runs/*/mallard.jsonl")) or sorted(root.glob("*/mallard.jsonl"))


def from_runs(root: Path, base: Path, mechanisms: Path | None) -> list[Row]:
    groups: dict[tuple[Any, ...], list[tuple[float, str, Path]]] = defaultdict(list)
    info: dict[tuple[Any, ...], tuple[str, str, int, str]] = {}
    for log in run_logs(root):
        result: dict[str, Any] = {}
        with contextlib.suppress(OSError, ValueError):
            result = json.loads((log.parent / "result.json").read_text(encoding="utf-8"))
        build_name = str(result.get("build", ""))
        if build_name and not build_name.startswith("main"):
            continue
        start, end = ends(log)
        if not start or not end:
            continue
        cps, steps = end.get("cells_per_second"), end.get("steps")
        if not isinstance(cps, (int, float)) or cps <= 0 or not steps or end.get("outcome") not in FINISHED:
            continue
        prov = start.get("provenance") or {}
        build, run = prov.get("build") or {}, prov.get("run") or {}
        if int(run.get("ranks") or 1) != 1:
            continue
        cls = device_class(str(build.get("backend", "")), str(run.get("device", "")))
        try:
            doc = tomllib.loads((prov.get("input") or {}).get("resolved", ""))
        except tomllib.TOMLDecodeError:
            continue
        rec = reconstruction(doc.get("numerics", {}))
        phys = physics(doc, mechanisms)
        if cls is None or rec is None or phys is None or not default_options(doc):
            continue
        dim = int(build.get("dim", 0))
        placement = start.get("placement") or [{}]
        threads = int(placement[0].get("threads") or 1) if cls.startswith("cpu") else 0
        name = str(result.get("name") or re.sub(r"-r\d+-[0-9a-f]{8}$", "", log.parent.name))
        group = (cls, dim, rec, *phys, str(build.get("precision", "double")), threads, name)
        groups[group].append((float(cps), str(run.get("start_time", ""))[:10], log))
        campaign = str(result.get("campaign", ""))
        # Local runs share the machine with other work: their figures are indicative, and the basis says so
        local = f"local runs {root.name}, indicative (shared machine)"
        origin = f"{root.name} campaign {campaign}" if campaign else local
        info[group] = (hardware_name(str(run.get("device", ""))), origin, mesh_cells(doc, dim), name)
    rows = []
    for group, samples in groups.items():
        cls, dim, rec, phys, species, precision, threads, _ = group
        hardware, origin, cells, name = info[group]
        value = statistics.median(s[0] for s in samples)
        logs = sorted(s[2] for s in samples)
        where = relative(logs[0].parent.parent, base)
        reps = f", median of {len(logs)} runs" if len(logs) > 1 else ""
        source = f"{where}/{', '.join(p.parent.name for p in logs)} ({origin}{reps})"
        rows.append(
            Row(
                cls,
                dim,
                rec,
                phys,
                species,
                value / threads if threads else value,
                threads,
                precision,
                hardware,
                name,
                cells,
                max(s[1] for s in samples),
                source,
                origin,
            )
        )
    return rows


# The performance set and the dashboard


def perf_cases(mallard: Path) -> dict[str, tuple[int, str, str, int, int]]:
    """Benchmark name: (dim, reconstruction, physics, species, cells) for the performance set's solver cases."""
    perf = mallard / "benchmarks" / "perf"
    suite = json.loads((perf / "suite.json").read_text(encoding="utf-8"))
    cases = {}
    for case in suite["cases"]:
        if case.get("command") or case.get("metric"):
            continue  # chemistry alone, or a throughput that counts output
        dim = int(str(case["build"]).rstrip("d"))
        doc = tomllib.loads((perf / case["input"]).read_text(encoding="utf-8"))
        rec, phys = reconstruction(doc.get("numerics", {})), physics(doc, mallard / "mechanisms")
        if rec and phys:
            cases[case["name"]] = (dim, rec, *phys, mesh_cells(doc, dim))
    return cases


def git_date(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%cs", *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def from_baselines(mallard: Path, base: Path) -> list[Row]:
    perf = mallard / "benchmarks" / "perf"
    cases = perf_cases(mallard)
    date = git_date(mallard, "--", "benchmarks/perf/baselines.csv")
    rows = []
    with (perf / "baselines.csv").open(encoding="utf-8") as f:
        for line in csv.DictReader(f):
            cls = device_class("Cuda", line["hardware"])  # the performance set's baselines are GPUs
            if cls is None or line["benchmark"] not in cases:
                continue
            dim, rec, phys, species, cells = cases[line["benchmark"]]
            where = relative(perf / "baselines.csv", base)
            rows.append(
                Row(
                    cls,
                    dim,
                    rec,
                    phys,
                    species,
                    float(line["cells_per_s"]),
                    0,
                    "double",
                    hardware_name(line["hardware"]),
                    line["benchmark"],
                    cells,
                    date,
                    f"{where}, {line['benchmark']} (performance set baseline, best of 3)",
                    f"performance set {line['benchmark']}",
                )
            )
    return rows


def read_dashboard(spec: str) -> tuple[dict[str, Any], str]:
    if Path(spec).is_file():
        return json.loads(Path(spec).read_text(encoding="utf-8")), spec
    repo, ref, path = spec.rsplit(":", 2)
    out = subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True, check=True)
    return json.loads(out.stdout), f"{Path(repo).name}:{ref}:{path}"


def from_dashboard(spec: str, mallard: Path) -> list[Row]:
    data, where = read_dashboard(spec)
    cases = perf_cases(mallard)
    commits = data.get("commits") or []
    if not commits:
        return []
    newest = max(commits, key=lambda c: str(c.get("date", "")))
    cls = device_class("Cuda", str(data.get("hardware", "")))
    if cls is None:
        return []
    rows = []
    for name, result in sorted((newest.get("results") or {}).items()):
        if name not in cases or not result.get("best"):
            continue
        dim, rec, phys, species, cells = cases[name]
        rows.append(
            Row(
                cls,
                dim,
                rec,
                phys,
                species,
                float(result["best"]),
                0,
                "double",
                hardware_name(str(data["hardware"])),
                name,
                cells,
                str(newest.get("date", ""))[:10],
                f"{where}, {name} at {str(newest.get('sha', ''))[:9]} (performance dashboard, PR {newest.get('pr')})",
                f"performance dashboard {name}",
            )
        )
    return rows


# Mallard's performance guide


GUIDE_ROWS = {"MUSCL": "muscl", "TENO-E, order 3": "teno3", "TENO-E, order 5": "teno5", "TENO-E, order 6": "teno6"}


def from_guide(mallard: Path, base: Path) -> list[Row]:
    """The first table of the performance guide: 2D Riemann problem, Euler, ns per step and cell."""
    path = mallard / "docs" / "guide" / "performance.md"
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("|")]
    header = next(line for line in lines if "ns per step and cell" in line)
    columns = [c.strip() for c in header.strip("|").split("|")]
    body = lines[lines.index(header) + 2 :]
    nx = re.search(r"on (\d+) × (\d+) quadrilaterals", text)
    cells = int(nx.group(1)) * int(nx.group(2)) if nx else 0
    cpu = re.search(r"CPU: \d+ of the \d+ cores of an? ([^,]+),", text)
    where = relative(path, base)
    rows = []
    for col, title in enumerate(columns):
        if "ns per step and cell" not in title:
            continue
        version = re.search(r"\((\d+\.\d+\.\d+)\)", title)
        date = git_date(mallard, f"v{version.group(1)}") if version else ""
        threads_m = re.search(r"(\d+) threads", title)
        if title.startswith("CPU"):
            threads = int(threads_m.group(1)) if threads_m else 1
            hardware = cpu.group(1) if cpu else "CPU"
            cls = "cpu-apple-m" if "Apple" in hardware else "cpu-x86"
        elif "A100" in title:
            threads, cls, hardware = 0, "a100", "A100"
        else:
            continue
        for line in body:
            cells_ = [c.strip() for c in line.strip("|").split("|")]
            label = next((v for k, v in GUIDE_ROWS.items() if cells_[0].startswith(k)), None)
            if label is None or col >= len(cells_):
                continue
            try:
                ns = float(cells_[col])
            except ValueError:
                continue
            rate = 1e9 / ns
            v = f"Mallard {version.group(1)}" if version else "Mallard"
            rows.append(
                Row(
                    cls,
                    2,
                    label,
                    "euler",
                    0,
                    rate / threads if threads else rate,
                    threads,
                    "double",
                    hardware,
                    "riemann_2d_quads",
                    cells,
                    date,
                    f"{where}, first table: {title.split(':')[0]} ({v})",
                    f"Mallard's performance guide ({v})",
                )
            )
    return rows


# Output


def select(rows: list[Row]) -> list[Row]:
    best: dict[tuple[str, int, str, str, int], Row] = {}
    for row in rows:
        if row.key not in best or row.rank > best[row.key].rank:
            best[row.key] = row
    order = {"a100": 0, "cpu-apple-m": 1, "cpu-x86": 2}
    return sorted(
        best.values(),
        key=lambda r: (
            order.get(r.device_class, 9),
            r.dim,
            RECONSTRUCTIONS.index(r.reconstruction),
            PHYSICS.index(r.physics),
            r.species,
        ),
    )


def literal(value: Any) -> str:
    if isinstance(value, float):
        return repr(float(f"{value:.4g}"))
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(value)


def render(rows: list[Row]) -> str:
    entries = []
    for row in rows:
        fields = []
        for name in Row.__dataclass_fields__:
            value = getattr(row, name)
            text = f"        {name}={literal(value)},"
            if len(text) > 120 and isinstance(value, str):
                chunks = re.findall(r".{1,90}(?:\s|$)|.{1,90}", value)
                inner = "\n".join(f"            {literal(chunk)}" for chunk in chunks if chunk)
                text = f"        {name}=(\n{inner}\n        ),"
            fields.append(text)
        entries.append("    Entry(\n" + "\n".join(fields) + "\n    ),")
    return TEMPLATE.replace("@ENTRIES@", "\n".join(entries))


TEMPLATE = '''"""Measured throughput per rank, by device class, dimension, reconstruction and physics: the run dialog's
estimate before any run of the project's own. Generated by `tools/throughput_table.py` from Mallard's run logs, its
performance set, dashboard and guide; do not edit, regenerate.

`cells_per_second_per_rank` is per thread for CPU entries (`threads` is what the measurement ran on), so a CPU
estimate scales by the threads of the run (`Entry.rate`); GPU entries are one rank on one device.

`nearest()` takes the exact key when the table has it. Otherwise it ranks every entry by, in order: the same device
class (else another CPU class for a CPU, else anything), the same dimension, the nearest reconstruction (in the
order MUSCL, TENO-E 3, 5, 6), the nearest physics (Euler, NS, LES, reacting), the nearest species count, and on a
tie the slower entry, so a missing key is estimated on the expensive side.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

RECONSTRUCTIONS = ("muscl", "teno3", "teno5", "teno6")
PHYSICS = ("euler", "ns", "les", "reacting")
RECONSTRUCTION_LABELS = {"muscl": "MUSCL", "teno3": "TENO-E 3", "teno5": "TENO-E 5", "teno6": "TENO-E 6"}
PHYSICS_LABELS = {"euler": "Euler", "ns": "NS", "les": "LES", "reacting": "reacting"}


@dataclass(frozen=True)
class Entry:
    device_class: str  # "a100", "cpu-apple-m" or "cpu-x86"
    dim: int
    reconstruction: str  # one of RECONSTRUCTIONS
    physics: str  # one of PHYSICS
    species: int  # reacting: the mechanism's species; 0 otherwise
    cells_per_second_per_rank: float  # per thread on CPUs
    threads: int  # threads of the measurement (CPU); 0 on GPUs
    precision: str
    hardware: str
    case: str
    cells: int  # the case's cells (0: not a generated box)
    date: str
    source: str  # file or run directories and the run, campaign or benchmark
    origin: str  # the campaign, benchmark or document, for the basis

    @property
    def cpu(self) -> bool:
        return self.device_class.startswith("cpu")

    @property
    def ns_per_cell_step(self) -> float:
        """Per thread on CPUs."""
        return 1e9 / self.cells_per_second_per_rank

    def rate(self, threads: int = 1) -> float:
        """Cells per second on one rank: CPU entries scaled linearly to `threads`."""
        return self.cells_per_second_per_rank * (max(1, threads) if self.cpu else 1)

    def label(self) -> str:
        return label(self.dim, self.reconstruction, self.physics, self.species)


TABLE: tuple[Entry, ...] = (
@ENTRIES@
)


def label(dim: int, reconstruction: str, physics: str, species: int = 0) -> str:
    """E.g. "3D TENO-E 5, reacting (6 species)"."""
    phys = PHYSICS_LABELS.get(physics, physics)
    if physics == "reacting" and species:
        phys += f" ({species} species)"
    return f"{dim}D {RECONSTRUCTION_LABELS.get(reconstruction, reconstruction)}, {phys}"


def _device_distance(want: str, have: str) -> int:
    if want == have:
        return 0
    return 1 if want.startswith("cpu") and have.startswith("cpu") else 2


def _index(names: tuple[str, ...], name: str) -> int:
    return names.index(name) if name in names else len(names)


def nearest(
    device_class: str, dim: int, reconstruction: str, physics: str, species: int = 0
) -> tuple[Entry, str] | None:
    """The table's entry for this key or the closest to it (see the module's docstring), with a one-line basis such
    as "measured: A100-SXM4-80GB, 3D TENO-E 5, NS, ovn529 campaign c529"; None only for an empty table."""
    if not TABLE:
        return None

    def distance(e: Entry) -> tuple[float, ...]:
        rec = abs(_index(RECONSTRUCTIONS, reconstruction) - _index(RECONSTRUCTIONS, e.reconstruction))
        phys = abs(_index(PHYSICS, physics) - _index(PHYSICS, e.physics))
        # Chemistry outweighs the reconstruction: a reacting case is costed as reacting at any order
        first, second = (phys, rec) if physics == "reacting" else (rec, phys)
        return (
            _device_distance(device_class, e.device_class),
            abs(dim - e.dim),
            first,
            second,
            abs(math.log((species + 1) / (e.species + 1))),
            e.cells_per_second_per_rank,
        )

    entry = min(TABLE, key=distance)
    where = entry.hardware + (f" per thread (measured on {entry.threads})" if entry.cpu else "")
    basis = f"measured: {where}, {entry.label()}, {entry.origin}"
    key = (device_class, dim, reconstruction, physics)
    exact = (entry.device_class, entry.dim, entry.reconstruction, entry.physics) == key and (
        physics != "reacting" or not species or entry.species == species
    )
    if not exact:
        basis += f" (nearest to {device_class}, {label(dim, reconstruction, physics, species)})"
    return entry, basis
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=Path, nargs="*", default=DEFAULT_RUNS, help="campaign or run directories")
    parser.add_argument("--mallard", type=Path, default=DEFAULT_MALLARD, help="a Mallard checkout")
    parser.add_argument("--dashboard", default=DEFAULT_DASHBOARD, help="dashboard JSON, or <git dir>:<ref>:<path>")
    parser.add_argument("--base", type=Path, default=HOME_LOCAL, help="paths in the table are relative to it")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    mechanisms = args.mallard / "mechanisms"
    rows: list[Row] = []
    sources = [(str(d), lambda d=d: from_runs(d, args.base, mechanisms)) for d in args.runs]
    sources += [
        ("performance set baselines", lambda: from_baselines(args.mallard, args.base)),
        ("dashboard", lambda: from_dashboard(args.dashboard, args.mallard)),
        ("performance guide", lambda: from_guide(args.mallard, args.base)),
    ]
    for name, read in sources:
        try:
            found = read()
        except (OSError, ValueError, KeyError, StopIteration, subprocess.CalledProcessError) as e:
            print(f"skipped {name}: {e}", file=sys.stderr)
            continue
        print(f"{name}: {len(found)} measurements", file=sys.stderr)
        rows += found
    table = select([replace(r, cells_per_second_per_rank=float(f"{r.cells_per_second_per_rank:.4g}")) for r in rows])
    args.output.write_text(render(table), encoding="utf-8")
    print(f"wrote {len(table)} entries to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
