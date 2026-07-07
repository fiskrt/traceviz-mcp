"""Scale-adaptive timeline rendering.

The core challenge is dynamic range: a trace may span a few microseconds with a
handful of ops per engine, or a few milliseconds with millions of ns-granularity
ops. A fixed bar-per-event Gantt chart works for the former and is unrenderable
for the latter. So each row is drawn in one of two modes, chosen automatically
from the pixel budget:

* **bars** — when a typical event is at least ``MIN_BAR_PX`` pixels wide, draw
  each event as a discrete colored rectangle (op identity is visible).
* **occupancy** — otherwise, bin the row to one bucket per output pixel column
  and draw a busy-fraction heat strip (structure is visible at any zoom).

Axis units (ns / us / ms / s) are chosen from the span being drawn, so the same
code reads naturally whether the window is 3 us or 3 ms.
"""

from __future__ import annotations

from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")  # headless / server-side PNG output
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

from .model import TraceData

# Below this width a bar is not worth drawing individually -> use occupancy mode.
MIN_BAR_PX = 2.0

# Fixed, high-contrast color per engine class. Distinct hues so overlapping
# engines are told apart at a glance; readable on the light background used here.
ENGINE_COLORS = {
    "SCALAR": "#4C78A8",    # blue
    "VECTOR": "#F58518",    # orange
    "CUBE": "#E45756",      # red
    "MTE1": "#72B7B2",      # teal
    "MTE2": "#54A24B",      # green
    "MTE3": "#B279A2",      # purple
    "FIXP": "#EECA3B",      # yellow
    "FLOWCTRL": "#9D755D",  # brown
    "CACHEMISS": "#BAB0AC", # grey
    "ALL": "#333333",       # near-black
}
_FALLBACK_COLOR = "#7F7F7F"


def engine_color(engine: str) -> str:
    return ENGINE_COLORS.get(engine, _FALLBACK_COLOR)


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def interval_union_ns(
    ts: np.ndarray, dur: np.ndarray, t0: float, t1: float
) -> float:
    """Exact busy time = length of the union of event intervals within [t0,t1].

    Summing durations double-counts events that overlap in time (multiple
    sub-threads collapsed onto one engine lane), which can push naive "busy"
    past 100%. Merging intervals gives the true occupied time.
    """
    if ts.size == 0:
        return 0.0
    s = np.clip(ts, t0, t1)
    e = np.clip(ts + dur, t0, t1)
    order = np.argsort(s, kind="stable")
    s, e = s[order], e[order]
    total = 0.0
    cur_s, cur_e = s[0], e[0]
    for i in range(1, s.size):
        if s[i] > cur_e:
            total += cur_e - cur_s
            cur_s, cur_e = s[i], e[i]
        elif e[i] > cur_e:
            cur_e = e[i]
    total += cur_e - cur_s
    return float(total)


def pick_time_unit(span_ns: float) -> tuple[float, str]:
    """Return (divisor, label) mapping nanoseconds to a readable axis unit."""
    if span_ns >= 1e9:
        return 1e9, "s"
    if span_ns >= 1e6:
        return 1e6, "ms"
    if span_ns >= 1e3:
        return 1e3, "us"
    return 1.0, "ns"


def _row_occupancy(
    ts: np.ndarray, dur: np.ndarray, t0: float, t1: float, nbins: int
) -> np.ndarray:
    """Busy fraction in [0,1] for each of ``nbins`` equal time buckets.

    Vectorised interval accumulation: each event contributes its overlap with
    the window, distributing time across the buckets it spans.
    """
    occ = np.zeros(nbins, dtype=np.float64)
    if ts.size == 0 or t1 <= t0:
        return occ
    bin_w = (t1 - t0) / nbins
    starts = np.clip(ts, t0, t1)
    ends = np.clip(ts + dur, t0, t1)
    b0 = np.minimum(((starts - t0) / bin_w).astype(np.int64), nbins - 1)
    b1 = np.minimum(((ends - t0) / bin_w).astype(np.int64), nbins - 1)

    same = b0 == b1
    # Events wholly inside one bucket: add their duration directly.
    np.add.at(occ, b0[same], (ends - starts)[same])
    # Events spanning multiple buckets: split into head, full-middle, tail.
    for i in np.nonzero(~same)[0]:
        lo, hi = int(b0[i]), int(b1[i])
        occ[lo] += (t0 + (lo + 1) * bin_w) - starts[i]
        occ[hi] += ends[i] - (t0 + hi * bin_w)
        if hi - lo > 1:
            occ[lo + 1:hi] += bin_w
    return np.clip(occ / bin_w, 0.0, 1.0)


