"""MCP server exposing the trace timeline renderer to an LLM.

Intended workflow: the model edits code, runs the profiler (which writes a fresh
``OPPROF_*`` directory), then analyses that run with these tools, acts on the
findings, and repeats. Because every run is a new directory, ``path`` is required
on every call — there is deliberately no default trace, so you can never
accidentally analyse a stale run.

Install into Claude Code:

    claude mcp add traceviz -- uvx --from git+https://github.com/<you>/<repo> traceviz-mcp

Tools:
  * ``describe_trace``  — list the cores, metrics and time span in a run.
  * ``render_timeline`` — image of chosen cores × metrics, optional time window.
  * ``overlap``         — how much two lanes are busy simultaneously.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Annotated

from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field

from .loader import find_trace_json, load_trace
from .render import pairwise_overlap, render_timeline

mcp = FastMCP(
    "traceviz",
    instructions=(
        "Analyse an NPU profiler (OPPROF) run while iterating on code. After you "
        "run the profiler, pass the resulting OPPROF_* directory as `path` (it is "
        "required on every call, since each run is a new directory). Call "
        "describe_trace first to learn the valid core and metric names and the "
        "time span, then render_timeline for an image or overlap for a "
        "busy-overlap number. Cores look like 'core2.veccore1'; metrics are "
        "engine/pipe classes like SCALAR, VECTOR, CUBE, MTE2, MTE3. Times are ns."
    ),
)

# Parsed traces are cached per trace file and invalidated when the file changes,
# so re-running the profiler into a reused path is never served stale.
_CACHE: dict[str, tuple[float, object]] = {}


def _load(path: str):
    if not path:
        raise ValueError(
            "`path` is required: pass the OPPROF_* directory of the profiler run "
            "you want to analyse."
        )
    trace_json = find_trace_json(os.path.expanduser(path))
    key = str(trace_json.resolve())
    mtime = trace_json.stat().st_mtime
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    td = load_trace(trace_json)
    _CACHE[key] = (mtime, td)
    return td


def _window(td, start_ns, end_ns):
    if start_ns is None and end_ns is None:
        return None
    return (
        start_ns if start_ns is not None else td.t_min,
        end_ns if end_ns is not None else td.t_max,
    )


def _summary_text(summary: dict) -> str:
    lines = [
        f"Timeline · {len(summary['rows'])} lanes · "
        f"span {summary['span_ns']:.3f} ns (axis in {summary['time_unit']})",
    ]
    for r in sorted(summary["rows"], key=lambda x: -x["busy_pct"]):
        lines.append(
            f"  {r['unit']}/{r['engine']}: busy {r['busy_pct']:.1f}% "
            f"({r['busy_ns']:.3f} ns, {r['n_events']} events)"
        )
    return "\n".join(lines)


@mcp.tool()
def describe_trace(
    path: Annotated[str, Field(description="The OPPROF_* directory (or its simulator dir / trace.json) of the run to analyse.")],
) -> str:
    """List the cores, metrics (engine/pipe classes) and time span in a run.

    Call this first after a profiler run so you know which `cores` and `metrics`
    names are valid and what nanosecond range the run covers.
    """
    td = _load(path)
    return json.dumps(
        {
            "cores": td.units,
            "metrics": td.engines,
            "time_start_ns": td.t_min,
            "time_end_ns": td.t_max,
            "span_ns": td.span(),
            "n_events": td.n,
            "lanes": [f"{u}/{e}" for (u, e) in td.rows()],
        },
        indent=2,
    )


@mcp.tool()
def render_timeline_image(
    path: Annotated[str, Field(description="The OPPROF_* directory of the run to analyse.")],
    cores: Annotated[list[str] | None, Field(description="Cores to show, e.g. ['core0.cubecore0','core1.veccore0']. Omit for all.")] = None,
    metrics: Annotated[list[str] | None, Field(description="Engine/pipe classes to show, e.g. ['VECTOR','CUBE','MTE2']. Omit for all.")] = None,
    start_ns: Annotated[float | None, Field(description="Window start in nanoseconds (optional).")] = None,
    end_ns: Annotated[float | None, Field(description="Window end in nanoseconds (optional).")] = None,
    aggregate: Annotated[str, Field(description="'row' = one lane per (core, metric); 'unit' = one lane per core.")] = "row",
) -> list:
    """Render a scale-adaptive timeline of the chosen cores and metrics.

    Returns a text summary (per-lane busy %) followed by a PNG image. The x-axis
    unit (ns/us/ms/s) is chosen automatically from the window. Idle time reads as
    white; busy time is colored by metric. Give `start_ns`/`end_ns` to zoom into
    a hotspot.
    """
    td = _load(path)
    window = _window(td, start_ns, end_ns)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        out = fh.name
    try:
        summary = render_timeline(
            td, out, units=cores, engines=metrics, window=window,
            aggregate=aggregate,
        )
        data = open(out, "rb").read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass
    return [_summary_text(summary), Image(data=data, format="png")]


@mcp.tool()
def overlap(
    path: Annotated[str, Field(description="The OPPROF_* directory of the run to analyse.")],
    lane_a: Annotated[str, Field(description="First lane as 'core/metric', e.g. 'core2.veccore1/VECTOR'.")],
    lane_b: Annotated[str, Field(description="Second lane as 'core/metric', e.g. 'core1.veccore0/VECTOR'.")],
    start_ns: Annotated[float | None, Field(description="Window start in nanoseconds (optional).")] = None,
    end_ns: Annotated[float | None, Field(description="Window end in nanoseconds (optional).")] = None,
) -> str:
    """Quantify how much two lanes are busy at the same time.

    Each lane is 'core/metric' (e.g. 'core2.veccore1/VECTOR'). Returns overlap
    time and overlap as a percentage of each lane's own busy time.
    """
    td = _load(path)

    def parse(s: str):
        u, _, e = s.partition("/")
        if not e:
            raise ValueError(f"lane must be 'core/metric', got {s!r}")
        return (u, e)

    window = _window(td, start_ns, end_ns)
    stats = pairwise_overlap(td, parse(lane_a), parse(lane_b), window=window)
    return json.dumps(stats, indent=2)


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
