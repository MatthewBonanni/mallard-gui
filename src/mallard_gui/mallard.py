"""The `mallard` command line, the GUI's only way to know about keys, checks and case views.

Nothing here knows physics or input keys: it runs `mallard` and reads its JSON.
"""

from __future__ import annotations

import ast
import contextvars
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .presets import SetupError
from .service import MallardService, session_env
from .session_client import SessionError

THREADS = 2  # local commands stay light on a shared machine


class MallardError(RuntimeError):
    pass


class Superseded(MallardError):
    """The command was stopped: its group's work was superseded (`Mallard.supersede`) before it finished."""


_GROUP: contextvars.ContextVar[str] = contextvars.ContextVar("mallard_group", default="")


def find_mallard(explicit: str | None = None) -> Path:
    """The `mallard` to use: an explicit path, $MALLARD, the one installed beside the GUI (the `[solver]`
    extra: `uv tool install` puts its executables in the tool's environment, not on PATH), or PATH's."""
    beside = Path(sys.executable).parent / "mallard"
    candidate = explicit or os.environ.get("MALLARD") or (str(beside) if beside.is_file() else shutil.which("mallard"))
    if not candidate:
        raise MallardError("no mallard found: pass --mallard PATH, set $MALLARD, or put mallard on PATH")
    path = Path(candidate).expanduser()
    if path.is_dir():
        path = path / "mallard"
    if not path.exists():
        raise MallardError(f"{path}: no such file")
    return path


