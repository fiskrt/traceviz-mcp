"""In-memory representation of a parsed profiler trace.

All timestamps and durations are stored in nanoseconds (the profiler's native
``displayTimeUnit``). Rendering code is responsible for choosing a human-facing
unit (ns / us / ms) based on the span it is about to draw.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class TraceData:
    """A flat, columnar view of all timeline events across every core/engine.

    Each of the parallel arrays below has one entry per event (``n`` events
    total). Categorical columns are stored as integer indices into the
    corresponding lookup list to keep memory bounded on large traces.
    """

    units: list[str]              # e.g. ["core0.cubecore0", "core0.veccore0", ...]
    engines: list[str]            # e.g. ["SCALAR", "VECTOR", "MTE2", ...]
    ops: list[str]                # distinct instruction / op names

    unit_i: np.ndarray            # int32[n] -> index into `units`
    engine_i: np.ndarray          # int32[n] -> index into `engines`
    op_i: np.ndarray              # int32[n] -> index into `ops`
    ts: np.ndarray                # float64[n] event start, nanoseconds
    dur: np.ndarray               # float64[n] event duration, nanoseconds
    pc: np.ndarray                # int64[n] program counter (0 if absent)

    # Optional per-instruction static table from visualize_data.bin (may be None).
    instr: "InstrTable | None" = None

    @property
    def n(self) -> int:
        return int(self.ts.shape[0])

    @property
    def t_min(self) -> float:
        return float(self.ts.min()) if self.n else 0.0

    @property
    def t_max(self) -> float:
        return float((self.ts + self.dur).max()) if self.n else 0.0

    def span(self) -> float:
        return self.t_max - self.t_min

    def rows(self) -> list[tuple[str, str]]:
        """Distinct (unit, engine) pairs that actually carry events, sorted."""
        seen = set()
        for u, e in zip(self.unit_i, self.engine_i):
            seen.add((int(u), int(e)))
        pairs = sorted(seen, key=lambda p: (self.units[p[0]], self.engines[p[1]]))
        return [(self.units[u], self.engines[e]) for u, e in pairs]

    def mask(
        self,
        units: list[str] | None = None,
        engines: list[str] | None = None,
        window: tuple[float, float] | None = None,
    ) -> np.ndarray:
        """Boolean mask selecting events by unit, engine and/or time window.

        `window` is an inclusive/overlapping range in nanoseconds: any event that
        overlaps [t0, t1] is kept, so partially-visible events at the edges are
        not dropped.
        """
        m = np.ones(self.n, dtype=bool)
        if units:
            want = {self.units.index(u) for u in units if u in self.units}
            m &= np.isin(self.unit_i, list(want))
        if engines:
            want = {self.engines.index(e) for e in engines if e in self.engines}
            m &= np.isin(self.engine_i, list(want))
        if window is not None:
            t0, t1 = window
            m &= (self.ts <= t1) & (self.ts + self.dur >= t0)
        return m


@dataclass
class InstrTable:
    """Per-instruction static analysis, indexed by (address, core).

    Sourced from the trailer chunk of ``visualize_data.bin``. Array-valued fields
    (Cycles, UB Read Conflict, Vector Utilization Percentage, ...) hold one entry
    per core, aligned with `cores`.
    """

    cores: list[str]
    # address(int) -> {field_name -> value}. Array fields stay as python lists.
    by_addr: dict[int, dict] = field(default_factory=dict)

    def core_index(self, core: str) -> int | None:
        return self.cores.index(core) if core in self.cores else None