@dataclass
class RowStat:
    unit: str
    engine: str
    busy_ns: float
    busy_pct: float
    n_events: int


def _selected_rows(
    td: TraceData, units, engines, aggregate: str
) -> list[tuple[str, str | None]]:
    rows = td.rows()
    if units:
        rows = [(u, e) for (u, e) in rows if u in units]
    if engines:
        rows = [(u, e) for (u, e) in rows if e in engines]
    if aggregate == "unit":
        seen = []
        for u, _ in rows:
            if u not in seen:
                seen.append(u)
        return [(u, None) for u in seen]
    return rows


def render_timeline(
    td: TraceData,
    out_path: str,
    units: list[str] | None = None,
    engines: list[str] | None = None,
    window: tuple[float, float] | None = None,
    width_px: int = 1600,
    row_px: int = 26,
    aggregate: str = "row",  # "row" = per (unit,engine); "unit" = per unit
    title: str | None = None,
) -> dict:
    """Render a scale-adaptive timeline PNG. Returns a summary dict.

    ``aggregate="unit"`` collapses all engines of a unit onto one lane (colored
    by engine), which is the compact view for many units at once. ``"row"``
    gives every (unit, engine) its own lane.
    """
    rows = _selected_rows(td, units, engines, aggregate)
    if not rows:
        raise ValueError("No (unit, engine) rows match the given filters.")

    t0 = window[0] if window else td.t_min
    t1 = window[1] if window else td.t_max
    if t1 <= t0:
        t1 = t0 + 1.0
    span = t1 - t0
    div, unit_label = pick_time_unit(span)
    px_per_ns = width_px / span

    fig_w = width_px / 100.0
    fig_h = max(1.5, (len(rows) * row_px + 90) / 100.0)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=100)

    row_stats: list[RowStat] = []
    used_engines: set[str] = set()

    for r, (unit, engine) in enumerate(rows):
        y = len(rows) - 1 - r  # first row at top
        sel_engines = [engine] if engine else None
        m = td.mask(units=[unit], engines=sel_engines, window=(t0, t1))
        ts_r, dur_r = td.ts[m], td.dur[m]
        eng_r = td.engine_i[m]

        busy = interval_union_ns(ts_r, dur_r, t0, t1)
        row_stats.append(
            RowStat(unit, engine or "*", busy, 100.0 * busy / span, int(ts_r.size))
        )

        typical_px = (np.median(dur_r) * px_per_ns) if ts_r.size else 0.0
        draw_bars = ts_r.size > 0 and typical_px >= MIN_BAR_PX and ts_r.size <= 4 * width_px

        if draw_bars:
            # group bars by engine so each gets its color
            for ei in np.unique(eng_r):
                sub = eng_r == ei
                ename = td.engines[int(ei)]
                used_engines.add(ename)
                segs = [((s - t0) / div, d / div) for s, d in zip(ts_r[sub], dur_r[sub])]
                ax.broken_barh(
                    segs, (y + 0.1, 0.8), facecolors=engine_color(ename),
                    edgecolors="none",
                )
        elif ts_r.size > 0:
            # Occupancy heat strip, tinted by the lane's engine color so idle
            # reads as white and busy as a saturated engine color. When a lane
            # mixes engines (unit aggregate), fall back to a neutral dark tint.
            nbins = width_px
            occ = _row_occupancy(ts_r, dur_r, t0, t1, nbins)
            engines_here = np.unique(eng_r)
            if engine is None and engines_here.size > 1:
                r_, g_, b_ = _rgb("#333333")
            else:
                r_, g_, b_ = _rgb(engine_color(td.engines[int(engines_here[0])]))
                used_engines.add(td.engines[int(engines_here[0])])
            rgba = np.zeros((1, nbins, 4), dtype=np.float64)
            rgba[0, :, 0], rgba[0, :, 1], rgba[0, :, 2] = r_, g_, b_
            rgba[0, :, 3] = np.sqrt(occ)  # gamma-boost so faint activity stays visible
            ax.imshow(
                rgba, aspect="auto",
                extent=(0, span / div, y + 0.1, y + 0.9),
                interpolation="nearest", zorder=2,
            )

    ax.set_xlim(0, span / div)
    ax.set_ylim(0, len(rows))
    ax.set_yticks([len(rows) - 1 - r + 0.5 for r in range(len(rows))])
    ax.set_yticklabels(
        [f"{u} / {e}" if e else u for (u, e) in rows], fontsize=8, fontfamily="monospace"
    )
    ax.set_xlabel(f"time ({unit_label})  —  window {t0/div:.3f}…{t1/div:.3f} {unit_label}", fontsize=9)
    ax.tick_params(axis="x", labelsize=8)
    ax.grid(axis="x", color="#00000018", linewidth=0.6)
    ax.set_axisbelow(True)

    ttl = title or f"{len(rows)} lanes · span {span/div:.3f} {unit_label} · {td.n} events total"
    ax.set_title(ttl, fontsize=10, loc="left")

    # Legend: engine colors (bars) plus an occupancy note if any strip was drawn.
    legend_items = [
        Patch(facecolor=engine_color(e), label=e)
        for e in sorted(used_engines, key=lambda x: list(ENGINE_COLORS).index(x) if x in ENGINE_COLORS else 99)
    ]
    if legend_items:
        # Placed outside the axes (to the right) so it never covers a lane.
        ax.legend(
            handles=legend_items, loc="upper left", bbox_to_anchor=(1.005, 1.0),
            fontsize=7, framealpha=0.9, borderaxespad=0.0, title="engine",
            title_fontsize=7,
        )

    fig.tight_layout()
    fig.savefig(out_path, dpi=100, bbox_inches="tight")
    plt.close(fig)

    return {
        "out_path": out_path,
        "window_ns": [t0, t1],
        "span_ns": span,
        "time_unit": unit_label,
        "rows": [rs.__dict__ for rs in row_stats],
    }