@dataclass
class KeyInfo:
    """One entry of the key catalog (`mallard list keys --format json`)."""

    path: str
    set: bool = False
    type: str | None = None
    default: str | None = None
    required: bool = False
    unset: str | None = None  # what leaving it out means (#1400); "" says nothing, None: the catalog predates it
    values: list[str] = field(default_factory=list)
    dimension: str | None = None
    form: str | None = None  # a table's: "table", "array" (of tables) or "named" (entries under chosen names)
    dims: list[int] = field(default_factory=list)  # the dimensions that read it; empty: not said
    defaults_by_dim: dict[str, str] = field(default_factory=dict)
    description: str = ""  # one line, from catalogs that give one (#1256)
    when: str = ""  # when it applies, from catalogs that say (#1256)
    models: dict[str, ModelDoc] = field(default_factory=dict)  # values naming models from the literature

    @property
    def section(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def leaf(self) -> str:
        return self.path.rsplit(".", 1)[-1]

    @property
    def widget(self) -> str:
        """The form widget the declaration asks for."""
        if self.values:
            return "select"
        if self.type == "bool":
            return "bool"
        if self.type in ("number", "integer"):
            return "number"
        if self.type in ("array", "table"):
            return "toml"
        return "text"

    @property
    def unit(self) -> str:
        """The SI unit in a `dimension` such as "pressure (kg m^-1 s^-2)"."""
        if not self.dimension:
            return ""
        m = re.search(r"\(([^)]*)\)\s*$", self.dimension)
        return m.group(1) if m else ""

    def default_for(self, dim: int | None) -> str | None:
        """The default as a TOML literal, that of `dim` where the catalog gives one per dimension."""
        if dim is not None and str(dim) in self.defaults_by_dim:
            return self.defaults_by_dim[str(dim)]
        return self.default

    def applies_in(self, dim: int | None) -> bool:
        return dim is None or not self.dims or dim in self.dims

    @property
    def default_value(self) -> str | None:
        """The default as a bare value: the catalog gives TOML literals (`"\\"mesh.msh\\""`).

        TODO(#1256): typed defaults in the catalog."""
        return bare_literal(self.default)


SITE = "https://matthewbonanni.github.io/mallard/"


def site_url(docs: str) -> str:
    """The website's page for a docs path of the repository, as MkDocs builds it:
    `docs/theory/particles.md#singh-drag` -> `<site>docs/theory/particles/#singh-drag`."""
    page, _, anchor = docs.partition("#")
    page = page.removesuffix(".md")
    page = page.removesuffix("index") if page.endswith("/index") else page + "/"
    return SITE + page + (f"#{anchor}" if anchor else "")


@dataclass(frozen=True)
class ModelDoc:
    """A value of a key that names a model from the literature: the theory section with its formulation (a docs
    path with its anchor) and its source."""

    docs: str
    cite: str = ""

    @property
    def url(self) -> str:
        return site_url(self.docs)

    def tip(self, value: str) -> str:
        page = self.docs.partition("#")[0].removeprefix("docs/").removesuffix(".md").replace("_", " ")
        source = f": {self.cite}" if self.cite else ""
        return f"{value}{source}\nIts formulation in the theory manual ({page}), on the Mallard website"


def parse_models(entry: Any) -> dict[str, ModelDoc]:
    """A catalog entry's `models` (Mallard PR adding them; absent from older catalogs): value -> docs, cite."""
    if not isinstance(entry, dict):
        return {}
    return {
        str(v): ModelDoc(str(m["docs"]), str(m.get("cite") or ""))
        for v, m in entry.items()
        if isinstance(m, dict) and isinstance(m.get("docs"), str) and m["docs"].startswith("docs/")
    }


@dataclass
class Species:
    names: list[str]
    molar_masses: dict[str, float] = field(default_factory=dict)  # kg/kmol, from builds that give them


def parse_species(data: dict[str, Any]) -> Species:
    """`list species --format json`: `species`, and `molar_masses` beside it (the same order) where given."""
    names = [str(n) for n in data.get("species") or []]
    masses = data.get("molar_masses") or []
    weights = {n: float(w) for n, w in zip(names, masses, strict=False) if isinstance(w, (int, float)) and w > 0}
    return Species(names, weights if len(weights) == len(names) else {})


def bare_literal(d: str | None) -> str | None:
    """A catalog's TOML literal as a field shows it: strings without their quotes."""
    if d is None:
        return None
    if len(d) >= 2 and d[0] == d[-1] == '"':
        return d[1:-1]
    return d


FORMS = ("table", "array", "named")


def read_catalog(path: Path) -> dict[str, KeyInfo] | None:
    """The catalog `check --catalog` wrote (its keys object alone); None when the check wrote none (its setup
    stopped before) or it does not parse."""
    try:
        keys = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(keys, dict):
        return None
    try:
        return parse_catalog({"keys": keys.get("keys", keys)})
    except (MallardError, AttributeError, TypeError, ValueError):
        return None


def parse_catalog(data: dict[str, Any]) -> dict[str, KeyInfo]:
    keys = data.get("keys")
    if not isinstance(keys, dict):
        raise MallardError("key catalog: no 'keys' object")
    # TODO(#1256): the catalog has no schema name or version yet; accept what has 'keys'.
    states_unset = any(isinstance(e, dict) and "unset" in e for e in keys.values())
    out = {}
    for path, entry in keys.items():
        out[path] = KeyInfo(
            path=path,
            set=bool(entry.get("set", False)),
            type=entry.get("type"),
            default=entry.get("default"),
            required=bool(entry.get("required", False)),
            unset=str(entry.get("unset") or "") if states_unset else None,
            values=list(entry.get("values", [])),
            dimension=entry.get("dimension"),
            form=entry.get("form") if entry.get("form") in FORMS else None,
            dims=[int(d) for d in entry.get("dims") or [] if isinstance(d, int)],
            defaults_by_dim={str(k): str(v) for k, v in (entry.get("defaults_by_dim") or {}).items()},
            description=str(entry.get("description") or entry.get("summary") or ""),
            when=str(entry.get("when") or entry.get("applies") or ""),
            models=parse_models(entry.get("models")),
        )
    return out


@dataclass
class Diagnostic:
    severity: str  # error | warning | ok
    code: str
    message: str
    docs: str = ""
    key: str | None = None
    line: int | None = None
    source: str = "check"
    refs: tuple[str, ...] = ()  # every key or section the message names, `key` first when it has one
    column: int | None = None
    structured: bool = False  # the build gave `key`, `line` and `column` (null for none), so no text is guessed

    @property
    def section(self) -> str | None:
        return re.split(r"[.\[]", self.key, maxsplit=1)[0] if self.key else None


_LOCATED = re.compile(
    r"^(?P<file>[^:\s]+\.toml): (?:line (?P<line>\d+): )?(?P<key>[A-Za-z_][^:]*?): (?P<text>.*)$", re.S
)
_STDERR_DIAG = re.compile(
    r"^(?P<sev>warning|error) (?P<code>[EW]-[A-Z0-9-]+): (?P<msg>.*?)\n\s+(?P<docs>docs/\S+)", re.M | re.S
)


def locate(message: str) -> tuple[str | None, int | None, str]:
    """Key and line from a message's text: `input.toml: line 31: boundaries[name = "farfield"].T: missing`, for
    builds whose check report has no structured `key`, `line` and `column` (before PR #1377)."""
    m = _LOCATED.match(message)
    if not m:
        return None, None, message
    key = m["key"].strip()
    if " " in key.replace(" = ", "=").split("[", 1)[0]:
        return None, int(m["line"]) if m["line"] else None, message
    return key, int(m["line"]) if m["line"] else None, m["text"]


def stderr_diagnostics(stderr: str) -> list[Diagnostic]:
    """Warnings printed during setup, which the JSON report of builds before PR #1377 leaves out (later builds
    report them as diagnostics)."""
    out = []
    for m in _STDERR_DIAG.finditer(stderr):
        message = " ".join(m["msg"].split())
        key, line, _ = locate(message)
        out.append(Diagnostic(m["sev"], m["code"], message, m["docs"], key, line, source="stderr"))
    return out


_LEADING_TABLE = re.compile(r"^\[\[?([a-z_]+)\]\]? ")


def _optional[T](value: Any, kind: Callable[[Any], T]) -> T | None:
    return None if value is None else kind(value)


@dataclass
class CheckReport:
    status: str
    errors: int
    warnings: int
    diagnostics: list[Diagnostic]
    data: dict[str, Any]
    seconds: float = 0.0
    view: Path | None = None

    @property
    def dim(self) -> int | None:
        return self.data.get("dim")

    @property
    def boundaries(self) -> list[dict[str, Any]]:
        return list(self.data.get("boundaries") or [])

    @property
    def regions(self) -> list[dict[str, Any]]:
        return list(self.data.get("regions") or [])

    @property
    def frames(self) -> list[dict[str, Any]] | None:
        """Each `[[boundaries]]` entry's local coordinate frame, from the case view (`mallard-case-view`, what
        `case.view` returns); None from builds whose view has none."""
        view = self.data.get("view")
        if not isinstance(view, dict) or view.get("schema") != "mallard-case-view" or "frames" not in view:
            return None
        return [f for f in view.get("frames") or [] if isinstance(f, dict)]

    @classmethod
    def from_json(cls, data: dict[str, Any], stderr: str = "") -> CheckReport:
        version = data.get("version")
        if version is None or int(version) > 1:
            raise MallardError(f"check report version {version}: this GUI reads version 1")
        unused = [u for u in data.get("unused_keys") or [] if isinstance(u, dict)]
        diags = []
        structured = False
        for d in data.get("diagnostics", []):
            message = d.get("message", "")
            if "key" in d:
                structured = True
                diags.append(
                    Diagnostic(
                        d.get("severity", "error"),
                        d.get("code", ""),
                        message,
                        d.get("docs", ""),
                        _optional(d["key"], str),
                        _optional(d.get("line"), int),
                        column=_optional(d.get("column"), int),
                        structured=True,
                    )
                )
                continue
            key, line, _ = locate(message)
            if key is None and unused and d.get("code") == "E-KEY-UNUSED":
                match = next((u for u in unused if str(u.get("key")) in message), None)
                if match:
                    key, line = str(match["key"]), match.get("line")
            diags.append(
                Diagnostic(d.get("severity", "error"), d.get("code", ""), message, d.get("docs", ""), key, line)
            )
        seen = {(d.code, d.message) for d in diags}
        for d in [] if structured else stderr_diagnostics(stderr):
            if (d.code, d.message) not in seen:
                if d.key is None:
                    m = _LEADING_TABLE.match(d.message)
                    d.key = m.group(1) if m else None
                diags.append(d)
        warnings = sum(d.severity == "warning" for d in diags)
        errors = sum(d.severity == "error" for d in diags)
        status = "error" if errors else "warning" if warnings else "ok"
        return cls(status, errors, warnings, diags, data)


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str
    seconds: float


def parse_version(text: str) -> str:
    """`0.10.0-147-g1fa6e0b05` from the text of `mallard --version`.

    TODO(#1314, PR #1331): `mallard version --format json`, the capabilities query."""
    m = re.search(r"v?(\d+\.\d+\.\d+\S*)", text)
    return m.group(1) if m else text.strip()


# Commands of any `mallard`, local or on a remote through a backend: their arguments and their JSON replies.
# TODO(mallard session): each becomes a request of the resident `mallard session` (JSON-RPC over stdin and
# stdout, with schemas); until then they are one-shot commands, and their callers go through these helpers.
USAGE_ERROR = 2  # exit status of an unknown command or option: what a `mallard` older than a command answers
INSTALL_VERIFY_SCHEMA = "mallard-install-verify"


def install_verify_args() -> list[str]:
    """`mallard install verify` (0.12 and later) with its JSON verdict."""
    return ["install", "verify", "--format", "json"]


def environment_check_args() -> list[str]:
    """`mallard check` without an input: the environment alone, as JSON."""
    return ["check", "--format", "json"]


def json_reply(stdout: str) -> dict[str, Any] | None:
    """A command's JSON reply: its whole standard output, one object; None for anything else."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def install_verify_reply(stdout: str) -> dict[str, Any] | None:
    data = json_reply(stdout)
    return data if data is not None and data.get("schema") == INSTALL_VERIFY_SCHEMA else None


class Mallard:
    """A local `mallard`. Every command runs with the case's directory as working directory,
    since relative paths in an input start from there."""

    def __init__(self, executable: Path, threads: int = THREADS, timeout: float = 120.0) -> None:
        self.executable = executable
        self.threads = threads
        self.timeout = timeout
        self._version: str | None = None
        self._help: dict[str, str] = {}
        self._services: dict[str, MallardService] = {}
        self.case_input: Path | None = None  # the case requests without one start their session with
        self._capabilities: Capabilities | None = None
        self._running: dict[str, set[subprocess.Popen[str]]] = {}
        self._superseded: set[subprocess.Popen[str]] = set()
        self._lock = threading.Lock()

    @staticmethod
    def group(name: str) -> contextvars.Token[str]:
        """Run the commands of this context (and of the threads started from it with its context, as
        `asyncio.to_thread` does) in group `name`, which `supersede` stops."""
        return _GROUP.set(name)

    def supersede(self, name: str) -> None:
        """Stop the commands of group `name` still running: their answer is no longer wanted. Each raises
        `Superseded` in its caller."""
        with self._lock:
            procs = list(self._running.get(name, ()))
            self._superseded.update(procs)
        for proc in procs:
            proc.kill()

    def run(self, args: Sequence[str], cwd: Path, kokkos: bool = True) -> Completed:
        cmd = [str(self.executable), *args]
        if kokkos:
            cmd.append(f"--kokkos-num-threads={self.threads}")
        env = dict(os.environ)
        data = self.data_dir()
        if data is not None and "MALLARD_DATA_DIR" not in env:
            env["MALLARD_DATA_DIR"] = str(data)
        start = time.monotonic()
        group = _GROUP.get()
        with subprocess.Popen(
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        ) as running:
            with self._lock:
                self._running.setdefault(group, set()).add(running)
            try:
                stdout, stderr = running.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                running.kill()
                running.communicate()
                raise MallardError(f"mallard {' '.join(args[:2])}: no answer in {self.timeout:g} s") from None
            finally:
                with self._lock:
                    self._running[group].discard(running)
                    superseded = running in self._superseded
                    self._superseded.discard(running)
        if superseded:
            raise Superseded(f"mallard {args[0] if args else ''}: superseded")
        return Completed(running.returncode, stdout, stderr, time.monotonic() - start)

    def data_dir(self) -> Path | None:
        """The source tree of a build run in place (its `docs/input-keys` hold the general key catalog)."""
        for parent in Path(os.path.realpath(self.executable)).parents:
            if (parent / "docs" / "input-keys").is_dir():
                return parent
        return None

    def version(self) -> str:
        if self._version is None:
            out = self.run(["--version"], cwd=Path.cwd(), kokkos=False)
            if out.returncode != 0:
                raise MallardError(out.stderr.strip() or "mallard --version failed")
            self._version = parse_version(out.stdout)
        return self._version

    def list_keys(self, input_file: Path | None = None) -> tuple[dict[str, KeyInfo] | None, list[Diagnostic]]:
        """The keys this input reads, or the general catalog without an input.

        TODO(#1311): a partial input fails here; the caller keeps the last good catalog."""
        args = ["list", "keys"]
        cwd = Path.cwd()
        if input_file is not None:
            args.append(input_file.name)
            cwd = input_file.parent
        out = self.run([*args, "--format", "json"], cwd=cwd)
        if out.returncode != 0:
            return None, _failure_diagnostics(out.stderr, "E-LIST")
        try:
            return parse_catalog(json.loads(out.stdout)), stderr_diagnostics(out.stderr)
        except json.JSONDecodeError as e:
            raise MallardError(f"mallard list keys: not JSON: {e}") from e

    def resolve(self, input_file: Path) -> dict[str, Any] | None:
        """`mallard input resolve`: the input as a run uses it, every key explicit (defaults and presets filled);
        None when the build has no such command or the case does not set up."""
        out = self.run(["input", "resolve", input_file.name], cwd=input_file.parent)
        if out.returncode != 0:
            error = setup_error(out.stderr, out.returncode)
            if error.startswith("E-USAGE"):  # a build without the command
                return None
            raise SetupError(error)
        try:
            return tomllib.loads(out.stdout)
        except tomllib.TOMLDecodeError:
            return None

    def list_species(self, input_file: Path) -> Species | None:
        """`mallard list species`: the case's mechanism's species (none for a single gas), and their molar masses
        where the build gives them; None from builds without the command. Raises SetupError when the case's
        mechanism does not load."""
        out = self.run(["list", "species", input_file.name, "--format", "json"], cwd=input_file.parent)
        if out.returncode != 0:
            error = setup_error(out.stderr, out.returncode)
            if error.startswith("E-USAGE"):
                return None
            raise SetupError(error)
        try:
            return parse_species(json.loads(out.stdout))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            raise MallardError(f"mallard list species: not its JSON: {e}") from e

    def list_fields(self, input_file: Path | None = None) -> dict[str, Any] | None:
        """`mallard list fields`: the case's output fields and the catalog's others with what they need, or the
        build's whole catalog without an input; None from builds without the command or a case that does not set
        up."""
        args, cwd = ["list", "fields"], Path.cwd()
        if input_file is not None:
            args.append(input_file.name)
            cwd = input_file.parent
        out = self.run([*args, "--format", "json"], cwd=cwd)
        if out.returncode != 0:
            return None
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) and isinstance(data.get("fields"), list) else None

    def preset_line(self, input_file: Path) -> str | None:
        """The "Preset" row of `mallard check`'s report: the preset in effect and the keys the input sets over it
        (`les (the input sets numerics.riemann_solver = "RHLL")`); None when the report has no such row.

        TODO: from `check --format json` once it carries the preset (the run block's `preset` has it)."""
        out = self.run(["check", input_file.name], cwd=input_file.parent)
        m = re.search(r"^\s*Preset\s{2,}(.+(?:\n\s{10,}\S.*)*)", out.stdout, re.M)
        return " ".join(m.group(1).split()) if m else None

    def help_text(self, command: str) -> str:
        """`mallard COMMAND --help`, cached. Only `capabilities()` reads it, until the session protocol."""
        if command not in self._help:
            out = self.run([command, "--help"], cwd=Path.cwd(), kokkos=False)
            self._help[command] = out.stdout if out.returncode == 0 else ""
        return self._help[command]

    def service(self, input_file: Path | None = None) -> MallardService:
        """This build's resident `mallard session`, started on first use; `has(method)` says what it answers. With
        `input_file`, the session of that case (the front end picks its dimension's solver from it); without, any
        session already started, since requests without a case are the same in each."""
        if input_file is None and self._services:
            return next(iter(self._services.values()))
        input_file = input_file or self.case_input
        key = str(input_file.resolve()) if input_file is not None else ""
        if key not in self._services:
            argv = [str(self.executable), "session"]
            cwd = Path.cwd()
            if input_file is not None:
                argv.append(input_file.name)
                cwd = input_file.parent
            argv.append(f"--kokkos-num-threads={self.threads}")
            self._services[key] = MallardService(argv, cwd, session_env(self.data_dir()))
        return self._services[key]

    def close(self) -> None:
        for service in self._services.values():
            service.close()
        self._services.clear()

    def capabilities(self) -> Capabilities:
        """What this build offers beyond the input keys: the GPUs check sizes for, the keys a running run takes and
        the control files it polls; from `initialize` where the build answers it, else from `--help` pages."""
        if self._capabilities is None:
            caps = capabilities_from_help(self.help_text("check"), self.help_text("ctl"))
            service = self.service()
            if service.available:
                caps = capabilities_from_initialize(service.capabilities, caps)
            self._capabilities = caps
        return self._capabilities

    def file_info(self, path: Path) -> dict[str, Any]:
        """`file.info` of a local file (`mallard info FILE --format json` from builds without it)."""
        service = self.service()
        if service.has("file.info"):
            try:
                data = service.request("file.info", {"file": str(path)})
            except SessionError as e:
                raise MallardError(str(e)) from e
            if not isinstance(data, dict):
                raise MallardError(f"file.info {path}: not an object")
            return data
        return file_info(local_exec, [str(self.executable)], str(path))

    def units_catalog(self) -> dict[str, Any] | None:
        """`units.catalog`: every unit with its exact factor; None from builds without it."""
        service = self.service()
        if not service.has("units.catalog"):
            return None
        try:
            data = service.request("units.catalog")
        except SessionError:
            return None
        return data if isinstance(data, dict) and data.get("schema") == "mallard-unit-catalog" else None

    def case_files(self, input_file: Path, text: str | None = None) -> dict[str, Any] | None:
        """`case.files`: the input and every file its setup read; None from builds without it."""
        service = self.service(input_file)
        if not service.has("case.files"):
            return None
        try:
            data = service.case_request(input_file, text, "case.files", {})
        except SessionError as e:
            raise MallardError(str(e)) from e
        return data if isinstance(data, dict) else None

    def boundary_values(self, input_file: Path, text: str | None, out_dir: Path) -> dict[str, Any] | None:
        """`case.boundary_values` written into `out_dir`: each entry's values at its faces; None from builds
        without it."""
        service = self.service(input_file)
        if not service.has("case.boundary_values"):
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            data = service.case_request(input_file, text, "case.boundary_values", {"dir": str(out_dir)})
        except SessionError as e:
            raise MallardError(str(e)) from e
        return data if isinstance(data, dict) and data.get("schema") == "mallard-boundary-values" else None

    def ctl_set(self, run_dir: Path, assignments: Mapping[str, str]) -> Completed:
        """`mallard ctl RUN_DIR set k=v ...`: the run applies them from its next step, or refuses them all.
        The reply is shown as Mallard wrote it, never parsed."""
        return self.run(["ctl", ".", "set", *(f"{k}={v}" for k, v in assignments.items())], run_dir, kokkos=False)

    def check(
        self,
        input_file: Path,
        view_dir: Path | None = None,
        extra: Sequence[str] = (),
        catalog: Path | None = None,
    ) -> CheckReport:
        """`mallard check`; with `catalog`, the keys its setup looked up written there, as `list keys` of the
        input gives them, from the same setup."""
        args = ["check", input_file.name, "--format", "json", *extra]
        if view_dir is not None:
            args += ["--view", str(view_dir)]
        if catalog is not None:
            args += ["--catalog", str(catalog)]
        out = self.run(args, cwd=input_file.parent)
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError:
            diags = _failure_diagnostics(out.stderr, "E-CHECK") or [
                Diagnostic("error", "E-CHECK", out.stderr.strip() or f"mallard check exited {out.returncode}")
            ]
            return CheckReport("error", len(diags), 0, diags, {}, out.seconds)
        report = CheckReport.from_json(data, out.stderr)
        report.seconds = out.seconds
        view = data.get("case_view")
        if view:
            report.view = (input_file.parent / view).resolve()
        return report

    def analyze(self, input_file: Path, out_dir: Path) -> Analysis:
        """`mallard check` with the case view, the mesh fields and, from builds that write it, the initial state
        written into `out_dir`."""
        out_dir.mkdir(parents=True, exist_ok=True)
        args = ["check", input_file.name, "--format", "json", "--view", str(out_dir / "view")]
        args += ["--mesh-fields", str(out_dir / "mesh")]
        if self.capabilities().initial:
            args += ["--initial", str(out_dir / "initial")]
        out = self.run(args, cwd=input_file.parent)
        try:
            data = json.loads(out.stdout)
        except json.JSONDecodeError as e:
            raise MallardError(out.stderr.strip() or f"mallard check exited {out.returncode}") from e
        report = CheckReport.from_json(data, out.stderr)
        report.seconds = out.seconds
        return analysis_of(report, input_file.parent)

    def initial_state(self, input_file: Path, out_dir: Path) -> Path:
        """The case's initial state as a snapshot of the whole domain (RHO, U, P, T per cell), written by
        Mallard without running the case: `check --initial` from builds that have it, else a run of one step
        in `out_dir` whose only output is the snapshot at step 0."""
        out_dir.mkdir(parents=True, exist_ok=True)
        if self.capabilities().initial:
            args = ["check", input_file.name, "--format", "json", "--initial", str(out_dir / "initial")]
            out = self.run(args, cwd=input_file.parent)
            try:
                written = json.loads(out.stdout).get("initial_file")
            except json.JSONDecodeError:
                written = None
            if not written:
                raise MallardError(_last_error(out) or f"mallard check exited {out.returncode}")
            return (input_file.parent / str(written)).resolve()
        work = out_dir / "initial-run"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        text = initial_input(input_file.read_text(encoding="utf-8"), input_file.parent, work / "initial")
        (work / "input.toml").write_text(text, encoding="utf-8")
        out = self.run(["-i", "input.toml"], cwd=work)
        snapshot = work / "initial_000000.vtu"
        if not snapshot.is_file():
            raise MallardError(_last_error(out) or f"mallard wrote no initial state (exit {out.returncode})")
        return snapshot


def initial_input(text: str, root: Path, prefix: Path) -> str:
    """The input of a run that only writes the case's initial state: one step, and one snapshot of the whole
    domain at step 0 (`prefix`_000000.vtu) with the primitive fields, in place of every output the case has.
    Files the input names relative to `root` (the mesh, tables, a restart to start from) are named in full,
    as the run is made elsewhere."""
    import tomlkit

    doc = tomlkit.parse(text)

    def absolute(value: Any) -> Any:
        if isinstance(value, str) and value and not Path(value).is_absolute() and (root / value).is_file():
            return str((root / value).resolve())
        if isinstance(value, dict):
            for k in list(value):
                new = absolute(value[k])
                if new is not value[k]:
                    value[k] = new
        elif isinstance(value, list):
            for i, item in enumerate(value):
                new = absolute(item)
                if new is not item:
                    value[i] = new
        return value

    for name in ("write_data", "probes", "forces", "statistics"):
        doc.pop(name, None)
    absolute(doc)
    run = doc.setdefault("run", tomlkit.table())
    for key in ("t_stop", "t_wall_stop"):
        run.pop(key, None)
    run["n_steps"] = 1
    out = tomlkit.table()
    out["prefix"] = str(prefix)
    out["fields"] = ["RHO", "U", "P", "T"]
    out["format"] = "vtu"
    out["interval"] = 1_000_000_000  # step 0 and no other
    snapshots = tomlkit.aot()
    snapshots.append(out)
    doc["write_data"] = snapshots
    return tomlkit.dumps(doc)


@dataclass
class Analysis:
    """The check of a case with what the analysis overlays draw, every file written by Mallard: the case view
    (walls with `y_plus`), the mesh fields (per-cell quality) and the initial state."""

    report: CheckReport
    view: Path | None = None
    mesh_fields: Path | None = None
    initial: Path | None = None


def _last_error(out: Completed) -> str:
    """What Mallard said went wrong: its error line, else the end of what it printed."""
    lines = [line for line in (out.stderr + "\n" + out.stdout).splitlines() if line.strip()]
    errors = [line.strip() for line in lines if line.lstrip().lower().startswith("error")]
    return errors[0] if errors else " ".join(lines[-2:]).strip()


def analysis_of(report: CheckReport, root: Path) -> Analysis:
    """The files the report names, relative paths from `root` (the directory the check ran in)."""

    def path(key: str) -> Path | None:
        value = report.data.get(key)
        return (root / value).resolve() if isinstance(value, str) and value else None

    return Analysis(report, path("case_view"), path("mesh_fields_file"), path("initial_file"))


def setup_error(stderr: str, returncode: int) -> str:
    """The error a command stopped on, as Mallard printed it (`E-INPUT: input.toml: line 3: ...`), without the
    lists and pointers after it."""
    m = re.search(r"^error ([EW]-[A-Z0-9-]+): (.+)$", stderr, re.M)
    if m:
        return f"{m[1]}: {m[2].strip()}"
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else f"exit status {returncode}"


def _failure_diagnostics(stderr: str, fallback_code: str) -> list[Diagnostic]:
    diags = stderr_diagnostics(stderr)
    for d in diags:
        # "mallard list: input.toml: line 31: key: text" -> located
        inner = re.sub(r"^mallard \w+: ", "", d.message)
        key, line, _ = locate(inner)
        d.key, d.line = d.key or key, d.line or line
    if not diags and stderr.strip():
        diags = [Diagnostic("error", fallback_code, stderr.strip())]
    return diags


def write_set_file(run_dir: Path, assignments: Mapping[str, str]) -> Path:
    """The SET control file a run polls (PR #1334): written whole as SET.new, then renamed, so the run never
    reads half of it. Replies come back as run-log `set` events."""
    tmp = run_dir / "SET.new"
    tmp.write_text("".join(f"{k} = {v}\n" for k, v in assignments.items()), encoding="utf-8")
    return tmp.replace(run_dir / "SET")


@dataclass
class Capabilities:
    gpus: list[str] = field(default_factory=list)  # what `check --memory` takes besides a size
    settable: list[str] = field(default_factory=list)  # keys `ctl set` takes; `<...>` names an element
    set_file: bool = False  # the run polls a SET file (PR #1334)
    initial: bool = False  # `check --initial` writes the initial state (PR #1343)
    catalog: bool = False  # `check --catalog` writes the keys its setup looked up
    source: str = "help"  # "initialize": Mallard's own capabilities; "help": read from `--help` pages
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)  # by settable key: type, units, help
    control_files: list[str] | None = None  # the control files the run looks for; None: not reported
    commands: list[str] = field(default_factory=list)  # what the control socket takes
    devices: dict[str, Any] = field(default_factory=dict)  # the backend, its GPUs, the host

    def control_file(self, name: str) -> bool:
        """Whether runs of this build look for control file `name` (STOP, CHECKPOINT, SET); builds that do not
        report their control files have STOP and CHECKPOINT."""
        if self.control_files is None:
            return name in ("STOP", "CHECKPOINT") or (name == "SET" and self.set_file)
        return name in self.control_files


