from __future__ import annotations

import copy
import json
import shutil
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from mallard_gui.macos import hide_from_dock
from mallard_gui.mallard import (
    Analysis,
    Capabilities,
    CheckReport,
    Diagnostic,
    KeyInfo,
    Mallard,
    MallardError,
    Species,
    analysis_of,
    capabilities_from_help,
    find_mallard,
    parse_catalog,
)

FIXTURES = Path(__file__).parent / "fixtures"
H2O2_SPECIES = ("H2", "H", "O", "O2", "OH", "H2O", "HO2", "H2O2", "AR", "N2")  # list species on the h2o2 mechanism

hide_from_dock()


def fixture_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MALLARD_GUI_CONFIG_DIR", str(tmp_path_factory.mktemp("config")))
    monkeypatch.setenv("MALLARD_GUI_CACHE_DIR", str(tmp_path_factory.mktemp("cache")))


@pytest.fixture
def cylinder_project(tmp_path: Path) -> Path:
    shutil.copy(FIXTURES / "cylinder_input.toml", tmp_path / "input.toml")
    shutil.copy(FIXTURES / "cylinder.msh", tmp_path / "cylinder.msh")
    return tmp_path


@pytest.fixture
def real_mallard() -> Mallard:
    try:
        return Mallard(find_mallard(), threads=2)
    except MallardError:
        pytest.skip("no mallard build: set MALLARD or put mallard on PATH")


GPU_BYTES = {"a100-80": 80e9, "a100-40": 40e9, "h100-80": 80e9}


