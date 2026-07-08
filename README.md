# traceviz

The goal is to have the auto-research loop access profiling timelines easier.

The LLM follows these steps:
1. Make change in code that it suspect improves the pipelining
2. Run profiler which creates a trace
3. Uses `traceviz` mcp to visualize the timeline relevant to the change made
4. Analyses timeline output from mcp and determines uses this information and return to step 1.

---

The MCP allows for more fine-grained access, for example:
LLM saw an inbalance in cores, so it want to see how core 7 vector core 2 was improved in MTE2 access.
The mcp will then return an timeline image over just these MTE2 acess on core 7 vector core 2.

----


---

## Install mcp into Claude Code

```

`uvx` builds and runs the package straight from git:

```bash
claude mcp add traceviz -- uvx --from git+https://github.com/<you>/<repo> traceviz-mcp
# if you are missing uv package manager, install it:
# curl -LsSf https://astral.sh/uv/install.sh | sh
# if you already cloned the repo you can install from a path:
# claude mcp add traceviz -- uvx --from /abs/path/to/repo traceviz-mcp
```

Traceviz parses and renders the timelines from `trace.json` and the `visualize_data.bin` without any 3p deps.


**Scope** (where the server registration lives) is controlled with `-s`:

| Flag | Effect |
|------|--------|
| `-s local` (default) | Only you, only this project. |
| `-s project` | Committed to `.mcp.json` and shared with the repo. |
| `-s user` | Available to you across all projects. |

**Verify and manage:**

```bash
claude mcp list            # is it registered?
claude mcp get traceviz    # show its config
claude mcp remove traceviz # uninstall
```

Inside a session, `/mcp` lists connected servers and their tools.


### Tools the model sees

| Tool | Purpose |
|------|---------|
| `describe_trace` | Return the valid **cores**, **metrics**, and time span. The model calls this first to learn what it can request. |
| `render_timeline_image` | Render a timeline of chosen cores × metrics over an optional time window; returns a text busy-summary and a PNG. |
| `overlap` | Report how much two `core/metric` lanes are busy at the same time. |
| `fragmentation` | Rank lanes by ops-per-run — find solid-looking blocks that are actually many tiny ops worth coalescing. |

See [Tool reference](#tool-reference) below for exact arguments and return values.

---

## What the trace looks like

A run lives in an `OPPROF_*/` directory. Point traceviz at that directory, its
`simulator/` subdirectory, or a `trace.json` file directly.

There are **24 execution units** = 8 physical cores × 3 engines each:
`coreN.cubecore0` (matrix/cube), `coreN.veccore0`, `coreN.veccore1` (vector).
Within a unit, work is split across **engine/pipe classes** — `SCALAR`, `VECTOR`,
`CUBE`, `MTE1`/`MTE2`/`MTE3` (memory transfer engines), `FIXP`, `FLOWCTRL`,
`CACHEMISS`. In this tool a *core* is a unit name and a *metric* is an engine class.

### `simulator/trace.json` — the timeline (primary input)

[Chrome Trace Event Format](https://docs.google.com/document/d/1CvAClvFfyA5R-PhYUmn5OOQtYMH4h6I0nSsKchNAySU).
A single JSON object:

```jsonc
{
  "displayTimeUnit": "ns",
  "profilingType": "op",
  "schemaVersion": 1,
  "traceEvents": [ /* one object per executed instruction */ ]
}
```

Every event is a complete ("X") event. The fields traceviz uses:

| Field | Meaning |
|-------|---------|
| `ph`  | Always `"X"` (a duration event). Non-`X` entries are ignored. |
| `pid` | Unit name, e.g. `"core2.veccore1"` → the **core**. |
| `tid` | Engine class, e.g. `"VECTOR"` → the **metric**. |
| `ts`  | Start time in nanoseconds (float). |
| `dur` | Duration in nanoseconds (float). |
| `name`| Instruction/op mnemonic, e.g. `"MOVK"`. |
| `args.pc_addr` | Program counter, hex string — the join key to the instruction table below. |

This is the only file where all 24 units share one time axis, which is why it's
the primary source. (The per-unit `coreN.xxx/trace.json` files hold the same
events keyed by numeric pid with an extra per-thread split; traceviz doesn't need
them.)

### `simulator/visualize_data.bin` — per-instruction static data (optional)

A length-prefixed container of two JSON chunks. Each chunk is:

```
┌───────────────────────┬───────────┬──────────────────────────┐
│ 8 bytes: uint64 LE     │ 4 bytes   │ <length> bytes: UTF-8 JSON│
│ payload length         │ flags     │                          │
└───────────────────────┴───────────┴──────────────────────────┘
```

- **Chunk 1** is a byte-for-byte copy of the aggregated `trace.json` (redundant —
  traceviz reads the plain file instead).
- **Chunk 2** is the payoff: `{"Cores": [...], "Instructions": [...], "Instructions Dtype": {...}}`.
  `Cores` is the 24-element unit ordering. Each entry in `Instructions` is one
  static instruction, and its array-valued fields are indexed by that `Cores`
  order (one value per core):

  | Field | Type | Meaning |
  |-------|------|---------|
  | `Address` | hex string | PC, joins to `args.pc_addr` in the timeline. |
  | `Source` | string | Disassembled instruction. |
  | `Pipe` | string | Engine class it runs on. |
  | `Cycles`, `GPR Count`, `Instructions Executed`, `Process Bytes` | int[24] | Per-core counters. |
  | `UB Read Conflict`, `UB Write Conflict` | int[24] | Unified-buffer bank conflicts per core (`-1` = N/A). |
  | `Vector Utilization Percentage` | float[24] | Per-core vector-lane utilization (`-1` = N/A). |

  This is the only place UB conflicts and vector utilization exist — they are not
  in any `trace.json`. traceviz loads it when present (matched to events by PC) and
  never fails the load if it's missing or malformed.

---

## What the image represents

The output PNG is a Gantt-style timeline read top-to-bottom, left-to-right:

- **Each row ("lane") is one core+metric** (`aggregate="row"`) or one whole core
  (`aggregate="unit"`, all its metrics blended). Lanes are labelled on the left.
- **The x-axis is time**, with the unit (ns / µs / ms / s) chosen automatically
  from the window being drawn.
- **White = idle, color = busy**, and the color identifies the metric (see the
  legend, placed outside the plot so it never covers data).
- **Two drawing modes**, picked per lane from the pixel budget so the same code
  works from a few µs to a few ms:
  - **bars** — each event is a discrete rectangle, used when events are wide
    enough to distinguish. You see individual instructions.
  - **occupancy** — when events are finer than a pixel, each output column is
    shaded with opacity proportional to the fraction of that time slice the lane
    was busy. You see density and structure instead of individual events.

Reading it: overlapping colored regions across lanes mean those engines/cores are
busy simultaneously. Long white stretches are stalls. That's the "how's the
overlap" story at a glance; `overlap` / `pairwise_overlap` give you the number.

**What the image can't show — fragmentation.** A solid-looking busy block may be
one large operation or a thousand tiny back-to-back ones; at the pixel level they
are identical. That distinction matters because many small ops (e.g. lots of
little MTE2 reads) carry per-instruction overhead that fusing them removes. So
fragmentation is reported numerically, not drawn: the render summary annotates
each lane with `events in N runs = ops/run`, and the `fragmentation` tool ranks
lanes by it. See below.

---

## Tool reference

MCP tool → underlying Python function. Times are always nanoseconds.

Every tool takes `path` (the OPPROF_* run directory) as a required first
argument.

### `describe_trace(path) -> str` (JSON)

Enumerate what a run contains. Call first, after producing the run, to get valid
names and the time range.

Returns: `cores` (unit names), `metrics` (engine classes), `time_start_ns`,
`time_end_ns`, `span_ns`, `n_events`, and `lanes` (every `core/metric` present).

### `render_timeline_image(path, ...) -> [summary, PNG]`

Render a timeline. Backed by `render.render_timeline`.

| Argument | Default | Meaning |
|----------|---------|---------|
| `path` | *required* | The OPPROF_* run directory to analyse. |
| `cores` | all | Units to include, e.g. `["core0.cubecore0","core1.veccore0"]`. |
| `metrics` | all | Engine classes to include, e.g. `["VECTOR","CUBE","MTE2"]`. |
| `start_ns`, `end_ns` | full span | Time window; give either or both to zoom. |
| `aggregate` | `"row"` | `"row"` = one lane per core+metric; `"unit"` = one lane per core. |

Returns a two-part MCP response: a text summary followed by the PNG. Each lane in
the summary reports busy %, event count, contiguous-run count, ops/run, and mean
op duration (fragmented lanes are flagged). The Python function `render_timeline`
returns the summary as a dict (`out_path`, `window_ns`, `span_ns`, `time_unit`,
and a `rows` list of `{unit, engine, busy_ns, busy_pct, n_events, n_segments,
mean_op_ns}`).

> **Busy %** is the *union* of event intervals in the lane, not the sum, so lanes
> where sub-threads overlap never exceed 100%.

### `overlap(path, lane_a, lane_b, start_ns=None, end_ns=None) -> str` (JSON)

How much two lanes are busy at the same time. Each lane is `"core/metric"`
(e.g. `"core2.veccore1/VECTOR"`). Backed by `render.pairwise_overlap`.

Returns: `busy_a_ns`, `busy_b_ns`, `overlap_ns`, and the overlap as a percentage
of lane A's busy time, lane B's busy time, and the whole window.

### `fragmentation(path, cores=None, metrics=None, start_ns=None, end_ns=None, merge_gap_ns=0.0, limit=None) -> str` (JSON)

Find coalescing opportunities — lanes that are many small ops rather than a few
large ones. Backed by `stats.fragmentation_report`. Merges each lane's events into
contiguous busy runs and ranks lanes, most fragmented first.

| Argument | Default | Meaning |
|----------|---------|---------|
| `merge_gap_ns` | `0.0` | Events separated by ≤ this gap count as one run (so near-contiguous regions score as solid). |
| `limit` | all | Keep only the N most-fragmented lanes. |

Per lane it returns: `n_events`, `n_segments` (contiguous runs), `ops_per_segment`
(events per solid-looking block — the headline fragmentation number),
`coalescable_ops` (`n_events − n_segments`, i.e. how many ops could be removed if
each run were one op), per-op duration stats (`mean/median/min/max_op_ns`),
`distinct_ops`, and `top_ops` (the most frequent mnemonics — *what* to fuse).

Example: an MTE2 lane reporting `n_events: 973, n_segments: 1, ops_per_segment:
973` looks like one clean block but is 973 tiny reads that could collapse to one
transfer.

---

## Feature gallery

Every feature below is a real command against the bundled sample, run from the
repo root, with its actual output. Images are written to `out/img/` (regenerate
any of them by re-running its command).

```bash
SAMPLE=OPPROF_20260707154004_XZPYWIDKXUKKJVTX
```

### `--list` / `describe_trace` — what's in the run

```bash
python3 -m traceviz.cli $SAMPLE --list
```

```json
{
  "units": ["core0.veccore0", "core1.veccore1", ... 24 total],
  "engines": ["SCALAR", "VECTOR", "FLOWCTRL", "MTE2", "MTE3", "ALL", "CACHEMISS", "FIXP", "MTE1", "CUBE"],
  "span_ns": 18.285,
  "n_events": 68112,
  "lanes": ["core0.cubecore0/ALL", ...]
}
```

### Overview — one lane per core, all 24 at once

```bash
python3 -m traceviz.cli $SAMPLE --aggregate unit --out out/img/overview.png
```

![overview](img/overview.png)

### Compare cores across metrics

```bash
python3 -m traceviz.cli $SAMPLE \
    --units core2.veccore1 core1.veccore0 \
    --engines SCALAR VECTOR MTE2 MTE3 \
    --out out/img/compare.png