def capabilities_from_initialize(caps: Mapping[str, Any], fallback: Capabilities) -> Capabilities:
    """The capabilities `initialize` reports (its `control` and `devices`); what it does not report (the GPU
    names `check --memory` sizes for, `check --initial`) stays as the `--help` pages say."""
    control = caps.get("control")
    if not isinstance(control, Mapping):
        return fallback
    settings = {
        str(s["key"]): {k: s.get(k) for k in ("type", "units", "selector", "help")}
        for s in control.get("settings") or []
        if isinstance(s, Mapping) and s.get("key")
    }
    files = [str(f) for f in control.get("files") or []]
    devices = caps.get("devices")
    return Capabilities(
        gpus=fallback.gpus,
        settable=list(settings),
        set_file="SET" in files,
        initial=fallback.initial,
        catalog=fallback.catalog,
        source="initialize",
        settings=settings,
        control_files=files,
        commands=[str(c) for c in control.get("commands") or []],
        devices=dict(devices) if isinstance(devices, Mapping) else {},
    )


def capabilities_from_help(check_help: str, ctl_help: str) -> Capabilities:
    """TEMPORARY(session request: capabilities): read from `--help` pages until `mallard session` reports
    GPUs, settable keys and control channels as data."""
    flat = check_flat = " ".join(check_help.split())
    initial = "--initial" in flat
    m = re.search(r"--memory SIZE\|GPU.*?or a GPU \(([^)]*)\)", flat)
    gpus = [g.strip() for g in m[1].split(",")] if m else []
    flat = " ".join(ctl_help.split())
    settable: list[str] = []
    m = re.search(r"set: change settings from the next step on: (.*?)\. Other keys are refused", flat)
    if m:
        items = re.sub(r"\([^)]*\)", "", m[1]).split(",")
        for item in items:
            alts = [a.strip() for a in re.split(r" or (?![^<]*>)", item)]
            first = alts[0]
            settable.append(first)
            for alt in alts[1:]:
                if alt.startswith("."):
                    settable.append(first.rsplit(".", 1)[0] + alt)
    return Capabilities(gpus, settable, "SET file" in flat, initial, catalog="--catalog KEYS" in check_flat)