def sized(data: dict[str, Any], extra: Sequence[str]) -> dict[str, Any]:
    """`check --ranks N --memory M` as Mallard answers it for the cylinder: a part of 1536 / N owned cells with a
    halo growing with N, its peak memory by the check's own bytes per cell, sized for M."""
    args = dict(zip(extra[::2], extra[1::2], strict=False))
    if not data.get("sizing"):
        data = fixture_json("cylinder_check_cost.json")
    n = int(args.get("--ranks", 1))
    mem, sizing = data["memory"], data["sizing"]
    owned = -(-1536 // n)
    halo = 0 if n == 1 else min(1536 - owned, 40 * n)
    mem.update(target_ranks=n, target_owned=owned, target_halo=halo)
    mem["target_bytes_peak"] = sizing["fixed_bytes"] + sizing["bytes_per_cell"] * (owned + halo)
    data["cost"]["ranks"] = n
    if args.get("--memory") in GPU_BYTES:
        sizing.update(name=args["--memory"], memory=GPU_BYTES[args["--memory"]], gpu=True)
    return data


def preset_of(data: dict[str, Any]) -> str | None:
    numerics = data.get("numerics", {})
    return numerics.get("preset") or (None if "face_reconstruction" in numerics else "high_order")


class FakeMallard:
    """Answers `list keys` and `check` from captured outputs of the cylinder case."""

    def __init__(self, check: str = "cylinder_check.json", stderr: str = "", view: bool = False) -> None:
        self.executable = Path("/nonexistent/mallard")
        self.view = view  # answer with the captured case view, as `--view` would write it
        self.threads = 2
        self.check_fixture = check
        self.restart_check = ""  # the report of `check --restart`; empty: a build that classifies no key
        self.converted_check = ""  # the report of `check --restart` on a file under converted/, if other
        self.models_fixture = ""  # `list models`; empty: a build without it
        self.convert_fixture = ""  # `restart convert`'s summary; empty: a build without it
        self.converts: list[dict[str, str]] = []
        self.stderr = stderr
        self.checks = 0
        self.extra: list[str] = []
        self.calls: list[list[str]] = []
        self.key_entries: dict[str, dict[str, Any]] = {}  # catalog entries in place of the captured ones
        self.setups: list[str] = []  # the commands that set the case up (its mesh), in order

    def data_dir(self) -> Path | None:
        return None

    def version(self) -> str:
        return "0.10.0-test"

    def list_keys(self, input_file: Path | None = None) -> tuple[dict[str, KeyInfo] | None, list[Diagnostic]]:
        if input_file is not None:
            self.setups.append("list keys")
        return parse_catalog(self._keys(input_file)), []

    def _keys(self, input_file: Path | None) -> dict[str, Any]:
        data = fixture_json("cylinder_keys.json" if input_file else "general_keys.json")
        if input_file is not None and 'gas = "mixture"' in input_file.read_text():
            # A mixture case reads the compositions the captured perfect-gas case does not
            text = input_file.read_text()
            for table in ("initialize", "boundaries"):
                for basis in ("X", "Y"):
                    data["keys"][f"{table}.{basis}"] = {"set": f"{basis} = {{" in text}
        for path, entry in self.key_entries.items():
            entry = dict(entry)
            case = entry.pop("case", False)  # also a key the case reads, beyond the captured case's
            if input_file is None or path in data["keys"] or case:
                data["keys"][path] = {"set": data["keys"].get(path, {}).get("set", False), **entry}
        return data

    def check(
        self,
        input_file: Path,
        view_dir: Path | None = None,
        extra: Sequence[str] = (),
        catalog: Path | None = None,
    ) -> CheckReport:
        self.setups.append("check")
        if catalog is not None:
            catalog.write_text(json.dumps(self._keys(input_file)["keys"]), encoding="utf-8")
        self.checks += 1
        self.extra = list(extra)
        self.calls.append(self.extra)
        name = "cylinder_check_cost.json" if extra else self.check_fixture
        if "--restart" in extra:
            name = self.restart_check or self.check_fixture
            if self.converted_check and Path(extra[list(extra).index("--restart") + 1]).parent.name == "converted":
                name = self.converted_check
        data = fixture_json(name)
        if "--ranks" in extra or "--memory" in extra:
            data = sized(data, extra)
        report = CheckReport.from_json(data, self.stderr)
        if self.view and view_dir is not None:
            own = FIXTURES / self.check_fixture.replace("_check.json", "_view") / "case.vtm"  # box_check: box_view
            report.view = own if own.exists() else FIXTURES / "cylinder_view" / "case.vtm"
        return report

    def capabilities(self) -> Capabilities:
        return capabilities_from_help(self.help_text("check"), self.help_text("ctl"))

    # A build older than the session's catalogs: every request falls back.
    def units_catalog(self) -> dict[str, Any] | None:
        return None

    def boundary_values(self, input_file: Path, text: str | None, out_dir: Path) -> dict[str, Any] | None:
        return None

    def case_files(self, input_file: Path, text: str | None = None) -> dict[str, Any] | None:
        return None

    def gas_models(self, input_file: Path) -> dict[str, Any] | None:
        """`list models` as the build gives it, with the input's model; None (a build without it) unless set."""
        if not self.models_fixture:
            return None
        data = fixture_json(self.models_fixture)
        physics = tomllib.loads(input_file.read_text(encoding="utf-8")).get("physics", {})
        chemistry = tomllib.loads(input_file.read_text(encoding="utf-8")).get("chemistry")
        gas = physics.get("gas", "perfect")
        reacting = isinstance(chemistry, dict) and chemistry.get("enabled", True)
        data["current"] = {"mixture": "finite_rate" if reacting else "frozen"}.get(gas, gas)
        return data

    def restart_convert(
        self, restart: Path, input_file: Path, out: Path | None = None, accept: str = ""
    ) -> dict[str, Any] | None:
        self.converts.append({"restart": str(restart), "out": str(out) if out else "", "accept": accept})
        if not self.convert_fixture:
            return None
        if out is not None:
            out.write_bytes(restart.read_bytes() + b"converted\n")
        return fixture_json(self.convert_fixture)

    def file_info(self, path: Path) -> dict[str, Any]:
        return {"schema": "mallard-file-info", "kind": "restart"}

    def list_species(self, input_file: Path) -> Species | None:
        """A mixture's species from its mechanism (the h2o2 mechanism's, whatever file it names), as
        `list species` gives them; none for a single gas."""
        physics = tomllib.loads(input_file.read_text(encoding="utf-8")).get("physics", {})
        if physics.get("gas") != "mixture":
            return Species([])
        return Species(list(H2O2_SPECIES))

    def list_fields(self, input_file: Path | None = None) -> dict[str, Any] | None:
        """`list fields` as the build gives it for the cylinder (any case) and without an input."""
        return fixture_json("cylinder_fields.json" if input_file else "field_catalog.json")

    def resolve(self, input_file: Path) -> dict[str, Any] | None:
        """`mallard input resolve`, as the captured resolutions of the cylinder under each preset give it: the
        preset's keys (the default's without a preset or a reconstruction table) under the input's own."""
        data = tomllib.loads(input_file.read_text(encoding="utf-8"))
        name = preset_of(data)
        base = copy.deepcopy(fixture_json("preset_resolutions.json")[name or "none"])
        numerics = data.get("numerics", {})
        merged = {**base["numerics"], **{k: v for k, v in numerics.items() if k != "face_reconstruction"}}
        merged["face_reconstruction"] = {
            **base["numerics"]["face_reconstruction"],
            **numerics.get("face_reconstruction", {}),
        }
        if not name:
            merged.pop("preset", None)
        run = dict(data.get("run", {}))
        if "cfl" not in run and "dt" not in run and base["run"]["cfl"] is not None:
            run["cfl"] = base["run"]["cfl"]
        return {**data, "numerics": merged, "run": run}

    def preset_line(self, input_file: Path) -> str | None:
        self.setups.append("preset line")
        data = tomllib.loads(input_file.read_text(encoding="utf-8"))
        name = preset_of(data)
        if not name:
            return None
        numerics = data.get("numerics", {})
        sets = {
            f"numerics.{k}": v
            for k, v in numerics.items()
            if k in ("riemann_solver", "convective_flux", "time_integrator")
        }
        sets |= {f"numerics.face_reconstruction.{k}": v for k, v in numerics.get("face_reconstruction", {}).items()}
        sets |= {f"run.{k}": v for k, v in data.get("run", {}).items() if k in ("cfl", "dt")}
        notes = [] if "preset" in numerics else ["the default since 0.10.0"]
        if sets:
            notes.append("the input sets " + ", ".join(f"{k} = {json.dumps(v)}" for k, v in sets.items()))
        return name + (f" ({'; '.join(notes)})" if notes else "")

    def close(self) -> None:
        pass

    def supersede(self, name: str) -> None:
        """No command of the fake runs long enough to stop."""

    def help_text(self, command: str) -> str:
        path = FIXTURES / f"{command}_help.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def initial_state(self, input_file: Path, out_dir: Path) -> Path:
        """The cylinder's captured initial state, copied into out_dir as Mallard would write it there."""
        self.initials = getattr(self, "initials", 0) + 1
        out_dir.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(FIXTURES / "cylinder_analysis" / "initial_000000.vtu", out_dir / "initial_000000.vtu"))

    def analyze(self, input_file: Path, out_dir: Path) -> Analysis:
        """The cylinder's captured `check --view --mesh-fields --initial`, copied into out_dir."""
        shutil.copytree(FIXTURES / "cylinder_analysis", out_dir, dirs_exist_ok=True)
        report = CheckReport.from_json(json.loads((out_dir / "check.json").read_text(encoding="utf-8")))
        return analysis_of(report, out_dir)


class RequestsMallard(FakeMallard):
    """A build with the session's newer requests: the case's files (its mesh missing) and a restart this build
    refuses, as `case.files` and `file.info` answer them."""

    REFUSAL = "Restart file holds a 2D run, but Mallard was built with Mallard_DIM = 3."

    def case_files(self, input_file: Path, text: str | None = None) -> dict[str, Any] | None:
        root = input_file.parent
        return {
            "schema": "mallard-case-files",
            "version": 1,
            "input": input_file.name,
            "complete": True,
            "files": [
                {"role": "input", "path": input_file.name, "resolved": str(input_file), "exists": True, "bytes": 1890},
                {"role": "mesh", "path": "missing.msh", "resolved": str(root / "missing.msh"), "exists": False,
                 "bytes": None},
            ],
        }  # fmt: skip

    def file_info(self, path: Path) -> dict[str, Any]:
        return {"schema": "mallard-file-info", "kind": "restart", "continuable": False,
                "incompatibilities": [self.REFUSAL]}  # fmt: skip
