"""Command-line entry point for rendering trace timelines.

Thin wrapper over the library so it can be driven from a shell today and wrapped
as an MCP tool later (the same argument set maps cleanly to a tool schema).

Examples:
    python -m traceviz.cli OPPROF_.../ --out out/overview.png --aggregate unit
    python -m traceviz.cli OPPROF_.../ --units core2.veccore1 core1.veccore0 \\
        --engines VECTOR MTE3 --out out/q.png
    python -m traceviz.cli OPPROF_.../ --overlap core2.veccore1/VECTOR core1.veccore0/VECTOR
"""

from __future__ import annotations

import argparse
import json
import sys

from .loader import load_trace
from .render import pairwise_overlap, render_timeline


def _parse_lane(s: str) -> tuple[str, str]:
    unit, _, engine = s.partition("/")
    if not engine:
        raise argparse.ArgumentTypeError(f"lane must be UNIT/ENGINE, got {s!r}")
    return unit, engine


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="traceviz", description=__doc__)
    ap.add_argument("path", help="OPPROF dir, simulator dir, or trace.json")
    ap.add_argument("--out", default="timeline.png", help="output PNG path")
    ap.add_argument("--units", nargs="*", help="filter to these units")
    ap.add_argument("--engines", nargs="*", help="filter to these engine classes")
    ap.add_argument("--window", nargs=2, type=float, metavar=("T0", "T1"),
                    help="time window in nanoseconds")
    ap.add_argument("--aggregate", choices=["row", "unit"], default="row",
                    help="'row' = per (unit,engine) lane; 'unit' = one lane per unit")
    ap.add_argument("--width", type=int, default=1600, help="output width in px")
    ap.add_argument("--title", default=None)
    ap.add_argument("--overlap", nargs=2, type=_parse_lane, metavar=("A", "B"),
                    help="print overlap stats for two UNIT/ENGINE lanes and exit")
    ap.add_argument("--list", action="store_true", help="list units/engines and exit")
    args = ap.parse_args(argv)

    td = load_trace(args.path)

    if args.list:
        print(json.dumps({
            "units": td.units, "engines": td.engines,
            "span_ns": td.span(), "n_events": td.n,
            "lanes": [f"{u}/{e}" for (u, e) in td.rows()],
        }, indent=2))
        return 0

    window = tuple(args.window) if args.window else None

    if args.overlap:
        stats = pairwise_overlap(td, args.overlap[0], args.overlap[1], window=window)
        print(json.dumps(stats, indent=2))
        return 0

    summary = render_timeline(
        td, args.out, units=args.units, engines=args.engines, window=window,
        width_px=args.width, aggregate=args.aggregate, title=args.title,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