_AT = re.compile(r"(\(u, v\) = \([^)]*\)|u = 0) at \(([^)]*)\)")


def coordinate_points(boundary: Mapping[str, Any]) -> list[tuple[str, tuple[float, float, float]]]:
    """Where a boundary's local coordinates start, read from a check report's human-readable `details`: for
    builds whose case view has no `frames`."""
    out = []
    for label, at in _AT.findall(str(boundary.get("details", ""))):
        try:
            xs = [float(c) for c in at.split(",")] + [0.0, 0.0]
        except ValueError:
            continue
        out.append((label, (xs[0], xs[1], xs[2])))
    return out


_MATH = {
    name: getattr(math, name)
    for name in ("sin", "cos", "tan", "exp", "log", "sqrt", "tanh", "sinh", "cosh", "atan", "asin", "acos", "fabs")
}
_MATH |= {"abs": abs, "pi": math.pi, "atan2": math.atan2, "min": min, "max": max, "pow": pow}


def sample_expressions(
    components: Sequence[Any], points: Sequence[Sequence[float]], dim: int, t: float = 0.0
) -> list[tuple[float, float, float]] | None:
    """A vector of numbers or expressions of x, y, z and t at each point, or None for what this cannot read
    (a boundary's local u, v, n; a data table).

    For builds without `case.boundary_values`, which evaluates the profile itself."""
    if len(components) != dim:
        return None
    trees = []
    for c in components:
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            trees.append(ast.parse(repr(float(c)), mode="eval"))
            continue
        if not isinstance(c, str):
            return None
        try:
            tree = ast.parse(c.replace("^", "**"), mode="eval")
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            ok = isinstance(
                node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Load, ast.operator, ast.unaryop)
            )
            if isinstance(node, ast.Name):
                ok = node.id in ("x", "y", "z", "t") or node.id in _MATH
            elif isinstance(node, ast.Call):
                ok = isinstance(node.func, ast.Name) and node.func.id in _MATH and not node.keywords
            if not ok:
                return None
        trees.append(tree)
    codes = [compile(tree, "<expression>", "eval") for tree in trees]
    out = []
    for p in points:
        env = {**_MATH, "x": p[0], "y": p[1], "z": p[2] if len(p) > 2 else 0.0, "t": t}
        try:
            xs = [float(eval(code, {"__builtins__": {}}, env)) for code in codes]
        except (ArithmeticError, ValueError, TypeError):
            return None
        xs += [0.0] * (3 - dim)
        out.append((xs[0], xs[1], xs[2]))
    return out


# `mallard` where a file is: this machine or a remote's shell (ssh, a helper pod). Every such call goes through
# here, as every local one goes through `Mallard`, so the session protocol can take them over in one place.
Exec = Callable[[Sequence[str]], tuple[int, bytes, bytes]]


def file_info(run: Exec, mallard: Sequence[str], path: str) -> dict[str, Any]:
    """`mallard info FILE --format json` run where FILE is: its kind, format and version, dimension, precision,
    mesh, fields and species; a restart's `continuable` and `incompatibilities` from builds that report them.
    Raises MallardError (the command's own message) when it cannot read the file. A local file goes through
    `Mallard.file_info`, the session's `file.info`."""
    code, out, err = run([*mallard, "info", path, "--format", "json"])
    if code != 0:
        raise MallardError(err.decode("utf-8", "replace").strip() or f"mallard info {path}: exit {code}")
    try:
        data: dict[str, Any] = json.loads(out)
    except json.JSONDecodeError as e:
        raise MallardError(f"mallard info {path}: not JSON: {e}") from e
    return data


def local_exec(argv: Sequence[str]) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(list(argv), capture_output=True, timeout=300, check=False)
    return proc.returncode, proc.stdout, proc.stderr
