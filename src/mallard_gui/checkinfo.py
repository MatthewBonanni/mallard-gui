"""Everything `mallard check --format json` reports, arranged for the setup UI, the Run button and the launch
dialog: the time step and what sets it, resolution estimates, mesh quality, memory per rank with its
breakdown, sizing for a memory or GPU, cost (wall time per simulated time, to `t_stop`, per flow-through,
GPU- or rank-hours), the environment and the diagnostics with their suggestions.

Derived numbers are only arithmetic on the report's own (wall time to t_stop = wall time per unit time x the
time left); nothing about physics is computed here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .analysis.report import periodic_pairs
from .ui.fmt import bytes_text, duration, number

# Where each part of the check report goes besides the report tab's sections (a new part of the report is listed
# here or summarized, or the coverage test fails)
SUMMARIZED = frozenset(
    {"time_step", "cost", "memory", "sizing", "resolution", "mesh_quality", "environment", "boundaries"}
    | {"unassigned", "periodic", "regions", "version", "setup_done"}
)
ELSEWHERE = {
    "schema": "read by CheckReport",
    "status": "the status bar",
    "errors": "the status bar and the Run button",
    "warnings": "the status bar",
    "diagnostics": "the problems panel and the report's diagnostics, linked to their key and line",
    "unused_keys": "diagnostics (E-KEY-UNUSED), with the suggested key as a fix",
    "input_file": "the title",
    "dim": "the status bar and the case view",
    "case_view": "the case view",
    "case_view_images": "unused: the case view draws the blocks",
    "initial_file": "the analysis panel's initial state",
    "mesh_fields_file": "the analysis panel's mesh quality",
    "stencil_file": "unused: the GUI never asks for stencils",
}


@dataclass
class LaunchOptions:
    """The machine a run is sized for: ranks, memory per rank (a size or a GPU), and the throughput in cell
    updates per second per rank (estimated, measured or given: `throughput.py`)."""

    ranks: int = 1
    memory: str = ""
    throughput: float = 0.0
    throughput_note: str = ""  # what the throughput rests on, for the cost's row

    def args(self) -> list[str]:
        """`mallard check` options."""
        out = ["--ranks", str(self.ranks)] if self.ranks > 1 else []
        if self.memory:
            out += ["--memory", self.memory]
        if self.throughput > 0:
            out += ["--throughput", f"{self.throughput:g}"]
        return out


def with_throughput(data: Mapping[str, Any], throughput: float) -> dict[str, Any]:
    """A check report's data costed at another throughput, by `mallard check`'s own formula
    (`Solver::check_cost`: wall per unit time = cells / (ranks x throughput x dt)), until the next check."""
    out = dict(data)
    cost = dict(out.get("cost") or {})
    dt = (out.get("time_step") or {}).get("dt") or 0
    ranks = (out.get("memory") or {}).get("target_ranks") or cost.get("ranks") or 1
    if not cost or throughput <= 0:
        return out
    cost.update(throughput=throughput, throughput_measured=False, steps_timed=0)
    cost["wall_per_time"] = cost.get("cells", 0) / (ranks * throughput * dt) if dt else 0
    out["cost"] = cost
    return out


# `mallard check --memory`'s GPUs (solver_check.cpp), by the memory per rank each stands for.
GPU_TARGETS = {
    "a100-40": "NVIDIA A100 40 GB",
    "a100-80": "NVIDIA A100 80 GB",
    "h100-80": "NVIDIA H100 80 GB",
    "h200": "NVIDIA H200 141 GB",
    "mi250x": "AMD MI250X 64 GB (one GCD)",
    "mi300x": "AMD MI300X 192 GB",
}


def target_label(memory: str) -> str:
    """A `--memory` value in words: a GPU by its model and memory, a size as memory per rank."""
    return GPU_TARGETS.get(memory.lower(), f"{memory} per rank")


def fit_text(data: Mapping[str, Any], ranks: int) -> dict[str, str]:
    """What the check's memory and sizing say of a run on `ranks` ranks, in a sentence, and whether it fits."""
    mem = data.get("memory") or {}
    sizing = data.get("sizing") or {}
    if not mem:
        return {"text": "", "status": ""}
    at = int(mem.get("target_ranks") or mem.get("ranks") or ranks or 1)
    need = f"Needs {bytes_text(mem.get('target_bytes_peak') or mem.get('bytes_peak'))} per rank on {_ranks(at)}"
    if not sizing:
        return {"text": f"{need}; not sized for a device: choose one to see what it fits on.", "status": ""}
    name = target_label(str(sizing.get("name", "")))
    fewest = int(sizing.get("min_ranks") or 1)
    cells = number(sizing.get("max_local_cells"))
    if at >= fewest:
        text = f"{need}: fits on {name} from {_ranks(fewest)} (at most {cells} cells per rank)"
        status = "ok"
    else:
        text = f"{need}: does not fit on {name}; it needs at least {_ranks(fewest)} (at most {cells} cells per rank)"
        status = "error"
    halo = sizing.get("halo_ranks")
    if halo:
        text += f"; up to {_ranks(int(halo))} keep the halo under 10 %"
    return {"text": text + ".", "status": status}