```

![compare](img/compare.png)

### Fine-grained — a single core's single metric

The loop's "zoom in on one thing" case, e.g. *how does core7.veccore1's MTE2
access look?*

```bash
python3 -m traceviz.cli $SAMPLE --units core7.veccore1 --engines MTE2 --out out/img/finegrained.png
```

![finegrained](img/finegrained.png)

### Zoom to a time window

`--window T0 T1` in nanoseconds; the x-axis unit adapts to the span shown.

```bash
python3 -m traceviz.cli $SAMPLE --units core0.cubecore0 \
    --engines SCALAR CUBE MTE2 --window 26 32 --out out/img/zoom.png
```

![zoom](img/zoom.png)

### Overlap — how much two lanes run at the same time (JSON)

```bash
python3 -m traceviz.cli $SAMPLE --overlap core2.veccore1/VECTOR core1.veccore0/VECTOR
```

```json
{
  "lane_a": "core2.veccore1/VECTOR",
  "lane_b": "core1.veccore0/VECTOR",
  "busy_a_ns": 6.487, "busy_b_ns": 6.486,
  "overlap_ns": 6.467,
  "overlap_pct_of_a": 99.69, "overlap_pct_of_b": 99.71,
  "overlap_pct_of_window": 35.37
}
```

### Fragmentation — find coalescing opportunities (JSON)

```bash
python3 -m traceviz.cli $SAMPLE --engines MTE2 --fragmentation
```

```json
// most-fragmented lane first
{
  "lane": "core0.veccore0/MTE2",
  "n_events": 34,
  "n_segments": 8,
  "ops_per_segment": 4.25,
  "coalescable_ops": 26,
  "mean_op_ns": 0.407,
  "distinct_ops": 1,
  "top_ops": [{"op": "MOV_SRC_TO_DST_ALIGN", "count": 34}]
}
```

### CLI flags

| Flag | Meaning |
|------|---------|
| `path` (positional) | OPPROF dir, `simulator/` dir, or a `trace.json`. |
| `--out PATH` | Output PNG (default `timeline.png`). |
| `--units U ...` | Filter to these cores. |
| `--engines E ...` | Filter to these metrics. |
| `--window T0 T1` | Time window in nanoseconds. |
| `--aggregate {row,unit}` | Lane per core+metric, or lane per core. |
| `--width PX` | Output width (default 1600). |
| `--overlap A/E B/E` | Print overlap stats for two lanes and exit. |
| `--fragmentation` | Rank lanes by ops-per-run (coalescing opportunities) and exit. |
| `--merge-gap NS` | Events within NS count as one run (for `--fragmentation`). |
| `--list` | Print units/metrics/lanes and exit. |

---

## Python API

```python
from traceviz.loader import load_trace
from traceviz.render import render_timeline, pairwise_overlap

