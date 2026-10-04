"""Recover observed, still-open off-CPU intervals from the final map dump.

Unlike stack *keys*, bpftrace prints a saved stack map *value* as an indented
block after ``@_off_ustack[tid]:``. General parsers discard these scratch maps.
Here they are joined by TID and bounded by timestamps written in the probe,
before map printing starts. They are right-censored observations: the thread
has not resumed, so the full wait duration is unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .stacks import MapEntry, StackKey

_HEADER_RE = re.compile(
    r"^@(_off_(?:start|ustack|kstack|state|comm|window_start|window_end|min_us))"
    r"(?:\[([0-9]+)\])?:\s?(.*)$"
)


@dataclass
class PendingOffCpu:
    entries: List[MapEntry] = field(default_factory=list)
    by_state: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    window_ns: Optional[Tuple[int, int]] = None

    @property
    def total_us(self) -> int:
        return sum(entry.value for entry in self.entries)


def parse_pending_offcpu(text: str) -> PendingOffCpu:
    """Join only actual pending switch-outs with their captured stacks.

    Historical dumps lack the checkpoint metadata and remain unchanged.
    A stackless thread present only in a process snapshot is never added.
    Intervals that start after the final checkpoint are conservatively left
    out, rather than extended past the observed tracing window.
    """
    result = PendingOffCpu()
    scalar: Dict[str, Dict[str, str]] = {}
    stacks: Dict[str, Dict[str, StackKey]] = {}
    current: Optional[Tuple[str, str, List[str]]] = None

    def flush_stack() -> None:
        nonlocal current
        if current is not None:
            name, tid, frames = current
            stacks.setdefault(name, {})[tid] = StackKey(frames)
            current = None

    for line in text.splitlines():
        if line.startswith("@"):
            flush_stack()
            match = _HEADER_RE.match(line)
            if match is None:
                continue
            name, tid, value = match.groups()
            tid = tid or ""
            if name in ("_off_ustack", "_off_kstack"):
                current = (name, tid, [value.strip()] if value.strip() else [])
            else:
                scalar.setdefault(name, {})[tid] = value.strip()
        elif current is not None and line[:1].isspace() and line.strip():
            current[2].append(line.strip())
    flush_stack()

    def number(name: str, tid: str = "") -> Optional[int]:
        value = scalar.get(name, {}).get(tid)
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError:
            result.warnings.append(f"pending off-CPU @{name}[{tid}] is not an integer; skipped")
            return None
        return parsed

    # Absence of all new metadata identifies an old probe, not a corrupt one.
    if not any(name in scalar for name in ("_off_window_start", "_off_window_end", "_off_min_us")):
        return result
    start = number("_off_window_start")
    end = number("_off_window_end")
    threshold = number("_off_min_us")
    if start is None or end is None or threshold is None or start <= 0 or end < start or threshold < 0:
        result.warnings.append("pending off-CPU intervals omitted: tracing checkpoint metadata is incomplete or invalid")
        return result
    result.window_ns = (start, end)
    for tid in scalar.get("_off_start", {}):
        if not tid or int(tid) <= 0:
            result.warnings.append("pending off-CPU switch-out has no positive TID; skipped")
            continue
        switch_out = number("_off_start", tid)
        if switch_out is None or switch_out <= 0:
            continue
        observed_us = (end - max(start, switch_out)) // 1000
        if observed_us <= 0 or observed_us < threshold:
            continue
        kernel = stacks.get("_off_kstack", {}).get(tid)
        user = stacks.get("_off_ustack", {}).get(tid)
        state = number("_off_state", tid)
        comm = scalar.get("_off_comm", {}).get(tid)
        if kernel is None or user is None or state is None or comm is None:
            result.warnings.append(f"pending off-CPU tid {tid} has incomplete captured state; skipped")
            continue
        result.entries.append(MapEntry((kernel, user, comm, tid), observed_us))
        state_key = str(state)
        result.by_state[state_key] = result.by_state.get(state_key, 0) + observed_us
    return result