@dataclass
class Row:
    k: str
    v: str
    u: str = ""
    status: str = ""  # ok | warn | error | ""
    note: str = ""
    key: str = ""  # the input key this row is about, which a click selects
    x: list[float] | None = None  # where in the domain, which a click marks on the case view
    node: str = ""  # the case-tree node this row is about (a zone, a region)


@dataclass
class Summary:
    rows: dict[str, list[Row]] = field(default_factory=dict)
    run_label: str = ""  # on the Run button: memory, steps, wall time
    cost_known: bool = False
    fit: dict[str, str] = field(default_factory=lambda: {"text": "", "status": ""})

    def to_dict(self) -> dict[str, Any]:
        return {
            "sections": [{"title": t, "rows": [vars(r) for r in rows]} for t, rows in self.rows.items() if rows],
            "run_label": self.run_label,
            "cost_known": self.cost_known,
            "fit": self.fit,
        }


def _vec(x: Any) -> str:
    return "(" + ", ".join(number(c) for c in x) + ")" if isinstance(x, list) else ""


def _status(s: Any) -> str:
    s = str(s or "").lower()
    return "warn" if s.startswith("warn") else "error" if s.startswith("err") else "ok" if s == "ok" else ""


def _point(x: Any) -> list[float] | None:
    return [float(c) for c in x] if isinstance(x, list) and x and all(isinstance(c, (int, float)) for c in x) else None


