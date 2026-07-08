"""Parse profiler output into a :class:`TraceData`.

Two on-disk sources are understood:

* ``simulator/trace.json`` — the aggregated Chrome Trace Event file. ``pid`` is
  the unit name (``core2.veccore1``), ``tid`` is the engine class. This is the
  primary source because every unit shares one time axis.
* ``simulator/visualize_data.bin`` — a chunked container whose first chunk is a
  byte-identical copy of the aggregated trace, and whose trailer holds a
  per-instruction static-analysis table. We read only the trailer here (unique
  data); the trace copy is ignored in favour of the plain JSON.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

from .model import InstrTable, TraceData

# Chrome Trace Event Format records ts/dur at microsecond granularity. We store
# nanoseconds internally, so scale on load. (The file's displayTimeUnit is only a
# viewer display hint and does not change that ts/dur are microseconds.)
US_TO_NS = 1000.0


def _to_int(addr) -> int:
    """Parse a pc/address value that may be a hex string, int, or empty."""
    if addr is None or addr == "":
        return 0
    if isinstance(addr, int):
        return addr
    try:
        return int(addr, 16) if str(addr).startswith("0x") else int(addr)
    except (ValueError, TypeError):
        return 0


def find_trace_json(path: str | Path) -> Path:
    """Resolve a user-supplied path to the aggregated trace.json.

    Accepts the OPPROF root, the ``simulator`` dir, or the file itself.
    """
    p = Path(path)
    if p.is_file():
        return p
    for cand in (p / "trace.json", p / "simulator" / "trace.json"):
        if cand.is_file():
            return cand
    raise FileNotFoundError(f"No aggregated trace.json found under {path}")


def load_trace(path: str | Path, with_instr: bool = True) -> TraceData:
    """Load the aggregated trace (and, if present, the instruction table)."""
    trace_path = find_trace_json(path)
    with open(trace_path, "rb") as f:
        doc = json.loads(f.read())

    events = [e for e in doc.get("traceEvents", []) if e.get("ph") == "X"]

    units: dict[str, int] = {}
    engines: dict[str, int] = {}
    ops: dict[str, int] = {}

    n = len(events)
    unit_i = np.empty(n, dtype=np.int32)
    engine_i = np.empty(n, dtype=np.int32)
    op_i = np.empty(n, dtype=np.int32)
    ts = np.empty(n, dtype=np.float64)
    dur = np.empty(n, dtype=np.float64)
    pc = np.empty(n, dtype=np.int64)

    def intern(d: dict, key: str) -> int:
        i = d.get(key)
        if i is None:
            i = len(d)
            d[key] = i
        return i

    for k, e in enumerate(events):
        unit_i[k] = intern(units, str(e.get("pid", "")))
        engine_i[k] = intern(engines, str(e.get("tid", "")))
        op_i[k] = intern(ops, str(e.get("name", "")))
        ts[k] = float(e.get("ts", 0.0)) * US_TO_NS
        dur[k] = float(e.get("dur", 0.0)) * US_TO_NS
        pc[k] = _to_int(e.get("args", {}).get("pc_addr"))

    instr = None
    if with_instr:
        bin_path = trace_path.parent / "visualize_data.bin"
        if bin_path.is_file():
            try:
                instr = load_instr_table(bin_path)
            except Exception:
                instr = None  # trailer is a bonus; never fail the whole load on it

    def keys_sorted(d: dict) -> list[str]:
        out = [""] * len(d)
        for key, i in d.items():
            out[i] = key
        return out

    return TraceData(
        units=keys_sorted(units),
        engines=keys_sorted(engines),
        ops=keys_sorted(ops),
        unit_i=unit_i,
        engine_i=engine_i,
        op_i=op_i,
        ts=ts,
        dur=dur,
        pc=pc,
        instr=instr,
    )


def _read_chunks(raw: bytes):
    """Yield the JSON payload of each length-prefixed chunk in a .bin file.

    Layout per chunk: uint64 little-endian length, 2 flag bytes, 2 marker bytes
    ("ZZ"/"Z\\0" style), then `length` bytes of UTF-8 JSON.
    """
    off = 0
    total = len(raw)
    while off + 12 <= total:
        length = struct.unpack_from("<Q", raw, off)[0]
        payload_start = off + 12
        payload_end = payload_start + length
        if length == 0 or payload_end > total:
            break
        yield raw[payload_start:payload_end]
        off = payload_end


def load_instr_table(bin_path: str | Path) -> InstrTable:
    """Parse the per-instruction static table from the .bin trailer chunk."""
    raw = Path(bin_path).read_bytes()
    meta = None
    for payload in _read_chunks(raw):
        # The trace chunk starts with {"displayTimeUnit"...; the metadata chunk
        # with {"Cores"...  We only want the latter.
        head = payload[:32]
        if b'"Cores"' in head or b'"Instructions"' in head:
            meta = json.loads(payload.decode("utf-8", "replace"))
            break
    if meta is None:
        raise ValueError("No instruction/metadata chunk found in .bin")

    cores = meta.get("Cores", [])
    by_addr: dict[int, dict] = {}
    for row in meta.get("Instructions", []):
        addr = _to_int(row.get("Address"))
        by_addr[addr] = row
    return InstrTable(cores=cores, by_addr=by_addr)