def pairwise_overlap(
    td: TraceData,
    a: tuple[str, str],
    b: tuple[str, str],
    window: tuple[float, float] | None = None,
    nbins: int = 4000,
) -> dict:
    """Quantify how much two lanes are busy at the same time.

    Uses fine occupancy binning as a fast approximation. Returns overlap time and
    overlap as a fraction of each lane's own busy time (and of the window).
    """
    t0 = window[0] if window else td.t_min
    t1 = window[1] if window else td.t_max
    span = max(t1 - t0, 1e-12)

    def occ(pair):
        m = td.mask(units=[pair[0]], engines=[pair[1]], window=(t0, t1))
        return _row_occupancy(td.ts[m], td.dur[m], t0, t1, nbins)

    oa, ob = occ(a), occ(b)
    bin_w = span / nbins
    busy_a = float(oa.sum() * bin_w)
    busy_b = float(ob.sum() * bin_w)
    overlap = float(np.minimum(oa, ob).sum() * bin_w)
    return {
        "lane_a": f"{a[0]}/{a[1]}",
        "lane_b": f"{b[0]}/{b[1]}",
        "window_ns": [t0, t1],
        "busy_a_ns": busy_a,
        "busy_b_ns": busy_b,
        "overlap_ns": overlap,
        "overlap_pct_of_a": 100.0 * overlap / busy_a if busy_a else 0.0,
        "overlap_pct_of_b": 100.0 * overlap / busy_b if busy_b else 0.0,
        "overlap_pct_of_window": 100.0 * overlap / span,
    }