td = load_trace("OPPROF_20260707154004_XZPYWIDKXUKKJVTX")   # -> TraceData

summary = render_timeline(
    td, "out/query.png",
    units=["core2.veccore1", "core1.veccore0"],  # None = all
    engines=["VECTOR", "MTE3"],                   # None = all
    window=None,                                  # (t0_ns, t1_ns) to zoom
    aggregate="row",                              # or "unit"
)   # -> {out_path, window_ns, span_ns, time_unit, rows: [...]}

stats = pairwise_overlap(
    td, ("core2.veccore1", "VECTOR"), ("core1.veccore0", "VECTOR"),
)   # -> {overlap_ns, overlap_pct_of_a, ...}
```

## Package layout

| Module | Responsibility |
|--------|----------------|
| `model.py` | `TraceData` (columnar event store) and `InstrTable`. |
| `loader.py` | Parse `trace.json` and the `.bin` instruction table into a `TraceData`. |
| `render.py` | Scale-adaptive `render_timeline`, `pairwise_overlap`, and `busy_and_segments`. |
| `stats.py` | Fragmentation analysis (`lane_fragmentation`, `fragmentation_report`). |
| `cli.py` | Command-line entry point (`python -m traceviz.cli`). |
| `mcp_server.py` | MCP server exposing the four tools (`traceviz-mcp`). |
