"""`tools/unit_keys.py`: the reference's Units table, in its shorthand, to catalog paths."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

TOOL = Path(__file__).resolve().parent.parent / "tools" / "unit_keys.py"

DOCS = """
| Dimension | Keys |
|---|---|
| Time (s) | `run.dt`, `t_stop`; `statistics.phase.t_start`, `period` |
| Length (m) | `particles.points`, `lattice.lo`, `hi`, `diameter`; `wall_model.exchange_distance`; {regions} |
| Angle (plain numbers in degrees) | `periodic.angle` |
| Temperature (K) | `T` of `initialize`, `boundaries` |
| Density (kg/m^3) | `rho` of `initialize` (`analytical`), `dirichlet` boundaries and `probes.reference` |
| Sources (per unit volume) | `source.rho` (kg/(m^3 s)), and the same of `[[sources]]` |

Every other real-valued key is dimensionless. `statistics.phase.level` has the dimension of its
`variable` (`P`: pressure, ...).
""".replace("{regions}", "region lengths (`center`, `radius`, ...)")

CATALOG = {
    "run.dt", "run.t_stop", "statistics.phase.t_start", "statistics.phase.period", "statistics.phase.level",
    "particles.points", "particles.lattice.lo", "particles.lattice.hi", "particles.diameter",
    "boundaries.wall_model.exchange_distance", "regions.<name>.center", "probes.region.radius", "probes.radius",
    "periodic.angle", "initialize.T", "boundaries.T", "initialize.rho", "boundaries.rho", "probes.reference",
    "source.rho", "sources.rho",
}  # fmt: skip


def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("unit_keys_tool", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_units_tables_shorthand_resolves_to_catalog_paths() -> None:
    dims, fixed, unknown, missing = tool().build(DOCS, CATALOG)
    assert dims["run.t_stop"] == dims["statistics.phase.period"] == "time (s)"  # continues the group's table
    assert dims["particles.lattice.hi"] == "length (m)"  # continues the deepest table that has it
    assert dims["particles.diameter"] == "length (m)" and "particles.lattice.diameter" not in dims
    assert dims["boundaries.wall_model.exchange_distance"] == "length (m)"  # a table under any section
    assert dims["regions.<name>.center"] == dims["probes.region.radius"] == "length (m)"
    assert "probes.radius" not in dims  # a probe's own radius is not a region's
    assert dims["initialize.T"] == dims["boundaries.T"] == "temperature (K)"
    assert dims["boundaries.rho"] == "density (kg/m^3)" and dims["probes.reference.rho"] == "density (kg/m^3)"
    assert dims["source.rho"] == "mass source (kg/(m^3 s))"
    assert fixed == {"periodic.angle": "deg"}
    assert {"sources.rho", "statistics.phase.level"} <= set(unknown) and "total" in unknown["sources.rho"]
    assert "sources.rho" not in dims
    assert "probes.reference.rho" in missing  # kept, as written, though the general catalog lacks it
