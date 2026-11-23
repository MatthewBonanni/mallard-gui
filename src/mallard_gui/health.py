"""A run's health, live: what Mallard's run log says first, then what the GUI can see in the numbers.

Mallard's own records come first and are taken as they are: its warnings (`W-HEALTH-MARGIN` and the
others after setup), the divergence report, the error that ended a run, a `diverged` event and the end
record. The GUI adds what the run log carries but does not judge, from the progress records and the probe,
force and reduction CSV files:

- a NaN or Inf: a progress value that was a number turning `null` (the run log writes non-finite numbers
  as `null`), or a non-finite value in a CSV file;
- the CFL number above the time integrator's limit (the start record's check), or a spike above
  `CFL_SPIKE` times its recent median;
- a dt collapse: `dt` below `DT_COLLAPSE` of its recent median;
- the run log stalled: no progress record for `STALL_FACTOR` of the usual interval (at least
  `STALL_MIN_SECONDS`) while the job runs;
- diverging residuals and monitors: the conservation ledger's unexplained residual, or a `max_*`
  monitor, growing by `DIVERGE_GROWTH` over the last `DIVERGE_ROWS` rows without a pause;
- the wall-time limit (`[run] t_wall_stop`, or the job's) approaching: the run past `WALL_FRACTION` of
  it, or its ETA beyond it.

Each kind of alert is raised once; the GUI's own re-arm when their condition clears.
"""

from __future__ import annotations

import itertools
import math
import statistics
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .runlog import RunState

CRITICAL, WARNING, INFO = "critical", "warning", "info"
CFL_SPIKE = 2.0
DT_COLLAPSE = 0.1
REARM_ROWS = 5  # progress rows a GUI alert's condition must stay clear before it is announced again
RECENT = 20  # progress rows a median is taken over
MIN_HISTORY = 5
STALL_FACTOR = 10.0
STALL_MIN_SECONDS = 120.0
DIVERGE_ROWS = 6
DIVERGE_GROWTH = 100.0
RESIDUAL_FLOOR = 1e-10  # a relative ledger residual below this is round-off whatever it does
WALL_FRACTION = 0.9
# Mallard's warnings about the solution itself; its others (formats, caches, sockets) are information.
HEALTH_CODES = ("W-HEALTH-",)
DIVERGED = "mallard:diverged"
_NULLABLE = {"wall_per_step", "cells_per_second", "eta", "memory_device", "memory_device_total"}


@dataclass(frozen=True)
class Alert:
    key: str  # one alert per key: `mallard:W-HEALTH-MARGIN`, `gui:cfl`, ...
    kind: str  # nan | cfl | dt | stalled | diverging | wall | mallard | end
    severity: str
    title: str
    text: str
    source: str  # "mallard" (its run log said so) or "gui" (a heuristic of this GUI)
    step: int | None = None
    t: float | None = None
    code: str = ""
    docs: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "text": self.text,
            "source": self.source,
            "step": self.step,
            "t": self.t,
            "code": self.code,
            "docs": self.docs,
        }


def wall_limit(start: dict[str, Any]) -> float | None:
    """`[run] t_wall_stop` of the run's resolved input, from its start record."""
    inp = ((start.get("provenance") or {}).get("input")) or {}
    for text in (inp.get("resolved"), inp.get("text")):
        if not isinstance(text, str):
            continue
        try:
            run = tomllib.loads(text).get("run") or {}
        except tomllib.TOMLDecodeError:
            continue
        limit = run.get("t_wall_stop")
        if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
            return float(limit)
    return None


def cfl_limit(start: dict[str, Any]) -> float | None:
    limit = (((start.get("check") or {}).get("time_step")) or {}).get("cfl_limit")
    return float(limit) if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0 else None


