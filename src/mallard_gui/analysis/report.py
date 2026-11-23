"""What the analysis overlays show, read from the check report's JSON as Mallard computes it: the zones
without a condition, the periodic pairs as the mesh joined them, the first-cell y+ target and the mesh-quality
histograms. Nothing here is derived from the mesh."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The order the inspector lists the metrics in, by the field names of the report and the mesh fields
METRICS = ("SKEWNESS", "ASPECT_RATIO", "NON_ORTHOGONALITY", "VOLUME_RATIO")
# Relative node offset above which a pair is reported as not matching (Mallard's tolerance is 1e-6 of an edge)
PERIODIC_MATCH = 1e-6


@dataclass(frozen=True)
class Histogram:
    metric: str
    name: str
    units: str
    limit: float
    edges: list[float]
    counts: list[int]
    above_limit: int
    worst: list[dict[str, Any]] = field(default_factory=list)

    @property
    def cells(self) -> int:
        return sum(self.counts)

    def bin_range(self, k: int) -> tuple[float, float]:
        return self.edges[k], self.edges[k + 1]


@dataclass(frozen=True)
class PeriodicPair:
    zones: tuple[str, str] | None
    rotation: bool
    translation: list[float] | None
    angle: float | None
    center: list[float] | None
    faces: int
    area: float
    node_offset_max: float | None  # None on a mesh read in parallel, whose offsets Mallard does not measure
    node_offset_relative: float | None

    @property
    def label(self) -> str:
        return " / ".join(self.zones) if self.zones else "periodic"

    @property
    def matches(self) -> bool:
        """Within Mallard's tolerance, or not measured: Mallard joined the pair either way."""
        return self.node_offset_relative is None or self.node_offset_relative <= PERIODIC_MATCH

    @property
    def map_text(self) -> str:
        if self.rotation:
            center = ", ".join(f"{c:g}" for c in self.center or [])
            return f"rotation {self.angle:g}° about ({center})"
        return "translation (" + ", ".join(f"{c:g}" for c in self.translation or []) + ")"


def unassigned(data: dict[str, Any]) -> dict[str, int]:
    """Zones whose faces have no condition, with their face counts."""
    return {str(u["zone"]): int(u.get("faces", 0)) for u in data.get("unassigned") or [] if "zone" in u}


def _float_or_none(x: Any) -> float | None:
    return None if x is None else float(x)


def periodic_pairs(data: dict[str, Any]) -> list[PeriodicPair]:
    out = []
    for p in data.get("periodic") or []:
        zones = p.get("zones")
        out.append(
            PeriodicPair(
                (str(zones[0]), str(zones[1])) if zones else None,
                p.get("map") == "rotation",
                p.get("translation"),
                p.get("angle"),
                p.get("center"),
                int(p.get("faces", 0)),
                float(p.get("area", 0.0)),
                _float_or_none(p.get("node_offset_max")),
                _float_or_none(p.get("node_offset_relative")),
            )
        )
    return out


def y_plus(data: dict[str, Any]) -> dict[str, Any] | None:
    """The resolution report's first-cell y+ estimate: largest, mean, target and what u_tau is from."""
    res = data.get("resolution") or {}
    largest, mean, u_tau = res.get("y_plus_max"), res.get("y_plus_mean"), res.get("u_tau")
    if not largest and not mean:
        return None
    return {
        "max": (largest or {}).get("value"),
        "mean": (mean or {}).get("value"),
        "target": (largest or {}).get("target", ""),
        "status": (largest or {}).get("status", ""),
        "basis": (u_tau or {}).get("basis", ""),
    }


def histograms(data: dict[str, Any]) -> list[Histogram]:
    quality = data.get("mesh_quality") or {}
    out = []
    for metric in METRICS:
        q = quality.get(metric)
        hist = (q or {}).get("histogram")
        if not q or not hist:
            continue
        out.append(
            Histogram(
                metric,
                str(q.get("name", metric.lower())),
                str(q.get("units", "")),
                float(q.get("limit", 0.0)),
                [float(e) for e in hist["edges"]],
                [int(c) for c in hist["counts"]],
                int(q.get("above_limit", 0)),
                list(q.get("worst") or []),
            )
        )
    return out
