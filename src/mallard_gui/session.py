"""One user's session: the project, the open case, its catalog and check, and its runs.

This is the GUI's logic without the UI: the trame app binds it to widgets, the tests drive it directly.
"""

from __future__ import annotations

import json
import tempfile
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import tomlkit

from . import choices, recent, typed, unit_keys, units
from .backends.base import Backend, Job, JobState, RunSpec
from .backends.k8s import HelperPod
from .backends.local import LocalBackend
from .backends.staging import run_input
from .backends.ssh import RemoteError
from .checkinfo import LaunchOptions
from .checklinks import referenced_keys
from .data.chunks import tree_at
from .data.locations import LOCAL, ShellLocation, SshLocation, TransferError
from .data.manager import DataManager
from .data.relocate import RelocateError, Relocator, RestartCheck
from .executables import Executables
from .features import FEATURES, has_content, is_on, missing_mechanism
from .fields import FieldCatalog, toml_array
from .grouping import grouped
from .input_doc import InputDocument, display_value, format_path, parse_path, parse_value
from .live import CONTROL_FILE_INTERVAL
from .mallard import CheckReport, Diagnostic, KeyInfo, Mallard, MallardError, Species, bare_literal, read_catalog
from .postjobs import Launcher, LocalLauncher, PostJobs, SlurmLauncher
from .presets import PresetView, SetupError, line_signature, run_log_presets, switch_changes
from .presets import compute as compute_presets
from .project import Project, RunRecord, copy_project_files, sha256_file
from .runlog import RunLogFollower, RunState
from .setup import regions as setup_regions
from .setup.editor import COORDINATE_KEYS, canonical
from .throughput import TIME_CAP, Summary, Throughput, from_runs, from_table, interval_for
from .throughput import measure as measure_throughput
from .units import Dim, UnitError, convert, dimension_of, field_of, parse_unit, si_unit, store
from .vectors import component, point_list_of, vector_of
from .view.scene import label_of
from .viewer.batch import BatchImages
from .viewer.client import LocalViewer, RemoteViewer, StdioViewer, Viewer, start_remote_viewer
from .viewer.engine import ViewerError
from .viewer.k8s import K8sViewer, SocketViewer
from .viewer.parallel import (
    DRIVER_DIR,
    LocalParallelViewer,
    ParallelPlan,
    plan,
    start_parallel_viewer,
)

# The case tree's order, as a case is built, under the headings of Mallard's input reference (docs/input.md) and
# user guide: group, then its sections (id, title, the tables each shows). Presentation only.
# TODO(#1256): order and grouping as display hints of the catalog.
GROUPS: tuple[tuple[str, tuple[tuple[str, str, tuple[str, ...]], ...]], ...] = (
    ("Geometry", (("mesh", "Mesh", ("mesh",)), ("periodic", "Periodicity", ("periodic",)))),
    (
        "Physics",
        (
            ("physics", "Physics", ("physics",)),
            ("chemistry", "Chemistry", ("chemistry",)),
            ("les", "LES", ("les",)),
            ("radiation", "Radiation", ("radiation",)),
            ("particles", "Particles", ("particles",)),
        ),
    ),
    (
        "Conditions",
        (
            ("initialize", "Initial state", ("initialize",)),
            ("boundaries", "Boundaries", ("boundaries",)),
            ("regions", "Regions", ("regions",)),
            ("sponges", "Sponges", ("sponges",)),
            ("sources", "Sources", ("source", "sources")),
        ),
    ),
    ("Solver", (("numerics", "Numerics", ("numerics",)), ("run", "Run", ("run",)))),
    (
        "Output",
        (
            ("output", "Output", ("output", "write_data", "forces", "integrals")),
            ("probes", "Probes", ("probes",)),
            ("statistics", "Statistics", ("statistics",)),
            ("triggers", "Triggers", ("triggers",)),
        ),
    ),
    ("Execution", (("parallel", "Parallel", ("parallel",)),)),
)
SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(sec for _, secs in GROUPS for sec in secs)
# Shown whether or not the catalog lists them; the other sections only when a catalog or the input has them.
CORE_SECTIONS = {
    "mesh",
    "physics",
    "boundaries",
    "initialize",
    "numerics",
    "regions",
    "probes",
    "output",
    "statistics",
    "run",
}
# `[check]` holds the check's own test settings, not the case's: the check is reported in the check status.
NOT_SECTIONS = {"check"}
OTHER_GROUP = "Other"
_KNOWN_TABLES = {t for _, _, tables in SECTIONS for t in tables}


def catalog_path(path: str) -> str:
    """`boundaries[1].T` and `boundaries[name = "x"].T` -> `boundaries.T`, as the catalog names keys."""
    return ".".join(s for s in parse_path(path) if isinstance(s, str))


@dataclass
class Row:
    path: str
    label: str
    value: str
    set: bool
    type: str | None = None
    widget: str = "text"
    unit: str = ""
    options: list[str] = field(default_factory=list)
    placeholder: str = ""
    required: bool = False
    error: str = ""
    severity: str = ""
    readonly: bool = False
    number: str = ""  # a dimensional key's number, shown beside its unit selector
    unit_choice: str = ""
    unit_options: list[str] = field(default_factory=list)
    origin: str = ""  # "preset" (a preset fills it) or "override" (the input sets it over the preset)
    other_dim: str = ""  # "3D only" for a key the case's dimension does not read
    unset: str = ""  # "not set" (": no wall-clock limit"): an unset key with no default value
    help: dict[str, str] = field(default_factory=dict)  # the key's catalog entry, for the key help
    entry: str = ""  # a link row's array entry (`sponges[1]`), which it can duplicate and delete
    free: bool = False  # its dropdown also takes a value not listed (names the check may not know yet)
    file: bool = False  # it names a file, chosen with the file dialog
    comp: dict[str, Any] = field(default_factory=dict)  # a composition's editor: basis, entries, sum, species
    fields: list[dict[str, str]] = field(default_factory=list)  # a field list's entries: problem, names covered
    oneof: dict[str, Any] = field(default_factory=dict)  # alternatives' selector: labels, active, conflict, clear
    vector: dict[str, Any] = field(default_factory=dict)  # one box per component: labels, values, placeholders
    unit_unknown: str = ""  # why the key's unit is not known from the key alone (`unit_keys.UNKNOWN`)
    points: dict[str, Any] = field(default_factory=dict)  # a list of points: labels, and rows of components
    models: dict[str, dict[str, str]] = field(default_factory=dict)  # value -> its theory page: url, tip, cite

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


@dataclass
class Group:
    title: str
    rows: list[Row]
    note: str = ""
    collapsed: bool = False
    id: str = ""  # the table's path, else the title
    depth: int = 0  # nesting under the selected table
    entry: str = ""  # an array entry the group shows (`initialize.regions[0]`), which it can duplicate and delete
    add: dict[str, Any] = field(default_factory=dict)  # the array or named table it adds entries to: path, types
    feature: dict[str, Any] = field(default_factory=dict)  # an optional feature's switch (features.py)
    hidden: bool = False  # under a feature that is off

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "note": self.note,
            "collapsed": self.collapsed,
            "id": self.id or self.title,
            "depth": self.depth,
            "entry": self.entry,
            "add": self.add,
            "feature": self.feature,
            "hidden": self.hidden,
            "rows": [r.to_dict() for r in self.rows],
        }


RESUMABLE = ("stopped", "signal", "wall_time_limit")
PRESET_LINES = 32  # Preset rows kept, by signature

# What the key editor offers for a table the input does not set yet, by the catalog's `form`.
FORM_PLACEHOLDERS = {
    "table": "a TOML table: {key = value, ...}",
    "array": "an array of tables: [{key = value, ...}, ...]",
    "named": "entries under names you choose: {name = value, ...}",
}


def _components(literal: str, n: int) -> list[str]:
    """A default such as `[0.0, 0.0]` as each component's placeholder (none for anything else)."""
    try:
        value = tomllib.loads(f"v = {literal}")["v"] if literal.startswith("[") else None
    except tomllib.TOMLDecodeError:
        value = None
    if isinstance(value, list) and len(value) == n:
        return [display_value(v) for v in value]
    return [""] * n


@dataclass
class RestartStatus:
    """Whether this build continues a run's last restart (`file.info`'s `continuable`), and why not."""

    path: str
    continuable: bool | None  # None: the build does not say (an older Mallard), or the file was not read
    reasons: list[str] = field(default_factory=list)


class RestartError(RuntimeError):
    pass


class UnsavedChanges(RestartError):
    pass


@dataclass
class ActiveRun:
    record: RunRecord
    job: Job
    follower: RunLogFollower
    state: JobState = JobState.RUNNING
    # The run log's start records once this part has started: a continued run's log holds the parts before.
    starts: int = 1
    backend: Backend | None = None  # where it runs, when not this machine

    @property
    def started(self) -> bool:
        return self.follower.state.starts >= self.starts


