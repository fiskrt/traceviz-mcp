"""Fragmentation analysis.

A lane can look like a solid, healthy block in the timeline yet actually be
hundreds or thousands of tiny back-to-back operations (e.g. many small MTE2
reads). Visually the occupancy strip cannot tell that apart from one coalesced
transfer, but it is a prime optimization target: fusing those ops removes
per-instruction overhead.

The signal is numeric. We merge each lane's events into contiguous busy *runs*
(segments) and compare the raw event count to the segment count:

* ``ops_per_segment`` — how many events make up each solid-looking block. High =
  fragmented (the "1000 reads that look like one region" case).
* ``coalescable_ops`` — ``n_events - n_segments``: how many events could in
  principle be removed if each contiguous run were a single op.

Grouping by op mnemonic (``top_ops``) points at *what* to fuse.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from .model import AGGREGATE_ENGINES, TraceData
from .render import busy_and_segments


def lane_fragmentation(
    td: TraceData,
    unit: str,
    engine: str,
    window: tuple[float, float] | None = None,
    merge_gap_ns: float = 0.0,
    top_ops: int = 5,
) -> dict:
    """Fragmentation stats for a single (core, metric) lane over a window.

    ``merge_gap_ns`` lets events separated by up to that many ns still count as
    one run, so "looks contiguous" regions are scored as such even with sub-ns
    scheduling gaps.
    """
    t0 = window[0] if window else td.t_min
    t1 = window[1] if window else td.t_max
    m = td.mask(units=[unit], engines=[engine], window=(t0, t1))
    ts, dur = td.ts[m], td.dur[m]
    lane = f"{unit}/{engine}"

    if ts.size == 0:
        return {
            "lane": lane, "window_ns": [t0, t1], "n_events": 0, "n_segments": 0,
            "ops_per_segment": 0.0, "coalescable_ops": 0, "busy_ns": 0.0,
            "mean_op_ns": 0.0, "median_op_ns": 0.0, "min_op_ns": 0.0,
            "max_op_ns": 0.0, "distinct_ops": 0, "top_ops": [],
        }

    busy, n_seg = busy_and_segments(ts, dur, t0, t1, merge_gap_ns)
    clipped = np.clip(ts + dur, t0, t1) - np.clip(ts, t0, t1)
    counts = Counter(td.ops[int(i)] for i in td.op_i[m])

    return {
        "lane": lane,
        "window_ns": [t0, t1],
        "n_events": int(ts.size),
        "n_segments": n_seg,
        "ops_per_segment": ts.size / n_seg if n_seg else 0.0,
        "coalescable_ops": int(ts.size) - n_seg,
        "busy_ns": busy,
        "mean_op_ns": float(clipped.mean()),
        "median_op_ns": float(np.median(clipped)),
        "min_op_ns": float(clipped.min()),
        "max_op_ns": float(clipped.max()),
        "distinct_ops": len(counts),
        "top_ops": [{"op": nm, "count": c} for nm, c in counts.most_common(top_ops)],
    }


def fragmentation_report(
    td: TraceData,
    units: list[str] | None = None,
    metrics: list[str] | None = None,
    window: tuple[float, float] | None = None,
    merge_gap_ns: float = 0.0,
    top_ops: int = 5,
    limit: int | None = None,
) -> list[dict]:
    """Fragmentation for every matching lane, most fragmented first.

    Ranked by ``ops_per_segment`` so the biggest coalescing opportunities float
    to the top. ``limit`` keeps only the worst N lanes.
    """
    rows = td.rows()
    if units:
        rows = [(u, e) for (u, e) in rows if u in units]
    if metrics:
        rows = [(u, e) for (u, e) in rows if e in metrics]
    else:
        # Default "all metrics": skip aggregate lanes (e.g. ALL); they are not a
        # real coalescing target, just the sum of the physical pipes.
        rows = [(u, e) for (u, e) in rows if e not in AGGREGATE_ENGINES]
    out = [
        lane_fragmentation(td, u, e, window, merge_gap_ns, top_ops) for (u, e) in rows
    ]
    out.sort(key=lambda r: r["ops_per_segment"], reverse=True)
    return out[:limit] if limit else out