def summarize(data: Mapping[str, Any], nondimensional: bool = False, throughput_note: str = "") -> Summary:
    out = Summary()
    t_unit = "" if nondimensional else "s"
    ts = data.get("time_step") or {}
    rows: list[Row] = []
    if ts:
        by_dt = "dt" in str(ts.get("set_by", "")).lower()
        rows.append(
            Row(
                "dt",
                number(ts.get("dt")),
                t_unit,
                note=f"set by {ts.get('set_by', '')}",
                key=f"run.{'dt' if by_dt else 'cfl'}",
            )
        )
        if ts.get("cfl") is not None:
            st = "warn" if ts.get("cfl_limit") and ts["cfl"] > ts["cfl_limit"] else "ok"
            rows.append(
                Row(
                    "CFL",
                    number(ts["cfl"]),
                    "",
                    st,
                    f"limit {number(ts.get('cfl_limit'))} ({ts.get('integrator', '')})",
                    key="run.cfl",
                )
            )
        if ts.get("dt_cfl1") is not None:
            rows.append(Row("dt at CFL 1", number(ts["dt_cfl1"]), t_unit))
        if ts.get("limiting_x") is not None:
            rows.append(
                Row(
                    "limiting cell",
                    f"{ts.get('limiting_cell')} at {_vec(ts['limiting_x'])}",
                    "",
                    x=_point(ts["limiting_x"]),
                )
            )
        if ts.get("region_cells"):
            lo, hi = _point(ts.get("region_lo")), _point(ts.get("region_hi"))
            center = [(a + b) / 2 for a, b in zip(lo, hi, strict=True)] if lo and hi else None
            rows.append(
                Row(
                    "cells near the limit",
                    number(ts["region_cells"]),
                    "",
                    note=f"in {_vec(lo)} – {_vec(hi)}" if lo and hi else "",
                    x=center,
                )
            )
    out.rows["TIME STEP"] = rows

    cost = data.get("cost") or {}
    mem = data.get("memory") or {}
    gpu = bool(mem.get("device_memory"))
    rows = []
    if cost:
        rows.append(Row("cells", number(cost.get("cells")), "", key="mesh"))
        rows.append(Row("ranks", str(cost.get("ranks", 1)), ""))
        dt = ts.get("dt")
        t_left = (cost.get("t_stop") or 0) - (ts.get("t") or 0)
        steps = cost.get("n_steps") or (round(t_left / dt) if dt and t_left > 0 else 0)
        if steps:
            stop = "run.n_steps" if cost.get("n_steps") else "run.t_stop"
            rows.append(Row("steps", number(steps), "", note="to the stop condition at this dt", key=stop))
        if cost.get("flow_through_time"):
            rows.append(Row("flow-through time", number(cost["flow_through_time"]), t_unit))
        wpt = cost.get("wall_per_time") or 0
        if cost.get("throughput"):
            how = throughput_note or (
                f"measured over {cost.get('steps_timed')} steps" if cost.get("throughput_measured") else "given"
            )
            rows.append(Row("throughput", number(cost["throughput"]), "cells/s/rank", note=how))
        if wpt:
            out.cost_known = True
            ranks = int(cost.get("ranks") or 1)
            rows.append(Row("wall per unit time", duration(wpt), "", note=f"per {t_unit or 'unit'} simulated"))
            if cost.get("flow_through_time"):
                rows.append(Row("wall per flow-through", duration(wpt * cost["flow_through_time"]), ""))
            total = wpt * t_left if t_left > 0 else 0
            if not total and steps and dt:
                total = wpt * steps * dt
            if total:
                rows.append(
                    Row(
                        "wall to the end",
                        duration(total),
                        "",
                        note=f"to t_stop {number(cost.get('t_stop'))}",
                        key="run.t_stop",
                    )
                )
                hours = total * ranks / 3600
                rows.append(Row("GPU-hours" if gpu else "rank-hours", number(hours), "h", note=_ranks(ranks)))
        else:
            rows.append(Row("wall time", "–", "", note="needs a throughput: measure some steps, or give one"))
    out.rows["COST"] = rows

    rows = []
    if mem:
        peak = mem.get("target_bytes_peak") or mem.get("bytes_peak")
        rows.append(Row("peak per rank", bytes_text(peak), "", note=_ranks(int(mem.get("target_ranks", 1) or 1))))
        rows.append(Row("steady per rank", bytes_text(mem.get("target_bytes") or mem.get("bytes")), ""))
        if mem.get("target_halo"):
            rows.append(
                Row("halo cells", number(mem["target_halo"]), "", note=f"of {number(mem.get('target_owned'))} owned")
            )
        if mem.get("device_capacity"):
            rows.append(Row("device memory", bytes_text(mem["device_capacity"]), ""))
        steady = mem.get("steady") or {}
        for part, b in sorted(steady.items(), key=lambda kv: -kv[1]):
            if b:
                rows.append(Row(part, bytes_text(b), "", note="steady"))
        for part, b in sorted((mem.get("at_setup_peak") or {}).items(), key=lambda kv: -kv[1]):
            if b and not steady.get(part):
                rows.append(Row(part, bytes_text(b), "", note="at the setup's peak only"))
    out.rows["MEMORY"] = rows

    sizing = data.get("sizing") or {}
    rows = []
    if sizing:
        rows.append(
            Row("for", target_label(str(sizing.get("name", ""))), "", note="--memory " + str(sizing.get("name", "")))
        )
        rows.append(
            Row(
                "cells per rank, at most",
                number(sizing.get("max_local_cells")),
                "",
                note=f"limit: {sizing.get('limit', '')}",
            )
        )
        rows.append(Row("ranks, at least", str(sizing.get("min_ranks", "")), ""))
        if sizing.get("halo_ranks"):
            rows.append(Row("ranks, halo under 10 %", str(sizing["halo_ranks"]), ""))
        rows.append(Row("per cell", bytes_text(sizing.get("bytes_per_cell")), ""))
    out.rows["SIZING"] = rows
    out.fit = fit_text(data, 1)

    rows = []
    for key, r in (data.get("resolution") or {}).items():
        if not isinstance(r, Mapping) or r.get("value") is None:
            continue
        units = "" if nondimensional and r.get("units") not in ("wall units",) else str(r.get("units") or "")
        rows.append(
            Row(
                str(r.get("quantity") or key),
                number(r["value"]),
                units,
                _status(r.get("status")),
                " · ".join(x for x in (str(r.get("target") or ""), str(r.get("basis") or "")) if x),
                x=_point(r.get("x")),
            )
        )
    out.rows["RESOLUTION"] = rows

    rows = []
    for q in (data.get("mesh_quality") or {}).values():
        if not isinstance(q, Mapping):
            continue
        st = "warn" if q.get("above_limit") else "ok"
        worst = (q.get("worst") or [{}])[0]
        units = "" if q.get("units") == "dimensionless" else str(q.get("units") or "")
        rows.append(
            Row(
                str(q.get("name")),
                number(q.get("max")),
                units,
                st,
                f"mean {number(q.get('mean'))} · limit {number(q.get('limit'))} · {q.get('above_limit', 0)} above"
                + (f" · worst cell {worst.get('cell')} at {_vec(worst.get('x'))}" if worst else ""),
                key="mesh",
                x=_point(worst.get("x")),
            )
        )
        if q.get("above_limit"):
            for w in (q.get("worst") or [])[1:]:
                rows.append(
                    Row(
                        f"  cell {w.get('cell')}",
                        number(w.get("value")),
                        units,
                        "warn" if (w.get("value") or 0) > (q.get("limit") or 0) else "",
                        f"{q.get('name')} at {_vec(w.get('x'))}",
                        x=_point(w.get("x")),
                    )
                )
    out.rows["MESH QUALITY"] = rows

    rows = []
    for b in data.get("boundaries") or []:
        if not isinstance(b, Mapping):
            continue
        note = " · ".join(
            x
            for x in (f"{number(b.get('faces'))} faces", str(b.get("details") or ""), str(b.get("turbulence") or ""))
            if x
        )
        zone = str(b.get("zone"))
        rows.append(Row(zone, str(b.get("condition") or ""), "", note=note, node=f"zone:{zone}"))
    for u in data.get("unassigned") or []:
        if isinstance(u, Mapping) and "zone" in u:
            zone = str(u["zone"])
            rows.append(
                Row(zone, "no condition", "", "error", f"{number(u.get('faces', 0))} faces", node=f"zone:{zone}")
            )
    for p in periodic_pairs(dict(data)):
        offset = (
            "node offsets not measured (a mesh read in parallel)"
            if p.node_offset_relative is None
            else f"nodes off by {number(p.node_offset_relative)} of the shortest edge"
        )
        rows.append(
            Row(
                p.label,
                "periodic",
                "",
                "ok" if p.matches else "warn",
                f"{p.map_text} · {number(p.faces)} faces · {offset}",
            )
        )
    out.rows["BOUNDARIES"] = rows

    rows = []
    for g in data.get("regions") or []:
        if not isinstance(g, Mapping):
            continue
        dim = g.get("dimension")
        what = "of the mesh" if g.get("of_mesh") else "defined by the solution" if g.get("solution") else ""
        shape = "exact" if g.get("exact") else f"extracted at {number(g.get('resolution'))}"
        measure = {1: "length", 2: "area", 3: "volume"}.get(int(dim or 0), "measure")
        note = " · ".join(x for x in (f"{dim}D", f"{measure} {number(g.get('measure'))}", shape, what) if x)
        empty = not g.get("cells") and not g.get("solution")
        name = str(g.get("name"))
        rows.append(
            Row(name, f"{number(g.get('cells'))} cells", "", "error" if empty else "", note, node=f"region:{name}")
        )
    out.rows["REGIONS"] = rows

    env = data.get("environment") or {}
    build = env.get("build") or {}
    rows = []
    if build:
        rows.append(Row("build", f"{build.get('dim')}D · {build.get('precision')} · {build.get('backend')}", ""))
        rows.append(
            Row("MPI · HDF5", f"{'yes' if build.get('mpi') else 'no'} · {'yes' if build.get('hdf5') else 'no'}", "")
        )
        solvers = env.get("solvers") or []
        if solvers:
            rows.append(Row("solvers installed", ", ".join(f"{d}D" for d in solvers), ""))
        if env.get("ranks"):
            rows.append(Row("ranks · hosts", f"{env.get('ranks')} · {env.get('hosts', 1)}", ""))
        aware = env.get("gpu_aware_mpi") or {}
        if build.get("gpu_aware_mpi") or aware.get("used"):
            rows.append(
                Row(
                    "GPU-aware MPI",
                    "used" if aware.get("used") else "not used",
                    "",
                    note=str(aware.get("library") or ""),
                )
            )
        if build.get("nccl"):
            rows.append(Row("NCCL", "used" if (env.get("nccl") or {}).get("used") else "not used", ""))
        for name, version in (env.get("dependencies") or {}).items():
            if version:
                rows.append(Row(name.replace("_", " "), str(version), "", note="dependency"))
        for p in env.get("placement") or []:
            rows.append(
                Row(
                    f"rank {p.get('rank')}",
                    f"{p.get('host')} · {p.get('device')}",
                    "",
                    note=f"{p.get('threads')} threads",
                )
            )
    out.rows["ENVIRONMENT"] = rows

    rows = [
        Row("check report", f"version {data.get('version')}", ""),
        Row("setup", "completed" if data.get("setup_done") else "stopped early", ""),
    ]
    out.rows["REPORT"] = rows

    peak = mem.get("target_bytes_peak") or mem.get("bytes_peak")
    parts = [bytes_text(peak)] if peak else []
    for r in out.rows["COST"]:
        if r.k == "steps":
            parts.append(f"{r.v} steps")
        if r.k == "wall to the end":
            parts.append(r.v)
    out.run_label = " · ".join(parts)
    return out


def suggestions(data: Mapping[str, Any]) -> dict[str, str]:
    """Unused keys the check suggests another name for: key -> suggested key."""
    out = {}
    for u in data.get("unused_keys") or []:
        if isinstance(u, Mapping) and u.get("key") and u.get("suggestion"):
            out[str(u["key"])] = str(u["suggestion"])
    return out


def worst_status(rows: Sequence[Row]) -> str:
    st = {r.status for r in rows}
    return "error" if "error" in st else "warn" if "warn" in st else "ok" if "ok" in st else ""


def _ranks(n: int) -> str:
    return f"{n} rank" if n == 1 else f"{n} ranks"


def diagnostic_text(
    severity: str, code: str, message: str, key: str = "", where: str = "", fix: str = "", docs: str = ""
) -> str:
    """A diagnostic as one copyable text: what, where, the message, the fix and the docs link."""
    head = " ".join(p for p in (severity, code) if p)
    lines = [head, *(f"{label}: {v}" for label, v in (("key", key), ("at", where)) if v), message]
    lines += [f"{label}: {v}" for label, v in (("fix", fix), ("docs", docs)) if v]
    return "\n".join(line for line in lines if line)