class Session:
    def __init__(
        self,
        project_dir: Path,
        mallard: Mallard,
        input_name: str | None = None,
        executables: Executables | None = None,
    ) -> None:
        self.mallard = mallard
        self.executables = executables  # None: `mallard` runs every case, whatever its dimension
        self.dim: int | None = None
        self.project = Project.open(project_dir)
        inputs = self.project.inputs
        name = input_name or (inputs[0] if inputs else "input.toml")
        self.input_path = self.project.root / name
        if name not in inputs and self.input_path.is_file():
            self.project.data["inputs"] = [*inputs, name]
            self.project.save()
        self.doc = InputDocument(self.input_path)
        self.mallard.case_input = self.input_path
        self.doc.restore_recovery()
        recent.add(self.input_path)
        self.catalog: dict[str, KeyInfo] = {}
        self.launch = LaunchOptions()  # the machine checks size the run for
        self.throughput: Throughput | None = None  # what launch.throughput rests on
        self.preferred_units: dict[Dim, str] = {}  # settings.toml's [units]: where empty fields start
        self.shown_units: dict[str, str] = {}  # by key path: the unit its field shows, as last chosen or typed
        self.general: dict[str, KeyInfo] = {}  # every key Mallard knows, for sections the input does not set
        self.general_error = ""  # why this build gave no general catalog
        self.presets: dict[str, PresetView] = {}  # by preset key, for the text they were resolved from
        self._presets_text = ""
        self._preset_lines: dict[tuple[str, tuple[Any, ...]], str] = {}
        self._log_presets: dict[Path, tuple[tuple[int, int], tuple[str, list[str]] | None]] = {}
        self.catalog_stale = False
        self.case_catalog = False  # `catalog` is the case's own, not the general catalog standing in for it
        self.entry_reads: dict[tuple[str, str], frozenset[str] | None] = {}  # (catalog path, signature): keys read
        self.report: CheckReport | None = None
        self.structure: CheckReport | None = None  # the last check whose setup completed
        self.view_dir = Path(tempfile.mkdtemp(prefix="mallard-gui-view-"))
        self.backend = LocalBackend(mallard.executable, mallard.threads)
        self.active: dict[str, ActiveRun] = {}
        self._data: DataManager | None = None
        self.viewers: dict[str, Viewer] = {}
        self.postjobs = PostJobs()
        self.boundary_values: dict[str, Any] | None = None  # `case.boundary_values` of the last check
        self.case_files: dict[str, Any] | None = None  # `case.files` of the last check
        self.species: Species | None = None  # the mechanism's species (`list species`); None: not known
        self.case_fields: dict[str, Any] | None = None  # `list fields input.toml`; None: not known
        self.build_fields: dict[str, Any] | None = None  # `list fields`, the build's catalog
        self.species_error = ""  # why the species are not known
        self.comp_basis: dict[str, str] = {}  # a composition table's X or Y chosen while it sets neither
        self.oneof_pick: dict[tuple[str, int], int] = {}  # (table, set of alternatives): the branch chosen
        self._restarts: dict[tuple[str, int, int], RestartStatus] = {}
        self.select_mallard()
        self.load_units()

    def select_mallard(self) -> bool:
        """The executable for the case's dimension (Settings' per-dimension paths, else the front end); True
        when it changed."""
        if self.executables is None:
            return False
        self.dim = self.executables.dimension(self.doc.data, self.input_path.parent)
        path = self.executables.for_dim(self.dim)
        if path == self.mallard.executable:
            return False
        self.mallard.close()
        self.mallard = Mallard(path, threads=self.mallard.threads)
        self.mallard.case_input = self.input_path
        self.backend = LocalBackend(path, self.mallard.threads)
        return True

    @property
    def data(self) -> DataManager:
        """The data manager, opened on first use (it reads remotes.toml and the cache index)."""
        if self._data is None:
            self._data = DataManager(self.project)
        return self._data

    # Catalog and check

    @property
    def checked_path(self) -> Path:
        """The input as edited: the recovery file beside it while there are unsaved edits, else the file."""
        recovery = self.doc.recovery_path
        return recovery if self.doc.dirty and recovery.is_file() else self.input_path

    @property
    def catalog_in_check(self) -> bool:
        """Whether `check` gives the case's key catalog from its own setup (`--catalog`), so no `list keys` of
        the input sets the case up a second time."""
        return self.mallard.capabilities().catalog

    def refresh_catalog(self) -> list[Diagnostic]:
        """The keys this input reads; a failing input keeps the last good catalog."""
        catalog, diags = self.mallard.list_keys(self.checked_path)
        self._use_catalog(catalog)
        return self._named_as_input(diags)

    def _use_catalog(self, catalog: dict[str, KeyInfo] | None) -> None:
        if not self.general:
            general, failed = self.mallard.list_keys(None)
            self.general = general or {}
            self.general_error = "" if general else "; ".join(d.message for d in failed) or "no key catalog"
        if catalog is not None:
            self.catalog = catalog
            self.catalog_stale = False
            self.case_catalog = True
            self.refresh_entry_reads()
        else:
            self.catalog_stale = True
            if not self.catalog:
                general, _ = self.mallard.list_keys(None)
                self.catalog = general or {}
        self.refresh_species()
        self.refresh_fields()

    def _named_as_input(self, diags: list[Diagnostic]) -> list[Diagnostic]:
        """Mallard names the file it read: while there are unsaved edits that is the hidden recovery file, which
        the user never opened. Its messages name the input instead."""
        read, shown = self.checked_path.name, self.input_path.name
        if read == shown:
            return diags
        return [replace(d, message=d.message.replace(read, shown)) for d in diags]

    def typed_paths(self) -> frozenset[str]:
        """The catalog paths of tables whose keys depend on their `type`."""
        stamp = (id(self.general), id(self.catalog))
        if getattr(self, "_typed_stamp", None) != stamp:
            self._typed_cache = frozenset(
                k.removesuffix(".type")
                for c in (self.general, self.catalog)
                for k, v in c.items()
                if k.endswith(".type") and v.values
            )
            self._typed_stamp = stamp
        return self._typed_cache

    def refresh_entry_reads(self) -> None:
        """What each kind of typed table reads where the input has several kinds at one path: one `list keys` of
        a scratch input per kind not seen yet."""
        data = self.doc.data
        run = self._scratch_runner(self.mallard.list_keys)
        for cp in self.typed_paths():
            if not typed.mixed(data, cp):
                continue
            for table in typed.instances(data, cp):
                sig = (cp, typed.signature(table))
                if sig in self.entry_reads:
                    continue
                try:
                    catalog, _ = run(tomlkit.dumps(typed.uniform(data, cp, table)))
                except (MallardError, OSError):
                    catalog = None
                self.entry_reads[sig] = (
                    frozenset(k.rpartition(".")[2] for k in catalog if k.rpartition(".")[0] == cp) if catalog else None
                )

    def reads(self, path: str) -> frozenset[str] | None:
        """The keys a typed table reads, as Mallard reports them for this input; None when not known (an untyped
        table, a stale catalog, a table the case's catalog does not have yet)."""
        cp = catalog_path(path)
        table = self.doc.get(path)
        if not isinstance(table, dict) or not self.case_catalog or self.catalog_stale or cp not in self.typed_paths():
            return None
        if typed.mixed(self.doc.data, cp):
            return self.entry_reads.get((cp, typed.signature(table)))
        found = frozenset(k.rpartition(".")[2] for k in self.catalog if k.rpartition(".")[0] == cp)
        return found or None

    def refresh_species(self) -> None:
        """The mechanism's species, for composition editors and species names; a mechanism that does not load
        leaves them unknown, with Mallard's error."""
        try:
            self.species = self.mallard.list_species(self.checked_path)
            self.species_error = "" if self.species is not None else "this mallard has no list species"
        except (SetupError, MallardError) as e:
            self.species, self.species_error = None, str(e)

    def refresh_fields(self) -> None:
        """The case's output fields; a case that does not set up keeps the last it had."""
        try:
            self.case_fields = self.mallard.list_fields(self.checked_path) or self.case_fields
            if self.build_fields is None:
                self.build_fields = self.mallard.list_fields(None)
        except (MallardError, OSError):
            pass

    def field_catalog(self, path: str) -> FieldCatalog | None:
        """What a field list (`write_data[2].fields`) can name, or None when it is not one Mallard lists."""
        if catalog_path(path) != "write_data.fields" or self.case_fields is None:
            return None
        return FieldCatalog(self.case_fields, self.build_fields, "write_data")

    def field_entries(self, path: str) -> list[str]:
        value = self.doc.get(path)
        return [str(v) for v in value] if isinstance(value, list) else []

    def set_fields(self, path: str, entries: list[str]) -> None:
        """A field list as picked, written as the TOML array of its entries."""
        self.set_value(path, toml_array(entries) if entries else "")

    def check(self) -> CheckReport:
        if self.select_mallard():
            self.refresh_catalog()
            self.load_units()
        written = self.view_dir / "catalog.json" if self.catalog_in_check else None
        if written is not None:
            written.unlink(missing_ok=True)
            report = self.mallard.check(self.checked_path, self.view_dir, self.launch.args(), catalog=written)
            self._use_catalog(read_catalog(written) if report.data.get("setup_done") else None)  # partial else
        else:
            report = self.mallard.check(self.checked_path, self.view_dir, self.launch.args())
        self.report = report
        self.report.diagnostics = self._named_as_input(self.report.diagnostics)
        read, shown = self.checked_path.name, self.input_path.name
        for d in self.report.data.get("diagnostics") or []:
            if isinstance(d.get("message"), str):
                d["message"] = d["message"].replace(read, shown)
        self.link_diagnostics(self.report)
        if self.report.data.get("setup_done"):
            self.structure = self.report
        self.refresh_boundary_values()
        return self.report

    # Mallard's own answers, from builds that give them (capability detection: `initialize`'s methods)

    @property
    def _text(self) -> str | None:
        """The editor's text when it differs from the file, for the session's case."""
        return self.doc.text if self.doc.dirty else None

    def load_units(self) -> bool:
        """The units and their exact factors from `units.catalog`; the bundled table for builds without it."""
        return units.use_catalog(self.mallard.units_catalog())

    def refresh_boundary_values(self) -> dict[str, Any] | None:
        """Each `[[boundaries]]` entry's values at its faces (`case.boundary_values`), when the case lets flow in
        somewhere; None for builds without the request or a case whose setup fails."""
        from .setup.inflow import INFLOW_TYPES

        self.boundary_values = None
        if not any(e.kind in INFLOW_TYPES for e in self.doc.elements("boundaries")):
            return None
        if self.report is not None and self.report.errors:
            return None
        try:
            self.boundary_values = self.mallard.boundary_values(
                self.input_path, self._text, self.view_dir / "boundary_values"
            )
        except MallardError:
            self.boundary_values = None
        return self.boundary_values

    def refresh_case_files(self) -> dict[str, Any] | None:
        """The input and every file its setup read (`case.files`); None for builds without the request."""
        try:
            self.case_files = self.mallard.case_files(self.input_path, self._text)
        except MallardError as e:
            self.case_files = {"error": str(e)}
        return self.case_files

    def restart_status(self, record: RunRecord) -> RestartStatus:
        """Whether this build continues `record`'s last restart: `file.info`'s `continuable` and its
        `incompatibilities`, asked again only when the file changes."""
        entry = str(record.last_restart.get("path", ""))
        if not entry or record.home.get("remote", "local") != "local":
            return RestartStatus(entry, None)
        path = self.project.root / record.home.get("directory", f"runs/{record.name}") / entry.removeprefix("./")
        try:
            st = path.stat()
        except OSError:
            return RestartStatus(str(path), False, [f"{entry}: missing"])
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key in self._restarts:
            return self._restarts[key]
        status = RestartStatus(str(path), None)
        try:
            data = self.mallard.file_info(path)
        except MallardError as e:
            status = RestartStatus(str(path), False, [str(e)])
        else:
            if isinstance(data.get("continuable"), bool):
                reasons = [str(r) for r in data.get("incompatibilities") or []]
                status = RestartStatus(str(path), data["continuable"], reasons)
        self._restarts[key] = status
        return status

    def continue_state(self, record: RunRecord, live: bool) -> dict[str, Any]:
        """Whether Continue is enabled for a run, and every reason it is not: a running run, an outcome that
        cannot continue, no restart, or what this build's `file.info` finds against the restart (also
        `continue_refused`, the reasons the page shows)."""
        why: list[str] = []
        refused: list[str] = []
        if live:
            why.append("the run is running")
        else:
            if record.outcome not in RESUMABLE:
                why.append(f"it ended {record.outcome or 'without an outcome'}; continue needs {', '.join(RESUMABLE)}")
            if not record.last_restart.get("path"):
                why.append("no restart recorded")
            if not why:
                status = self.restart_status(record)
                if status.continuable is False:
                    refused = status.reasons or ["this Mallard does not continue from it"]
                    why += refused
        return {"can_continue": not why, "continue_why": why, "continue_refused": refused}

    def run_controls(self) -> dict[str, bool]:
        """The controls of a running run this build's runs answer (`initialize`'s control files)."""
        caps = self.mallard.capabilities()
        return {"stop": caps.control_file("STOP"), "checkpoint": caps.control_file("CHECKPOINT")}

    def preload_throughput(self) -> Throughput:
        """The throughput the cost uses before any is measured or given: this project's earlier runs', else the
        nearest measured run's of the throughput table."""
        if self.throughput is None or self.throughput.kind == "estimated":
            data = self.report.data if self.report else {}
            cells = int((data.get("cost") or {}).get("cells") or 0)
            dim = int((data.get("environment") or {}).get("build", {}).get("dim") or self.dim or 0)
            logs = [
                (r.name, self.project.root / r.home.get("directory", f"runs/{r.name}") / "mallard.jsonl")
                for r in self.project.runs()
                if r.home.get("remote", "local") == "local"
            ]
            gpu = bool((data.get("memory") or {}).get("device_memory"))
            species = len(self.species.names) if self.species else 0
            self.throughput = from_runs(logs, cells, dim) or from_table(
                self.doc.data, dim, gpu, self.backend.threads, species
            )
            self._use(self.throughput)
        return self.throughput

    def _use(self, throughput: Throughput) -> None:
        self.throughput = throughput
        self.launch.throughput = throughput.value
        self.launch.throughput_note = throughput.note()

    def give_throughput(self, value: float) -> None:
        """A throughput typed in; 0 goes back to the estimate."""
        if value > 0:
            self._use(Throughput(value, "given", "typed in"))
        else:
            self.throughput = None
            self.preload_throughput()

    def measure_throughput(
        self, on_progress: Callable[[Summary | None, int], None], cancel: threading.Event, time_cap: float = TIME_CAP
    ) -> Throughput:
        """Times this case's steady stepping on one rank of this machine; later checks cost the run with it."""
        cells = int(((self.report.data if self.report else {}).get("cost") or {}).get("cells") or 0)
        estimate = self.preload_throughput().value if self.throughput is None else self.throughput.value
        data = self.mallard.data_dir()
        result = measure_throughput(
            self.mallard.executable,
            self.checked_path,
            self.backend.threads,
            interval_for(estimate, cells),
            on_progress,
            cancel,
            time_cap=time_cap,
            env={"MALLARD_DATA_DIR": str(data)} if data else None,
        )
        self._use(result)
        return result

    def knows(self, path: str) -> bool:
        """A key or a section of the key catalog (the case's or the general one)."""
        if path in self.catalog or path in self.general:
            return True
        prefix = path + "."
        return any(k.startswith(prefix) for k in (*self.catalog, *self.general))

    def link_diagnostics(self, report: CheckReport) -> None:
        """The keys each diagnostic is about: the structured key the build gives (null for none), else, from builds
        before structured keys, those its text names, the first full key named as its key."""
        for d in report.diagnostics:
            if d.structured:
                d.refs = (d.key,) if d.key else ()
                continue
            refs = referenced_keys(d.message, self.knows)
            if d.key is None:
                d.key = next((r for r in refs if "." in r or "[" in r), None)
            d.refs = tuple(dict.fromkeys([d.key, *refs] if d.key else refs))

    @property
    def diagnostics(self) -> list[Diagnostic]:
        return self.report.diagnostics if self.report else []

    @property
    def run_blocked(self) -> str:
        """Why Run is disabled, or '' when it is not: errors block, warnings never do."""
        if self.report is None:
            return "check first"
        if self.report.errors:
            n = self.report.errors
            return f"{n} error{'s' if n > 1 else ''}"
        return ""

    def diagnostic_for(self, path: str) -> Diagnostic | None:
        """The worst diagnostic attached to the key at `path` (any path syntax)."""
        try:
            want = self.doc.resolve(path)
        except (KeyError, ValueError):
            return None
        found = None
        for d in self.diagnostics:
            if not d.key:
                continue
            try:
                got = self.doc.resolve(d.key)
            except (KeyError, ValueError):
                continue
            if got == want and (found is None or d.severity == "error"):
                found = d
        return found

    def node_of(self, d: Diagnostic) -> str | None:
        """The case-tree node a diagnostic belongs to (and counts in the badge of)."""
        return self.node_of_key(d.key or "")

    def link_of(self, d: Diagnostic) -> str | None:
        """Where a click on a diagnostic goes: its node, else that of the first key its text names."""
        return self.node_of(d) or self.node_of_key(next(iter(d.refs), ""))

    def node_of_key(self, key: str) -> str | None:
        """The case-tree node holding a key, an element or a section."""
        if not key:
            return None
        try:
            segments = self.doc.resolve(key)
        except (KeyError, ValueError):
            try:
                segments = [s for s in parse_path(key) if isinstance(s, str)]
            except ValueError:
                return None
        if not segments:
            return None
        head = segments[0]
        if head == "boundaries" and len(segments) > 1 and isinstance(segments[1], int):
            el = self.doc.get(f"boundaries[{segments[1]}]") or {}
            return f"zone:{el.get('name', segments[1])}"
        if isinstance(head, str) and len(segments) > 1 and isinstance(segments[1], int):
            return f"el:{head}[{segments[1]}]"
        return f"section:{self.section_of(str(head))}"

    @staticmethod
    def section_of(table: str) -> str:
        return next((sid for sid, _, tables in SECTIONS if table in tables), table)

    def spans_of(self, node_id: str) -> list[tuple[int, int]]:
        """The input's lines a case-tree node stands for: a section's tables, an element, a zone's condition."""
        kind, _, ident = node_id.partition(":")
        if kind == "section":
            tables = next((t for s, _, t in SECTIONS if s == ident), (ident,))
            return self.doc.spans_of([t for t in tables if t in self.doc.data])
        if kind == "zone":
            el = next((e for e in self.doc.elements("boundaries") if e.label == ident), None)
            return self.doc.spans_of([f"boundaries[{el.index}]"]) if el else []
        if kind in ("el", "region") and self.doc.has(ident):
            return self.doc.spans_of([ident])
        return []

    def node_at_line(self, line: int) -> str | None:
        """The case-tree node of what is written at a 1-based line of the input."""
        path = self.doc.path_at_line(line)
        return self.node_of_key(format_path(path)) if path else None

    # Editing

    def info(self, path: str) -> KeyInfo | None:
        """A key's catalog entry: the case's (whether the input sets it, what this case reads), with what only the
        general catalog says (dimensions, per-dimension defaults, descriptions) filled in from it, and the
        dimension from the input reference's Units table (`unit_keys`) where the catalog gives none; a key the
        reference gives as a plain number of a fixed unit (an angle in degrees) has no dimension to convert."""
        key = catalog_path(path)
        case, general = self.catalog.get(key), self.general.get(key)
        if case is None or general is None:
            merged = case or general
        else:
            merged = KeyInfo(
                **{**vars(general), **{k: v for k, v in vars(case).items() if v not in (None, [], {}, "")}}
            )
            merged.set = case.set
        if merged is not None and key in unit_keys.FIXED:  # a plain number in its unit: degrees, not radians
            merged = replace(merged, dimension=None)
        elif merged is not None and not merged.dimension and key in unit_keys.DIMENSIONS:
            merged = replace(merged, dimension=unit_keys.DIMENSIONS[key])
        return merged

    def show_unit(self, path: str, unit: str) -> None:
        """The unit a dimensional key's field shows; the input keeps its SI value."""
        info = self.info(path)
        found = dimension_of(info.dimension if info else None)
        if found is None or parse_unit(unit).dim != found[1]:
            raise ValueError(f"{unit} is not a unit of {found[0] if found else 'a dimensionless key'}")
        self.shown_units[path] = unit

    def required(self, path: str) -> bool:
        """Whether this case must set a key. The general catalog's `required` holds wherever a key is read at all
        (`initialize.file` only for a restart), so where the case reads the key's table its own catalog decides;
        the general catalog only speaks for a table the input has but the case's catalog does not know yet (an
        entry just added)."""
        key = catalog_path(path)
        parent = key.rpartition(".")[0]
        if self.case_catalog and parent in self.catalog:
            info = self.catalog.get(key)
            return bool(info and info.required)
        info = self.general.get(key)
        segments = parse_path(path)
        return bool(info and info.required) and len(segments) > 1 and self.doc.has(format_path(segments[:-1]))

    def children(self, path: str) -> list[str]:
        """The keys directly under a table, as the catalogs name them (`<name>` for a named entry), in their order."""
        index = self._index()
        return index.get(catalog_path(path), [])

    def _index(self) -> dict[str, list[str]]:
        stamp = (id(self.catalog), len(self.catalog), id(self.general), len(self.general))
        if getattr(self, "_index_stamp", None) != stamp:
            index: dict[str, list[str]] = {}
            for key in sorted({*self.general, *self.catalog}):
                parent, _, leaf = key.rpartition(".")
                index.setdefault(parent, []).append(leaf)
            self._index_cache, self._index_stamp = index, stamp
        return self._index_cache

    @property
    def case_dim(self) -> int | None:
        """The case's dimension: its executable's, else the check's."""
        if self.dim:
            return self.dim
        for report in (self.structure, self.report):
            if report is not None and report.dim:
                return int(report.dim)
        return None

    @property
    def nondimensional(self) -> bool:
        return bool(self.doc.get("physics.units") == "nondimensional")

    def form(self, path: str) -> str | None:
        """A table's form as the catalog gives it: "table", "array" (of tables) or "named"; None from builds
        whose catalog has no forms, and for keys that are not tables. The case's catalog leaves out the form of a
        table the input does not set; the general catalog has it."""
        key = catalog_path(path)
        for catalog in (self.catalog, self.general):
            info = catalog.get(key)
            if info is not None and info.form:
                return info.form
        return None

    def is_array(self, path: str) -> bool:
        """Whether a key is an array of tables: the input or the catalog's `form` says so; for catalogs without
        forms, a key with keys of its own whose type is an array, or that has `name` or `region` keys."""
        value = self.doc.get(path)
        if isinstance(value, list) and value:
            return all(isinstance(e, dict) for e in value)
        if isinstance(value, dict):
            return False
        form = self.form(path)
        if form is not None:
            return form == "array"
        kids = self.children(path)
        if not kids:
            return False
        info = self.info(path)
        return (info is not None and info.type == "array") or any(k in kids for k in ("name", "region"))

    def is_named(self, path: str) -> bool:
        """Whether a table holds entries under names the input chooses (`[regions] name = {...}`)."""
        return self.form(path) == "named" or any(k.startswith("<") for k in self.children(path))

    def is_table(self, path: str) -> bool:
        """Whether a key is a table of keys the inspector lists one by one (not a value, an array or named)."""
        value = self.doc.get(path)
        if isinstance(value, dict):
            return not self._is_leaf_table(path)
        if value is not None:
            return False
        return bool(self.children(path)) and not self.is_array(path)

    def set_value(self, path: str, text: str, unit: str | None = None) -> None:
        """Set a key from a field's text, typed by the catalog, and save the file in place. A dimensional key
        takes a quantity (`1 cm`) or a number in the selected `unit`."""
        info = self.info(path)
        text = text.strip()
        if text == "" and self.doc.has(path):
            self.doc.delete(path)
        elif text != "":
            if info is not None and info.dimension and unit is not None:
                try:
                    value, used = store(text, unit, info.dimension, self.nondimensional)
                except UnitError as e:
                    raise ValueError(str(e)) from None
                if used:
                    self.shown_units[path] = used
                if isinstance(value, str) and info.type == "number" and value == text:
                    value = parse_value(text, None)  # an expression, as typed
            else:
                value = parse_value(text, info.type if info else None)
            self.doc.set(path, value)
            segments = parse_path(path)
            where = choices.branch_of(catalog_path(format_path(segments[:-1])), str(segments[-1]))
            if where is not None and len(segments) > 1:
                self.oneof_pick[(format_path(segments[:-1]), where[0])] = where[1]
        self.doc.commit()

    def _vector_unit(self, path: str, info: KeyInfo | None) -> tuple[str, list[str]] | None:
        """The unit a dimensional vector's boxes show, one for every component, and the units offered."""
        if info is None or self.nondimensional:
            return None
        f = field_of(None, info.dimension, False, self.preferred_units, self.shown_units.get(path, ""))
        return (f.unit, f.choices) if f is not None and f.choices else None

    def _shown(self, v: Any, info: KeyInfo, unit: str) -> str:
        found = dimension_of(info.dimension)
        if found and isinstance(v, (int, float)) and not isinstance(v, bool):
            return units.from_si(v, unit, found[1])
        return display_value(v)

    def set_vector(self, path: str, texts: list[str], unit: str = "") -> list[str]:
        """A vector key from its components as typed, numbers in `unit` (or each in a unit typed after it), written
        in SI: the TOML array once every one is valid (all empty: the key goes); else nothing is written and each
        component's problem is returned."""
        vec = vector_of(catalog_path(path), None, len(texts))
        expressions, integer = bool(vec and vec.expressions), bool(vec and vec.integer)
        if all(not t.strip() for t in texts):
            if self.doc.has(path):
                self.doc.delete(path)
                self.doc.commit()
            return [""] * len(texts)
        info = self.info(path)
        if unit and not integer and info is not None and info.dimension and not self.nondimensional:
            parsed = [self._si_component(path, t, unit, info.dimension, expressions) for t in texts]
        else:
            parsed = [component(t, expressions, integer) for t in texts]
        errors = [e for _, e in parsed]
        if any(errors):
            return errors
        self.doc.set(path, [v for v, _ in parsed])
        self.doc.commit()
        return errors

    def _si_component(self, path: str, text: str, unit: str, dimension: str, expressions: bool) -> tuple[Any, str]:
        """A dimensional component in SI: a number in `unit`, or with its own unit (which the boxes then show)."""
        if not text.strip():
            return None, "needed"
        try:
            value, used = store(text, unit, dimension, False)
        except UnitError as e:
            return None, str(e)
        if isinstance(value, str):
            return component(text, expressions)
        if used and (used != unit or path not in self.shown_units):
            self.shown_units[path] = used
        return value, ""

    def set_points(self, path: str, rows: list[list[str]], unit: str = "") -> list[list[str]]:
        """A list of points from its rows of components as typed (numbers in `unit`, written in SI): blank rows
        dropped, the array written once every component of the others is a number (none left: the key goes); else
        each component's problem."""
        rows = [r for r in rows if any(t.strip() for t in r)]
        info = self.info(path)
        if unit and info is not None and info.dimension and not self.nondimensional:
            parsed = [[self._si_component(path, t, unit, info.dimension, False) for t in r] for r in rows]
        else:
            parsed = [[component(t, False) for t in r] for r in rows]
        errors = [[e for _, e in r] for r in parsed]
        if any(any(r) for r in errors):
            return errors
        if rows:
            self.doc.set(path, [[v for v, _ in r] for r in parsed])
        elif self.doc.has(path):
            self.doc.delete(path)
        else:
            return errors
        self.doc.commit()
        return errors

    # Compositions: a table's X or Y (Mallard takes exactly one), one undo step per edit

    def composition(self, table: str) -> tuple[str, dict[str, Any]]:
        value = self.doc.get(table)
        value = value if isinstance(value, dict) else {}
        basis = next((b for b in choices.BASES if b in value), None) or self.comp_basis.get(table, "X")
        fractions = value.get(basis)
        return basis, dict(fractions) if isinstance(fractions, dict) else {}

    def write_composition(self, table: str, basis: str, fractions: dict[str, Any]) -> None:
        if basis not in choices.BASES:
            raise ValueError(f"{basis}: a composition is X (mole fractions) or Y (mass fractions)")
        for b in choices.BASES:
            if self.doc.has(f"{table}.{b}"):
                self.doc.delete_entry(f"{table}.{b}")
        if fractions:
            self.doc.set(f"{table}.{basis}", fractions)
        self.comp_basis[table] = basis
        self.doc.commit()

    def set_fraction(self, table: str, species: str, text: str) -> None:
        """A species' fraction as typed (a number or an expression); empty removes the species."""
        basis, f = self.composition(table)
        if text.strip():
            f[species] = choices.parse_fraction(text)
        else:
            f.pop(species, None)
        self.write_composition(table, basis, f)

    def add_species(self, table: str, species: str) -> None:
        basis, f = self.composition(table)
        if species and species not in f:
            f[species] = 0.0
            self.write_composition(table, basis, f)

    def rename_species(self, table: str, old: str, new: str) -> None:
        basis, f = self.composition(table)
        if not new or new == old or old not in f:
            return
        if new in f:
            raise ValueError(f"{new} is in the composition already")
        self.write_composition(table, basis, {(new if k == old else k): v for k, v in f.items()})

    def normalize_composition(self, table: str) -> None:
        basis, f = self.composition(table)
        self.write_composition(table, basis, choices.normalized(f))

    def switch_basis(self, table: str, basis: str, clear: bool = False) -> bool:
        """Give the composition as `basis` (X or Y): converted with the species' molar masses where Mallard gives
        them, else only with `clear` (the values go); False when it needs that choice."""
        old, f = self.composition(table)
        if basis == old:
            return True
        if not f:
            self.comp_basis[table] = basis
            return True
        new = choices.converted(f, basis, self._molar_masses())
        if new is None and not clear:
            return False
        self.write_composition(table, basis, new or {})
        return True

    def set_text(self, text: str) -> None:
        self.doc.set_text(text)
        self.doc.commit()

    def assign_zone(self, zone: str, condition: str) -> None:
        """Give a zone without a condition its `[[boundaries]]` element."""
        self.doc.append_element("boundaries", {"name": zone, "type": condition})
        self.doc.commit()

    # The document: saved, saved as, reverted

    def save(self) -> None:
        self.doc.save()

    def save_as(self, path: Path, copy_project: bool = False) -> bool:
        """Write the input to `path`. In this project's directory it becomes the case's input (and one of the
        project's inputs); elsewhere, with `copy_project`, the project's files are copied beside it first so its
        relative paths hold. Returns whether the input is still in this project."""
        path = path.resolve()
        if path.parent == self.project.root:
            self.doc.save(path)
            self.input_path = path
            if path.name not in self.project.inputs:
                self.project.data["inputs"] = [*self.project.inputs, path.name]
                self.project.save()
            recent.add(path)
            return True
        if copy_project:
            copy_project_files(self.project, path.parent)
        self.doc.save(path)
        recent.add(path)
        return False

    def disk_change(self) -> str | None:
        """The input's text on disk when another program changed it since this session last read or wrote it
        (`""` when the file is gone), else None."""
        try:
            disk = self.input_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except (OSError, UnicodeDecodeError):
            return None
        return None if disk == self.doc.saved_text else disk

    def reload_from_disk(self) -> None:
        """The file as it is on disk, as one undo step (Undo brings back what was here)."""
        self.doc.revert()

    def keep_mine(self, disk: str) -> None:
        """Keep the text here: it now differs from the file (`disk`), so it is unsaved until the next Save."""
        self.doc.saved_text = disk
        self.doc.keep_recovery()

    def require_saved(self) -> None:
        """Runs start from the input on disk: never from something other than what the user sees."""
        if self.doc.dirty:
            raise UnsavedChanges(f"{self.input_path.name} has unsaved changes: save it first")

    # The case tree

    def zones(self) -> list[dict[str, Any]]:
        """Boundary zones: from the check (which knows the mesh), else from the input."""
        known: dict[str, dict[str, Any]] = {}
        if self.structure:
            for b in self.structure.boundaries:
                known[b["zone"]] = {"zone": b["zone"], "condition": b.get("condition", ""), "faces": b.get("faces")}
            for r in self.structure.regions:
                if r.get("of_mesh") and r["name"] != "domain" and r.get("dimension") == (self.structure.dim or 3) - 1:
                    known.setdefault(r["name"], {"zone": r["name"], "condition": "", "faces": r.get("cells")})
        # Zones without a condition, which the check names also when the setup stops on them
        for u in (self.report.data.get("unassigned") or []) if self.report else []:
            if isinstance(u, dict) and "zone" in u:
                known.setdefault(str(u["zone"]), {"zone": str(u["zone"]), "condition": "", "faces": u.get("faces")})
        for el in self.doc.elements("boundaries"):
            entry = known.setdefault(el.label, {"zone": el.label, "condition": el.kind, "faces": None})
            entry["condition"] = entry["condition"] or el.kind
            entry["index"] = el.index
        return list(known.values())

    def tree(self) -> list[dict[str, Any]]:
        counts: dict[str, list[int]] = {}
        for d in self.diagnostics:
            at = self.node_of(d)
            if at is None:
                continue
            c = counts.setdefault(at, [0, 0])
            c[0 if d.severity == "error" else 1] += d.severity in ("error", "warning")
            if at.startswith(("zone:", "el:")):
                parent = (
                    "section:boundaries"
                    if at.startswith("zone:")
                    else f"section:{self.section_of(at[3:].split('[')[0])}"
                )
                pc = counts.setdefault(parent, [0, 0])
                pc[0 if d.severity == "error" else 1] += 1

        def node(
            nid: str, label: str, value: str, depth: int, kind: str, path: str | None, children: int = 0
        ) -> dict[str, Any]:
            e, w = counts.get(nid, [0, 0])
            return {
                "id": nid,
                "label": label,
                "value": value,
                "depth": depth,
                "kind": kind,
                "path": path,
                "errors": e,
                "warnings": w,
                "children": children,
            }

        data = self.doc.data
        out: list[dict[str, Any]] = []
        tops = {k for k in (*self.catalog, *self.general) if "." not in k}
        listed = {t for _, _, tables in SECTIONS for t in tables} | NOT_SECTIONS
        groups: list[tuple[str, list[tuple[str, str, tuple[str, ...]]]]] = [
            (g, [sec for sec in secs if sec[0] in CORE_SECTIONS or any(t in tops or t in data for t in sec[2])])
            for g, secs in GROUPS
        ]
        # Every section the catalogs know, set or not: nothing is reachable only by typing TOML.
        extra = [t for t in data if t not in listed and isinstance(data[t], (dict, list))]
        extra += sorted(
            k for k in tops if k not in listed and k not in extra and not isinstance(data.get(k), (str, int, float))
        )
        groups.append((OTHER_GROUP, [(t, t, (t,)) for t in extra]))
        for group, sections in groups:
            for n, (sid, title, tables) in enumerate(sections):
                kids: list[dict[str, Any]] = []
                if sid == "boundaries":
                    for z in self.zones():
                        zid = z["zone"]
                        path = f'boundaries[name = "{zid}"]' if "index" in z else None
                        kids.append(node(f"zone:{zid}", zid, z["condition"] or "no condition", 1, "zone", path))
                    summary = str(len(kids))
                elif sid == "regions":
                    kids = self._region_nodes(node)
                    summary = str(len(kids))
                else:
                    for t in tables:
                        for el in self.doc.elements(t):
                            label = el.label.rstrip("/").rsplit("/", 1)[-1]
                            value = el.kind or (t if len(tables) > 1 else "")
                            kids.append(node(f"el:{t}[{el.index}]", label, value, 1, "element", f"{t}[{el.index}]"))
                    summary = _summary(data, tables, len(kids))
                present = any(t in data for t in tables) or bool(kids)
                head = node(
                    f"section:{sid}",
                    title,
                    summary if present else "",
                    0,
                    "section",
                    tables[0] if tables[0] in data and not isinstance(data[tables[0]], list) else None,
                    len(kids),
                )
                head.update(group=group, group_head=n == 0, present=present)
                if not kids and sid != "boundaries" and any(self.is_array(t) or self.is_named(t) for t in tables):
                    head["value"] = head["value"] or "none"  # a list with no entries: none, not unset
                if all(t in FEATURES for t in tables):  # a section with a list (Sources) has no one switch
                    head["feature"] = "on" if any(is_on(FEATURES[t], data.get(t)) for t in tables) else "off"
                out.append(head)
                for kid in kids:
                    kid.update(group=group, parent=head["id"], present=True)
                out.extend(kids)
        return out

    def _region_nodes(self, node: Any) -> list[dict[str, Any]]:
        """The named regions, the inline ones wherever a feature uses one, and the mesh's own."""
        kids = []
        seen = set()
        for e in setup_regions.regions(self.doc):
            kind = e.value.get("type", "") if isinstance(e.value, dict) else "name"
            kids.append(node(f"region:{e.path}", e.label, kind or "inline", 1, "region", e.path))
            seen.add(e.path)
        for r in self.structure.regions if self.structure else []:
            name = r["name"]
            if r.get("of_mesh"):
                kids.append(node(f"region:{name}", name, "mesh" if name == "domain" else "zone", 1, "region", None))
            elif canonical(self.doc, name) not in seen and f"regions.{name}" not in seen:
                kids.append(node(f"region:{name}", label_of(name), "check", 1, "region", None))
        return kids

    # Properties of the selected node

    def properties(self, node_id: str) -> dict[str, Any]:
        kind, _, ident = node_id.partition(":")
        groups: list[Group] = []
        title, subtitle, path = ident, "", None
        if kind == "section":
            sid = ident
            tables = next((t for s, _, t in SECTIONS if s == sid), (sid,))
            title = next((name for s, name, _ in SECTIONS if s == sid), sid)
            for t in tables:
                if self.is_array(t):
                    groups.append(self._entries_group(t))
                elif self.is_named(t):
                    groups.append(self._named_group(t))
                else:
                    groups += self._table_groups(t, f"[{t}]")
            if sid == "boundaries":
                groups.insert(
                    0,
                    Group(
                        "ZONES",
                        [
                            Row(
                                f"zone:{z['zone']}",
                                "",
                                f"{z['zone']} \u00b7 {z['condition'] or 'no condition'}",
                                True,
                                readonly=True,
                            )
                            for z in self.zones()
                        ],
                        note="from check",
                    ),
                )
                groups = [g for g in groups if g.id != "boundaries"]
            if sid == "regions":
                self._other_regions(groups)
            path = tables[0]
        elif kind == "zone":
            groups, path = self._zone_groups(ident)
            title, subtitle = ident, "boundary zone"
        elif kind == "el":
            path = ident
            el = self.doc.get(path) or {}
            title = str(el.get("name") or el.get("zone") or el.get("prefix") or ident)
            subtitle = ident
            groups = self._table_groups(path, f"[[{path.split('[')[0]}]] {path[path.index('[') :]}")
            if not groups or groups[0].id != path:
                groups.insert(0, Group(f"[[{path.split('[')[0]}]] {path[path.index('[') :]}", [], id=path))
            groups[0].entry = path  # the entry shown can be duplicated and deleted where it is shown
        elif kind == "region":
            groups, path = self._region_groups(ident)
            title, subtitle = label_of(ident.removeprefix("regions.")), ident
        self._feature_switches(groups)
        line = self.doc.line_of(path) if path else None
        return {
            "id": node_id,
            "title": title,
            "subtitle": subtitle,
            "path": path,
            "line": line,
            "groups": [g.to_dict() for g in groups],
        }

    def _row(self, path: str, value: Any, set_: bool) -> Row:
        info = self.info(path)
        leaf = str(parse_path(path)[-1])
        row = Row(path=path, label=leaf, value=display_value(value) if set_ else "", set=set_)
        dim = self.case_dim
        preset = self._preset_for(path)
        effective = preset.effective.get(catalog_path(path)) if preset and "[" not in path else None
        if info:
            row.type, row.widget, row.unit, row.options = info.type, info.widget, info.unit, info.values
            row.models = {v: {"url": m.url, "tip": m.tip(v), "cite": m.cite} for v, m in info.models.items()}
            row.required = self.required(path)
            default = bare_literal(info.default_for(dim))
            if not set_ and not row.required:
                row.unset = choices.unset_meaning(default, info.description, info.unset)
            if not set_ and default is not None and not row.unset:
                row.placeholder = default
            if not info.applies_in(dim):
                row.other_dim = " & ".join(f"{d}D" for d in info.dims) + " only"
            row.help = {
                "path": catalog_path(path),
                "type": info.type or "",
                "default": info.default_for(dim) or ("required" if row.required else ""),
                "units": info.dimension or "",
                "values": ", ".join(info.values),
                "dims": " & ".join(f"{d}D" for d in info.dims),
                "description": info.description,
                "when": info.when,
            }
        elif isinstance(value, (dict, list)):
            row.widget = "toml"
        if not row.help:
            row.help = {
                "path": catalog_path(path),
                "type": "",
                "default": "",
                "units": "",
                "values": "",
                "dims": "",
                "description": "not in the key catalog: mallard check says whether this build reads it",
                "when": "",
            }
        if preset:
            row.origin = preset.origin(catalog_path(path))
            if effective is not None and not set_:
                row.placeholder = display_value(effective)
            if row.origin:
                fill = preset.fills.get(preset.active or "", {}).get(catalog_path(path))
                row.help = {**row.help, "preset": f"{preset.active}: {display_value(fill)}" if fill is not None else ""}
        form = self.form(path)
        if form:
            row.type, row.widget = form, "toml"
            row.placeholder = row.placeholder or FORM_PLACEHOLDERS[form]
        if info and info.dimension and row.widget in ("number", "text") and not isinstance(value, (list, dict)):
            f = field_of(
                value if set_ else None,
                info.dimension,
                self.nondimensional,
                self.preferred_units,
                self.shown_units.get(path, ""),
            )
            if f is not None and not f.expression:
                row.widget, row.number, row.unit_choice, row.unit_options = "quantity", f.number, f.unit, f.choices
                row.unit = "" if self.nondimensional else row.unit
                if not set_:
                    row.number = ""
                    row.placeholder = _in_unit(row.placeholder, info.dimension, f.unit)
            if f is not None and f.error:
                row.error, row.severity = f.error, "error"
        if self.nondimensional:
            row.unit = ""
        key = catalog_path(path)
        if key in unit_keys.FIXED:
            row.unit = unit_keys.FIXED[key]
        row.unit_unknown = "" if info and info.dimension else unit_keys.UNKNOWN.get(key, "")
        self._choices(row, info, leaf, value if set_ else None)
        if row.unset and (row.placeholder or form or row.widget == "bool"):
            row.unset = ""
        elif row.unset:
            row.placeholder = row.unset
            row.help = {**row.help, "default": "none" + row.unset.removeprefix("not set")}
        fields = self.field_catalog(path)
        if fields is not None and (not set_ or (isinstance(value, list) and all(isinstance(v, str) for v in value))):
            row.widget = "fields"
            row.fields = [
                {"name": e, "problem": fields.problem(e), "covers": ", ".join(fields.covered(e))}
                for e in (value if set_ else [])
            ]
        vec = vector_of(
            catalog_path(path),
            value if set_ else None,
            self.case_dim or 0,
            bool(info and info.type == "array" and info.dimension),
        )
        if vec is not None and row.widget not in ("fields", "pick"):
            values = [display_value(v) for v in value] if set_ and isinstance(value, list) else [""] * len(vec.labels)
            placeholders = _components(row.placeholder, len(vec.labels))
            shown = None if vec.integer else self._vector_unit(path, info)
            if shown and info is not None:
                if set_ and isinstance(value, list):
                    values = [self._shown(v, info, shown[0]) for v in value]
                placeholders = [_in_unit(p, info.dimension or "", shown[0]) for p in placeholders]
                row.unit = ""
            row.widget = "vector"
            row.vector = {
                "unit": shown[0] if shown else "",
                "units": shown[1] if shown else [],
                "labels": list(vec.labels),
                "values": values,
                "placeholders": placeholders,
                "expressions": vec.expressions,
                "errors": [""] * len(vec.labels),
            }
        labels = point_list_of(catalog_path(path), value if set_ else None, self.case_dim or 0)
        if labels is not None and row.widget not in ("fields", "pick"):
            row.widget = "points"
            shown = self._vector_unit(path, info)
            rows = value if set_ and isinstance(value, list) else []
            row.points = {
                "labels": list(labels),
                "rows": [
                    [self._shown(c, info, shown[0]) if shown and info else display_value(c) for c in p] for p in rows
                ],
                "unit": shown[0] if shown else "",
                "units": shown[1] if shown else [],
            }
            if shown:
                row.unit = ""
        d = self.diagnostic_for(path)
        if d:
            row.error, row.severity = f"{d.code}: {d.message}", d.severity
        return row

    def _choices(self, row: Row, info: KeyInfo | None, leaf: str, value: Any) -> None:
        """A dropdown of the names a key refers to (zones, regions, species), true and false for a boolean, the
        file dialog for a file."""
        if row.widget not in ("text", "number", "bool"):
            return
        if row.widget == "bool" or (info is not None and info.type == "bool"):
            row.widget = "bool"
            return
        if isinstance(value, (dict, list)):
            return
        kind = choices.reference(leaf)
        names = self.names(kind) if kind else []
        if names:
            row.widget, row.options, row.free = "select", names, kind != "species"
        elif choices.is_file_key(leaf, info.type if info else None, info.default_value if info else None, value):
            row.file = True

    def names(self, kind: str) -> list[str]:
        """The names defined in the case of a kind: its zones, its regions (named and the mesh's), the species of
        its mechanism."""
        if kind == "zones":
            return [z["zone"] for z in self.zones()]
        if kind == "regions":
            named = self.doc.get("regions")
            out = list(named) if isinstance(named, dict) else []
            out += [str(r["name"]) for r in (self.structure.regions if self.structure else []) if r["name"] not in out]
            return out
        if kind == "species":
            return list(self.species.names) if self.species else []
        return []

    def _composition_row(self, path: str, value: dict[str, Any]) -> Row | None:
        """One editor for a table's X and Y (Mallard takes exactly one), or None when one is not a table of
        species (left as their own rows)."""
        given = {b: value[b] for b in choices.BASES if b in value}
        if any(not isinstance(v, dict) for v in given.values()):
            return None
        basis = next(iter(given), None) or self.comp_basis.get(path, "X")
        fractions = given.get(basis, {})
        species = self.names("species")
        known = set(species)
        s = choices.total(fractions)
        key = f"{path}.{basis}"
        row = self._row(key, fractions or None, bool(given))
        row.label, row.widget, row.value, row.placeholder = "composition", "composition", "", ""
        row.comp = {
            "table": path,
            "basis": basis,
            "entries": [
                {
                    "species": k,
                    "value": display_value(v),
                    "unknown": bool(species) and k not in known,
                    "number": choices.number(v) is not None,
                }
                for k, v in fractions.items()
            ],
            "sum": "" if s is None else f"{s:.6g}",
            "normal": s is not None and abs(s - 1.0) < 1e-9,
            "species": [k for k in species if k not in fractions],
            "species_error": self.species_error,
            "convertible": bool(fractions) and choices.converted(fractions, "Y", self._molar_masses()) is not None,
        }
        if len(given) == 2:
            row.error, row.severity = f"{path}: give the composition as exactly one of X and Y, not both", "error"
        unknown = [e["species"] for e in row.comp["entries"] if e["unknown"]]
        if unknown and not row.error:
            row.error = f"the mechanism has no species {', '.join(unknown)}"
            row.severity = "error"
        row.help = {**row.help, "path": f"{catalog_path(path)}.X | .Y", "type": "composition",
                    "description": "mole (X) or mass (Y) fractions by species; Mallard normalizes them"}  # fmt: skip
        return row

    def _molar_masses(self) -> dict[str, float]:
        return self.species.molar_masses if self.species else {}

    def _table_groups(self, path: str, title: str, depth: int = 0) -> list[Group]:
        """A table's groups: its keys, every one the catalogs know whether the input sets it or not, then each
        table under it (collapsed while it sets nothing), each array of tables under it with its entries, and each
        named table with its names."""
        value = self.doc.get(path)
        if not isinstance(value, dict):
            value = {}
        leaves = [k for k in self.children(path) if not k.startswith("<")]
        leaves += [k for k in value if k not in leaves]
        rows: list[Row] = []
        nested: list[Group] = []
        comp = self._composition_row(path, value) if all(b in leaves for b in choices.BASES) else None
        unread: list[Row] = []  # keys only another dimension reads: not offered, a set one flagged
        reads = self.reads(path)
        for k in leaves:
            sub = f"{path}.{k}"
            if reads is not None and k not in reads:
                if k in value:
                    row = self._row(sub, value[k], True)
                    kind = value.get("type")
                    row.error = row.error or (
                        f"{f'a {kind} entry' if kind else 'this entry'} does not read {k} "
                        "(mallard check reports it unused): Reset removes it"
                    )
                    row.severity = row.severity or "warning"
                    rows.append(row)
                continue
            if comp is not None and k in choices.BASES:
                if comp not in rows:
                    rows.append(comp)
                continue
            if not self.applies(sub):
                row = self._row(sub, value.get(k), k in value) if k in value else Row(sub, k, "", False)
                row.readonly, row.widget = True, "text"
                row.other_dim = row.other_dim or self._dims_text(sub)
                if k in value:
                    row.error = (
                        f"a {self.case_dim}D case does not read {k} (mallard check reports it unused): Reset removes it"
                    )
                    row.severity = row.severity or "warning"
                    rows.append(row)
                else:
                    row.value = row.other_dim
                    unread.append(row)
                continue
            if self.is_array(sub):
                nested += self._entries_groups(sub, depth + 1)
            elif self.is_named(sub):
                nested.append(self._named_group(sub, depth + 1))
            elif self.is_table(sub):
                nested += self._table_groups(sub, format_path(parse_path(sub)[-1:]), depth + 1)
            else:
                rows.append(self._row(sub, value.get(k), k in value))
        missing = {r.path for r in rows}
        rows += [r for r in self._missing_rows(path, value) if r.path not in missing]
        rows, alternatives = self._alternatives(path, rows, depth)
        nested[:0] = alternatives
        note = f"{sum(r.set for r in rows)} of {len(rows)} options set" if rows else ""
        own = Group(title, rows, note=note, id=path, depth=depth, collapsed=depth > 0 and not value)
        if unread:
            nested.append(
                Group(
                    f"not read in {self.case_dim}D",
                    unread,
                    note=str(len(unread)),
                    id=f"{path}#unread",
                    depth=depth + 1,
                    collapsed=True,
                )
            )
        return [own, *nested] if rows or not nested else nested

    def _alternatives(self, path: str, rows: list[Row], depth: int) -> tuple[list[Row], list[Group]]:
        """Keys Mallard takes one of (`dt` or `cfl`): each choice a group of its own under a selector, the one in
        use open, the others collapsed as not used; a key set in an unused one is a conflict to clear."""
        groups: list[Group] = []
        for n, branches in enumerate(choices.ALTERNATIVES.get(catalog_path(path), ())):
            parts = [[r for r in rows if r.path and str(parse_path(r.path)[-1]) in keys] for _, keys in branches]
            present = [i for i, p in enumerate(parts) if p]
            if len(present) < 2:
                continue
            set_in = [i for i in present if any(r.set for r in parts[i])]
            pick = self.oneof_pick.get((path, n))
            if pick in present and (pick in set_in or not set_in):
                active = pick
            elif set_in:
                active = set_in[0]
            else:
                active = next((i for i in present if any(r.placeholder and not r.unset for r in parts[i])), present[0])
            taken = {id(r) for p in parts for r in p}
            rows = [r for r in rows if id(r) not in taken]
            ident = f"{path}#oneof{n}"
            selector = Row("", "", branches[active][0], True, widget="oneof")
            selector.oneof = {
                "table": path,
                "n": n,
                "labels": [branches[i][0] for i in present],
                "branches": present,
                "active": active,
                "conflict": [r.path for i in set_in if i != active for r in parts[i] if r.set],
                "clear": [r.path for r in parts[active] if r.set],
            }
            title = " or ".join(branches[i][0] for i in present)
            groups.append(Group(title, [selector], note="one of", id=ident, depth=depth + 1))
            for i in present:
                groups.append(
                    Group(
                        branches[i][0],
                        parts[i],
                        note="" if i == active else "not used",
                        collapsed=i != active,
                        id=f"{ident}.{i}",
                        depth=depth + 2,
                    )
                )
        return rows, groups

    def pick_alternative(self, table: str, n: int, branch: int, clear: bool = False) -> bool:
        """Use another of a table's alternatives; False, changing nothing, while the one in use sets keys and
        `clear` is not given (they are cleared with it, as one undo step)."""
        branches = choices.ALTERNATIVES.get(catalog_path(table), ())
        if not 0 <= n < len(branches) or not 0 <= branch < len(branches[n]):
            return False
        value = self.doc.get(table)
        value = value if isinstance(value, dict) else {}
        others = [k for b, (_, keys) in enumerate(branches[n]) if b != branch for k in keys if k in value]
        if others and not clear:
            return False
        for k in others:
            self.doc.delete(f"{table}.{k}")
        if others:
            self.doc.commit()
        self.oneof_pick[(table, n)] = branch
        return True

    def applies(self, path: str) -> bool:
        """Whether the case's dimension reads a key. The general catalog's `dims` are the dimensions its inputs
        read a key in, so a dimension none of them covers may read it still (2D reads `forces.zone`, dims [3]):
        the case's own catalog overrides them where it read the key typed (a type, default or `required`), not
        merely listed it (`write_data.boundary` in 2D: no type, and check reports it unused)."""
        info = self.info(path)
        if info is None or info.applies_in(self.case_dim):
            return True
        case = self.catalog.get(catalog_path(path)) if self.case_catalog else None
        return case is not None and bool(case.type or case.default is not None or case.required or case.unset)

    def _dims_text(self, path: str) -> str:
        info = self.info(path)
        return " & ".join(f"{d}D" for d in info.dims) + " only" if info and info.dims else ""

    def _entries_group(self, array: str, depth: int = 0) -> Group:
        """An array of tables as links to its entries, each of which can be duplicated or deleted; and Add."""
        rows = [
            Row(
                f"{array}[{el.index}]",
                "",
                f"{el.label}" + (f" · {el.kind}" if el.kind else ""),
                True,
                readonly=True,
                entry=f"{array}[{el.index}]",
            )
            for el in self.doc.elements(array)
        ]
        n = len(rows)
        return Group(
            f"[[{array}]]",
            rows,
            note=f"entries {n}" if n else "none yet",
            id=array,
            depth=depth,
            add=self._add_spec(array),
        )

    def _entries_groups(self, array: str, depth: int) -> list[Group]:
        """An array of tables inside a table: each entry's keys in place, under a heading that adds entries."""
        head = Group(f"[[{format_path(parse_path(array)[-1:])}]]", [], id=array, depth=depth, add=self._add_spec(array))
        entries = self.doc.get(array) or []
        out = [head]
        for i, el in enumerate(entries if isinstance(entries, list) else []):
            if not isinstance(el, dict):
                continue
            label = next((str(el[k]) for k in ("name", "type") if k in el), "")
            groups = self._table_groups(f"{array}[{i}]", f"[{i}] {label}".strip(), depth + 1)
            if groups:
                groups[0].entry = f"{array}[{i}]"
                groups[0].collapsed = False
            out += groups
        head.note = f"entries {len(entries)}" if entries else "none yet"
        return out

    def _other_regions(self, groups: list[Group]) -> None:
        """The regions the tree lists besides `[regions]`'s names (inline ones where a feature uses one, the
        mesh's own) as links in the `[regions]` group: the case has them though `[regions]` is empty."""
        head = next((g for g in groups if g.id == "regions"), None)
        if head is None:
            return
        named = {r.path for r in head.rows}
        kids = self._region_nodes(lambda nid, label, value, *_a, **_k: (nid, label, value))
        head.rows += [
            Row(nid, "", f"{label} \u00b7 {value}", True, readonly=True)
            for nid, label, value in kids
            if nid.removeprefix("region:") not in named
        ]
        if head.rows:
            head.note = f"regions {len(head.rows)}"

    def _named_group(self, table: str, depth: int = 0) -> Group:
        """A table of named entries (`[regions]`): links to each name, and Add."""
        value = self.doc.get(table)
        names = list(value) if isinstance(value, dict) else []
        rows = [Row(f"{table}.{n}", "", n, True, readonly=True, entry=f"{table}.{n}") for n in names]
        return Group(
            table.upper() if depth == 0 else format_path(parse_path(table)[-1:]),
            rows,
            note=f"entries {len(names)}" if names else "none yet",
            id=table,
            depth=depth,
            add=self._add_spec(table),
        )

    def _add_spec(self, table: str) -> dict[str, Any]:
        """What Add offers for an array or named table: its entries' `type` values, from the catalog."""
        if not self.applies(table):
            return {"path": "", "types": []}
        key = f"{catalog_path(table)}{'.<name>' if self.is_named(table) else ''}.type"
        info = self.catalog.get(key) or self.general.get(key)
        return {"path": table, "types": list(info.values) if info else []}

    def _missing_rows(self, path: str, value: dict[str, Any]) -> list[Row]:
        """Keys a diagnostic names in this table that the input does not set (a missing required key)."""
        try:
            base = self.doc.resolve(path)
        except (KeyError, ValueError):
            return []
        rows = []
        for d in self.diagnostics:
            if not d.key:
                continue
            try:
                got = self.doc.resolve(d.key)
            except (KeyError, ValueError):
                continue
            if len(got) == len(base) + 1 and got[: len(base)] == base and got[-1] not in value:
                row = self._row(f"{path}.{got[-1]}", None, False)
                row.error, row.severity = f"{d.code}: {d.message}", d.severity
                rows.append(row)
        return rows

    def _is_leaf_table(self, path: str) -> bool:
        """An inline table the catalog declares as one value (a region, a reference state); a table with a
        `form` is read key by key, so its keys are rows of their own."""
        info = self.catalog.get(catalog_path(path))
        return info is not None and info.type == "table" and not self.form(path)

    # Presets, as Mallard resolves them

    def preset_keys(self) -> list[str]:
        """The catalogs' preset keys: any key named `preset` that takes values (`numerics.preset`)."""
        out = []
        for k in sorted({*self.catalog, *self.general}):
            if k.rsplit(".", 1)[-1] != "preset" or "<" in k:
                continue
            info = self.info(k)
            if info is not None and info.values:
                out.append(k)
        return out

    @property
    def presets_fresh(self) -> bool:
        return bool(self.presets) and self._presets_text == self.doc.text

    def wants_presets(self, node_id: str) -> bool:
        """Whether a node shows keys a preset may fill (its table's section, or a table it fills keys of) and
        they are not resolved for the text as it is."""
        if self.presets_fresh or not self.preset_keys():
            return False
        kind, _, ident = node_id.partition(":")
        if kind != "section":
            return False
        tables = next((t for s, _, t in SECTIONS if s == ident), (ident,))
        heads = {k.split(".", 1)[0] for k in self.preset_keys()}
        filled = {k.split(".", 1)[0] for v in self.presets.values() for f in v.fills.values() for k in f}
        return any(t in heads | filled or t == "run" for t in tables)

    def _scratch_runner(self, run: Callable[[Path], Any]) -> Callable[[str], Any]:
        """`run` on a text written to a hidden scratch input beside the case's (its relative paths hold)."""
        import os
        import threading

        def go(text: str) -> Any:
            path = self.input_path.with_name(
                f".{self.input_path.stem}.scratch-{os.getpid()}-{threading.get_ident()}.toml"
            )
            path.write_text(text, encoding="utf-8")
            try:
                return run(path)
            except SetupError as e:  # named as the user knows it, not by its scratch copy
                raise SetupError(str(e).replace(path.name, self.input_path.name)) from None
            finally:
                path.unlink(missing_ok=True)

        return go

    def refresh_presets(self) -> dict[str, PresetView]:
        """Resolve each preset key's presets for the text as it is (several `mallard input resolve`s)."""
        text = self.doc.text
        logs = [
            self.project.root / str(r.home.get("directory", f"runs/{r.name}")) / "mallard.jsonl"
            for r in self.project.runs()
        ]
        recorded = run_log_presets(logs, self._log_presets)
        resolve = self._scratch_runner(self.mallard.resolve)
        views = {}
        for key in self.preset_keys():
            info = self.info(key)
            views[key] = compute_presets(
                text, key, info.values if info else [], resolve, self._preset_line(key), recorded
            )
        self.presets, self._presets_text = views, text
        return views

    def _preset_line(self, key: str) -> Callable[[str], str | None]:
        """The Preset row of `mallard check` for a text, which sets the case up (the mesh, its partition): once
        per signature (`line_signature`), so an edit of a value reuses the row."""
        check = self._scratch_runner(self.mallard.preset_line)

        def line(text: str) -> str | None:
            sig = (key, line_signature(text, key))
            if sig not in self._preset_lines:
                found = check(text)
                if found is None:  # a case that does not set up now may with another value: not kept
                    return None
                if len(self._preset_lines) >= PRESET_LINES:
                    self._preset_lines.pop(next(iter(self._preset_lines)))
                self._preset_lines[sig] = found
            return self._preset_lines[sig]

        return line

    def _preset_for(self, path: str) -> PresetView | None:
        if not self.presets_fresh:
            return None
        key = catalog_path(path)
        for view in self.presets.values():
            if key.startswith(view.table + ".") or key in view.overrides or any(key in f for f in view.fills.values()):
                return view
        return None

    def preset_switch(self, key: str, name: str, keep: bool) -> tuple[list[dict[str, str]], str | None]:
        view = self.presets.get(key)
        if view is None:
            return [], "presets not resolved yet"
        return switch_changes(self.doc.text, view, name, keep, self._scratch_runner(self.mallard.resolve))

    def apply_preset(self, key: str, name: str, keep: bool) -> None:
        """Name a preset in the input; without `keep`, the keys set over the preset go, so it fills them."""
        view = self.presets.get(key)
        if not keep and view is not None:
            for k in view.overrides_for(name):
                if self.doc.has(k):
                    self.doc.delete(k)
        if name:
            self.doc.set(key, name)
        elif self.doc.has(key):
            self.doc.delete(key)
        self.doc.commit()

    def add_entry(self, array: str, type_: str = "") -> str:
        """A new entry of an array of tables or a named table: its `type` when chosen, a `name` (and `prefix`)
        the catalog requires made unique, the other required keys left for the user (flagged until set)."""
        doc = self.doc
        leaf = str(parse_path(array)[-1])
        if self.is_named(array):
            names = doc.get(array) or {}
            n = 1
            while f"{type_ or leaf}_{n}" in names:
                n += 1
            path = f"{array}.{type_ or leaf}_{n}"
            doc.set(path, {"type": type_} if type_ else {})
            doc.commit()
            return path
        existing = doc.get(array) or []
        taken = {str(e.get(k)) for e in existing if isinstance(e, dict) for k in ("name", "prefix")}
        n = len(existing) + 1
        while f"{leaf}_{n}" in taken or f"./solut/{leaf}_{n}" in taken:
            n += 1
        values: dict[str, Any] = {"type": type_} if type_ else {}
        for k in self.children(array):
            info = self.info(f"{array}.{k}")
            if info is None or not info.required or info.default is not None or k in values:
                continue
            if k == "name":
                values[k] = f"{leaf}_{n}"
            elif k == "prefix":
                values[k] = f"./solut/{leaf}_{n}"
        path = doc.add_entry(array, values)
        doc.commit()
        return path

    def duplicate_entry(self, path: str) -> str:
        new = self.doc.duplicate_entry(path)
        self.doc.commit()
        return new

    def delete_entry(self, path: str) -> None:
        self.doc.delete_entry(path)
        self.doc.commit()

    def _feature_switches(self, groups: list[Group]) -> None:
        """A switch on each optional feature's group, on only while the input has its table; the groups under
        one that is off are hidden, and its own `enabled` key is the switch's."""
        off: list[str] = []
        seen: set[str] = set()
        for g in groups:
            base = g.id.split("#")[0]
            if base in off or any(base.startswith((o + ".", o + "[")) for o in off):
                g.hidden = True
                continue
            f = FEATURES.get(catalog_path(base)) if base else None
            if f is None or base in seen:  # one switch per table, on its first group
                continue
            seen.add(base)
            value = self.doc.get(base)
            on = is_on(f, value)
            g.feature = {
                "path": base,
                "name": f.name,
                "blurb": f.blurb,
                "on": on,
                "content": has_content(f, value),
                "key": f"{base}.{f.enabled_key}" if f.enabled_key else "",
                "mechanism": not on and missing_mechanism(f, self.doc.data),
            }
            if f.enabled_key:
                g.rows = [r for r in g.rows if r.path != g.feature["key"]]
            if not on:
                off.append(base)
                g.collapsed = False

    def set_feature(self, path: str, on: bool, mechanism: str = "") -> None:
        """Turn a table feature on (its table, empty; or its `enabled` key back to the default) or off (its
        table and everything in it): one step to undo. Chemistry needs a mixture
        with a mechanism, given as `mechanism` when the physics has none."""
        f = FEATURES[catalog_path(path)]
        value = self.doc.get(path)
        if on == is_on(f, value):
            return
        if on and mechanism:
            self.doc.set("physics.gas", "mixture")
            self.doc.set("physics.mechanism", mechanism)
        elif on and missing_mechanism(f, self.doc.data):
            raise ValueError(f"{f.name} needs a reacting mixture: choose its mechanism")
        if on and isinstance(value, dict):
            try:
                self.doc.delete(f"{path}.{f.enabled_key}")
            except KeyError:  # an inline table
                self.doc.set(path, {k: v for k, v in value.items() if k != f.enabled_key})
        elif on:
            self.doc.add_table(path)
        else:
            self.doc.delete_table(path)
        self.doc.commit()

    def reset_key(self, path: str) -> None:
        """Back to the default (or the preset's value): the key leaves the input."""
        if self.doc.has(path):
            self.doc.delete(path)
            self.doc.commit()

    def _zone_groups(self, zone: str) -> tuple[list[Group], str | None]:
        groups: list[Group] = []
        z = next((z for z in self.zones() if z["zone"] == zone), None)
        path = f'boundaries[name = "{zone}"]' if z and "index" in z else None
        if path:
            groups += self._table_groups(path, "CONDITION")
            el = self.doc.get(path) or {}
            coords = []
            for key in COORDINATE_KEYS:
                row = self._row(f"{path}.{key}", el.get(key), key in el)
                row.widget = "pick"
                coords.append(row)
            groups.append(Group("COORDINATES", coords, note="pick on the view"))
            for g in groups:
                if g.title != "COORDINATES":
                    g.rows = [r for r in g.rows if r.label not in COORDINATE_KEYS]
        else:
            options = (self.catalog.get("boundaries.type") or KeyInfo("boundaries.type")).values
            groups.append(
                Group(
                    "CONDITION",
                    [
                        Row(
                            path=f"zone:{zone}",
                            label="type",
                            value="",
                            set=False,
                            widget="assign",
                            options=options,
                            severity="error",
                            error="no condition: pick one to add its [[boundaries]] element",
                        )
                    ],
                )
            )
        info = [
            Row(
                path="",
                label="faces",
                value=grouped(z["faces"]) if z and z.get("faces") else "",
                set=True,
                readonly=True,
            )
        ]
        region = next((r for r in (self.structure.regions if self.structure else []) if r["name"] == zone), None)
        if region:
            info.append(
                Row(
                    path="",
                    label="area" if region.get("dimension") == 2 else "length",
                    value=f"{region['measure']:.6g}",
                    set=True,
                    readonly=True,
                )
            )
        details = next(
            (b.get("details") for b in (self.structure.boundaries if self.structure else []) if b["zone"] == zone), ""
        )
        if details:
            info.append(Row(path="", label="patch", value=str(details), set=True, readonly=True))
        groups.append(Group("ZONE", info, note="from check"))
        return groups, path

    def _region_groups(self, ident: str) -> tuple[list[Group], str | None]:
        groups: list[Group] = []
        path: str | None = ident if self.doc.has(ident) else None
        if path:
            value = self.doc.get(path)
            if isinstance(value, dict):
                named = len(parse_path(path)) == 2 and self.is_named(str(parse_path(path)[0]))
                rows = [self._row(f"{path}.{k}", v, True) for k, v in value.items()]
                groups.append(Group("SHAPE", rows, entry=path if named else ""))
        name = ident.removeprefix("regions.")
        want = canonical(self.doc, ident) or ident
        region = next(
            (
                r
                for r in (self.structure.regions if self.structure else [])
                if r["name"] == name or canonical(self.doc, r["name"]) == want
            ),
            None,
        )
        if region:
            rows = [
                Row("", "dimension", str(region.get("dimension")), True, readonly=True),
                Row("", "cells", str(region.get("cells")), True, readonly=True),
                Row("", "measure", f"{region.get('measure', 0):.6g}", True, readonly=True),
            ]
            if region.get("surface") is not None:
                rows.append(Row("", "surface", f"{region['surface']:.6g}", True, readonly=True))
            groups.append(Group("ON THE MESH", rows, note="from check"))
        users = self.used_by(name)
        if users:
            groups.append(Group("USED BY", [Row(u, "", u, True, readonly=True) for u in users]))
        return groups, path

    def used_by(self, region: str) -> list[str]:
        """Every element whose value names this region (by its name in [regions] or of the mesh)."""
        out: list[str] = []

        def walk(node: Any, prefix: list[str | int]) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if prefix == [] and k == "regions":
                        continue
                    walk(v, [*prefix, k])
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, [*prefix, i])
            elif node == region and prefix and prefix[-1] != "name" and prefix[0] != "boundaries":
                out.append(format_path(prefix))

        walk(self.doc.data, [])
        return out

    # Runs

    def start_run(self, restart_from: str = "", ranks: int = 1, restart_path: str = "") -> ActiveRun:
        """A new run of the case's input; `restart_from` names a run whose restart initializes it: `restart_path`,
        one of its `restarts`, else its last. A build that polls control files gets them turned on, so the run
        can be changed from wherever its files are."""
        self.require_saved()
        if self.run_blocked:
            raise RuntimeError(f"cannot run: {self.run_blocked}")
        restart = ""
        if restart_from:
            record = self.project.run(restart_from)
            restart = self.verified_restart(record, self._restart_entry(record, restart_path))
        name = self.project.new_run_name(self.input_path.stem)
        run_dir = self.project.root / "runs" / name
        settings = [f"control_file_interval={CONTROL_FILE_INTERVAL}"] if self.mallard.capabilities().set_file else []
        spec = RunSpec(
            name=name,
            input_file=self.input_path,
            run_dir=str(run_dir),
            restart=restart,
            ranks=max(1, ranks),
            settings=settings,
        )
        job = self.backend.submit(spec)
        record = RunRecord(
            name=name,
            home={"remote": "local", "directory": f"runs/{name}"},
            input=self.input_path.name,
            input_sha256=sha256_file(self.input_path),
            mallard=self.mallard.version(),
            job=job.id,
        )
        self.project.save_run(record)
        follower = RunLogFollower(
            run_dir / "mallard.jsonl", read_file=lambda path, off: self.backend.read_file(job, path, off)
        )
        run = ActiveRun(record, job, follower)
        self.active[name] = run
        return run

    def continue_run(self, name: str) -> ActiveRun:
        """Continue a stopped run in its own directory from its last restart, whose SHA-256 is checked first."""
        if name in self.active and self.active[name].state.active:
            raise RestartError(f"run {name} is running")
        record = self.project.run(name)
        if record.outcome not in RESUMABLE:
            raise RestartError(
                f"run {name}: {record.outcome or 'no outcome'}; continue needs one of {', '.join(RESUMABLE)}"
            )
        if record.home.get("remote", "local") != "local":
            raise RestartError(f"run {name}: on {record.home.get('remote')}; remote runs come with the remote launch")
        status = self.restart_status(record)
        if status.continuable is False:
            raise RestartError(f"run {name}: {'; '.join(status.reasons) or 'this Mallard does not continue from it'}")
        restart = self.verified_restart(record)
        run_dir = self.project.root / record.home.get("directory", f"runs/{name}")
        before = RunLogFollower(run_dir / "mallard.jsonl")
        before.poll()
        job = self.backend.submit(RunSpec(name, run_dir / "input.toml", str(run_dir), restart=restart, resume=True))
        record.job, record.outcome = job.id, ""
        self.project.save_run(record)
        follower = RunLogFollower(
            run_dir / "mallard.jsonl", read_file=lambda path, off: self.backend.read_file(job, path, off)
        )
        run = ActiveRun(record, job, follower, starts=before.state.starts + 1)
        self.active[name] = run
        return run

    @property
    def relocator(self) -> Relocator:
        return Relocator(self.data, self.mallard.executable, local_backend=self.backend)

    def continue_elsewhere(
        self, name: str, dst: str, progress: Callable[[int, int, str], None] | None = None
    ) -> tuple[ActiveRun, RestartCheck]:
        """A new run of the case at `dst` from run `name`'s newest restart, once `dst` has read that restart
        and found in it what the run's home finds."""
        if name in self.active and self.active[name].state.active:
            raise RestartError(f"run {name} is running")
        self.require_saved()
        record = self.project.run(name)
        new_name = self.project.new_run_name(self.input_path.stem)
        try:
            c = self.relocator.continue_elsewhere(record, dst, self.input_path, new_name, progress)
        except (RelocateError, RemoteError, TransferError, ValueError, KeyError, OSError) as e:
            raise RestartError(str(e)) from e
        backend = c.backend
        job = c.job
        follower = RunLogFollower(
            Path(job.run_dir) / "mallard.jsonl", read_file=lambda path, off: backend.read_file(job, path, off)
        )
        run = ActiveRun(c.record, job, follower, backend=None if dst == LOCAL else backend)
        self.active[new_name] = run
        return run, c.check

    def move_run(
        self, name: str, dst: str, progress: Callable[[int, int, str], None] | None = None
    ) -> tuple[RunRecord, RestartCheck | None, list[str]]:
        """Copy run `name` to `dst` and make it its home; the old home's files are returned, left in place."""
        if name in self.active and self.active[name].state.active:
            raise RestartError(f"run {name} is running")
        try:
            return self.relocator.move(self.project.run(name), dst, progress)
        except (RelocateError, RemoteError, TransferError, ValueError, KeyError, OSError) as e:
            raise RestartError(str(e)) from e

    def restarts(self, name: str) -> list[dict[str, Any]]:
        """Every restart file run `name`'s log recorded, newest first: path, step, t, bytes and its hash."""
        active = self.active.get(name)
        st = active.follower.state if active else self.open_run(self.project.run(name))
        out: list[dict[str, Any]] = []
        for o in st.outputs:
            if o.get("kind") != "restart" or not o.get("path"):
                continue
            path = str(o["path"])
            out.append({"path": path, "step": o.get("step"), "t": o.get("t"), "bytes": o.get("bytes")})
            out[-1] |= self._sha_of(st, path)
        return list(reversed(out))

    def _restart_entry(self, record: RunRecord, path: str) -> dict[str, Any]:
        if not path or path == record.last_restart.get("path"):
            return record.last_restart
        for entry in self.restarts(record.name):
            if entry["path"].removeprefix("./") == path.removeprefix("./"):
                return entry
        raise RestartError(f"run {record.name}: no restart {path} in its run log")

    def restart_check(self, name: str, path: str = "") -> dict[str, Any]:
        """What a new run of the case from run `name`'s restart `path` (else its last) does with each key the case
        changes from the input the file was written with (`mallard check --restart`'s `restart`): `changes`, and
        `known` false from builds that do not classify them."""
        record = self.project.run(name)
        entry = self._restart_entry(record, path)
        try:
            file = self.verified_restart(record, entry)
        except RestartError as e:
            return {"path": entry.get("path", path), "error": str(e), "known": False, "changes": []}
        # The input as the run directory gets it, so its file paths and settings are the run's
        staged = self.view_dir / "restart-check" / "input.toml"
        staged.parent.mkdir(exist_ok=True)
        staged.write_text(run_input(self.checked_path, absolute_paths=True), encoding="utf-8")
        report = self.mallard.check(staged, None, [*self.launch.args(), "--restart", file])
        restart = report.data.get("restart")
        out: dict[str, Any] = {"path": entry.get("path", path), "error": "", "known": isinstance(restart, dict)}
        out["changes"] = list(restart.get("changes") or []) if isinstance(restart, dict) else []
        if not out["known"] and report.errors and not report.data.get("setup_done"):
            out["error"] = "; ".join(d.message for d in report.diagnostics if d.severity == "error")
        return out

    def lineage(self, name: str) -> list[dict[str, Any]]:
        """The restarts run `name` descends from, nearest first: each file, its step and the project's run that
        wrote it (empty where the project has none of that run_id)."""
        by_id = {r.run_id: r for r in self.project.runs() if r.run_id}
        out: list[dict[str, Any]] = []
        seen = {name}
        record: RunRecord | None = self.project.run(name)
        while record is not None:
            active = self.active.get(record.name)
            start = (active.follower.state if active else self.open_run(record)).start
            of = (start or {}).get("restart_of") or {}
            if not of.get("file"):
                break
            parent = by_id.get(str(of.get("run_id") or ""))
            out.append({"file": str(of["file"]), "step": of.get("step"), "run": parent.name if parent else ""})
            record = parent if parent and parent.name not in seen else None
            if record is not None:
                seen.add(record.name)
        return out

    def verified_restart(self, record: RunRecord, entry: dict[str, Any] | None = None) -> str:
        """The absolute path of one of the run's restarts (`entry`, else its last), once its SHA-256 matches the
        one its run log recorded."""
        entry = record.last_restart if entry is None else entry
        if not entry.get("path"):
            raise RestartError(f"run {record.name}: no restart recorded")
        home = self.data.home(record)
        path = home.path(str(entry["path"]).removeprefix("./"))
        expected = entry.get("sha256")
        if expected and entry.get("chunk_bytes"):
            found = tree_at(home.location, path, int(entry["chunk_bytes"]))
            if found is None:
                raise RestartError(f"{path}: missing")
            if f"tree:{found}" != expected:
                raise RestartError(f"{path}: chunk tree {found[:12]} is not the recorded {expected[5:17]}")
        elif expected:
            found = home.location.hash_files([(path, None)]).get(path)
            if found is None:
                raise RestartError(f"{path}: missing")
            if found != expected:
                raise RestartError(f"{path}: SHA-256 {found[:12]} is not the recorded {expected[:12]}")
        return path

    def poll_run(self, name: str) -> RunState:
        run = self.active[name]
        run.follower.poll()
        run.state = (run.backend or self.backend).state(run.job)
        st = run.follower.state
        changed = False
        if st.run_id and run.record.run_id != st.run_id:
            run.record.run_id, changed = st.run_id, True
        if st.end is not None and run.started and not run.record.outcome:
            run.record.outcome = str(st.end.get("outcome", ""))
            for key in ("last_snapshot", "last_restart"):
                if st.end.get(key):
                    entry = {"path": st.end[key].get("path", "")}
                    entry |= self._sha_of(st, entry["path"])
                    setattr(run.record, key, entry)
            changed = True
        if changed:
            self.project.save_run(run.record)
        return st

    @staticmethod
    def _sha_of(st: RunState, path: str) -> dict[str, Any]:
        """The recorded hash of a file: its SHA-256, or its chunk tree's root (#1329) and chunk size."""
        for out in reversed(st.outputs):
            for f in out.get("files", []):
                if f.get("path") != path:
                    continue
                if f.get("sha256"):
                    return {"sha256": str(f["sha256"])}
                if f.get("sha256_tree") and f.get("chunk_bytes"):
                    return {"sha256": f"tree:{f['sha256_tree']}", "chunk_bytes": int(f["chunk_bytes"])}
        return {}

    def open_run(self, record: RunRecord) -> RunState:
        """A finished or earlier run, read from its run log where the run is (a remote's run over its shell)."""
        if record.home.get("remote", LOCAL) == LOCAL:
            directory = self.project.root / record.home.get("directory", f"runs/{record.name}")
            follower = RunLogFollower(directory / "mallard.jsonl")
        else:
            home = self.data.home(record)
            follower = RunLogFollower(
                Path(home.path("mallard.jsonl")),
                read_file=lambda path, off: home.location.read_bytes(home.path(path), off),
            )
        follower.poll()
        return follower.state

    # The results viewer

    def viewer(
        self, record: RunRecord, on_wait: Callable[[float], None] | None = None, login: bool = False
    ) -> tuple[Viewer, str]:
        """The viewer for a run and its directory there: a viewer process here for a local run, reading it in
        place; for a remote run, one viewer job per remote. Each starts on first use and serves every run of
        its place."""
        home = self.data.home(record)
        name = home.location.name
        viewer = self.viewers.get(name)
        if login and isinstance(viewer, RemoteViewer) and viewer.launch != "login":
            viewer = None  # the user chose the login node over the job that failed
        if isinstance(viewer, (StdioViewer, SocketViewer)) and not viewer.alive:
            viewer = None  # it timed out idle or its job ended: start another
        if viewer is None:
            if name == LOCAL:
                viewer = LocalViewer(Path("/"))
            else:
                location = self.data.location(name)
                remote = self.data.remotes.remotes[name]
                if isinstance(location, SshLocation):
                    viewer = start_remote_viewer(location.conn, remote, "/", on_wait=on_wait, login=login)
                elif isinstance(location, ShellLocation) and isinstance(location.conn, HelperPod):
                    viewer = K8sViewer(remote, location.conn, root="/", on_wait=on_wait)
                else:
                    raise ValueError(f"remote {name}: no viewer for its backend")
            self.viewers[name] = viewer
        return viewer, home.root

    def parallel_plan(self, record: RunRecord, cells: int) -> ParallelPlan:
        """The parallel viewer job a snapshot of `cells` cells would take: its ranks and cost, shown first."""
        name = self.data.home(record).location.name
        return plan(None if name == LOCAL else self.data.remotes.remotes[name], cells)

    def parallel_viewer(
        self, record: RunRecord, ranks: int, on_wait: Callable[[float], None] | None = None
    ) -> tuple[Viewer, str]:
        """A parallel viewer job (pvserver of `ranks` ranks) for what must be rendered whole: through Slurm on
        an SSH remote, with ParaView's own MPI here. It replaces the place's previous one."""
        home = self.data.home(record)
        name = home.location.name
        key = f"{name} (parallel)"
        old = self.viewers.pop(key, None)
        if old is not None:
            old.close()
        if name == LOCAL:
            try:
                viewer: Viewer = LocalParallelViewer("/", ranks)
            except ViewerError as e:
                raise ValueError(str(e)) from e
        else:
            location = self.data.location(name)
            if not isinstance(location, SshLocation):
                raise ValueError(f"remote {name}: the parallel viewer needs Slurm over SSH")
            remote = self.data.remotes.remotes[name]
            viewer = start_parallel_viewer(location.conn, remote, "/", ranks, DRIVER_DIR, on_wait=on_wait)
        self.viewers[key] = viewer
        return viewer, home.root

    def post_launcher(self, record: RunRecord) -> tuple[Launcher, str]:
        """Where a post-processing job of a run runs, and its directory there."""
        home = self.data.home(record)
        name = home.location.name
        if name == LOCAL:
            return LocalLauncher(self.mallard.executable, self.mallard.threads), home.root
        location = self.data.location(name)
        if not isinstance(location, SshLocation):
            raise ValueError(f"remote {name}: no post-processing jobs for its backend yet")
        return SlurmLauncher(location.conn, self.data.remotes.remotes[name]), home.root

    def batch_images(self, record: RunRecord) -> tuple[BatchImages, str]:
        """Images of a remote run rendered by batch jobs: when no live viewer can be reached."""
        home = self.data.home(record)
        location = self.data.location(home.location.name)
        if not isinstance(location, SshLocation):
            raise ValueError(f"{home.location.name}: batch images are for SSH remotes")
        return BatchImages(location.conn, self.data.remotes.remotes[home.location.name]), home.root

    def close(self) -> None:
        """Viewer jobs and pods, and Kubernetes helper pods, end now rather than at their idle timeouts."""
        self.close_viewers()
        if self._data is not None:
            self._data.close()
        self.mallard.close()

    def close_viewers(self) -> None:
        for viewer in self.viewers.values():
            viewer.close()
        self.viewers.clear()

    def stop_run(self, name: str) -> None:
        run = self.active[name]
        (run.backend or self.backend).stop(run.job)

    def checkpoint_run(self, name: str) -> None:
        run = self.active[name]
        (run.backend or self.backend).checkpoint(run.job)


def _summary(data: dict[str, Any], tables: tuple[str, ...], n_elements: int) -> str:
    """A section's value in the tree: its `type`, else its first set values."""
    if n_elements and len(tables) > 1:
        return f"{n_elements} item{'s' if n_elements != 1 else ''}"
    if n_elements:
        return str(n_elements)
    table = data.get(tables[0])
    if not isinstance(table, dict):
        return ""
    if isinstance(table.get("type"), str):
        return str(table["type"])
    strings = [v for v in table.values() if isinstance(v, str)]
    if strings:
        return " · ".join(strings[:2])
    numbers = [(k, v) for k, v in table.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return f"{numbers[0][0]} {display_value(numbers[0][1])}" if numbers else ""


def _in_unit(default: str, catalog_dimension: str, unit: str) -> str:
    """A catalog default (a number in SI) shown in the unit a field starts in."""
    found = dimension_of(catalog_dimension)
    if not default or not unit or found is None or unit == si_unit(found[1]):
        return default
    try:
        return convert(default, parse_unit(si_unit(found[1])), parse_unit(unit))[0]
    except (UnitError, ArithmeticError, ValueError):
        return default


def report_json(report: CheckReport | None) -> str:
    return json.dumps(report.data, indent=2) if report else ""