def _numbers(record: dict[str, Any], prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Every leaf of a progress record that can carry a measured number, by dotted name."""
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _numbers(value, f"{name}.")
        elif not isinstance(value, (list, str, bool)):
            yield name, value


def _watched(name: str) -> bool:
    top = name.split(".", 1)[0]
    if top in ("dt", "cfl"):
        return True
    if top in ("monitors", "integrals", "probes") or top.startswith("forces_"):
        return not name.endswith((".step", ".t"))
    return name.startswith("health.margins.") and name.endswith(".value")


def _fmt(v: float | None) -> str:
    if v is None:
        return "–"
    return f"{v:.3g}"


def _duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60} min"
    if s >= 60:
        return f"{s // 60} min {s % 60} s"
    return f"{s} s"


class HealthMonitor:
    """The alerts of one run, fed its RunState after every poll; `update` returns the new ones."""

    def __init__(self, job_wall_limit: float | None = None) -> None:
        self.job_wall_limit = job_wall_limit  # a batch job's time limit, in seconds, where known
        self._reset(None)

    def _reset(self, st: RunState | None) -> None:
        self._state = st
        self.alerts: list[Alert] = []
        self._raised: set[str] = set()
        self._quiet: dict[str, int] = {}
        self._new: list[Alert] = []
        self._warnings = self._events = self._records = 0
        self._ended = False
        self._divergence = False
        self._numeric: set[str] = set()
        self._csv_seen: dict[tuple[str, str, int], int] = {}
        self._residuals: dict[str, list[float]] = {}
        self._progress_at: list[float] = []
        self._last_step: Any = None

    @property
    def active(self) -> list[Alert]:
        return list(self.alerts)

    @property
    def worst(self) -> str:
        sev = {a.severity for a in self.alerts}
        return CRITICAL if CRITICAL in sev else WARNING if WARNING in sev else INFO if sev else ""

    def update(self, st: RunState, now: float, running: bool = True) -> list[Alert]:
        if st is not self._state:
            self._reset(st)
        self._new = []
        self._mallard(st)
        if st.records != self._records:
            self._records = st.records
            if st.last and st.last.get("step") != self._last_step:
                self._last_step = st.last.get("step")
                self._progress_at.append(now)
                self._clear("gui:stalled")
            self._nonfinite(st)
            self._cfl(st)
            self._dt(st)
            self._diverging(st)
            self._wall(st)
        self._csv(st)
        if running and st.end is None:
            self._stalled(st, now)
        return self._new

    def _raise(self, alert: Alert, update: bool = True) -> None:
        """A new alert, or the latest of one still raised, in place: it is announced once."""
        self._quiet.pop(alert.key, None)
        if alert.key in self._raised:
            if update:
                for items in (self.alerts, self._new):
                    for i, a in enumerate(items):
                        if a.key == alert.key:
                            items[i] = alert
            return
        self._raised.add(alert.key)
        self.alerts = [a for a in self.alerts if a.key != alert.key] + [alert]
        self._new.append(alert)

    def _clear(self, key: str) -> None:
        """A GUI alert whose condition is gone for REARM_ROWS rows in a row is announced again if it returns."""
        if key in self._raised:
            self._quiet[key] = self._quiet.get(key, 0) + 1
            if self._quiet[key] >= REARM_ROWS:
                self._raised.discard(key)
                del self._quiet[key]

    def _where(self, st: RunState) -> dict[str, Any]:
        last = st.last
        step = last.get("step")
        t = last.get("t")
        return {
            "step": step if isinstance(step, int) else None,
            "t": float(t) if isinstance(t, (int, float)) else None,
        }

    # What Mallard says

    def _mallard(self, st: RunState) -> None:
        """A divergence comes as an event, the divergence report, an `E-DIVERGED` error and the end record:
        one alert, which the report (the richest) completes."""
        for e in st.events[self._events :]:
            step = e.get("step") if isinstance(e.get("step"), int) else None
            if e.get("kind") == "diverged":
                self._raise(
                    Alert(
                        DIVERGED,
                        "nan",
                        CRITICAL,
                        "Diverged",
                        str(e.get("text", "")),
                        "mallard",
                        step,
                        code="E-DIVERGED",
                    ),
                    update=False,
                )
            elif e.get("kind") == "trigger":
                self._raise(
                    Alert(
                        f"mallard:trigger:{e.get('name', '')}:{e.get('firing', '')}",
                        "mallard",
                        INFO,
                        f"Trigger {e.get('name', '')}",
                        str(e.get("text", "")),
                        "mallard",
                        step,
                    )
                )
        self._events = len(st.events)
        for w in st.warnings[self._warnings :]:
            code = str(w.get("code", ""))
            error = w.get("type") == "error" or code.startswith("E-")
            health = any(code.startswith(c) for c in HEALTH_CODES)
            severity = CRITICAL if error else WARNING if health else INFO
            self._raise(
                Alert(
                    DIVERGED if code == "E-DIVERGED" else f"mallard:{code}",
                    "nan" if code == "E-DIVERGED" else "mallard",
                    severity,
                    "Diverged" if code == "E-DIVERGED" else code or ("error" if error else "warning"),
                    str(w.get("message", "")),
                    "mallard",
                    code=code,
                    docs=str(w.get("docs", "")),
                    **self._where(st),
                ),
                update=False,
            )
        self._warnings = len(st.warnings)
        div = st.divergence
        if isinstance(div, dict) and not self._divergence:
            self._divergence = True
            causes = div.get("causes") or []
            first = causes[0] if causes and isinstance(causes[0], dict) else {}
            text = f"{div.get('invalid_cells', '?')} invalid cells"
            if first:
                text += f"; most likely: {first.get('title', first.get('kind', ''))}"
                if first.get("fix"):
                    text += f". {first['fix']}"
            step = div.get("step")
            t = div.get("t")
            self._raise(
                Alert(
                    DIVERGED,
                    "nan",
                    CRITICAL,
                    "Diverged",
                    text,
                    "mallard",
                    step=step if isinstance(step, int) else None,
                    t=float(t) if isinstance(t, (int, float)) else None,
                    code="E-DIVERGED",
                    docs="docs/guide/running.md#the-divergence-report",
                )
            )
        if st.end is not None and not self._ended:
            self._ended = True
            outcome = str(st.end.get("outcome", "ended"))
            failed = outcome in ("diverged", "error")
            if failed and any(a.source == "mallard" and a.severity == CRITICAL for a in self.alerts):
                return  # the error that ended it is already announced
            step = st.end.get("step")
            self._raise(
                Alert(
                    f"mallard:end:{st.starts}",
                    "end",
                    CRITICAL if failed else INFO,
                    f"Run {outcome.replace('_', ' ')}",
                    str(st.end.get("reason") or st.end.get("cause") or ""),
                    "mallard",
                    step=step if isinstance(step, int) else None,
                )
            )
        elif st.end is None:
            self._ended = False

    # What the GUI sees

    def _nonfinite(self, st: RunState) -> None:
        bad = []
        for name, value in _numbers(st.last):
            if name in _NULLABLE or not _watched(name):
                continue
            if isinstance(value, (int, float)):
                if math.isfinite(value):
                    self._numeric.add(name)
                else:
                    bad.append(name)
            elif value is None and name in self._numeric:
                bad.append(name)
        if bad:
            shown = ", ".join(bad[:4]) + (f" and {len(bad) - 4} more" if len(bad) > 4 else "")
            self._raise(
                Alert(
                    "gui:nan",
                    "nan",
                    CRITICAL,
                    "NaN or Inf",
                    f"not finite in the run log: {shown}",
                    "gui",
                    **self._where(st),
                )
            )

    def _csv(self, st: RunState) -> None:
        for path, c in st.csv.items():
            for (name, point), h in c.series.items():
                key = (path, name, point)
                ts, vs = h.recent()  # the latest samples; those since the last look are at its end
                start = max(0, len(vs) - (h.samples - self._csv_seen.get(key, 0)))
                for i in range(start, len(vs)):
                    if not math.isfinite(vs[i]):
                        label = path.rsplit("/", 1)[-1]
                        self._raise(
                            Alert(
                                f"gui:nan:{path}",
                                "nan",
                                CRITICAL,
                                "NaN or Inf",
                                f"{name}{f' [{point}]' if point else ''} in {label} at t {_fmt(ts[i])}",
                                "gui",
                                t=ts[i],
                            )
                        )
                        break
                self._csv_seen[key] = h.samples

    def _recent(self, st: RunState, name: str) -> tuple[list[float], float | None]:
        s = st.series.get(f"run/{name}")
        if s is None or not s.values:
            return [], None
        values = [v for v in s.values[-RECENT - 1 : -1] if math.isfinite(v) and v > 0]
        last = s.values[-1]
        return values, last if math.isfinite(last) else None

    def _cfl(self, st: RunState) -> None:
        history, cfl = self._recent(st, "cfl")
        if cfl is None:
            return
        limit = cfl_limit(st.start)
        if limit is not None and cfl > limit * (1 + 1e-9):
            self._raise(
                Alert(
                    "gui:cfl-limit",
                    "cfl",
                    CRITICAL,
                    "CFL above the limit",
                    f"CFL {_fmt(cfl)} is above the time integrator's stability limit {_fmt(limit)}",
                    "gui",
                    **self._where(st),
                )
            )
        elif limit is not None:
            self._clear("gui:cfl-limit")
        if len(history) >= MIN_HISTORY:
            median = statistics.median(history)
            if cfl > CFL_SPIKE * median:
                self._raise(
                    Alert(
                        "gui:cfl-spike",
                        "cfl",
                        WARNING,
                        "CFL spike",
                        f"CFL {_fmt(cfl)}, {cfl / median:.1f} times its recent median {_fmt(median)}",
                        "gui",
                        **self._where(st),
                    )
                )
            else:
                self._clear("gui:cfl-spike")

    def _dt(self, st: RunState) -> None:
        history, dt = self._recent(st, "dt")
        if dt is None or len(history) < MIN_HISTORY:
            return
        median = statistics.median(history)
        if dt < DT_COLLAPSE * median:
            self._raise(
                Alert(
                    "gui:dt",
                    "dt",
                    CRITICAL,
                    "Time step collapsed",
                    f"dt {_fmt(dt)}, {median / dt:.0f} times below its recent median {_fmt(median)}",
                    "gui",
                    **self._where(st),
                )
            )
        else:
            self._clear("gui:dt")

    def _diverging(self, st: RunState) -> None:
        tails: dict[str, list[float]] = {}
        for s in st.series.values():
            if s.group == "monitors" and s.name.lower().startswith("max_"):
                tails[s.name] = s.values[-DIVERGE_ROWS:]
        ledger = (st.last.get("health") or {}).get("balance") or {}
        for q, entry in ledger.items() if isinstance(ledger, dict) else ():
            rel = entry.get("relative") if isinstance(entry, dict) else None
            if isinstance(rel, (int, float)) and not isinstance(rel, bool):
                self._residuals.setdefault(q, []).append(abs(float(rel)))
        for q, values in self._residuals.items():
            if values and values[-1] > RESIDUAL_FLOOR:
                tails[f"{q} residual"] = values[-DIVERGE_ROWS:]
        for name, tail in tails.items():
            key = f"gui:diverging:{name}"
            mags = [abs(v) for v in tail]
            growing = (
                len(mags) == DIVERGE_ROWS
                and all(math.isfinite(m) for m in mags)
                and mags[0] > 0
                and all(b > a for a, b in itertools.pairwise(mags))
                and mags[-1] >= DIVERGE_GROWTH * mags[0]
            )
            if growing:
                self._raise(
                    Alert(
                        key,
                        "diverging",
                        WARNING,
                        f"{name} diverging",
                        f"{name} grew {mags[-1] / mags[0]:.0f} times over the last {DIVERGE_ROWS} progress rows, "
                        f"to {_fmt(tail[-1])}",
                        "gui",
                        **self._where(st),
                    )
                )
            else:
                self._clear(key)

    def _wall(self, st: RunState) -> None:
        limit = wall_limit(st.start) or self.job_wall_limit
        wall = st.last.get("wall")
        if limit is None or not isinstance(wall, (int, float)):
            return
        eta = st.last.get("eta")
        source = "t_wall_stop" if wall_limit(st.start) else "the job's time limit"
        if wall >= WALL_FRACTION * limit:
            self._raise(
                Alert(
                    "gui:wall",
                    "wall",
                    WARNING,
                    "Wall-time limit approaching",
                    f"{_duration(wall)} of {_duration(limit)} ({source}) used, "
                    f"{100 * st.progress:.0f} % of the run done",
                    "gui",
                    **self._where(st),
                )
            )
        elif (
            isinstance(eta, (int, float))
            and eta > 0
            and st.progress > 0.02
            and self._reach(st, wall, eta, limit) < 0.99
        ):
            reach = self._reach(st, wall, eta, limit)
            self._raise(
                Alert(
                    "gui:wall-eta",
                    "wall",
                    INFO,
                    "Will reach the wall-time limit",
                    f"ETA {_duration(eta)} is past the limit of {_duration(limit)} ({source}): "
                    f"about {100 * reach:.0f} % of the run will be done",
                    "gui",
                    **self._where(st),
                )
            )

    @staticmethod
    def _reach(st: RunState, wall: float, eta: float, limit: float) -> float:
        """The fraction of the run done at the limit, at the rate the ETA assumes."""
        return min(1.0, st.progress + (1 - st.progress) * max(limit - wall, 0.0) / eta)

    def _stalled(self, st: RunState, now: float) -> None:
        if len(self._progress_at) < 2 or not st.last:
            return
        gaps = [b - a for a, b in itertools.pairwise(self._progress_at)][-RECENT:]
        limit = max(STALL_FACTOR * statistics.median(gaps), STALL_MIN_SECONDS)
        quiet = now - self._progress_at[-1]
        if quiet > limit:
            self._raise(
                Alert(
                    "gui:stalled",
                    "stalled",
                    WARNING,
                    "Run log stalled",
                    f"no progress record for {_duration(quiet)} (they came every {_duration(statistics.median(gaps))}) "
                    "while the job runs",
                    "gui",
                    **self._where(st),
                )
            )
