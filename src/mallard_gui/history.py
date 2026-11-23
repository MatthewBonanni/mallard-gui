"""A monitored quantity's history in bounded memory, however long the run (design: Monitor): min/max/last per
bin at a few resolutions, each level about `cap` bins. The finest level holds the latest samples themselves;
each coarser one bins `factor` times as many and reaches further back; the coarsest always reaches the first
sample, and when it fills, a coarser one is made from it. A plot reads the finest level that covers its
window in at most its pixel budget, so drawing costs the budget, not the run's length. Every bin keeps the
byte offset of its first record, so zooming past what is held reads only that byte range again."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

CAP = 10_000
FACTOR = 8


@dataclass
class Bin:
    t0: float
    t1: float
    lo: float
    t_lo: float
    hi: float
    t_hi: float
    last: float
    n: int  # samples
    offset: int  # byte offset of the first sample's record, -1 if unknown
    total: float = 0.0  # the samples' sum: the bin's mean, for running means

    @classmethod
    def of(cls, t: float, v: float, offset: int) -> Bin:
        return cls(t, t, v, t, v, t, v, 1, offset, v)

    def add(self, other: Bin) -> None:
        """`other` follows this bin in time."""
        if other.lo < self.lo:
            self.lo, self.t_lo = other.lo, other.t_lo
        if other.hi > self.hi:
            self.hi, self.t_hi = other.hi, other.t_hi
        self.t1, self.last, self.n = other.t1, other.last, self.n + other.n
        self.total += other.total

    def copy(self) -> Bin:
        return Bin(self.t0, self.t1, self.lo, self.t_lo, self.hi, self.t_hi, self.last, self.n, self.offset, self.total)


class _Level:
    def __init__(self, width: int) -> None:
        self.width = width  # samples per bin
        self.bins: list[Bin] = []
        self.open: Bin | None = None

    def add(self, b: Bin) -> None:
        if self.open is None:
            self.open = b.copy()
        else:
            self.open.add(b)
        if self.open.n >= self.width:
            self.bins.append(self.open)
            self.open = None

    def all(self) -> list[Bin]:
        return self.bins + ([self.open] if self.open is not None else [])


class History:
    def __init__(self, cap: int = CAP, factor: int = FACTOR) -> None:
        self.cap = cap
        self.factor = factor
        self.levels = [_Level(1)]
        self.samples = 0

    def append(self, t: float, v: float, offset: int = -1) -> None:
        b = Bin.of(t, v, offset)
        for level in self.levels:
            level.add(b)
        self.samples += 1
        top = self.levels[-1]
        if len(top.bins) > self.cap:
            self._coarsen()
        for level in self.levels[:-1]:
            if len(level.bins) > self.cap + self.cap // 4:
                del level.bins[: len(level.bins) - self.cap]

    def extend(self, items: Iterable[tuple[float, float]]) -> None:
        for t, v in items:
            self.append(t, v)

    def _coarsen(self) -> None:
        """A coarser level over the whole history, from the current coarsest, which then starts trimming."""
        top = self.levels[-1]
        new = _Level(top.width * self.factor)
        for b in top.all():
            new.add(b)
        self.levels.append(new)

    # Reading

    @property
    def t(self) -> list[float]:
        """The latest samples' times (the finest level)."""
        return [b.t0 for b in self.levels[0].bins]

    @property
    def values(self) -> list[float]:
        return [b.last for b in self.levels[0].bins]

    def recent(self) -> tuple[list[float], list[float]]:
        return self.t, self.values

    @property
    def span(self) -> tuple[float, float] | None:
        first, last = self.levels[-1].all(), self.levels[0].all()
        return (first[0].t0, last[-1].t1) if first and last else None

    def level_for(self, t0: float, t1: float, budget: int) -> int:
        """The finest level reaching back to `t0` with at most `budget` bins over [t0, t1], else the coarsest."""
        for k, level in enumerate(self.levels):
            bins = level.all()
            if not bins or (k < len(self.levels) - 1 and bins[0].t0 > t0):
                continue
            if sum(1 for b in bins if b.t1 >= t0 and b.t0 <= t1) <= budget:
                return k
        return len(self.levels) - 1

    def curve(
        self, t0: float | None = None, t1: float | None = None, max_points: int = 2000
    ) -> tuple[list[float], list[float]]:
        """Points for a plot over [t0, t1]: each bin's minimum and maximum at their times (samples as they
        are on the finest level), merged further where even the coarsest level has too many bins."""
        span = self.span
        if span is None:
            return [], []
        lo_t = span[0] if t0 is None else t0
        hi_t = span[1] if t1 is None else t1
        budget = max(1, max_points // 2)
        k = self.level_for(lo_t, hi_t, budget)
        bins = [b for b in self.levels[k].all() if b.t1 >= lo_t and b.t0 <= hi_t]
        if len(bins) > budget:
            group = -(-len(bins) // budget)
            merged = []
            for i in range(0, len(bins), group):
                m = bins[i].copy()
                for b in bins[i + 1 : i + group]:
                    m.add(b)
                merged.append(m)
            bins = merged
        xs: list[float] = []
        ys: list[float] = []
        for b in bins:
            if b.n == 1:
                xs.append(b.t0)
                ys.append(b.last)
                continue
            for t, v in sorted({(b.t_lo, b.lo), (b.t_hi, b.hi)}):
                xs.append(t)
                ys.append(v)
        return xs, ys

    def means(
        self, t0: float | None = None, t1: float | None = None, max_points: int = 2000
    ) -> tuple[list[float], list[float], list[int]]:
        """Each bin's mean at its middle time and its sample count, over [t0, t1] at the level `curve` reads:
        what a running mean averages, where min/max points would bias it."""
        span = self.span
        if span is None:
            return [], [], []
        lo_t = span[0] if t0 is None else t0
        hi_t = span[1] if t1 is None else t1
        k = self.level_for(lo_t, hi_t, max(1, max_points))
        bins = [b for b in self.levels[k].all() if b.t1 >= lo_t and b.t0 <= hi_t]
        return [(b.t0 + b.t1) / 2 for b in bins], [b.total / b.n for b in bins], [b.n for b in bins]

    def offsets(self, t0: float, t1: float) -> tuple[int, int] | None:
        """The byte range of the records over [t0, t1], from the finest level reaching back to `t0`, to read
        again where the levels held are too coarse; the end is -1 when the range runs to the latest record."""
        for k, level in enumerate(self.levels):
            bins = level.all()
            if not bins or (k < len(self.levels) - 1 and bins[0].t0 > t0):
                continue
            inside = [i for i, b in enumerate(bins) if b.t1 >= t0 and b.t0 <= t1]
            if not inside or bins[inside[0]].offset < 0:
                return None
            after = inside[-1] + 1
            return bins[inside[0]].offset, bins[after].offset if after < len(bins) else -1
        return None
