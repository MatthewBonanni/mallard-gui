"""The case's optional features: a single table whose presence in the input turns a part of the physics or
the output on. The truth is the input: a feature with no table is off, whatever its keys' defaults say. A few
also have a key that turns them off while the table stays (`[chemistry] enabled`): on means the table is there
and that key is not false. A list of entries (`[[sponges]]`, `[[probes]]`) is no feature: whether there are any
is what its list shows, and Add makes one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Feature:
    path: str  # the catalog's path of its table: "chemistry", "boundaries.wall_model"
    name: str
    blurb: str  # what turning it on does, in one sentence
    enabled_key: str = ""  # a key of the table that turns it off while the table stays
    needs_mechanism: bool = False  # only for a reacting mixture: turning it on asks for the mechanism first


FEATURES: dict[str, Feature] = {
    f.path: f
    for f in (
        Feature(
            "chemistry",
            "Chemistry",
            "Finite-rate chemistry of a reacting mixture, from the mechanism set in Physics.",
            enabled_key="enabled",
            needs_mechanism=True,
        ),
        Feature("les", "LES", "Large-eddy simulation: a subgrid-scale eddy viscosity added to the molecular one."),
        Feature("les.combustion", "LES combustion", "A subgrid model of turbulent combustion for reacting LES."),
        Feature("radiation", "Radiation", "Radiative heat loss of the gas in the optically thin limit."),
        Feature("particles", "Particles", "Lagrangian particles carried by the flow: tracers or solid particles."),
        Feature("statistics", "Statistics", "Running time averages of the flow, with products and spectra."),
        Feature("source", "Body forces", "Uniform volume sources over the domain: gravity and a driving mass flow."),
        Feature("boundaries.wall_model", "Wall model", "The wall's shear stress and heat flux from a wall model."),
        Feature("boundaries.turbulence", "Inflow turbulence", "Synthetic turbulence added to the inflow."),
        Feature("initialize.turbulence", "Initial turbulence", "Synthetic turbulence added to the initial state."),
        Feature(
            "initialize.regions.turbulence",
            "Initial turbulence",
            "Synthetic turbulence added to the region's initial state.",
        ),
    )
}


def is_on(feature: Feature, value: Any) -> bool:
    """On, from what the input has at the feature's path (None: nothing)."""
    if not isinstance(value, dict):
        return False
    return not (feature.enabled_key and value.get(feature.enabled_key) is False)


def has_content(feature: Feature, value: Any) -> bool:
    """Whether turning it off throws away settings of the user's (more than the bare table)."""
    return isinstance(value, dict) and any(k != feature.enabled_key for k in value)


def missing_mechanism(feature: Feature, data: dict[str, Any]) -> bool:
    """Whether turning it on needs the mechanism first: the physics is not a mixture with one."""
    physics = data.get("physics")
    if not isinstance(physics, dict):
        physics = {}
    return feature.needs_mechanism and not (physics.get("gas") == "mixture" and physics.get("mechanism"))
