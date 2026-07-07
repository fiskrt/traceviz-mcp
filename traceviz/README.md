# traceviz

Render NPU profiler traces (OPPROF / Ascend-style) into clear, scale-adaptive
timeline PNGs and overlap statistics — built so an LLM can ask *"core2.veccore1
vs core1 — how's the overlap?"* and get back a legible image plus hard numbers.

Pure Python, only `numpy` + `matplotlib` (no pandas), so it runs headless and
wraps cleanly as an MCP tool later.

## What it reads

Point it at an `OPPROF_*` directory (or its `simulator/` dir, or a `trace.json`).

- **`simulator/trace.json`** (primary) — the aggregated Chrome-trace file where
  every unit shares one time axis. `pid` = unit (`core2.veccore1`), `tid` =
  engine class (SCALAR, VECTOR, CUBE, MTE1/2/3, FIXP, FLOWCTRL, …).
- **`simulator/visualize_data.bin`** (bonus) — its trailer holds a
  per-instruction static table (Cycles, Pipe, **UB read/write conflicts**,
  **vector utilization %**, source), joined to events by program counter.

The 24 units are 8 cores × {`cubecore0`, `veccore0`, `veccore1`}.

## Scale adaptivity

The renderer copes with traces from a few microseconds to a few milliseconds:

- The x-axis auto-selects **ns / µs / ms / s** from the window span.
- Each lane is drawn in one of two modes, chosen from the pixel budget:
  - **bars** — one colored rectangle per event, when events are wide enough to see;
  - **occupancy** — one bucket per output pixel, tinted by the engine color with
    opacity ∝ how busy that slice is, when events are too dense to draw individually.

So a 3 ms / 2M-event trace renders in a few seconds, and zooming to a 5 µs
window on the same data automatically switches lanes back to discrete bars.

## Install

```bash
# from the repo root (the dir containing the `traceviz/` package)
python3 -m pip install numpy matplotlib   # if not already present
```

## CLI examples (on the bundled OPPROF sample)

All commands below are run from the repo root. `SAMPLE` is the sample trace dir.

```bash
SAMPLE=OPPROF_20260707154004_XZPYWIDKXUKKJVTX
```

**1. List what's in the trace** (units, engines, span, lane names):

```bash
python3 -m traceviz.cli $SAMPLE --list
# -> 24 units, engines [SCALAR, VECTOR, ...], span_ns≈18.28, 68112 events
```

**2. Overview — one lane per unit, all 24 at once:**

```bash
python3 -m traceviz.cli $SAMPLE --aggregate unit --out out/overview.png
```

**3. The overlap question — compare two units across their key engines:**

```bash
python3 -m traceviz.cli $SAMPLE \
    --units core2.veccore1 core1.veccore0 \
    --engines SCALAR VECTOR MTE2 MTE3 \
    --out out/query.png
# idle = white, busy = engine-colored; you can see VECTOR & MTE3 loads overlap.
```

**4. Zoom into a time window** (nanoseconds); axis units adapt automatically:

```bash
python3 -m traceviz.cli $SAMPLE --units core0.cubecore0 --window 26 30 --out out/win.png
```

**5. Overlap statistics between two lanes** (no image, JSON to stdout):

```bash
python3 -m traceviz.cli $SAMPLE \
    --overlap core2.veccore1/VECTOR core1.veccore0/VECTOR
# -> overlap_ns≈6.467, overlap_pct_of_a≈99.7%
```

## Python API

```python
from traceviz.loader import load_trace
from traceviz.render import render_timeline, pairwise_overlap

td = load_trace("OPPROF_20260707154004_XZPYWIDKXUKKJVTX")

# render a filtered timeline; returns a summary dict (busy %, span, time unit)
summary = render_timeline(
    td, "out/query.png",
    units=["core2.veccore1", "core1.veccore0"],
    engines=["VECTOR", "MTE3"],
    window=None,          # (t0_ns, t1_ns) to zoom
    aggregate="row",      # "row" = per (unit,engine); "unit" = one lane per unit
)

# quantify simultaneous-busy time between two lanes
stats = pairwise_overlap(td, ("core2.veccore1", "VECTOR"), ("core1.veccore0", "VECTOR"))
```

## CLI reference

| Flag | Meaning |
|------|---------|
| `path` | OPPROF dir, `simulator/` dir, or a `trace.json` (positional) |
| `--out PATH` | output PNG (default `timeline.png`) |
| `--units U ...` | filter to these units |
| `--engines E ...` | filter to these engine classes |
| `--window T0 T1` | time window in nanoseconds |
| `--aggregate {row,unit}` | lane per (unit,engine) vs. lane per unit |
| `--width PX` | output width in pixels (default 1600) |
| `--overlap A/E B/E` | print overlap stats for two `UNIT/ENGINE` lanes and exit |
| `--list` | print units/engines/lanes and exit |

## Layout

- `model.py` — `TraceData` columnar store + `InstrTable`.
- `loader.py` — parse aggregated `trace.json` and the `.bin` instruction table.
- `render.py` — scale-adaptive `render_timeline` + `pairwise_overlap`.
- `cli.py` — command-line entry point (`python -m traceviz.cli`).
